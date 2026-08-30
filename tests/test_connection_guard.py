"""The connection guard: a change to the object carrying the operator's own path.

**What is under test is a mechanism, not a label.** A guarded change is one made to the
resource an operator is reaching LocalPlane over, and the thing that makes it survivable
rather than merely refused is a *reversal held on the host with a deadline*: armed before
the write, dispatched by the agent with no further request from anybody if nothing proves
within the window that the console can still be reached over that resource. So these tests
assert what the mechanism does — a row on disk before a dispatch is possible, a real timer
whose expiry sends a real reversal through the real privileged helper to a kernel, and a
run that cannot be kept by a request which has not proved the path — rather than asserting
that something is labelled ``guarded``.

Everything runs through the same estate the write-boundary tests use: a real agent over a
real AF_UNIX socket, a real privileged-helper process object over a second one, real frame
construction and a simulated kernel behind the last datagram. The one thing scripted here
that is not scripted there is the **passage of time**: a guard's deadline is fired on
demand rather than waited for, so a firing is deterministic while the reversal it dispatches
is entirely real.

The claims:

* **``unknown`` is not guarded.** A target whose relation to the management path cannot be
  established gets neither path, and no arrangement of the other prerequisites changes it.
* **A guard is armed before a Change can exist**, and the store enforces that rather than
  the order of the code.
* **A caller supplies nothing.** Not a backup path, not an interface, not a command, not a
  deadline, not the value to restore.
* **Only a proof carried by a request can keep a guarded change**, and it is the same
  two-source proof the management path is always established with.
* **Nothing settles into success on a timer**, and everything the guard cannot establish
  ends in the recovery model that already exists.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pytest

from localplane.agent.server import AgentServer
from localplane.agent.service import AgentService
from localplane.backend.agent_client import AgentClient
from localplane.backend.changes import ChangeService
from localplane.backend.db.repositories import ManagementPathRepository
from localplane.backend.domain.changes import (
    ChangeResult,
    HostEffect,
    MutationOutcome,
    RecoveryReason,
    VerificationOutcome,
)
from localplane.backend.domain.guard import GuardPhase
from localplane.backend.domain.protection import ManagementPathVerdict
from localplane.backend.domain.runs import ExecutionEligibility, OperationType, RunState
from localplane.backend.ingest import Ingestor, ObservationCoordinator
from localplane.backend.management import ManagementService
from localplane.backend.operations import OPERATIONS, build_executors
from localplane.backend.provenance import ProvenanceService
from localplane.backend.runs import RunRefused, RunService
from localplane.helper.client import HelperClient
from localplane.helper.server import HelperServer
from localplane.helper.service import HelperService
from tests.conftest import FakeKernelLinks, write_interface
from tests.test_write_boundary import Estate, _runner

RECONCILE = OperationType.NETWORK_INTERFACE_RECONCILE_MTU
_NOW = "2026-08-23T12:00:00.000000+00:00"


# ------------------------------------------------------------------------------- fixture


@dataclass
class _Pending:
    """One armed deadline, held rather than counted down."""

    delay_s: float
    action: Callable[[], None]
    cancelled: bool = False

    def cancel(self) -> None:
        self.cancelled = True


@dataclass
class ManualTimers:
    """Every guard deadline this agent arms, fired on demand instead of on the clock.

    The seam is the *timer* and nothing else. When one is fired the registry does exactly
    what it does in production: it dispatches the reversal it was armed with, through the
    agent's own typed write, through the real privileged helper, to the kernel behind it.
    """

    armed: list[_Pending] = field(default_factory=list)

    def __call__(self, delay_s: float, action: Callable[[], None]) -> _Pending:
        pending = _Pending(delay_s, action)
        self.armed.append(pending)
        return pending

    @property
    def live(self) -> list[_Pending]:
        return [p for p in self.armed if not p.cancelled]

    def fire(self) -> int:
        """Let every deadline that is still armed expire. Returns how many did."""
        fired = 0
        for pending in list(self.live):
            # Marked before the action runs, because a deadline that has expired is no
            # longer armed — `live` means "still holding", and a fired guard is not.
            pending.cancelled = True
            pending.action()
            fired += 1
        return fired


@pytest.fixture
def guarded_estate(database, fake_root: Path, sysfs_net: Path, tmp_path: Path) -> Any:
    """The write-boundary estate, with guard deadlines under this test's control.

    ``eth0`` is both the write target and the object the operator's connection is proven to
    arrive on — which is the whole subject of this file and the situation every earlier
    build refused outright.
    """
    write_interface(sysfs_net, "lo", ifindex=1, address="00:00:00:00:00:00", arphrd="772",
                    flags="0x9", operstate="unknown", carrier="1", mtu="65536",
                    speed=None, duplex=None)
    write_interface(sysfs_net, "eth0", ifindex=2, address="02:00:00:00:00:10", flags="0x1003",
                    operstate="up", carrier="1", mtu="1500", device="fd580000.ethernet",
                    subsystem="platform")
    write_interface(sysfs_net, "eth1", ifindex=3, address="02:00:00:00:00:12", flags="0x1003",
                    operstate="up", carrier="1", mtu="1500", speed="1000", duplex="full",
                    device="1-1.4:1.0", subsystem="usb")

    kernel = FakeKernelLinks(links={1: ("lo", 65536), 2: ("eth0", 1500), 3: ("eth1", 1500)},
                             sysfs_net=sysfs_net)
    helper_server = HelperServer(tmp_path / "helper.sock", HelperService(transport=kernel))
    helper_server.serve_in_thread()

    timers = ManualTimers()
    runner = _runner()
    service = AgentService(root=fake_root, sysfs_net=sysfs_net, runner=runner,
                           docker_socket=tmp_path / "docker-absent.sock",
                           helper_client=HelperClient(tmp_path / "helper.sock"),
                           helper_socket=tmp_path / "helper.sock",
                           guard_timer=timers)
    agent_server = AgentServer(tmp_path / "agent.sock", service)
    agent_server.serve_in_thread()

    client = AgentClient(agent_server.socket_path, timeout_s=10.0)
    provenance = ProvenanceService(database)
    management = ManagementService(database, freshness_ttl_s=60.0, provenance=provenance)
    ingestor = Ingestor(database, management)
    coordinator = ObservationCoordinator(client, ingestor)
    runs = RunService(database, 60.0, OPERATIONS, provenance)
    estate = Estate(
        database=database, sysfs=sysfs_net, runner=runner, kernel=kernel, service=service,
        agent_server=agent_server, helper_server=helper_server, client=client,
        ingestor=ingestor, coordinator=coordinator, management=management, runs=runs,
        changes=ChangeService(database, runs,
                              build_executors(client, coordinator, ingestor.objects)),
    )
    estate.timers = timers  # type: ignore[attr-defined]
    estate.observe()
    estate.prove_path_on("eth0")
    try:
        yield estate
    finally:
        for server in (agent_server, helper_server):
            server.shutdown()
            server.server_close()


@pytest.fixture
def guarded(guarded_estate: Estate) -> Estate:
    """``eth0`` adopted at 1500, drifted to 1400, and proven to carry this connection."""
    guarded_estate.adopt("eth0")
    guarded_estate.drift("eth0", "1400")
    guarded_estate.prove_path_on("eth0")
    return guarded_estate


def _typed(estate: Estate, run: Any) -> Any:
    """Satisfy the typed confirmation a guarded plan requires."""
    return estate.changes.confirm(
        run,
        preview_id=run.preview.preview_id,
        acknowledge=True,
        acknowledge_object=estate.object_named("eth0").display_name,
        expected_preview_digest=None,
        management_path=estate.management_path,
    )


def guarded_apply(estate: Estate) -> Any:
    """Plan, confirm with a typed statement, and apply. Ends in ``guarded``."""
    run = estate.plan("eth0").run
    _typed(estate, run)
    return estate.apply(run)


def _guard_row(estate: Estate, run_id: str) -> Any:
    return estate.changes.guards.for_run(run_id)


def _unknown_path(estate: Estate) -> ManagementPathVerdict:
    """A request that cannot establish where it is connected from."""
    return ManagementPathVerdict(resource_id=None, reason="management_path_unobserved")


def _path_evidence(estate: Estate, *, peer: str = "192.0.2.131") -> str:
    """A second, later management-path observation proving the same path.

    Real evidence rather than a fabricated identifier: the guard's keep proof is a foreign
    key into the observations table and a trigger checks it is about this host, so a verdict
    naming a row that does not exist is one the store refuses.
    """
    observation_id = f"mpo_{uuid.uuid4().hex}"
    with estate.database.transaction():
        ManagementPathRepository(estate.database).insert({
            "observation_id": observation_id, "host_id": estate.host_id, "observed_at": _NOW,
            "agent_instance_id": None,
            "transport_peer_address": peer, "transport_peer_family": "inet",
            "local_endpoint_address": "192.0.2.215", "local_endpoint_family": "inet",
            "capability": "network.route.observe", "provider": "linux.route",
            "provider_version": "1", "method": "netlink_rtm_getroute",
            "route_status": "resolved", "route_reason": None, "route_family": "inet",
            "route_destination": peer, "route_destination_prefix_length": 32,
            "route_preferred_source": "192.0.2.215", "route_gateway": None,
            "route_oif_index": 2, "route_table": 254, "route_type": "unicast",
            "route_scope": "universe", "route_protocol": "unspec", "route_priority": None,
            "route_error": None,
        })
    return observation_id


def _proof_over_target(estate: Estate) -> ManagementPathVerdict:
    """The evidence a keep request carries: this connection lands on ``eth0``, now."""
    return ManagementPathVerdict(
        resource_id=estate.object_named("eth0").object_id,
        reason="management_path_confirmed",
        evidence_id=_path_evidence(estate),
        observed_at=_NOW,
    )


# ------------------------------------------------------------- what stays blocked, and why


def test_a_target_whose_relation_is_unknown_is_still_blocked_outright(guarded: Estate):
    """The rule the whole product rests on, restated against a build that has a guard now.

    An unresolved relation gets neither path. Not the ordinary one, because LocalPlane
    cannot prove the target is not carrying this connection; and not the guarded one,
    because a guard is a mechanism against a *specific* hazard — arming one here would mean
    dispatching a change LocalPlane cannot justify and then a reversal it cannot justify
    either.
    """
    unknown = _unknown_path(guarded)
    outcome = guarded.runs.create(RECONCILE, guarded.object_named("eth0"), unknown)
    plan = outcome.plan

    assert plan.protection.management_path == "unknown"
    assert plan.execution.eligibility is ExecutionEligibility.BLOCKED
    assert "management_path_unproven" in plan.execution.blockers
    assert plan.guard.availability == "unavailable"
    assert plan.guard.reason == "management_path_unproven"
    assert "target_is_proven_management_path" in [str(p) for p in plan.guard.unmet]

    # And it cannot be confirmed or applied, so no guard can be armed from it.
    with pytest.raises(RunRefused) as confirm_refusal:
        guarded.changes.confirm(
            outcome.run, preview_id=outcome.run.preview.preview_id, acknowledge=True,
            acknowledge_object="eth0", expected_preview_digest=None, management_path=unknown)
    assert confirm_refusal.value.code == "preview_not_executable"
    assert guarded.rows("run_guards") == []


def test_a_guard_is_not_offered_where_the_host_cannot_hold_one(guarded: Estate):
    """A plan against an agent that does not declare the guard capability is blocked.

    The two sides of this product are versioned independently: an agent that can set an MTU
    and has never heard of a guard simply does not list it. A backend that assumed
    otherwise would publish a guarded plan against a host that would refuse to arm one, and
    then dispatch a change with nothing watching it.
    """
    from localplane.backend.domain.policy import assess_guard
    from localplane.backend.operations import RECONCILE_MTU

    plan = guarded.plan("eth0").plan
    without = assess_guard(
        RECONCILE_MTU, protection=plan.protection, guard_capability_declared=False)
    assert without.availability == "unavailable"
    assert without.reason == "guard_capability_undeclared"
    assert [str(p) for p in without.unmet] == ["guard_capability_declared"]


def test_an_operation_with_no_unattended_reversal_is_never_guarded(guarded: Estate):
    """A guard is a reversal. An operation without one cannot have a guard, whatever else.

    The inverse of ``start`` is not ``stop``; it is a second change nobody asked for. A
    deadline that expires into one of those would make a bad situation worse with nobody
    watching, which is the opposite of what the mechanism is for.
    """
    import dataclasses

    from localplane.backend.container_operations import OPERATIONS as CONTAINERS
    from localplane.backend.domain.policy import assess_guard

    plan = guarded.plan("eth0").plan

    # A container action declares it could not be carrying a management path at all, so the
    # question stops one step earlier: there is no hazard to guard against, which is a
    # different sentence from "it could not be guarded" and gets its own code.
    for operation, typed in CONTAINERS.items():
        assessed = assess_guard(
            typed.definition, protection=plan.protection, guard_capability_declared=True)
        assert assessed.availability == "unavailable", operation
        assert assessed.reason == "guard_not_required", operation
        assert typed.definition.guarded_execution is False, operation

    # And the reversibility branch itself: an operation that *could* be on the path and has
    # no unattended inverse is refused a guard on that ground and names it.
    from localplane.backend.operations import RECONCILE_MTU

    irreversible = dataclasses.replace(RECONCILE_MTU, guarded_execution=False)
    assessed = assess_guard(
        irreversible, protection=plan.protection, guard_capability_declared=True)
    assert assessed.availability == "unavailable"
    assert assessed.reason == "operation_has_no_unattended_reversal"
    assert [str(u) for u in assessed.unmet] == ["operation_is_reversible"]


def test_a_target_proven_off_the_path_keeps_the_ordinary_route_and_gains_no_guard(
    guarded: Estate,
):
    """Nothing about the guard changes what happens to an ordinary target.

    A guard for an object that is not carrying the connection would be a mechanism armed
    against no hazard, so it is reported as `guard_not_required` rather than as a missing
    prerequisite — and the plan stays plainly `eligible`, confirmed with `acknowledge`, and
    applied without a guard row ever existing.
    """
    guarded.prove_path_on("eth1")
    outcome = guarded.plan("eth0")
    assert outcome.plan.execution.eligibility is ExecutionEligibility.ELIGIBLE
    assert outcome.plan.execution.blockers == ()
    assert outcome.plan.guard.availability == "unavailable"
    assert outcome.plan.guard.reason == "guard_not_required"
    assert outcome.plan.confirmation.method == "acknowledge"

    guarded.confirm(outcome.run)
    applied = guarded.apply(outcome.run)
    assert applied.run.state == str(RunState.SUCCEEDED)
    assert guarded.rows("run_guards") == []
    assert guarded.timers.armed == []
    assert guarded.mtu_of("eth0") == 1500


def test_stale_evidence_cannot_arm_a_guard(guarded: Estate):
    """A plan whose reading has aged past the freshness horizon cannot be applied at all.

    The guard rests on a checkpoint, the checkpoint rests on an observation, and the value
    it would restore is only as good as the reading it came from. Nothing here is special to
    the guard — it is the ordinary staleness rule — and the point of the test is that a
    guarded apply does not get around it.
    """
    outcome = guarded.plan("eth0")
    _typed(guarded, outcome.run)
    with guarded.database.transaction():
        guarded.database.connection.execute(
            "UPDATE observations SET observed_at = '2020-01-01T00:00:00+00:00'")
    with pytest.raises(RunRefused) as refusal:
        guarded.apply(outcome.run)
    assert refusal.value.code == "preview_stale"
    assert guarded.rows("run_guards") == []
    assert guarded.rows("changes") == []


# ------------------------------------------------------------------- what a guarded plan says


def test_a_guarded_plan_says_ordinary_execution_is_blocked_and_names_the_other_path(
    guarded: Estate,
):
    """The preview an operator reviews, and the three things it has to say at once.

    That the ordinary route is closed, and why; that a guarded route exists; and what
    arming would actually establish — which is a reversal held somewhere that survives this
    connection, not a stronger warning.
    """
    plan = guarded.plan("eth0").plan

    assert plan.protection.management_path == "on_management_path"
    assert plan.protection.status == "protected"
    assert plan.execution.eligibility is ExecutionEligibility.GUARDED
    assert plan.execution.blockers == ("target_is_management_path",)
    assert plan.guard.availability == "available"
    assert plan.guard.reason == "guarded_execution_available"
    assert plan.guard.unmet == ()
    assert plan.guard.window_s > 0
    # The one prerequisite a published plan may never claim: it cannot be evaluated before
    # the attempt, and a document claiming it would be a document claiming a guard.
    assert "host_side_guard_accepted" in [str(p) for p in plan.guard.prerequisites]
    assert plan.risk.tier == "high"
    assert plan.confirmation.method == "typed"
    # And nothing is armed by publishing it.
    assert plan.guard.availability == "available" and guarded.rows("run_guards") == []


def test_the_published_plan_is_stored_with_its_guard_and_the_digest_covers_it(guarded: Estate):
    """The guard section is content-addressed like every other part of the plan.

    A plan whose only write path is a guarded one and the same plan when it was simply
    blocked are different decisions to review, so they may not share an identity. The row
    holds everything the digest covers, which is what keeps the digest recomputable from the
    store rather than merely repeatable through today's planner.
    """
    from localplane.backend.domain.runs import plan_digest

    outcome = guarded.plan("eth0")
    row = guarded.rows("run_previews")[0]
    assert row["guard_availability"] == "available"
    assert row["guard_window_s"] == outcome.plan.guard.window_s
    assert row["guard_armed"] == 0
    assert row["execution_eligibility"] == "guarded"

    rebuilt = guarded.runs.published_plan(guarded.runs.get(outcome.run.run_id))
    assert rebuilt.guard == outcome.plan.guard
    assert plan_digest(rebuilt, row["digest_version"]) == row["preview_digest"]


def test_the_same_object_off_the_path_and_on_it_are_different_documents(guarded: Estate):
    """Two plans for the same reconciliation, and they must not share a digest.

    One of them can be applied by pressing a button. The other rearms the console the
    operator is holding. A digest that could not tell them apart would make a confirmation
    for the first authority for the second.
    """
    on_path = guarded.plan("eth0").plan
    guarded.prove_path_on("eth1")
    off_path = guarded.plan("eth0").plan

    from localplane.backend.domain.runs import plan_digest

    assert plan_digest(on_path) != plan_digest(off_path)
    assert on_path.change == off_path.change  # the same reconciliation, either way


# -------------------------------------------------------------------------- confirmation


def test_a_guarded_plan_requires_the_typed_method_and_the_object_name(guarded: Estate):
    """`typed` has been the policy's demand for a management-path target since the policy
    existed, and nothing could satisfy it until a guard could be armed. Now something can.

    What typing establishes is that a person looked at *which* object this is about. It is
    not authority — the guard, the checkpoint and the proven relation are what make a
    guarded execution permissible — and the store keeps what was written rather than the
    fact that something was.
    """
    run = guarded.plan("eth0").run

    with pytest.raises(RunRefused) as missing:
        guarded.changes.confirm(
            run, preview_id=run.preview.preview_id, acknowledge=True,
            expected_preview_digest=None, management_path=guarded.management_path)
    assert missing.value.code == "confirmation_object_required"

    with pytest.raises(RunRefused) as wrong:
        guarded.changes.confirm(
            run, preview_id=run.preview.preview_id, acknowledge=True,
            acknowledge_object="eth1", expected_preview_digest=None,
            management_path=guarded.management_path)
    assert wrong.value.code == "confirmation_object_mismatch"

    record = _typed(guarded, run)
    assert record.method == "typed"
    assert record.required_method == "typed"
    assert record.typed_statement == "eth0"
    assert record.consumed is False


def test_an_ordinary_plan_refuses_a_typed_statement_it_did_not_ask_for(guarded: Estate):
    """Refused rather than ignored. A field that is silently dropped is one a caller can
    come to believe means something."""
    guarded.prove_path_on("eth1")
    run = guarded.plan("eth0").run
    with pytest.raises(RunRefused) as refusal:
        guarded.changes.confirm(
            run, preview_id=run.preview.preview_id, acknowledge=True,
            acknowledge_object="eth0", expected_preview_digest=None,
            management_path=guarded.management_path)
    assert refusal.value.code == "confirmation_object_not_required"


def test_the_typed_confirmation_is_single_use_exactly_as_the_other_kind_is(guarded: Estate):
    """One confirmation authorises one attempt. The attempt happened.

    The guarded path introduces no second authority system and relaxes nothing about the
    first: the same table, the same trigger refusing an update to a consumed row, the same
    partial unique index allowing one apply confirmation per Run, and the same refusal on a
    second apply.
    """
    run = guarded.plan("eth0").run
    _typed(guarded, run)
    guarded.apply(run)

    stored = guarded.changes.confirmation_for(run.run_id)
    assert stored.consumed is True

    with pytest.raises(RunRefused) as second:
        _typed(guarded, guarded.runs.get(run.run_id))
    assert second.value.code == "run_not_confirmable"

    with pytest.raises(sqlite3.IntegrityError):
        with guarded.database.transaction():
            guarded.database.connection.execute(
                "INSERT INTO run_confirmations (confirmation_id, run_id, purpose, preview_id, "
                "preview_digest, digest_version, required_method, method, typed_statement, "
                "policy, source, satisfied_at) VALUES "
                "(?,?,'apply',?,?,3,'typed','typed','eth0','p','unauthenticated_request',?)",
                (f"cnf_{uuid.uuid4().hex}", run.run_id, run.preview.preview_id,
                 run.preview.preview_digest, _NOW))


# ----------------------------------------------------------------------------- the arming


def test_the_guard_is_durably_armed_before_a_change_can_exist(guarded: Estate):
    """The ordering the whole mechanism rests on, asserted from the durable record.

    A guard is armed, its row says the *holder answered*, and only then does a Change come
    into existence — so a process that dies anywhere after the boundary leaves something on
    the host that will put the value back without being asked again. The transcript carries
    the same order, and the timestamps agree with it.
    """
    outcome = guarded_apply(guarded)
    run_id = outcome.run.run_id
    guard = _guard_row(guarded, run_id)
    change = guarded.changes.change_for_run(run_id)

    assert guard is not None and guard.armed_at is not None
    assert guard.holder_id == guarded.service.instance_id
    assert guard.expires_at is not None
    assert guard.window_s == outcome.run.preview.guard_window_s

    # Armed, then the boundary, then the dispatch. Nothing overlaps and nothing is implied.
    assert guard.arm_began_at <= guard.armed_at <= change.created_at
    assert change.created_at <= change.dispatch_began_at

    events = guarded.events(run_id)
    assert events.index("checkpoint_written") < events.index("guard_armed")
    assert events.index("guard_armed") < events.index("write_boundary_crossed")
    assert events.index("write_boundary_crossed") < events.index("mutation_dispatched")


def test_the_store_refuses_a_change_to_the_management_path_with_no_armed_guard(guarded: Estate):
    """The statement 0007 made with a CHECK, moved to where it is actually about.

    A checkpoint for a management-path target may exist now. A **Change** against one may
    not, unless a guard is armed and holding — and the trigger reads the guard's own
    `armed_at`, which is written only when the holder has answered. A guard LocalPlane
    merely asked for does not open the boundary.
    """
    outcome = guarded_apply(guarded)
    run_id = outcome.run.run_id
    change = guarded.changes.change_for_run(run_id)
    checkpoint = guarded.changes.checkpoint_for(run_id)
    assert checkpoint.protection_management_path == "on_management_path"

    # Forge a second Change against the same checkpoint with the guard settled away.
    with guarded.database.transaction():
        guarded.database.connection.execute(
            "UPDATE run_guards SET settled_at = ?, settled_phase = 'disarmed' WHERE run_id = ?",
            (_NOW, run_id))
    with pytest.raises(sqlite3.IntegrityError, match="armed connection guard"):
        with guarded.database.transaction():
            guarded.database.connection.execute(
                "INSERT INTO changes (change_id, run_id, preview_id, checkpoint_id, host_id, "
                "object_id, operation, change_kind, field, value_type, before_value, "
                "desired_value, created_at, apply_attempt_id) VALUES "
                "(?,?,?,?,?,?,?,'field','mtu','integer',1400,1500,?,?)",
                (f"chg_{uuid.uuid4().hex}", run_id, change.preview_id, checkpoint.checkpoint_id,
                 change.host_id, change.object_id, change.operation, _NOW,
                 f"apl_{uuid.uuid4().hex}"))


def test_a_guard_that_cannot_be_armed_ends_the_run_before_the_boundary(guarded: Estate):
    """Refused, and refused where a checkpoint that cannot be written is refused.

    `failed` is honest here for the one reason it is honest anywhere: the code that could
    perform a write had not been reached. No Change exists, the lock is released, and the
    confirmation stays consumed — it authorised an attempt, and the attempt happened.
    """
    run = guarded.plan("eth0").run
    _typed(guarded, run)

    original = guarded.service._guards.arm

    def refuse(**kwargs: Any) -> Any:
        from localplane.agent.guard import GuardRefusal

        raise GuardRefusal("guard_window_out_of_range", {"window_s": kwargs.get("window_s")})

    guarded.service._guards.arm = refuse  # type: ignore[method-assign]
    try:
        with pytest.raises(RunRefused) as refusal:
            guarded.apply(run)
    finally:
        guarded.service._guards.arm = original  # type: ignore[method-assign]

    assert refusal.value.code == "invalid_params"
    assert guarded.rows("changes") == []
    assert guarded.runs.get(run.run_id).state == str(RunState.FAILED)
    assert guarded.runs.get(run.run_id).host_effect == "none"
    assert guarded.changes.lock_for(run.run_id) is None
    assert guarded.changes.confirmation_for(run.run_id).consumed is True
    assert "guard_arming_failed" in guarded.events(run.run_id)
    # And the row is settled rather than left claiming something is holding.
    guard = _guard_row(guarded, run.run_id)
    assert guard.settled is True and guard.armed_at is None
    assert guarded.mtu_of("eth0") == 1400  # untouched


def test_arming_names_only_what_the_checkpoint_already_holds(guarded: Estate):
    """The reversal is fully determined by records LocalPlane already had.

    Nothing about it is computed from a request, and its precondition is the value the
    change is about to leave behind — which is the single line that makes a guard incapable
    of undoing anything but its own change.
    """
    seen: list[dict[str, Any]] = []
    original = guarded.service._guards.arm

    def record(**kwargs: Any) -> Any:
        seen.append(dict(kwargs))
        return original(**kwargs)

    guarded.service._guards.arm = record  # type: ignore[method-assign]
    try:
        guarded_apply(guarded)
    finally:
        guarded.service._guards.arm = original  # type: ignore[method-assign]

    assert len(seen) == 1
    armed = seen[0]
    assert armed["guarded_mtu"] == 1500  # what the change leaves behind
    assert armed["restore_mtu"] == 1400  # what the checkpoint holds
    assert armed["ifindex"] == guarded.ifindex_of("eth0")
    assert armed["expected_interface_name"] == "eth0"
    assert set(armed) == {
        "guard_id", "attempt_id", "ifindex", "expected_interface_name",
        "guarded_mtu", "restore_mtu", "window_s",
    }


# ------------------------------------------------------------------------------- the hold


def test_a_verified_guarded_write_holds_rather_than_succeeding(guarded: Estate):
    """The operation's own result is proved and the connection's is not. That is `guarded`.

    Everything the write boundary already asserts still holds — the value is on the host,
    a fresh reading through the ordinary path proved it, the drift finding moved — and none
    of it is success, because the question a guarded change exists to ask is whether the
    operator is still there.
    """
    outcome = guarded_apply(guarded)
    run = guarded.runs.get(outcome.run.run_id)
    change = guarded.changes.change_for_run(run.run_id)

    assert run.state == str(RunState.GUARDED)
    assert run.host_effect == str(HostEffect.WRITTEN)
    assert run.finished_at is None
    assert change.mutation_outcome == str(MutationOutcome.WRITTEN)
    assert change.verification_outcome == str(VerificationOutcome.VERIFIED)
    assert change.verification_observation_id is not None
    assert change.result == str(ChangeResult.IN_FLIGHT)
    assert guarded.mtu_of("eth0") == 1500

    events = guarded.events(run.run_id)
    assert events[-1] == "guard_hold_started"
    assert "run_finished" not in events
    # The object stays held while the answer is outstanding.
    assert guarded.changes.lock_for(run.run_id) is not None
    # And a deadline is genuinely armed on the host side.
    assert len(guarded.timers.live) == 1


def test_a_guarded_run_cannot_be_cancelled_or_applied_again(guarded: Estate):
    """Past the boundary, and the only two ways out are the guard's and the operator's."""
    outcome = guarded_apply(guarded)
    run = guarded.runs.get(outcome.run.run_id)

    with pytest.raises(RunRefused) as cancelled:
        guarded.runs.cancel(run)
    assert cancelled.value.code == "run_not_cancellable"

    with pytest.raises(RunRefused) as applied:
        guarded.apply(run)
    assert applied.value.code == "run_not_appliable"


# -------------------------------------------------------------------------------- keeping


def test_only_a_request_that_proves_the_path_can_keep_a_guarded_change(guarded: Estate):
    """The whole mechanism, in one test: the proof is the request, not a permission.

    A call that cannot establish it reached LocalPlane over the object that was changed
    cannot keep the change — and the guard goes on holding, which is the correct outcome
    rather than a punishment: the question is still open.
    """
    outcome = guarded_apply(guarded)
    run = guarded.runs.get(outcome.run.run_id)

    with pytest.raises(RunRefused) as refusal:
        guarded.changes.guard_keep(run, _unknown_path(guarded), acknowledge=True)
    assert refusal.value.code == "guard_connection_not_proved"
    assert refusal.value.detail["management_path"] == "unknown"

    assert guarded.runs.get(run.run_id).state == str(RunState.GUARDED)
    assert _guard_row(guarded, run.run_id).settled is False
    assert len(guarded.timers.live) == 1
    assert "guard_keep_refused" in guarded.events(run.run_id)


def test_a_proof_over_a_different_object_cannot_keep_it_either(guarded: Estate):
    """Proving *a* management path is not proving *this* one.

    An operator who has fallen back to another route has proved that route works. What a
    guarded change is waiting to hear is that the object it changed still carries a session,
    and nothing else answers that.
    """
    outcome = guarded_apply(guarded)
    run = guarded.runs.get(outcome.run.run_id)
    elsewhere = ManagementPathVerdict(
        resource_id=guarded.object_named("eth1").object_id,
        reason="management_path_confirmed", evidence_id=_path_evidence(guarded),
        observed_at=_NOW)

    with pytest.raises(RunRefused) as refusal:
        guarded.changes.guard_keep(run, elsewhere, acknowledge=True)
    assert refusal.value.code == "guard_connection_not_proved"
    assert refusal.value.detail["management_path"] == "not_on_management_path"
    assert guarded.runs.get(run.run_id).state == str(RunState.GUARDED)


def test_an_unacknowledged_keep_changes_nothing(guarded: Estate):
    outcome = guarded_apply(guarded)
    run = guarded.runs.get(outcome.run.run_id)
    with pytest.raises(RunRefused) as refusal:
        guarded.changes.guard_keep(run, _proof_over_target(guarded), acknowledge=False)
    assert refusal.value.code == "keep_not_acknowledged"
    assert _guard_row(guarded, run.run_id).settled is False
    assert len(guarded.timers.live) == 1


def test_a_proved_keep_releases_the_guard_and_succeeds(guarded: Estate):
    """The successful guarded execution, end to end, and what makes it a success.

    Both halves: the operation's own result was proved by a fresh reading before the hold
    began, and the connection was proved by a request that arrived over the changed object
    afterwards. The guard is released — the deadline is cancelled on the host, so nothing
    will fire — and the evidence that released it is recorded as what it was.
    """
    outcome = guarded_apply(guarded)
    run = guarded.runs.get(outcome.run.run_id)
    proof = _proof_over_target(guarded)

    kept = guarded.changes.guard_keep(run, proof, acknowledge=True)

    assert kept.run.state == str(RunState.SUCCEEDED)
    assert kept.run.host_effect == str(HostEffect.WRITTEN)
    assert kept.change.result == str(ChangeResult.SUCCEEDED)
    assert guarded.mtu_of("eth0") == 1500

    guard = _guard_row(guarded, run.run_id)
    assert guard.settled_phase == str(GuardPhase.DISARMED)
    assert guard.kept_evidence_id == proof.evidence_id
    assert guard.fired_at is None and guard.reversal_outcome is None
    assert guarded.timers.live == []  # the deadline was actually cancelled

    events = guarded.events(run.run_id)
    assert "guard_connection_proved" in events
    assert events.index("guard_connection_proved") < events.index("guard_settled")
    assert events[-1] == "run_finished"
    assert guarded.changes.lock_for(run.run_id) is None


def test_keeping_twice_is_refused(guarded: Estate):
    outcome = guarded_apply(guarded)
    run = guarded.runs.get(outcome.run.run_id)
    guarded.changes.guard_keep(run, _proof_over_target(guarded), acknowledge=True)
    with pytest.raises(RunRefused) as second:
        guarded.changes.guard_keep(
            guarded.runs.get(run.run_id), _proof_over_target(guarded), acknowledge=True)
    assert second.value.code == "run_not_guarded"


# ------------------------------------------------------------------------- the guard fires


def test_a_window_that_lapses_reverts_the_change_on_the_host(guarded: Estate):
    """The mechanism doing the thing it exists for, with nobody asking it to.

    Nothing in the backend runs this. The deadline expires on the host side and the agent
    dispatches the reversal it was armed with, through the real privileged helper, to the
    kernel — and the value on the link goes back before LocalPlane has been told anything.
    """
    outcome = guarded_apply(guarded)
    run_id = outcome.run.run_id
    assert guarded.mtu_of("eth0") == 1500

    assert guarded.timers.fire() == 1

    # The host is back, and the backend has not been involved.
    assert guarded.mtu_of("eth0") == 1400
    assert guarded.runs.get(run_id).state == str(RunState.GUARDED)
    assert _guard_row(guarded, run_id).settled is False

    # The holder's own account of what it did.
    status = guarded.client.mtu_guard_status(_guard_row(guarded, run_id).guard_id)["guard"]
    assert status["phase"] == str(GuardPhase.FIRED)
    assert status["mutation"]["outcome"] == str(MutationOutcome.WRITTEN)


def test_a_lapsed_guard_is_collected_and_the_run_ends_rolled_back(guarded: Estate):
    """The reversal is a rollback, and it is held to the same standard as any other.

    An acknowledgement is not a restoration: `rolled_back` is claimed only after a fresh
    reading through the ordinary observation path proves the checkpoint's value is back. The
    guard's own three-valued outcome is recorded in the rollback columns that already mean
    exactly that, so a reader does not have to learn a second vocabulary.
    """
    outcome = guarded_apply(guarded)
    run = guarded.runs.get(outcome.run.run_id)
    guarded.timers.fire()

    settled = guarded.changes.guard_keep(run, _proof_over_target(guarded), acknowledge=True)

    assert settled.run.state == str(RunState.ROLLED_BACK)
    assert settled.change.result == str(ChangeResult.ROLLED_BACK)
    assert settled.change.mutation_outcome == str(MutationOutcome.WRITTEN)
    assert settled.change.rollback_outcome == str(MutationOutcome.WRITTEN)
    assert settled.change.rollback_verification_outcome == str(VerificationOutcome.VERIFIED)
    assert settled.change.rollback_verification_observed_value == 1400
    assert settled.change.rollback_attempt_id == _guard_row(guarded, run.run_id).reversal_attempt_id

    guard = _guard_row(guarded, run.run_id)
    assert guard.settled_phase == str(GuardPhase.FIRED)
    assert guard.reversal_outcome == str(MutationOutcome.WRITTEN)
    assert guard.kept_at is None  # nothing kept it; it was reverted
    assert guarded.changes.lock_for(run.run_id) is None
    assert "guard_settled" in guarded.events(run.run_id)


def test_a_late_proof_reports_what_happened_rather_than_keeping_it(guarded: Estate):
    """A window that has lapsed cannot be un-lapsed, and the record says so plainly.

    This is the case an operator hits when their connection came back *after* the guard had
    already acted. The request proves the path, and the answer is still that the change was
    reverted — because it was.
    """
    outcome = guarded_apply(guarded)
    run = guarded.runs.get(outcome.run.run_id)
    guarded.timers.fire()

    settled = guarded.changes.guard_keep(run, _proof_over_target(guarded), acknowledge=True)
    assert settled.run.state == str(RunState.ROLLED_BACK)
    assert _guard_row(guarded, run.run_id).kept_evidence_id is None
    assert guarded.mtu_of("eth0") == 1400


def test_a_reversal_that_writes_nothing_ends_in_recovery(guarded: Estate):
    """The guard acted, the reversal was refused, and nothing proves what the host holds.

    Reproduced honestly: a third party moves the value between the write and the deadline,
    so the reversal's own compare-and-set no longer holds and the privileged helper refuses
    it before a mutating frame exists. That is the guard being *unable* to undo something it
    did not do — the property that makes a stray firing harmless — and here it means the
    situation needs a person.
    """
    outcome = guarded_apply(guarded)
    run = guarded.runs.get(outcome.run.run_id)

    guarded.write("eth0", "mtu", "9000")
    guarded.kernel.links[guarded.ifindex_of("eth0")] = ("eth0", 9000)
    guarded.timers.fire()

    settled = guarded.changes.guard_keep(run, _proof_over_target(guarded), acknowledge=True)
    assert settled.run.state == str(RunState.RECOVERY_REQUIRED)
    assert settled.change.recovery_reason == str(RecoveryReason.GUARD_REVERSAL_UNPROVEN)
    guard = _guard_row(guarded, run.run_id)
    assert guard.settled_phase == str(GuardPhase.FIRED)
    assert guard.reversal_outcome == str(MutationOutcome.NOT_WRITTEN)
    # The hold is kept: the value is unproven and a second change would build on nothing.
    assert guarded.changes.lock_for(run.run_id) is not None
    assert guarded.mtu_of("eth0") == 9000


def test_a_guard_the_holder_has_forgotten_ends_in_recovery(guarded: Estate):
    """`lost` is an answer and it is never success.

    The change is applied, the window is irrelevant, and nothing is going to put it back.
    It must not be confused with a guard that fired: one says a reversal was attempted, this
    one says none ever will be.
    """
    outcome = guarded_apply(guarded)
    run = guarded.runs.get(outcome.run.run_id)
    # The agent restarted: a new registry, and this guard is not in it.
    from localplane.agent.guard import GuardRegistry

    guarded.service._guards = GuardRegistry(
        guarded.service.instance_id, guarded.service._set_interface_mtu,
        timer_factory=guarded.timers)

    settled = guarded.changes.guard_keep(run, _proof_over_target(guarded), acknowledge=True)
    assert settled.run.state == str(RunState.RECOVERY_REQUIRED)
    assert settled.change.recovery_reason == str(RecoveryReason.GUARD_LOST)
    assert _guard_row(guarded, run.run_id).settled_phase == str(GuardPhase.LOST)
    assert guarded.changes.lock_for(run.run_id) is not None
    assert guarded.mtu_of("eth0") == 1500  # still applied, and nothing is watching it


def test_a_guard_that_cannot_be_interrogated_ends_in_recovery_and_says_so(guarded: Estate):
    """`unreachable` is the absence of an answer, and it is the most conservative ending.

    The reversal may be about to happen, may have happened, or may never happen. Reporting
    that as `lost` would be claiming to know the guard is gone; reporting it as success
    would be worse.
    """
    outcome = guarded_apply(guarded)
    run = guarded.runs.get(outcome.run.run_id)
    guarded.agent_server.shutdown()
    guarded.agent_server.server_close()

    settled = guarded.changes.guard_keep(run, _proof_over_target(guarded), acknowledge=True)
    assert settled.run.state == str(RunState.RECOVERY_REQUIRED)
    assert settled.change.recovery_reason == str(RecoveryReason.GUARD_UNREACHABLE)
    assert _guard_row(guarded, run.run_id).settled_phase == str(GuardPhase.UNREACHABLE)
    assert guarded.changes.lock_for(run.run_id) is not None


# --------------------------------------------------------- the operation's own result fails


def test_a_guarded_write_that_cannot_be_verified_is_restored_and_the_guard_released(
    guarded: Estate,
):
    """The guard answers for the connection and never for the result.

    A third party moves the value the instant LocalPlane's write lands, so verification
    cannot prove the intended MTU. That is the ordinary restoration path and it runs exactly
    as it does for an unguarded change — and only *after* it has run is the guard released,
    so a process death in the middle leaves the guard still holding. Two mechanisms aiming
    at the same value is not a hazard: both write what the checkpoint holds.
    """
    interfered: list[int] = []
    original = guarded.kernel.mutate

    def compete(request: bytes) -> bytes:
        reply = original(request)
        if not interfered:
            interfered.append(1)
            guarded.write("eth0", "mtu", "1234")
            guarded.kernel.links[guarded.ifindex_of("eth0")] = ("eth0", 1234)
        return reply

    guarded.kernel.mutate = compete  # type: ignore[method-assign]
    try:
        outcome = guarded_apply(guarded)
    finally:
        guarded.kernel.mutate = original  # type: ignore[method-assign]

    run = guarded.runs.get(outcome.run.run_id)
    change = guarded.changes.change_for_run(run.run_id)
    assert change.verification_outcome == str(VerificationOutcome.MISMATCH)
    assert run.state in (str(RunState.ROLLED_BACK), str(RunState.RECOVERY_REQUIRED))
    assert run.state != str(RunState.GUARDED)

    guard = _guard_row(guarded, run.run_id)
    assert guard.settled is True
    assert guarded.timers.live == []
    events = guarded.events(run.run_id)
    # The restoration happened first, and the guard was released after it.
    assert events.index("rollback_started") < events.index("guard_settled")


def test_a_guarded_write_the_helper_refuses_ends_failed_and_releases_the_guard(
    guarded: Estate,
):
    """`not_written` is still a proof, and a guard for a change that did not happen is
    released rather than left running.

    The reversal would have been a no-op anyway — its precondition is the value the change
    was supposed to leave behind, which the link never took — but a deadline nobody is
    expecting is not something to leave armed.
    """
    # Move the kernel's value out from under the plan so the helper's compare-and-set fails.
    guarded.kernel.links[guarded.ifindex_of("eth0")] = ("eth0", 1111)
    outcome = guarded_apply(guarded)
    run = guarded.runs.get(outcome.run.run_id)
    change = guarded.changes.change_for_run(run.run_id)

    assert change.mutation_outcome == str(MutationOutcome.NOT_WRITTEN)
    assert run.state == str(RunState.FAILED)
    assert run.host_effect == "none"
    guard = _guard_row(guarded, run.run_id)
    assert guard.settled_phase == str(GuardPhase.DISARMED)
    assert guarded.timers.live == []


# ------------------------------------------------------------------- crash and interruption


def _restart(estate: Estate) -> list[Any]:
    """What a backend start does, in the order it does it, and nothing else."""
    settled = estate.changes.settle_interrupted_guards()
    estate.changes.settle_interrupted()
    estate.changes.settle_interrupted_recovery()
    return settled


def test_a_crash_before_the_guard_was_armed_leaves_nothing_and_settles_to_nothing(
    guarded: Estate,
):
    """Provably nothing happened: no guard row, no Change, no write."""
    run = guarded.plan("eth0").run
    _typed(guarded, run)
    assert guarded.rows("run_guards") == []
    _restart(guarded)
    assert guarded.rows("run_guards") == []
    assert guarded.rows("changes") == []
    assert guarded.mtu_of("eth0") == 1400


def test_a_crash_after_arming_and_before_dispatch_leaves_the_guard_holding(guarded: Estate):
    """The row says a guard was asked for and the holder answered. No Change exists.

    A restart asks the holder rather than assuming: the guard is still armed, so it is left
    exactly as it is. Nothing is written from a restart, and nothing is released by one —
    releasing here would cancel protection for a change that may yet be dispatched.
    """
    run = guarded.plan("eth0").run
    _typed(guarded, run)

    # The process dies the instant after the guard is armed and before the boundary. The
    # apply is real up to that line: confirmation consumed, checkpoint on disk, the host
    # side holding a reversal it will act on without being asked.
    original = guarded.changes._cross

    def die(*args: Any, **kwargs: Any) -> Any:
        raise KeyboardInterrupt("the process died before the write boundary")

    guarded.changes._cross = die  # type: ignore[method-assign]
    try:
        with pytest.raises(KeyboardInterrupt):
            guarded.apply(run)
    finally:
        guarded.changes._cross = original  # type: ignore[method-assign]

    guard = _guard_row(guarded, run.run_id)
    assert guard.armed is True
    assert guarded.rows("changes") == []

    settled = _restart(guarded)
    assert settled == []  # still armed: left alone
    assert _guard_row(guarded, run.run_id).settled is False
    assert guarded.mtu_of("eth0") == 1400

    # And when the deadline passes with no change to undo, the reversal writes nothing:
    # its precondition is the value the change would have left behind, which never arrived.
    guarded.timers.fire()
    assert guarded.mtu_of("eth0") == 1400
    status = guarded.client.mtu_guard_status(guard.guard_id)["guard"]
    assert status["phase"] == str(GuardPhase.FIRED)
    assert status["mutation"]["outcome"] == str(MutationOutcome.NOT_WRITTEN)


def test_a_crash_after_dispatch_with_no_result_is_write_unknown_and_the_guard_still_fires(
    guarded: Estate,
):
    """The apply path's own crash window, with a guard in it.

    A Change that says dispatch began and does not say what happened is `write_unknown`,
    exactly as before. What is different is that the host is not left holding an unproven
    value indefinitely: the guard fires on its own deadline, and its account of what it did
    is collected on a later start rather than invented on this one.
    """
    outcome = guarded_apply(guarded)
    run_id = outcome.run.run_id
    change = guarded.changes.change_for_run(run_id)

    # Rewind the durable record to the instant after the dispatch marker was committed.
    with guarded.database.transaction():
        guarded.database.connection.execute(
            "UPDATE changes SET mutation_outcome = NULL, mutation_reason = NULL, "
            "settled_at = NULL, host_effect = 'none', result = 'in_flight', "
            "finished_at = NULL, verification_outcome = 'not_attempted', "
            "verification_observation_id = NULL, verification_observed_value = NULL "
            "WHERE change_id = ?", (change.change_id,))
        guarded.database.connection.execute(
            "UPDATE runs SET state = 'applying', host_effect = 'none', finished_at = NULL "
            "WHERE run_id = ?", (run_id,))

    settled = _restart(guarded)
    assert settled == []  # the guard is still armed and is left to do its job
    after = guarded.changes.get_change(change.change_id)
    assert after.mutation_outcome == str(MutationOutcome.WRITE_UNKNOWN)
    assert after.result == str(ChangeResult.RECOVERY_REQUIRED)
    assert after.recovery_reason == str(RecoveryReason.APPLY_WRITE_UNKNOWN)
    assert guarded.changes.lock_for(run_id) is not None

    # The guard still puts the value back without being asked.
    guarded.timers.fire()
    assert guarded.mtu_of("eth0") == 1400
    # And a later start collects what it did, without rewriting the Change.
    _restart(guarded)
    guard = _guard_row(guarded, run_id)
    assert guard.settled_phase == str(GuardPhase.FIRED)
    assert guard.reversal_outcome == str(MutationOutcome.WRITTEN)
    unchanged = guarded.changes.get_change(change.change_id)
    assert unchanged.mutation_outcome == str(MutationOutcome.WRITE_UNKNOWN)
    assert unchanged.result == str(ChangeResult.RECOVERY_REQUIRED)


def test_a_crash_after_a_durable_result_preserves_it(guarded: Estate):
    """A restart never replaces a fact LocalPlane established with one it did not.

    The guarded Run is deliberately unsettled — it wrote, it proved what it wrote, and it is
    waiting on a connection nobody in this process can produce. Reading its Change back as
    "interrupted" would report something proved as something unprovable, in the direction
    nobody thinks to check.
    """
    outcome = guarded_apply(guarded)
    run_id = outcome.run.run_id
    before = guarded.changes.change_for_run(run_id)

    _restart(guarded)

    after = guarded.changes.get_change(before.change_id)
    assert after.mutation_outcome == str(MutationOutcome.WRITTEN)
    assert after.verification_outcome == str(VerificationOutcome.VERIFIED)
    assert after.result == str(ChangeResult.IN_FLIGHT)
    assert guarded.runs.get(run_id).state == str(RunState.GUARDED)
    # And it can still be kept afterwards, which is the point of leaving it alone.
    kept = guarded.changes.guard_keep(
        guarded.runs.get(run_id), _proof_over_target(guarded), acknowledge=True)
    assert kept.run.state == str(RunState.SUCCEEDED)


def test_a_crash_before_the_hold_began_does_not_settle_a_guarded_change_as_success(
    guarded: Estate,
):
    """A written and verified guarded change is still not a success, and a restart says so.

    The narrow window between the verification and the hold: both halves of `succeeded` are
    on disk — the write occurred and a reading proved it — and for an ordinary Run that is
    exactly what a restart may conclude from. Not here. The guard is armed, its deadline is
    still coming, and the one thing outstanding is whether the operator can still reach
    LocalPlane at all. Concluding `succeeded` would claim a proof this build gets only from
    a request that arrives, and it would release an object whose value may be about to be
    put back without anybody asking.
    """
    outcome = guarded_apply(guarded)
    run_id = outcome.run.run_id
    change = guarded.changes.change_for_run(run_id)
    assert change.mutation_outcome == str(MutationOutcome.WRITTEN)
    assert change.verification_outcome == str(VerificationOutcome.VERIFIED)

    # Rewind to the instant before `_hold` ran: the Run has not reached `guarded`, so the
    # settlement does not skip it and has to decide for itself.
    with guarded.database.transaction():
        guarded.database.connection.execute(
            "UPDATE runs SET state = 'verifying', finished_at = NULL WHERE run_id = ?",
            (run_id,))

    _restart(guarded)

    after = guarded.changes.get_change(change.change_id)
    assert after.mutation_outcome == str(MutationOutcome.WRITTEN)
    assert after.verification_outcome == str(VerificationOutcome.VERIFIED)
    assert after.result == str(ChangeResult.RECOVERY_REQUIRED)
    assert after.recovery_reason == str(RecoveryReason.APPLY_INTERRUPTED_AFTER_WRITE)
    assert guarded.runs.get(run_id).state == str(RunState.RECOVERY_REQUIRED)
    assert guarded.changes.lock_for(run_id) is not None
    # The guard was neither released nor cancelled by the restart.
    assert _guard_row(guarded, run_id).settled is False


def test_a_restart_collects_a_guard_that_fired_while_the_backend_was_down(guarded: Estate):
    """The reversal happened without the backend. The restart reads it and ends truthfully.

    Nothing is written from the restart: the only calls it makes are the status read and the
    ordinary observation path — the same reading any other verification takes.
    """
    outcome = guarded_apply(guarded)
    run_id = outcome.run.run_id
    guarded.timers.fire()
    assert guarded.mtu_of("eth0") == 1400

    settled = _restart(guarded)

    assert len(settled) == 1
    assert settled[0].settled_phase == str(GuardPhase.FIRED)
    assert settled[0].reversal_outcome == str(MutationOutcome.WRITTEN)
    run = guarded.runs.get(run_id)
    assert run.state == str(RunState.ROLLED_BACK)
    assert guarded.changes.change_for_run(run_id).result == str(ChangeResult.ROLLED_BACK)
    assert guarded.changes.lock_for(run_id) is None


def test_a_restart_never_releases_a_guard_that_is_still_holding(guarded: Estate):
    """Startup asks and never disarms. A backend coming back must not cancel protection.

    The distinction is a whole method: `guard_status` reads, `disarm_mtu_guard` releases,
    and one call that sometimes did both would be one call away from doing exactly that.
    """
    outcome = guarded_apply(guarded)
    run_id = outcome.run.run_id

    for _ in range(3):
        assert _restart(guarded) == []

    assert len(guarded.timers.live) == 1
    assert _guard_row(guarded, run_id).settled is False
    assert guarded.runs.get(run_id).state == str(RunState.GUARDED)


def test_a_restart_that_cannot_reach_the_holder_settles_nothing(guarded: Estate):
    """`unreachable` at startup means "ask again later", not "the guard is gone".

    An agent that has not finished starting is not a guard that has disappeared, and
    settling a Run on that would record an admission the next attempt would contradict.
    """
    outcome = guarded_apply(guarded)
    run_id = outcome.run.run_id
    guarded.agent_server.shutdown()
    guarded.agent_server.server_close()

    assert _restart(guarded) == []
    assert _guard_row(guarded, run_id).settled is False
    assert guarded.runs.get(run_id).state == str(RunState.GUARDED)


# -------------------------------------------------------------------- recovery integration


def test_a_guarded_hold_that_ends_badly_uses_the_recovery_engine_that_already_exists(
    guarded: Estate,
):
    """No second recovery system. The hold, the codes and both ways out are the existing ones.

    A guard that could not put the value back leaves exactly the situation recovery
    built for: an object held, a Change that goes on saying what happened, and a person
    who can retry the end state forward or release the hold.
    """
    outcome = guarded_apply(guarded)
    run = guarded.runs.get(outcome.run.run_id)
    guarded.write("eth0", "mtu", "9000")
    guarded.kernel.links[guarded.ifindex_of("eth0")] = ("eth0", 9000)
    guarded.timers.fire()
    settled = guarded.changes.guard_keep(run, _proof_over_target(guarded), acknowledge=True)
    change = settled.change

    hold = guarded.changes.recovery_hold(change)
    assert hold.unresolved is True
    assert hold.object_write_locked is True

    resolved = guarded.changes.recovery_resolve(
        change, acknowledge=True, operator_statement="eth0", object_name="eth0", note=None,
        expected_recovery_reason=str(RecoveryReason.GUARD_REVERSAL_UNPROVEN))
    assert resolved.hold.state == "resolved"
    assert guarded.changes.lock_for(run.run_id) is None
    # The Change is untouched by the resolution, exactly as it always was.
    after = guarded.changes.get_change(change.change_id)
    assert after.result == str(ChangeResult.RECOVERY_REQUIRED)
    assert after.recovery_reason == str(RecoveryReason.GUARD_REVERSAL_UNPROVEN)


def test_the_original_change_is_never_rewritten_by_anything_the_guard_does(guarded: Estate):
    """What a Change was about is immutable, and a guard settling later does not touch it.

    The guard's own record is where a guard's history lives. A settlement collected after
    the Change finished changes the guard row and nothing else.
    """
    outcome = guarded_apply(guarded)
    run_id = outcome.run.run_id
    change = guarded.changes.change_for_run(run_id)
    identity = {k: change.__dict__[k] for k in (
        "change_id", "run_id", "preview_id", "checkpoint_id", "host_id", "object_id",
        "operation", "field", "value_type", "before_value", "desired_value", "created_at",
        "apply_attempt_id")}

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        guarded.database.connection.execute(
            "UPDATE changes SET before_value = 1 WHERE change_id = ?", (change.change_id,))

    guarded.timers.fire()
    guarded.changes.guard_keep(
        guarded.runs.get(run_id), _proof_over_target(guarded), acknowledge=True)
    after = guarded.changes.get_change(change.change_id)
    assert {k: after.__dict__[k] for k in identity} == identity


# --------------------------------------------------------------- the store refuses a lie


def _guard_columns(estate: Estate, run_id: str) -> dict[str, Any]:
    row = estate.database.query_one("SELECT * FROM run_guards WHERE run_id = ?", (run_id,))
    return dict(row)


def test_the_store_refuses_every_impossible_guard_record(guarded: Estate):
    """Written as SQL rather than through the service, because a CHECK is what is claimed.

    Each of these is a durable statement that contradicts itself. A build that decided
    otherwise, a bug in the settlement code, or SQL typed into a shell all fail on the same
    constraint — which is the difference between a rule and a code path nobody took.
    """
    outcome = guarded_apply(guarded)
    run_id = outcome.run.run_id
    guard_id = _guard_row(guarded, run_id).guard_id

    impossible = [
        # A deadline with nobody holding it, and a holder with no deadline.
        ("armed_at = ?, expires_at = NULL, holder_id = 'a'", (_NOW,)),
        ("armed_at = ?, expires_at = ?, holder_id = NULL", (_NOW, _NOW)),
        # Settled without saying how, and a phase that is not a settlement.
        ("settled_at = ?", (_NOW,)),
        ("settled_phase = 'disarmed'", ()),
        ("settled_at = ?, settled_phase = 'still_going'", (_NOW,)),
        # Fired without a reversal, and a reversal without having fired.
        ("settled_at = ?, settled_phase = 'fired', fired_at = ?", (_NOW, _NOW)),
        ("settled_at = ?, settled_phase = 'fired', reversal_outcome = 'written'", (_NOW,)),
        ("settled_at = ?, settled_phase = 'disarmed', fired_at = ?", (_NOW, _NOW)),
        ("settled_at = ?, settled_phase = 'disarmed', reversal_outcome = 'written'", (_NOW,)),
        # A reversal outcome this vocabulary does not have.
        ("settled_at = ?, settled_phase = 'fired', fired_at = ?, reversal_outcome = 'ok'",
         (_NOW, _NOW)),
        # Kept without the proof that kept it, and proof without a release.
        ("kept_at = ?", (_NOW,)),
        ("kept_at = ?, kept_evidence_id = ?, settled_at = ?, settled_phase = 'fired', "
         "fired_at = ?, reversal_outcome = 'written'",
         (_NOW, _path_evidence(guarded), _NOW, _NOW)),
    ]
    for assignment, params in impossible:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK|constraint"):
            with guarded.database.transaction():
                guarded.database.connection.execute(
                    f"UPDATE run_guards SET {assignment} WHERE guard_id = ?",
                    (*params, guard_id))

    # And a window of nothing, which the store refuses on the way in rather than on the way
    # past: `window_s` is one of the columns that say what a guard was armed *for*, so the
    # immutability trigger stops it before the CHECK is reached.
    columns = _guard_columns(guarded, run_id)
    columns["guard_id"] = f"grd_{uuid.uuid4().hex}"
    columns["run_id"] = run_id
    columns["window_s"] = 0
    with pytest.raises(sqlite3.IntegrityError):
        with guarded.database.transaction():
            guarded.database.connection.execute(
                f"INSERT INTO run_guards ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})", tuple(columns.values()))


def test_a_guard_may_only_exist_for_a_target_proven_to_be_the_management_path(
    guarded: Estate,
):
    """One value, and it is the positive proof.

    Not `unknown`, which would be a mechanism armed against a hazard nobody established,
    and not `not_on_management_path`, which would be one armed against no hazard at all.
    """
    outcome = guarded_apply(guarded)
    columns = _guard_columns(guarded, outcome.run.run_id)
    for relation in ("unknown", "not_on_management_path"):
        columns["guard_id"] = f"grd_{uuid.uuid4().hex}"
        columns["run_id"] = outcome.run.run_id
        columns["protection_management_path"] = relation
        with pytest.raises(sqlite3.IntegrityError):
            with guarded.database.transaction():
                guarded.database.connection.execute(
                    f"INSERT INTO run_guards ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)})", tuple(columns.values()))


def test_a_guard_requires_this_runs_own_typed_apply_confirmation(guarded: Estate):
    """Four forgeries, each closed by the same trigger.

    Authority borrowed from another Run; a recovery grant standing in for an apply one; a
    mere acknowledgement where the policy demanded a typed statement; and a confirmation
    naming a different plan than the one this guard's Run published.
    """
    outcome = guarded_apply(guarded)
    columns = _guard_columns(guarded, outcome.run.run_id)

    # A second Run, on a second object, with an ordinary `acknowledge` confirmation.
    guarded.adopt("eth1")
    guarded.drift("eth1", "1400")
    guarded.prove_path_on("eth0")  # eth1 is not the path, so its plan is the ordinary kind
    other = guarded.plan("eth1")
    guarded.confirm(other.run)
    other_confirmation = guarded.changes.confirmation_for(other.run.run_id)

    for label, mutation in (
        ("another run's authority", {"confirmation_id": other_confirmation.confirmation_id}),
        ("no authority at all", {"confirmation_id": "cnf_does_not_exist"}),
    ):
        forged = dict(columns)
        forged["guard_id"] = f"grd_{uuid.uuid4().hex}"
        forged.update(mutation)
        with pytest.raises(sqlite3.IntegrityError):
            with guarded.database.transaction():
                guarded.database.connection.execute(
                    f"INSERT INTO run_guards ({', '.join(forged)}) "
                    f"VALUES ({', '.join('?' for _ in forged)})", tuple(forged.values()))

    # And an acknowledgement, on this very Run, is not the typed statement the policy asked
    # for. Written directly, because the service refuses to record one for a guarded plan.
    weak_run = guarded.runs.get(other.run.run_id)
    forged = dict(columns)
    forged["guard_id"] = f"grd_{uuid.uuid4().hex}"
    forged["run_id"] = weak_run.run_id
    forged["preview_id"] = weak_run.preview.preview_id
    forged["confirmation_id"] = other_confirmation.confirmation_id
    with pytest.raises(sqlite3.IntegrityError):
        with guarded.database.transaction():
            guarded.database.connection.execute(
                f"INSERT INTO run_guards ({', '.join(forged)}) "
                f"VALUES ({', '.join('?' for _ in forged)})", tuple(forged.values()))


def test_a_guard_may_cite_only_evidence_about_its_own_host(guarded: Estate):
    """A foreign key says an observation exists. It does not say it is about this host.

    Evidence about another host is not weaker proof that this target carries the path — it
    is none at all, and it would be the most convincing-looking falsehood the record could
    hold, because a kept guard means precisely that a reading established the connection.
    """
    outcome = guarded_apply(guarded)
    guard_id = _guard_row(guarded, outcome.run.run_id).guard_id

    with guarded.database.transaction():
        guarded.database.connection.execute(
            "INSERT INTO hosts VALUES ('other','mid','high',NULL,NULL,NULL,NULL,NULL,NULL,"
            "NULL,NULL,NULL,'[]',?,?)", (_NOW, _NOW))
        foreign = f"mpo_{uuid.uuid4().hex}"
        ManagementPathRepository(guarded.database).insert({
            "observation_id": foreign, "host_id": "other", "observed_at": _NOW,
            "agent_instance_id": None,
            "transport_peer_address": "10.9.9.9", "transport_peer_family": "inet",
            "local_endpoint_address": "10.9.9.1", "local_endpoint_family": "inet",
            "capability": "network.route.observe", "provider": "linux.route",
            "provider_version": "1", "method": "netlink_rtm_getroute",
            "route_status": "resolved", "route_reason": None, "route_family": "inet",
            "route_destination": "10.9.9.9", "route_destination_prefix_length": 32,
            "route_preferred_source": "10.9.9.1", "route_gateway": None,
            "route_oif_index": 2, "route_table": 254, "route_type": "unicast",
            "route_scope": "universe", "route_protocol": "unspec", "route_priority": None,
            "route_error": None,
        })

    with pytest.raises(sqlite3.IntegrityError, match="own host"):
        with guarded.database.transaction():
            guarded.database.connection.execute(
                "UPDATE run_guards SET kept_at = ?, kept_evidence_id = ?, settled_at = ?, "
                "settled_phase = 'disarmed' WHERE guard_id = ?",
                (_NOW, foreign, _NOW, guard_id))


def test_what_a_guard_was_armed_for_is_immutable_while_it_is_still_holding(guarded: Estate):
    """Only what became of it moves — the same division `changes` makes, for the reason."""
    outcome = guarded_apply(guarded)
    guard_id = _guard_row(guarded, outcome.run.run_id).guard_id
    for column, value in (("window_s", 9), ("reversal_attempt_id", "rev_other"),
                          ("checkpoint_id", "ckp_other"),
                          ("protection_management_path", "unknown"),
                          ("confirmation_id", "cnf_other"), ("arm_began_at", _NOW)):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            guarded.database.connection.execute(
                f"UPDATE run_guards SET {column} = ? WHERE guard_id = ?", (value, guard_id))


def test_a_settled_guard_never_moves_again_and_is_never_deleted(guarded: Estate):
    outcome = guarded_apply(guarded)
    run = guarded.runs.get(outcome.run.run_id)
    guarded.changes.guard_keep(run, _proof_over_target(guarded), acknowledge=True)
    guard_id = _guard_row(guarded, run.run_id).guard_id

    with pytest.raises(sqlite3.IntegrityError, match="settles once"):
        guarded.database.connection.execute(
            "UPDATE run_guards SET settled_reason = 'x' WHERE guard_id = ?", (guard_id,))
    with pytest.raises(sqlite3.IntegrityError, match="record of what protected"):
        guarded.database.connection.execute(
            "DELETE FROM run_guards WHERE guard_id = ?", (guard_id,))


def test_one_guard_per_run_and_one_per_checkpoint(guarded: Estate):
    """A second guard for one change would be a second reversal nobody accounted for."""
    outcome = guarded_apply(guarded)
    columns = _guard_columns(guarded, outcome.run.run_id)
    columns["guard_id"] = f"grd_{uuid.uuid4().hex}"
    columns["reversal_attempt_id"] = f"rev_{uuid.uuid4().hex}"
    with pytest.raises(sqlite3.IntegrityError):
        with guarded.database.transaction():
            guarded.database.connection.execute(
                f"INSERT INTO run_guards ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})", tuple(columns.values()))


# ------------------------------------------------------ what the host side will not accept


def test_the_agent_refuses_a_window_it_will_not_hold(guarded: Estate):
    """Refused rather than clamped. A guard silently narrowed or widened is a guard whose
    behaviour the backend's durable record no longer describes."""
    from localplane.agent.guard import MAX_WINDOW_S, MIN_WINDOW_S
    from localplane.backend.agent_client import AgentError

    for window in (0, MIN_WINDOW_S - 1, MAX_WINDOW_S + 1):
        with pytest.raises(AgentError) as refusal:
            guarded.client.arm_mtu_guard(
                guard_id=f"grd_{uuid.uuid4().hex}", attempt_id="rev_x", ifindex=2,
                guarded_mtu=1500, restore_mtu=1400, window_s=window)
        assert refusal.value.code == "invalid_params"


