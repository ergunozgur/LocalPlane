"""Adopt, release, reconciliation and findings.

Everything here runs through the real agent service over a fixture ``/sys/class/net``, so
drift is produced the way drift actually happens: a value on the host changes, the provider
reads it, and the comparison notices. Nothing hand-writes an observation payload, because a
test that constructs its own evidence proves only that the comparison can compare.

The fixture tree is written to and rewritten constantly. The machine this runs on is not
touched at any point — see ``test_live_host.py`` for the read-only exercise against the
real host, and for the proof that it did not move.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import pytest

from localplane.agent.service import AgentService
from localplane.backend.db.database import MIGRATIONS_DIR, open_database
from localplane.backend.db.repositories import ObjectRecord
from localplane.backend.domain.findings import (
    FINDING_TYPE_INTERFACE_DRIFT,
    FindingResolution,
    finding_key,
)
from localplane.backend.domain.intent import (
    ADOPTABLE_INTERFACE_FIELDS,
    INTENT_SCHEMA_VERSION,
    ValueType,
    capture_interface_intent,
    coerce,
)
from localplane.backend.domain.reconciliation import (
    Comparison,
    IntendedField,
    ObservedSnapshot,
    reconcile,
)
from localplane.backend.domain.states import HealthState, ManagementState, ReconciliationState
from localplane.backend.ingest import Ingestor
from localplane.backend.management import ManagementRefused, ManagementService
from tests.conftest import FakeRunner, json_result, write_interface


# --------------------------------------------------------------------------- the estate


@dataclass
class Estate:
    """A fixture host, and the levers a test needs to move it."""

    database: Any
    sysfs: Path
    service: AgentService
    ingestor: Ingestor
    management: ManagementService
    host_id: str = ""

    def observe(self, names: Sequence[str] | None = None) -> Any:
        params = {"names": list(names)} if names is not None else {}
        result = self.ingestor.ingest_network_sweep(
            self.service.handle("network.observe_interfaces", params)
        )
        self.host_id = result.host_id
        return result

    def object_named(self, name: str) -> ObjectRecord:
        from localplane.backend.domain.identity import OBJECT_KIND_NETWORK_INTERFACE

        for record in self.ingestor.objects.list_by_kind(
            self.host_id, OBJECT_KIND_NETWORK_INTERFACE
        ):
            if record.display_name == name:
                return record
        raise AssertionError(f"no object named {name}")

    def write(self, interface: str, field: str, value: str) -> None:
        """Change a value on the fixture host. Never on the machine."""
        path = self.sysfs / interface / field
        if path.is_dir():
            path.rmdir()
        path.write_text(value + "\n")

    def make_unreadable(self, interface: str, field: str) -> None:
        """Model a field the kernel refuses to answer for: a directory where a file goes."""
        path = self.sysfs / interface / field
        if path.exists() and not path.is_dir():
            path.unlink()
        if not path.exists():
            path.mkdir()

    def link_snapshot(self) -> dict[str, dict[str, str]]:
        """Every value in the fixture tree, for proving nothing moved."""
        snapshot: dict[str, dict[str, str]] = {}
        for entry in sorted(self.sysfs.iterdir()):
            if not (entry / "ifindex").exists():
                continue
            values = {}
            for file in sorted(entry.iterdir()):
                if file.is_file():
                    values[file.name] = file.read_text()
            snapshot[entry.name] = values
        return snapshot


def _runner() -> FakeRunner:
    """rtnetlink answers for the tree below, so observations are complete rather than partial."""
    from localplane.agent.providers.linux_network import ADDR_ARGV, LINK_ARGV

    return FakeRunner(
        {
            LINK_ARGV: json_result(
                LINK_ARGV,
                [
                    {"ifindex": 2, "ifname": "eth0"},
                    {"ifindex": 3, "ifname": "eth1"},
                    {"ifindex": 4, "ifname": "veth0", "linkinfo": {"info_kind": "veth"}},
                ],
            ),
            ADDR_ARGV: json_result(
                ADDR_ARGV,
                [
                    {"ifindex": 1, "ifname": "lo", "addr_info": []},
                    {"ifindex": 2, "ifname": "eth0", "addr_info": []},
                    {"ifindex": 3, "ifname": "eth1", "addr_info": [
                        {"family": "inet", "local": "192.0.2.215", "prefixlen": 24,
                         "scope": "global", "dynamic": True,
                         "valid_life_time": 41039, "preferred_life_time": 41039}]},
                    {"ifindex": 4, "ifname": "veth0", "addr_info": []},
                ],
            ),
        }
    )


@pytest.fixture
def estate(database, fake_root: Path, sysfs_net: Path, absent_docker: Path) -> Estate:
    """Four links: one loopback, two adoptable ethernets, one veth.

    ``eth0`` is administratively down with no carrier; ``eth1`` is up and healthy. Both are
    adoptable, and having one of each is what makes it possible to show that health and
    reconciliation do not depend on one another.
    """
    write_interface(
        sysfs_net, "lo", ifindex=1, address="00:00:00:00:00:00", arphrd="772", flags="0x9",
        operstate="unknown", carrier="1", mtu="65536", speed=None, duplex=None,
    )
    write_interface(
        sysfs_net, "eth0", ifindex=2, address="02:00:00:00:00:10", flags="0x1003",
        operstate="down", carrier="0", mtu="1500", device="fd580000.ethernet",
        subsystem="platform",
    )
    write_interface(
        sysfs_net, "eth1", ifindex=3, address="02:00:00:00:00:12", flags="0x1003",
        operstate="up", carrier="1", mtu="1500", speed="1000", duplex="full",
        device="1-1.4:1.0", subsystem="usb",
    )
    write_interface(
        sysfs_net, "veth0", ifindex=4, address="02:00:00:00:00:14", addr_assign_type="3",
        flags="0x1303", operstate="up", carrier="1", mtu="1500", speed=None, duplex=None,
    )

    service = AgentService(
        root=fake_root,
        sysfs_net=sysfs_net,
        runner=_runner(),
        docker_socket=absent_docker,
    )
    management = ManagementService(database, freshness_ttl_s=60.0)
    ingestor = Ingestor(database, management)
    ingestor.ingest_handshake(service.handle("agent.hello", {}))
    estate = Estate(database, sysfs_net, service, ingestor, management)
    estate.observe()
    return estate


# -------------------------------------------------------------------- the adoptable set


def test_the_adoptable_set_is_small_and_named():
    """Growing this is a schema decision, not something that happens by accident."""
    assert [f.name for f in ADOPTABLE_INTERFACE_FIELDS] == ["admin_up", "mtu"]
    assert [f.value_type for f in ADOPTABLE_INTERFACE_FIELDS] == [
        ValueType.BOOLEAN,
        ValueType.INTEGER,
    ]


def test_an_admin_state_is_never_mistaken_for_an_mtu():
    """In Python True is an int. A plain isinstance check would accept it as one."""
    assert coerce(ValueType.INTEGER, True) is None
    assert coerce(ValueType.BOOLEAN, 1) is None
    assert coerce(ValueType.INTEGER, 1500) == 1500
    assert coerce(ValueType.BOOLEAN, False) is False


def test_an_mtu_the_kernel_could_not_have_reported_is_not_captured():
    assert coerce(ValueType.INTEGER, 0) is None
    assert coerce(ValueType.INTEGER, -1) is None


def test_capture_takes_the_writable_fields_and_nothing_else():
    facts = {
        "name": "eth0", "kind": "ethernet", "admin_up": True, "mtu": 1500,
        "operstate": "up", "carrier": True, "speed_mbps": 1000, "duplex": "full",
        "mac_address": "02:00:00:00:00:10", "ifindex": 2, "master": "br0",
        "addresses": [{"family": "inet", "address": "192.0.2.1", "prefix_length": 24}],
        "statistics": {"rx_bytes": 12},
    }
    capture = capture_interface_intent(facts)
    assert capture.complete
    assert {f.field for f in capture.fields} == {"admin_up", "mtu"}


def test_an_unreadable_controlled_value_is_not_defaulted():
    capture = capture_interface_intent({"name": "eth0", "admin_up": None, "mtu": 1500})
    assert not capture.complete
    assert [(f.field, f.reason) for f in capture.uncapturable] == [
        ("admin_up", "observed_value_unreadable")
    ]


# ---------------------------------------------------------------------------- adopting


def test_an_observed_interface_can_be_adopted(estate: Estate):
    record = estate.object_named("eth0")
    assert record.management_state == ManagementState.OBSERVED

    outcome = estate.management.adopt(record)

    assert outcome.transition == "adopt"
    assert outcome.from_state == "observed"
    assert outcome.to_state == "managed"
    assert outcome.host_effect == "none"
    after = estate.object_named("eth0")
    assert after.management_state == ManagementState.MANAGED
    assert after.active_intent_id == outcome.intent.intent_id
    assert after.management_reason == "adopted"


def test_adoption_records_only_the_supported_writable_fields(estate: Estate):
    outcome = estate.management.adopt(estate.object_named("eth0"))
    assert {f.field for f in outcome.intent.fields} == {"admin_up", "mtu"}
    # eth0 is administratively up with no carrier: IFF_UP is set in flags 0x1003, and the
    # link being down is the world's doing, not the operator's.
    assert {(f.field, f.value) for f in outcome.intent.fields} == {
        ("admin_up", True),
        ("mtu", 1500),
    }


def test_transient_observation_fields_never_become_intent(estate: Estate):
    """The link's own behaviour is not something anybody intended."""
    outcome = estate.management.adopt(estate.object_named("eth1"))
    captured = {f.field for f in outcome.intent.fields}
    for transient in (
        "operstate", "carrier", "carrier_changes", "speed_mbps", "duplex",
        "statistics", "addresses", "health", "health_state", "fidelity", "gaps",
        "observed_at", "freshness", "ifindex", "master", "mac_address", "device_path",
    ):
        assert transient not in captured


