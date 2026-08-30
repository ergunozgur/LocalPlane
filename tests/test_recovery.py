"""Recovery completion: retrying an unproven change, and resolving a hold by hand.

Until recovery completion existed, a Change that ended ``recovery_required`` held its
object's write lock for ever and the only way out was a human editing the database. There
are two ways out, and this file is where both are held to what they may claim.

The claims, and the file is organised around them:

* **history is not rewritten** — the original Change goes on saying ``recovery_required``,
  goes on saying which mutation outcome produced it and goes on saying why, whatever an
  operator does about it afterwards. A recovery action is a *later* event with its own
  append-only row;
* **a retry looks before it writes** — a fresh reading through the ordinary observation path,
  judged by the operation's own proof semantics, and a retry that finds the end state already
  reached completes with no host mutation at all;
* **a retry that writes is a new write attempt** — fresh evidence, fresh gates, and fresh
  operator authority. The confirmation the original apply consumed authorises nothing here;
* **a resolution claims nothing** — no mutation, no verification, no rollback, and the store
  refuses a resolution row that carries any of them;
* **the hold is released once, explicitly, by whoever owns it, and never by accident.**

Both mutation shapes are exercised through the *same* engine: a field change
(``network.interface.reconcile_mtu``, against a simulated kernel behind a real privileged
helper) and an action (``docker.container.*``, against a Docker daemon that really moves).
There is one recovery engine and it learns nothing about either.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Iterator

import pytest

from localplane.backend.db.database import MIGRATIONS_DIR
from localplane.backend.domain.changes import (
    CHANGE_KIND_ACTION,
    ChangeResult,
    HostEffect,
    MutationOutcome,
    RecoveryActionKind,
    RecoveryAttemptOutcome,
    RecoveryHoldState,
    RecoveryReason,
    RunEvent,
    VerificationOutcome,
    recovery_outcome_for,
)
from localplane.backend.domain.protection import ManagementPathVerdict
from localplane.backend.domain.runs import OperationType, RunState
from localplane.backend.runs import ObservationAttempt, RunRefused
from localplane.helper.mtu import NetlinkFailure

from tests.conftest import netlink_ack
from tests.test_docker import Estate as ContainerEstate, _ID, _OTHER
from tests.test_docker import daemon  # noqa: F401 - a fixture this module depends on
from tests.test_docker import estate as _container_estate
from tests.test_write_boundary import Estate as LinkEstate, RECONCILE, _sequence
from tests.test_write_boundary import estate as _link_estate
from tests.test_write_boundary import ready as _link_ready

_NOW = "2026-08-23T12:00:00.000000+00:00"


# ------------------------------------------------------------------------------ fixtures
#
# Both estates already exist — one per mutation shape — and recovery has to work against both
# through one engine, so this file borrows them rather than growing a third. They are wrapped
# under distinct names because a module may only bind one fixture called `estate`.


def _raw(fixture: Any) -> Any:
    """The function inside a fixture, so this module can bind it under another name.

    ``pytest.fixture`` wraps with ``functools.wraps`` and refuses a direct call, which is the
    right default and exactly what has to be stepped around here: a module may only bind one
    fixture called ``estate`` and this file needs both.
    """
    return getattr(fixture, "__wrapped__", fixture)


@pytest.fixture
def links(database, fake_root: Path, sysfs_net: Path, tmp_path: Path) -> Iterator[LinkEstate]:
    yield from _raw(_link_estate)(database, fake_root, sysfs_net, tmp_path)


@pytest.fixture
def ready(links: LinkEstate) -> LinkEstate:
    return _raw(_link_ready)(links)


@pytest.fixture
def containers(
    database, fake_root: Path, sysfs_net: Path, tmp_path: Path, daemon  # noqa: F811
) -> Iterator[ContainerEstate]:
    yield from _raw(_container_estate)(database, fake_root, sysfs_net, tmp_path, daemon)


# --------------------------------------------------------------------- getting into recovery


def into_recovery(ready: LinkEstate, name: str = "eth0") -> Any:
    """An MTU write that lands, a verification that fails, and a restoration that does not.

    The most awkward of the truthful endings and the one worth building on: LocalPlane wrote,
    could not prove the value it wanted, tried to put the previous one back, and the
    restoration was acknowledged without moving anything. Nothing about the final state is
    established, so the object is held.
    """
    run = ready.plan(name).run
    ready.confirm(run)
    ifindex = ready.ifindex_of(name)
    calls = {"n": 0}
    original = ready.kernel.mutate

    def sabotaged(frame: bytes) -> bytes:
        calls["n"] += 1
        if calls["n"] == 1:
            reply = original(frame)
            # A third writer moves it the instant ours lands: the verification will fail.
            ready.kernel.links[ifindex] = (name, 9000)
            ready.write(name, "mtu", "9000")
            return reply
        return netlink_ack(_sequence(frame), errno=0)  # acknowledged, nothing moves

    ready.kernel.mutate = sabotaged  # type: ignore[method-assign]
    try:
        outcome = ready.apply(run)
    finally:
        ready.kernel.mutate = original  # type: ignore[method-assign]
    assert outcome.change.result == str(ChangeResult.RECOVERY_REQUIRED)
    assert ready.changes.lock_for(run.run_id) is not None
    return outcome.change


def container_into_recovery(
    containers: ContainerEstate, operation: OperationType = OperationType.DOCKER_CONTAINER_START,
    name: str = "web", **daemon_state: Any,
) -> Any:
    """A lifecycle action the daemon began and could not account for. No rollback exists."""
    for attribute, value in daemon_state.items():
        setattr(containers.daemon, attribute, value)
    if not daemon_state:
        containers.daemon.lifecycle_status = {str(operation).rsplit(".", 1)[-1]: 500}
    outcome = containers.apply(operation, name)
    containers.daemon.lifecycle_status = {}
    containers.daemon.freeze_started_at = False
    assert outcome.change.result == str(ChangeResult.RECOVERY_REQUIRED)
    assert containers.changes.lock_for(outcome.run.run_id) is not None
    return outcome.change


def authorise(estate: Any, change: Any) -> Any:
    return estate.changes.recovery_confirm(
        change, acknowledge=True, expected_recovery_reason=None,
        management_path=estate.management_path)


def events(estate: Any, change: Any) -> list[str]:
    return [e.event for e in estate.changes.transcript(change.run_id)]


def frozen(change: Any) -> dict[str, Any]:
    """Everything the original Change says. Compared before and after, byte for byte."""
    return {
        key: value for key, value in vars(change).items()
    }


# ------------------------------------------------------------- what recovery may act on at all


def test_retry_is_refused_unless_the_change_actually_ended_in_recovery(ready: LinkEstate):
    """A Change that succeeded has nothing to recover, and saying so is not a technicality.

    Offering a retry over a Change that ended cleanly would be offering to write to a host
    for no reason anybody recorded.
    """
    run = ready.plan("eth0").run
    ready.confirm(run)
    outcome = ready.apply(run)
    assert outcome.change.result == str(ChangeResult.SUCCEEDED)

    for act in (
        lambda: ready.changes.recovery_retry(outcome.change, ready.management_path),
        lambda: ready.changes.recovery_confirm(
            outcome.change, acknowledge=True, expected_recovery_reason=None,
            management_path=ready.management_path),
        lambda: ready.changes.recovery_resolve(
            outcome.change, acknowledge=True, operator_statement="eth0", object_name="eth0",
            note=None, expected_recovery_reason=None),
    ):
        with pytest.raises(RunRefused) as raised:
            act()
        assert raised.value.code == "change_not_in_recovery"
    assert ready.database.query("SELECT * FROM change_recovery_attempts") == []


def test_resolve_is_refused_when_the_change_no_longer_owns_the_hold(ready: LinkEstate):
    """A hold somebody removed outside this path is not one LocalPlane will pretend to own.

    The lock *is* the hold. If it is gone the honest answer is that this Change is not holding
    anything, not that releasing it again succeeded.
    """
    change = into_recovery(ready)
    with ready.database.transaction():
        ready.changes.locks.release(change.run_id)

    for act in (
        lambda: ready.changes.recovery_resolve(
            change, acknowledge=True, operator_statement="eth0", object_name="eth0",
            note=None, expected_recovery_reason=None),
        lambda: ready.changes.recovery_retry(change, ready.management_path),
    ):
        with pytest.raises(RunRefused) as raised:
            act()
        assert raised.value.code == "recovery_hold_not_held"
    assert ready.database.query("SELECT * FROM change_recovery_attempts") == []


def test_a_resolution_must_be_acknowledged_and_the_object_named(ready: LinkEstate):
    """The operator types the name of the thing being held. Here it is the object's.

    An escape from a safety hold should not be one accidental click, and the statement is
    recorded rather than merely required.
    """
    change = into_recovery(ready)
    with pytest.raises(RunRefused) as raised:
        ready.changes.recovery_resolve(
            change, acknowledge=False, operator_statement="eth0", object_name="eth0",
            note=None, expected_recovery_reason=None)
    assert raised.value.code == "recovery_resolution_not_acknowledged"

    with pytest.raises(RunRefused) as raised:
        ready.changes.recovery_resolve(
            change, acknowledge=True, operator_statement="eth1", object_name="eth0",
            note=None, expected_recovery_reason=None)
    assert raised.value.code == "recovery_resolution_object_mismatch"

    assert ready.database.query("SELECT * FROM change_recovery_attempts") == []
    assert ready.changes.lock_for(change.run_id) is not None


# --------------------------------------------------------------- proving instead of writing


def test_a_fresh_observation_can_settle_a_recovery_without_a_second_write(ready: LinkEstate):
    """The first thing a retry does is look, and looking is often the whole answer.

    The uncertain write may in fact have landed, or a third writer may have put the value
    right. Either way the end state the Change wanted is now established by evidence, and
    writing again because somebody clicked Retry would be a host mutation nobody needed.
    """
    change = into_recovery(ready)
    before = frozen(change)
    mutations_before = list(ready.kernel.mutations)

    # The world moves back to the value the intent holds, by somebody else's hand.
    ready.write("eth0", "mtu", "1500")
    ready.kernel.links[2] = ("eth0", 1500)

    result = ready.changes.recovery_retry(change, ready.management_path)

    assert result.attempt.kind == str(RecoveryActionKind.RETRY)
    assert result.attempt.outcome == str(RecoveryAttemptOutcome.PROVEN)
    assert result.attempt.evidence_outcome == str(VerificationOutcome.VERIFIED)
    assert result.attempt.evidence_observed_value == 1500
    assert result.attempt.evidence_observation_id is not None
    # Nothing was dispatched and nothing could have been.
    assert result.attempt.mutation_attempt_id is None
    assert result.attempt.mutation_outcome is None
    assert result.attempt.confirmation_id is None
    assert result.attempt.host_effect == str(HostEffect.NONE)
    assert ready.kernel.mutations == mutations_before

    # The hold is given back, and the Change still says exactly what happened to it.
    assert result.hold.state is RecoveryHoldState.RESOLVED
    assert result.hold.object_write_locked is False
    assert result.hold.released_by == str(RecoveryActionKind.RETRY)
    assert ready.changes.lock_for(change.run_id) is None
    assert frozen(result.change) == before

    assert events(ready, change)[-4:] == [
        str(RunEvent.RECOVERY_RETRY_STARTED), str(RunEvent.RECOVERY_EVIDENCE_RESULT),
        str(RunEvent.RECOVERY_RETRY_FINISHED), str(RunEvent.RECOVERY_HOLD_RELEASED)]


def test_a_retry_that_proves_the_end_state_needs_no_confirmation(ready: LinkEstate):
    """Authority is for writing. A retry that writes nothing consumes none and needs none."""
    change = into_recovery(ready)
    ready.write("eth0", "mtu", "1500")
    ready.kernel.links[2] = ("eth0", 1500)
    result = ready.changes.recovery_retry(change, ready.management_path)
    assert result.attempt.outcome == str(RecoveryAttemptOutcome.PROVEN)
    assert ready.database.query(
        "SELECT * FROM run_confirmations WHERE purpose = 'recovery_retry'") == []


# ------------------------------------------------------------------ a retry that must write


def test_a_retry_that_must_write_will_not_reuse_the_confirmation_that_authorised_the_apply(
    ready: LinkEstate,
):
    """The confirmation the original attempt consumed authorises nothing here.

    A confirmation authorises *an attempt*; that attempt happened. A retry is a second write
    attempt and needs authority nobody has spent — and the refusal names what to do, rather
    than quietly proceeding on an old grant.
    """
    change = into_recovery(ready)
    applied = ready.changes.confirmation_for(change.run_id)
    assert applied.consumed is True

    with pytest.raises(RunRefused) as raised:
        ready.changes.recovery_retry(change, ready.management_path)
    assert raised.value.code == "recovery_confirmation_required"
    assert raised.value.detail["object_write_locked"] is True
    assert ready.changes.lock_for(change.run_id) is not None

    # The refused attempt is recorded: somebody tried, and this is how far it got.
    attempts = ready.changes.recovery_history(change)
    assert [a.outcome for a in attempts] == [str(RecoveryAttemptOutcome.REFUSED)]
    assert attempts[0].refusal_code == "recovery_confirmation_required"
    assert attempts[0].mutation_outcome is None
    assert attempts[0].host_effect == str(HostEffect.NONE)
    # It did take a reading first, and recorded what it found.
    assert attempts[0].evidence_outcome == str(VerificationOutcome.MISMATCH)
    assert attempts[0].evidence_observed_value == 9000

    # The original confirmation is untouched by any of it.
    assert ready.changes.confirmation_for(change.run_id) == applied


def test_a_retry_writes_verifies_and_releases_the_hold_without_rewriting_history(
    ready: LinkEstate,
):
    """The full path: authority, a fresh race guard, a real write, an independent proof."""
    change = into_recovery(ready)
    before = frozen(change)
    assert ready.mtu_of("eth0") == 9000

    confirmation = authorise(ready, change)
    assert confirmation.purpose == "recovery_retry"
    assert confirmation.consumed is False

    result = ready.changes.recovery_retry(change, ready.management_path)

    assert result.attempt.outcome == str(RecoveryAttemptOutcome.VERIFIED)
    assert result.attempt.evidence_outcome == str(VerificationOutcome.MISMATCH)
    assert result.attempt.mutation_outcome == str(MutationOutcome.WRITTEN)
    assert result.attempt.host_effect == str(HostEffect.WRITTEN)
    assert result.attempt.verification_outcome == str(VerificationOutcome.VERIFIED)
    assert result.attempt.verification_observed_value == 1500
    assert result.attempt.confirmation_id == confirmation.confirmation_id
    assert ready.mtu_of("eth0") == 1500

    # The race guard came from the reading just taken, not from the Change's before-value.
    assert ready.kernel.mutations[-1] == (2, 1500)
    assert result.attempt.mutation_detail.get("replayed") in (None, False)

    assert result.hold.state is RecoveryHoldState.RESOLVED
    assert ready.changes.lock_for(change.run_id) is None
    # And the Change is untouched. It still says it required recovery, and still says why.
    assert frozen(result.change) == before
    assert result.change.result == str(ChangeResult.RECOVERY_REQUIRED)
    assert result.change.recovery_reason == str(RecoveryReason.ROLLBACK_VERIFICATION_FAILED)
    assert ready.runs.get(change.run_id).state == str(RunState.RECOVERY_REQUIRED)

    assert events(ready, change)[-8:] == [
        str(RunEvent.RECOVERY_CONFIRMATION_SATISFIED),
        str(RunEvent.RECOVERY_RETRY_STARTED),
        str(RunEvent.RECOVERY_EVIDENCE_RESULT),
        str(RunEvent.RECOVERY_CONFIRMATION_CONSUMED),
        str(RunEvent.RECOVERY_MUTATION_DISPATCHED),
        str(RunEvent.RECOVERY_MUTATION_RESULT),
        str(RunEvent.RECOVERY_VERIFICATION_RESULT),
        str(RunEvent.RECOVERY_RETRY_FINISHED),
    ] or events(ready, change)[-9:-1] == [
        str(RunEvent.RECOVERY_CONFIRMATION_SATISFIED),
        str(RunEvent.RECOVERY_RETRY_STARTED),
        str(RunEvent.RECOVERY_EVIDENCE_RESULT),
        str(RunEvent.RECOVERY_CONFIRMATION_CONSUMED),
        str(RunEvent.RECOVERY_MUTATION_DISPATCHED),
        str(RunEvent.RECOVERY_MUTATION_RESULT),
        str(RunEvent.RECOVERY_VERIFICATION_RESULT),
        str(RunEvent.RECOVERY_RETRY_FINISHED),
    ]


def test_one_recovery_authority_covers_one_attempt_and_no_more(ready: LinkEstate):
    """Single-use, structurally, for a recovery grant exactly as for an apply one."""
    change = into_recovery(ready)
    confirmation = authorise(ready, change)

    # A second grant cannot be recorded while the first is outstanding.
    with pytest.raises(RunRefused) as raised:
        authorise(ready, change)
    assert raised.value.code == "recovery_confirmation_already_satisfied"

    # Spend it on a retry that cannot complete: the write is refused by the kernel.
    original = ready.kernel.mutate
    ready.kernel.mutate = lambda frame: netlink_ack(_sequence(frame), errno=1)  # EPERM
    try:
        result = ready.changes.recovery_retry(change, ready.management_path)
    finally:
        ready.kernel.mutate = original  # type: ignore[method-assign]
    assert result.attempt.mutation_outcome == str(MutationOutcome.NOT_WRITTEN)

    spent = ready.database.query_one(
        "SELECT * FROM run_confirmations WHERE confirmation_id = ?",
        (confirmation.confirmation_id,))
    assert spent["consumed_at"] is not None
    assert spent["consumed_by_attempt_id"] == result.attempt.mutation_attempt_id

    # A further retry needs a new grant, and the spent one is not reachable.
    with pytest.raises(RunRefused) as raised:
        ready.changes.recovery_retry(change, ready.management_path)
    assert raised.value.code == "recovery_confirmation_required"


# ------------------------------------------------------- what a retry may not do differently


def test_a_hold_older_than_the_freshness_horizon_can_still_be_retried(ready: LinkEstate):
    """A hold normally outlives the 60s observation horizon, because a person has to look.

    The retry takes a fresh reading before it re-plans, so the re-plan must see *that* one.
    Re-planning against the record the call started from would refuse a perfectly good retry
    with `observation_stale`, having just refreshed the very observation it was complaining
    about — and every real recovery would hit it, because no operator answers within a minute.
    """
    change = into_recovery(ready)
    authorise(ready, change)
    # Age everything that was observed before this retry, the way a hold left overnight does.
    with ready.database.transaction():
        ready.database.connection.execute(
            "UPDATE observations SET observed_at = '2026-08-23T09:00:00.000000+00:00'")

    result = ready.changes.recovery_retry(change, ready.management_path)

    assert result.attempt.outcome == str(RecoveryAttemptOutcome.VERIFIED)
    assert result.attempt.mutation_outcome == str(MutationOutcome.WRITTEN)
    assert ready.mtu_of("eth0") == 1500
    assert ready.changes.lock_for(change.run_id) is None


def test_a_retry_may_not_substitute_todays_intent_for_the_one_the_change_was_for(
    ready: LinkEstate,
):
    """Recovery belongs to the original Change and may not quietly become a different one.

    Between the failure and the recovery an operator may revise the intent. Re-planning
    against current truth would then produce a perfectly valid plan for a *different* value,
    and writing that under this Change would record that LocalPlane finished what it started
    when it did something else.
    """
    change = into_recovery(ready)
    record = ready.object_named("eth0")
    active = ready.ingestor.objects.get(record.object_id).active_intent_id
    ready.management.revise_intent(record, expected_intent_id=active, fields={"mtu": 1400})
    authorise(ready, change)

    with pytest.raises(RunRefused) as raised:
        ready.changes.recovery_retry(change, ready.management_path)
    assert raised.value.code == "recovery_intent_changed"
    assert raised.value.detail["recorded"]["desired"] == 1500
    assert raised.value.detail["would_plan_now"]["desired"] == 1400

    assert ready.mtu_of("eth0") == 9000  # nothing was written
    assert ready.changes.lock_for(change.run_id) is not None
    attempts = ready.changes.recovery_history(change)
    assert attempts[-1].outcome == str(RecoveryAttemptOutcome.REFUSED)
    assert attempts[-1].refusal_code == "recovery_intent_changed"


def test_a_retry_cannot_bypass_the_ownership_gate_as_it_stands_now(ready: LinkEstate):
    """Every execution gate is re-derived against current truth, not against the old plan."""
    change = into_recovery(ready)
    authorise(ready, change)
    ready.networkmanager_takes_over_eth0()
    ready.observe()

    with pytest.raises(RunRefused) as raised:
        ready.changes.recovery_retry(change, ready.management_path)
    assert raised.value.code in ("execution_blocked", "recovery_operation_not_applicable")
    if raised.value.code == "execution_blocked":
        assert "externally_configured" in raised.value.detail["blockers"]
    assert ready.mtu_of("eth0") == 9000
    assert ready.changes.lock_for(change.run_id) is not None


@pytest.mark.parametrize(
    "verdict_for, code",
    [(None, "management_path_unproven"), ("eth0", "target_is_management_path")],
)
def test_a_retry_uses_this_requests_management_path_evidence_and_no_other(
    ready: LinkEstate, verdict_for: str | None, code: str
):
    """Evidence about where an operator was an hour ago is not evidence about now.

    The original Run proved its target was not the path it was reached over. That proof
    belonged to that request. A recovery arrives over a different connection, and the gate is
    re-applied against *its* evidence — including the case where the target is now proven to
    be the path, which is the one whose wrong answer ends the session asking.
    """
    change = into_recovery(ready)
    authorise(ready, change)
    verdict = (ManagementPathVerdict(resource_id=None, reason="management_path_unobserved")
               if verdict_for is None else ready.prove_path_on(verdict_for))

    for act in (
        lambda: ready.changes.recovery_retry(change, verdict),
        lambda: ready.changes.recovery_confirm(
            change, acknowledge=True, expected_recovery_reason=None, management_path=verdict),
    ):
        with pytest.raises(RunRefused) as raised:
            act()
        assert raised.value.code == code

    assert ready.mtu_of("eth0") == 9000
    assert ready.changes.lock_for(change.run_id) is not None
    # Refused before an attempt row exists: there was nothing to record but the refusal.
    assert ready.database.query("SELECT * FROM change_recovery_attempts") == []


def test_resolving_needs_no_management_path_evidence_because_it_writes_nothing(
    ready: LinkEstate,
):
    """The escape hatch stays available on a deployment that can never prove a path.

    A gate that blocked the one action which cannot touch the host would leave an operator on
    a loopback deployment with a hold they could neither retry nor release.
    """
    change = into_recovery(ready)
    unproven = ManagementPathVerdict(resource_id=None, reason="management_path_unobserved")
    with pytest.raises(RunRefused):
        ready.changes.recovery_retry(change, unproven)

    result = ready.changes.recovery_resolve(
        change, acknowledge=True, operator_statement="eth0", object_name="eth0",
        note=None, expected_recovery_reason=None)
    assert result.attempt.outcome == str(RecoveryAttemptOutcome.RESOLVED)
    assert ready.changes.lock_for(change.run_id) is None


# ------------------------------------------------------- what a retry's own outcome may say


def test_a_retry_that_provably_wrote_nothing_says_so_and_keeps_the_hold(ready: LinkEstate):
    """``not_written`` is a proof here exactly as it is on the apply path."""
    change = into_recovery(ready)
    authorise(ready, change)
    original = ready.kernel.mutate
    ready.kernel.mutate = lambda frame: netlink_ack(_sequence(frame), errno=1)
    try:
        result = ready.changes.recovery_retry(change, ready.management_path)
    finally:
        ready.kernel.mutate = original  # type: ignore[method-assign]

    assert result.attempt.outcome == str(RecoveryAttemptOutcome.NOT_WRITTEN)
    assert result.attempt.mutation_outcome == str(MutationOutcome.NOT_WRITTEN)
    assert result.attempt.host_effect == str(HostEffect.NONE)
    assert result.attempt.verification_outcome == str(VerificationOutcome.NOT_ATTEMPTED)
    assert result.attempt.releases_hold is False
    assert result.hold.state is RecoveryHoldState.UNRESOLVED
    assert ready.changes.lock_for(change.run_id) is not None
    assert ready.mtu_of("eth0") == 9000


def test_a_retry_that_may_have_written_keeps_the_hold_and_never_claims_otherwise(
    ready: LinkEstate,
):
    """A second unprovable write on top of an unprovable one. Nothing is resolved."""
    change = into_recovery(ready)
    authorise(ready, change)
    original = ready.kernel.mutate

    def lose_the_acknowledgement(frame: bytes) -> bytes:
        original(frame)  # the write really lands
        raise NetlinkFailure("acknowledgement_timeout", {"timeout_s": 2.0}, True)

    ready.kernel.mutate = lose_the_acknowledgement  # type: ignore[method-assign]
    try:
        result = ready.changes.recovery_retry(change, ready.management_path)
    finally:
        ready.kernel.mutate = original  # type: ignore[method-assign]

    assert result.attempt.outcome == str(RecoveryAttemptOutcome.WRITE_UNKNOWN)
    assert result.attempt.host_effect == str(HostEffect.WRITE_UNKNOWN)
    # No verification is attempted: it answers what the host holds, not what LocalPlane did.
    assert result.attempt.verification_outcome == str(VerificationOutcome.NOT_ATTEMPTED)
    assert result.hold.state is RecoveryHoldState.UNRESOLVED
    assert ready.changes.lock_for(change.run_id) is not None
    # And the Change still says what its own attempt did, not what this one did.
    assert result.change.host_effect == str(HostEffect.WRITTEN)
    assert result.change.recovery_reason == str(RecoveryReason.ROLLBACK_VERIFICATION_FAILED)


def test_a_retry_that_wrote_and_could_not_be_proven_keeps_the_hold(ready: LinkEstate):
    """A kernel acknowledgement is not success on a retry either."""
    change = into_recovery(ready)
    authorise(ready, change)
    original = ready.kernel.mutate
    # Acknowledged, and nothing moves: a third writer is still winning.
    ready.kernel.mutate = lambda frame: netlink_ack(_sequence(frame), errno=0)
    try:
        result = ready.changes.recovery_retry(change, ready.management_path)
    finally:
        ready.kernel.mutate = original  # type: ignore[method-assign]

    assert result.attempt.mutation_outcome == str(MutationOutcome.WRITTEN)
    assert result.attempt.verification_outcome == str(VerificationOutcome.MISMATCH)
    assert result.attempt.verification_observed_value == 9000
    assert result.attempt.outcome == str(RecoveryAttemptOutcome.NOT_PROVEN)
    assert result.attempt.releases_hold is False
    assert result.hold.state is RecoveryHoldState.UNRESOLVED
    assert ready.changes.lock_for(change.run_id) is not None


def test_a_retry_that_cannot_read_the_target_writes_nothing(ready: LinkEstate):
    """No reading, no race guard, no write. A retry does not guess what it is writing over."""
    change = into_recovery(ready)
    authorise(ready, change)
    executor = ready.changes._executors[RECONCILE]
    original = executor.observe
    executor.observe = lambda record: ObservationAttempt(  # type: ignore[method-assign]
        record=None, failure="observation_unavailable")
    try:
        with pytest.raises(RunRefused) as raised:
            ready.changes.recovery_retry(change, ready.management_path)
    finally:
        executor.observe = original  # type: ignore[method-assign]

    assert raised.value.code == "current_value_unreadable"
    attempts = ready.changes.recovery_history(change)
    assert attempts[-1].outcome == str(RecoveryAttemptOutcome.REFUSED)
    assert attempts[-1].evidence_outcome == str(VerificationOutcome.OBSERVATION_UNAVAILABLE)
    assert attempts[-1].host_effect == str(HostEffect.NONE)
    assert ready.mtu_of("eth0") == 9000
    assert ready.changes.lock_for(change.run_id) is not None


# --------------------------------------------------------------------------- the resolution


def test_a_resolution_writes_nothing_claims_nothing_and_releases_the_hold(ready: LinkEstate):
    """The explicit human escape, and the whole of what it may say.

    Not that the Change succeeded, not that the mutation happened, not that the host is safe,
    not that anything was rolled back. Only that at this time a person released the hold, with
    whatever could be observed recorded beside it as what it is.
    """
    change = into_recovery(ready)
    before = frozen(change)
    mutations_before = list(ready.kernel.mutations)
    host_before = ready.link_snapshot()

    result = ready.changes.recovery_resolve(
        change, acknowledge=True, operator_statement="eth0", object_name="eth0",
        note="third writer identified; MTU set deliberately", expected_recovery_reason=None)

    attempt = result.attempt
    assert attempt.kind == str(RecoveryActionKind.RESOLVE)
    assert attempt.outcome == str(RecoveryAttemptOutcome.RESOLVED)
    assert attempt.operator_statement == "eth0"
    assert attempt.note == "third writer identified; MTU set deliberately"
    # No mutation of any kind, and the store would refuse a row that claimed one.
    assert attempt.mutation_attempt_id is None
    assert attempt.mutation_outcome is None
    assert attempt.confirmation_id is None
    assert attempt.host_effect == str(HostEffect.NONE)
    assert attempt.verification_outcome == str(VerificationOutcome.NOT_ATTEMPTED)
    assert ready.kernel.mutations == mutations_before
    assert ready.link_snapshot() == host_before

    # What could be seen is recorded, and it is not a verification.
    assert attempt.evidence_outcome == str(VerificationOutcome.MISMATCH)
    assert attempt.evidence_observed_value == 9000
    assert attempt.evidence_observation_id is not None

    # The original Change is inspectable and unchanged.
    assert frozen(result.change) == before
    assert result.change.result == str(ChangeResult.RECOVERY_REQUIRED)
    assert result.change.recovery_reason == str(RecoveryReason.ROLLBACK_VERIFICATION_FAILED)
    assert result.change.mutation_outcome == str(MutationOutcome.WRITTEN)

    assert result.hold.state is RecoveryHoldState.RESOLVED
    assert result.hold.released_by == str(RecoveryActionKind.RESOLVE)
    assert result.hold.reason == str(RecoveryReason.ROLLBACK_VERIFICATION_FAILED)
    assert ready.changes.lock_for(change.run_id) is None
    assert events(ready, change)[-2:] == [
        str(RunEvent.RECOVERY_RESOLVED), str(RunEvent.RECOVERY_HOLD_RELEASED)]


def test_a_resolution_records_that_it_could_not_observe_rather_than_inventing_a_result(
    ready: LinkEstate,
):
    """An absence must stay visible. A missing reading is not a clean bill of health."""
    change = into_recovery(ready)
    executor = ready.changes._executors[RECONCILE]
    original = executor.observe
    executor.observe = lambda record: ObservationAttempt(  # type: ignore[method-assign]
        record=None, failure="observation_unavailable")
    try:
        result = ready.changes.recovery_resolve(
            change, acknowledge=True, operator_statement="eth0", object_name="eth0",
            note=None, expected_recovery_reason=None)
    finally:
        executor.observe = original  # type: ignore[method-assign]

    assert result.attempt.evidence_outcome == str(VerificationOutcome.OBSERVATION_UNAVAILABLE)
    assert result.attempt.evidence_observation_id is None
    assert result.attempt.evidence_observed_value is None
    assert result.attempt.outcome == str(RecoveryAttemptOutcome.RESOLVED)
    assert ready.changes.lock_for(change.run_id) is None


def test_a_resolution_releases_only_the_lock_of_the_change_it_names(links: LinkEstate):
    """Two held objects, one released. The other is untouched, and so is every other lock."""
    links.prove_path_on("lo")
    for name in ("eth0", "eth1"):
        links.adopt(name)
        links.drift(name, "1400")
    links.prove_path_on("lo")

    first = into_recovery(links, "eth0")
    second = into_recovery(links, "eth1")

    locks_before = {r["lock_key"] for r in links.rows("object_write_locks")}
    assert len(locks_before) == 2

    links.changes.recovery_resolve(
        first, acknowledge=True, operator_statement="eth0", object_name="eth0",
        note=None, expected_recovery_reason=None)

    remaining = {r["run_id"] for r in links.rows("object_write_locks")}
    assert remaining == {second.run_id}
    assert links.changes.lock_for(second.run_id) is not None
    assert links.changes.get_change(second.change_id).result == str(
        ChangeResult.RECOVERY_REQUIRED)


# -------------------------------------------------------------------------------- one way out


def test_a_hold_is_released_once_and_the_store_makes_a_second_release_impossible(
    ready: LinkEstate,
):
    """Retry-then-resolve, resolve-then-retry, and resolve twice all end in one refusal."""
    change = into_recovery(ready)
    ready.changes.recovery_resolve(
        change, acknowledge=True, operator_statement="eth0", object_name="eth0",
        note=None, expected_recovery_reason=None)

    for act in (
        lambda: ready.changes.recovery_resolve(
            change, acknowledge=True, operator_statement="eth0", object_name="eth0",
            note=None, expected_recovery_reason=None),
        lambda: ready.changes.recovery_retry(change, ready.management_path),
        lambda: authorise(ready, change),
    ):
        with pytest.raises(RunRefused) as raised:
            act()
        assert raised.value.code == "recovery_already_resolved"

    assert len(ready.changes.recovery_history(change)) == 1
    assert ready.changes.lock_for(change.run_id) is None


def test_a_second_recovery_action_cannot_run_while_one_is_in_flight(ready: LinkEstate):
    """Serialised in the database, so it holds against a second process too.

    Driven from inside the first attempt's own observation, which is the window a concurrent
    request would actually arrive in: the row saying an attempt is under way is already
    committed, and the second caller is refused before it can dispatch or release anything.
    """
    change = into_recovery(ready)
    authorise(ready, change)
    executor = ready.changes._executors[RECONCILE]
    original = executor.observe
    seen: dict[str, str] = {}

    def observe_and_race(record: Any) -> Any:
        for name, act in (
            ("retry", lambda: ready.changes.recovery_retry(change, ready.management_path)),
            ("resolve", lambda: ready.changes.recovery_resolve(
                change, acknowledge=True, operator_statement="eth0", object_name="eth0",
                note=None, expected_recovery_reason=None)),
        ):
            try:
                act()
            except RunRefused as exc:
                seen[name] = exc.code
        return original(record)

    executor.observe = observe_and_race  # type: ignore[method-assign]
    try:
        result = ready.changes.recovery_retry(change, ready.management_path)
    finally:
        executor.observe = original  # type: ignore[method-assign]

    assert seen == {"retry": "recovery_attempt_in_flight",
                    "resolve": "recovery_attempt_in_flight"}
    # Exactly one attempt exists and exactly one mutation was dispatched.
    attempts = ready.changes.recovery_history(change)
    assert len(attempts) == 1
    assert attempts[0].attempt_id == result.attempt.attempt_id
    assert result.attempt.outcome == str(RecoveryAttemptOutcome.VERIFIED)
    assert ready.kernel.mutations[-1] == (2, 1500)
    assert ready.database.query(
        "SELECT * FROM change_recovery_attempts WHERE releases_hold = 1") != []
    assert len(ready.database.query(
        "SELECT * FROM change_recovery_attempts WHERE releases_hold = 1")) == 1


def test_the_store_refuses_a_second_releasing_row_and_a_release_by_a_run_without_the_hold(
    ready: LinkEstate, database
):
    """The structural backstops, reached directly rather than through the service."""
    import sqlite3

    change = into_recovery(ready)
    ready.changes.recovery_resolve(
        change, acknowledge=True, operator_statement="eth0", object_name="eth0",
        note=None, expected_recovery_reason=None)
    row = dict(database.query_one("SELECT * FROM change_recovery_attempts"))

    # A second releasing row for the same change: refused by a partial unique index — and it
    # would be refused by the "you must hold it" trigger too, which is the point of both.
    forged = {**row, "attempt_id": "rcv_forged", "sequence": 2}
    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction():
            ready.changes.recovery_attempts.insert(forged)

    # And a settled attempt does not move.
    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction():
            ready.changes.recovery_attempts.update(row["attempt_id"], {"outcome": "proven"})
    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction():
            database.connection.execute(
                "DELETE FROM change_recovery_attempts WHERE attempt_id = ?",
                (row["attempt_id"],))


def test_the_store_refuses_a_resolution_that_claims_a_host_mutation(ready: LinkEstate, database):
    """A resolution performs no host mutation, and that is a CHECK rather than a code path."""
    import sqlite3

    change = into_recovery(ready)
    base = {
        "attempt_id": "rcv_forged", "change_id": change.change_id, "run_id": change.run_id,
        "host_id": change.host_id, "object_id": change.object_id, "sequence": 1,
        "kind": "resolve", "started_at": _NOW,
        "protection_management_path": "unknown", "outcome": "resolved",
        "finished_at": _NOW, "operator_statement": "eth0",
    }
    for claim in (
        {"host_effect": "written"},
        {"mutation_outcome": "written", "dispatch_began_at": _NOW, "host_effect": "written"},
        {"mutation_attempt_id": "rat_x"},
        {"verification_outcome": "verified"},
    ):
        with pytest.raises(sqlite3.IntegrityError):
            with database.transaction():
                ready.changes.recovery_attempts.insert({**base, **claim})


def test_the_store_refuses_a_new_write_attempt_with_no_authority(ready: LinkEstate, database):
    """A retry that writes needs a confirmation, structurally and not only in code."""
    import sqlite3

    change = into_recovery(ready)
    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction():
            ready.changes.recovery_attempts.insert({
                "attempt_id": "rcv_forged", "change_id": change.change_id,
                "run_id": change.run_id, "host_id": change.host_id,
                "object_id": change.object_id, "sequence": 1, "kind": "retry",
                "started_at": _NOW, "protection_management_path": "not_on_management_path",
                "mutation_attempt_id": "rat_x", "dispatch_began_at": _NOW,
                "mutation_outcome": "written", "host_effect": "written",
                "outcome": "write_unknown", "finished_at": _NOW,
            })


def test_a_recovery_attempt_may_only_exist_for_a_change_that_is_in_recovery(
    ready: LinkEstate, database
):
    import sqlite3

    run = ready.plan("eth0").run
    ready.confirm(run)
    change = ready.apply(run).change
    assert change.result == str(ChangeResult.SUCCEEDED)
    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction():
            ready.changes.recovery_attempts.insert({
                "attempt_id": "rcv_forged", "change_id": change.change_id,
                "run_id": change.run_id, "host_id": change.host_id,
                "object_id": change.object_id, "sequence": 1, "kind": "resolve",
                "started_at": _NOW, "protection_management_path": "unknown",
                "outcome": "resolved", "finished_at": _NOW, "operator_statement": "eth0",
            })


# ------------------------------------------------------------------- interruption on restart


#: The five points a process can die at during a recovery retry, and what each leaves on
#: disk. The last three are the ones that matter most: a durable mutation result is a fact
#: LocalPlane established, and a restart that replaced it with `write_unknown` would take
#: something known and report it as unknown — the same lie as the reverse, in the direction
#: nobody checks for.
_CRASH_POINTS = [
    # name, dispatch marker, outcome on disk before the restart, outcome after it,
    # host effect after it, verification outcome (unchanged by the restart)
    ("before_dispatch", False, None, None, "none", "not_attempted"),
    ("after_dispatch", True, None, "write_unknown", "write_unknown", "not_attempted"),
    ("after_not_written", True, "not_written", "not_written", "none", "not_attempted"),
    ("after_written", True, "written", "written", "written", "not_attempted"),
    ("after_verification", True, "written", "written", "written", "mismatch"),
]


def _crash_a_retry(ready: LinkEstate, change: Any, where: str) -> None:
    """Interrupt a real retry at one point, the way a process death would.

    Nothing is faked about what came before the interruption: the reading is real, the
    dispatch marker is real, the kernel is really asked, and the result really lands in the
    store. What is simulated is only the process ceasing to exist afterwards.
    """
    executor = ready.changes._executors[RECONCILE]
    restore: list[tuple[Any, str, Any]] = []

    def die(*_args: Any, **_kwargs: Any) -> Any:
        raise KeyboardInterrupt("the process is gone")

    if where == "before_dispatch":
        restore.append((executor, "observe", executor.observe))
        executor.observe = die  # type: ignore[method-assign]
    elif where == "after_dispatch":
        restore.append((executor, "mutate", executor.mutate))
        executor.mutate = die  # type: ignore[method-assign]
    elif where == "after_written":
        # The write really lands; the process dies before the reading that would judge it.
        calls = {"n": 0}
        original_observe = executor.observe

        def observe_once(record: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                return original_observe(record)
            raise KeyboardInterrupt("the process is gone")

        restore.append((executor, "observe", original_observe))
        executor.observe = observe_once  # type: ignore[method-assign]
    else:
        # `after_not_written` and `after_verification`: everything the engine records has
        # been recorded, and the process dies before the transition that would settle it.
        if where == "after_not_written":
            original_mutate = ready.kernel.mutate
            restore.append((ready.kernel, "mutate", original_mutate))
            ready.kernel.mutate = lambda frame: netlink_ack(_sequence(frame), errno=1)
        else:
            original_mutate = ready.kernel.mutate
            restore.append((ready.kernel, "mutate", original_mutate))
            # Acknowledged, and nothing moves: written, and the verification will not prove it.
            ready.kernel.mutate = lambda frame: netlink_ack(_sequence(frame), errno=0)
        restore.append((ready.changes, "_settle_retry", ready.changes._settle_retry))
        ready.changes._settle_retry = die  # type: ignore[method-assign]

    try:
        with pytest.raises(KeyboardInterrupt):
            ready.changes.recovery_retry(change, ready.management_path)
    finally:
        for target, name, original in restore:
            setattr(target, name, original)


@pytest.mark.parametrize(
    "where, dispatched, recorded, mutation, effect, verification",
    _CRASH_POINTS,
    ids=[point[0] for point in _CRASH_POINTS],
)
def test_an_interrupted_recovery_attempt_is_settled_without_losing_what_was_known(
    ready: LinkEstate, where: str, dispatched: bool, recorded: str | None,
    mutation: str | None, effect: str, verification: str,
):
    """The recovery path's crash window, read by the rule the apply path already uses.

    ``outcome_on_recovery`` takes two facts and this passes it both: **a recorded outcome is
    the answer**, and only a dispatch with nothing recorded is ``write_unknown``. A restart
    that overwrote a durable ``written`` or ``not_written`` would destroy something LocalPlane
    had established, and an operator reading the row afterwards could not tell whether the
    write had been proved or merely assumed away.

    Nothing is retried on a restart, whatever was found. A record nobody has looked at since a
    crash is not authority to write to a host, and the management-path evidence every write on
    this path requires belongs to a request, which a restart does not have.
    """
    change = into_recovery(ready)
    authorise(ready, change)
    _crash_a_retry(ready, change, where)

    before = ready.changes.recovery_attempts.in_flight(change.change_id)
    assert before is not None
    assert before.dispatch_began is dispatched
    assert before.mutation_outcome == recorded
    assert before.verification_outcome == verification

    settled = ready.changes.settle_interrupted_recovery()
    assert [a.attempt_id for a in settled] == [before.attempt_id]
    attempt = settled[0]

    assert attempt.outcome == str(RecoveryAttemptOutcome.INTERRUPTED)
    assert attempt.releases_hold is False
    assert attempt.finished_at is not None
    assert attempt.mutation_outcome == mutation
    assert attempt.host_effect == effect
    assert attempt.verification_outcome == verification

    if recorded is not None:
        # Every durable fact about the write is preserved exactly, not restated.
        assert attempt.mutation_attempt_id == before.mutation_attempt_id
        assert attempt.confirmation_id == before.confirmation_id
        assert attempt.dispatch_began_at == before.dispatch_began_at
        assert attempt.mutation_reason == before.mutation_reason
        assert attempt.mutation_provider == before.mutation_provider
        assert attempt.mutation_method == before.mutation_method
        assert attempt.mutation_detail == before.mutation_detail
        assert attempt.execution_correlation == before.execution_correlation
        # And so is whatever verification evidence had already been recorded.
        assert attempt.verification_observation_id == before.verification_observation_id
        assert attempt.verification_observed_value == before.verification_observed_value
        assert attempt.verification_reason == before.verification_reason
        # The event says the result was kept rather than re-derived.
        finished = [e for e in ready.changes.transcript(change.run_id)
                    if e.event == str(RunEvent.RECOVERY_RETRY_FINISHED)][-1]
        assert finished.detail["mutation_outcome_preserved"] is True
        assert finished.detail["mutation_outcome"] == mutation
    # The pre-write evidence is untouched in every case.
    assert attempt.evidence_outcome == before.evidence_outcome
    assert attempt.evidence_observation_id == before.evidence_observation_id
    assert attempt.evidence_observed_value == before.evidence_observed_value

    # The hold is kept in every case, and the Change is still what it was.
    assert ready.changes.lock_for(change.run_id) is not None
    assert ready.changes.recovery_hold(change).state is RecoveryHoldState.UNRESOLVED
    assert ready.changes.get_change(change.change_id).result == str(
        ChangeResult.RECOVERY_REQUIRED)
    # A settle does not run a second one, and it does not write.
    assert ready.changes.settle_interrupted_recovery() == []
    assert ready.changes.recovery_attempts.in_flight(change.change_id) is None


def test_a_restart_never_downgrades_a_known_write_to_unknown(ready: LinkEstate):
    """The regression this rule exists for, stated on its own so it cannot be lost.

    A retry that wrote and was acknowledged has established a fact. If the process then dies
    before the verification transition, the restart must find that fact and leave it alone —
    not replace it with ``write_unknown``, which is what deriving the answer from the dispatch
    marker alone would do.
    """
    change = into_recovery(ready)
    authorise(ready, change)
    _crash_a_retry(ready, change, "after_written")

    attempt = ready.changes.settle_interrupted_recovery()[0]
    assert attempt.mutation_outcome == str(MutationOutcome.WRITTEN)
    assert attempt.mutation_outcome != str(MutationOutcome.WRITE_UNKNOWN)
    assert attempt.host_effect == str(HostEffect.WRITTEN)
    # And the kernel really did take it, which is what makes `written` the truthful answer.
    assert ready.mtu_of("eth0") == 1500


# ------------------------------------------------ what the store refuses: authority

def _in_flight_row(estate: Any, change: Any, sequence: int = 1) -> str:
    """One in-flight attempt row, inserted the way the service inserts it."""
    attempt_id = f"rcv_forged{sequence}"
    with estate.database.transaction():
        estate.changes.recovery_attempts.insert({
            "attempt_id": attempt_id, "change_id": change.change_id,
            "run_id": change.run_id, "host_id": change.host_id,
            "object_id": change.object_id, "sequence": sequence, "kind": "retry",
            "started_at": _NOW, "protection_management_path": "not_on_management_path",
        })
    return attempt_id


def _dispatch_columns(confirmation_id: str, mutation_attempt_id: str) -> dict[str, Any]:
    """The four fields the dispatch marker writes together, as a forger would write them."""
    return {
        "confirmation_id": confirmation_id,
        "execution_correlation": "{}",
        "mutation_attempt_id": mutation_attempt_id,
        "dispatch_began_at": _NOW,
    }


def test_a_recovery_mutation_may_only_name_the_authority_that_was_spent_on_it(
    links: LinkEstate, database
):
    """Four forgeries, four different holes, one trigger.

    The service consumes a fresh `recovery_retry` confirmation and records the attempt id it
    was consumed by, in one transaction, before the provider is called. That is the correct
    sequence; this is what makes it the only storable one.
    """
    import sqlite3

    links.prove_path_on("lo")
    for name in ("eth0", "eth1"):
        links.adopt(name)
        links.drift(name, "1400")
    links.prove_path_on("lo")

    change = into_recovery(links, "eth0")
    other = into_recovery(links, "eth1")
    grant = authorise(links, change)
    other_grant = authorise(links, other)
    applied = links.changes.confirmation_for(change.run_id)
    assert applied.purpose == "apply" and applied.consumed is True

    attempt = _in_flight_row(links, change)

    def refused(columns: dict[str, Any]) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            with database.transaction():
                links.changes.recovery_attempts.update(attempt, columns)

    # 1 — the confirmation the *apply* consumed. It is consumed, it belongs to this run, and
    #     it even names a real attempt id — and it is still not authority for a second write.
    refused(_dispatch_columns(applied.confirmation_id, applied.consumed_by_attempt_id))
    # 2 — a recovery grant nobody has spent. Authority that exists is not authority that was
    #     given to *this* attempt.
    refused(_dispatch_columns(grant.confirmation_id, "rat_unspent"))
    # 3 — a recovery grant belonging to another Run.
    refused(_dispatch_columns(other_grant.confirmation_id, "rat_elsewhere"))

    # 4 — consumed, this run, right purpose, wrong attempt.
    with database.transaction():
        assert links.changes.confirmations.consume(
            confirmation_id=grant.confirmation_id, attempt_id="rat_a", now=_NOW)
    refused(_dispatch_columns(grant.confirmation_id, "rat_b"))

    # The one shape that is accepted is the one the service writes.
    with database.transaction():
        links.changes.recovery_attempts.update(
            attempt, _dispatch_columns(grant.confirmation_id, "rat_a"))
    stored = links.changes.recovery_attempts.get(attempt)
    assert stored.confirmation_id == grant.confirmation_id
    assert stored.mutation_attempt_id == "rat_a"


def test_one_consumed_recovery_authority_cannot_cover_a_second_mutation_attempt(
    ready: LinkEstate, database
):
    """Spent is spent, and the store says so twice over."""
    import sqlite3

    change = into_recovery(ready)
    grant = authorise(ready, change)
    first = _in_flight_row(ready, change, sequence=1)
    with database.transaction():
        assert ready.changes.confirmations.consume(
            confirmation_id=grant.confirmation_id, attempt_id="rat_a", now=_NOW)
        ready.changes.recovery_attempts.update(
            first, _dispatch_columns(grant.confirmation_id, "rat_a"))
        # Settle it so a second attempt may exist at all.
        ready.changes.recovery_attempts.update(first, {
            "mutation_outcome": "write_unknown", "host_effect": "write_unknown",
            "outcome": "write_unknown", "finished_at": _NOW})

    second = _in_flight_row(ready, change, sequence=2)
    # The same attempt id: refused by `mutation_attempt_id` being UNIQUE.
    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction():
            ready.changes.recovery_attempts.update(
                second, _dispatch_columns(grant.confirmation_id, "rat_a"))
    # A new attempt id under the same spent grant: refused by the trigger.
    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction():
            ready.changes.recovery_attempts.update(
                second, _dispatch_columns(grant.confirmation_id, "rat_b"))
    # And the confirmation itself is still single-use, both ways it can be reached: the
    # guarded UPDATE changes no row, and a write that skipped the guard is refused outright.
    with database.transaction():
        assert ready.changes.confirmations.consume(
            confirmation_id=grant.confirmation_id, attempt_id="rat_b", now=_NOW) is False
    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction():
            database.connection.execute(
                "UPDATE run_confirmations SET consumed_by_attempt_id = 'rat_b' "
                "WHERE confirmation_id = ?", (grant.confirmation_id,))
    spent = database.query_one(
        "SELECT * FROM run_confirmations WHERE confirmation_id = ?",
        (grant.confirmation_id,))
    assert spent["consumed_by_attempt_id"] == "rat_a"


# ------------------------------------------- what the store refuses: impossible states


#: States the domain says cannot exist. Each is a single column set applied to an in-flight
#: attempt row, and each must be refused rather than stored — a one-way constraint would let
#: a row claim an ending its own columns contradict.
_IMPOSSIBLE = {
    "a retry ending as a resolution":
        {"outcome": "resolved", "releases_hold": 1, "finished_at": _NOW},
    "a releasing outcome that did not release":
        {"outcome": "proven", "releases_hold": 0, "finished_at": _NOW,
         "evidence_outcome": "verified"},
    "a non-releasing outcome that released":
        {"outcome": "refused", "refusal_code": "x", "releases_hold": 1,
         "finished_at": _NOW},
    "not_written over a write that happened":
        {"mutation_outcome": "written", "host_effect": "written",
         "outcome": "not_written", "finished_at": _NOW},
    "write_unknown over a write that provably did not":
        {"mutation_outcome": "not_written", "host_effect": "none",
         "outcome": "write_unknown", "finished_at": _NOW},
    "not_proven with no verification attempted":
        {"mutation_outcome": "written", "host_effect": "written",
         "outcome": "not_proven", "finished_at": _NOW},
    "not_proven over a verification that succeeded":
        {"mutation_outcome": "written", "host_effect": "written",
         "verification_outcome": "verified", "outcome": "not_proven", "finished_at": _NOW},
    "proven after a mutation":
        {"mutation_outcome": "written", "host_effect": "written",
         "evidence_outcome": "verified", "outcome": "proven", "releases_hold": 1,
         "finished_at": _NOW},
    "a dispatch marker with no authority":
        {"dispatch_began_at": _NOW},
    "authority with no dispatch":
        {"confirmation_id": "cnf_x"},
    "a mutation result with no attempt":
        {"mutation_outcome": "written", "host_effect": "written"},
    "a verification with no mutation":
        {"verification_outcome": "mismatch"},
    "a finished attempt still in flight":
        {"finished_at": _NOW},
}


@pytest.mark.parametrize("claim", sorted(_IMPOSSIBLE), ids=lambda c: c.replace(" ", "_"))
def test_the_store_refuses_a_recovery_attempt_state_that_cannot_exist(
    ready: LinkEstate, database, claim: str
):
    """Every one of these is a durable row that would say something that did not happen."""
    import sqlite3

    change = into_recovery(ready)
    attempt = _in_flight_row(ready, change)
    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction():
            ready.changes.recovery_attempts.update(attempt, _IMPOSSIBLE[claim])
    assert ready.changes.recovery_attempts.get(attempt).outcome == "in_flight"


# --------------------------------------------- what the store refuses: borrowed evidence


def test_a_recovery_attempt_may_only_cite_evidence_about_its_own_object_and_host(
    links: LinkEstate, database
):
    """A foreign key says the observation exists. It does not say it is about this object.

    A reading of some other object is not weaker evidence for this one — it is none at all,
    and it is the most convincing-looking falsehood the record could hold, because `proven`
    means precisely that a reading established the end state.
    """
    import sqlite3

    links.prove_path_on("lo")
    links.adopt("eth0")
    links.drift("eth0", "1400")
    links.prove_path_on("lo")
    change = into_recovery(links, "eth0")
    attempt = _in_flight_row(links, change)

    elsewhere = links.ingestor.objects.get(links.object_named("eth1").object_id)
    foreign = elsewhere.observation.observation_id
    assert foreign is not None

    for columns in (
        {"evidence_outcome": "verified", "evidence_observation_id": foreign},
        {"mutation_outcome": "written", "host_effect": "written",
         "verification_outcome": "verified", "verification_observation_id": foreign},
    ):
        with pytest.raises(sqlite3.IntegrityError):
            with database.transaction():
                links.changes.recovery_attempts.update(attempt, columns)

    # The object's own newest observation is accepted, which is what makes this a binding
    # rather than a blanket refusal.
    own = links.ingestor.objects.get(change.object_id).observation.observation_id
    with database.transaction():
        links.changes.recovery_attempts.update(
            attempt, {"evidence_outcome": "verified", "evidence_observation_id": own})
    assert links.changes.recovery_attempts.get(attempt).evidence_observation_id == own


def test_a_recovery_attempt_is_bound_to_its_changes_host_and_not_merely_to_a_host(
    ready: LinkEstate, database
):
    """`host_id` is part of what an attempt is about, so it is part of what is checked."""
    import sqlite3

    change = into_recovery(ready)
    with database.transaction():
        database.connection.execute(
            "INSERT INTO hosts (host_id, identity_basis, identity_confidence, hostname, "
            "first_seen_at, last_seen_at) VALUES "
            "('h_other','machine_id','high','somewhere-else',?,?)", (_NOW, _NOW))
        database.connection.execute(
            "INSERT INTO management_path_observations (observation_id, host_id, observed_at, "
            "agent_instance_id, transport_peer_address, transport_peer_family, "
            "local_endpoint_address, local_endpoint_family, capability, provider, "
            "provider_version, method, route_status) VALUES "
            "('mpo_other','h_other',?,NULL,'10.9.0.2','inet','10.9.0.1','inet',"
            "'network.route.observe','linux.route','1','netlink_rtm_getroute','resolved')",
            (_NOW,))

    base = {
        "attempt_id": "rcv_forged", "change_id": change.change_id, "run_id": change.run_id,
        "object_id": change.object_id, "sequence": 1, "kind": "retry", "started_at": _NOW,
        "protection_management_path": "not_on_management_path",
    }
    # A different host than the Change's: refused by the belongs-to trigger.
    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction():
            ready.changes.recovery_attempts.insert({**base, "host_id": "h_other"})
    # Management-path evidence belonging to another host: refused by the evidence trigger.
    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction():
            ready.changes.recovery_attempts.insert({
                **base, "host_id": change.host_id, "protection_evidence_id": "mpo_other"})
    assert ready.database.query("SELECT * FROM change_recovery_attempts") == []


# ------------------------------------------------------------- the same engine, an action


def test_a_container_recovery_is_proven_by_a_fresh_reading_with_no_second_verb(
    containers: ContainerEstate,
):
    """A start the daemon began and could not account for, and a container now proven running.

    The engine asks the *operation* whether the reading proves the end state, exactly as it
    does for a field change, and learns nothing about containers in the process.
    """
    change = container_into_recovery(containers)
    assert change.change_kind == CHANGE_KIND_ACTION
    assert change.recovery_reason == str(RecoveryReason.APPLY_WRITE_UNKNOWN)
    before = frozen(change)

    # The daemon had in fact carried it out; nothing could say so at the time.
    containers.daemon.act(_ID, "start")
    posts_before = [p for verb, p in containers.daemon.requests if verb == "POST"]

    result = containers.changes.recovery_retry(change, containers.management_path)

    assert result.attempt.outcome == str(RecoveryAttemptOutcome.PROVEN)
    assert result.attempt.evidence_observed_state == "running"
    assert result.attempt.evidence_observed_value is None
    assert result.attempt.mutation_attempt_id is None
    assert result.attempt.host_effect == str(HostEffect.NONE)
    # No second verb was sent. The probe the capability check issues is not one.
    assert [p for verb, p in containers.daemon.requests
            if verb == "POST"][len(posts_before):] == []
    assert result.hold.state is RecoveryHoldState.RESOLVED
    assert containers.changes.lock_for(change.run_id) is None
    assert frozen(result.change) == before


def test_a_restart_recovery_is_proven_by_dockers_own_record_of_when_it_started(
    containers: ContainerEstate,
):
    """The reason proof belongs to the operation.

    A restart leaves a container running, and so does doing nothing to one that was already
    running. The engine cannot tell those apart and does not try: it hands the reading to the
    executor, which compares the instant Docker records against the one taken at dispatch.
    """
    change = container_into_recovery(
        containers, OperationType.DOCKER_CONTAINER_RESTART, "sidecar",
        freeze_started_at=True)
    assert change.recovery_reason == str(RecoveryReason.ACTION_NOT_PROVEN)

    # Still unprovable while the daemon's record has not moved.
    with pytest.raises(RunRefused) as raised:
        containers.changes.recovery_retry(change, containers.management_path)
    assert raised.value.code == "recovery_confirmation_required"
    assert containers.changes.recovery_history(change)[-1].evidence_outcome == str(
        VerificationOutcome.MISMATCH)

    # Authoritative evidence arrives: the container did start again.
    containers.daemon.act(_OTHER, "restart")
    result = containers.changes.recovery_retry(change, containers.management_path)
    assert result.attempt.outcome == str(RecoveryAttemptOutcome.PROVEN)
    assert result.attempt.evidence_observed_state == "running"
    assert containers.changes.lock_for(change.run_id) is None


def test_a_container_recovery_that_must_act_again_needs_fresh_authority(
    containers: ContainerEstate,
):
    """The same rule, the same table, the same single-use grant — for an action."""
    change = container_into_recovery(containers)
    with pytest.raises(RunRefused) as raised:
        containers.changes.recovery_retry(change, containers.management_path)
    assert raised.value.code == "recovery_confirmation_required"

    authorise(containers, change)
    result = containers.changes.recovery_retry(change, containers.management_path)

    assert result.attempt.outcome == str(RecoveryAttemptOutcome.VERIFIED)
    assert result.attempt.mutation_outcome == str(MutationOutcome.WRITTEN)
    assert result.attempt.verification_observed_state == "running"
    assert containers.daemon.containers[_ID]["State"]["Status"] == "running"
    assert containers.changes.lock_for(change.run_id) is None
    # The Change still says what it said. Its own attempt was never proven.
    assert result.change.result == str(ChangeResult.RECOVERY_REQUIRED)
    assert result.change.recovery_reason == str(RecoveryReason.APPLY_WRITE_UNKNOWN)


def test_a_container_recovery_leaves_every_other_container_untouched(
    containers: ContainerEstate,
):
    change = container_into_recovery(containers)
    other = json.dumps(containers.daemon.containers[_OTHER], sort_keys=True)
    authorise(containers, change)
    containers.changes.recovery_retry(change, containers.management_path)
    assert json.dumps(containers.daemon.containers[_OTHER], sort_keys=True) == other


def test_a_container_recovery_may_be_resolved_by_hand_without_touching_the_daemon(
    containers: ContainerEstate,
):
    change = container_into_recovery(containers)
    posts = [p for verb, p in containers.daemon.requests if verb == "POST"]
    result = containers.changes.recovery_resolve(
        change, acknowledge=True, operator_statement="web", object_name="web",
        note="the image is broken; handled out of band", expected_recovery_reason=None)

    assert result.attempt.outcome == str(RecoveryAttemptOutcome.RESOLVED)
    assert result.attempt.host_effect == str(HostEffect.NONE)
    assert result.attempt.evidence_observed_state == "exited"
    assert [p for verb, p in containers.daemon.requests if verb == "POST"][len(posts):] == []
    assert containers.changes.lock_for(change.run_id) is None
    assert containers.daemon.containers[_ID]["State"]["Status"] == "exited"


def test_a_retry_may_not_substitute_a_different_verb_for_the_one_the_change_was_for(
    containers: ContainerEstate,
):
    """An action's intended outcome is its verb and the state it promised. Both are checked."""
    change = container_into_recovery(containers)
    authorise(containers, change)
    # A Change recorded for `start`, handed the plan a `stop` would produce now.
    original = containers.changes._runs.replan

    def replan_as_stop(operation, record, management_path, **kwargs):
        return original(OperationType.DOCKER_CONTAINER_STOP, record, management_path, **kwargs)

    containers.changes._runs.replan = replan_as_stop  # type: ignore[method-assign]
    try:
        with pytest.raises(RunRefused) as raised:
            containers.changes.recovery_retry(change, containers.management_path)
    finally:
        containers.changes._runs.replan = original  # type: ignore[method-assign]

    assert raised.value.code in ("recovery_intent_changed", "recovery_operation_not_applicable")
    assert containers.changes.lock_for(change.run_id) is not None


