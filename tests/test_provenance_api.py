"""Ownership end to end: agent, ingest, store, API, adopt, and the claims that follow.

The estate below is a fixture host with four links and three provider daemons that a test
can turn on, break, or take away between observations — which is how the interesting cases
are reached. A Docker daemon that becomes readable *after* an object was adopted is not an
exotic scenario; it is what happens the first time somebody adds a user to the docker group,
and it is the one case where LocalPlane can find itself managing something it does not own.

Nothing here touches the machine running the tests. The Docker socket is a real AF_UNIX
server the test starts itself, and the other two providers answer through the command seam.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import pytest
from fastapi.testclient import TestClient

from localplane.agent.providers.base import CommandResult
from localplane.agent.providers.network_manager import GENERAL_ARGV
from localplane.agent.server import AgentServer
from localplane.agent.service import AgentService
from localplane.backend.app import create_app
from localplane.backend.config import Settings
from localplane.backend.db.repositories import ObjectRecord
from localplane.backend.domain.findings import FINDING_TYPE_OWNERSHIP_CONFLICT, finding_key
from localplane.backend.domain.identity import OBJECT_KIND_NETWORK_INTERFACE
from localplane.backend.domain.provenance import OwnershipRelation, OwnershipState
from localplane.backend.domain.states import ManagementState
from localplane.backend.ingest import Ingestor
from localplane.backend.management import ManagementRefused, ManagementService
from localplane.backend.provenance import ProvenanceService
from tests.conftest import (
    FakeRunner,
    docker_network,
    json_result,
    nmcli_devices,
    tailscale_status,
    write_interface,
)

DOCKER0_GATEWAY = "172.19.0.1"
TAILSCALE_ADDRESS = "100.64.0.10"
MONITORING_NETWORK = "d" * 64


# --------------------------------------------------------------------------- the estate


@dataclass
class Estate:
    """A fixture host whose providers a test can start, break and take away."""

    database: Any
    sysfs: Path
    runner: FakeRunner
    docker_socket: Path
    fake_root: Path
    daemons: Any
    service: AgentService = None
    ingestor: Ingestor = None
    management: ManagementService = None
    provenance: ProvenanceService = None
    host_id: str = ""

    def build_agent(self) -> AgentService:
        """A fresh agent process's worth of state, re-probing its capabilities.

        Handshakes if the store is already open, exactly as the coordinator does before
        every observation — a restarted agent is a new instance, and a sweep may only
        reference one the store has seen.
        """
        self.service = AgentService(
            root=self.fake_root,
            sysfs_net=self.sysfs,
            runner=self.runner,
            docker_socket=self.docker_socket,
        )
        if self.ingestor is not None:
            self.ingestor.ingest_handshake(self.service.handle("agent.hello", {}))
        return self.service

    def start_docker(self, *networks: dict[str, Any]) -> Any:
        return self.daemons(list(networks), name=self.docker_socket.name)

    def break_provider(self, argv: tuple[str, ...], returncode: int = 1) -> None:
        self.runner.responses[argv] = CommandResult(argv, returncode, "", "not answering")

    def observe(self) -> Any:
        payload = self.service.handle("network.observe_interfaces", {})
        providers = self.service.handle("network.observe_providers", {})["providers"]
        result = self.ingestor.ingest_network_sweep({**payload, "providers": providers})
        self.host_id = result.host_id
        return result

    def observe_without_providers(self) -> Any:
        """A sweep from an agent that cannot answer the provider operation at all."""
        payload = self.service.handle("network.observe_interfaces", {})
        result = self.ingestor.ingest_network_sweep(
            {
                **payload,
                "providers": None,
                "provider_error": {
                    "code": "capability_unavailable",
                    "message": "network.providers.observe is not available on this agent",
                    "detail": {"capability": "network.providers.observe"},
                },
            }
        )
        self.host_id = result.host_id
        return result

    def object_named(self, name: str) -> ObjectRecord:
        for record in self.ingestor.objects.list_by_kind(
            self.host_id, OBJECT_KIND_NETWORK_INTERFACE
        ):
            if record.display_name == name:
                return record
        raise AssertionError(f"no object named {name}")

    def ownership_of(self, name: str):
        return self.provenance.for_object(self.object_named(name))

    def eligibility_of(self, name: str):
        record = self.object_named(name)
        return self.provenance.eligibility(record, self.provenance.for_object(record))


def _runner(sysfs_addresses: Sequence[dict[str, Any]]) -> FakeRunner:
    from localplane.agent.providers.linux_network import ADDR_ARGV, LINK_ARGV

    return FakeRunner(
        {
            LINK_ARGV: json_result(
                LINK_ARGV,
                [
                    {"ifindex": 3, "ifname": "docker0", "linkinfo": {"info_kind": "bridge"}},
                    {"ifindex": 4, "ifname": "tailscale0", "linkinfo": {"info_kind": "tun"}},
                ],
            ),
            ADDR_ARGV: json_result(ADDR_ARGV, list(sysfs_addresses)),
            **nmcli_devices(
                [
                    ("lo", "loopback", "connected (externally)", "lo", "uuid-lo"),
                    ("eth0", "ethernet", "unavailable", "", ""),
                    ("docker0", "bridge", "connected (externally)", "docker0", "uuid-docker"),
                    ("tailscale0", "tun", "unmanaged", "", ""),
                ]
            ),
            **tailscale_status(addresses=(TAILSCALE_ADDRESS,)),
        }
    )


@pytest.fixture
def estate(database, fake_root: Path, sysfs_net: Path, tmp_path: Path, docker_daemon) -> Estate:
    """Four links: loopback, an adoptable ethernet, a Docker-shaped bridge, a tunnel.

    None of them is *named* in a way LocalPlane may use, and the bridge is deliberately not
    called ``docker0`` in Docker's own records — the only thing that could tie it to Docker
    is the gateway address Docker declares.
    """
    write_interface(
        sysfs_net, "lo", ifindex=1, address="00:00:00:00:00:00", arphrd="772", flags="0x9",
        operstate="unknown", carrier="1", mtu="65536", speed=None, duplex=None,
    )
    write_interface(
        sysfs_net, "eth0", ifindex=2, address="02:00:00:00:00:10", flags="0x1003",
        operstate="up", carrier="1", mtu="1500", device="fd580000.ethernet",
        subsystem="platform",
    )
    write_interface(
        sysfs_net, "docker0", ifindex=3, address="02:00:00:00:00:13", addr_assign_type="3",
        flags="0x1003", operstate="up", carrier="1", mtu="1500", devtype="bridge",
        bridge=True, speed=None, duplex=None,
    )
    write_interface(
        sysfs_net, "tailscale0", ifindex=4, address="00:00:00:00:00:00", arphrd="65534",
        flags="0x1091", operstate="unknown", carrier="1", mtu="1280", tun=True,
        speed=None, duplex=None,
    )

    runner = _runner(
        [
            {"ifindex": 1, "ifname": "lo", "addr_info": []},
            {"ifindex": 2, "ifname": "eth0", "addr_info": []},
            {"ifindex": 3, "ifname": "docker0", "addr_info": [
                {"family": "inet", "local": DOCKER0_GATEWAY, "prefixlen": 16, "scope": "global"}]},
            {"ifindex": 4, "ifname": "tailscale0", "addr_info": [
                {"family": "inet", "local": TAILSCALE_ADDRESS, "prefixlen": 32,
                 "scope": "global"}]},
        ]
    )
    provenance = ProvenanceService(database)
    management = ManagementService(database, freshness_ttl_s=60.0, provenance=provenance)
    estate = Estate(
        database=database,
        sysfs=sysfs_net,
        runner=runner,
        docker_socket=tmp_path / "docker.sock",
        fake_root=fake_root,
        daemons=docker_daemon,
        ingestor=Ingestor(database, management),
        management=management,
        provenance=provenance,
    )
    estate.build_agent()
    return estate


def monitoring_network() -> dict[str, Any]:
    return docker_network(
        MONITORING_NETWORK,
        "monitoring_default",
        gateway=DOCKER0_GATEWAY,
        subnet="172.19.0.0/16",
        compose_project="monitoring",
    )


@pytest.fixture
def client(estate: Estate, tmp_path: Path) -> TestClient:
    """The API over a real agent socket, against the estate's store."""
    estate.start_docker(monitoring_network())
    estate.build_agent()
    server = AgentServer(tmp_path / "run" / "agent.sock", estate.service)
    server.serve_in_thread()
    settings = Settings(
        database_path=Path(estate.database.path),
        agent_socket=tmp_path / "run" / "agent.sock",
        agent_timeout_s=10,
        freshness_ttl_s=60,
        log_level="WARNING",
        observe_on_startup=True,
    )
    with TestClient(create_app(settings, estate.database)) as test_client:
        yield test_client
    server.shutdown()
    server.server_close()