def test_an_intent_is_versioned_and_names_the_observation_it_came_from(estate: Estate):
    record = estate.object_named("eth0")
    outcome = estate.management.adopt(record)
    intent = outcome.intent

    assert intent.version == 1
    assert intent.supersedes is None
    assert intent.schema_version == INTENT_SCHEMA_VERSION
    assert intent.origin == "adopt"
    assert intent.object_id == record.object_id
    assert intent.host_id == record.host_id
    assert intent.observation_id == record.observation.observation_id
    assert intent.sweep_id == record.observation.sweep_id
    assert intent.observed_at == record.observation.observed_at
    assert intent.capability == "network.observe"
    assert intent.provider == "linux.network"
    assert intent.created_at


def test_a_watch_only_interface_cannot_be_adopted(estate: Estate):
    """Loopback is observe_only, and adoption is not offered for it in any form."""
    loopback = estate.object_named("lo")
    assert loopback.management_state == ManagementState.OBSERVE_ONLY
    with pytest.raises(ManagementRefused) as raised:
        estate.management.adopt(loopback)
    assert raised.value.code == "object_observe_only"
    assert estate.object_named("lo").management_state == ManagementState.OBSERVE_ONLY
    assert estate.database.query("SELECT * FROM intents") == []


def test_an_ephemeral_virtual_pair_cannot_be_adopted(estate: Estate):
    veth = estate.object_named("veth0")
    assert veth.management_state == ManagementState.OBSERVE_ONLY
    with pytest.raises(ManagementRefused) as raised:
        estate.management.adopt(veth)
    assert raised.value.code == "object_observe_only"
    assert raised.value.detail["reason"] == "ephemeral_virtual_pair"


