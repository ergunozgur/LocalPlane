"""The Change Engine's pre-write foundation: Runs, plans, previews and staleness.

Same discipline as the suites before it. Everything runs through the real agent service
over a fixture ``/sys/class/net``, so a plan is produced the way a plan happens — a value
on the host differs from the one LocalPlane retains, the provider reads it, and the planner
notices. Nothing hand-writes an observation payload, and nothing constructs a preview by
hand except where the subject under test *is* the canonical form.

The machine this runs on is never touched. ``test_live_host.py`` exercises the same path
against the real host, read-only, and proves nothing moved.

Three claims are asserted over and over, because they are the whole of this behaviour:

* **a Run is not a Change** — no host was written, no Change record exists, and there is no
  table for one to live in;
* **a published preview is immutable** — it says what was shown, and when the truth under
  it moves it becomes recognisably stale rather than being quietly rewritten;
* **nothing is claimed that was not established** — execution is not implemented, recovery
  is not armed, verification did not run, no confirmation was accepted, and whether this
  interface carries the operator's path is unknown and stays unknown.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Sequence

import pytest

from localplane.agent.service import AgentService
from localplane.backend.db.database import MIGRATIONS_DIR, open_database
from localplane.backend.db.repositories import ObjectRecord
from localplane.backend.domain.identity import OBJECT_KIND_NETWORK_INTERFACE
from localplane.backend.domain.intent import ValueType
from localplane.backend.domain.management_path import MANAGEMENT_PATH_EVIDENCE
from localplane.backend.domain.policy import (
    CONFIRM_FROM_RISK,
    assess_protection,
    assess_risk,
    effective_confirmation,
)
from localplane.backend.domain.protection import (
    ManagementPathRelation,
    ManagementPathVerdict,
    ProtectionReason,
    ProtectionStatus,
)
from localplane.backend.domain.runs import (
    PLAN_DIGEST_VERSION,
    REACHABLE_RUN_STATES,
    ConfirmationMethod,
    ExecutionAvailability,
    ExecutionEligibility,
    OperationType,
    PlanValidityState,
    RecoveryMode,
    RiskTier,
    RunState,
    canonical_plan,
    plan_digest,
)
from localplane.backend.ingest import Ingestor
from localplane.backend.management import ManagementService
from localplane.backend.operations import CAPABILITY_SET_MTU, RECONCILE_MTU
from localplane.backend.provenance import ProvenanceService
from localplane.backend.runs import RunRefused, RunService
from localplane.protocol.capabilities import CAPABILITIES
from tests.conftest import (
    FakeRunner,
    docker_network,
    json_result,
    nmcli_devices,
    tailscale_status,
    write_interface,
)

RECONCILE = OperationType.NETWORK_INTERFACE_RECONCILE_MTU

#: The management path as it stands when nothing has observed one. Every earlier plan
#: was made under exactly this, and it stays the default here so that a test which does
#: not care about protection reads the same as it always did.
UNOBSERVED = ManagementPathVerdict(
    resource_id=None,
    reason="management_path_unobserved",
    missing_evidence=MANAGEMENT_PATH_EVIDENCE,
)


def confirmed_path(object_id: str, evidence_id: str, observed_at: str) -> ManagementPathVerdict:
    """The management path proven to terminate on one object, naming its evidence."""
    return ManagementPathVerdict(
        resource_id=object_id,
        reason="management_path_confirmed",
        evidence_id=evidence_id,
        observed_at=observed_at,
    )


_NOW = "2026-08-22T12:00:00.000000+00:00"


def unproven_protection():
    """A protection assessment made with no management-path evidence at all."""
    return assess_protection(RECONCILE_MTU, management_path=UNOBSERVED, resource_id="obj")

DOCKER0_GATEWAY = "172.19.0.1"
MONITORING_NETWORK = "b" * 64


# ------------------------------------------------------------------------------- fixture


@dataclass
class Estate:
    """A fixture host, and the levers a Run test needs to move it."""

    database: Any
    sysfs: Path
    runner: FakeRunner
    service: AgentService
    ingestor: Ingestor
    management: ManagementService
    runs: RunService
    docker_socket: Path
    fake_root: Path
    daemons: Any
    host_id: str = ""
    management_path: ManagementPathVerdict = UNOBSERVED
    """The verdict every plan and every validity check in this estate is made under.

    A field rather than a parameter because it is a property of the *request* in the real
    system, not of the object being planned against: one answer per connection, applied to
    everything that connection asks about."""

    # ------------------------------------------------------------------ the fixture host

    def observe(self, providers: bool = True) -> Any:
        """One sweep, optionally asking the providers who owns what.

        ``providers=False`` models an agent that cannot answer the provider operation, so
        every source is a gap — which is the condition an execution plan has to treat as
        unresolved rather than clean.
        """
        payload = self.service.handle("network.observe_interfaces", {})
        if providers:
            answered = self.service.handle("network.observe_providers", {})["providers"]
            result = self.ingestor.ingest_network_sweep({**payload, "providers": answered})
        else:
            result = self.ingestor.ingest_network_sweep(
                {
                    **payload,
                    "providers": None,
                    "provider_error": {
                        "code": "capability_unavailable",
                        "message": "network.providers.observe is not available",
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

    def write(self, interface: str, field: str, value: str) -> None:
        """Change a value on the fixture host. Never on the machine."""
        path = self.sysfs / interface / field
        if path.is_dir():
            path.rmdir()
        path.write_text(value + "\n")

    def make_unreadable(self, interface: str, field: str) -> None:
        path = self.sysfs / interface / field
        if path.exists() and not path.is_dir():
            path.unlink()
        if not path.exists():
            path.mkdir()

    def set_networkmanager(self, rows: Sequence[Sequence[str]]) -> None:
        self.runner.responses.update(nmcli_devices(rows))

    def forget_provider_evidence(self) -> None:
        """Model a host on which no provider has ever been consulted.

        Provider readings deliberately outlive the sweep that took them — ownership is
        derived from the *newest* reading per provider, so a sweep that could not consult
        anybody leaves the previous answers standing rather than erasing them. That is why
        a gap needs there to be no reading at all, which is the state an agent that never
        had the capability would leave the store in.
        """
        self.database.connection.execute("DELETE FROM provider_observations")

    def start_docker(self, *networks: dict[str, Any]) -> Any:
        daemon = self.daemons(list(networks), name=self.docker_socket.name)
        self.build_agent()
        return daemon

    def build_agent(self) -> AgentService:
        # Deliberately pointed at a helper socket that is not there. This file is about the
        # *planning* half of the Change Engine, and a host with no privileged helper is the
        # case where every plan is honestly blocked on `required_capability_undeclared`.
        # The write half runs against a real helper in `test_write_boundary.py`.
        self.service = AgentService(
            root=self.fake_root,
            sysfs_net=self.sysfs,
            runner=self.runner,
            docker_socket=self.docker_socket,
            helper_socket=self.docker_socket.parent / "helper-absent.sock",
        )
        self.ingestor.ingest_handshake(self.service.handle("agent.hello", {}))
        return self.service

    def link_snapshot(self) -> dict[str, dict[str, str]]:
        """Every value in the fixture tree, for proving nothing moved."""
        snapshot: dict[str, dict[str, str]] = {}
        for entry in sorted(self.sysfs.iterdir()):
            if not (entry / "ifindex").exists():
                continue
            snapshot[entry.name] = {
                file.name: file.read_text()
                for file in sorted(entry.iterdir())
                if file.is_file()
            }
        return snapshot

    # -------------------------------------------------------------------------- shortcuts

    def adopt(self, name: str = "eth0") -> Any:
        return self.management.adopt(self.object_named(name))

    def drift(self, name: str = "eth0", mtu: str = "1400") -> None:
        """Make the runtime disagree with the retained intent, the way drift happens."""
        self.write(name, "mtu", mtu)
        self.observe()

    def plan(self, name: str = "eth0") -> Any:
        return self.runs.create(RECONCILE, self.object_named(name), self.management_path)

    def validity(self, run: Any) -> Any:
        return self.runs.validity(run, self.management_path)

    def confirm_path(self, name: str = "eth0", observed_at: str = _NOW) -> ManagementPathVerdict:
        """Prove the management path onto one of this estate's objects.

        Real evidence is written, not a fabricated identifier: a preview's protection
        evidence is a foreign key into the observations table, so a verdict naming a row
        that does not exist is one the store would refuse — which is the point.
        """
        evidence_id = self.record_path_evidence(observed_at)
        self.management_path = confirmed_path(
            self.object_named(name).object_id, evidence_id, observed_at
        )
        return self.management_path

    def record_path_evidence(self, observed_at: str = _NOW) -> str:
        import uuid as _uuid

        from localplane.backend.db.repositories import ManagementPathRepository

        observation_id = f"mpo_{_uuid.uuid4().hex}"
        with self.database.transaction():
            ManagementPathRepository(self.database).insert(
                {
                    "observation_id": observation_id,
                    "host_id": self.host_id,
                    "observed_at": observed_at,
                    "agent_instance_id": None,
                    "transport_peer_address": "192.0.2.130",
                    "transport_peer_family": "inet",
                    "local_endpoint_address": "192.0.2.215",
                    "local_endpoint_family": "inet",
                    "capability": "network.route.observe",
                    "provider": "linux.route",
                    "provider_version": "1",
                    "method": "netlink_rtm_getroute",
                    "route_status": "resolved",
                    "route_reason": None,
                    "route_family": "inet",
                    "route_destination": "192.0.2.130",
                    "route_destination_prefix_length": 32,
                    "route_preferred_source": "192.0.2.215",
                    "route_gateway": None,
                    "route_oif_index": 3,
                    "route_table": 254,
                    "route_type": "unicast",
                    "route_scope": "universe",
                    "route_protocol": "unspec",
                    "route_priority": None,
                    "route_error": None,
                }
            )
        return observation_id

    def publish_legacy_preview(
        self,
        plan: Any,
        reason: str = "management_path_evidence_unavailable",
        missing: tuple[str, ...] = ("route.observe", "session.peer"),
    ) -> Any:
        """Store a preview the way a pre-0006 build would have, and a Run naming it.

        Constructed by hand because the subject under test *is* the older canonical form:
        nothing in this build produces one any more, and "a preview published by an earlier
        build stays readable and stays checkable against its own digest" is exactly the
        claim that cannot be tested with a preview this build wrote.

        The protection columns hold what 0006's backfill puts there — which is a restatement
        of the one relation such a row could carry, not a judgement about history.
        """
        import uuid as _uuid

        from localplane.backend.domain.protection import ProtectionAssessment, ReasonAssessment
        from localplane.backend.runs import _preview_row

        legacy = replace(
            plan,
            protection=ProtectionAssessment(
                status=ProtectionStatus.UNKNOWN,
                reasons=(),
                unresolved=(ProtectionReason.MANAGEMENT_PATH,),
                assessed=(
                    ReasonAssessment(
                        reason=ProtectionReason.MANAGEMENT_PATH,
                        status=ProtectionStatus.UNKNOWN,
                        detail=reason,
                    ),
                ),
                management_path=ManagementPathRelation.UNKNOWN,
                reason=reason,
                missing_evidence=missing,
            ),
        )
        preview_id = f"prv_{_uuid.uuid4().hex}"
        run_id = f"run_{_uuid.uuid4().hex}"
        row = _preview_row(preview_id, plan_digest(legacy, 1), legacy, _NOW)
        row["digest_version"] = 1
        plan = legacy
        with self.database.transaction():
            self.runs.runs.insert_preview(row)
            self.runs.runs.insert_run(
                run_id=run_id,
                host_id=plan.host_id,
                object_id=plan.object_id,
                operation=str(plan.operation),
                state="preview",
                preview_id=preview_id,
                created_at=_NOW,
            )
        return self.runs.get(run_id)

    def store_snapshot(self) -> dict[str, list[tuple]]:
        """Every row of every table that a plan must not be able to move."""
        return {
            table: [tuple(r) for r in self.database.query(f"SELECT * FROM {table}")]
            for table in (
                "objects",
                "intents",
                "intent_fields",
                "intent_revisions",
                "management_transitions",
                "findings",
                "ownership_findings",
                "observations",
                "observation_sweeps",
                "provider_observations",
            )
        }


def _runner() -> FakeRunner:
    from localplane.agent.providers.linux_network import ADDR_ARGV, LINK_ARGV

    return FakeRunner(
        {
            LINK_ARGV: json_result(
                LINK_ARGV,
                [
                    {"ifindex": 2, "ifname": "eth0"},
                    {"ifindex": 3, "ifname": "eth1"},
                    {"ifindex": 4, "ifname": "br0", "linkinfo": {"info_kind": "bridge"}},
                    {"ifindex": 5, "ifname": "veth0", "linkinfo": {"info_kind": "veth"}},
                ],
            ),
            ADDR_ARGV: json_result(
                ADDR_ARGV,
                [
                    {"ifindex": 1, "ifname": "lo", "addr_info": []},
                    {"ifindex": 2, "ifname": "eth0", "addr_info": []},
                    {"ifindex": 3, "ifname": "eth1", "addr_info": []},
                    {
                        "ifindex": 4,
                        "ifname": "br0",
                        "addr_info": [
                            {
                                "family": "inet",
                                "local": DOCKER0_GATEWAY,
                                "prefixlen": 16,
                                "scope": "global",
                            }
                        ],
                    },
                    {"ifindex": 5, "ifname": "veth0", "addr_info": []},
                ],
            ),
            **nmcli_devices(
                [
                    ("lo", "loopback", "connected (externally)", "lo", "uuid-lo"),
                    ("eth0", "ethernet", "unavailable", "", ""),
                    ("eth1", "ethernet", "unavailable", "", ""),
                    ("br0", "bridge", "connected (externally)", "br0", "uuid-br0"),
                ]
            ),
            **tailscale_status(),
        }
    )


@pytest.fixture
def estate(database, fake_root: Path, sysfs_net: Path, tmp_path: Path, docker_daemon) -> Estate:
    """Five links: a loopback, two adoptable ethernets, a bridge and a veth.

    ``eth0`` and ``eth1`` start clean — NetworkManager enumerates them and holds no active
    profile — so both are adoptable and a Run can be planned against either. ``br0`` is
    deliberately not called ``docker0``: the only thing that could ever tie it to Docker is
    the gateway address Docker declares for one of its networks.
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
        sysfs_net, "eth1", ifindex=3, address="02:00:00:00:00:12", flags="0x1003",
        operstate="up", carrier="1", mtu="1500", speed="1000", duplex="full",
        device="1-1.4:1.0", subsystem="usb",
    )
    write_interface(
        sysfs_net, "br0", ifindex=4, address="02:00:00:00:00:13", addr_assign_type="3",
        flags="0x1003", operstate="up", carrier="1", mtu="1500", devtype="bridge",
        bridge=True, speed=None, duplex=None,
    )
    write_interface(
        sysfs_net, "veth0", ifindex=5, address="02:00:00:00:00:14", addr_assign_type="3",
        flags="0x1303", operstate="up", carrier="1", mtu="1500", speed=None, duplex=None,
    )

    runner = _runner()
    provenance = ProvenanceService(database)
    management = ManagementService(database, freshness_ttl_s=60.0, provenance=provenance)
    ingestor = Ingestor(database, management)
    from localplane.backend.operations import OPERATIONS

    estate = Estate(
        database=database,
        sysfs=sysfs_net,
        runner=runner,
        service=None,  # type: ignore[arg-type]
        ingestor=ingestor,
        management=management,
        runs=RunService(database, 60.0, OPERATIONS, provenance),
        docker_socket=tmp_path / "docker.sock",
        fake_root=fake_root,
        daemons=docker_daemon,
    )
    estate.build_agent()
    estate.observe()
    return estate