def interfaces_by_name(client: TestClient) -> dict[str, Any]:
    body = client.get("/api/v1/network/interfaces").json()
    return {i["name"]: i for i in body["interfaces"]}


# ------------------------------------------------------------------ the ownership answer


def test_a_docker_bridge_is_attributed_without_its_name_being_used(estate: Estate):
    estate.start_docker(monitoring_network())
    estate.observe()

    provenance = estate.ownership_of("docker0")
    claim = provenance.owner_for(OwnershipRelation.CONFIGURED_BY)
    assert provenance.state is OwnershipState.ATTRIBUTED
    assert claim.owner.provider == "docker"
    assert claim.owner.instance == MONITORING_NETWORK
    assert claim.owner.label == "monitoring_default"
    assert claim.reason == "docker_ipam_gateway_on_link"
    assert claim.evidence[0].detail["gateway"] == DOCKER0_GATEWAY


def test_ownership_does_not_change_the_management_state(estate: Estate):
    """The axes stay apart: a Docker bridge is still an object LocalPlane watches."""
    estate.start_docker(monitoring_network())
    estate.observe()
    assert estate.object_named("docker0").management_state == ManagementState.OBSERVED
    assert estate.object_named("docker0").management_reason == "management_candidate"
    assert estate.eligibility_of("docker0").eligible is False