def test_adopting_an_already_managed_interface_is_refused_explicitly(estate: Estate):
    """Not a silent no-op and not a second intent: a named refusal that says what is true."""
    first = estate.management.adopt(estate.object_named("eth0"))
    with pytest.raises(ManagementRefused) as raised:
        estate.management.adopt(estate.object_named("eth0"))

    assert raised.value.code == "already_managed"
    assert raised.value.detail["active_intent_id"] == first.intent.intent_id
    assert len(estate.database.query("SELECT * FROM intents")) == 1
    assert len(estate.database.query("SELECT * FROM management_transitions")) == 1


def test_adoption_is_refused_when_the_observation_is_too_old(estate: Estate, database):
    """Intent is retained state. Taking it from a reading nobody vouches for is guessing."""
    impatient = ManagementService(database, freshness_ttl_s=0.0)
    with pytest.raises(ManagementRefused) as raised:
        impatient.adopt(estate.object_named("eth0"))

    assert raised.value.code == "observation_stale"
    assert raised.value.detail["freshness"] == "stale"
    assert raised.value.detail["ttl_seconds"] == 0.0
    assert estate.object_named("eth0").management_state == ManagementState.OBSERVED


def test_adoption_is_refused_when_a_controlled_value_could_not_be_read(estate: Estate):
    estate.make_unreadable("eth0", "flags")
    estate.observe()

    record = estate.object_named("eth0")
    assert record.observation.facts["admin_up"] is None
    assert record.observation.fidelity == "degraded"

    with pytest.raises(ManagementRefused) as raised:
        estate.management.adopt(record)

    assert raised.value.code == "controlled_values_unverified"
    assert [f["field"] for f in raised.value.detail["fields"]] == ["admin_up"]
    assert raised.value.detail["fields"][0]["reason"] == "observed_value_unreadable"
    assert "sysfs.flags" in raised.value.detail["gaps"]
    assert estate.database.query("SELECT * FROM intents") == []


def test_adoption_is_refused_for_an_object_that_has_never_been_observed(estate: Estate, database):
    record = estate.object_named("eth0")
    database.connection.execute("DELETE FROM observations WHERE object_id = ?", (record.object_id,))
    with pytest.raises(ManagementRefused) as raised:
        estate.management.adopt(estate.object_named("eth0"))
    assert raised.value.code == "no_observation"


def test_a_partial_sweep_can_still_be_adopted_when_the_controlled_fields_were_read(
    database, fake_root: Path, sysfs_net: Path
, absent_docker: Path):
    """No `ip` on the host: addresses are unknown, but admin_up and mtu came from sysfs.

    Refusing here would mean LocalPlane could never manage a host without iproute2, over a
    gap in evidence about fields it does not control.
    """
    write_interface(
        sysfs_net, "eth0", ifindex=2, address="02:00:00:00:00:10", flags="0x1003",
        operstate="down", carrier="0", mtu="1500",
    )
    service = AgentService(
        root=fake_root,
        sysfs_net=sysfs_net,
        runner=FakeRunner({}),
        docker_socket=absent_docker,
    )
    management = ManagementService(database, freshness_ttl_s=60.0)
    ingestor = Ingestor(database, management)
    ingestor.ingest_handshake(service.handle("agent.hello", {}))
    estate = Estate(database, sysfs_net, service, ingestor, management)
    result = estate.observe()

    assert result.status == "partial"
    record = estate.object_named("eth0")
    assert record.observation.fidelity == "partial"
    assert record.observation.facts["addresses"] is None

    outcome = management.adopt(record)
    assert {f.field for f in outcome.intent.fields} == {"admin_up", "mtu"}


# ---------------------------------------------------------------------- reconciliation


def test_an_observed_object_has_no_reconciliation(estate: Estate):
    record = estate.object_named("eth1")
    assert estate.management.reconciliation_for(record, None) is None


def test_a_watch_only_object_has_no_reconciliation(estate: Estate):
    assert estate.management.reconciliation_for(estate.object_named("lo"), None) is None


def test_adoption_leaves_the_object_in_sync_with_what_it_was_adopted_from(estate: Estate):
    outcome = estate.management.adopt(estate.object_named("eth0"))
    assert outcome.reconciliation.state is ReconciliationState.IN_SYNC
    assert outcome.reconciliation.reason == "controlled_fields_match"
    assert {c.comparison for c in outcome.reconciliation.fields} == {Comparison.MATCHES}