# ------------------------------------------------------------------------ the seam, asserted


def test_the_recovery_engine_knows_nothing_about_what_it_is_recovering():
    """One recovery engine, and it learns neither what an MTU is nor what a container is.

    The generic half coordinates: it takes a reading through the executor's ordinary
    observation path, asks the executor whether that reading proves the end state, asks the
    executor to correlate a target and to dispatch. *What counts as proof* stays behind the
    operation seam, which is why a field change and an action go through the same code here.

    Asserted by reading the source rather than described, and extended to cover the two
    modules the recovery path was added to.
    """
    root = Path(__file__).resolve().parents[1] / "src" / "localplane" / "backend"
    for module in (root / "changes.py", root / "domain" / "changes.py",
                   root / "runs.py", root / "domain" / "policy.py",
                   root / "domain" / "protection.py"):
        source = module.read_text()
        code = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )
        without_docstrings = "".join(
            part for i, part in enumerate(code.split('"""')) if i % 2 == 0
        )
        for token in ("mtu", "interface", "link", "ethernet", "sysfs",
                      "docker", "container", "reconcile_mtu"):
            assert token not in without_docstrings.lower(), f"{module.name} mentions {token}"
    for module in (root / "changes.py", root / "domain" / "changes.py"):
        text = module.read_text()
        assert "backend.operations" not in text
        assert "container_operations" not in text