def test_a_tunnel_is_attributed_only_by_the_daemons_addresses(estate: Estate):
    estate.start_docker(monitoring_network())
    estate.observe()
    claim = estate.ownership_of("tailscale0").owner_for(OwnershipRelation.CREATED_BY)
    assert claim.owner.provider == "tailscale"
    assert claim.evidence[0].detail["matched_addresses"] == [TAILSCALE_ADDRESS]


def test_an_ordinary_interface_is_the_kernels_and_stays_adoptable(estate: Estate):
    estate.start_docker(monitoring_network())
    estate.observe()
    provenance = estate.ownership_of("eth0")
    assert provenance.reason == "host_kernel_only"
    assert provenance.owner_for(OwnershipRelation.CONFIGURED_BY) is None
    eligibility = estate.eligibility_of("eth0")
    assert eligibility.eligible is True
    assert eligibility.evidence_gaps == ()


# --------------------------------------------------------------- providers that fail


def test_an_unreadable_docker_does_not_break_the_network_observation(estate: Estate):
    """No Docker daemon at all: every link is still observed, judged and stored."""
    result = estate.observe()
    assert result.status == "ok"
    assert result.object_count == 4
    assert {p["provider"]: p["status"] for p in result.providers} == {
        "docker": "absent", "networkmanager": "ok", "tailscale": "ok"
    }
    assert estate.object_named("docker0").observation.health_state == "healthy"


def test_a_provider_that_refuses_produces_uncertainty_not_a_verdict(estate: Estate):
    estate.break_provider(GENERAL_ARGV, returncode=8)
    estate.build_agent()
    result = estate.observe()

    assert {p["provider"]: p["status"] for p in result.providers}["networkmanager"] == (
        "unavailable"
    )
    provenance = estate.ownership_of("eth0")
    assert "networkmanager.devices" in provenance.gaps
    # A gap does not become an owner, and does not block adoption on its own.
    assert estate.eligibility_of("eth0").eligible is True
    assert "networkmanager.devices" in estate.eligibility_of("eth0").evidence_gaps


def test_an_agent_that_cannot_answer_at_all_leaves_the_sweep_intact(estate: Estate):
    result = estate.observe_without_providers()
    assert result.object_count == 4
    assert result.providers == []
    assert [i["code"] for i in result.issues] == ["capability_unavailable"]
    provenance = estate.ownership_of("docker0")
    assert provenance.state is OwnershipState.UNKNOWN
    assert provenance.reason == "evidence_incomplete"


def test_a_reading_that_failed_is_stored_without_records(estate: Estate):
    estate.break_provider(GENERAL_ARGV, returncode=8)
    estate.build_agent()
    result = estate.observe()
    rows = {
        r["provider"]: r
        for r in estate.database.query(
            "SELECT * FROM provider_observations WHERE sweep_id = ?", (result.sweep_id,)
        )
    }
    assert rows["networkmanager"]["status"] == "unavailable"
    assert rows["networkmanager"]["reason"] == "nmcli_failed"
    assert rows["networkmanager"]["records"] == "[]"
    assert json.loads(rows["tailscale"]["records"])


def test_the_store_refuses_a_failed_reading_that_carries_records(estate: Estate):
    """The invariant is in the schema, so no code path can put an answer behind a failure."""
    result = estate.observe()
    with pytest.raises(sqlite3.IntegrityError):
        with estate.database.transaction():
            estate.database.connection.execute(
                """
                INSERT INTO provider_observations (
                    provider_observation_id, sweep_id, host_id, provider, source, status,
                    reason, method, observed_at, received_at, records, detail
                ) VALUES ('pobs_x', ?, ?, 'docker', 'docker.networks', 'unavailable',
                          'permission_denied', 'unix_socket_http', 'now', 'now',
                          '[{"network_id":"x"}]', '{}')
                """,
                (result.sweep_id, result.host_id),
            )