def test_a_matching_observation_keeps_a_managed_object_in_sync(estate: Estate):
    estate.management.adopt(estate.object_named("eth0"))
    estate.observe()
    record = estate.object_named("eth0")
    intent = estate.management.intents.active_for([record.object_id])[record.object_id]
    assert estate.management.reconciliation_for(record, intent).state is ReconciliationState.IN_SYNC


def test_a_controlled_field_that_moves_is_drift(estate: Estate):
    estate.management.adopt(estate.object_named("eth0"))
    estate.write("eth0", "mtu", "9000")
    estate.observe()

    record = estate.object_named("eth0")
    intent = estate.management.intents.active_for([record.object_id])[record.object_id]
    result = estate.management.reconciliation_for(record, intent)

    assert result.state is ReconciliationState.DRIFTED
    assert result.reason == "controlled_field_differs"
    drifted = result.drifted_fields
    assert [f.field for f in drifted] == ["mtu"]
    assert drifted[0].intended == 1500
    assert drifted[0].observed == 9000


def test_an_uncontrolled_field_that_moves_is_not_drift(estate: Estate):
    """The carrier coming up is the host doing its job, not a disagreement."""
    estate.management.adopt(estate.object_named("eth0"))
    estate.write("eth0", "carrier", "1")
    estate.write("eth0", "operstate", "up")
    estate.write("eth0", "speed", "1000")
    estate.write("eth0", "duplex", "full")
    estate.observe()

    record = estate.object_named("eth0")
    intent = estate.management.intents.active_for([record.object_id])[record.object_id]
    assert record.observation.facts["operstate"] == "up"
    assert estate.management.reconciliation_for(record, intent).state is ReconciliationState.IN_SYNC


def test_a_controlled_field_that_cannot_be_read_is_unknown_not_drift(estate: Estate):
    estate.management.adopt(estate.object_named("eth0"))
    estate.make_unreadable("eth0", "mtu")
    estate.observe()

    record = estate.object_named("eth0")
    intent = estate.management.intents.active_for([record.object_id])[record.object_id]
    result = estate.management.reconciliation_for(record, intent)

    assert result.state is ReconciliationState.UNKNOWN
    assert result.reason == "controlled_field_not_observable"
    mtu = [f for f in result.fields if f.field == "mtu"][0]
    assert mtu.comparison is Comparison.UNKNOWN
    assert mtu.observed is None
    assert mtu.reason == "observed_value_unreadable"
    assert result.drifted_fields == ()


def test_a_proven_disagreement_outranks_a_field_nobody_could_read(estate: Estate):
    """An unreadable second field does not unprove the first one."""
    estate.management.adopt(estate.object_named("eth0"))
    estate.write("eth0", "mtu", "9000")
    estate.make_unreadable("eth0", "flags")
    estate.observe()

    record = estate.object_named("eth0")
    intent = estate.management.intents.active_for([record.object_id])[record.object_id]
    result = estate.management.reconciliation_for(record, intent)

    assert result.state is ReconciliationState.DRIFTED
    by_field = {f.field: f for f in result.fields}
    assert by_field["mtu"].comparison is Comparison.DIFFERS
    assert by_field["admin_up"].comparison is Comparison.UNKNOWN


def test_an_observation_from_another_provider_is_not_compared():
    """The same field name from a different source is not the same fact."""
    result = reconcile(
        intent_capability="network.observe",
        intent_provider="linux.network",
        intended=[IntendedField("mtu", ValueType.INTEGER, 1500)],
        observation=ObservedSnapshot(
            observation_id="obs_1", sweep_id="sweep_1", observed_at="2026-08-21T00:00:00+00:00",
            capability="network.observe", provider="some.other.provider", facts={"mtu": 9000},
        ),
    )
    assert result.state is ReconciliationState.UNKNOWN
    assert result.reason == "observation_source_incompatible"
    assert result.drifted_fields == ()


def test_a_managed_object_with_no_observation_is_unknown():
    result = reconcile(
        intent_capability="network.observe",
        intent_provider="linux.network",
        intended=[IntendedField("mtu", ValueType.INTEGER, 1500)],
        observation=None,
    )
    assert result.state is ReconciliationState.UNKNOWN
    assert result.reason == "no_observation"


def test_drift_evidence_is_typed_and_field_scoped(estate: Estate):
    estate.management.adopt(estate.object_named("eth1"))
    estate.write("eth1", "flags", "0x1002")
    estate.observe()

    record = estate.object_named("eth1")
    intent = estate.management.intents.active_for([record.object_id])[record.object_id]
    result = estate.management.reconciliation_for(record, intent)

    assert [f.field for f in result.fields] == ["admin_up", "mtu"]
    admin = [f for f in result.fields if f.field == "admin_up"][0]
    assert admin.value_type is ValueType.BOOLEAN
    assert admin.intended is True
    assert admin.observed is False
    assert admin.comparison is Comparison.DIFFERS
    assert admin.reason == "observed_value_differs"
    assert result.observation_id == record.observation.observation_id
    assert result.sweep_id == record.observation.sweep_id


# ------------------------------------------------------- health stays its own question