def test_the_agent_refuses_a_reversal_that_would_change_nothing(guarded: Estate):
    """Mirrors the privileged helper's own precondition, one layer up."""
    from localplane.backend.agent_client import AgentError

    with pytest.raises(AgentError):
        guarded.client.arm_mtu_guard(
            guard_id=f"grd_{uuid.uuid4().hex}", attempt_id="rev_x", ifindex=2,
            guarded_mtu=1500, restore_mtu=1500, window_s=60)


def test_the_agent_refuses_a_second_guard_for_a_link_it_is_already_guarding(guarded: Estate):
    """Two timers writing one link is a mechanism arguing with itself."""
    from localplane.backend.agent_client import AgentError

    guarded.client.arm_mtu_guard(
        guard_id="grd_one", attempt_id="rev_one", ifindex=2,
        guarded_mtu=1500, restore_mtu=1400, window_s=60)
    with pytest.raises(AgentError):
        guarded.client.arm_mtu_guard(
            guard_id="grd_two", attempt_id="rev_two", ifindex=2,
            guarded_mtu=1500, restore_mtu=1400, window_s=60)
    guarded.client.disarm_mtu_guard("grd_one")


def test_asking_about_a_guard_never_releases_it(guarded: Estate):
    """The two methods do two things, and one of them must never do the other's."""
    guarded.client.arm_mtu_guard(
        guard_id="grd_read", attempt_id="rev_read", ifindex=2,
        guarded_mtu=1500, restore_mtu=1400, window_s=60)
    for _ in range(3):
        assert guarded.client.mtu_guard_status("grd_read")["guard"]["phase"] == "armed"
    assert guarded.client.disarm_mtu_guard("grd_read")["guard"]["phase"] == "disarmed"
    assert guarded.client.mtu_guard_status("grd_read")["guard"]["phase"] == "disarmed"