def test_the_store_refuses_a_failed_reading_that_does_not_say_why(estate: Estate):
    result = estate.observe()
    with pytest.raises(sqlite3.IntegrityError):
        with estate.database.transaction():
            estate.database.connection.execute(
                """
                INSERT INTO provider_observations (
                    provider_observation_id, sweep_id, host_id, provider, source, status,
                    reason, method, observed_at, received_at, records, detail
                ) VALUES ('pobs_y', ?, ?, 'docker', 'docker.networks', 'error',
                          NULL, 'unix_socket_http', 'now', 'now', '[]', '{}')
                """,
                (result.sweep_id, result.host_id),
            )


def test_provider_evidence_is_kept_per_sweep_and_the_newest_is_used(estate: Estate):
    first = estate.observe()
    estate.start_docker(monitoring_network())
    estate.build_agent()
    second = estate.observe()

    assert first.sweep_id != second.sweep_id
    stored = estate.provenance.readings.for_sweep(first.sweep_id)
    assert {r.provider: r.status for r in stored}["docker"] == "absent"
    # History is kept; the assessment uses the newest reading of each source.
    assert estate.provenance.evidence(estate.host_id).get("docker").sweep_id == second.sweep_id
    assert estate.ownership_of("docker0").state is OwnershipState.ATTRIBUTED


# -------------------------------------------------------------------------- adoption


def test_an_owned_object_cannot_be_newly_adopted(estate: Estate):
    estate.start_docker(monitoring_network())
    estate.observe()
    with pytest.raises(ManagementRefused) as raised:
        estate.management.adopt(estate.object_named("docker0"))

    refusal = raised.value
    assert refusal.code == "externally_configured"
    assert refusal.detail["owner"]["provider"] == "docker"
    assert refusal.detail["owner"]["instance"] == MONITORING_NETWORK
    assert refusal.detail["evidence"][0]["kind"] == "ipam_gateway_address"
    # Refused, and nothing was written.
    assert estate.object_named("docker0").management_state == ManagementState.OBSERVED
    assert estate.management.intents.history(estate.object_named("docker0").object_id) == []


def test_an_observe_only_object_is_refused_before_ownership_is_consulted(estate: Estate):
    estate.start_docker(monitoring_network())
    estate.observe()
    with pytest.raises(ManagementRefused) as raised:
        estate.management.adopt(estate.object_named("tailscale0"))
    assert raised.value.code == "object_observe_only"


def test_an_unowned_interface_is_adopted_with_its_ownership_recorded(estate: Estate):
    estate.start_docker(monitoring_network())
    estate.observe()
    outcome = estate.management.adopt(estate.object_named("eth0"))
    assert outcome.to_state == ManagementState.MANAGED
    assert outcome.host_effect == "none"
    assert outcome.provenance.reason == "host_kernel_only"
    assert outcome.provenance.gaps == ()


def test_a_gap_does_not_prevent_adoption_but_is_reported(estate: Estate):
    """Adoption writes nothing to the host, so an unread source is a note, not a veto."""
    estate.break_provider(GENERAL_ARGV, returncode=8)
    estate.build_agent()
    estate.observe()
    outcome = estate.management.adopt(estate.object_named("eth0"))
    assert outcome.provenance.gaps == ("networkmanager.devices",)


# ------------------------------------------------------- a managed object that is owned


def adopt_docker0_before_docker_is_readable(estate: Estate) -> ObjectRecord:
    """The state this behaviour exists to make safe.

    With no Docker daemon to ask, the bridge is an ordinary management candidate and
    adoption is allowed. Then Docker becomes readable and says it owns it.
    """
    estate.observe()
    estate.management.adopt(estate.object_named("docker0"))
    estate.start_docker(monitoring_network())
    estate.build_agent()
    estate.observe()
    return estate.object_named("docker0")


def test_provider_evidence_never_releases_a_managed_object(estate: Estate):
    record = adopt_docker0_before_docker_is_readable(estate)
    assert record.management_state == ManagementState.MANAGED
    assert record.management_reason == "adopted"
    assert record.active_intent_id is not None


def test_the_conflict_is_recorded_as_a_finding_with_its_evidence(estate: Estate):
    record = adopt_docker0_before_docker_is_readable(estate)
    findings = estate.management.ownership_findings.open_for_object(record.object_id)

    by_relation = {f.subject: f for f in findings}
    assert set(by_relation) == {"created_by", "configured_by"}
    conflict = by_relation["configured_by"]
    assert conflict.finding_type == FINDING_TYPE_OWNERSHIP_CONFLICT
    assert conflict.owner_provider == "docker"
    assert conflict.owner_instance == MONITORING_NETWORK
    assert conflict.confidence == "corroborated"
    assert conflict.intent_id == record.active_intent_id
    assert conflict.finding_key == finding_key(
        record.host_id, record.object_id, FINDING_TYPE_OWNERSHIP_CONFLICT, "configured_by"
    )