def test_health_and_reconciliation_are_independent(estate: Estate):
    """A drifted interface can be perfectly healthy; an in-sync one can be down."""
    estate.management.adopt(estate.object_named("eth0"))  # down, and adopted as down
    estate.management.adopt(estate.object_named("eth1"))  # up, and adopted as up
    estate.write("eth1", "mtu", "9000")  # healthy, and now drifted
    estate.observe()

    def state_of(name: str) -> tuple[str, ReconciliationState]:
        record = estate.object_named(name)
        intent = estate.management.intents.active_for([record.object_id])[record.object_id]
        return (
            record.observation.health_state,
            estate.management.reconciliation_for(record, intent).state,
        )

    assert state_of("eth1") == (HealthState.HEALTHY, ReconciliationState.DRIFTED)
    assert state_of("eth0") == (HealthState.INACTIVE, ReconciliationState.IN_SYNC)


# ---------------------------------------------------------------------------- findings


def _findings(estate: Estate, name: str) -> list[Any]:
    record = estate.object_named(name)
    return estate.management.findings.history_for_object(record.object_id)


def test_drift_opens_one_finding_with_typed_evidence(estate: Estate):
    estate.management.adopt(estate.object_named("eth0"))
    estate.write("eth0", "mtu", "9000")
    estate.observe()

    findings = _findings(estate, "eth0")
    assert len(findings) == 1
    finding = findings[0]
    assert finding.finding_type == FINDING_TYPE_INTERFACE_DRIFT
    assert finding.subject == "mtu"
    assert finding.status == "open"
    assert finding.intended_value == 1500
    assert finding.observed_value == 9000
    assert finding.intended_type == "integer"
    assert finding.comparison == "differs"
    assert finding.reason == "observed_value_differs"
    assert finding.observation_id
    assert finding.sweep_id
    assert finding.resolved_at is None


def test_a_finding_identity_is_stable_across_observations(estate: Estate):
    record = estate.object_named("eth0")
    estate.management.adopt(record)
    estate.write("eth0", "mtu", "9000")
    estate.observe()
    finding = _findings(estate, "eth0")[0]
    assert finding.finding_key == finding_key(
        record.host_id, record.object_id, FINDING_TYPE_INTERFACE_DRIFT, "mtu"
    )


def test_observing_the_same_drift_repeatedly_does_not_pile_up_findings(estate: Estate):
    estate.management.adopt(estate.object_named("eth0"))
    estate.write("eth0", "mtu", "9000")
    for _ in range(6):
        estate.observe()

    findings = _findings(estate, "eth0")
    assert len(findings) == 1
    assert findings[0].status == "open"
    # Six observations, one claim, and the record shows both: when it started, and that it
    # was still true at the last look.
    assert findings[0].last_seen_at > findings[0].first_seen_at


def test_the_schema_refuses_a_second_open_finding_for_the_same_claim(estate: Estate, database):
    estate.management.adopt(estate.object_named("eth0"))
    estate.write("eth0", "mtu", "9000")
    estate.observe()
    existing = _findings(estate, "eth0")[0]

    with pytest.raises(sqlite3.IntegrityError):
        database.connection.execute(
            "INSERT INTO findings (finding_id, finding_key, host_id, object_id, finding_type, "
            "subject, status, intent_id, intended_type, intended_value, observed_type, "
            "observed_value, comparison, reason, first_seen_at, last_seen_at, updated_at) "
            "VALUES ('fnd_dup',?,?,?,?,'mtu','open',?,'integer',1500,'integer',9000,'differs',"
            "'observed_value_differs','t','t','t')",
            (
                existing.finding_key,
                existing.host_id,
                existing.object_id,
                existing.finding_type,
                existing.intent_id,
            ),
        )


def test_a_drift_that_goes_away_resolves_its_finding_with_the_proof(estate: Estate):
    estate.management.adopt(estate.object_named("eth0"))
    estate.write("eth0", "mtu", "9000")
    estate.observe()
    estate.write("eth0", "mtu", "1500")
    estate.observe()

    findings = _findings(estate, "eth0")
    assert len(findings) == 1
    finding = findings[0]
    assert finding.status == "resolved"
    assert finding.resolution == FindingResolution.OBSERVED_MATCHES_INTENT
    assert finding.resolved_at is not None
    assert finding.resolved_by_observation_id == estate.object_named("eth0").observation.observation_id
    # The evidence that supported the claim is kept. It was true when it was made.
    assert finding.observed_value == 9000


def test_a_recurrence_is_a_new_episode_and_the_old_one_survives(estate: Estate):
    estate.management.adopt(estate.object_named("eth0"))
    estate.write("eth0", "mtu", "9000")
    estate.observe()
    estate.write("eth0", "mtu", "1500")
    estate.observe()
    estate.write("eth0", "mtu", "9000")
    estate.observe()

    findings = _findings(estate, "eth0")
    assert len(findings) == 2
    assert sorted(f.status for f in findings) == ["open", "resolved"]
    assert len({f.finding_id for f in findings}) == 2
    assert len({f.finding_key for f in findings}) == 1