def test_the_guard_methods_take_nothing_but_the_parameters_they_declare(guarded: Estate):
    """No path, no command, no argv, no probe target, no peer, no second channel.

    Sent at the protocol rather than reasoned about: the agent refuses an unknown key
    before it reaches a handler, which is what makes "a caller cannot name a backup path"
    a property of the surface rather than of the code behind it.
    """
    from localplane.backend.agent_client import AgentError
    from localplane.protocol.wire import Method

    for key, value in (
        ("command", "ip link set eth0 mtu 1400"), ("argv", ["ip"]), ("shell", True),
        ("interface", "eth1"), ("backup_interface", "eth1"), ("second_path", "tailscale0"),
        ("peer", "192.0.2.1"), ("probe", "192.0.2.1"), ("route", "default"),
        ("gateway", "192.0.2.1"), ("script", "x"), ("watchdog", "x"), ("rollback", "x"),
        ("object_id", "obj_x"), ("deadline", "2026-01-01"), ("timeout_action", "revert"),
    ):
        with pytest.raises(AgentError) as refusal:
            guarded.client.call(Method.NETWORK_ARM_MTU_GUARD, {
                "guard_id": "g", "attempt_id": "a", "ifindex": 2, "guarded_mtu": 1500,
                "restore_mtu": 1400, "window_s": 60, key: value})
        assert refusal.value.code == "unknown_field", key
        with pytest.raises(AgentError) as read_refusal:
            guarded.client.call(Method.NETWORK_MTU_GUARD_STATUS, {"guard_id": "g", key: value})
        assert read_refusal.value.code == "unknown_field", key