def test_re_observing_the_same_conflict_updates_one_finding(estate: Estate):
    record = adopt_docker0_before_docker_is_readable(estate)
    first = estate.management.ownership_findings.open_for_object(record.object_id)
    estate.observe()
    second = estate.management.ownership_findings.open_for_object(record.object_id)

    assert {f.finding_id for f in first} == {f.finding_id for f in second}
    proven_again = {f.subject: f for f in second}["configured_by"]
    original = {f.subject: f for f in first}["configured_by"]
    assert proven_again.first_seen_at == original.first_seen_at
    assert proven_again.last_seen_at > original.last_seen_at


def test_a_provider_that_stops_answering_does_not_close_the_conflict(estate: Estate):
    record = adopt_docker0_before_docker_is_readable(estate)
    before = {f.subject: f for f in estate.management.ownership_findings.open_for_object(
        record.object_id
    )}["configured_by"]

    estate.docker_socket.unlink()  # the socket goes away mid-life: unreadable, not gone
    estate.docker_socket.write_text("")
    estate.observe()

    after = estate.management.ownership_findings.get(before.finding_id)
    assert after.status == "open"
    assert after.last_seen_at == before.last_seen_at  # not proven again
    assert after.updated_at > before.updated_at  # but looked at


def test_a_sweep_that_consulted_nobody_reconfirms_nothing(estate: Estate):
    """An observation with no provider evidence may not renew a claim on old readings.

    The stored readings are still there and the API will still show them, with their age.
    What must not happen is a finding recording that its claim was *proven again* by a
    sweep during which nobody was asked.
    """
    record = adopt_docker0_before_docker_is_readable(estate)
    before = {
        f.subject: f
        for f in estate.management.ownership_findings.open_for_object(record.object_id)
    }["configured_by"]

    estate.observe_without_providers()

    after = estate.management.ownership_findings.get(before.finding_id)
    assert after.status == "open"
    assert after.last_seen_at == before.last_seen_at
    assert after.updated_at > before.updated_at


def test_a_sweep_that_consulted_nobody_opens_no_conflict(estate: Estate):
    estate.start_docker(monitoring_network())
    estate.build_agent()
    estate.observe()
    estate.management.adopt(estate.object_named("eth0"))
    eth0 = estate.object_named("eth0")

    estate.observe_without_providers()
    assert estate.management.ownership_findings.open_for_object(eth0.object_id) == []


def test_a_provider_that_no_longer_claims_the_object_resolves_the_conflict(estate: Estate):
    record = adopt_docker0_before_docker_is_readable(estate)
    open_ids = [
        f.finding_id
        for f in estate.management.ownership_findings.open_for_object(record.object_id)
    ]

    # Docker answers, and the network is gone.
    estate.daemons([], name=estate.docker_socket.name + ".unused")
    estate.docker_socket.unlink()
    estate.start_docker()
    estate.build_agent()
    estate.observe()

    assert estate.management.ownership_findings.open_for_object(record.object_id) == []
    resolved = [estate.management.ownership_findings.get(i) for i in open_ids]
    assert {f.resolution for f in resolved} == {"owner_no_longer_claims"}
    assert all(f.resolved_by_provider_observation_id for f in resolved)


def test_releasing_the_object_ends_the_conflict(estate: Estate):
    record = adopt_docker0_before_docker_is_readable(estate)
    estate.management.release(record)

    assert estate.management.ownership_findings.open_for_object(record.object_id) == []
    history = estate.management.ownership_findings.history_for_object(record.object_id)
    assert {f.resolution for f in history} == {"intent_released"}
    # The object is still Docker's. Only LocalPlane's claim ended.
    assert estate.ownership_of("docker0").state is OwnershipState.ATTRIBUTED


def test_a_conflicted_object_still_reconciles_normally(estate: Estate):
    """Ownership, reconciliation, health and freshness are independent to the end."""
    record = adopt_docker0_before_docker_is_readable(estate)
    intent = estate.management.intents.active_for([record.object_id])[record.object_id]
    reconciliation = estate.management.reconciliation_for(record, intent)

    assert reconciliation.state == "in_sync"
    assert record.observation.health_state == "healthy"
    assert estate.ownership_of("docker0").reason == "externally_configured"
    assert estate.eligibility_of("docker0").reason == "already_managed"


# ------------------------------------------------- revising an object somebody else owns


def active_intent_id(estate: Estate, name: str) -> str:
    return estate.object_named(name).active_intent_id or ""