def test_an_unreadable_value_does_not_resolve_an_open_drift(estate: Estate):
    """The most dangerous thing here would be to call a failed read good news."""
    estate.management.adopt(estate.object_named("eth0"))
    estate.write("eth0", "mtu", "9000")
    estate.observe()
    opened = _findings(estate, "eth0")[0]

    estate.make_unreadable("eth0", "mtu")
    estate.observe()

    findings = _findings(estate, "eth0")
    assert len(findings) == 1
    finding = findings[0]
    assert finding.finding_id == opened.finding_id
    assert finding.status == "open"
    assert finding.resolution is None
    assert finding.comparison == "unknown"
    assert finding.observed_value is None
    assert finding.reason == "observed_value_unreadable"
    # Still open, last confirmed when it was last proven, looked at since and could not tell.
    assert finding.last_seen_at == opened.last_seen_at
    assert finding.updated_at > finding.last_seen_at


def test_an_unreadable_value_does_not_open_a_finding_on_its_own(estate: Estate):
    """Nobody observed a disagreement, so LocalPlane does not claim one."""
    estate.management.adopt(estate.object_named("eth0"))
    estate.make_unreadable("eth0", "mtu")
    estate.observe()

    record = estate.object_named("eth0")
    intent = estate.management.intents.active_for([record.object_id])[record.object_id]
    assert estate.management.reconciliation_for(record, intent).state is ReconciliationState.UNKNOWN
    assert _findings(estate, "eth0") == []


def test_an_observed_object_never_produces_a_finding(estate: Estate):
    estate.write("eth0", "mtu", "9000")
    estate.observe()
    assert _findings(estate, "eth0") == []
    assert estate.database.query("SELECT * FROM findings") == []


def test_only_the_objects_a_sweep_re_read_are_re_evaluated(estate: Estate):
    """A sweep that did not look at an object is not evidence about it."""
    estate.management.adopt(estate.object_named("eth0"))
    estate.write("eth0", "mtu", "9000")
    estate.observe()
    opened = _findings(estate, "eth0")[0]

    estate.observe(["eth1"])

    finding = _findings(estate, "eth0")[0]
    assert finding.status == "open"
    assert finding.updated_at == opened.updated_at


# ---------------------------------------------------------------------------- releasing


def test_release_returns_the_object_to_observed(estate: Estate):
    estate.management.adopt(estate.object_named("eth0"))
    outcome = estate.management.release(estate.object_named("eth0"))

    assert outcome.transition == "release"
    assert outcome.from_state == "managed"
    assert outcome.to_state == "observed"
    assert outcome.reconciliation is None
    assert outcome.host_effect == "none"

    after = estate.object_named("eth0")
    assert after.management_state == ManagementState.OBSERVED
    assert after.active_intent_id is None
    assert after.management_reason == "released"


def test_release_keeps_every_intent_version_as_history(estate: Estate):
    record = estate.object_named("eth0")
    adopted = estate.management.adopt(record)
    estate.management.release(estate.object_named("eth0"))

    history = estate.management.intents.history(record.object_id)
    assert [i.intent_id for i in history] == [adopted.intent.intent_id]
    assert history[0].fields
    assert estate.management.intents.active_for([record.object_id]) == {}


def test_release_is_recorded_because_it_leaves_no_other_trace(estate: Estate):
    record = estate.object_named("eth0")
    estate.management.adopt(record)
    estate.management.release(estate.object_named("eth0"))

    transitions = estate.management.intents.transitions(record.object_id)
    assert [t.transition for t in transitions] == ["release", "adopt"]
    assert all(t.host_effect == "none" for t in transitions)
    release = transitions[0]
    assert release.from_state == "managed"
    assert release.to_state == "observed"
    assert release.observation_id is None


def test_release_does_not_change_the_observation_or_the_host(estate: Estate):
    """Release is not a rollback. Nothing is put back, because nothing was applied."""
    estate.management.adopt(estate.object_named("eth0"))
    estate.write("eth0", "mtu", "9000")
    estate.observe()

    before_tree = estate.link_snapshot()
    before = estate.object_named("eth0")

    estate.management.release(estate.object_named("eth0"))

    after = estate.object_named("eth0")
    assert estate.link_snapshot() == before_tree
    assert after.observation.observation_id == before.observation.observation_id
    assert after.observation.facts == before.observation.facts
    assert after.observation.facts["mtu"] == 9000
    assert after.observation.health_state == before.observation.health_state


def test_release_resolves_open_findings_truthfully(estate: Estate):
    estate.management.adopt(estate.object_named("eth0"))
    estate.write("eth0", "mtu", "9000")
    estate.observe()
    assert _findings(estate, "eth0")[0].status == "open"

    estate.management.release(estate.object_named("eth0"))

    finding = _findings(estate, "eth0")[0]
    assert finding.status == "resolved"
    assert finding.resolution == FindingResolution.INTENT_RELEASED
    assert finding.resolved_by_observation_id is None
    assert finding.observed_value == 9000


def test_releasing_something_that_is_not_managed_is_refused(estate: Estate):
    with pytest.raises(ManagementRefused) as raised:
        estate.management.release(estate.object_named("eth1"))
    assert raised.value.code == "not_managed"

    with pytest.raises(ManagementRefused) as raised:
        estate.management.release(estate.object_named("lo"))
    assert raised.value.code == "not_managed"