# ------------------------------------------------------------------- the generic half stays


@pytest.mark.parametrize(
    "module",
    ["backend/domain/guard.py", "backend/domain/policy.py", "backend/domain/protection.py",
     "backend/domain/changes.py", "backend/changes.py", "backend/runs.py"],
)
@pytest.mark.parametrize(
    "forbidden",
    ["reconcile_mtu", "mtu", "interface", "link", "ifindex", "netlink", "sysfs", "ethernet",
     "docker", "container"],
)
def test_the_generic_guard_path_names_nothing_it_is_guarding(module: str, forbidden: str):
    """Read out of the source, with comments and docstrings stripped, because that is the claim.

    A connection guard is a protection concept. ``network.interface.reconcile_mtu`` is
    merely the one concrete operation in this build that can exercise it, and an engine that
    branched on that name would be an engine that knows what it is changing — which is
    exactly the coupling every seam in this product exists to prevent.

    The stripping is the same one the Change Engine's own purity test uses: prose explains a
    seam by naming what is on the other side of it, and the code may not.
    """
    source = (Path("src/localplane") / module).read_text()
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    without_docstrings = "".join(
        part for i, part in enumerate(code.split('"""')) if i % 2 == 0
    )
    assert forbidden not in without_docstrings.lower(), f"{module} mentions {forbidden!r}"