@pytest.fixture
def drifted(estate: Estate) -> Estate:
    """``eth0`` adopted at MTU 1500 and now carrying 1400: a plan waiting to be made."""
    estate.adopt("eth0")
    estate.drift("eth0", "1400")
    return estate


def monitoring_network() -> dict[str, Any]:
    return docker_network(
        MONITORING_NETWORK,
        "monitoring_default",
        gateway=DOCKER0_GATEWAY,
        subnet="172.19.0.0/16",
        compose_project="monitoring",
    )


# ------------------------------------------------------------------------- the vocabulary


def test_the_run_state_vocabulary_is_complete_and_kept_whole():
    """All fourteen names, because the lifecycle is the product's, not a single feature's."""
    assert [str(s) for s in RunState] == [
        "draft",
        "preview",
        "awaiting_confirmation",
        "arming",
        "applying",
        "verifying",
        "guarded",
        "succeeded",
        "failed",
        "rolling_back",
        "rollback_verifying",
        "rolled_back",
        "recovery_required",
        "cancelled",
    ]


def test_only_the_truthful_states_are_reachable():
    """Thirteen of fourteen, and the one that is absent describes something absent.

    ``draft`` describes a Run without a published plan, which no act here produces.
    ``guarded`` is reachable now, and it describes what it always described: a change
    written to the object carrying the operator's own connection, with a host-side guard
    holding a reversal, waiting on the one fact nothing on this side can establish.
    """
    assert REACHABLE_RUN_STATES == (
        RunState.PREVIEW,
        RunState.AWAITING_CONFIRMATION,
        RunState.ARMING,
        RunState.APPLYING,
        RunState.VERIFYING,
        RunState.GUARDED,
        RunState.SUCCEEDED,
        RunState.FAILED,
        RunState.ROLLING_BACK,
        RunState.ROLLBACK_VERIFYING,
        RunState.ROLLED_BACK,
        RunState.RECOVERY_REQUIRED,
        RunState.CANCELLED,
    )
    assert set(RunState) - set(REACHABLE_RUN_STATES) == {RunState.DRAFT}


def test_the_store_accepts_no_run_state_beyond_the_reachable_ones(drifted: Estate):
    """Thirteen of the fourteen run states, and the other one is refused by the store.

    The set widened because execution exists, and again because a guard does; what did not
    widen is the principle. ``draft`` is unreachable because creating a Run *is* planning
    one, and a store that could hold it would be a store that could hold a claim no code
    path can make true.
    """
    outcome = drifted.plan("eth0")
    assert {str(s) for s in REACHABLE_RUN_STATES} == {
        "preview",
        "awaiting_confirmation",
        "arming",
        "applying",
        "verifying",
        "guarded",
        "succeeded",
        "failed",
        "rolling_back",
        "rollback_verifying",
        "rolled_back",
        "recovery_required",
        "cancelled",
    }
    assert {str(s) for s in RunState} - {str(s) for s in REACHABLE_RUN_STATES} == {"draft"}
    for state in RunState:
        if state in REACHABLE_RUN_STATES:
            continue
        with pytest.raises(sqlite3.IntegrityError):
            drifted.database.connection.execute(
                "UPDATE runs SET state = ? WHERE run_id = ?", (str(state), outcome.run.run_id)
            )


def test_the_operation_vocabulary_is_closed_and_every_member_is_named():
    """Seven, of two kinds, and named rather than counted.

    The Docker and systemd entries are *actions*, which is a different shape of change and
    not another field to reconcile — see ``PlannedAction``. An eighth arriving without a
    change that argued for it fails here.
    """
    assert [str(o) for o in OperationType] == [
        "network.interface.reconcile_mtu",
        "docker.container.start",
        "docker.container.stop",
        "docker.container.restart",
        "systemd.service.start",
        "systemd.service.stop",
        "systemd.service.restart",
    ]


def test_an_operation_localplane_does_not_implement_is_refused(drifted: Estate):
    """There is no ``custom``, no ``other`` and no free-text variant to fall through to."""
    with pytest.raises(ValueError):
        OperationType("network.interface.set_mtu")
    with pytest.raises(ValueError):
        OperationType("shell")


def test_the_store_accepts_no_operation_outside_the_vocabulary(drifted: Estate):
    outcome = drifted.plan("eth0")
    for name in ("network.interface.set_mtu", "exec", "custom", ""):
        with pytest.raises(sqlite3.IntegrityError):
            drifted.database.connection.execute(
                "UPDATE runs SET operation = ? WHERE run_id = ?", (name, outcome.run.run_id)
            )


def test_a_run_service_with_no_handler_for_an_operation_refuses(estate: Estate):
    """The registry is the allowlist. An unregistered type never reaches a planner."""
    empty = RunService(estate.database, 60.0, {})
    estate.adopt("eth0")
    with pytest.raises(RunRefused) as raised:
        empty.create(RECONCILE, estate.object_named("eth0"), UNOBSERVED)
    assert raised.value.code == "unsupported_operation"
    assert estate.database.query("SELECT * FROM runs") == []


def test_nothing_on_the_run_path_can_execute_anything():
    """A hard invariant: no generic executor, anywhere on this path.

    The engine, the policy and the one concrete operation are read for the vocabulary of
    execution — subprocess, shells, dynamic dispatch — and none of it is there. The planner
    resolves an operation to a *description* of what would be needed, and there is nowhere
    for a command to go even if one were produced.
    """
    root = Path(__file__).resolve().parents[1] / "src" / "localplane" / "backend"
    forbidden = (
        "subprocess",
        "os.system",
        "os.exec",
        "popen",
        "shell=",
        "getattr(provider",
        "eval(",
        "exec(",
    )
    for module in (
        root / "runs.py",
        root / "operations.py",
        root / "domain" / "runs.py",
        root / "domain" / "policy.py",
        root / "domain" / "protection.py",
        root / "domain" / "management_path.py",
        root / "management_path.py",
    ):
        source = module.read_text()
        for token in forbidden:
            assert token not in source, f"{module.name} mentions {token}"


def test_the_engine_core_knows_nothing_about_networking():
    """The seam, asserted rather than described.

    The Run engine and its policy must not name a network interface, an MTU or a Linux
    concept. The concrete planner may name all three, and does. This is what makes a second
    operation a new file rather than a rewrite of the persistence model.
    """
    root = Path(__file__).resolve().parents[1] / "src" / "localplane" / "backend"
    core = (root / "runs.py", root / "domain" / "policy.py", root / "domain" / "protection.py")
    for module in core:
        source = module.read_text()
        code = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )
        # The docstrings explain the seam by naming what is on the other side of it; the
        # code must not. Strip them, and the remainder may not mention an interface at all.
        without_docstrings = "".join(
            part for i, part in enumerate(code.split('"""')) if i % 2 == 0
        )
        for token in ("mtu", "interface", "link", "ethernet", "sysfs", "ip link"):
            assert token not in without_docstrings.lower(), f"{module.name} mentions {token}"
    # And the one operation that does know is not imported by either of them.
    for module in core:
        assert "backend.operations" not in module.read_text().replace(
            ":mod:`localplane.backend.operations`", ""
        )


# ------------------------------------------------------------------------- preconditions


def test_planning_requires_a_managed_object(estate: Estate):
    with pytest.raises(RunRefused) as raised:
        estate.plan("eth0")
    assert raised.value.code == "not_managed"
    assert estate.database.query("SELECT * FROM runs") == []


def test_an_observe_only_object_is_refused_before_anything_else(estate: Estate):
    for name in ("lo", "veth0"):
        with pytest.raises(RunRefused) as raised:
            estate.plan(name)
        assert raised.value.code == "object_observe_only", name


def test_planning_requires_the_active_intent_to_control_the_field(drifted: Estate, database):
    """A field nobody intends has no target to reconcile towards.

    The controlled set is narrowed directly in the store, because widening or narrowing it
    is not something any endpoint offers — which is the point: reconciling a value
    LocalPlane never agreed to be answerable for would be inventing an intention.
    """
    record = drifted.object_named("eth0")
    database.connection.execute(
        "DELETE FROM intent_fields WHERE intent_id = ? AND field = 'mtu'",
        (record.active_intent_id,),
    )
    with pytest.raises(RunRefused) as raised:
        drifted.plan("eth0")
    assert raised.value.code == "field_not_controlled"
    assert raised.value.detail["controlled_fields"] == ["admin_up"]


def test_an_observation_nobody_has_refreshed_refuses_the_plan(estate: Estate, database):
    """Freshness is checked before the values are, so this is what a caller is told."""
    estate.adopt("eth0")
    record = estate.object_named("eth0")
    database.connection.execute(
        "UPDATE observations SET observed_at = '1999-01-01T00:00:00+00:00' "
        "WHERE object_id = ?",
        (record.object_id,),
    )
    with pytest.raises(RunRefused) as raised:
        estate.plan("eth0")
    assert raised.value.code == "observation_stale"
    assert raised.value.detail["ttl_seconds"] == 60.0


def test_an_observation_from_another_source_refuses_the_plan(drifted: Estate, database):
    """The same field name from a different provider is not the same fact."""
    record = drifted.object_named("eth0")
    database.connection.execute(
        "UPDATE observations SET provider = 'some.other.provider' WHERE object_id = ?",
        (record.object_id,),
    )
    with pytest.raises(RunRefused) as raised:
        drifted.plan("eth0")
    assert raised.value.code == "observation_source_incompatible"


def test_an_unreadable_current_value_refuses_rather_than_assuming_one(estate: Estate):
    """Unknown is never converted into a value a plan could act on."""
    estate.adopt("eth0")
    estate.make_unreadable("eth0", "mtu")
    estate.observe()
    with pytest.raises(RunRefused) as raised:
        estate.plan("eth0")
    assert raised.value.code == "current_value_unreadable"
    assert raised.value.detail["observed"] is None
    assert estate.database.query("SELECT * FROM runs") == []


def test_a_runtime_that_already_agrees_is_a_refusal_not_an_empty_plan(estate: Estate):
    """Reconciling a value the host already has is work reported without work happening."""
    estate.adopt("eth0")
    with pytest.raises(RunRefused) as raised:
        estate.plan("eth0")
    assert raised.value.code == "already_reconciled"
    assert raised.value.detail["value"] == 1500
    assert estate.database.query("SELECT * FROM runs") == []
    assert estate.database.query("SELECT * FROM run_previews") == []


def test_a_refused_plan_leaves_neither_a_run_nor_a_preview(estate: Estate):
    for name in ("lo", "eth0"):
        with pytest.raises(RunRefused):
            estate.plan(name)
    assert estate.database.query("SELECT * FROM runs") == []
    assert estate.database.query("SELECT * FROM run_previews") == []