def test_a_proven_external_owner_refuses_an_explicit_revision(estate: Estate):
    """The same gate adopt applies, applied for the same reason.

    LocalPlane has no write model that could share this object, so the only thing a revised
    intent could ever mean is "apply this" — which it must not. Deepening a claim it cannot
    act on would make the conflict harder to see rather than easier, and the conflict is
    the thing an operator needs to be looking at.
    """
    record = adopt_docker0_before_docker_is_readable(estate)
    with pytest.raises(ManagementRefused) as raised:
        estate.management.revise_intent(
            record, fields={"mtu": 1400}, expected_intent_id=record.active_intent_id or ""
        )

    assert raised.value.code == "externally_configured"
    assert raised.value.detail["owner"]["provider"] == "docker"
    assert raised.value.detail["ownership_state"] == "attributed"


def test_a_proven_external_owner_refuses_adopting_its_runtime_as_intent(estate: Estate):
    """The more tempting of the two, and the more dangerous.

    Adopting the runtime of an object Docker is configuring would make LocalPlane's desired
    state a copy of Docker's, and the pair would then read `in_sync` — a green light over
    an object LocalPlane must not touch. The conflict is what is true here, and it stays
    the thing being reported.
    """
    record = adopt_docker0_before_docker_is_readable(estate)
    with pytest.raises(ManagementRefused) as raised:
        estate.management.adopt_runtime_as_intent(
            record, expected_intent_id=record.active_intent_id or ""
        )
    assert raised.value.code == "externally_configured"


def test_a_refused_revision_changes_nothing_about_the_object_or_the_conflict(estate: Estate):
    """Refusing is not releasing, and it is not quietly revising either."""
    record = adopt_docker0_before_docker_is_readable(estate)
    before = {
        f.subject: f
        for f in estate.management.ownership_findings.open_for_object(record.object_id)
    }
    with pytest.raises(ManagementRefused):
        estate.management.revise_intent(
            record, fields={"mtu": 1400}, expected_intent_id=record.active_intent_id or ""
        )

    after = estate.object_named("docker0")
    assert after.management_state == ManagementState.MANAGED
    assert after.active_intent_id == record.active_intent_id
    assert len(estate.management.intents.history(after.object_id)) == 1
    assert estate.database.query("SELECT * FROM intent_revisions") == []

    still = {
        f.subject: f
        for f in estate.management.ownership_findings.open_for_object(record.object_id)
    }
    assert set(still) == set(before)
    for subject, finding in still.items():
        assert finding.finding_id == before[subject].finding_id
        assert finding.status == "open"
        assert finding.updated_at == before[subject].updated_at


def test_an_ownership_conflict_is_not_ended_by_revising_a_desired_value(estate: Estate):
    """The two claims are independent, and one of them is not about configuration at all.

    Proven here through the object the gate does not cover: an ownership conflict raised on
    ``created_by`` alone does not make LocalPlane answerable for what Docker is doing, and a
    revision that went ahead would still not touch it. Whatever happens to the desired
    state, the conflict ends when the owner stops claiming the object or when the object is
    released — never because somebody changed their mind about an MTU.
    """
    record = adopt_docker0_before_docker_is_readable(estate)
    conflicts = estate.management.ownership_findings.open_for_object(record.object_id)
    assert {f.subject for f in conflicts} == {"created_by", "configured_by"}

    with pytest.raises(ManagementRefused):
        estate.management.revise_intent(
            record, fields={"mtu": 1400}, expected_intent_id=record.active_intent_id or ""
        )

    assert {
        f.finding_id
        for f in estate.management.ownership_findings.open_for_object(record.object_id)
    } == {f.finding_id for f in conflicts}
    assert estate.ownership_of("docker0").state is OwnershipState.ATTRIBUTED


def test_a_provider_that_cannot_be_read_does_not_forbid_a_revision(estate: Estate):
    """A gap is a gap. It is neither proof of an owner nor permission to ignore one.

    NetworkManager is installed and will not answer, so nothing is proven either way. The
    revision goes ahead — nothing has been shown to own this object — and the sources it
    could not consult are reported rather than quietly treated as clean.
    """
    estate.observe()
    estate.management.adopt(estate.object_named("eth0"))
    estate.break_provider(GENERAL_ARGV)
    estate.build_agent()
    estate.observe()

    outcome = estate.management.revise_intent(
        estate.object_named("eth0"),
        fields={"mtu": 1400},
        expected_intent_id=active_intent_id(estate, "eth0"),
    )

    assert outcome.intent.version == 2
    assert outcome.provenance.gaps == ("networkmanager.devices",)
    # And the gap did not become a claim in either direction.
    assert not any(c.owner.external for c in outcome.provenance.claims)