def test_the_generic_guard_path_imports_no_operation_module():
    source = Path("src/localplane/backend/changes.py").read_text()
    assert "backend.operations" not in source
    assert "backend.container_operations" not in source
    assert "arm_mtu_guard" not in source
    assert "disarm_mtu_guard" not in source


def test_there_is_still_no_generic_execution_surface(guarded: Estate):
    """Nothing was widened to make a guard fit.

    The two host-write surfaces are the two that were there. `guard/keep` is a third
    non-`GET` on the run resource and it can only prevent a write — and none of the shapes
    a guard could have taken as an escape hatch exists: no arm endpoint, no disarm endpoint,
    no watchdog, no probe, no second-path selector.
    """
    from fastapi.testclient import TestClient

    from localplane.backend.app import create_app
    from localplane.backend.config import Settings

    run_id = guarded_apply(guarded).run.run_id
    settings = Settings(database_path=Path(guarded.database.path),
                        agent_socket=guarded.agent_server.socket_path,
                        agent_timeout_s=10.0, freshness_ttl_s=60.0, log_level="WARNING",
                        observe_on_startup=False)
    with TestClient(create_app(settings, guarded.database)) as client:
        for path in (
            "/api/v1/execute",
            "/api/v1/guards",
            "/api/v1/guard",
            "/api/v1/watchdog",
            "/api/v1/probe",
            "/api/v1/paths",
            f"/api/v1/runs/{run_id}/arm",
            f"/api/v1/runs/{run_id}/guard",
            f"/api/v1/runs/{run_id}/guard/arm",
            f"/api/v1/runs/{run_id}/guard/disarm",
            f"/api/v1/runs/{run_id}/guard/extend",
            f"/api/v1/runs/{run_id}/guard/rollback",
            f"/api/v1/runs/{run_id}/rollback",
            f"/api/v1/runs/{run_id}/execute",
        ):
            assert client.post(path, json={}).status_code == 404, path
            assert client.get(path).status_code in (404, 405), path