def test_re_adopting_after_a_release_writes_a_second_version(estate: Estate):
    record = estate.object_named("eth0")
    first = estate.management.adopt(record)
    estate.management.release(estate.object_named("eth0"))
    estate.write("eth0", "mtu", "9000")
    estate.observe()
    second = estate.management.adopt(estate.object_named("eth0"))

    assert second.intent.version == 2
    assert second.intent.supersedes == first.intent.intent_id
    assert {(f.field, f.value) for f in second.intent.fields} == {
        ("admin_up", True),
        ("mtu", 9000),
    }
    assert len(estate.management.intents.history(record.object_id)) == 2
    assert second.reconciliation.state is ReconciliationState.IN_SYNC


# -------------------------------------------------------------------------- invariants


def test_observing_a_managed_object_does_not_quietly_release_it(estate: Estate):
    """A sweep answers "could this be managed". It does not decide whether it is."""
    outcome = estate.management.adopt(estate.object_named("eth0"))
    for _ in range(3):
        estate.observe()
    record = estate.object_named("eth0")
    assert record.management_state == ManagementState.MANAGED
    assert record.active_intent_id == outcome.intent.intent_id
    assert record.management_reason == "adopted"


def test_the_store_refuses_managed_without_an_active_intent(estate: Estate, database):
    record = estate.object_named("eth0")
    with pytest.raises(sqlite3.IntegrityError):
        database.connection.execute(
            "UPDATE objects SET management_state = 'managed' WHERE object_id = ?",
            (record.object_id,),
        )