def test_a_provider_failure_never_becomes_a_reason_a_revision_was_allowed(estate: Estate):
    """The evidence gap travels with the answer, so nobody reads silence as consent."""
    estate.start_docker(monitoring_network())
    estate.build_agent()
    estate.observe()
    estate.management.adopt(estate.object_named("eth0"))

    estate.docker_socket.unlink()
    estate.docker_socket.write_text("")  # present, unreadable: a gap, not an absence
    estate.build_agent()
    estate.observe()

    outcome = estate.management.revise_intent(
        estate.object_named("eth0"),
        fields={"mtu": 1400},
        expected_intent_id=active_intent_id(estate, "eth0"),
    )
    assert "docker.networks" in outcome.provenance.gaps
    assert outcome.intent.version == 2


def test_the_revision_response_carries_the_evidence_gaps(client: TestClient, estate: Estate):
    """A revision reports what it could not consult, exactly as adoption does."""
    client.post("/api/v1/network/observations/refresh")
    eth0 = interfaces_by_name(client)["eth0"]
    adopted = client.post(f"/api/v1/network/interfaces/{eth0['object_id']}/adopt").json()

    estate.break_provider(GENERAL_ARGV)
    estate.build_agent()
    client.post("/api/v1/network/observations/refresh")

    body = client.post(
        f"/api/v1/network/interfaces/{eth0['object_id']}/intent/revise",
        json={"expected_intent_id": adopted["intent"]["intent_id"], "fields": {"mtu": 1400}},
    ).json()
    assert body["ownership"]["evidence_gaps"] == ["networkmanager.devices"]
    assert body["ownership"]["adoption"]["reason"] == "already_managed"


def test_revising_an_object_docker_owns_is_a_structured_409(client: TestClient):
    """Through the API, with the owner and the evidence named."""
    client.post("/api/v1/network/observations/refresh")
    docker0 = interfaces_by_name(client)["docker0"]
    response = client.post(
        f"/api/v1/network/interfaces/{docker0['object_id']}/intent/revise",
        json={"expected_intent_id": "int_whatever", "fields": {"mtu": 1400}},
    )
    # It is not managed in the first place, which is the first thing wrong with the request.
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_managed"


# ------------------------------------------------------------------------------- the API


def test_the_inventory_carries_the_ownership_answer(client: TestClient):
    interfaces = interfaces_by_name(client)
    docker0 = interfaces["docker0"]

    assert docker0["management"] == {"state": "observed", "reason": "management_candidate"}
    assert docker0["ownership"]["state"] == "attributed"
    assert docker0["ownership"]["configured_by"]["owner"]["label"] == "monitoring_default"
    assert docker0["ownership"]["configured_by"]["confidence"] == "corroborated"
    assert docker0["ownership"]["adoption"] == {
        "eligible": False,
        "reason": "externally_configured",
        "blocked_by": {
            "provider": "docker",
            "instance": MONITORING_NETWORK,
            "label": "monitoring_default",
            "version": "27.1.1",
        },
        "evidence_gaps": [],
    }


def test_the_inventory_says_what_is_not_known(client: TestClient):
    """Unknown is rendered as unknown, with the reason, not as an absent field."""
    client.post("/api/v1/network/observations/refresh")
    lo = interfaces_by_name(client)["lo"]
    assert lo["ownership"]["state"] == "attributed"
    assert lo["ownership"]["created_by"]["owner"]["provider"] == "kernel"
    assert lo["ownership"]["configured_by"] is None
    assert lo["ownership"]["adoption"]["reason"] == "object_observe_only"


def test_the_inventory_does_not_carry_the_provider_evidence(client: TestClient):
    """A network inventory must not become a dump of every daemon's records.

    The owner's *identifier* is carried, because it is what makes the claim checkable
    against Docker itself and it is one string. The evidence behind the claim — the IPAM
    configuration, the subnets, the Compose labels, the container map — is on the detail
    resource, where somebody asks for it.
    """
    raw = client.get("/api/v1/network/interfaces").text
    assert MONITORING_NETWORK in raw  # the identifier, deliberately
    for heavy in ("172.19.0.0/16", "Containers", "EndpointID", "com.docker.compose"):
        assert heavy not in raw

    ownership = interfaces_by_name(client)["docker0"]["ownership"]
    assert set(ownership) == {
        "state", "reason", "created_by", "configured_by", "evidence_gaps", "adoption"
    }
    assert set(ownership["configured_by"]) == {
        "relation", "owner", "confidence", "reason", "evidence_sources"
    }