# ------------------------------------------------------------------------ the upgrade


def _schema(database: Any) -> list[tuple[str, str]]:
    return [
        (row["name"], " ".join((row["sql"] or "").split()))
        for row in database.query(
            "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name"
        )
    ]


def test_a_pre_0010_store_upgrades_without_losing_or_inventing_anything(
    tmp_path: Path,
):
    """A pre-0010 store upgrades through 0010, 0011 and 0012 without losing any row.

    The store this migration will actually meet is one built when recovery was added, so it is
    built that way here rather than asserted about in the abstract: nine migrations, a
    published plan and a Run in it, and then the three later migrations applied on top.
    """
    import shutil
    import subprocess

    from localplane.backend.db.database import MIGRATIONS_DIR, open_database
    from tests.test_runs import _seed_a_published_plan

    staged = tmp_path / "migrations"
    staged.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(path.name.split("_", 1)[0]) <= 9:
            shutil.copy(path, staged / path.name)

    old = open_database(tmp_path / "staged.db", staged)
    with old.transaction():
        _seed_a_published_plan(old)
        old.connection.execute(
            "INSERT INTO run_confirmations (confirmation_id, run_id, purpose, preview_id, "
            "preview_digest, digest_version, required_method, method, policy, source, "
            "satisfied_at) VALUES ('cnf_old','run_legacy','apply','prv_legacy','sha256:legacy',"
            "1,'acknowledge','acknowledge','pol','unauthenticated_request','t')")
    before = {v: c for v, c in old.query("SELECT version, checksum FROM schema_migrations")}
    old.close()

    for name in (
        "0010_connection_guard.sql",
        "0011_observation_scope.sql",
        "0012_systemd_lifecycle.sql",
        "0013_self_impact.sql",
        "0014_self_impact_override.sql",
        "0015_systemd_lifecycle_changes.sql",
    ):
        shutil.copy(MIGRATIONS_DIR / name, staged / name)
    upgraded = open_database(tmp_path / "staged.db", staged)
    fresh = open_database(tmp_path / "fresh.db")
    try:
        after = {v: c for v, c in upgraded.query("SELECT version, checksum FROM schema_migrations")}
        assert sorted(after) == list(range(1, 16))
        assert {v: after[v] for v in before} == before  # nothing earlier was rewritten

        preview = dict(upgraded.query("SELECT * FROM run_previews")[0])
        assert preview["preview_id"] == "prv_legacy"
        assert preview["preview_digest"] == "sha256:legacy"
        assert preview["current_value"] == 1400 and preview["desired_value"] == 1500
        # Restated, not decided: no build before this one could arm a guard, so
        # `unavailable` with `guard_not_required` is exactly what that plan said.
        assert preview["guard_availability"] == "unavailable"
        assert preview["guard_reason"] == "guard_not_required"
        assert preview["guard_armed"] == 0
        assert preview["execution_eligibility"] == "blocked"

        confirmation = dict(upgraded.query("SELECT * FROM run_confirmations")[0])
        assert confirmation["method"] == "acknowledge"
        assert confirmation["typed_statement"] is None  # it was never typed

        assert [tuple(r) for r in upgraded.query("SELECT run_id, state FROM runs")] == [
            ("run_legacy", "preview")]
        assert upgraded.query("SELECT * FROM run_guards") == []
        assert upgraded.query("PRAGMA foreign_key_check") == []
        assert upgraded.query("PRAGMA integrity_check")[0][0] == "ok"
        # And an upgraded store is the same schema as one built fresh through all twelve.
        assert _schema(upgraded) == _schema(fresh)
    finally:
        upgraded.close()
        fresh.close()