# -------------------------------------------------------------------------- what and why


def test_a_drifted_mtu_produces_the_before_the_desired_and_the_expected_after(drifted: Estate):
    outcome = drifted.plan("eth0")
    change = outcome.plan.change
    assert change.field == "mtu"
    assert change.value_type is ValueType.INTEGER
    assert change.current == 1400
    assert change.desired == 1500
    assert change.expected_after == 1500
    assert outcome.run.state == str(RunState.PREVIEW)


def test_the_target_comes_from_the_retained_intent_and_not_from_the_caller(drifted: Estate):
    """Revise the intent and the plan follows it. There is no other way to move it.

    ``RunService.create`` takes an operation and an object and nothing else: there is no
    parameter a desired value could arrive through, at any layer. An operator who wants
    1300 revises the intent to 1300 and plans against the authoritative version.
    """
    first = drifted.plan("eth0")
    assert first.plan.change.desired == 1500

    record = drifted.object_named("eth0")
    drifted.management.revise_intent(
        record, fields={"mtu": 9000}, expected_intent_id=record.active_intent_id or ""
    )
    second = drifted.plan("eth0")
    assert second.plan.change.desired == 9000
    assert second.plan.change.current == 1400

    import inspect

    parameters = set(inspect.signature(drifted.runs.create).parameters)
    assert parameters == {
        "operation", "record", "management_path", "systemd_lifecycle_context"
    }
    assert not parameters & {"desired", "value", "unit_id", "verb", "command", "argv"}


def test_the_preview_binds_the_exact_observation_it_read(drifted: Estate):
    outcome = drifted.plan("eth0")
    record = drifted.object_named("eth0")
    assert outcome.run.preview.observation_id == record.observation.observation_id
    assert outcome.run.preview.sweep_id == record.observation.sweep_id
    assert outcome.run.preview.observed_at == record.observation.observed_at


def test_the_preview_binds_the_exact_intent_id_and_version(drifted: Estate):
    record = drifted.object_named("eth0")
    active = drifted.management.intents.get(record.active_intent_id or "")
    outcome = drifted.plan("eth0")
    assert outcome.run.preview.intent_id == active.intent_id
    assert outcome.run.preview.intent_version == active.version
    assert outcome.run.preview.intent_capability == active.capability
    assert outcome.run.preview.intent_provider == active.provider


def test_the_preview_names_the_open_drift_finding_it_answers(drifted: Estate):
    record = drifted.object_named("eth0")
    open_now = drifted.management.findings.open_for_object(record.object_id)
    assert [f.subject for f in open_now] == ["mtu"]
    outcome = drifted.plan("eth0")
    assert outcome.plan.evidence.drift_finding_id == open_now[0].finding_id


def test_a_plan_can_be_made_before_a_sweep_has_turned_the_drift_into_a_finding(
    estate: Estate, database
):
    """The finding is evidence, not a precondition. A disagreement is real either way."""
    estate.adopt("eth0")
    estate.drift("eth0", "1400")
    record = estate.object_named("eth0")
    database.connection.execute(
        "DELETE FROM findings WHERE object_id = ?", (record.object_id,)
    )
    outcome = estate.plan("eth0")
    assert outcome.plan.evidence.drift_finding_id is None
    assert outcome.plan.change.current == 1400


# ------------------------------------------------------- ownership, protection, execution


def test_a_clean_object_is_blocked_on_a_host_with_no_privileged_helper(drifted: Estate):
    """Two different questions, and only one of them has changed.

    ``availability`` is about the build, and the build now has execution code, so it is
    ``available``. ``eligibility`` is about this plan on this host, and this host's agent
    has no privileged helper to reach — so the capability is not declared and the plan is
    blocked, with the blocker naming the thing that is actually missing rather than
    pretending LocalPlane never implemented the feature.
    """
    outcome = drifted.plan("eth0")
    execution = outcome.plan.execution
    assert execution.availability is ExecutionAvailability.AVAILABLE
    assert execution.eligibility is ExecutionEligibility.BLOCKED
    assert "required_capability_undeclared" in execution.blockers
    assert "execution_not_implemented" not in execution.blockers
    assert outcome.plan.ownership.gaps == ()
    assert outcome.plan.ownership.reason == "host_kernel_only"


def test_an_externally_configured_object_is_execution_blocked(drifted: Estate):
    """Adopted while clean, then NetworkManager took it over. The plan says so.

    This is the shape the ownership axis exists for, and it is why ownership does not
    refuse a plan: the preview still says MTU 1400 → 1500, and it also says that executing
    it would make LocalPlane a second writer.
    """
    drifted.set_networkmanager(
        [
            ("lo", "loopback", "connected (externally)", "lo", "uuid-lo"),
            ("eth0", "ethernet", "connected", "Wired 1", "uuid-eth0"),
            ("eth1", "ethernet", "unavailable", "", ""),
            ("br0", "bridge", "connected (externally)", "br0", "uuid-br0"),
        ]
    )
    drifted.observe()

    outcome = drifted.plan("eth0")
    assert outcome.plan.change.current == 1400 and outcome.plan.change.desired == 1500
    assert "externally_configured" in outcome.plan.execution.blockers
    assert outcome.plan.execution.eligibility is ExecutionEligibility.BLOCKED
    assert outcome.plan.risk.tier is RiskTier.HIGH
    claims = {(c.relation, c.provider) for c in outcome.plan.ownership.claims}
    assert ("configured_by", "networkmanager") in claims


def test_conflicting_ownership_claims_block_execution(estate: Estate):
    """Docker and NetworkManager both configuring one bridge. Nobody picks a winner.

    ``br0`` is adopted while nothing claims it — no Docker daemon is running and
    NetworkManager describes it as externally configured, which is a posture and not a
    claim. Then both systems claim to be configuring it, which is a condition to report
    rather than one to resolve by choosing.
    """
    estate.adopt("br0")
    estate.write("br0", "mtu", "1400")

    estate.start_docker(monitoring_network())
    estate.set_networkmanager(
        [
            ("lo", "loopback", "connected (externally)", "lo", "uuid-lo"),
            ("eth0", "ethernet", "unavailable", "", ""),
            ("eth1", "ethernet", "unavailable", "", ""),
            ("br0", "bridge", "connected", "bridge-br0", "uuid-br0"),
        ]
    )
    estate.observe()

    provenance = estate.management.provenance.for_object(estate.object_named("br0"))
    assert [str(r) for r in provenance.conflicting_relations] == ["configured_by"]

    outcome = estate.runs.create(RECONCILE, estate.object_named("br0"), UNOBSERVED)
    assert "conflicting_ownership_claims" in outcome.plan.execution.blockers
    assert outcome.plan.risk.tier is RiskTier.HIGH
    assert outcome.plan.change.current == 1400
    assert outcome.plan.change.desired == 1500


def test_incomplete_provider_evidence_blocks_execution(estate: Estate):
    """Stricter than adoption, and deliberately so.

    Adopt records values the host already has, so an unexamined source is reported and does
    not refuse it. A write would be LocalPlane acting, and a source that might have named
    another owner and could not be read is not a clean bill of health.
    """
    estate.adopt("eth0")
    estate.write("eth0", "mtu", "1400")
    estate.forget_provider_evidence()
    estate.observe(providers=False)

    # A gap names the *source* that left the question open, not merely the provider.
    unconsulted = {"docker.networks", "networkmanager.devices", "tailscale.status"}
    outcome = estate.plan("eth0")
    assert set(outcome.plan.ownership.gaps) == unconsulted
    assert "ownership_evidence_incomplete" in outcome.plan.execution.blockers
    assert "ownership_evidence_incomplete" in {f.code for f in outcome.plan.risk.factors}
    # And adoption's own view of the same gaps is unchanged: it reports them and allows.
    eligibility = estate.management.provenance.eligibility(estate.object_named("eth1"))
    assert eligibility.eligible is True
    assert set(eligibility.evidence_gaps) == unconsulted


def test_the_management_path_is_unknown_and_says_what_would_settle_it(drifted: Estate):
    """Nothing observed, so nothing proven — and the reason names which of the two."""
    outcome = drifted.plan("eth0")
    protection = outcome.plan.protection
    assert protection.status is ProtectionStatus.UNKNOWN
    assert protection.management_path is ManagementPathRelation.UNKNOWN
    assert protection.reason == "management_path_unobserved"
    assert set(protection.missing_evidence) == set(MANAGEMENT_PATH_EVIDENCE)
    assert "management_path_unproven" in outcome.plan.execution.blockers


def test_the_management_path_is_never_inferred_from_a_name_or_a_shape(drifted: Estate):
    """Every interface gets the same answer, whatever it is called and however it looks."""
    drifted.adopt("eth1")
    drifted.write("eth1", "mtu", "1400")
    drifted.observe()
    for name in ("eth0", "eth1"):
        outcome = drifted.plan(name)
        assert outcome.plan.protection.management_path is ManagementPathRelation.UNKNOWN


def test_unknown_evidence_never_becomes_a_safe_answer(estate: Estate):
    """Nothing unproven is allowed to read as proven-safe, anywhere in a plan."""
    estate.adopt("eth0")
    estate.write("eth0", "mtu", "1400")
    estate.forget_provider_evidence()
    estate.observe(providers=False)
    plan = estate.plan("eth0").plan

    assert plan.protection.management_path is ManagementPathRelation.UNKNOWN
    assert plan.execution.eligibility is ExecutionEligibility.BLOCKED
    assert plan.risk.tier is not RiskTier.LOW
    assert plan.recovery.armed is False
    assert plan.verification.executed is False
    assert plan.confirmation.token_issued is False


def test_the_required_capability_is_real_now_and_is_still_checked_rather_than_assumed(
    drifted: Estate,
):
    """It is in the protocol's vocabulary, and this host still does not have it.

    The capability stopped being a name a preview used to describe what was missing and
    became a declared, mutating capability. What did not change is that a plan reports
    whether *this agent probed it and found it*, rather than concluding it exists because
    LocalPlane knows the concept — and this fixture's agent has no helper to find.
    """
    outcome = drifted.plan("eth0")
    assert outcome.plan.execution.required_capability == CAPABILITY_SET_MTU
    assert CAPABILITY_SET_MTU in CAPABILITIES
    assert CAPABILITIES[CAPABILITY_SET_MTU].mutating is True
    assert outcome.plan.execution.capability_declared_by_agent is False
    assert "required_capability_undeclared" in outcome.plan.execution.blockers
    probed = {c.capability: c for c in drifted.service.capabilities}
    assert str(probed[CAPABILITY_SET_MTU].status) == "unavailable"
    assert probed[CAPABILITY_SET_MTU].reason == "helper_unavailable"


def test_the_write_provider_is_named_now_that_one_exists(drifted: Estate):
    """0005 CHECKed this column NULL because naming a provider would have invented a path.

    One exists now and it is not a plausible-sounding daemon: it is the privileged helper's
    fixed ``RTM_NEWLINK``/``IFLA_MTU`` message, and the name is the provider that sends it.
    """
    outcome = drifted.plan("eth0")
    assert outcome.plan.execution.provider == "linux.link"
    assert outcome.run.preview.execution_provider == "linux.link"