def test_the_store_refuses_retained_intent_on_an_unmanaged_object(estate: Estate, database):
    record = estate.object_named("eth0")
    outcome = estate.management.adopt(record)
    with pytest.raises(sqlite3.IntegrityError):
        database.connection.execute(
            "UPDATE objects SET management_state = 'observed' WHERE object_id = ?",
            (record.object_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        database.connection.execute(
            "UPDATE objects SET active_intent_id = NULL WHERE object_id = ?",
            (record.object_id,),
        )
    assert estate.object_named("eth0").active_intent_id == outcome.intent.intent_id


def test_the_store_refuses_an_active_intent_belonging_to_another_object(estate: Estate, database):
    eth0 = estate.object_named("eth0")
    outcome = estate.management.adopt(eth0)
    eth1 = estate.object_named("eth1")
    with pytest.raises(sqlite3.IntegrityError):
        database.connection.execute(
            "UPDATE objects SET management_state = 'managed', active_intent_id = ? "
            "WHERE object_id = ?",
            (outcome.intent.intent_id, eth1.object_id),
        )


def test_the_store_refuses_an_active_intent_that_does_not_exist(estate: Estate, database):
    record = estate.object_named("eth0")
    with pytest.raises(sqlite3.IntegrityError):
        database.connection.execute(
            "UPDATE objects SET management_state = 'managed', active_intent_id = 'int_ghost' "
            "WHERE object_id = ?",
            (record.object_id,),
        )


def test_an_active_intent_cannot_be_deleted_out_from_under_the_object(estate: Estate, database):
    outcome = estate.management.adopt(estate.object_named("eth0"))
    with pytest.raises(sqlite3.IntegrityError):
        database.connection.execute(
            "DELETE FROM intents WHERE intent_id = ?", (outcome.intent.intent_id,)
        )


def test_a_transition_record_cannot_claim_a_host_write(estate: Estate, database):
    outcome = estate.management.adopt(estate.object_named("eth0"))
    with pytest.raises(sqlite3.IntegrityError):
        database.connection.execute(
            "UPDATE management_transitions SET host_effect = 'wrote' WHERE intent_id = ?",
            (outcome.intent.intent_id,),
        )


def test_adoption_captures_the_newest_observation_not_the_one_the_caller_read(
    estate: Estate,
):
    """Intent is what is verified now. A sweep that lands mid-request is the newer truth."""
    stale = estate.object_named("eth0")
    estate.write("eth0", "mtu", "9000")
    estate.observe()

    outcome = estate.management.adopt(stale)

    current = estate.object_named("eth0")
    assert outcome.intent.observation_id == current.observation.observation_id
    assert outcome.intent.observation_id != stale.observation.observation_id
    assert {(f.field, f.value) for f in outcome.intent.fields} == {
        ("admin_up", True),
        ("mtu", 9000),
    }
    assert outcome.reconciliation.state is ReconciliationState.IN_SYNC


def test_a_second_adopt_racing_the_first_loses_truthfully(estate: Estate):
    """Two adopts in flight at once. One wins; the other is told what actually happened.

    The stale record is the whole point: both callers checked the object and saw it
    observed, and only the re-read under the write lock can tell the loser apart from a
    caller who is simply wrong. Reproduced without threads because the interleaving that
    matters is "checked before, wrote after", not the scheduling.
    """
    stale = estate.object_named("eth0")
    winner = estate.management.adopt(estate.object_named("eth0"))

    with pytest.raises(ManagementRefused) as raised:
        estate.management.adopt(stale)

    assert raised.value.code == "already_managed"
    assert raised.value.detail["active_intent_id"] == winner.intent.intent_id
    assert len(estate.database.query("SELECT * FROM intents")) == 1
    assert len(estate.database.query("SELECT * FROM management_transitions")) == 1
    assert estate.object_named("eth0").active_intent_id == winner.intent.intent_id


def test_a_second_release_racing_the_first_loses_truthfully(estate: Estate):
    estate.management.adopt(estate.object_named("eth0"))
    stale = estate.object_named("eth0")
    estate.management.release(estate.object_named("eth0"))

    with pytest.raises(ManagementRefused) as raised:
        estate.management.release(stale)

    assert raised.value.code == "not_managed"
    assert len(estate.database.query("SELECT * FROM management_transitions")) == 2
    assert estate.object_named("eth0").management_state == ManagementState.OBSERVED


def test_an_adopt_that_fails_part_way_leaves_the_object_observed(estate: Estate, monkeypatch):
    """The intent, the pointer and the transition land together or not at all."""
    record = estate.object_named("eth0")

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("interrupted after the intent was written")

    monkeypatch.setattr(estate.management.intents, "record_transition", explode)
    with pytest.raises(RuntimeError):
        estate.management.adopt(record)

    after = estate.object_named("eth0")
    assert after.management_state == ManagementState.OBSERVED
    assert after.active_intent_id is None
    assert estate.database.query("SELECT * FROM intents") == []
    assert estate.database.query("SELECT * FROM intent_fields") == []
    assert estate.database.query("SELECT * FROM management_transitions") == []


def test_a_release_that_fails_part_way_leaves_the_object_managed(estate: Estate, monkeypatch):
    outcome = estate.management.adopt(estate.object_named("eth0"))
    estate.write("eth0", "mtu", "9000")
    estate.observe()

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("interrupted after the pointer moved")

    monkeypatch.setattr(estate.management.intents, "record_transition", explode)
    with pytest.raises(RuntimeError):
        estate.management.release(estate.object_named("eth0"))

    after = estate.object_named("eth0")
    assert after.management_state == ManagementState.MANAGED
    assert after.active_intent_id == outcome.intent.intent_id
    assert _findings(estate, "eth0")[0].status == "open"


# ------------------------------------------------------------------------- persistence


def test_intent_and_findings_survive_a_restart(estate: Estate, tmp_path: Path):
    """Everything asserted here is read back from a connection that was not there."""
    outcome = estate.management.adopt(estate.object_named("eth0"))
    estate.write("eth0", "mtu", "9000")
    estate.observe()
    object_id = estate.object_named("eth0").object_id
    path = estate.database.path
    estate.database.close()

    reopened = open_database(path)
    try:
        management = ManagementService(reopened, freshness_ttl_s=60.0)
        intent = management.intents.active_for([object_id])[object_id]
        assert intent.intent_id == outcome.intent.intent_id
        assert intent.version == 1
        assert intent.schema_version == INTENT_SCHEMA_VERSION
        assert {(f.field, f.value, f.value_type) for f in intent.fields} == {
            ("admin_up", True, "boolean"),
            ("mtu", 1500, "integer"),
        }
        findings = management.findings.open_for_object(object_id)
        assert len(findings) == 1
        assert findings[0].intended_value == 1500
        assert findings[0].observed_value == 9000
        transitions = management.intents.transitions(object_id)
        assert [t.transition for t in transitions] == ["adopt"]
    finally:
        reopened.close()


def test_a_boolean_intent_comes_back_as_a_boolean(estate: Estate):
    """1 and true are different answers, and the store must not blur them."""
    outcome = estate.management.adopt(estate.object_named("eth1"))
    admin = [f for f in outcome.intent.fields if f.field == "admin_up"][0]
    mtu = [f for f in outcome.intent.fields if f.field == "mtu"][0]
    assert admin.value is True and isinstance(admin.value, bool)
    assert isinstance(mtu.value, int) and not isinstance(mtu.value, bool)


def test_a_store_written_by_the_first_migration_upgrades(tmp_path: Path):
    """A database created by an earlier schema version opens, and keeps what it held."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    shutil.copy(MIGRATIONS_DIR / "0001_initial.sql", migrations / "0001_initial.sql")

    old = open_database(tmp_path / "upgrade.db", migrations)
    old.connection.execute(
        "INSERT INTO hosts VALUES ('h','machine_id','high',NULL,NULL,NULL,NULL,NULL,NULL,"
        "NULL,NULL,NULL,'[]','t','t')"
    )
    old.connection.execute(
        "INSERT INTO objects VALUES ('o','h','network.interface','kernel_name','eth0','low',"
        "'eth0','observed','management_candidate','t','t')"
    )
    assert [r["version"] for r in old.query("SELECT version FROM schema_migrations")] == [1]
    old.close()

    shutil.copy(MIGRATIONS_DIR / "0002_management.sql", migrations / "0002_management.sql")
    upgraded = open_database(tmp_path / "upgrade.db", migrations)
    try:
        assert [
            r["version"] for r in upgraded.query("SELECT version FROM schema_migrations")
        ] == [1, 2]
        row = upgraded.query_one("SELECT * FROM objects WHERE object_id = 'o'")
        assert row["display_name"] == "eth0"
        assert row["management_state"] == "observed"
        assert row["active_intent_id"] is None
        tables = {
            r["name"] for r in upgraded.query("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"intents", "intent_fields", "management_transitions", "findings"} <= tables
        assert upgraded.query("PRAGMA foreign_key_check") == []
    finally:
        upgraded.close()


def test_the_second_migration_is_deterministic(tmp_path: Path):
    """Applied twice, from scratch, the same schema comes out."""
    def schema_of(name: str) -> list[str]:
        db = open_database(tmp_path / name)
        try:
            return sorted(
                r["sql"] for r in db.query("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")
            )
        finally:
            db.close()

    assert schema_of("one.db") == schema_of("two.db")