def test_the_migrations_through_0009_are_byte_identical_to_head():
    """0001–0009 are what HEAD holds. Asserted against git, not against a checksum."""
    import subprocess

    from localplane.backend.db.database import MIGRATIONS_DIR

    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(path.stem.split("_")[0]) > 9:
            continue
        committed = subprocess.run(
            ["git", "show", f"HEAD:src/localplane/backend/db/migrations/{path.name}"],
            cwd=str(Path(__file__).resolve().parents[1]), capture_output=True)
        if committed.returncode != 0:
            pytest.skip("not a git checkout")
        assert path.read_bytes() == committed.stdout, f"{path.name} changed on disk"


def test_a_crash_between_releasing_a_guard_and_recording_why_settles_conservatively(
    guarded: Estate,
):
    """The one-transaction crash window in keeping a guarded change, and it is named.

    The holder was told to stand down and the process died before the evidence that
    justified it reached the store. Nothing was reverted and the operation's own result was
    proved before the hold began — so this is not a failure. What it is, is a proof
    LocalPlane cannot produce, and `succeeded` would be producing it anyway.
    """
    outcome = guarded_apply(guarded)
    run_id = outcome.run.run_id
    guard = _guard_row(guarded, run_id)

    # Exactly what `guard_keep` does, and then the process dies.
    report = guarded.changes._executor(
        guarded.runs.get(run_id)).disarm_guard(guard.guard_id)
    assert report.phase is GuardPhase.DISARMED
    assert guarded.timers.live == []
    assert _guard_row(guarded, run_id).settled is False

    settled = _restart(guarded)

    assert len(settled) == 1
    assert settled[0].settled_phase == str(GuardPhase.DISARMED)
    assert settled[0].kept_at is None
    run = guarded.runs.get(run_id)
    assert run.state == str(RunState.RECOVERY_REQUIRED)
    change = guarded.changes.change_for_run(run_id)
    assert change.recovery_reason == str(RecoveryReason.GUARD_RELEASED_WITHOUT_RECORDED_PROOF)
    # The value is still on the host — nothing reverted it — and the hold is kept.
    assert guarded.mtu_of("eth0") == 1500
    assert guarded.changes.lock_for(run_id) is not None


def test_a_restart_writes_nothing_to_the_host_on_any_path(guarded: Estate):
    """The rule that survives every build, asserted against the kernel's own frame count.

    A backend start settles guards, settles interrupted Changes and settles interrupted
    recoveries, and between the three of them not one netlink frame is put on a socket. The
    count is the simulated kernel's own record of what reached it, not anything LocalPlane
    recorded about itself — and it is taken across a hold, a lapsed guard and a hold whose
    Change is in recovery, which are the three states a restart actually meets.
    """
    held = guarded_apply(guarded).run.run_id
    frames = len(guarded.kernel.mutations)

    # 1 — a guard still holding.
    assert _restart(guarded) == []
    assert len(guarded.kernel.mutations) == frames

    # 2 — the deadline expires. That write is the guard's, not a restart's.
    guarded.timers.fire()
    assert len(guarded.kernel.mutations) == frames + 1
    fired = len(guarded.kernel.mutations)

    # 3 — the restart that collects it, and every one after.
    for _ in range(3):
        _restart(guarded)
        assert len(guarded.kernel.mutations) == fired

    assert guarded.runs.get(held).state == str(RunState.ROLLED_BACK)
    assert guarded.mtu_of("eth0") == 1400