def test_the_store_cannot_record_execution_as_available_or_eligible(drifted: Estate):
    outcome = drifted.plan("eth0")
    preview = outcome.run.preview.preview_id
    for column, value in (
        ("execution_availability", "available"),
        ("execution_availability", "unavailable"),
        ("execution_eligibility", "eligible"),
        ("recovery_armed", 1),
        ("verification_executed", 1),
        ("confirmation_token_issued", 1),
        ("protection_management_path", "somewhere_else"),
        ("protection_status", "safe"),
        ("execution_provider", "nmcli"),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            drifted.database.connection.execute(
                f"UPDATE run_previews SET {column} = ? WHERE preview_id = ?", (value, preview)
            )


# ---------------------------------------------------------------- risk and confirmation


def test_risk_starts_at_the_operations_tier_and_is_only_ever_raised(drifted: Estate):
    outcome = drifted.plan("eth0")
    assert RECONCILE_MTU.base_risk is RiskTier.MEDIUM
    assert outcome.plan.risk.tier is RiskTier.MEDIUM
    codes = {f.code for f in outcome.plan.risk.factors}
    assert "operation_base_risk" in codes
    assert "management_path_unproven" in codes


def test_an_unproven_management_path_sets_a_floor_even_for_a_low_risk_operation():
    """The rule survives an operation that would otherwise be low risk."""
    harmless = replace(RECONCILE_MTU, base_risk=RiskTier.LOW)
    protection = unproven_protection()
    risk = assess_risk(
        harmless, ownership_block_reason=None, ownership_gaps=(), protection=protection
    )
    assert risk.tier is RiskTier.MEDIUM
    assert "management_path_unproven" in {f.code for f in risk.factors}


def test_an_operation_off_the_management_path_is_not_floored_by_it():
    """The floor comes from the evidence, not from a blanket rule."""
    unrelated = replace(RECONCILE_MTU, base_risk=RiskTier.LOW, can_affect_management_path=False)
    risk = assess_risk(
        unrelated,
        ownership_block_reason=None,
        ownership_gaps=(),
        protection=unproven_protection(),
    )
    assert risk.tier is RiskTier.LOW


def test_confirmation_is_required_by_policy_and_says_so(drifted: Estate):
    outcome = drifted.plan("eth0")
    confirmation = outcome.plan.confirmation
    assert confirmation.required is True
    assert confirmation.method is ConfirmationMethod.TYPED
    assert confirmation.source == "policy"
    assert str(CONFIRM_FROM_RISK) in " ".join(confirmation.reasons)


def test_an_operation_may_strengthen_confirmation_and_can_never_weaken_it():
    """The safety decision belongs to the host, not to the operation."""
    low = replace(
        RECONCILE_MTU,
        base_risk=RiskTier.LOW,
        can_affect_management_path=False,
        confirmation_requested=False,
    )
    protection = unproven_protection()
    relaxed = effective_confirmation(
        low,
        risk=assess_risk(
            low, ownership_block_reason=None, ownership_gaps=(), protection=protection
        ),
        protection=protection,
        execution_available=False,
    )
    assert relaxed.required is False

    asked = replace(low, confirmation_requested=True, confirmation_reason="the operator asked")
    stronger = effective_confirmation(
        asked,
        risk=assess_risk(
            asked, ownership_block_reason=None, ownership_gaps=(), protection=protection
        ),
        protection=protection,
        execution_available=False,
    )
    assert stronger.required is True
    assert stronger.source == "operation"

    # And an operation that declares it wants none still gets one when the policy says so.
    risky = replace(RECONCILE_MTU, confirmation_requested=False)
    imposed = effective_confirmation(
        risky,
        risk=assess_risk(
            risky, ownership_block_reason=None, ownership_gaps=(), protection=protection
        ),
        protection=protection,
        execution_available=False,
    )
    assert imposed.required is True
    assert imposed.source == "policy"


def test_no_confirmation_token_is_issued_even_though_confirmation_is_now_real(
    drifted: Estate,
):
    """A confirmation became satisfiable. It did not become a bearer value.

    ``token_issued`` is still false on every path and the store still CHECKs the column to
    zero, because there is nothing to issue: a confirmation is a row naming one Run and one
    published plan, and the only thing that can use it is an apply of that Run.
    """
    outcome = drifted.plan("eth0")
    assert outcome.plan.confirmation.token_issued is False
    assert outcome.run.preview.confirmation_token_issued is False
    # Satisfiable now, because execution exists. Which is a different claim from issued.
    assert outcome.plan.confirmation.satisfiable is True
    assert outcome.plan.confirmation.unsatisfiable_reason is None
    with pytest.raises(sqlite3.IntegrityError):
        drifted.database.connection.execute(
            "UPDATE run_previews SET confirmation_token_issued = 1 WHERE preview_id = ?",
            (outcome.run.preview.preview_id,),
        )


def test_the_policy_sentence_is_stored_with_the_plan_that_was_reviewed_under_it(
    drifted: Estate,
):
    """An older preview is a pending semantic upgrade, never re-read under a later policy."""
    outcome = drifted.plan("eth0")
    assert outcome.run.preview.confirmation_policy == outcome.plan.confirmation.policy
    assert "management path" in outcome.run.preview.confirmation_policy


# ---------------------------------------------------------- recovery and verification


def test_rollback_is_described_as_a_capability_and_never_performed(drifted: Estate):
    outcome = drifted.plan("eth0")
    recovery = outcome.plan.recovery
    assert recovery.mode is RecoveryMode.AUTO
    assert recovery.rollback_possible is True
    assert recovery.armed is False
    assert recovery.guarantee == "none"
    # A preview is published before anything is armed, and says so. Arming happens during
    # apply, in `run_checkpoints`, after the confirmation is consumed.
    assert "nothing is armed" in recovery.reason
    assert outcome.run.preview.recovery_armed is False


def test_recovery_is_never_reported_as_armed_on_any_path(estate: Estate):
    estate.adopt("eth0")
    estate.adopt("eth1")
    estate.write("eth0", "mtu", "1400")
    estate.write("eth1", "mtu", "9000")
    estate.observe()
    for name in ("eth0", "eth1"):
        assert estate.plan(name).plan.recovery.armed is False
    assert [r["recovery_armed"] for r in estate.database.query("SELECT * FROM run_previews")] == [
        0,
        0,
    ]


def test_verification_is_described_and_never_executed(drifted: Estate):
    outcome = drifted.plan("eth0")
    record = drifted.object_named("eth0")
    active = drifted.management.intents.get(record.active_intent_id or "")
    verification = outcome.plan.verification
    assert verification.executed is False
    assert verification.capability == active.capability
    assert verification.provider == active.provider
    assert "re-read" in verification.condition


def test_a_published_plan_contains_no_command_no_argv_and_no_executable(drifted: Estate):
    """Transparency is showing what is known, and no command is known."""
    outcome = drifted.plan("eth0")
    row = drifted.database.query_one(
        "SELECT * FROM run_previews WHERE preview_id = ?", (outcome.run.preview.preview_id,)
    )
    published = json.dumps({k: row[k] for k in row.keys()}).lower()
    for token in ("ip link", "nmcli", "/sbin", "/usr/bin", "argv", "exit 0", "stdout"):
        assert token not in published, token


# ---------------------------------------------------------------------------- the digest


def test_the_digest_is_deterministic_for_the_same_plan(drifted: Estate):
    first = drifted.plan("eth0")
    second = drifted.plan("eth0")
    assert first.run.preview.preview_digest == second.run.preview.preview_digest
    assert first.run.preview.preview_digest.startswith("sha256:")
    assert len(first.run.preview.preview_digest) == len("sha256:") + 64


def test_the_digest_survives_key_order_and_ignores_reading_time(drifted: Estate):
    """Order and timestamps must not move it; the values it is about must.

    The canonical document is serialised with sorted keys, so a plan assembled in a
    different order hashes the same. And nothing that merely moves with the clock is in it:
    the published time, the observation's time and the observation's identifier are all
    absent, so re-reading a host and finding it unchanged does not invalidate the plan
    somebody is holding.
    """
    outcome = drifted.plan("eth0")
    document = canonical_plan(outcome.plan)
    reordered = dict(reversed(list(document.items())))
    assert json.dumps(document, sort_keys=True) == json.dumps(reordered, sort_keys=True)

    flat = json.dumps(document)
    assert outcome.plan.evidence.observation_id not in flat
    assert outcome.plan.evidence.observed_at not in flat
    assert outcome.run.preview.published_at not in flat
    for reading in outcome.plan.ownership.readings.values():
        if reading:
            assert reading not in flat


def test_a_sweep_that_changes_nothing_does_not_move_the_digest(drifted: Estate):
    before = drifted.plan("eth0")
    drifted.observe()
    after = drifted.plan("eth0")
    assert before.run.preview.observation_id != after.run.preview.observation_id
    assert before.run.preview.preview_digest == after.run.preview.preview_digest


def test_a_meaningful_change_to_the_plan_moves_the_digest(drifted: Estate):
    before = drifted.plan("eth0")
    drifted.write("eth0", "mtu", "1300")
    drifted.observe()
    after = drifted.plan("eth0")
    assert after.plan.change.current == 1300
    assert after.run.preview.preview_digest != before.run.preview.preview_digest


def test_every_section_of_the_canonical_document_is_load_bearing(drifted: Estate):
    """Changing any one part of the published plan changes its identity."""
    outcome = drifted.plan("eth0")
    baseline = plan_digest(outcome.plan)
    mutations = (
        replace(outcome.plan, host_id="other-host"),
        replace(outcome.plan, object_id="obj_other"),
        replace(outcome.plan, change=replace(outcome.plan.change, desired=9000)),
        replace(outcome.plan, change=replace(outcome.plan.change, current=1300)),
        replace(outcome.plan, evidence=replace(outcome.plan.evidence, intent_version=99)),
        replace(outcome.plan, risk=replace(outcome.plan.risk, tier=RiskTier.HIGH)),
        replace(
            outcome.plan,
            ownership=replace(outcome.plan.ownership, reason="externally_configured"),
        ),
        replace(
            outcome.plan,
            protection=replace(outcome.plan.protection, reason="something_else"),
        ),
        replace(
            outcome.plan,
            execution=replace(outcome.plan.execution, blockers=("execution_not_implemented",)),
        ),
        replace(
            outcome.plan, recovery=replace(outcome.plan.recovery, mode=RecoveryMode.NONE)
        ),
        replace(
            outcome.plan,
            verification=replace(outcome.plan.verification, condition="something else"),
        ),
        replace(
            outcome.plan,
            confirmation=replace(outcome.plan.confirmation, method=ConfirmationMethod.NONE),
        ),
    )
    for mutated in mutations:
        assert plan_digest(mutated) != baseline


def test_evidence_identifiers_are_bound_to_the_preview_without_being_in_the_digest(
    drifted: Estate,
):
    """Both halves of the rule, together.

    The observation is *bound* — its id is a column with a foreign key, so what a plan was
    made from is answerable forever. It is not part of the plan's *identity*, because a
    sweep that re-read the same value did not produce a different plan.
    """
    outcome = drifted.plan("eth0")
    record = drifted.object_named("eth0")
    assert outcome.run.preview.observation_id == record.observation.observation_id
    assert outcome.plan.evidence.observation_id not in json.dumps(canonical_plan(outcome.plan))


def test_the_stored_digest_can_be_recomputed_from_the_row_alone(drifted: Estate):
    """Content-addressing, not mere repeatability: the row carries everything the hash covers."""
    outcome = drifted.plan("eth0")
    stored = drifted.runs.get(outcome.run.run_id)
    rebuilt = drifted.runs.published_plan(stored)
    assert plan_digest(rebuilt) == stored.preview.preview_digest
    assert stored.preview.digest_version == PLAN_DIGEST_VERSION


# ---------------------------------------------------------------------- immutability


def test_a_published_preview_cannot_be_updated(drifted: Estate):
    outcome = drifted.plan("eth0")
    preview = outcome.run.preview.preview_id
    for column, value in (
        ("desired_value", 9000),
        ("current_value", 1),
        ("risk_tier", "low"),
        ("preview_digest", "sha256:" + "0" * 64),
        ("published_at", "2000-01-01T00:00:00+00:00"),
        ("ownership_reason", "no_provider_claim"),
    ):
        with pytest.raises(sqlite3.IntegrityError) as raised:
            drifted.database.connection.execute(
                f"UPDATE run_previews SET {column} = ? WHERE preview_id = ?", (value, preview)
            )
        assert "immutable" in str(raised.value)


def test_a_published_preview_cannot_be_deleted_while_a_run_names_it(drifted: Estate):
    outcome = drifted.plan("eth0")
    with pytest.raises(sqlite3.IntegrityError):
        drifted.database.connection.execute(
            "DELETE FROM run_previews WHERE preview_id = ?", (outcome.run.preview.preview_id,)
        )


def test_a_run_cannot_be_repointed_at_another_plan(drifted: Estate):
    """The failure this build exists to prevent: one identity, a different plan."""
    first = drifted.plan("eth0")
    drifted.write("eth0", "mtu", "1300")
    drifted.observe()
    second = drifted.plan("eth0")
    with pytest.raises(sqlite3.IntegrityError):
        drifted.database.connection.execute(
            "UPDATE runs SET preview_id = ? WHERE run_id = ?",
            (second.run.preview.preview_id, first.run.run_id),
        )


def test_a_runs_target_and_operation_are_immutable(drifted: Estate):
    outcome = drifted.plan("eth0")
    for column, value in (
        ("object_id", drifted.object_named("eth1").object_id),
        ("operation", "network.interface.reconcile_mtu"),
        ("created_at", "2000-01-01T00:00:00+00:00"),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            drifted.database.connection.execute(
                f"UPDATE runs SET {column} = ? WHERE run_id = ?", (value, outcome.run.run_id)
            )
    # `host_effect` has left that list, because it is now a fact that moves: a Run that
    # crosses the boundary records what became of the host. What a Run *is* has not moved.
    assert "host_effect" not in drifted.database.query_one(
        "SELECT sql FROM sqlite_master WHERE name = 'runs_identity_is_immutable'"
    )["sql"]


def _clone_preview(estate: Estate, preview_id: str, **changes: Any) -> str:
    """Copy a published preview, changing named columns. For the trigger tests only.

    Built from ``PRAGMA table_info`` rather than a literal column list so that a column
    added later cannot make these tests quietly stop covering what they claim to.
    """
    columns = [r["name"] for r in estate.database.query("PRAGMA table_info(run_previews)")]
    row = dict(
        estate.database.query_one(
            "SELECT * FROM run_previews WHERE preview_id = ?", (preview_id,)
        )
    )
    row.update(changes)
    row["preview_id"] = changes.get("preview_id", "prv_clone")
    estate.database.connection.execute(
        f"INSERT INTO run_previews ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        tuple(row[c] for c in columns),
    )
    return row["preview_id"]


def test_the_store_refuses_a_run_whose_plan_cites_another_objects_intent(drifted: Estate):
    """A plan about eth0 taking its target from eth1's intent is coherent and false."""
    drifted.adopt("eth1")
    outcome = drifted.plan("eth0")
    eth0 = drifted.object_named("eth0")
    eth1 = drifted.object_named("eth1")
    preview = _clone_preview(
        drifted, outcome.run.preview.preview_id, intent_id=eth1.active_intent_id
    )
    with pytest.raises(sqlite3.IntegrityError) as raised:
        drifted.database.connection.execute(
            "INSERT INTO runs VALUES ('run_x', ?, ?, 'network.interface.reconcile_mtu', "
            "'preview', ?, 'none', 't', NULL, NULL)",
            (eth0.host_id, eth0.object_id, preview),
        )
    assert "intent" in str(raised.value)


def test_the_store_refuses_a_run_whose_plan_read_another_objects_observation(
    drifted: Estate,
):
    """And the current value has to have been read from the object being planned against."""
    outcome = drifted.plan("eth0")
    eth0 = drifted.object_named("eth0")
    eth1 = drifted.object_named("eth1")
    preview = _clone_preview(
        drifted,
        outcome.run.preview.preview_id,
        observation_id=eth1.observation.observation_id,
        sweep_id=eth1.observation.sweep_id,
    )
    with pytest.raises(sqlite3.IntegrityError) as raised:
        drifted.database.connection.execute(
            "INSERT INTO runs VALUES ('run_y', ?, ?, 'network.interface.reconcile_mtu', "
            "'preview', ?, 'none', 't', NULL, NULL)",
            (eth0.host_id, eth0.object_id, preview),
        )
    assert "observation" in str(raised.value)


def test_the_store_cannot_record_a_host_effect_on_a_run(drifted: Estate):
    outcome = drifted.plan("eth0")
    with pytest.raises(sqlite3.IntegrityError):
        drifted.database.connection.execute(
            "UPDATE runs SET host_effect = 'wrote' WHERE run_id = ?", (outcome.run.run_id,)
        )


def test_a_plan_whose_two_ends_agree_cannot_be_stored(drifted: Estate):
    outcome = drifted.plan("eth0")
    with pytest.raises(sqlite3.IntegrityError):
        drifted.database.connection.execute(
            "UPDATE run_previews SET desired_value = current_value WHERE preview_id = ?",
            (outcome.run.preview.preview_id,),
        )


# ----------------------------------------------------------------------------- staleness


def test_a_newer_observation_that_changes_the_value_makes_the_preview_stale(drifted: Estate):
    outcome = drifted.plan("eth0")
    assert outcome.validity.state is PlanValidityState.CURRENT

    drifted.write("eth0", "mtu", "1300")
    drifted.observe()
    validity = drifted.validity(drifted.runs.get(outcome.run.run_id))
    assert validity.state is PlanValidityState.STALE
    assert [r.code for r in validity.reasons] == ["planned_values_changed"]
    assert validity.reasons[0].detail["was"]["current"] == 1400
    assert validity.reasons[0].detail["now"]["current"] == 1300


def test_a_newer_observation_that_changes_nothing_leaves_the_preview_current(drifted: Estate):
    """Staleness fires on material change, not on somebody having looked again."""
    outcome = drifted.plan("eth0")
    for _ in range(3):
        drifted.observe()
    assert drifted.validity(drifted.runs.get(outcome.run.run_id)).state is (
        PlanValidityState.CURRENT
    )


def test_an_intent_revision_makes_the_preview_stale(drifted: Estate):
    outcome = drifted.plan("eth0")
    record = drifted.object_named("eth0")
    drifted.management.revise_intent(
        record, fields={"mtu": 9000}, expected_intent_id=record.active_intent_id or ""
    )
    validity = drifted.validity(drifted.runs.get(outcome.run.run_id))
    assert validity.state is PlanValidityState.STALE
    assert set(r.code for r in validity.reasons) == {"planned_values_changed", "intent_replaced"}


def test_adopting_the_runtime_makes_the_preview_stale_by_removing_the_disagreement(
    drifted: Estate,
):
    outcome = drifted.plan("eth0")
    record = drifted.object_named("eth0")
    drifted.management.adopt_runtime_as_intent(
        record, expected_intent_id=record.active_intent_id or ""
    )
    validity = drifted.validity(drifted.runs.get(outcome.run.run_id))
    assert validity.state is PlanValidityState.STALE
    assert [r.code for r in validity.reasons] == ["already_reconciled"]


def test_releasing_the_object_makes_the_preview_stale(drifted: Estate):
    outcome = drifted.plan("eth0")
    drifted.management.release(drifted.object_named("eth0"))
    validity = drifted.validity(drifted.runs.get(outcome.run.run_id))
    assert validity.state is PlanValidityState.STALE
    assert [r.code for r in validity.reasons] == ["not_managed"]


def test_an_ownership_change_makes_the_preview_stale(drifted: Estate):
    """The plan can still be described; what it would mean to execute it has changed."""
    outcome = drifted.plan("eth0")
    drifted.set_networkmanager(
        [
            ("lo", "loopback", "connected (externally)", "lo", "uuid-lo"),
            ("eth0", "ethernet", "connected", "Wired 1", "uuid-eth0"),
            ("eth1", "ethernet", "unavailable", "", ""),
            ("br0", "bridge", "connected (externally)", "br0", "uuid-br0"),
        ]
    )
    drifted.observe()
    validity = drifted.validity(drifted.runs.get(outcome.run.run_id))
    assert validity.state is PlanValidityState.STALE
    codes = {r.code for r in validity.reasons}
    assert "ownership_changed" in codes
    assert "execution_changed" in codes
    assert "risk_changed" in codes


def test_an_observation_that_stops_being_readable_makes_the_preview_stale(drifted: Estate):
    outcome = drifted.plan("eth0")
    drifted.make_unreadable("eth0", "mtu")
    drifted.observe()
    validity = drifted.validity(drifted.runs.get(outcome.run.run_id))
    assert validity.state is PlanValidityState.STALE
    assert [r.code for r in validity.reasons] == ["current_value_unreadable"]


def test_a_stale_preview_is_never_rewritten_in_place(drifted: Estate):
    """The published plan is what was shown. Re-planning is a new Run, not an edit."""
    outcome = drifted.plan("eth0")
    before = drifted.database.query_one(
        "SELECT * FROM run_previews WHERE preview_id = ?", (outcome.run.preview.preview_id,)
    )
    drifted.write("eth0", "mtu", "1300")
    drifted.observe()
    for _ in range(3):
        drifted.validity(drifted.runs.get(outcome.run.run_id))
    after = drifted.database.query_one(
        "SELECT * FROM run_previews WHERE preview_id = ?", (outcome.run.preview.preview_id,)
    )
    assert tuple(before) == tuple(after)


def test_a_run_whose_operation_a_build_no_longer_implements_is_stale_not_an_error(
    drifted: Estate,
):
    outcome = drifted.plan("eth0")
    forgetful = RunService(drifted.database, 60.0, {})
    validity = forgetful.validity(forgetful.get(outcome.run.run_id), UNOBSERVED)
    assert validity.state is PlanValidityState.STALE
    assert [r.code for r in validity.reasons] == ["unsupported_operation"]


# ---------------------------------------------------------------- a Run is not a Change


def test_planning_creates_no_change_even_though_a_changes_table_now_exists(
    drifted: Estate,
):
    """The table arrived with the boundary. Planning still does not reach one.

    0005 said an empty ``changes`` table would be a promise. The promise is kept now, and
    the distinction it was protecting is unchanged: a Change is created by applying and by
    nothing else — not by publishing a plan, not by confirming it, and not by arming one.
    """
    outcome = drifted.plan("eth0")
    assert outcome.run.host_effect == "none"
    tables = {
        row["name"]
        for row in drifted.database.query("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"runs", "run_previews", "changes", "run_checkpoints", "run_confirmations"} <= tables
    assert drifted.database.query("SELECT * FROM changes") == []
    assert drifted.database.query("SELECT * FROM run_checkpoints") == []
    assert drifted.database.query("SELECT * FROM run_confirmations") == []
    assert drifted.database.query("SELECT * FROM object_write_locks") == []


def test_planning_moves_no_other_truth_at_all(drifted: Estate):
    """Not management, not intent, not reconciliation, not findings, not the host."""
    before_store = drifted.store_snapshot()
    before_tree = drifted.link_snapshot()

    drifted.plan("eth0")

    assert drifted.store_snapshot() == before_store
    assert drifted.link_snapshot() == before_tree


def test_planning_does_not_resolve_the_drift_it_describes(drifted: Estate):
    """A preview saying "this would reconcile MTU" is not remediation."""
    record = drifted.object_named("eth0")
    open_before = drifted.management.findings.open_for_object(record.object_id)
    drifted.plan("eth0")
    open_after = drifted.management.findings.open_for_object(record.object_id)
    assert [f.finding_id for f in open_after] == [f.finding_id for f in open_before]
    assert all(f.status == "open" for f in open_after)

    active = drifted.management.intents.active_for([record.object_id])[record.object_id]
    reconciliation = drifted.management.reconciliation_for(record, active)
    assert str(reconciliation.state) == "drifted"


def test_a_run_is_not_a_management_transition_and_not_an_intent_revision(drifted: Estate):
    transitions_before = drifted.database.query("SELECT * FROM management_transitions")
    revisions_before = drifted.database.query("SELECT * FROM intent_revisions")
    outcome = drifted.plan("eth0")
    drifted.runs.cancel(outcome.run)
    assert drifted.database.query("SELECT * FROM management_transitions") == transitions_before
    assert drifted.database.query("SELECT * FROM intent_revisions") == revisions_before
    # Three concepts, three tables, and a Run is in none of the other two.
    assert len(drifted.database.query("SELECT * FROM runs")) == 1


# -------------------------------------------------------------------------- cancellation


def test_cancelling_before_the_boundary_creates_no_change_and_moves_nothing(drifted: Estate):
    outcome = drifted.plan("eth0")
    before_store = drifted.store_snapshot()
    before_tree = drifted.link_snapshot()

    cancelled = drifted.runs.cancel(outcome.run)

    assert cancelled.state == str(RunState.CANCELLED)
    assert cancelled.cancelled_at is not None
    assert cancelled.host_effect == "none"
    assert drifted.store_snapshot() == before_store
    assert drifted.link_snapshot() == before_tree


def test_a_cancelled_run_keeps_the_plan_it_published(drifted: Estate):
    outcome = drifted.plan("eth0")
    published = drifted.database.query_one(
        "SELECT * FROM run_previews WHERE preview_id = ?", (outcome.run.preview.preview_id,)
    )
    drifted.runs.cancel(outcome.run)
    after = drifted.runs.get(outcome.run.run_id)
    assert after.state == str(RunState.CANCELLED)
    assert tuple(published) == tuple(
        drifted.database.query_one(
            "SELECT * FROM run_previews WHERE preview_id = ?", (outcome.run.preview.preview_id,)
        )
    )
    assert drifted.runs.published_plan(after).change.desired == 1500


def test_a_cancelled_run_cannot_be_cancelled_again(drifted: Estate):
    outcome = drifted.plan("eth0")
    cancelled = drifted.runs.cancel(outcome.run)
    with pytest.raises(RunRefused) as raised:
        drifted.runs.cancel(cancelled)
    assert raised.value.code == "run_not_cancellable"
    assert raised.value.detail["state"] == str(RunState.CANCELLED)


def test_the_schema_will_not_hold_a_cancelled_run_without_a_time(drifted: Estate):
    outcome = drifted.plan("eth0")
    with pytest.raises(sqlite3.IntegrityError):
        drifted.database.connection.execute(
            "UPDATE runs SET state = 'cancelled' WHERE run_id = ?", (outcome.run.run_id,)
        )


# --------------------------------------------------------------------------------- purity


def test_reading_a_run_writes_nothing(drifted: Estate):
    outcome = drifted.plan("eth0")
    before = drifted.store_snapshot()
    before_runs = [tuple(r) for r in drifted.database.query("SELECT * FROM runs")]
    before_previews = [tuple(r) for r in drifted.database.query("SELECT * FROM run_previews")]

    for _ in range(3):
        run = drifted.runs.get(outcome.run.run_id)
        drifted.runs.published_plan(run)
        drifted.validity(run)
        drifted.runs.list_for_host(drifted.host_id)

    assert drifted.store_snapshot() == before
    assert [tuple(r) for r in drifted.database.query("SELECT * FROM runs")] == before_runs
    assert [
        tuple(r) for r in drifted.database.query("SELECT * FROM run_previews")
    ] == before_previews


def test_reading_a_run_does_not_contact_the_host(drifted: Estate):
    """Not the kernel, not sysfs, not a provider. Everything comes out of the store."""
    outcome = drifted.plan("eth0")
    commands_before = list(drifted.runner.calls)
    tree_before = drifted.link_snapshot()
    sweeps_before = len(drifted.database.query("SELECT * FROM observation_sweeps"))

    run = drifted.runs.get(outcome.run.run_id)
    drifted.validity(run)
    drifted.runs.list_for_host(drifted.host_id)

    assert drifted.runner.calls == commands_before
    assert drifted.link_snapshot() == tree_before
    assert len(drifted.database.query("SELECT * FROM observation_sweeps")) == sweeps_before


# ---------------------------------------------------------------------------- durability


def test_a_plan_that_fails_part_way_leaves_no_run_and_no_preview(drifted: Estate, monkeypatch):
    """The preview and the Run land together or not at all."""

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("interrupted after the preview was written")

    monkeypatch.setattr(drifted.runs.runs, "insert_run", explode)
    with pytest.raises(RuntimeError):
        drifted.plan("eth0")

    assert drifted.database.query("SELECT * FROM runs") == []
    assert drifted.database.query("SELECT * FROM run_previews") == []


def test_two_previews_for_the_same_object_do_not_overwrite_each_other(drifted: Estate):
    """Two truthful snapshots of one plan. Equal content, separate records."""
    first = drifted.plan("eth0")
    second = drifted.plan("eth0")
    assert first.run.run_id != second.run.run_id
    assert first.run.preview.preview_id != second.run.preview.preview_id
    assert first.run.preview.preview_digest == second.run.preview.preview_digest
    assert len(drifted.database.query("SELECT * FROM runs")) == 2
    assert len(drifted.database.query("SELECT * FROM run_previews")) == 2

    # Cancelling one leaves the other exactly as it was.
    drifted.runs.cancel(first.run)
    assert drifted.runs.get(second.run.run_id).state == str(RunState.PREVIEW)


def test_runs_for_different_objects_stay_apart(drifted: Estate):
    drifted.adopt("eth1")
    drifted.write("eth1", "mtu", "9000")
    drifted.observe()
    first = drifted.plan("eth0")
    second = drifted.plan("eth1")
    assert first.run.preview.preview_digest != second.run.preview.preview_digest

    listed = drifted.runs.list_for_host(drifted.host_id, object_id=second.run.object_id)
    assert [r.run_id for r in listed] == [second.run.run_id]


def test_a_run_survives_a_restart(drifted: Estate, tmp_path: Path):
    outcome = drifted.plan("eth0")
    digest = outcome.run.preview.preview_digest
    drifted.database.close()

    from localplane.backend.operations import OPERATIONS

    reopened = open_database(drifted.database.path)
    try:
        service = RunService(reopened, 60.0, OPERATIONS)
        run = service.get(outcome.run.run_id)
        assert run is not None
        assert run.state == str(RunState.PREVIEW)
        assert run.preview.preview_digest == digest
        assert plan_digest(service.published_plan(run)) == digest
    finally:
        reopened.close()


# ---------------------------------------------------------------------------- migrations


def test_a_store_written_before_0005_upgrades_and_keeps_what_it_held(tmp_path: Path):
    """A database created by 0001–0004 opens, and nothing it held is disturbed."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    earlier = (
        "0001_initial.sql",
        "0002_management.sql",
        "0003_provenance.sql",
        "0004_intent_revision.sql",
    )
    for name in earlier:
        shutil.copy(MIGRATIONS_DIR / name, migrations / name)

    old = open_database(tmp_path / "upgrade.db", migrations)
    old.connection.execute("BEGIN IMMEDIATE")
    old.connection.execute(
        "INSERT INTO hosts VALUES ('h','machine_id','high',NULL,NULL,NULL,NULL,NULL,NULL,"
        "NULL,NULL,NULL,'[]','t','t')"
    )
    old.connection.execute(
        "INSERT INTO objects (object_id, host_id, kind, identity_basis, identity_value, "
        "identity_confidence, display_name, management_state, management_reason, "
        "first_seen_at, last_seen_at, active_intent_id) VALUES ('o','h','network.interface',"
        "'kernel_name','eth0','low','eth0','observed','management_candidate','t','t',NULL)"
    )
    old.connection.execute(
        "INSERT INTO observation_sweeps VALUES ('s','h',NULL,'network.observe',"
        "'linux.network','1','ok','t','t','t',1,'[]','[]')"
    )
    old.connection.execute(
        "INSERT INTO observations VALUES ('ob','s','h','o','network.observe','linux.network',"
        "'1','sysfs','complete','t','t','healthy','ok','[]','{\"mtu\":1400}','{}')"
    )
    old.connection.execute(
        "INSERT INTO intents VALUES ('i1','o','h',1,NULL,1,'adopt','network.observe',"
        "'linux.network','1','ob','s','t','t')"
    )
    old.connection.execute("INSERT INTO intent_fields VALUES ('i1','mtu','integer',1500)")
    old.connection.execute(
        "UPDATE objects SET management_state='managed', active_intent_id='i1' WHERE object_id='o'"
    )
    old.connection.execute(
        "INSERT INTO management_transitions VALUES "
        "('t1','o','h','adopt','observed','managed','i1','ob','none','t')"
    )
    old.connection.execute("COMMIT")
    assert [
        r["version"] for r in old.query("SELECT version FROM schema_migrations")
    ] == [1, 2, 3, 4]
    old.close()

    shutil.copy(MIGRATIONS_DIR / "0005_change_engine.sql", migrations / "0005_change_engine.sql")
    upgraded = open_database(tmp_path / "upgrade.db", migrations)
    try:
        assert [
            r["version"] for r in upgraded.query("SELECT version FROM schema_migrations")
        ] == [1, 2, 3, 4, 5]
        assert upgraded.query_one("SELECT active_intent_id FROM objects")[0] == "i1"
        assert len(upgraded.query("SELECT * FROM intents")) == 1
        assert len(upgraded.query("SELECT * FROM management_transitions")) == 1
        # The two new tables exist and are empty: an upgrade invents no history.
        assert upgraded.query("SELECT * FROM runs") == []
        assert upgraded.query("SELECT * FROM run_previews") == []
        assert upgraded.query("PRAGMA foreign_key_check") == []
        assert upgraded.query("PRAGMA integrity_check")[0][0] == "ok"
    finally:
        upgraded.close()


def test_the_earlier_migrations_are_the_ones_older_stores_were_built_with(tmp_path: Path):
    """0005 adds. It does not edit history, and this is what would catch it if it had."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    earlier = (
        "0001_initial.sql",
        "0002_management.sql",
        "0003_provenance.sql",
        "0004_intent_revision.sql",
    )
    for name in earlier:
        shutil.copy(MIGRATIONS_DIR / name, migrations / name)

    old = open_database(tmp_path / "staged.db", migrations)
    staged = {r["version"]: r["checksum"] for r in old.query("SELECT * FROM schema_migrations")}
    old.close()

    shutil.copy(MIGRATIONS_DIR / "0005_change_engine.sql", migrations / "0005_change_engine.sql")
    upgraded = open_database(tmp_path / "staged.db", migrations)
    # The reference store is built from the same five, so this stays a statement about 0005
    # as later migrations are added rather than one that has to be revisited each time.
    fresh = open_database(tmp_path / "fresh.db", migrations)
    try:
        upgraded_sums = {
            r["version"]: r["checksum"] for r in upgraded.query("SELECT * FROM schema_migrations")
        }
        fresh_sums = {
            r["version"]: r["checksum"] for r in fresh.query("SELECT * FROM schema_migrations")
        }
        assert {v: staged[v] for v in (1, 2, 3, 4)} == {
            v: upgraded_sums[v] for v in (1, 2, 3, 4)
        }
        assert upgraded_sums == fresh_sums
        assert sorted(fresh_sums) == [1, 2, 3, 4, 5]
    finally:
        upgraded.close()
        fresh.close()


def test_the_fifth_migration_is_deterministic(tmp_path: Path):
    """Applied from scratch twice, the same schema comes out."""

    def schema_of(name: str) -> list[str]:
        db = open_database(tmp_path / name)
        try:
            return sorted(
                r["sql"] for r in db.query("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")
            )
        finally:
            db.close()

    assert schema_of("one.db") == schema_of("two.db")


# --------------------------------------------------------------------------- the agent


def test_the_agent_names_everything_it_has_that_can_change_anything(drifted: Estate):
    """Four mutating methods, four mutating capabilities, named rather than counted.

    On *this* host none is reachable — there is no privileged helper, the Docker socket in
    this fixture is not a daemon and there is no system bus — so the agent reports methods it
    has and capabilities it cannot currently use, which are two different facts and are
    reported as two.

    ``network.arm_mtu_guard`` is among the methods that can change a host because of what it
    can *cause*: arming a connection guard writes nothing and dispatches a kernel write with
    no further request if a deadline passes.
    """
    hello = drifted.service.handle("agent.hello", {})
    assert hello["agent"]["mutating_methods"] == [
        "docker.container_lifecycle",
        "network.arm_mtu_guard",
        "network.set_interface_mtu",
        "systemd.service_lifecycle",
    ]
    listed = drifted.service.handle("capabilities.list", {})["capabilities"]
    assert listed
    assert {c["capability"] for c in listed if c["mutating"]} == {
        CAPABILITY_SET_MTU,
        "network.interface.mtu_guard",
        "docker.container.lifecycle",
        "systemd.service.lifecycle",
    }
    assert {c["capability"] for c in listed} == {
        "host.observe",
        "network.observe",
        "network.providers.observe",
        "network.route.observe",
        CAPABILITY_SET_MTU,
        "network.interface.mtu_guard",
        "docker.containers.observe",
        "docker.container.lifecycle",
        "systemd.units.observe",
        "systemd.service.lifecycle",
        "systemd.service.lifecycle_context.observe",
    }
    by_name = {c["capability"]: c for c in listed}
    assert by_name[CAPABILITY_SET_MTU]["status"] == "unavailable"
    assert set(hello["agent"]["methods"]) == {
        "agent.hello",
        "capabilities.list",
        "host.identify",
        "network.observe_interfaces",
        "network.observe_providers",
        "network.observe_route",
        "network.set_interface_mtu",
        "docker.observe_containers",
        "docker.container_logs",
        "docker.container_stats",
        "docker.container_lifecycle",
        "systemd.observe_units",
        "systemd.observe_unit",
        "systemd.resolve_agent_unit",
        "systemd.observe_lifecycle_context",
        "systemd.service_lifecycle",
        # The connection guard's three, of which only the first can cause a write: the
        # other two release one and report on one, and a release can only *prevent* the
        # reversal it was holding.
        "network.arm_mtu_guard",
        "network.disarm_mtu_guard",
        "network.mtu_guard_status",
    }
    # The agent still holds no privilege itself. The write lives behind a socket.
    assert hello["agent"]["privilege"] == ("root" if os.geteuid() == 0 else "unprivileged")


def test_the_whole_run_surface_leaves_the_fixture_host_byte_identical(drifted: Estate):
    before = drifted.link_snapshot()
    first = drifted.plan("eth0")
    drifted.runs.get(first.run.run_id)
    drifted.validity(first.run)
    drifted.runs.list_for_host(drifted.host_id)
    second = drifted.plan("eth0")
    drifted.runs.cancel(second.run)
    drifted.runs.cancel(first.run)
    assert drifted.link_snapshot() == before


# =========================================================================== protection
#
# The management path arrived, and with it the first thing that can make a plan *more*
# dangerous rather than only less certain. What is asserted here is that the three states
# stay three states — proven target, proven non-target, unresolved — and that none of them
# quietly becomes another.


def test_a_target_proven_to_carry_the_management_path_is_protected(drifted: Estate):
    drifted.confirm_path("eth0")
    protection = drifted.plan("eth0").plan.protection

    assert protection.status is ProtectionStatus.PROTECTED
    assert protection.reasons == (ProtectionReason.MANAGEMENT_PATH,)
    assert protection.unresolved == ()
    assert protection.management_path is ManagementPathRelation.ON_MANAGEMENT_PATH
    assert protection.reason == "management_path_confirmed"
    assert protection.evidence_id == drifted.management_path.evidence_id
    assert protection.evidence_id.startswith("mpo_")
    assert protection.missing_evidence == ()


def test_a_protected_target_is_high_risk_and_says_which_fact_made_it_so(drifted: Estate):
    """A change to the port an operator is reached over is rated `high`, and so does this."""
    drifted.confirm_path("eth0")
    plan = drifted.plan("eth0").plan

    assert plan.risk.tier is RiskTier.HIGH
    factors = {f.code: f for f in plan.risk.factors}
    assert factors["target_is_management_path"].floor is RiskTier.HIGH
    assert "management_path_unproven" not in factors


def test_a_protected_target_requires_a_typed_confirmation_that_is_still_unsatisfiable(
    drifted: Estate,
):
    drifted.confirm_path("eth0")
    plan = drifted.plan("eth0").plan
    confirmation = plan.confirmation
    assert confirmation.required is True
    assert confirmation.method is ConfirmationMethod.TYPED
    assert confirmation.source == "policy"
    # Nothing is issued, here or anywhere.
    assert confirmation.token_issued is False
    # And nothing about being able to name the danger makes it possible to proceed: this
    # target is *proven* to carry the operator's path, so executing it is blocked outright
    # and no confirmation of any method would unblock it.
    assert "target_is_management_path" in plan.execution.blockers
    assert plan.execution.eligibility is ExecutionEligibility.BLOCKED


def test_a_protected_target_is_blocked_by_being_the_management_path_not_by_being_unproven(
    drifted: Estate,
):
    """The distinction matters most here. "Unproven" would be a plain falsehood."""
    drifted.confirm_path("eth0")
    blockers = drifted.plan("eth0").plan.execution.blockers
    assert "target_is_management_path" in blockers
    assert "management_path_unproven" not in blockers
    # It is blocked by *what it is*, not by the feature being missing. Guarded mutation of
    # the operator's own path is the capability this build does not have, and the blocker
    # names the target rather than the gap.
    assert "execution_not_implemented" not in blockers


def test_a_proven_non_target_loses_the_unproven_blocker_and_keeps_the_real_ones(
    drifted: Estate,
):
    drifted.adopt("eth1")
    drifted.write("eth1", "mtu", "1400")
    drifted.observe()
    drifted.confirm_path("eth1")

    plan = drifted.plan("eth0").plan
    assert plan.protection.status is ProtectionStatus.CLEAR
    assert plan.protection.management_path is ManagementPathRelation.NOT_ON_MANAGEMENT_PATH
    assert plan.protection.reason == "not_the_management_path"
    assert "management_path_unproven" not in plan.execution.blockers
    assert "target_is_management_path" not in plan.execution.blockers
    # Everything genuinely in the way is still in the way — and on this fixture host that
    # is the missing privileged helper and nothing else.
    assert plan.execution.eligibility is ExecutionEligibility.BLOCKED
    assert plan.execution.blockers == ("required_capability_undeclared",)
    # And the risk falls back to the operation's own tier, not below it.
    assert plan.risk.tier is RiskTier.MEDIUM
    assert plan.confirmation.method is ConfirmationMethod.ACKNOWLEDGE


def test_an_unresolved_path_keeps_the_blocker_and_never_lowers_the_risk(drifted: Estate):
    plan = drifted.plan("eth0").plan
    assert plan.protection.status is ProtectionStatus.UNKNOWN
    assert "management_path_unproven" in plan.execution.blockers
    assert plan.risk.tier is not RiskTier.LOW
    assert plan.confirmation.method is ConfirmationMethod.TYPED


def test_an_unresolved_path_marks_no_object_clear(drifted: Estate):
    """Not the target, not a bystander: while the path is unknown, everything is unknown."""
    drifted.adopt("eth1")
    drifted.write("eth1", "mtu", "1400")
    drifted.observe()
    for name in ("eth0", "eth1"):
        protection = drifted.plan(name).plan.protection
        assert protection.status is ProtectionStatus.UNKNOWN
        assert protection.management_path is ManagementPathRelation.UNKNOWN


def test_protection_is_not_ownership_and_the_two_block_separately(drifted: Estate):
    """A Docker-configured object can be `clear` and still be refused. Both are true."""
    drifted.start_docker(
        docker_network(MONITORING_NETWORK, "monitoring", gateway=DOCKER0_GATEWAY)
    )
    drifted.adopt("br0")
    drifted.write("br0", "mtu", "1400")
    drifted.observe()
    drifted.confirm_path("eth0")

    plan = drifted.plan("br0").plan
    assert plan.protection.status is ProtectionStatus.CLEAR
    assert plan.protection.reasons == ()
    assert plan.ownership.state != "unattributed"
    assert "externally_configured" in plan.execution.blockers
    assert plan.execution.eligibility is ExecutionEligibility.BLOCKED
    # And the protection reason is not an ownership one wearing a different name.
    assert "externally_configured" not in {str(r) for r in plan.protection.reasons}


def test_the_published_preview_freezes_the_protection_it_showed(drifted: Estate):
    drifted.confirm_path("eth0")
    outcome = drifted.plan("eth0")
    preview = outcome.run.preview

    assert preview.protection_status == "protected"
    assert preview.protection_reasons == ["management_path"]
    assert preview.protection_unresolved == []
    assert preview.protection_management_path == "on_management_path"
    assert preview.protection_evidence_id == drifted.management_path.evidence_id
    assert preview.protection_evidence_observed_at == _NOW

    # And it is rebuilt from the row, not re-derived: the path is now unknown and the
    # published document still says what it said.
    drifted.management_path = UNOBSERVED
    published = drifted.runs.published_plan(drifted.runs.get(outcome.run.run_id))
    assert published.protection.status is ProtectionStatus.PROTECTED
    assert published.protection.evidence_id == preview.protection_evidence_id


def test_a_plan_still_creates_no_change_when_its_target_is_the_management_path(
    drifted: Estate,
):
    """The most dangerous plan this build can describe, and it is still only a description."""
    drifted.confirm_path("eth0")
    before = drifted.link_snapshot()
    outcome = drifted.plan("eth0")

    assert outcome.run.host_effect == "none"
    assert "target_is_management_path" in outcome.plan.execution.blockers
    assert outcome.plan.execution.eligibility is ExecutionEligibility.BLOCKED
    assert outcome.plan.recovery.armed is False
    assert outcome.plan.verification.executed is False
    assert drifted.link_snapshot() == before
    # The table exists now. Nothing put a row in it, and nothing about this plan could.
    assert drifted.database.query("SELECT * FROM changes") == []
    assert drifted.database.query("SELECT * FROM run_checkpoints") == []


# --------------------------------------------------------------------- digest and version


def test_a_new_preview_is_published_under_the_current_digest_version(drifted: Estate):
    """Version 6 binds the backend self-impact derivation and the containment it rests on.

    Those sections are null for an MTU plan — it has no backend runtime to be assessed
    against — but they are still part of the current canonical form; versions 1–5 remain
    historical forms and are never silently reinterpreted.
    """
    assert PLAN_DIGEST_VERSION == 6
    assert drifted.plan("eth0").run.preview.digest_version == 6


def test_protection_meaning_changes_the_digest(drifted: Estate):
    """Two plans an operator must decide differently about may not share an identity."""
    unknown = drifted.plan("eth0").plan
    drifted.confirm_path("eth0")
    protected = drifted.plan("eth0").plan
    drifted.confirm_path("eth1")
    cleared = drifted.plan("eth0").plan

    digests = {plan_digest(p) for p in (unknown, protected, cleared)}
    assert len(digests) == 3
    assert canonical_plan(protected)["protection"]["status"] == "protected"
    assert canonical_plan(cleared)["protection"]["status"] == "clear"
    assert canonical_plan(unknown)["protection"]["status"] == "unknown"


def test_the_same_meaning_from_a_newer_observation_keeps_the_same_digest(drifted: Estate):
    """A fresh proof of the same path has confirmed the plan, not changed it."""
    drifted.confirm_path("eth0", observed_at="2026-08-22T12:00:00.000000+00:00")
    first = drifted.plan("eth0").plan
    drifted.confirm_path("eth0", observed_at="2026-08-22T12:00:30.000000+00:00")
    second = drifted.plan("eth0").plan

    assert plan_digest(first) == plan_digest(second)
    assert first.protection.evidence_id != second.protection.evidence_id
    assert first.protection.evidence_observed_at != second.protection.evidence_observed_at


def test_the_evidence_is_bound_to_the_preview_even_though_it_is_not_hashed(drifted: Estate):
    evidence_id = drifted.confirm_path("eth0").evidence_id
    outcome = drifted.plan("eth0")
    assert outcome.run.preview.protection_evidence_id == evidence_id
    assert evidence_id not in str(canonical_plan(outcome.plan))
    # And the foreign key means a preview cannot name evidence that was never taken.
    assert drifted.database.query("PRAGMA foreign_key_check") == []


def test_every_earlier_canonical_form_is_still_renderable_and_still_verifies(drifted: Estate):
    """History stays checkable under the rules it was made with.

    Version 1 carried the management-path relation and nothing else could be carried.
    Version 2 added the protection roll-up. Version 3 adds the connection guard. A preview
    published under any of them verifies against its own digest, and none of them shares an
    identity with another.
    """
    plan = drifted.plan("eth0").plan
    v1 = canonical_plan(plan, 1)
    v2 = canonical_plan(plan, 2)
    v3 = canonical_plan(plan, 3)
    v4 = canonical_plan(plan, 4)
    v5 = canonical_plan(plan, 5)
    assert set(v1["protection"]) == {"management_path", "reason", "missing_evidence"}
    assert set(v2["protection"]) == {
        "management_path", "reason", "missing_evidence", "status", "reasons", "unresolved",
    }
    assert "guard" not in v1 and "guard" not in v2
    assert set(v3["guard"]) == {
        "availability", "reason", "window_s", "prerequisites", "unmet",
    }
    assert v1["digest_version"] == 1 and v2["digest_version"] == 2
    assert v3["digest_version"] == 3
    assert v4["digest_version"] == 4
    assert v4["authorization"] is None and v4["lifecycle_context"] is None
    assert v5["authorization"] is None and v5["lifecycle_context"] is None
    assert "assessed" in v4["protection"]
    assert len({plan_digest(plan, v) for v in (1, 2, 3, 4, 5)}) == 5


# -------------------------------------------------------- staleness, from a moved path


def test_a_target_becoming_the_management_path_makes_its_preview_stale(drifted: Estate):
    """The important direction: what was safe to plan is now the thing you are reached over."""
    drifted.confirm_path("eth1")
    outcome = drifted.plan("eth0")
    assert outcome.validity.state is PlanValidityState.CURRENT

    drifted.confirm_path("eth0")
    validity = drifted.validity(drifted.runs.get(outcome.run.run_id))
    assert validity.state is PlanValidityState.STALE
    codes = {r.code for r in validity.reasons}
    assert "protection_changed" in codes
    assert "risk_changed" in codes
    assert "execution_changed" in codes


def test_a_target_ceasing_to_be_the_management_path_makes_its_preview_stale(drifted: Estate):
    drifted.confirm_path("eth0")
    outcome = drifted.plan("eth0")
    assert outcome.validity.state is PlanValidityState.CURRENT

    drifted.confirm_path("eth1")
    validity = drifted.validity(drifted.runs.get(outcome.run.run_id))
    assert validity.state is PlanValidityState.STALE
    reasons = {r.code: r for r in validity.reasons}
    assert reasons["protection_changed"].detail["was"]["status"] == "protected"
    assert reasons["protection_changed"].detail["now"]["status"] == "clear"


def test_a_confirmed_path_going_unknown_makes_its_preview_stale(drifted: Estate):
    """Losing the evidence is a change of meaning, and it must not read as unchanged."""
    drifted.confirm_path("eth1")
    outcome = drifted.plan("eth0")
    assert outcome.validity.state is PlanValidityState.CURRENT

    drifted.management_path = UNOBSERVED
    validity = drifted.validity(drifted.runs.get(outcome.run.run_id))
    assert validity.state is PlanValidityState.STALE
    reasons = {r.code: r for r in validity.reasons}
    assert reasons["protection_changed"].detail["was"]["status"] == "clear"
    assert reasons["protection_changed"].detail["now"]["status"] == "unknown"


def test_a_newer_proof_of_the_same_path_leaves_the_preview_current(drifted: Estate):
    """Staleness fires on meaning, not on a reading having been taken again."""
    drifted.confirm_path("eth0", observed_at="2026-08-22T12:00:00.000000+00:00")
    outcome = drifted.plan("eth0")
    later = drifted.confirm_path("eth0", observed_at="2026-08-22T12:00:45.000000+00:00")
    assert later.evidence_id != outcome.plan.protection.evidence_id
    assert drifted.validity(drifted.runs.get(outcome.run.run_id)).state is (
        PlanValidityState.CURRENT
    )


# ------------------------------------------------------------------- previews from before


def test_a_preview_published_before_protection_existed_is_still_readable(drifted: Estate):
    run = drifted.publish_legacy_preview(drifted.plan("eth0").plan)
    published = drifted.runs.published_plan(run)

    assert run.preview.digest_version == 1
    assert published.protection.status is ProtectionStatus.UNKNOWN
    assert published.protection.management_path is ManagementPathRelation.UNKNOWN
    assert published.protection.unresolved == (ProtectionReason.MANAGEMENT_PATH,)
    assert published.protection.reasons == ()
    assert published.protection.reason == "management_path_evidence_unavailable"
    assert published.protection.evidence_id is None


def test_an_older_preview_still_verifies_against_its_own_digest(drifted: Estate):
    """Recomputable from the row alone, under the rules it was published with."""
    run = drifted.publish_legacy_preview(drifted.plan("eth0").plan)
    published = drifted.runs.published_plan(run)
    assert plan_digest(published, run.preview.digest_version) == run.preview.preview_digest
    assert plan_digest(published, 2) != run.preview.preview_digest


def test_an_older_preview_is_not_declared_stale_by_the_canonical_form_moving(
    drifted: Estate,
):
    """Both sides are canonicalised under the version it was published with.

    A plan published under version 1 that still means what it meant comes out ``current``,
    even though this build hashes plans differently. If the comparison were made under the
    newer form, every older preview in the store would go stale at once — which is a
    staleness signal nobody would read.
    """
    run = drifted.publish_legacy_preview(
        drifted.plan("eth0").plan,
        reason="management_path_unobserved",
        missing=("session.peer", "route.observe"),
    )
    assert run.preview.digest_version == 1
    assert drifted.validity(run).state is PlanValidityState.CURRENT


def test_an_older_preview_whose_reason_moved_is_stale_by_section_not_by_hashing(
    drifted: Estate,
):
    """The difference is reported as what it is: the protection section, under version 1."""
    run = drifted.publish_legacy_preview(drifted.plan("eth0").plan)
    validity = drifted.validity(run)
    codes = {r.code for r in validity.reasons}
    assert validity.state is PlanValidityState.STALE
    assert codes == {"protection_changed"}
    assert "plan_digest_changed" not in codes


def test_an_older_preview_still_goes_stale_when_the_path_it_never_knew_moves(
    drifted: Estate,
):
    """Version 1 carried the relation, so a relation that moves still shows up in it."""
    run = drifted.publish_legacy_preview(drifted.plan("eth0").plan)
    drifted.confirm_path("eth0")
    validity = drifted.validity(run)
    assert validity.state is PlanValidityState.STALE
    assert "protection_changed" in {r.code for r in validity.reasons}


def test_an_older_previews_content_is_never_rewritten(drifted: Estate):
    run = drifted.publish_legacy_preview(drifted.plan("eth0").plan)
    before = [
        tuple(r)
        for r in drifted.database.query(
            "SELECT * FROM run_previews WHERE preview_id = ?", (run.preview.preview_id,)
        )
    ]
    drifted.confirm_path("eth0")
    drifted.validity(run)
    drifted.runs.published_plan(run)
    after = [
        tuple(r)
        for r in drifted.database.query(
            "SELECT * FROM run_previews WHERE preview_id = ?", (run.preview.preview_id,)
        )
    ]
    assert after == before


# ----------------------------------------------------------------------- migration 0006


def test_the_sixth_migration_upgrades_a_store_that_already_holds_runs(tmp_path: Path):
    """The rebuild, against a store with a published plan and a Run naming it."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    earlier = (
        "0001_initial.sql",
        "0002_management.sql",
        "0003_provenance.sql",
        "0004_intent_revision.sql",
        "0005_change_engine.sql",
    )
    for name in earlier:
        shutil.copy(MIGRATIONS_DIR / name, migrations / name)

    old = open_database(tmp_path / "staged.db", migrations)
    _seed_a_published_plan(old)
    staged = {r["version"]: r["checksum"] for r in old.query("SELECT * FROM schema_migrations")}
    old.close()

    shutil.copy(MIGRATIONS_DIR / "0006_management_path.sql", migrations / "0006_management_path.sql")
    upgraded = open_database(tmp_path / "staged.db", migrations)
    try:
        sums = {r["version"]: r["checksum"] for r in upgraded.query("SELECT * FROM schema_migrations")}
        assert sorted(sums) == [1, 2, 3, 4, 5, 6]
        assert {v: staged[v] for v in (1, 2, 3, 4, 5)} == {v: sums[v] for v in (1, 2, 3, 4, 5)}

        # The plan survived the rebuild, and its new columns restate what it already said.
        preview = dict(upgraded.query("SELECT * FROM run_previews")[0])
        assert preview["preview_id"] == "prv_legacy"
        assert preview["digest_version"] == 1
        assert preview["preview_digest"] == "sha256:legacy"
        assert preview["current_value"] == 1400 and preview["desired_value"] == 1500
        assert preview["protection_management_path"] == "unknown"
        assert preview["protection_status"] == "unknown"
        assert preview["protection_reasons"] == "[]"
        assert preview["protection_unresolved"] == '["management_path"]'
        assert preview["protection_evidence_id"] is None

        assert [tuple(r) for r in upgraded.query("SELECT run_id, preview_id, state FROM runs")] == [
            ("run_legacy", "prv_legacy", "preview")
        ]
        assert upgraded.query("PRAGMA foreign_key_check") == []
        assert upgraded.query("PRAGMA integrity_check")[0][0] == "ok"
        # The new table exists and is empty: an upgrade invents no evidence.
        assert upgraded.query("SELECT * FROM management_path_observations") == []
    finally:
        upgraded.close()


def test_the_rebuilds_keep_the_guarantees_0005_established_that_are_still_true(
    tmp_path: Path,
):
    """Three of 0005's seven CHECKs survive two rebuilds, and they are the right three.

    ``execution_availability``, ``execution_eligibility`` and ``execution_provider`` widened
    in 0007 because execution exists and the old values had become false statements about
    the build. The other three did not, because they are still true of a *preview*: it is
    published before anything is armed and before anything is verified, and this build still
    issues no confirmation token of any kind. Arming and verifying now happen — on the
    Change, in their own tables — and keeping these CHECKs is what stops the immutable
    document acquiring claims that belong to the execution of it.
    """
    database = open_database(tmp_path / "rebuilt.db")
    try:
        _seed_a_published_plan(database)
        for column, value in (
            ("recovery_armed", 1),
            ("guard_armed", 1),
            ("verification_executed", 1),
            ("confirmation_token_issued", 1),
            # Still closed vocabularies, even though two of them widened.
            ("execution_availability", "root"),
            ("execution_eligibility", "maybe"),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                database.connection.execute(
                    f"UPDATE run_previews SET {column} = ? WHERE preview_id = 'prv_legacy'",
                    (value,),
                )
        # And the immutability trigger, which is what actually refused each of those.
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            database.connection.execute(
                "UPDATE run_previews SET risk_tier = 'low' WHERE preview_id = 'prv_legacy'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            database.connection.execute(
                "UPDATE runs SET object_id = 'obj_other' WHERE run_id = 'run_legacy'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            database.connection.execute("DELETE FROM run_previews WHERE preview_id = 'prv_legacy'")
    finally:
        database.close()


def test_the_run_state_check_widened_only_where_a_mechanism_arrived_to_justify_it(
    tmp_path: Path,
):
    """0006 said widening this was the write boundary's migration. 0007 was that migration,
    and 0010 widened it once more — by exactly one state, and only because the mechanism
    that state describes now exists.

    What neither widening did is loosen the pairing between a state and what it may claim:
    ``host_effect`` gained two values in 0007 and immediately gained CHECKs tying them to
    the states that can honestly carry them, and ``guarded`` arrived in 0010 with its own.
    That is what keeps ``failed`` from ever meaning "something may have been written".
    """
    database = open_database(tmp_path / "states.db")
    try:
        sql = " ".join(
            database.query_one(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='runs'"
            )["sql"].split()
        )
        assert "'preview', 'awaiting_confirmation', 'arming', 'applying', 'verifying'" in sql
        assert "'draft'" not in sql
        assert "host_effect IN ('none', 'written', 'write_unknown')" in sql
        assert "CHECK (state <> 'failed' OR host_effect = 'none')" in sql
        assert "CHECK (state <> 'succeeded' OR host_effect = 'written')" in sql
        # 0010 added one state and paired it in the same breath: `guarded` is only reached
        # after a mutation that was acknowledged *and* proved, so a Run claiming to be
        # guarded while claiming nothing was written is unstorable.
        assert "CHECK (state <> 'guarded' OR host_effect = 'written')" in sql
    finally:
        database.close()


def test_the_management_path_migration_did_not_widen_the_run_state_check(tmp_path: Path):
    """0006's own promise, still checkable now that a later migration has kept it.

    Built at 0006 rather than read from the current schema, because "the migration that
    should widen this is the next one" is a claim about *that* migration and stays one.
    """
    import shutil as _shutil

    migrations = tmp_path / "at_0006"
    migrations.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(path.stem.split("_")[0]) <= 6:
            _shutil.copy(path, migrations / path.name)
    database = open_database(tmp_path / "at_0006.db", migrations)
    try:
        sql = database.query_one(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='runs'"
        )["sql"]
        assert "state IN ('preview', 'cancelled')" in sql
        assert "host_effect = 'none'" in sql
    finally:
        database.close()


def test_the_sixth_migration_is_deterministic(tmp_path: Path):
    def schema_of(name: str) -> list[str]:
        db = open_database(tmp_path / name)
        try:
            return sorted(
                " ".join(r["sql"].split())
                for r in db.query("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")
            )
        finally:
            db.close()

    assert schema_of("one.db") == schema_of("two.db")


def test_a_rebuilt_store_and_a_fresh_one_have_the_same_schema(tmp_path: Path):
    """A store that came through the rebuild is not a second kind of store."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for name in (
        "0001_initial.sql", "0002_management.sql", "0003_provenance.sql",
        "0004_intent_revision.sql", "0005_change_engine.sql",
    ):
        shutil.copy(MIGRATIONS_DIR / name, migrations / name)
    open_database(tmp_path / "staged.db", migrations).close()
    shutil.copy(MIGRATIONS_DIR / "0006_management_path.sql", migrations / "0006_management_path.sql")

    upgraded = open_database(tmp_path / "staged.db", migrations)
    fresh = open_database(tmp_path / "fresh.db", migrations)
    try:
        def schema(db) -> list[str]:
            return sorted(
                " ".join(r["sql"].split())
                for r in db.query("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")
            )

        assert schema(upgraded) == schema(fresh)
    finally:
        upgraded.close()
        fresh.close()


def _seed_a_published_plan(database) -> None:
    """One host, one object, one intent, one observation, one preview and one Run."""
    connection = database.connection
    connection.execute(
        "INSERT INTO hosts VALUES ('h','machine_id','high',NULL,NULL,NULL,NULL,NULL,NULL,"
        "NULL,NULL,NULL,'[]','t','t')"
    )
    connection.execute(
        "INSERT INTO objects (object_id, host_id, kind, identity_basis, identity_value, "
        "identity_confidence, display_name, management_state, management_reason, "
        "first_seen_at, last_seen_at) VALUES "
        "('o','h','network.interface','kernel_name','eth0','low','eth0','observed','x','t','t')"
    )
    # Name the original columns so the fixture works both before and after the generic
    # inventory/targeted scope was added in 0011. On a current schema the default proves
    # that every historical sweep upgrades as an inventory sweep.
    connection.execute(
        "INSERT INTO observation_sweeps (sweep_id, host_id, agent_instance_id, capability, "
        "provider, provider_version, status, started_at, completed_at, received_at, "
        "object_count, missing, issues) VALUES "
        "('s','h',NULL,'network.observe','linux.network','1','ok','t','t','t',1,'[]','[]')"
    )
    connection.execute(
        "INSERT INTO observations VALUES ('ob','s','h','o','network.observe','linux.network',"
        "'1','m','complete','t','t','healthy','ok','[]','{}','{}')"
    )
    connection.execute(
        "INSERT INTO intents (intent_id, object_id, host_id, version, supersedes, "
        "schema_version, origin, capability, provider, provider_version, observation_id, "
        "sweep_id, observed_at, created_at) VALUES "
        "('i','o','h',1,NULL,1,'adopt','network.observe','linux.network','1','ob','s','t','t')"
    )
    connection.execute(
        "UPDATE objects SET management_state='managed', active_intent_id='i', "
        "management_reason='adopted'"
    )
    columns = [r["name"] for r in database.query("PRAGMA table_info(run_previews)")]
    values: dict[str, Any] = {
        "preview_id": "prv_legacy", "preview_digest": "sha256:legacy", "digest_version": 1,
        "operation": "network.interface.reconcile_mtu",
        "field": "mtu", "value_type": "integer", "current_value": 1400, "desired_value": 1500,
        "intent_id": "i", "intent_version": 1, "intent_capability": "network.observe",
        "intent_provider": "linux.network",
        "change_kind": "field", "action": None,
        "observed_state": None, "expected_state": None,
        "observation_id": "ob", "sweep_id": "s", "observed_at": "t", "drift_finding_id": None,
        "ownership_state": "unattributed", "ownership_reason": "no_provider_claim",
        "ownership_claims": "[]", "ownership_gaps": "[]", "provider_readings": "{}",
        "protection_management_path": "unknown",
        "protection_reason": "management_path_evidence_unavailable",
        "protection_missing_evidence": '["route.observe","session.peer"]',
        "protection_status": "unknown", "protection_reasons": "[]",
        "protection_unresolved": '["management_path"]',
        "protection_evidence_id": None, "protection_evidence_observed_at": None,
        "protection_assessments": "[]",
        "authorization_assessment": None, "lifecycle_context": None, "self_impact": None,
        "risk_tier": "medium", "risk_factors": "[]",
        "confirmation_required": 1, "confirmation_method": "typed",
        "confirmation_source": "policy", "confirmation_reasons": "[]",
        "confirmation_policy": "pol", "confirmation_token_issued": 0,
        "execution_availability": "not_implemented", "execution_eligibility": "blocked",
        "execution_blockers": "[]", "execution_provider": None,
        "required_capability": "network.interface.set_mtu", "capability_declared": 0,
        "guard_availability": "unavailable", "guard_reason": "guard_not_required",
        "guard_window_s": 0, "guard_prerequisites": "[]", "guard_unmet": "[]",
        "guard_guarantee": "", "guard_armed": 0,
        "recovery_mode": "auto", "recovery_rollback_possible": 1, "recovery_armed": 0,
        "recovery_guarantee": "none", "recovery_reason": "r",
        "verification_capability": "network.observe", "verification_provider": "linux.network",
        "verification_condition": "c", "verification_executed": 0,
        "published_at": "t",
    }
    present = [c for c in columns if c in values]
    assert set(present) == set(columns), set(columns) ^ set(values)
    connection.execute(
        f"INSERT INTO run_previews ({', '.join(present)}) "
        f"VALUES ({', '.join('?' for _ in present)})",
        tuple(values[c] for c in present),
    )
    connection.execute(
        "INSERT INTO runs (run_id, host_id, object_id, operation, state, preview_id, "
        "host_effect, created_at, cancelled_at) VALUES "
        "('run_legacy','h','o','network.interface.reconcile_mtu','preview','prv_legacy',"
        "'none','t',NULL)"
    )