def test_the_provenance_resource_carries_the_evidence_and_every_source(client: TestClient):
    docker0 = interfaces_by_name(client)["docker0"]
    body = client.get(
        f"/api/v1/network/interfaces/{docker0['object_id']}/provenance"
    ).json()

    assert body["state"] == "attributed"
    assert body["management"]["state"] == "observed"
    claim = next(c for c in body["claims"] if c["relation"] == "configured_by")
    assert claim["evidence"][0]["detail"]["gateway"] == DOCKER0_GATEWAY
    assert claim["evidence"][0]["detail"]["compose_project"] == "monitoring"

    outcomes = {s["source"]: s for s in body["sources"]}
    assert set(outcomes) == {
        "kernel.interface", "docker.networks", "networkmanager.devices", "tailscale.status"
    }
    # NetworkManager was asked and disclaimed it. That is a result, and it is visible.
    assert outcomes["networkmanager.devices"]["outcome"] == "device_externally_configured"
    assert outcomes["networkmanager.devices"]["gap"] is False
    assert outcomes["docker.networks"]["freshness"] in {"current", "stale"}


def test_adopting_an_owned_object_over_http_is_refused_with_the_owner(client: TestClient):
    docker0 = interfaces_by_name(client)["docker0"]
    response = client.post(f"/api/v1/network/interfaces/{docker0['object_id']}/adopt")

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "externally_configured"
    assert error["detail"]["owner"]["label"] == "monitoring_default"
    assert error["detail"]["ownership_reason"] == "externally_configured"


def test_adopting_an_unowned_interface_over_http_reports_what_was_checked(client: TestClient):
    eth0 = interfaces_by_name(client)["eth0"]
    body = client.post(f"/api/v1/network/interfaces/{eth0['object_id']}/adopt").json()

    assert body["host_mutated"] is False
    assert body["to_state"] == "managed"
    assert body["ownership"]["reason"] == "host_kernel_only"
    assert body["ownership"]["adoption"]["reason"] == "already_managed"
    assert body["ownership"]["evidence_gaps"] == []


def test_the_refresh_result_reports_what_each_provider_did(client: TestClient):
    body = client.post("/api/v1/network/observations/refresh").json()
    assert body["status"] in {"ok", "partial"}
    assert {p["provider"]: p["status"] for p in body["providers"]} == {
        "docker": "ok", "networkmanager": "ok", "tailscale": "ok"
    }
    assert {p["version"] for p in body["providers"]} == {"27.1.1", "1.46.0", "1.102.3"}


def test_an_ownership_conflict_is_visible_in_findings(client: TestClient, estate: Estate):
    """Adopt an interface nothing is driving, then let NetworkManager start driving it.

    The everyday version of this: an operator adopts an ethernet port that has no carrier
    and no active profile, somebody plugs a cable in, and NetworkManager brings up the
    connection it holds for that device. LocalPlane now retains intent for something
    another system is configuring, and it has to say so without quietly letting go.
    """
    eth0 = interfaces_by_name(client)["eth0"]
    client.post(f"/api/v1/network/interfaces/{eth0['object_id']}/adopt")

    estate.runner.responses.update(
        nmcli_devices([("eth0", "ethernet", "connected", "service-lan", "uuid-lan")])
    )
    client.post("/api/v1/network/observations/refresh")

    body = client.get("/api/v1/findings").json()
    conflict = next(
        f for f in body["findings"] if f["finding_type"] == "network.interface.ownership_conflict"
    )
    assert conflict["object_name"] == "eth0"
    assert conflict["subject"] == "configured_by"
    assert conflict["evidence"]["owner"]["provider"] == "networkmanager"
    assert conflict["evidence"]["owner"]["label"] == "service-lan"
    assert "LocalPlane retains intent" in conflict["summary"]
    assert client.get(f"/api/v1/findings/{conflict['finding_id']}").json() == conflict

    # And the object is still managed, still in sync, still healthy.
    current = client.get(f"/api/v1/network/interfaces/{eth0['object_id']}").json()
    assert current["management"]["state"] == "managed"
    assert current["reconciliation"] == "in_sync"
    assert current["health"]["state"] == "healthy"
    assert current["ownership"]["configured_by"]["owner"]["provider"] == "networkmanager"


def test_the_schema_describes_ownership_and_its_uncertainty(client: TestClient):
    schema = client.get("/openapi.json").json()["components"]["schemas"]
    ownership = schema["Ownership"]["properties"]
    assert set(ownership) >= {
        "state", "reason", "created_by", "configured_by", "evidence_gaps", "adoption"
    }
    eligibility = schema["AdoptionEligibility"]["properties"]
    assert set(eligibility) >= {"eligible", "reason", "blocked_by", "evidence_gaps"}
    source = schema["ConsultedSource"]["properties"]
    assert set(source) >= {"status", "outcome", "gap", "observed_at", "freshness"}
    assert "ownership" in schema["NetworkInterface"]["required"]