def test_the_transcript_vocabulary_and_the_schema_agree_exactly():
    """A closed vocabulary is only closed if both ends hold the same list.

    Read from 0010, which is the migration that last rebuilt `run_events` — the six
    connection-guard members joined the thirty-three recovery left.
    """
    sql = (MIGRATIONS_DIR / "0010_connection_guard.sql").read_text()
    declared = {str(event) for event in RunEvent}
    for name in declared:
        assert f"'{name}'" in sql, name
    assert len(declared) == 39


def test_the_migrations_through_0008_are_byte_identical_to_head():
    """0001–0008 are what HEAD holds. Asserted against git, not against a checksum.

    0009 is excluded because it is the migration this file covers, and it is not yet
    committed at the point these tests were written; 0010 belongs to later work.
    """
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(path.stem.split("_")[0]) > 8:
            continue
        committed = subprocess.run(
            ["git", "show", f"HEAD:src/localplane/backend/db/migrations/{path.name}"],
            cwd=str(Path(__file__).resolve().parents[1]), capture_output=True)
        if committed.returncode != 0:
            pytest.skip("not a git checkout")
        assert path.read_bytes() == committed.stdout, f"{path.name} changed on disk"


def test_only_the_migrations_that_declared_it_suspend_foreign_keys():
    """0009 rebuilds two leaves, which needs no escape, and it still declares nothing.

    Five migrations in the repository declare it and are named rather than counted: 0008
    rebuilt `objects`, 0010 rebuilt four referenced tables, 0012 rebuilds the referenced
    Run/preview pair for the closed systemd planning vocabulary, 0013 rebuilds
    `run_previews` again for the backend self-impact derivation, and 0014 rebuilds it once
    more together with `run_confirmations` for the override authority. The escape is visible
    in each, travels inside the checksummed file, and another appearing without a change that
    argued for it fails here.
    """
    declaring = [
        path.name for path in sorted(MIGRATIONS_DIR.glob("*.sql"))
        if "-- localplane:foreign-keys=off" in path.read_text()
    ]
    assert declaring == [
        "0008_docker.sql",
        "0010_connection_guard.sql",
        "0012_systemd_lifecycle.sql",
        "0013_self_impact.sql",
        "0014_self_impact_override.sql",
        "0015_systemd_lifecycle_changes.sql",
    ]


def test_the_outcome_mapping_is_total_and_one_way():
    """What a retry that dispatched ended as, decided by the outcome and never by a reading."""
    assert recovery_outcome_for(
        MutationOutcome.NOT_WRITTEN, VerificationOutcome.VERIFIED
    ) is RecoveryAttemptOutcome.NOT_WRITTEN
    assert recovery_outcome_for(
        MutationOutcome.WRITE_UNKNOWN, VerificationOutcome.VERIFIED
    ) is RecoveryAttemptOutcome.WRITE_UNKNOWN
    assert recovery_outcome_for(
        MutationOutcome.WRITTEN, VerificationOutcome.VERIFIED
    ) is RecoveryAttemptOutcome.VERIFIED
    for outcome in VerificationOutcome:
        if outcome is not VerificationOutcome.VERIFIED:
            assert recovery_outcome_for(
                MutationOutcome.WRITTEN, outcome
            ) is RecoveryAttemptOutcome.NOT_PROVEN
    assert {o for o in RecoveryAttemptOutcome if o.releases_hold} == {
        RecoveryAttemptOutcome.PROVEN, RecoveryAttemptOutcome.VERIFIED,
        RecoveryAttemptOutcome.RESOLVED}
