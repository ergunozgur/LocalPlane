"""Ingestion: the point where a reading becomes LocalPlane's durable truth."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from localplane.agent.service import AgentService
from localplane.backend.db.repositories import ObjectRepository
from localplane.backend.domain.identity import OBJECT_KIND_NETWORK_INTERFACE
from localplane.backend.domain.states import HealthState, ManagementState
from localplane.backend.ingest import Ingestor
from tests.conftest import FakeRunner


@pytest.fixture
def agent_service(
    fake_root: Path, populated_sysfs: Path, working_runner: FakeRunner, absent_docker: Path
):
    return AgentService(
        root=fake_root,
        sysfs_net=populated_sysfs,
        runner=working_runner,
        docker_socket=absent_docker,
    )


@pytest.fixture
def ingested(database, agent_service):
    ingestor = Ingestor(database)
    ingestor.ingest_handshake(agent_service.handle("agent.hello", {}))
    result = ingestor.ingest_network_sweep(
        agent_service.handle("network.observe_interfaces", {})
    )
    return ingestor, result


# ------------------------------------------------------------------------------ handshake


def test_the_handshake_records_the_host_and_the_agent(database, agent_service):
    ingestor = Ingestor(database)
    host_id, agent_instance_id = ingestor.ingest_handshake(agent_service.handle("agent.hello", {}))

    host = ingestor.hosts.get(host_id)
    # The configured hostname comes from the fixture root; the running one is this
    # process's real host, because that is what it actually is.
    assert host.configured_hostname == "fixture-host"
    assert host.hostname == os.uname().nodename
    assert host.os_id == "debian"
    assert host.identity_basis == "machine_id"

    agent = ingestor.agents.most_recent()
    assert agent.agent_instance_id == agent_instance_id
    assert agent.transport == "af_unix"
    assert agent.process_isolated is True


def test_the_recorded_privilege_is_the_one_the_agent_actually_holds(database, agent_service):
    """A contract seam is not a privilege-separation claim, and is not recorded as one."""
    ingestor = Ingestor(database)
    ingestor.ingest_handshake(agent_service.handle("agent.hello", {}))
    assert ingestor.agents.most_recent().privilege == "unprivileged"


def test_capabilities_are_recorded_with_their_status(database, agent_service):
    ingestor = Ingestor(database)
    _, agent_instance_id = ingestor.ingest_handshake(agent_service.handle("agent.hello", {}))
    capabilities = {c.capability: c for c in ingestor.agents.capabilities(agent_instance_id)}
    assert capabilities["network.observe"].status == "available"
    assert capabilities["network.observe"].mutating is False


def test_capabilities_are_replaced_not_accumulated(database, agent_service, fake_root, populated_sysfs):
    """A capability that stopped being reported must not linger claiming 'available'."""
    ingestor = Ingestor(database)
    _, agent_instance_id = ingestor.ingest_handshake(agent_service.handle("agent.hello", {}))
    with database.transaction():
        ingestor.agents.replace_capabilities(
            agent_instance_id,
            [
                {
                    "capability": "network.observe",
                    "version": 1,
                    "status": "unavailable",
                    "mutating": False,
                    "summary": "s",
                    "reason": "sysfs_unreadable",
                    "detail": {},
                    "discovered_at": "2026-01-01T00:00:00+00:00",
                }
            ],
        )
    capabilities = ingestor.agents.capabilities(agent_instance_id)
    assert len(capabilities) == 1
    assert capabilities[0].status == "unavailable"


def test_the_handshake_is_idempotent(database, agent_service):
    ingestor = Ingestor(database)
    first = ingestor.ingest_handshake(agent_service.handle("agent.hello", {}))
    second = ingestor.ingest_handshake(agent_service.handle("agent.hello", {}))
    assert first == second
    assert len(database.query("SELECT * FROM hosts")) == 1
    assert len(database.query("SELECT * FROM agent_instances")) == 1


# --------------------------------------------------------------------------------- sweep


def test_a_sweep_persists_one_object_per_interface(ingested):
    ingestor, result = ingested
    assert result.status == "ok"
    assert result.object_count == 9
    objects = ingestor.objects.list_by_kind(result.host_id, OBJECT_KIND_NETWORK_INTERFACE)
    assert {o.display_name for o in objects} == {
        "lo", "eth0", "eth1", "wlan0", "wwan0", "tun0", "docker0", "veth0", "odd0"
    }


def test_the_backend_owns_the_health_judgement(ingested):
    ingestor, result = ingested
    health = {
        o.display_name: (o.observation.health_state, o.observation.health_reason)
        for o in ingestor.objects.list_by_kind(result.host_id, OBJECT_KIND_NETWORK_INTERFACE)
    }
    assert health["eth0"] == (HealthState.INACTIVE, "no_carrier")
    assert health["eth1"] == (HealthState.HEALTHY, "operstate_up")
    assert health["wlan0"] == (HealthState.INACTIVE, "admin_down")
    assert health["tun0"] == (HealthState.HEALTHY, "carrier_up_operstate_unknown")
    assert health["odd0"] == (HealthState.DEGRADED, "carrier_up_operstate_down")


def test_the_backend_owns_the_management_stance(ingested):
    ingestor, result = ingested
    stance = {
        o.display_name: o.management_state
        for o in ingestor.objects.list_by_kind(result.host_id, OBJECT_KIND_NETWORK_INTERFACE)
    }
    assert stance["lo"] == ManagementState.OBSERVE_ONLY
    assert stance["veth0"] == ManagementState.OBSERVE_ONLY
    assert stance["tun0"] == ManagementState.OBSERVE_ONLY
    assert stance["eth0"] == ManagementState.OBSERVED
    assert ManagementState.MANAGED not in set(stance.values())


def test_identity_evidence_is_stored_with_the_object(ingested):
    ingestor, result = ingested
    objects = {
        o.display_name: o
        for o in ingestor.objects.list_by_kind(result.host_id, OBJECT_KIND_NETWORK_INTERFACE)
    }
    assert objects["eth0"].identity_basis == "permanent_mac"
    assert objects["eth0"].identity_value == "02:00:00:00:00:10"
    assert objects["wwan0"].identity_basis == "device_path"
    assert objects["docker0"].identity_basis == "kernel_name"
    assert objects["docker0"].identity_confidence == "low"


def test_unknown_values_survive_the_round_trip_as_null(ingested):
    """The whole chain — sysfs to kernel to JSON to SQLite and back — keeps None as None."""
    ingestor, result = ingested
    facts = {
        o.display_name: o.observation.facts
        for o in ingestor.objects.list_by_kind(result.host_id, OBJECT_KIND_NETWORK_INTERFACE)
    }
    assert facts["eth0"]["speed_mbps"] is None
    assert facts["eth0"]["duplex"] is None
    assert facts["wlan0"]["carrier"] is None
    assert facts["eth0"]["addresses"] == []
    assert facts["eth1"]["speed_mbps"] == 1000


def test_the_sweep_record_explains_what_happened(ingested):
    ingestor, result = ingested
    sweep = ingestor.sweeps.latest(result.host_id)
    assert sweep.sweep_id == result.sweep_id
    assert sweep.status == "ok"
    assert sweep.object_count == 9
    assert sweep.provider == "linux.network"
    assert sweep.missing == []
    assert sweep.issues == []


def test_evidence_is_stored_and_retrievable(ingested):
    ingestor, result = ingested
    eth0 = next(
        o
        for o in ingestor.objects.list_by_kind(result.host_id, OBJECT_KIND_NETWORK_INTERFACE)
        if o.display_name == "eth0"
    )
    _, evidence = ingestor.objects.evidence(eth0.object_id)
    assert evidence["sysfs"]["flags"] == "0x1003"
    assert evidence["commands"][0]["argv"][0] == "ip"


# ------------------------------------------------------------------------- re-observation


def test_re_observing_appends_rather_than_overwriting(database, agent_service, ingested):
    ingestor, first = ingested
    second = ingestor.ingest_network_sweep(agent_service.handle("network.observe_interfaces", {}))

    objects = ingestor.objects.list_by_kind(first.host_id, OBJECT_KIND_NETWORK_INTERFACE)
    assert len(objects) == 9, "identity is stable, so no duplicate objects"

    eth0 = next(o for o in objects if o.display_name == "eth0")
    assert ingestor.sweeps.observation_count(eth0.object_id) == 2
    assert eth0.observation.sweep_id == second.sweep_id, "the newest observation wins"


def test_first_seen_is_never_overwritten(database, agent_service, ingested):
    ingestor, first = ingested
    before = {
        o.object_id: o.first_seen_at
        for o in ingestor.objects.list_by_kind(first.host_id, OBJECT_KIND_NETWORK_INTERFACE)
    }
    ingestor.ingest_network_sweep(agent_service.handle("network.observe_interfaces", {}))
    after = {
        o.object_id: (o.first_seen_at, o.last_seen_at)
        for o in ingestor.objects.list_by_kind(first.host_id, OBJECT_KIND_NETWORK_INTERFACE)
    }
    for object_id, first_seen in before.items():
        assert after[object_id][0] == first_seen
        assert after[object_id][1] >= first_seen


def test_a_renamed_interface_keeps_its_object_and_updates_its_name(
    database, agent_service, ingested, populated_sysfs
):
    """Identity rests on the permanent MAC, so a rename is the same object."""
    ingestor, first = ingested
    eth0 = next(
        o
        for o in ingestor.objects.list_by_kind(first.host_id, OBJECT_KIND_NETWORK_INTERFACE)
        if o.display_name == "eth0"
    )
    (populated_sysfs / "eth0").rename(populated_sysfs / "enp3s0")

    ingestor.ingest_network_sweep(agent_service.handle("network.observe_interfaces", {}))
    renamed = ingestor.objects.get(eth0.object_id)
    assert renamed.display_name == "enp3s0"
    assert len(ingestor.objects.list_by_kind(first.host_id, OBJECT_KIND_NETWORK_INTERFACE)) == 9


def test_an_interface_that_disappears_is_kept_and_marked_not_re_read(
    database, agent_service, ingested, populated_sysfs
):
    """Deleting the object would erase history; claiming it was re-read would be false."""
    import shutil

    ingestor, first = ingested
    shutil.rmtree(populated_sysfs / "veth0")
    second = ingestor.ingest_network_sweep(agent_service.handle("network.observe_interfaces", {}))

    objects = ingestor.objects.list_by_kind(first.host_id, OBJECT_KIND_NETWORK_INTERFACE)
    veth0 = next(o for o in objects if o.display_name == "veth0")
    assert veth0.observation.sweep_id == first.sweep_id
    assert veth0.observation.sweep_id != second.sweep_id
    assert second.object_count == 8


# ---------------------------------------------------------------------------- truthfulness


def test_a_degraded_sweep_is_recorded_as_partial(
    database,
    fake_root,
    populated_sysfs,
    absent_docker: Path,
):
    service = AgentService(
        root=fake_root,
        sysfs_net=populated_sysfs,
        runner=FakeRunner({}),
        docker_socket=absent_docker,
    )
    ingestor = Ingestor(database)
    ingestor.ingest_handshake(service.handle("agent.hello", {}))
    result = ingestor.ingest_network_sweep(service.handle("network.observe_interfaces", {}))

    assert result.status == "partial"
    sweep = ingestor.sweeps.latest(result.host_id)
    assert sweep.status == "partial"
    assert {issue["code"] for issue in sweep.issues} == {"command_failed"}
    objects = ingestor.objects.list_by_kind(result.host_id, OBJECT_KIND_NETWORK_INTERFACE)
    assert all(o.observation.facts["addresses"] is None for o in objects)


def test_requested_interfaces_that_do_not_exist_are_recorded_as_missing(database, agent_service):
    ingestor = Ingestor(database)
    ingestor.ingest_handshake(agent_service.handle("agent.hello", {}))
    result = ingestor.ingest_network_sweep(
        agent_service.handle("network.observe_interfaces", {"names": ["eth0", "ghost0"]})
    )
    assert result.missing == ["ghost0"]
    assert result.object_count == 1
    assert ingestor.sweeps.latest(result.host_id).missing == ["ghost0"]


def test_no_object_is_created_for_a_missing_interface(database, agent_service):
    ingestor = Ingestor(database)
    ingestor.ingest_handshake(agent_service.handle("agent.hello", {}))
    result = ingestor.ingest_network_sweep(
        agent_service.handle("network.observe_interfaces", {"names": ["ghost0"]})
    )
    assert ingestor.objects.list_by_kind(result.host_id, OBJECT_KIND_NETWORK_INTERFACE) == []
    assert ingestor.sweeps.latest(result.host_id).status == "ok"


def test_a_sweep_lands_whole_or_not_at_all(database, agent_service, monkeypatch):
    ingestor = Ingestor(database)
    host_id, _ = ingestor.ingest_handshake(agent_service.handle("agent.hello", {}))
    payload = agent_service.handle("network.observe_interfaces", {})

    calls = {"n": 0}
    original = ObjectRepository.upsert

    def fail_partway(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 4:
            raise RuntimeError("interrupted")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ObjectRepository, "upsert", fail_partway)
    with pytest.raises(RuntimeError):
        ingestor.ingest_network_sweep(payload)

    assert ingestor.objects.list_by_kind(host_id, OBJECT_KIND_NETWORK_INTERFACE) == []
    assert ingestor.sweeps.latest(host_id) is None
    assert database.query("SELECT * FROM observations") == []
