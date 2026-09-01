"""The write boundary: confirming, arming, applying, verifying, rolling back, recovering.

This is the half of the Change Engine that :mod:`localplane.backend.runs` deliberately does
not have. That module plans, publishes and cancels and provably cannot write; this one is the
single path on which LocalPlane changes a host:

    retained intent → Run → immutable preview → confirmation → arming → **boundary** →
    dispatch → typed outcome → verification → success, or restoration, or an admission

**Where the boundary is.** The transaction that creates the Change row. Every earlier step —
planning, confirming, arming — happens without one and can end a Run with nothing to record
about the host, because nothing about the host could have moved. After it there is a Change,
answerable for what became of the host whether or not anything did.

**The crash window is one transaction wide and it is not hidden.** The Change exists before
``dispatch_began_at`` is written; that is committed before the request is sent; the outcome is
written after the answer comes back. A process that dies in the middle leaves a row that says
which side of the dispatch it died on, and :meth:`ChangeService.settle_interrupted` reads it
back under the one rule that is safe without further evidence.

**Once a write may have happened, ``failed`` is a lie.** ``write_unknown`` goes to
restoration; a verification that cannot prove the wanted value goes to restoration; and a
restoration that cannot be proven goes to ``recovery_required``, which is a truthful ending
rather than an error path. The store agrees, and refuses anything else.

**Nothing here knows what kind of thing it is changing.** Correlating a target, dispatching a
typed mutation and re-reading through the ordinary observation path come from the operation's
executor. This module holds a field name, two values and a lifecycle.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from localplane.backend.db.database import Database, to_json
from localplane.backend.db.repositories import (
    CONFIRMATION_PURPOSE_APPLY,
    GuardRepository,
    RunGuardRecord,
    CONFIRMATION_PURPOSE_RECOVERY_RETRY,
    CONFIRMATION_PURPOSE_SELF_IMPACT_OVERRIDE,
    CONFIRMATION_SOURCE_AUTHENTICATED,
    RunPreviewRecord,
    ChangeRecord,
    ChangeRepository,
    CheckpointRecord,
    CheckpointRepository,
    ConfirmationRecord,
    ConfirmationRepository,
    ObjectRecord,
    RecoveryAttemptRecord,
    RecoveryAttemptRepository,
    RunEventRepository,
    RunRecord,
    WriteLockRecord,
    WriteLockRepository,
)
from localplane.backend.domain.changes import (
    ACTION_LOCK_ASPECT,
    CHANGE_KIND_ACTION,
    CHANGE_KIND_FIELD,
    ChangeResult,
    ExecutionRefused,
    Expectation,
    HostEffect,
    MutationOutcome,
    MutationReport,
    MutationRequest,
    ProofOfState,
    RecoveryActionKind,
    RecoveryAttemptOutcome,
    RecoveryHoldState,
    RecoveryReason,
    RunEvent,
    VerificationOutcome,
    host_effect_for,
    outcome_on_recovery,
    recovery_outcome_for,
)
from localplane.backend.domain.guard import (
    GuardArmed,
    GuardPhase,
    GuardRefused,
    GuardReport,
    GuardRequest,
    GuardSettlement,
    settle as settle_guard_report,
)
from localplane.backend.domain.intent import ValueType, coerce, encode
from localplane.backend.domain.protection import (
    ManagementPathRelation,
    ManagementPathVerdict,
    assess_management_path_reason,
)
from localplane.backend.domain.systemd_lifecycle import SystemdLifecycleContext
from localplane.backend.domain.runs import (
    ConfirmationMethod,
    ExecutionAvailability,
    ExecutionEligibility,
    OperationType,
    PlanRefused,
    PlannedAction,
    PlannedFieldChange,
    RunPlan,
    RunState,
    assess_validity,
)
from localplane.backend.runs import ObservationAttempt, OperationExecutor, RunRefused, RunService

LOG = logging.getLogger("localplane.backend.changes")

#: The states an apply may begin from, and the states a Run may be cancelled from — the same
#: two, because both are before the boundary. ``preview`` is where a Run starts;
#: ``awaiting_confirmation`` is where an apply that found no confirmation left it.
#:
#: There is deliberately no third. Cancelling during an apply leaves the interrupted
#: step's effect unknown, and an interrupted rollback is not a rollback, so cancellation
#: after the boundary is *refused* rather than designed: setting
#: ``state = 'cancelled'`` over a mutation that may have happened is the lie the
#: write-boundary rules exist to make unstorable, and the schema agrees.
CANCELLABLE_STATES = (RunState.PREVIEW, RunState.AWAITING_CONFIRMATION)
_APPLIABLE = {str(s) for s in CANCELLABLE_STATES}


@dataclass(frozen=True)
class ApplyOutcome:
    """What became of one apply: the Run as it now stands, and the Change if there is one."""

    run: RunRecord
    change: ChangeRecord | None


@dataclass(frozen=True)
class RecoveryHold:
    """Whether a Change is still holding its object, and how that ended if it has.

    Derived on every read from the durable lock and the append-only attempt history. There
    is no column for it, because a stored answer would be a second home for a fact two
    other records already state between them.
    """

    state: RecoveryHoldState
    reason: str | None
    object_write_locked: bool
    released_at: str | None = None
    released_by: str | None = None
    released_by_attempt_id: str | None = None
    released_outcome: str | None = None

    @property
    def unresolved(self) -> bool:
        return self.state is RecoveryHoldState.UNRESOLVED


@dataclass(frozen=True)
class RecoveryOutcome:
    """What became of one recovery action: the Change (unchanged), the attempt, the hold."""

    change: ChangeRecord
    attempt: RecoveryAttemptRecord
    hold: RecoveryHold


@dataclass(frozen=True)
class _ReadBack:
    """One fresh reading, judged. Shared by verification and rollback verification.

    ``value`` and ``state`` are the two shapes an observed answer comes in and exactly one
    of them is ever populated, matching the two halves the record itself keeps. Neither is
    the judgement — ``outcome`` is — and both are recorded so that "it was verified" and
    "here is what was actually read" stay separate facts.
    """

    outcome: VerificationOutcome
    observation_id: str | None = None
    value: bool | int | None = None
    state: str | None = None
    reason: str | None = None

    @property
    def observed(self) -> bool | int | str | None:
        return self.value if self.value is not None else self.state


class ChangeService:
    """Owns everything after the preview: confirmation, arming, the write, and recovery."""

    def __init__(
        self,
        database: Database,
        runs: RunService,
        executors: Mapping[OperationType, OperationExecutor],
    ) -> None:
        self._db = database
        self._runs = runs
        self._executors = dict(executors)
        self.confirmations = ConfirmationRepository(database)
        self.checkpoints = CheckpointRepository(database)
        self.changes = ChangeRepository(database)
        self.events = RunEventRepository(database)
        self.locks = WriteLockRepository(database)
        self.guards = GuardRepository(database)
        self.recovery_attempts = RecoveryAttemptRepository(database)

    # ------------------------------------------------------------------------- reading

    def change_for_run(self, run_id: str) -> ChangeRecord | None:
        return self.changes.for_run(run_id)

    def get_change(self, change_id: str) -> ChangeRecord | None:
        return self.changes.get(change_id)

    def list_changes(self, host_id: str, **filters: Any) -> list[ChangeRecord]:
        return self.changes.list_for_host(host_id, **filters)

    def transcript(self, run_id: str) -> list[Any]:
        return self.events.for_run(run_id)

    def confirmation_for(self, run_id: str) -> ConfirmationRecord | None:
        return self.confirmations.for_run(run_id)

    def checkpoint_for(self, run_id: str) -> CheckpointRecord | None:
        return self.checkpoints.for_run(run_id)

    def lock_for(self, run_id: str) -> Any:
        return self.locks.for_run(run_id)

    # -------------------------------------------------------------------- confirmation

    def confirm(
        self,
        run: RunRecord,
        *,
        preview_id: str,
        acknowledge: bool,
        expected_preview_digest: str | None,
        management_path: ManagementPathVerdict,
        acknowledge_object: str | None = None,
        systemd_lifecycle_context: SystemdLifecycleContext | None = None,
    ) -> ConfirmationRecord:
        """Record that this request satisfied the confirmation the published plan requires.

        **Bound to a Run and to a preview, never to a digest alone.** Two identical concurrent
        plans share one digest, so a confirmation keyed on content could not say which of them
        an operator looked at. ``preview_id`` is required and must be the one this Run
        published; the digest may be supplied as an optimistic cross-check and is recorded
        either way as evidence of *what* was confirmed.

        **Nothing is issued.** No token comes back and none is stored: the confirmation is a
        row naming this Run, and the only thing that can use it is an apply of this Run.

        **Nobody is identified.** ``source`` records only that this request crossed the
        accepted authentication boundary.

        A blocked or stale plan cannot be confirmed. Confirming work that could not proceed is
        how blocked work acquires the means to reach an apply review.
        """
        run = self._reread(run)
        plan = self._runs.published_plan(run)
        if run.state not in _APPLIABLE:
            raise RunRefused("run_not_confirmable",
                             "this run is not in a state that can be confirmed",
                             {"run_id": run.run_id, "state": run.state})
        if preview_id != run.preview.preview_id:
            raise RunRefused("confirmation_preview_mismatch",
                             "a confirmation must name the preview this run published",
                             {"run_id": run.run_id, "preview_id": preview_id,
                              "run_preview_id": run.preview.preview_id})
        if expected_preview_digest not in (None, run.preview.preview_digest):
            raise RunRefused("preview_digest_mismatch",
                             "the plan you are confirming is not the plan this run published",
                             {"expected": expected_preview_digest,
                              "published": run.preview.preview_digest})
        if not plan.confirmation.required:
            raise RunRefused("confirmation_not_required",
                             "this plan requires no confirmation, so there is nothing to satisfy",
                             {"run_id": run.run_id})
        self._require_executable_preview(run)
        required = plan.confirmation.method
        if required not in (ConfirmationMethod.ACKNOWLEDGE, ConfirmationMethod.TYPED):
            raise RunRefused("confirmation_method_unsupported",
                             "this plan requires a confirmation method this build does not accept",
                             {"required_method": str(required),
                              "accepted": [str(ConfirmationMethod.ACKNOWLEDGE),
                                           str(ConfirmationMethod.TYPED)]})
        if not acknowledge:
            raise RunRefused("confirmation_not_acknowledged",
                             "the confirmation was not acknowledged, so nothing was recorded",
                             {"run_id": run.run_id})

        typed_statement: str | None = None
        if required is ConfirmationMethod.TYPED:
            # **The typed method is the one the policy has demanded for a management-path
            # target since the policy existed, and nothing could satisfy it until a guard
            # could be armed.** The operator writes the name of the thing at risk;
            # here it is the object's display name under the identity this build already
            # holds, so nothing new has to be invented for a person to type.
            #
            # It is not a password and it is not authority: what makes a guarded execution
            # permissible is the guard, the checkpoint and the proven relation. What typing
            # establishes is that a person looked at *which* object this is about, and the
            # string is stored as their statement rather than compared and discarded.
            record = self._require_target(run)
            expected = record.display_name
            if acknowledge_object is None:
                raise RunRefused(
                    "confirmation_object_required",
                    "this plan requires a typed confirmation naming the object it changes",
                    {"run_id": run.run_id, "object_id": run.object_id,
                     "required_method": str(required)})
            if acknowledge_object != expected:
                raise RunRefused(
                    "confirmation_object_mismatch",
                    "the name typed is not the name of the object this plan changes",
                    {"run_id": run.run_id, "object_id": run.object_id})
            typed_statement = acknowledge_object
        elif acknowledge_object is not None:
            raise RunRefused(
                "confirmation_object_not_required",
                "this plan does not require a typed confirmation, so nothing may be typed",
                {"run_id": run.run_id, "required_method": str(required)})
        self._plan_now(run, management_path,
                       systemd_lifecycle_context=systemd_lifecycle_context)

        confirmation_id = f"cnf_{uuid.uuid4().hex}"
        with self._db.transaction():
            existing = self.confirmations.for_run(run.run_id)
            if existing is not None:
                raise RunRefused(
                    "confirmation_already_satisfied",
                    "this run already carries a confirmation and a confirmation is single-use",
                    {"run_id": run.run_id, "confirmation_id": existing.confirmation_id,
                     "consumed": existing.consumed})
            self.confirmations.insert({
                "confirmation_id": confirmation_id,
                "run_id": run.run_id,
                "purpose": CONFIRMATION_PURPOSE_APPLY,
                "preview_id": run.preview.preview_id,
                "preview_digest": run.preview.preview_digest,
                "digest_version": run.preview.digest_version,
                "required_method": str(required),
                "method": str(required),
                "typed_statement": typed_statement,
                "policy": run.preview.confirmation_policy,
                "source": CONFIRMATION_SOURCE_AUTHENTICATED,
                "satisfied_at": _now(),
            })
            self._append(run.run_id, RunEvent.CONFIRMATION_SATISFIED, detail={
                "confirmation_id": confirmation_id,
                "preview_id": run.preview.preview_id,
                "method": str(required),
                "typed_statement": typed_statement,
                "source": CONFIRMATION_SOURCE_AUTHENTICATED,
            })
            stored = self.confirmations.for_run(run.run_id)
            assert stored is not None  # written in this transaction

        LOG.info("confirmation satisfied", extra={
            "run_id": run.run_id, "confirmation_id": confirmation_id,
            "host_effect": "none", "change_created": False})
        return stored

    # ------------------------------------------------------------- self-impact override

    def grant_self_impact_override(
        self,
        run: RunRecord,
        *,
        preview_id: str,
        acknowledge: bool,
        expected_preview_digest: str | None,
        management_path: ManagementPathVerdict,
        systemd_lifecycle_context: SystemdLifecycleContext | None = None,
    ) -> ConfirmationRecord:
        """Record that an operator accepted this plan's one typed self-impact hazard.

        **A separate authority from the confirmation, and neither substitutes for the
        other.** The confirmation answers "do you want this change"; this answers "do you
        accept that carrying it out may take LocalPlane itself away, and that nothing here
        has verified another way to reach this host". They are different questions about
        different facts, they are granted by different requests, and the write boundary
        demands both through triggers that do not know about each other.

        **It authorises exactly one plan and nothing else.** The Run's own preview, named
        rather than inferred; the digest that preview carries; and the derivation frozen in
        it, which had to say this exact hazard is one such an authority could cover. A
        request cannot name a blocker to bypass, a status to assume or a force flag — the
        only thing it supplies is that somebody acknowledged, and everything the
        acknowledgement is *about* comes from the immutable document.

        **It removes nothing.** Protection stays what the evidence made it, every blocker
        stays published, and a plan whose eligibility is anything other than
        ``self_impact_override_required`` has no override to grant — including one that is
        merely ``blocked``, where granting this would be authorising past a hazard nobody
        established.

        **Nobody is identified.** The record says only that the request crossed the
        authentication boundary.
        """
        run = self._reread(run)
        if run.state not in _APPLIABLE:
            raise RunRefused("run_not_confirmable",
                             "this run is not in a state that can be confirmed",
                             {"run_id": run.run_id, "state": run.state})
        if preview_id != run.preview.preview_id:
            raise RunRefused("confirmation_preview_mismatch",
                             "an override must name the preview this run published",
                             {"run_id": run.run_id, "preview_id": preview_id,
                              "run_preview_id": run.preview.preview_id})
        if expected_preview_digest not in (None, run.preview.preview_digest):
            raise RunRefused("preview_digest_mismatch",
                             "the plan you are overriding is not the plan this run published",
                             {"expected": expected_preview_digest,
                              "published": run.preview.preview_digest})
        self._require_self_impact_override(run)
        if not acknowledge:
            raise RunRefused("confirmation_not_acknowledged",
                             "the override was not acknowledged, so nothing was recorded",
                             {"run_id": run.run_id})
        # Re-derived against *this* request's evidence, exactly as an apply re-derives its
        # gates: a preview whose self-impact truth has moved since publication is stale, and
        # the remedy for a stale plan is a new Run rather than authority granted over it.
        self._plan_now(run, management_path,
                       systemd_lifecycle_context=systemd_lifecycle_context)

        confirmation_id = f"cnf_{uuid.uuid4().hex}"
        with self._db.transaction():
            existing = self.confirmations.for_run(
                run.run_id, CONFIRMATION_PURPOSE_SELF_IMPACT_OVERRIDE)
            if existing is not None:
                raise RunRefused(
                    "self_impact_override_already_granted",
                    "this run already carries a self-impact override and it is single-use",
                    {"run_id": run.run_id, "confirmation_id": existing.confirmation_id,
                     "consumed": existing.consumed})
            self.confirmations.insert({
                "confirmation_id": confirmation_id,
                "run_id": run.run_id,
                "purpose": CONFIRMATION_PURPOSE_SELF_IMPACT_OVERRIDE,
                "preview_id": run.preview.preview_id,
                "preview_digest": run.preview.preview_digest,
                "digest_version": run.preview.digest_version,
                # An acknowledgement of a hazard the preview already states. There is no
                # second object to name and nothing to type that is not already in the
                # document this row is bound to by digest.
                "required_method": str(ConfirmationMethod.ACKNOWLEDGE),
                "method": str(ConfirmationMethod.ACKNOWLEDGE),
                "typed_statement": None,
                "policy": run.preview.confirmation_policy,
                "source": CONFIRMATION_SOURCE_AUTHENTICATED,
                "satisfied_at": _now(),
            })
            self._append(run.run_id, RunEvent.CONFIRMATION_SATISFIED, detail={
                "confirmation_id": confirmation_id,
                "purpose": CONFIRMATION_PURPOSE_SELF_IMPACT_OVERRIDE,
                "preview_id": run.preview.preview_id,
                "method": str(ConfirmationMethod.ACKNOWLEDGE),
                "source": CONFIRMATION_SOURCE_AUTHENTICATED,
                "host_effect": "none",
                "change_created": False,
            })
            stored = self.confirmations.for_run(
                run.run_id, CONFIRMATION_PURPOSE_SELF_IMPACT_OVERRIDE)
            assert stored is not None  # written in this transaction

        LOG.info("self-impact override granted", extra={
            "run_id": run.run_id, "confirmation_id": confirmation_id,
            "host_effect": "none", "change_created": False})
        return stored

    def self_impact_override_for(self, run_id: str) -> ConfirmationRecord | None:
        return self.confirmations.for_run(
            run_id, CONFIRMATION_PURPOSE_SELF_IMPACT_OVERRIDE)

    def _require_self_impact_override(self, run: RunRecord) -> None:
        """The published plan must itself say the override is what its execution rests on.

        Read from the preview rather than re-derived, for the same reason ``_is_guarded``
        is: the plan an operator was shown is the plan authority may be granted against.
        Whether that plan still holds is asked separately, by re-planning.
        """
        if run.preview.execution_eligibility != str(
            ExecutionEligibility.SELF_IMPACT_OVERRIDE_REQUIRED
        ):
            raise RunRefused(
                "self_impact_override_not_applicable",
                "this plan does not rest on a self-impact override, so there is none to grant",
                {"run_id": run.run_id, "preview_id": run.preview.preview_id,
                 "execution_eligibility": run.preview.execution_eligibility,
                 "blockers": list(run.preview.execution_blockers)})

    # --------------------------------------------------------------------------- apply

    def apply(
        self,
        run: RunRecord,
        management_path: ManagementPathVerdict,
        systemd_lifecycle_context: SystemdLifecycleContext | None = None,
    ) -> ApplyOutcome:
        """Execute the published plan. The only path in LocalPlane that writes to a host.

        Nothing is replanned into something else. The Run names the operation, the object, the
        intent version and both ends of the change, and every one is re-read and re-checked: a
        plan whose inputs moved is *stale*, and the remedy for a stale plan is a new Run.

        The order is the safety argument, and every step can only refuse:

        1. take the durable lock on this object and the aspect being changed — a second
           mutating Run gets a typed conflict rather than a queue position;
        2. re-read the object, and refuse a preview whose own published assessment says
           execution was not available — which is what makes a Run planned before this build
           existed non-executable, whatever else has changed;
        3. re-plan once under *this* request's management-path evidence, and refuse a stale
           plan, a blocked one, or — where the operation could affect one — a target no longer
           proven off the path;
        4. move to ``awaiting_confirmation`` and consume the confirmation atomically;
        5. prepare: arm the checkpoint where there is recovery to arm, and otherwise correlate
           the target, which is the last step before anything can be written either way;
        6. cross the boundary: create the Change;
        7. mark the dispatch, send it, record what came back.
        """
        executor = self._executor(run)
        run = self._acquire(run)
        try:
            record = self._require_target(run)
            self._require_executable_preview(run)
            self._plan_now(run, management_path, record=record,
                           systemd_lifecycle_context=systemd_lifecycle_context)
            attempt_id = f"att_{uuid.uuid4().hex}"
            run = self._consume_confirmation(run, attempt_id)
        except RunRefused as refusal:
            with self._db.transaction():
                self._append(run.run_id, RunEvent.APPLY_REFUSED, detail={
                    "code": refusal.code, "change_created": False, "host_effect": "none",
                    **refusal.detail})
                self.locks.release(run.run_id)
            raise

        checkpoint, correlation = self._prepare(run, record, executor)
        # For a guarded change the host side takes on the reversal *before* the boundary,
        # and the store refuses the Change without it. Everything after this line can
        # therefore assume that if the operator's path disappears, something on the host
        # will put the value back without being asked again.
        guard = self._arm_guard(run, checkpoint, executor) if _is_guarded(run) else None
        change = self._cross(run, checkpoint, correlation)
        run = self._reread(run)

        report = self._dispatch(run, change, executor)
        change = self._settle(run, change, report)
        run = self._reread(run)

        if report.outcome is MutationOutcome.NOT_WRITTEN:
            ended = self._finish(run, change, RunState.FAILED, ChangeResult.FAILED)
            self._release_guard(ended[0], guard, executor, "mutation_not_written",
                                change_id=change.change_id)
            return ApplyOutcome(*ended)

        if report.outcome is MutationOutcome.WRITTEN:
            run, change, read = self._verify(run, change, executor)
            if read.outcome.proved:
                if guard is not None:
                    # The operation's own result is proved and the connection's is not, and
                    # only one component can answer the second: the operator's next request.
                    return ApplyOutcome(*self._hold(run, change, guard))
                return ApplyOutcome(
                    *self._finish(run, change, RunState.SUCCEEDED, ChangeResult.SUCCEEDED))
            ended = self._unproven(run, change, checkpoint, executor, read)
            self._release_guard(ended[0], guard, executor, "verification_did_not_prove",
                                change_id=change.change_id)
            return ApplyOutcome(*ended)

        # write_unknown. Verification is not attempted: it would answer "what is the host now",
        # and the question that decides what to do here is "did our write occur", which no
        # reading can answer.
        ended = self._unproven(run, change, checkpoint, executor, None)
        self._release_guard(ended[0], guard, executor, "mutation_outcome_unknown",
                            change_id=change.change_id)
        return ApplyOutcome(*ended)

    # ------------------------------------------------------------------- guarded keep

    def guard_keep(
        self, run: RunRecord, management_path: ManagementPathVerdict, *, acknowledge: bool
    ) -> ApplyOutcome:
        """Settle a guarded change from the evidence *this* request carries.

        **The proof is the request itself.** Nothing LocalPlane can read from the host says
        an operator can still reach it: the value comes back fine over a link nobody can
        talk to. What settles it is a request that *arrived*, whose own transport
        re-establishes the management path by the same two-source rule the path is always
        proven by, and resolves it to the very object that was changed. That evidence
        travels over the path under test, which is what makes it a proof rather than a
        story about some other channel.

        So the order is: prove the connection from this request, and only then ask the
        holder to release the guard. A request that cannot prove it can never release one —
        not because it is refused a permission, but because the thing it would have to
        establish is exactly what it has failed to establish.

        **It can also arrive too late, and then it reports rather than keeps.** A window
        that has already lapsed cannot be un-lapsed, and the guard will have acted. This
        endpoint is where that is collected: the guard's own account of its reversal, a
        fresh reading to see whether the target is back, and the truthful ending that
        follows — ``rolled_back`` where the reading proves it, ``recovery_required`` where
        nothing does.
        """
        executor = self._executor(run)
        run = self._reread(run)
        if run.state != str(RunState.GUARDED):
            raise RunRefused("run_not_guarded",
                             "this run is not holding a guarded change",
                             {"run_id": run.run_id, "state": run.state})
        guard = self.guards.for_run(run.run_id)
        if guard is None or guard.settled:
            raise RunRefused("guard_already_settled",
                             "this run's connection guard has already been settled",
                             {"run_id": run.run_id,
                              "guard_id": guard.guard_id if guard else None,
                              "settled_phase": guard.settled_phase if guard else None})
        change = self.changes.for_run(run.run_id)
        assert change is not None  # a guarded run is past the boundary

        if not acknowledge:
            self._step(run.run_id, RunEvent.GUARD_KEEP_REFUSED, change_id=change.change_id,
                       detail={"code": "keep_not_acknowledged", "guard_id": guard.guard_id})
            raise RunRefused("keep_not_acknowledged",
                             "keeping a guarded change is a deliberate act and was not "
                             "acknowledged",
                             {"run_id": run.run_id})

        _assessment, relation = assess_management_path_reason(
            management_path, resource_id=run.object_id, applicable=True)
        proved = relation is ManagementPathRelation.ON_MANAGEMENT_PATH

        if proved:
            # Releasing and asking are the same call here on purpose: a guard that is still
            # holding is released, and one that has already acted answers with what it did.
            report = executor.disarm_guard(guard.guard_id)
        else:
            # Never `disarm` from a request that has not proved the path. Asking is a read
            # and it is how a lapsed window is still collected by a caller who cannot keep
            # the change — an operator reaching LocalPlane over some other route after their
            # own has gone is exactly the person who needs to be told what happened.
            report = executor.guard_status(guard.guard_id)

        if report.phase is GuardPhase.ARMED:
            # Still holding, and this request could not prove the connection.
            self._step(run.run_id, RunEvent.GUARD_KEEP_REFUSED, change_id=change.change_id,
                       detail={"code": "guard_connection_not_proved",
                               "guard_id": guard.guard_id,
                               "management_path": str(relation),
                               "reason": management_path.reason,
                               "missing_evidence": list(management_path.missing_evidence)})
            raise RunRefused(
                "guard_connection_not_proved",
                "this request cannot prove it reached LocalPlane over the object this change "
                "altered, which is the only thing that can keep it",
                {"run_id": run.run_id, "object_id": run.object_id,
                 "management_path": str(relation), "reason": management_path.reason,
                 "expires_at": guard.expires_at,
                 "missing_evidence": list(management_path.missing_evidence)})

        if proved and report.phase is GuardPhase.DISARMED:
            return ApplyOutcome(*self._keep(run, change, guard, report, management_path))

        # The window lapsed, the holder is gone, or it could not be asked. Whatever the
        # guard did, this Run did not end the way it was trying to.
        return ApplyOutcome(*self._settle_lapsed(run, change, guard, report, executor))

    def _keep(
        self,
        run: RunRecord,
        change: ChangeRecord,
        guard: RunGuardRecord,
        report: GuardReport,
        management_path: ManagementPathVerdict,
    ) -> tuple[RunRecord, ChangeRecord]:
        """The connection was proved and the guard released. The change is kept.

        ``succeeded`` here means both halves: the operation's own result was proved by a
        fresh reading before the hold began, and the connection was proved by this request.
        Neither on its own would be enough, and the store already refuses the first half
        being claimed without its observation.
        """
        with self._db.transaction():
            self.guards.update(guard.guard_id, {
                "kept_at": _now(),
                "kept_evidence_id": management_path.evidence_id,
                "settled_at": _now(),
                "settled_phase": str(GuardPhase.DISARMED),
                "settled_reason": "connection_proved",
                "settled_detail": to_json(dict(report.detail)),
            })
            self._append(run.run_id, RunEvent.GUARD_CONNECTION_PROVED,
                         change_id=change.change_id,
                         detail={"guard_id": guard.guard_id,
                                 "management_path": str(ManagementPathRelation.ON_MANAGEMENT_PATH),
                                 "evidence_id": management_path.evidence_id,
                                 "observed_at": management_path.observed_at,
                                 "reason": management_path.reason})
            self._append(run.run_id, RunEvent.GUARD_SETTLED, change_id=change.change_id,
                         detail={"guard_id": guard.guard_id, "phase": str(report.phase),
                                 "settled_because": "connection_proved"})
        LOG.info("guarded change kept", extra={
            "run_id": run.run_id, "change_id": change.change_id, "guard_id": guard.guard_id,
            "evidence_id": management_path.evidence_id})
        run = self._reread(run)
        return self._finish(run, self._require_change(change.change_id),
                            RunState.SUCCEEDED, ChangeResult.SUCCEEDED)

    def _settle_lapsed(
        self,
        run: RunRecord,
        change: ChangeRecord,
        guard: RunGuardRecord,
        report: GuardReport,
        executor: OperationExecutor,
    ) -> tuple[RunRecord, ChangeRecord]:
        """A guarded change that was not kept, ended from what the guard actually did.

        The guard's reversal is a rollback — it restores the value the checkpoint holds,
        through the same privileged path, with its own three-valued outcome — so it is
        recorded in the rollback columns that already mean exactly that, and the same rule
        applies to it as to a restoration LocalPlane performs itself: an acknowledgement is
        not a restoration until a reading proves it.

        Every other phase is an admission. A guard that is gone, or one that could not be
        asked, leaves this Run holding an object whose value nothing has confirmed, and the
        existing recovery machinery is what an operator uses from there.
        """
        checkpoint = self.checkpoints.for_run(run.run_id)
        assert checkpoint is not None  # a guarded change is a field change

        if report.phase is GuardPhase.FIRED and report.mutation is not None:
            self._step(run.run_id, RunEvent.ROLLBACK_STARTED, change_id=change.change_id,
                       state=RunState.ROLLING_BACK, state_from=run.state,
                       change={"rollback_required": 1},
                       detail={"restores_field": checkpoint.field,
                               "restores_value": checkpoint.before_value,
                               "performed_by": "connection_guard",
                               "guard_id": guard.guard_id,
                               "reason": "guard_window_lapsed_without_proof"})
            self._step(run.run_id, RunEvent.ROLLBACK_MUTATION_RESULT,
                       change_id=change.change_id,
                       change={"rollback_attempt_id": guard.reversal_attempt_id,
                               "rollback_dispatch_began_at": report.fired_at or _now(),
                               "rollback_outcome": str(report.mutation.outcome),
                               "rollback_reason": report.mutation.reason,
                               "rollback_detail": to_json({
                                   "performed_by": "connection_guard",
                                   "guard_id": guard.guard_id,
                                   **report.mutation.detail})},
                       detail={"outcome": str(report.mutation.outcome),
                               "reason": report.mutation.reason,
                               "performed_by": "connection_guard"})
            run, change = self._reread(run), self._require_change(change.change_id)

        self._record_guard_settlement(run, guard, report, "window_lapsed_without_proof",
                                      change_id=change.change_id)
        run = self._reread(run)

        if report.phase is GuardPhase.LOST:
            return self._recover(run, change, RecoveryReason.GUARD_LOST,
                                 {"guard_id": guard.guard_id, "reason": report.reason})
        if report.phase is GuardPhase.DISARMED:
            # Released, and the evidence that justified releasing it never reached the
            # store — the one-transaction crash window in keeping a guarded change. Nothing
            # was reverted and the operation's own result was proved before the hold began,
            # so this is not a failure; it is a proof LocalPlane cannot produce, and saying
            # `succeeded` would be producing it anyway.
            return self._recover(
                run, change, RecoveryReason.GUARD_RELEASED_WITHOUT_RECORDED_PROOF,
                {"guard_id": guard.guard_id, "reason": report.reason})
        if report.phase is not GuardPhase.FIRED:
            return self._recover(run, change, RecoveryReason.GUARD_UNREACHABLE,
                                 {"guard_id": guard.guard_id, "reason": report.reason})

        if not report.reverted:
            # The guard acted and its reversal did not land, or may not have. There is
            # nothing a reading could add about what LocalPlane did.
            return self._recover(run, change, RecoveryReason.GUARD_REVERSAL_UNPROVEN,
                                 {"guard_id": guard.guard_id,
                                  "reversal_outcome": str(report.mutation.outcome)
                                  if report.mutation else None})

        self._step(run.run_id, RunEvent.ROLLBACK_VERIFICATION_STARTED,
                   change_id=change.change_id, state=RunState.ROLLBACK_VERIFYING,
                   state_from=run.state,
                   detail={"expect": checkpoint.before_value, "field": checkpoint.field,
                           "performed_by": "connection_guard"})
        run = self._reread(run)
        read = self._read_back(run, change, executor, _restored_expectation(change))
        self._step(run.run_id, RunEvent.ROLLBACK_VERIFICATION_RESULT,
                   change_id=change.change_id,
                   change=_verification_columns("rollback_verification", change, read),
                   detail={"outcome": str(read.outcome), "reason": read.reason,
                           "observation_id": read.observation_id, "observed": read.value,
                           "expected": checkpoint.before_value})
        run, change = self._reread(run), self._require_change(change.change_id)

        assert report.mutation is not None  # `reverted` requires one
        settlement = settle_guard_report(report, reversal_proved=read.outcome.proved)
        if settlement is GuardSettlement.REVERTED:
            return self._finish(run, change, RunState.ROLLED_BACK, ChangeResult.ROLLED_BACK)
        return self._recover(run, change, _recovery_reason(read.outcome, report.mutation),
                             {"guard_id": guard.guard_id, "reason": read.reason})

    def settle_interrupted_guards(
        self, host_id: str | None = None, executors: Mapping[str, OperationExecutor] | None = None
    ) -> list[RunGuardRecord]:
        """Collect what became of every guard nothing has heard back about. Reads only.

        Run on every backend start, before :meth:`settle_interrupted`, and it is the reason
        a guarded Run survives a restart of the process that started it. It asks — it never
        releases: a backend coming back while a guard is still counting down must not cancel
        the protection an operator is relying on, and one method that sometimes did would be
        one call away from doing exactly that.

        **Two answers mean "ask again later" and are left alone.** A guard still ``armed``
        is doing its job. A guard that could not be interrogated has told LocalPlane nothing
        — an agent that has not finished starting is not a guard that is gone — and settling
        a Run on that would be recording an admission that the next attempt would contradict.

        **Nothing is written to the host here, and nothing could be.** The only calls this
        makes are the status read and, where a guard has definitely fired, the ordinary
        observation path — the same reading any other verification takes.
        """
        settled: list[RunGuardRecord] = []
        for guard in self.guards.unsettled(host_id):
            run = self._runs.runs.get(guard.run_id)
            assert run is not None  # a guard's run cannot be deleted
            try:
                executor = self._executor(run)
            except RunRefused:
                # An operation this build no longer implements. The guard is real and this
                # process cannot ask about it; leaving the row unsettled says exactly that.
                LOG.error("a connection guard belongs to an unsupported operation", extra={
                    "guard_id": guard.guard_id, "run_id": run.run_id,
                    "operation": run.operation})
                continue
            report = executor.guard_status(guard.guard_id)
            if report.phase in (GuardPhase.ARMING, GuardPhase.ARMED, GuardPhase.UNREACHABLE):
                LOG.warning("a connection guard is still outstanding", extra={
                    "guard_id": guard.guard_id, "run_id": run.run_id,
                    "phase": str(report.phase), "expires_at": guard.expires_at})
                continue
            change = self.changes.for_run(run.run_id)
            if run.state == str(RunState.GUARDED) and change is not None:
                self._settle_lapsed(run, change, guard, report, executor)
            else:
                # The Run ended some other way and nobody collected the guard's account of
                # itself. Recording it changes no Change: what happened to the Run happened,
                # and this is the guard's own record of what it did about it.
                self._record_guard_settlement(
                    run, guard, report, "settled_on_restart",
                    change_id=change.change_id if change else None)
            refreshed = self.guards.get(guard.guard_id)
            assert refreshed is not None
            settled.append(refreshed)
        return settled

    # ------------------------------------------------------------------------- recovery

    def settle_interrupted(self, host_id: str | None = None) -> list[ChangeRecord]:
        """Interpret every Change that crossed the boundary and never reached an ending.

        Run on startup, and callable directly. It is read by :func:`outcome_on_recovery` —
        the *same function* the recovery path's own crash window is read by, given the same
        two facts:

        * **a recorded outcome is the answer.** It was written by the path that knew, and a
          restart is not entitled to a second opinion about it. A durable ``not_written``,
          ``written`` or ``write_unknown`` survives untouched, with the reason, provider,
          method, detail and host effect the dispatch left beside it;
        * **dispatch began and nothing recorded an outcome** — ``write_unknown``. The process
          died between handing the request over and learning what happened, and there is no
          honest fourth answer;
        * **dispatch never began** — ``not_written``. A proof, not an assumption.

        **The window a recorded outcome has to survive is real.** Between ``_settle`` and
        ``_finish`` the mutation result is durable and the Run has not ended, and a
        verification is taken in the middle of it. Rewriting a recorded ``written`` into
        ``write_unknown`` there would destroy something LocalPlane had established, and
        rewriting a recorded ``not_written`` would invent a possible host write that provably
        did not happen — and then hold an object nothing had touched.

        **What the outcome decides is where the Run ends.** Nothing was written — ``failed``,
        and the lock is released. Written, and already proved by a verification the
        dispatching process recorded — ``succeeded``: both halves of that claim are on disk,
        which is the exact pair the store CHECKs, and neither was guessed here. Anything else
        — ``recovery_required``, holding the object until somebody looks.

        **A guarded Run is never concluded here.** ``settle_interrupted_guards`` runs first
        and asks the component that would know; a change whose reversal may still fire has
        not succeeded, whatever its own verification proved.

        No restoration is attempted. Writing to a host on the strength of a record nobody has
        looked at since a crash, without the evidence about the operator's own path that every
        other write requires, is not recovery. What LocalPlane owes here is an accurate
        statement of what it does and does not know.
        """
        settled: list[ChangeRecord] = []
        for change in self.changes.unsettled(host_id):
            run = self._runs.runs.get(change.run_id)
            assert run is not None  # a change's run cannot be deleted
            if run.state == str(RunState.GUARDED):
                # A guarded Run is unsettled on purpose: it wrote, it proved what it wrote,
                # and what it is waiting for is a connection nobody in this process can
                # produce. `settle_interrupted_guards` runs first and has already asked the
                # component that would know; a guard still holding is protection an operator
                # is relying on, and a Run waiting on it has neither succeeded nor failed.
                continue
            recorded = (MutationOutcome(change.mutation_outcome)
                        if change.mutation_outcome else None)
            outcome = outcome_on_recovery(change.dispatch_began, recorded)
            guarded = _is_guarded(run)
            detail = {"outcome": str(outcome), "dispatch_began": change.dispatch_began,
                      "mutation_outcome_preserved": recorded is not None,
                      "settled_by": "recovery_on_restart"}
            if recorded is None:
                # The window this exists for: the request may have left and nothing came
                # back. Everything else about the Change is already true and stays as it is.
                self._step(
                    run.run_id, RunEvent.MUTATION_RESULT, change_id=change.change_id,
                    host_effect=str(host_effect_for(outcome)),
                    change={
                        "mutation_outcome": str(outcome),
                        "mutation_reason": "interrupted_before_a_result_was_recorded",
                        "mutation_detail": to_json({"settled_by": "recovery_on_restart"}),
                        "settled_at": _now(),
                        "host_effect": str(host_effect_for(outcome)),
                    },
                    detail=detail,
                )
                run = self._reread(run)
                change = self._require_change(change.change_id)
            if outcome is MutationOutcome.NOT_WRITTEN:
                self._finish(run, change, RunState.FAILED, ChangeResult.FAILED,
                             detail=detail)
            elif (outcome is MutationOutcome.WRITTEN
                  and change.verification_outcome == str(VerificationOutcome.VERIFIED)
                  and not guarded):
                # Both halves of `succeeded` are on disk — the dispatch recorded that the
                # write occurred and a reading through the ordinary path proved the value —
                # so this reads a record rather than forming an opinion about a host nobody
                # has looked at since. A guarded Run is excluded: its reversal may still be
                # armed, and a change that is about to be put back has not succeeded.
                self._finish(run, change, RunState.SUCCEEDED, ChangeResult.SUCCEEDED,
                             detail=detail)
            else:
                self._recover(
                    run, change,
                    RecoveryReason.APPLY_WRITE_UNKNOWN
                    if outcome is MutationOutcome.WRITE_UNKNOWN
                    else RecoveryReason.APPLY_INTERRUPTED_AFTER_WRITE,
                    {**detail, "guarded": guarded,
                     "verification_outcome": change.verification_outcome},
                )
            LOG.warning("interrupted change settled", extra={
                "change_id": change.change_id, "run_id": change.run_id,
                "outcome": str(outcome),
                "mutation_outcome_preserved": recorded is not None})
            settled.append(self._require_change(change.change_id))
        return settled

    # ------------------------------------------------------------------------ lifecycle

    def _acquire(self, run: RunRecord) -> RunRecord:
        """Take the durable lock before anything is re-read, so nothing moves underneath."""
        with self._db.transaction():
            current = self._reread(run)
            if current.state not in _APPLIABLE:
                raise RunRefused("run_not_appliable",
                                 "this run is not in a state that can be applied",
                                 {"run_id": current.run_id, "state": current.state})
            if self.locks.for_run(current.run_id) is None:
                aspect = _lock_aspect(current)
                taken = self.locks.acquire(
                    host_id=current.host_id, object_id=current.object_id,
                    field=aspect, run_id=current.run_id, now=_now())
                if not taken:
                    other = self.locks.held(
                        host_id=current.host_id, object_id=current.object_id,
                        field=aspect)
                    raise RunRefused(
                        "object_write_locked",
                        "another run is already mutating this object's controlled field",
                        {"object_id": current.object_id, "field": aspect,
                         "held_by_run_id": other.run_id if other else None,
                         "acquired_at": other.acquired_at if other else None})
            return current

    def _consume_confirmation(self, run: RunRecord, attempt_id: str) -> RunRecord:
        """Pass through ``awaiting_confirmation``, then consume the confirmation once.

        The transition is committed on its own, before the confirmation is looked for. That
        ordering is what makes the state durable rather than notional: an apply that finds no
        confirmation leaves the Run *waiting for one*, which is true, instead of rolling the
        transition back with the refusal and leaving a Run that looks untouched.

        ``arming`` is entered only where there is recovery to arm. An action has none — there
        is no previous value to hold — so it waits, is confirmed, and goes to the boundary,
        and the transcript says ``target_correlated`` rather than borrowing arming's name for
        a step that did not happen.

        **Where the published plan rests on a self-impact override, both authorities are
        consumed here, in the same transaction.** Not because one implies the other — they
        are separate grants answering separate questions, and each is checked in full — but
        because "this write was authorised" has to become true all at once. Consuming one
        and failing on the other would leave a Run holding spent authority for a write that
        never happened, and re-usable authority after a failure is how one grant becomes two
        writes.
        """
        if run.state == str(RunState.PREVIEW):
            self._step(run.run_id, RunEvent.CONFIRMATION_REQUIRED,
                       state=RunState.AWAITING_CONFIRMATION, state_from=run.state,
                       detail={"preview_id": run.preview.preview_id})
            run = self._reread(run)

        with self._db.transaction():
            confirmation = self.confirmations.for_run(run.run_id)
            if confirmation is None:
                raise RunRefused(
                    "confirmation_required",
                    "this plan requires a confirmation and none has been recorded for it",
                    {"run_id": run.run_id, "preview_id": run.preview.preview_id,
                     "method": run.preview.confirmation_method})
            if confirmation.consumed:
                raise RunRefused(
                    "confirmation_already_consumed",
                    "this confirmation has already authorised an execution attempt",
                    {"confirmation_id": confirmation.confirmation_id,
                     "consumed_at": confirmation.consumed_at,
                     "consumed_by_attempt_id": confirmation.consumed_by_attempt_id})
            if confirmation.preview_digest != run.preview.preview_digest:
                raise RunRefused("preview_digest_mismatch",
                                 "the plan that was confirmed is not the plan this run published",
                                 {"confirmed": confirmation.preview_digest,
                                  "published": run.preview.preview_digest})
            if not self.confirmations.consume(
                confirmation_id=confirmation.confirmation_id, attempt_id=attempt_id, now=_now()
            ):
                # Lost the race with a concurrent apply; the other one holds the authority.
                raise RunRefused("confirmation_already_consumed",
                                 "this confirmation was consumed by another execution attempt",
                                 {"confirmation_id": confirmation.confirmation_id})
            self._append(run.run_id, RunEvent.CONFIRMATION_CONSUMED, detail={
                "confirmation_id": confirmation.confirmation_id,
                "purpose": CONFIRMATION_PURPOSE_APPLY, "attempt_id": attempt_id})
            if _requires_self_impact_override(run):
                self._consume_self_impact_override(run, attempt_id)
            if _arms_recovery(run):
                self._runs.runs.set_state(run_id=run.run_id, state=str(RunState.ARMING))
                self._append(run.run_id, RunEvent.ARMING_STARTED, state_from=run.state,
                             state_to=str(RunState.ARMING), detail={"attempt_id": attempt_id})
        return self._reread(run)

    def _consume_self_impact_override(self, run: RunRecord, attempt_id: str) -> None:
        """Spend the override, inside the caller's transaction. It refuses; it never grants.

        Every check the apply confirmation gets, asked again of a different row: it has to
        exist, it has to be unspent, and it has to have been granted against the digest this
        Run published. The consuming UPDATE carries its own ``consumed_at IS NULL`` guard, so
        two applies racing for one override leave only the winner able to proceed.

        An apply confirmation can never satisfy this and this can never satisfy an apply
        confirmation: they are found by purpose, spent separately, and demanded at the write
        boundary by triggers that do not know about each other.
        """
        override = self.confirmations.for_run(
            run.run_id, CONFIRMATION_PURPOSE_SELF_IMPACT_OVERRIDE)
        if override is None:
            raise RunRefused(
                "self_impact_override_required",
                "this plan may only be executed with a self-impact override and none has "
                "been granted for it",
                {"run_id": run.run_id, "preview_id": run.preview.preview_id,
                 "execution_eligibility": run.preview.execution_eligibility})
        if override.consumed:
            raise RunRefused(
                "self_impact_override_already_consumed",
                "this self-impact override has already authorised an execution attempt",
                {"confirmation_id": override.confirmation_id,
                 "consumed_at": override.consumed_at,
                 "consumed_by_attempt_id": override.consumed_by_attempt_id})
        if override.preview_digest != run.preview.preview_digest:
            raise RunRefused(
                "preview_digest_mismatch",
                "the plan this override was granted against is not the plan this run "
                "published",
                {"granted": override.preview_digest,
                 "published": run.preview.preview_digest})
        if not self.confirmations.consume(
            confirmation_id=override.confirmation_id, attempt_id=attempt_id, now=_now()
        ):
            raise RunRefused(
                "self_impact_override_already_consumed",
                "this self-impact override was consumed by another execution attempt",
                {"confirmation_id": override.confirmation_id})
        self._append(run.run_id, RunEvent.CONFIRMATION_CONSUMED, detail={
            "confirmation_id": override.confirmation_id,
            "purpose": CONFIRMATION_PURPOSE_SELF_IMPACT_OVERRIDE,
            "attempt_id": attempt_id})

    def _prepare(
        self, run: RunRecord, record: ObjectRecord, executor: OperationExecutor
    ) -> tuple[CheckpointRecord | None, dict[str, Any]]:
        """The last step before the boundary, in whichever of its two forms applies.

        For a change with an inverse this is **arming**: the executor correlates the target
        and a durable checkpoint holding the previous value is written, because nothing may
        be written to the host until the material that would put it back is on disk.

        For an action there is no previous value and therefore nothing to arm, and inventing
        a checkpoint that restores nothing would be exactly the fake rollback this build
        refuses to build. What still has to happen is the correlation — the executor has to
        establish that it can identify the target — and it happens here, before the boundary,
        so that a target it cannot identify ends the Run with nothing written.

        Both forms can only refuse, and a refusal on either ends the Run ``failed`` with no
        Change and the lock released.
        """
        if _arms_recovery(run):
            checkpoint = self._arm(run, record, executor)
            return checkpoint, dict(checkpoint.execution_correlation)
        return None, self._correlate(run, record, executor)

    def _correlate(
        self, run: RunRecord, record: ObjectRecord, executor: OperationExecutor
    ) -> dict[str, Any]:
        """Establish the material an action needs to reach its target, and record that.

        The transcript entry is the honest one: the target was correlated, and no recovery
        was armed *because this operation has none* — which is a decision the plan already
        published and which the entry restates rather than hiding.
        """
        try:
            correlation = executor.correlate(record)
        except (ExecutionRefused, Exception) as exc:  # noqa: BLE001 - report, never proceed
            code = exc.code if isinstance(exc, ExecutionRefused) else "target_not_correlated"
            detail = (dict(exc.detail) if isinstance(exc, ExecutionRefused)
                      else {"error": f"{type(exc).__name__}: {exc}"})
            LOG.error("target could not be correlated; nothing was dispatched",
                      extra={"run_id": run.run_id, "code": code})
            self._step(run.run_id, RunEvent.TARGET_CORRELATION_FAILED, state=RunState.FAILED,
                       state_from=run.state, host_effect=str(HostEffect.NONE),
                       finished_at=_now(), release=True,
                       detail={"code": code, "change_created": False,
                               "host_effect": "none", **detail})
            raise RunRefused(code, "the target could not be identified, so nothing was applied",
                             {"run_id": run.run_id, **detail}) from None

        with self._db.transaction():
            self._append(run.run_id, RunEvent.TARGET_CORRELATED, detail={
                "action": run.preview.action,
                "expected_state": run.preview.expected_state,
                "recovery_armed": False,
                "recovery_mode": run.preview.recovery_mode,
                "change_created": False,
                "host_effect": "none",
            })
        return correlation

    def _arm(
        self, run: RunRecord, record: ObjectRecord, executor: OperationExecutor
    ) -> CheckpointRecord:
        """Write the durable rollback material. Nothing may be written before this exists.

        "Recovery is armed" means this row is on disk. Knowing the previous value in memory is
        not arming — a process that dies holding it leaves nothing behind, which is precisely
        the situation the checkpoint exists for.

        A failure here ends the Run ``failed``, before the boundary, with no Change: the
        checkpoint is the last thing that happens while it is still true that nothing about the
        host could have moved. The confirmation stays consumed — it authorised *an attempt*,
        the attempt happened, and re-usable authority after a failure is how one confirmation
        becomes two writes.
        """
        preview = run.preview
        checkpoint_id = f"ckp_{uuid.uuid4().hex}"
        try:
            correlation = executor.correlate(record)
            with self._db.transaction():
                self.checkpoints.insert({
                    "checkpoint_id": checkpoint_id,
                    "run_id": run.run_id,
                    "preview_id": preview.preview_id,
                    "host_id": run.host_id,
                    "object_id": run.object_id,
                    "intent_id": preview.intent_id,
                    "intent_version": preview.intent_version,
                    "field": preview.field,
                    "value_type": preview.value_type,
                    "before_value": encode(ValueType(preview.value_type), preview.current_value),
                    "desired_value": encode(ValueType(preview.value_type), preview.desired_value),
                    "observation_id": preview.observation_id,
                    "observed_at": preview.observed_at,
                    "protection_management_path": preview.protection_management_path,
                    "protection_evidence_id": preview.protection_evidence_id,
                    "execution_correlation": to_json(correlation),
                    "armed_at": _now(),
                })
                self._append(run.run_id, RunEvent.CHECKPOINT_WRITTEN, detail={
                    "checkpoint_id": checkpoint_id, "restores_field": preview.field,
                    "restores_value": preview.current_value,
                    "desired_value": preview.desired_value,
                    "management_path": preview.protection_management_path})
        except (ExecutionRefused, Exception) as exc:  # noqa: BLE001 - report, never proceed
            code = exc.code if isinstance(exc, ExecutionRefused) else "checkpoint_not_written"
            detail = (dict(exc.detail) if isinstance(exc, ExecutionRefused)
                      else {"error": f"{type(exc).__name__}: {exc}"})
            LOG.error("arming failed; no change was created and nothing was written",
                      extra={"run_id": run.run_id, "code": code})
            self._step(run.run_id, RunEvent.ARMING_FAILED, state=RunState.FAILED,
                       state_from=str(RunState.ARMING), host_effect=str(HostEffect.NONE),
                       finished_at=_now(), release=True,
                       detail={"code": code, "change_created": False,
                               "host_effect": "none", **detail})
            raise RunRefused(code, "recovery could not be armed, so nothing was applied",
                             {"run_id": run.run_id, **detail}) from None

        stored = self.checkpoints.for_run(run.run_id)
        assert stored is not None  # written in the transaction above
        LOG.info("recovery armed", extra={
            "run_id": run.run_id, "checkpoint_id": checkpoint_id,
            "restores_value": preview.current_value, "change_created": False})
        return stored

    def _cross(
        self,
        run: RunRecord,
        checkpoint: CheckpointRecord | None,
        correlation: dict[str, Any],
    ) -> ChangeRecord:
        """The write boundary. After this transaction a Change exists.

        Nothing has been written yet and the row says so: no dispatch marker, no outcome,
        ``host_effect`` ``none``. What it asserts is that LocalPlane has entered the path on
        which a write may occur, which is the only thing that can honestly be recorded before
        one is attempted.
        """
        change_id = f"chg_{uuid.uuid4().hex}"
        attempt_id = f"apl_{uuid.uuid4().hex}"
        preview = run.preview
        with self._db.transaction():
            self._runs.runs.set_state(run_id=run.run_id, state=str(RunState.APPLYING))
            self.changes.insert({
                "change_id": change_id,
                "run_id": run.run_id,
                "preview_id": preview.preview_id,
                "checkpoint_id": checkpoint.checkpoint_id if checkpoint else None,
                "host_id": run.host_id,
                "object_id": run.object_id,
                "operation": run.operation,
                **_change_identity_columns(preview),
                # Durable before the dispatch marker, because after that marker is written
                # a later reader has to be able to say what was addressed even if nothing
                # else was ever recorded.
                "execution_correlation": to_json(correlation),
                "created_at": _now(),
                "apply_attempt_id": attempt_id,
            })
            self._append(run.run_id, RunEvent.WRITE_BOUNDARY_CROSSED, change_id=change_id,
                         state_from=run.state, state_to=str(RunState.APPLYING),
                         detail={"change_id": change_id,
                                 "checkpoint_id": checkpoint.checkpoint_id if checkpoint else None,
                                 "recovery_armed": checkpoint is not None,
                                 "apply_attempt_id": attempt_id, "host_effect": "none"})
        LOG.info("write boundary crossed", extra={
            "run_id": run.run_id, "change_id": change_id, "object_id": run.object_id,
            **_change_log(preview), "host_effect": "none"})
        return self._require_change(change_id)

    def _dispatch(
        self, run: RunRecord, change: ChangeRecord, executor: OperationExecutor
    ) -> MutationReport:
        """Mark the dispatch durably, then send it. The order is the crash-safety argument.

        Between the commit below and the commit that records the answer there is a window in
        which a process death leaves the question open. It is not concealed and not assumed
        away: the row says dispatch began, and any later reader interprets a missing outcome as
        ``write_unknown``.
        """
        request = _apply_request(change)
        self._step(run.run_id, RunEvent.MUTATION_DISPATCHED, change_id=change.change_id,
                   change={"dispatch_began_at": _now()},
                   detail={"attempt_id": change.apply_attempt_id,
                           "kind": request.kind, "action": request.action,
                           "expected_current": request.expected_current,
                           "desired": request.desired})
        return executor.mutate(request)

    def _settle(
        self, run: RunRecord, change: ChangeRecord, report: MutationReport
    ) -> ChangeRecord:
        effect = host_effect_for(report.outcome)
        self._step(run.run_id, RunEvent.MUTATION_RESULT, change_id=change.change_id,
                   host_effect=str(effect),
                   change={"mutation_outcome": str(report.outcome),
                           "mutation_reason": report.reason,
                           "mutation_provider": report.provider,
                           "mutation_method": report.method,
                           "mutation_detail": to_json(report.detail),
                           "settled_at": _now(), "host_effect": str(effect)},
                   detail={"outcome": str(report.outcome), "reason": report.reason,
                           "provider": report.provider, "host_effect": str(effect),
                           **report.detail})
        LOG.info("mutation result recorded", extra={
            "run_id": run.run_id, "change_id": change.change_id,
            "outcome": str(report.outcome), "reason": report.reason,
            "host_effect": str(effect)})
        return self._require_change(change.change_id)

    # ------------------------------------------------------------------ connection guard

    def _arm_guard(
        self, run: RunRecord, checkpoint: CheckpointRecord | None, executor: OperationExecutor
    ) -> RunGuardRecord:
        """Establish the host-side reversal, durably, before anything can be dispatched.

        **Two commits and a request between them, in that order.** The row is inserted
        saying only that LocalPlane has *asked* — which is a crash window and is meant to be
        visible — the request goes out, and the answer is written. A Change may be created
        only once the second commit has happened, and that is enforced by a trigger reading
        `run_guards.armed_at` rather than by the order of the code: a guard LocalPlane asked
        for and did not hear back about is not a guard.

        A refusal ends the Run ``failed`` before the boundary with no Change, exactly as a
        checkpoint that cannot be written does — and it first asks the holder what actually
        happened, because an arming request that failed *after* it left this process may well
        have armed one. Leaving that running would be a reversal nobody is expecting.
        """
        assert checkpoint is not None  # a guarded change is a field change; the store agrees
        preview = run.preview
        confirmation = self.confirmations.for_run(run.run_id)
        if confirmation is None or not confirmation.consumed:
            # Unreachable through `apply`, which consumes before it prepares. Stated rather
            # than assumed because the store would refuse the row and a clear refusal here
            # is a better failure than an IntegrityError three lines later.
            raise RunRefused("confirmation_required",
                             "a guarded execution requires this run's consumed typed confirmation",
                             {"run_id": run.run_id})

        guard_id = f"grd_{uuid.uuid4().hex}"
        reversal_attempt_id = f"rev_{uuid.uuid4().hex}"
        with self._db.transaction():
            self.guards.insert({
                "guard_id": guard_id,
                "run_id": run.run_id,
                "preview_id": preview.preview_id,
                "checkpoint_id": checkpoint.checkpoint_id,
                "host_id": run.host_id,
                "object_id": run.object_id,
                "protection_management_path": checkpoint.protection_management_path,
                "protection_evidence_id": checkpoint.protection_evidence_id,
                "confirmation_id": confirmation.confirmation_id,
                "window_s": preview.guard_window_s,
                "arm_began_at": _now(),
                "reversal_attempt_id": reversal_attempt_id,
            })

        request = GuardRequest(
            guard_id=guard_id,
            attempt_id=reversal_attempt_id,
            correlation=checkpoint.execution_correlation,
            # The reversal expects to find what the change is about to leave behind, and
            # writes back what the checkpoint holds. That single line is why a guard cannot
            # undo anything but its own change.
            guarded_value=checkpoint.desired_value,
            restore_value=checkpoint.before_value,
            window_s=preview.guard_window_s,
        )
        try:
            armed: GuardArmed = executor.arm_guard(request)
        except GuardRefused as refusal:
            self._abandon_guard(run, guard_id, executor, refusal)
            raise RunRefused(
                refusal.code,
                "a connection guard could not be established, so nothing was applied",
                {"run_id": run.run_id, "guard_id": guard_id, **refusal.detail},
            ) from None

        with self._db.transaction():
            self.guards.update(guard_id, {
                "armed_at": _now(),
                "expires_at": armed.expires_at,
                "holder_id": armed.holder_id,
            })
            self._append(run.run_id, RunEvent.GUARD_ARMED, detail={
                "guard_id": guard_id,
                "holder_id": armed.holder_id,
                "expires_at": armed.expires_at,
                "window_s": preview.guard_window_s,
                "restores_value": checkpoint.before_value,
                "reversal_attempt_id": reversal_attempt_id,
                "change_created": False,
                "host_effect": "none",
                **armed.detail,
            })
        stored = self.guards.get(guard_id)
        assert stored is not None  # written in the transactions above
        LOG.info("connection guard armed", extra={
            "run_id": run.run_id, "guard_id": guard_id, "holder_id": armed.holder_id,
            "expires_at": armed.expires_at, "change_created": False})
        return stored

    def _abandon_guard(
        self, run: RunRecord, guard_id: str, executor: OperationExecutor, refusal: GuardRefused
    ) -> None:
        """A guard was asked for and refused. Find out whether one is running anyway.

        The arming request travels over a socket, so "it failed" and "it failed after the
        far side had already taken it on" are two conditions — the same distinction the
        mutating path makes, applied to the thing that would perform a mutation later. A
        guard left running for a change that will now never be dispatched would fire into a
        host nobody has changed; harmless by its own precondition, and still a reversal
        nobody is expecting.
        """
        report = executor.disarm_guard(guard_id)
        columns: dict[str, Any] = {
            "settled_at": _now(),
            "settled_phase": str(report.phase),
            "settled_reason": refusal.code,
            "settled_detail": to_json({"refusal": refusal.detail, **report.detail}),
        }
        if report.phase is GuardPhase.DISARMED and report.holder_id:
            # It had been armed after all, and has now been released. Recorded as what it
            # was rather than as the refusal LocalPlane thought it got.
            columns["armed_at"] = _now()
            columns["expires_at"] = report.detail.get("expires_at") or _now()
            columns["holder_id"] = report.holder_id
        if report.phase is GuardPhase.FIRED:
            columns["fired_at"] = report.fired_at or _now()
            columns["reversal_outcome"] = str(
                report.mutation.outcome if report.mutation else MutationOutcome.WRITE_UNKNOWN)
            columns["reversal_reason"] = report.mutation.reason if report.mutation else None
            columns["armed_at"] = columns.get("armed_at") or _now()
            columns["expires_at"] = report.detail.get("expires_at") or _now()
            columns["holder_id"] = report.holder_id or "unknown"
        with self._db.transaction():
            self.guards.update(guard_id, columns)
        self._step(run.run_id, RunEvent.GUARD_ARMING_FAILED, state=RunState.FAILED,
                   state_from=run.state, host_effect=str(HostEffect.NONE),
                   finished_at=_now(), release=True,
                   detail={"code": refusal.code, "guard_id": guard_id,
                           "guard_phase": str(report.phase), "change_created": False,
                           "host_effect": "none", **refusal.detail})
        LOG.error("connection guard could not be armed; nothing was applied", extra={
            "run_id": run.run_id, "guard_id": guard_id, "code": refusal.code,
            "guard_phase": str(report.phase)})

    def _hold(
        self, run: RunRecord, change: ChangeRecord, guard: RunGuardRecord
    ) -> tuple[RunRecord, ChangeRecord]:
        """Enter ``guarded``: written, verified, and waiting on the connection itself.

        **Nothing here resolves on a timer in this process.** The deadline belongs to the
        component holding the guard, which is on the host and outlives this one. What the
        backend does is record that the wait has begun and answer the request; the next
        thing that moves this Run is an operator proving the path still works, or the guard
        acting on its own.
        """
        self._step(run.run_id, RunEvent.GUARD_HOLD_STARTED, change_id=change.change_id,
                   state=RunState.GUARDED, state_from=run.state,
                   detail={"guard_id": guard.guard_id, "expires_at": guard.expires_at,
                           "window_s": guard.window_s, "holder_id": guard.holder_id,
                           "restores_value": change.before_value,
                           "keeps_value": change.desired_value})
        LOG.warning("guarded change is holding", extra={
            "run_id": run.run_id, "change_id": change.change_id, "guard_id": guard.guard_id,
            "expires_at": guard.expires_at, "host_effect": change.host_effect})
        return self._reread(run), self._require_change(change.change_id)

    def _release_guard(
        self,
        run: RunRecord,
        guard: RunGuardRecord | None,
        executor: OperationExecutor,
        reason: str,
        change_id: str | None = None,
    ) -> GuardReport | None:
        """Settle a guard whose Run has ended some other way, and record what it did.

        Called **after** the ending it belongs to, never before. If a restoration is going to
        happen it happens first, so that a process death in the middle leaves the guard still
        holding — which is the whole reason it is there. Two mechanisms aiming at the same
        value is not a hazard here: both write what the checkpoint holds, the privileged path
        serialises them, and whichever arrives second finds its own precondition no longer
        true and writes nothing.
        """
        if guard is None:
            return None
        report = executor.disarm_guard(guard.guard_id)
        self._record_guard_settlement(run, guard, report, reason, change_id=change_id)
        return report

    def _record_guard_settlement(
        self,
        run: RunRecord,
        guard: RunGuardRecord,
        report: GuardReport,
        reason: str,
        change_id: str | None = None,
    ) -> None:
        """Write what became of a guard, once, and put it in the transcript.

        The reversal is recorded in the same three-valued vocabulary every other write on
        this path uses, and it is never derived from what the target holds afterwards — the
        same rule the apply's own outcome follows, for the same reason.
        """
        columns: dict[str, Any] = {
            "settled_at": _now(),
            "settled_phase": str(report.phase),
            "settled_reason": report.reason or reason,
            "settled_detail": to_json({"settled_because": reason, **report.detail}),
        }
        if report.phase is GuardPhase.FIRED:
            outcome = report.mutation.outcome if report.mutation else MutationOutcome.WRITE_UNKNOWN
            columns["fired_at"] = report.fired_at or _now()
            columns["reversal_outcome"] = str(outcome)
            columns["reversal_reason"] = report.mutation.reason if report.mutation else None
        with self._db.transaction():
            self.guards.update(guard.guard_id, columns)
            self._append(run.run_id, RunEvent.GUARD_SETTLED, change_id=change_id,
                         detail={"guard_id": guard.guard_id, "phase": str(report.phase),
                                 "settled_because": reason,
                                 "reversal_outcome": columns.get("reversal_outcome"),
                                 "reversal_reason": columns.get("reversal_reason"),
                                 "fired_at": columns.get("fired_at")})
        LOG.info("connection guard settled", extra={
            "run_id": run.run_id, "guard_id": guard.guard_id, "phase": str(report.phase),
            "settled_because": reason,
            "reversal_outcome": columns.get("reversal_outcome")})

    # -------------------------------------------------------------------- verification

    def _verify(
        self, run: RunRecord, change: ChangeRecord, executor: OperationExecutor
    ) -> tuple[RunRecord, ChangeRecord, _ReadBack]:
        """Prove the wanted value with a fresh reading, or say why it could not be proved.

        A kernel acknowledgement is not success. It says a request was accepted; it does not
        say the object now holds the value, that it is still the object that was meant, or that
        the value can be read at all. Only an observation through the ordinary path answers
        those, and only that observation may be cited as the proof.
        """
        expectation = _applied_expectation(change)
        self._step(run.run_id, RunEvent.VERIFICATION_STARTED, change_id=change.change_id,
                   state=RunState.VERIFYING, state_from=str(RunState.APPLYING),
                   detail={"expect": expectation.value if expectation.value is not None
                           else expectation.state,
                           "field": expectation.field, "kind": expectation.kind})
        read = self._read_back(run, change, executor, expectation)
        self._step(run.run_id, RunEvent.VERIFICATION_RESULT, change_id=change.change_id,
                   change=_verification_columns("verification", change, read),
                   detail={"outcome": str(read.outcome), "reason": read.reason,
                           "observation_id": read.observation_id, "observed": read.observed,
                           "expected": expectation.value if expectation.value is not None
                           else expectation.state})
        return self._reread(run), self._require_change(change.change_id), read

    def _read_back(
        self, run: RunRecord, change: ChangeRecord, executor: OperationExecutor,
        expectation: Expectation,
    ) -> _ReadBack:
        """Re-observe the target, check the reading is the right kind, and hand it over.

        Three things happen here and only three, because only three are the engine's. The
        reading is taken through the ordinary observation path; it is checked to have come
        from the capability and provider the **published plan** said a verification would use,
        because a reading from a different source is not the same fact and that rule must not
        get weaker at the moment it matters most; and then the operation is asked whether it
        proves the expectation.

        *What counts as proof* is deliberately not decided here. Comparing one integer and
        establishing that a restart happened are different questions, and an engine that
        knew the difference would be an engine that knows what it is changing.
        """
        record = self._runs.objects.get(run.object_id)
        attempt = (executor.observe(record) if record is not None
                   else ObservationAttempt(record=None, failure="target_absent"))
        if not attempt.taken:
            failure = attempt.failure or "observation_unavailable"
            return _ReadBack(
                VerificationOutcome.TARGET_ABSENT if failure == "target_absent"
                else VerificationOutcome.OBSERVATION_UNAVAILABLE, reason=failure)

        assert attempt.record is not None and attempt.record.observation is not None
        observation = attempt.record.observation
        preview = run.preview
        if (observation.capability != preview.verification_capability
                or observation.provider != preview.verification_provider):
            return _ReadBack(VerificationOutcome.SOURCE_INCOMPATIBLE, observation.observation_id,
                             reason="observation_source_incompatible")

        proof = executor.prove(observation.facts, expectation)
        return _ReadBack(proof.outcome, observation.observation_id, proof.value, proof.state,
                         proof.reason)

    # ------------------------------------------------------------------------ rollback

    def _unproven(
        self, run: RunRecord, change: ChangeRecord, checkpoint: CheckpointRecord | None,
        executor: OperationExecutor, read: _ReadBack | None,
    ) -> tuple[RunRecord, ChangeRecord]:
        """The host may have moved and LocalPlane cannot prove the end state it wanted.

        Two answers, and which one applies is decided by whether this operation has an
        inverse LocalPlane may perform — not by how bad the situation is.

        **A field change is restored.** The previous value is a verified scalar, writing it
        back is a complete reversal, and the checkpoint holding it was on disk before
        anything could be written.

        **An action is not.** There is no previous value, and the opposite verb is not its
        inverse: issuing ``stop`` because a ``start`` could not be verified is a second
        change nobody asked for, against a resource whose state is already not what was
        expected, and it can make the situation worse — a container that is crash-looping
        does not want another instruction, it wants somebody to look at it. So the Run ends
        ``recovery_required`` with a typed reason, the record says what was asked for and
        what was observed instead, and the object's write lock stays held.
        """
        if checkpoint is not None:
            return self._roll_back(run, change, checkpoint, executor)
        if read is None:
            # write_unknown: the daemon may have carried the action out and nothing can say.
            return self._recover(run, change, RecoveryReason.APPLY_WRITE_UNKNOWN,
                                 dict(change.mutation_detail))
        reason = (
            RecoveryReason.ACTION_NOT_PROVEN
            if read.outcome is VerificationOutcome.MISMATCH
            else RecoveryReason.TARGET_ABSENT_AFTER_MUTATION
            if read.outcome is VerificationOutcome.TARGET_ABSENT
            else RecoveryReason.ACTION_VERIFICATION_UNAVAILABLE
        )
        return self._recover(run, change, reason, {
            "action": change.action, "expected_state": change.expected_state,
            "observed_state": read.state, "reason": read.reason,
            "observation_id": read.observation_id,
        })

    def _roll_back(
        self, run: RunRecord, change: ChangeRecord, checkpoint: CheckpointRecord,
        executor: OperationExecutor,
    ) -> tuple[RunRecord, ChangeRecord]:
        """Put the checkpoint's value back, through the same privileged path, and prove it.

        The same path deliberately: a second executor would be a second thing that can write,
        with its own preconditions, its own failure modes and its own chance to disagree with
        the first about what happened.

        It begins with a reading rather than an assumption. After ``write_unknown`` nobody
        knows what the target holds, and after a failed verification the value is whatever the
        verification found — so the restoration's own precondition is taken from a fresh
        reading, and the privileged path checks it against the kernel again before it writes.

        ``rolled_back`` requires a reading that proves the checkpoint's value. An
        acknowledgement is not a restoration, and a restoration nobody has read back is a claim.
        """
        self._step(run.run_id, RunEvent.ROLLBACK_STARTED, change_id=change.change_id,
                   state=RunState.ROLLING_BACK, state_from=run.state,
                   change={"rollback_required": 1},
                   detail={"restores_field": checkpoint.field,
                           "restores_value": checkpoint.before_value,
                           "after_outcome": change.mutation_outcome,
                           "after_verification": change.verification_outcome})
        run = self._reread(run)

        # What does the target hold now? Not an assumption — after write_unknown nothing is
        # known, and the restoration needs a precondition it can defend.
        current = self._read_back(run, change, executor, _restored_expectation(change))
        if current.value is None:
            reason = (RecoveryReason.TARGET_ABSENT_AFTER_MUTATION
                      if current.outcome is VerificationOutcome.TARGET_ABSENT
                      else RecoveryReason.PRE_ROLLBACK_READ_UNAVAILABLE)
            return self._recover(run, change, reason, {"reason": current.reason})

        rollback_attempt_id = f"rbk_{uuid.uuid4().hex}"
        self._step(run.run_id, RunEvent.ROLLBACK_MUTATION_DISPATCHED, change_id=change.change_id,
                   change={"rollback_attempt_id": rollback_attempt_id,
                           "rollback_dispatch_began_at": _now()},
                   detail={"attempt_id": rollback_attempt_id, "expected_current": current.value,
                           "desired": checkpoint.before_value})
        report = executor.mutate(MutationRequest(
            kind=CHANGE_KIND_FIELD, attempt_id=rollback_attempt_id,
            correlation=change.execution_correlation,
            expected_current=current.value, desired=checkpoint.before_value))
        self._step(run.run_id, RunEvent.ROLLBACK_MUTATION_RESULT, change_id=change.change_id,
                   change={"rollback_outcome": str(report.outcome),
                           "rollback_reason": report.reason,
                           "rollback_detail": to_json(report.detail)},
                   detail={"outcome": str(report.outcome), "reason": report.reason,
                           "provider": report.provider, **report.detail})
        change = self._require_change(change.change_id)

        if report.outcome is MutationOutcome.WRITE_UNKNOWN:
            # An interrupted restoration is not a restoration, and no reading could add
            # anything: the question is what LocalPlane did, not what is there.
            return self._recover(run, change, RecoveryReason.ROLLBACK_WRITE_UNKNOWN,
                                 dict(report.detail))

        self._step(run.run_id, RunEvent.ROLLBACK_VERIFICATION_STARTED, change_id=change.change_id,
                   state=RunState.ROLLBACK_VERIFYING, state_from=str(RunState.ROLLING_BACK),
                   detail={"expect": checkpoint.before_value, "field": checkpoint.field})
        run = self._reread(run)
        read = self._read_back(run, change, executor, _restored_expectation(change))
        self._step(run.run_id, RunEvent.ROLLBACK_VERIFICATION_RESULT, change_id=change.change_id,
                   change=_verification_columns("rollback_verification", change, read),
                   detail={"outcome": str(read.outcome), "reason": read.reason,
                           "observation_id": read.observation_id, "observed": read.value,
                           "expected": checkpoint.before_value})
        run, change = self._reread(run), self._require_change(change.change_id)

        if read.outcome.proved:
            return self._finish(run, change, RunState.ROLLED_BACK, ChangeResult.ROLLED_BACK)
        return self._recover(run, change, _recovery_reason(read.outcome, report),
                             {"reason": read.reason})

    # -------------------------------------------------------------------------- endings

    def _recover(
        self, run: RunRecord, change: ChangeRecord, reason: RecoveryReason,
        detail: dict[str, Any],
    ) -> tuple[RunRecord, ChangeRecord]:
        """LocalPlane cannot prove a safe final state. Say so, and hold the object.

        The lock is deliberately not released: the target's value is unproven, and letting a
        second change start against it would be building on a foundation nobody has checked.
        That is a domain hold: here it is a row that outlives the run.
        """
        self._step(run.run_id, RunEvent.RECOVERY_REQUIRED, change_id=change.change_id,
                   detail={"reason": str(reason), **detail})
        LOG.error("recovery required", extra={
            "run_id": run.run_id, "change_id": change.change_id, "reason": str(reason),
            "mutation_outcome": change.mutation_outcome,
            "rollback_outcome": change.rollback_outcome, "host_effect": change.host_effect})
        return self._finish(run, change, RunState.RECOVERY_REQUIRED,
                            ChangeResult.RECOVERY_REQUIRED, recovery=reason)

    def _finish(
        self, run: RunRecord, change: ChangeRecord, state: RunState, result: ChangeResult,
        recovery: RecoveryReason | None = None, detail: dict[str, Any] | None = None,
    ) -> tuple[RunRecord, ChangeRecord]:
        """End the Run and the Change together. ``detail`` says what settled it, if anything.

        The extra detail exists for the endings nobody was present for: a settlement on
        restart has to be able to say so in the transcript, and the vocabulary of events is
        closed, so the fact travels beside ``run_finished`` rather than as an event of its
        own. The result and the host effect are this method's own and are never overridden.
        """
        settled = self._require_change(change.change_id)
        self._step(
            run.run_id, RunEvent.RUN_FINISHED, change_id=change.change_id, state=state,
            state_from=run.state, host_effect=settled.host_effect, finished_at=_now(),
            release=recovery is None,
            change={"result": str(result), "recovery_reason": str(recovery) if recovery else None,
                    "finished_at": _now()},
            detail={**(detail or {}),
                    "result": str(result), "host_effect": settled.host_effect,
                    "recovery_reason": str(recovery) if recovery else None},
        )
        LOG.info("run finished", extra={
            "run_id": run.run_id, "change_id": change.change_id, "state": str(state),
            "result": str(result), "host_effect": settled.host_effect,
            "lock_released": recovery is None})
        return self._reread(run), self._require_change(change.change_id)

    # ------------------------------------------------------------------- recovery: reading

    def recovery_hold(self, change: ChangeRecord) -> RecoveryHold:
        """Whether this Change is still holding its object, and how that ended if it has.

        **Derived, never stored.** The hold *is* the durable write lock plus the append-only
        attempt history; a column restating it would be a second thing to keep in step, and
        the first time the two disagreed a reader would have no way to tell which was right.

        ``resolved`` says the hold was released. It does not say the Change succeeded, and
        the Change goes on saying ``recovery_required`` for as long as it exists — because
        that is what happened, and a later act by an operator does not change it.
        """
        if not change.recovery_required:
            return RecoveryHold(state=RecoveryHoldState.NOT_REQUIRED, reason=None,
                                object_write_locked=self.locks.for_run(change.run_id) is not None)
        release = self.recovery_attempts.release_of(change.change_id)
        return RecoveryHold(
            state=(RecoveryHoldState.RESOLVED if release is not None
                   else RecoveryHoldState.UNRESOLVED),
            reason=change.recovery_reason,
            object_write_locked=self.locks.for_run(change.run_id) is not None,
            released_at=release.finished_at if release else None,
            released_by=release.kind if release else None,
            released_by_attempt_id=release.attempt_id if release else None,
            released_outcome=release.outcome if release else None,
        )

    def recovery_history(self, change: ChangeRecord) -> list[RecoveryAttemptRecord]:
        return self.recovery_attempts.for_change(change.change_id, change.value_type)

    def recovery_states(self, changes: list[ChangeRecord]) -> dict[str, RecoveryHoldState]:
        """The hold state of a page of Changes, in one query rather than one each."""
        released = self.recovery_attempts.released_change_ids(
            [c.change_id for c in changes if c.recovery_required])
        return {
            change.change_id: (
                RecoveryHoldState.NOT_REQUIRED if not change.recovery_required
                else RecoveryHoldState.RESOLVED if change.change_id in released
                else RecoveryHoldState.UNRESOLVED
            )
            for change in changes
        }

    def recovery_confirmation_for(self, change: ChangeRecord) -> ConfirmationRecord | None:
        """The recovery authority waiting to be used, if a person has granted one."""
        return self.confirmations.outstanding(
            change.run_id, CONFIRMATION_PURPOSE_RECOVERY_RETRY)

    # -------------------------------------------------------------- recovery: confirmation

    def recovery_confirm(
        self,
        change: ChangeRecord,
        *,
        acknowledge: bool,
        expected_recovery_reason: str | None,
        management_path: ManagementPathVerdict,
    ) -> ConfirmationRecord:
        """Authorise one recovery retry to dispatch a *new* mutation.

        **The confirmation that authorised the original apply is not reusable and this is
        not it.** That one authorised an attempt; the attempt happened. This is a second,
        separate grant of authority for a second, separate write — recorded in the same
        table, under the same single-use rule, with the same immutability triggers and the
        same consuming UPDATE. A parallel confirmation system beside the existing one would
        be two implementations of the most safety-critical rule in the product.

        **At most one may be outstanding.** A partial unique index refuses a second while one
        is waiting, so authority cannot be accumulated and then spent all at once.

        **A retry does not need this to begin.** A retry first asks whether a fresh reading
        already proves the end state, and one that does completes without writing and without
        consuming anything. This exists for the case where it does not.

        The required method is the one the *published plan* named, because that is the
        document whose operation is being re-attempted and what its operator was told. It is
        re-derived under current policy at dispatch time as well, and a retry whose policy now
        demands more than this row carries is refused there — so recording it here can only be
        as strong as today's rule, never weaker.
        """
        run = self._recovery_run(change)
        self._require_open_hold(change, run)
        if expected_recovery_reason not in (None, change.recovery_reason):
            raise RunRefused("recovery_reason_mismatch",
                             "the recovery you are confirming is not the one this change holds",
                             {"expected": expected_recovery_reason,
                              "recovery_reason": change.recovery_reason})
        self._require_executable_preview(run)
        definition = self._runs.definition(OperationType(run.operation))
        self._require_path_evidence(run, definition, management_path)

        required = ConfirmationMethod(run.preview.confirmation_method)
        if required is not ConfirmationMethod.ACKNOWLEDGE:
            raise RunRefused("confirmation_method_unsupported",
                             "this plan requires a confirmation method this build does not accept",
                             {"required_method": str(required),
                              "accepted": str(ConfirmationMethod.ACKNOWLEDGE)})
        if not acknowledge:
            raise RunRefused("confirmation_not_acknowledged",
                             "the confirmation was not acknowledged, so nothing was recorded",
                             {"change_id": change.change_id})

        confirmation_id = f"cnf_{uuid.uuid4().hex}"
        with self._db.transaction():
            outstanding = self.confirmations.outstanding(
                run.run_id, CONFIRMATION_PURPOSE_RECOVERY_RETRY)
            if outstanding is not None:
                raise RunRefused(
                    "recovery_confirmation_already_satisfied",
                    "this recovery already carries authority that has not been used",
                    {"change_id": change.change_id,
                     "confirmation_id": outstanding.confirmation_id})
            self.confirmations.insert({
                "confirmation_id": confirmation_id,
                "run_id": run.run_id,
                "purpose": CONFIRMATION_PURPOSE_RECOVERY_RETRY,
                "preview_id": run.preview.preview_id,
                "preview_digest": run.preview.preview_digest,
                "digest_version": run.preview.digest_version,
                "required_method": str(required),
                "method": str(ConfirmationMethod.ACKNOWLEDGE),
                # A recovery grant is an acknowledgement and carries no typed statement.
                # The typed method belongs to a guarded *apply*, where what an operator
                # writes is evidence about the object they looked at; a retry re-attempts an
                # end state the Change already records and has no second object to name.
                "typed_statement": None,
                "policy": run.preview.confirmation_policy,
                "source": CONFIRMATION_SOURCE_AUTHENTICATED,
                "satisfied_at": _now(),
            })
            self._append(run.run_id, RunEvent.RECOVERY_CONFIRMATION_SATISFIED,
                         change_id=change.change_id,
                         detail={"confirmation_id": confirmation_id,
                                 "recovery_reason": change.recovery_reason,
                                 "method": str(ConfirmationMethod.ACKNOWLEDGE),
                                 "source": CONFIRMATION_SOURCE_AUTHENTICATED,
                                 "host_effect": "none"})
            stored = self.confirmations.outstanding(
                run.run_id, CONFIRMATION_PURPOSE_RECOVERY_RETRY)
            assert stored is not None  # written in this transaction

        LOG.info("recovery confirmation satisfied", extra={
            "change_id": change.change_id, "run_id": run.run_id,
            "confirmation_id": confirmation_id, "host_effect": "none"})
        return stored

    # --------------------------------------------------------------------- recovery: retry

    def recovery_retry(
        self,
        change: ChangeRecord,
        management_path: ManagementPathVerdict,
        systemd_lifecycle_context: SystemdLifecycleContext | None = None,
    ) -> RecoveryOutcome:
        """Try again to reach the end state the original Change wanted. Operator-initiated.

        **It belongs to the original Change.** The operation, the target and the end state
        are the ones already recorded; nothing here can substitute today's intent, today's
        value or a different verb, and a re-plan that no longer produces the same intended
        outcome is a refusal rather than a silent substitution.

        **The evidence is this request's, not the failed one's.** The management-path
        judgement comes from the transport this call arrived on, the reading comes from a
        fresh sweep through the ordinary observation path, and the gates are re-derived
        against current truth.

        **Proof comes before writing.** The first thing a retry does — after the gates and
        before anything can be dispatched — is ask the *operation* whether a fresh reading
        already proves the end state it wanted. If it does, recovery completes with no host
        mutation at all. A retry that wrote merely because somebody clicked Retry would be a
        host change nobody needed and nobody asked for.

        **A retry that writes is a new write attempt** and needs authority that has not been
        used. The confirmation consumed by the original apply does not qualify and cannot be
        reached: it is a different row with a different purpose and it is already consumed.

        The hold is kept through every ending but two. A refusal wrote nothing and provably
        could not have; ``not_written``, ``write_unknown`` and ``not_proven`` leave the same
        question open that was open before, or a worse one.
        """
        run = self._recovery_run(change)
        executor = self._executor(run)
        self._require_open_hold(change, run)
        record = self._require_target(run)
        self._require_executable_preview(run)
        definition = self._runs.definition(OperationType(run.operation))
        relation = self._require_path_evidence(run, definition, management_path)

        attempt = self._open_recovery_attempt(
            change, run, RecoveryActionKind.RETRY, relation, management_path)

        # 1 — what does the target hold now, and does the operation call that proof?
        read = self._read_back(run, change, executor, _applied_expectation(change))
        attempt = self._recovery_evidence(run, change, attempt, read)
        if read.outcome.proved:
            return self._complete_recovery(
                run, change, attempt, RecoveryAttemptOutcome.PROVEN,
                detail={"proved_without_mutation": True,
                        "observation_id": read.observation_id, "observed": read.observed})

        # 2 — it is not proven. May the original mutation be attempted again, now? Every
        # step here can only refuse, and every refusal leaves the attempt settled as one:
        # nothing has been dispatched, and after the marker below that stops being true.
        #
        # The object is re-read first, so that the re-plan and the correlation both see the
        # reading just taken rather than the one this call started from. A hold normally
        # outlives the freshness horizon by a long way — a person has to look at it — so
        # re-planning against the record as it was before the sweep would refuse a perfectly
        # good retry with `observation_stale`, having just refreshed the very observation it
        # was complaining about.
        record = self._require_target(run)
        try:
            plan = self._recovery_plan_now(
                run, record, management_path, change,
                systemd_lifecycle_context=systemd_lifecycle_context)
            request = self._recovery_request(run, change, record, executor, read, plan)
            self._recovery_dispatch_marker(run, change, attempt, request)
        except RunRefused as refusal:
            raise self._refuse_recovery(run, change, attempt, refusal) from None

        # 3 — authority is spent and the dispatch is on the record. Send it.
        report = executor.mutate(request)
        attempt = self._recovery_result(run, change, attempt, report)

        if report.outcome is not MutationOutcome.WRITTEN:
            return self._settle_retry(
                run, change, attempt,
                recovery_outcome_for(report.outcome, VerificationOutcome.NOT_ATTEMPTED))

        verified = self._read_back(
            run, change, executor,
            _applied_expectation(change, dict(request.correlation)))
        attempt = self._recovery_verification(run, change, attempt, verified)
        outcome = recovery_outcome_for(MutationOutcome.WRITTEN, verified.outcome)
        if outcome is RecoveryAttemptOutcome.VERIFIED:
            return self._complete_recovery(
                run, change, attempt, outcome,
                detail={"proved_without_mutation": False,
                        "observation_id": verified.observation_id,
                        "observed": verified.observed})
        return self._settle_retry(run, change, attempt, outcome)

    # ------------------------------------------------------------------- recovery: resolve

    def recovery_resolve(
        self,
        change: ChangeRecord,
        *,
        acknowledge: bool,
        operator_statement: str,
        object_name: str,
        note: str | None,
        expected_recovery_reason: str | None,
    ) -> RecoveryOutcome:
        """A person says they have handled this, and LocalPlane gives the object back.

        **It performs no host mutation, and that is a CHECK rather than a code path.** A
        resolution row may carry no confirmation, no attempt id, no dispatch marker, no
        mutation outcome and no host effect; a build that decided otherwise would fail to
        store the row.

        **It claims nothing.** Not that the original Change succeeded, not that the mutation
        happened, not that the host is safe, not that anything was rolled back. The original
        result and the original recovery reason are untouched and stay inspectable, and the
        only new claim on the record is that at this time a person released the hold.

        **Whatever can be observed is recorded beside it**, through the ordinary observation
        path, and it is recorded as what it is. If the reading happens to prove the end state
        the Change wanted, the row says so and the outcome is still ``resolved`` — a human's
        decision is not silently upgraded into a verification. If nothing can be read, that
        stays visible as ``observation_unavailable`` rather than becoming an absence a reader
        could mistake for a clean result.

        **The act is deliberate.** The operator types the held domain's name; here it
        is the object's, under the identity this build actually holds. An escape from a safety
        hold should not be one accidental click.
        """
        run = self._recovery_run(change)
        self._require_open_hold(change, run)
        if expected_recovery_reason not in (None, change.recovery_reason):
            raise RunRefused("recovery_reason_mismatch",
                             "the recovery you are resolving is not the one this change holds",
                             {"expected": expected_recovery_reason,
                              "recovery_reason": change.recovery_reason})
        if not acknowledge:
            raise RunRefused(
                "recovery_resolution_not_acknowledged",
                "releasing a recovery hold is an explicit act and this one was not acknowledged",
                {"change_id": change.change_id})
        if operator_statement.strip() != object_name:
            raise RunRefused(
                "recovery_resolution_object_mismatch",
                "type the object's name to record that you have dealt with this yourself",
                {"change_id": change.change_id, "object_id": change.object_id})

        # A read, never a write. It may fail, and a failure is recorded as one.
        read = self._observe_for_recovery(run, change)

        attempt_id = f"rcv_{uuid.uuid4().hex}"
        now = _now()
        with self._db.transaction():
            self._require_open_hold_locked(change, run)
            row = {
                "attempt_id": attempt_id,
                **_recovery_identity(change, run,
                                     self.recovery_attempts.next_sequence(change.change_id)),
                "kind": str(RecoveryActionKind.RESOLVE),
                "started_at": now,
                "protection_management_path": str(ManagementPathRelation.UNKNOWN),
                "protection_evidence_id": None,
                **_recovery_evidence_columns(change, read),
                "outcome": str(RecoveryAttemptOutcome.RESOLVED),
                "releases_hold": 1,
                "finished_at": now,
                "operator_statement": operator_statement.strip(),
                "note": note,
            }
            self.recovery_attempts.insert(row)
            self._append(run.run_id, RunEvent.RECOVERY_RESOLVED, change_id=change.change_id,
                         occurred_at=now,
                         detail={"attempt_id": attempt_id,
                                 "recovery_reason": change.recovery_reason,
                                 "evidence_outcome": str(read.outcome),
                                 "observation_id": read.observation_id,
                                 "observed": read.observed,
                                 "evidence_proves_intended_state": read.outcome.proved,
                                 "operator_statement": operator_statement.strip(),
                                 "host_effect": "none", "host_mutated": False})
            self._append(run.run_id, RunEvent.RECOVERY_HOLD_RELEASED, change_id=change.change_id,
                         occurred_at=now,
                         detail={"attempt_id": attempt_id,
                                 "released_by": str(RecoveryActionKind.RESOLVE),
                                 "object_id": change.object_id,
                                 "field": _lock_aspect(run),
                                 "host_effect": "none"})
            self.locks.release(run.run_id)

        LOG.warning("recovery resolved by an operator; the hold is released", extra={
            "change_id": change.change_id, "run_id": run.run_id, "attempt_id": attempt_id,
            "recovery_reason": change.recovery_reason,
            "evidence_outcome": str(read.outcome),
            "host_effect": "none", "host_mutated": False})
        return self._recovery_outcome(change, attempt_id)

    # --------------------------------------------------- recovery: interruption on restart

    def settle_interrupted_recovery(
        self, host_id: str | None = None
    ) -> list[RecoveryAttemptRecord]:
        """Interpret every recovery attempt that began and never recorded what became of it.

        The recovery path has the same one-transaction crash window the apply path has, and
        it is read by the same rule — :func:`outcome_on_recovery`, the *same function*, given
        the same two facts:

        * **a recorded outcome is the answer.** It was written by the path that knew, and a
          restart is not entitled to a second opinion about it. A durable ``written`` or
          ``not_written`` survives untouched, with its reason, provider, method, detail,
          host effect and any verification evidence already beside it;
        * **dispatch began and nothing recorded an outcome** — ``write_unknown``;
        * **dispatch never began** — no outcome at all, and ``none``, which is a proof rather
          than an assumption.

        Downgrading a known fact to ``write_unknown`` because the process happened to die
        later would be the same lie in the other direction: it would take something LocalPlane
        had established and report it as unestablished, and an operator reading the record
        afterwards could not tell which of the two had happened.

        **The hold is kept in every case, and nothing is retried.** A record nobody has looked
        at since a crash is not authority to write to a host, and the operator evidence every
        write on this path requires belongs to a request, which a restart does not have.
        """
        settled: list[RecoveryAttemptRecord] = []
        for attempt in self.recovery_attempts.unsettled(host_id):
            recorded = (MutationOutcome(attempt.mutation_outcome)
                        if attempt.mutation_outcome else None)
            now = _now()
            columns: dict[str, Any] = {
                "outcome": str(RecoveryAttemptOutcome.INTERRUPTED),
                "refusal_code": None,
                "finished_at": now,
            }
            if recorded is None and attempt.dispatch_began:
                # The window this exists for: the request may have left and nothing came
                # back. Everything else about the attempt is already true and stays as it is.
                outcome = outcome_on_recovery(True, None)
                columns.update({
                    "mutation_outcome": str(outcome),
                    "mutation_reason": "interrupted_before_a_result_was_recorded",
                    "mutation_detail": to_json({"settled_by": "recovery_on_restart"}),
                    "host_effect": str(host_effect_for(outcome)),
                })
            with self._db.transaction():
                self.recovery_attempts.update(attempt.attempt_id, columns)
                self._append(
                    attempt.run_id, RunEvent.RECOVERY_RETRY_FINISHED,
                    change_id=attempt.change_id, occurred_at=now,
                    detail={"attempt_id": attempt.attempt_id,
                            "outcome": str(RecoveryAttemptOutcome.INTERRUPTED),
                            "dispatch_began": attempt.dispatch_began,
                            "mutation_outcome": columns.get(
                                "mutation_outcome", attempt.mutation_outcome),
                            "mutation_outcome_preserved": recorded is not None,
                            "verification_outcome": attempt.verification_outcome,
                            "settled_by": "recovery_on_restart",
                            "hold_released": False})
            LOG.warning("interrupted recovery attempt settled; the hold is kept", extra={
                "attempt_id": attempt.attempt_id, "change_id": attempt.change_id,
                "run_id": attempt.run_id, "dispatch_began": attempt.dispatch_began,
                "mutation_outcome_preserved": recorded is not None,
                "outcome": str(RecoveryAttemptOutcome.INTERRUPTED)})
            reread = self.recovery_attempts.get(attempt.attempt_id, self._value_type_of(attempt))
            assert reread is not None  # updated in the transaction above
            settled.append(reread)
        return settled

    def _value_type_of(self, attempt: RecoveryAttemptRecord) -> str | None:
        """How to read this attempt's observed values back, from the Change it belongs to."""
        change = self.changes.get(attempt.change_id)
        return change.value_type if change is not None else None

    # ------------------------------------------------------------------ recovery: internals

    def _recovery_run(self, change: ChangeRecord) -> RunRecord:
        run = self._runs.runs.get(change.run_id)
        if run is None:
            raise RunRefused("run_not_found", "the run this change belongs to is gone",
                             {"change_id": change.change_id, "run_id": change.run_id})
        return run

    def _require_open_hold(
        self, change: ChangeRecord, run: RunRecord, ignore_attempt_id: str | None = None
    ) -> WriteLockRecord:
        """Recovery is what this Change ended in, nobody has released it, and it still holds.

        Four separate refusals because they send an operator to four different places: a
        Change that never required recovery has nothing to recover; one already resolved has
        been dealt with; one whose lock is gone is a Change whose hold somebody removed
        outside this path, which is a situation LocalPlane will not silently paper over by
        acting as though it still owned the object; and one with an attempt already under way
        is a request that has to wait rather than a request that has to be refused for ever.
        """
        if not change.recovery_required:
            raise RunRefused(
                "change_not_in_recovery",
                "this change did not end in recovery, so there is no recovery to act on",
                {"change_id": change.change_id, "result": change.result})
        released = self.recovery_attempts.release_of(change.change_id)
        if released is not None:
            raise RunRefused(
                "recovery_already_resolved",
                "this recovery hold has already been released",
                {"change_id": change.change_id, "attempt_id": released.attempt_id,
                 "released_by": released.kind, "released_at": released.finished_at})
        lock = self.locks.for_run(run.run_id)
        if lock is None or lock.object_id != change.object_id:
            raise RunRefused(
                "recovery_hold_not_held",
                "this change no longer holds the object it was recovering",
                {"change_id": change.change_id, "run_id": run.run_id,
                 "object_id": change.object_id})
        in_flight = self.recovery_attempts.in_flight(change.change_id)
        if in_flight is not None and in_flight.attempt_id != ignore_attempt_id:
            raise RunRefused(
                "recovery_attempt_in_flight",
                "a recovery attempt against this change is already under way",
                {"change_id": change.change_id, "attempt_id": in_flight.attempt_id,
                 "started_at": in_flight.started_at})
        return lock

    def _require_open_hold_locked(
        self, change: ChangeRecord, run: RunRecord, ignore_attempt_id: str | None = None
    ) -> None:
        """The same questions, re-asked inside the transaction that is about to act on them.

        The checks above answer them for a caller; these answer them for the write. Between
        the two, another request may have resolved the hold, and the structural indexes would
        refuse the row anyway — this turns that into a typed refusal instead of an integrity
        error. ``ignore_attempt_id`` is this attempt's own: it is in flight by definition, and
        a step of it must not read itself as somebody else's.
        """
        self._require_open_hold(
            self._require_change(change.change_id), run, ignore_attempt_id)

    def _require_path_evidence(
        self,
        run: RunRecord,
        definition: Any,
        management_path: ManagementPathVerdict,
    ) -> ManagementPathRelation:
        """The current structural gate, against evidence belonging to *this* request.

        Asked once, at the start, and of the whole action rather than only of the branch that
        turns out to write. Whether a retry writes is not known until after a fresh reading
        has been taken and judged; a gate applied only on the writing branch would therefore
        be a gate whose applicability is decided after the evidence is gathered, which is one
        more moving part in the check whose wrong answer ends the session asking.

        For an operation that declares it could not affect a management path the question is
        not asked, exactly as it is not asked on the apply path — demanding proof of an
        unrelated fact would block every such recovery on a loopback deployment and protect
        nothing.
        """
        _reason, relation = assess_management_path_reason(
            management_path, resource_id=run.object_id,
            applicable=definition.can_affect_management_path)
        # Asked of interface-targeted operations only, for the reason `_plan_now` gives: the
        # relation is a question about a network interface, and an operation whose protection
        # is carried by typed per-reason findings leaves it `unknown` on purpose. What stands
        # in its place there is the re-planned eligibility, which `_recovery_plan_now` checks
        # in full — and which for such an operation is derived from those very findings.
        if (definition.can_affect_management_path
                and definition.protection_uses_resource_relation
                and relation is not ManagementPathRelation.NOT_ON_MANAGEMENT_PATH):
            raise RunRefused(
                "target_is_management_path"
                if relation is ManagementPathRelation.ON_MANAGEMENT_PATH
                else "management_path_unproven",
                "this request cannot prove the target is not the path it is reaching "
                "LocalPlane over",
                {"run_id": run.run_id, "object_id": run.object_id,
                 "management_path": str(relation), "reason": management_path.reason,
                 "missing_evidence": list(management_path.missing_evidence)})
        return relation

    def _open_recovery_attempt(
        self,
        change: ChangeRecord,
        run: RunRecord,
        kind: RecoveryActionKind,
        relation: ManagementPathRelation,
        management_path: ManagementPathVerdict,
    ) -> RecoveryAttemptRecord:
        """Record that a recovery attempt is under way, before it can do anything.

        This insert is the serialisation point. A partial unique index allows one in-flight
        attempt per Change, so two concurrent retries do not both reach a dispatch: the second
        fails on the index, whoever is asking and whichever process is asking.

        It is also the crash marker. An attempt that dies before recording anything else is
        read on the next start as interrupted, conservatively, with the hold kept.
        """
        attempt_id = f"rcv_{uuid.uuid4().hex}"
        now = _now()
        try:
            with self._db.transaction():
                self._require_open_hold_locked(change, run)
                self.recovery_attempts.insert({
                    "attempt_id": attempt_id,
                    **_recovery_identity(
                        change, run, self.recovery_attempts.next_sequence(change.change_id)),
                    "kind": str(kind),
                    "started_at": now,
                    "protection_management_path": str(relation),
                    "protection_evidence_id": management_path.evidence_id,
                })
                self._append(run.run_id, RunEvent.RECOVERY_RETRY_STARTED,
                             change_id=change.change_id, occurred_at=now,
                             detail={"attempt_id": attempt_id,
                                     "recovery_reason": change.recovery_reason,
                                     "management_path": str(relation),
                                     "protection_evidence_id": management_path.evidence_id,
                                     "host_effect": "none"})
        except sqlite3.IntegrityError as exc:
            raise RunRefused(
                "recovery_attempt_in_flight",
                "a recovery attempt against this change is already under way",
                {"change_id": change.change_id, "error": str(exc)}) from None
        stored = self.recovery_attempts.get(attempt_id, change.value_type)
        assert stored is not None  # written in the transaction above
        return stored

    def _recovery_evidence(
        self, run: RunRecord, change: ChangeRecord, attempt: RecoveryAttemptRecord,
        read: _ReadBack,
    ) -> RecoveryAttemptRecord:
        """Record the fresh reading and what the operation made of it. Before any write."""
        self._recovery_step(
            run, change, attempt, RunEvent.RECOVERY_EVIDENCE_RESULT,
            columns=_recovery_evidence_columns(change, read),
            detail={"attempt_id": attempt.attempt_id, "outcome": str(read.outcome),
                    "reason": read.reason, "observation_id": read.observation_id,
                    "observed": read.observed,
                    "expected": change.desired_value if change.change_kind == CHANGE_KIND_FIELD
                    else change.expected_state,
                    "proves_intended_state": read.outcome.proved,
                    "host_effect": "none"})
        return self._require_attempt(attempt.attempt_id, change)

    def _recovery_plan_now(
        self, run: RunRecord, record: ObjectRecord,
        management_path: ManagementPathVerdict, change: ChangeRecord,
        systemd_lifecycle_context: SystemdLifecycleContext | None = None,
    ) -> RunPlan:
        """Re-derive every gate against current truth, and refuse a changed intention.

        **Not the digest comparison the apply path makes**, and the difference is the point. A
        Change in recovery is one whose own execution may have moved the world, so the plan's
        starting value legitimately differs from the one published — that is what happened, not
        a reason to refuse. What may *not* differ is the end state: the operation, the target
        and what the change was for are the original's, and a re-plan that would now produce a
        different desired value, a different verb or a different expected state is refused
        rather than carried out. Silently writing today's intent under yesterday's Change is
        exactly the substitution recovery must not make.

        Everything else is asked again in full: the operation must still be plannable, the
        ownership gate must still pass, the capability must still be declared, the observation
        must still be current, and execution must still be eligible.
        """
        replanned = self._runs.replan(
            OperationType(run.operation), record, management_path,
            systemd_lifecycle_context=systemd_lifecycle_context)
        if isinstance(replanned, PlanRefused):
            raise RunRefused(
                "recovery_operation_not_applicable",
                "the operation this change was for can no longer be planned against current "
                "truth, so there is nothing to retry",
                {"run_id": run.run_id, "change_id": change.change_id,
                 "reasons": [{"code": replanned.code, "detail": replanned.detail}]})
        _require_same_intended_outcome(change, replanned)
        if replanned.execution.eligibility is not ExecutionEligibility.ELIGIBLE:
            raise RunRefused("execution_blocked", "executing this plan is blocked",
                             {"run_id": run.run_id,
                              "blockers": list(replanned.execution.blockers),
                              "availability": str(replanned.execution.availability)})
        if replanned.confirmation.method is not ConfirmationMethod.ACKNOWLEDGE:
            # Today's policy asks for more than any authority this build can record. The
            # grant made earlier cannot be stretched to cover it.
            raise RunRefused(
                "confirmation_method_unsupported",
                "current policy requires a confirmation method this build does not accept",
                {"required_method": str(replanned.confirmation.method),
                 "accepted": str(ConfirmationMethod.ACKNOWLEDGE)})
        return replanned

    def _recovery_request(
        self, run: RunRecord, change: ChangeRecord, record: ObjectRecord,
        executor: OperationExecutor, read: _ReadBack, plan: RunPlan,
    ) -> MutationRequest:
        """Assemble the new mutation: fresh identity material, and a fresh race guard.

        The correlation is taken again rather than reused. It is the executor's own material
        for reaching the target, and after a failure that may have moved the world the version
        recorded before the original dispatch is exactly the evidence that must not be leaned
        on: an identifier may have been recycled, and for an operation whose proof rests on
        what the target looked like at dispatch, a stale baseline would let a verification
        prove something that had already happened.

        A field change's ``expected_current`` comes from the reading just taken, not from the
        Change's recorded before-value. After an unprovable write nobody knows what the target
        holds, and the privileged path re-checks the guard against reality before it writes.
        """
        try:
            correlation = executor.correlate(record)
        except (ExecutionRefused, Exception) as exc:  # noqa: BLE001 - report, never proceed
            code = exc.code if isinstance(exc, ExecutionRefused) else "target_not_correlated"
            detail = (dict(exc.detail) if isinstance(exc, ExecutionRefused)
                      else {"error": f"{type(exc).__name__}: {exc}"})
            raise RunRefused(code, "the target could not be identified, so nothing was retried",
                             {"run_id": run.run_id, **detail}) from None

        if change.change_kind == CHANGE_KIND_ACTION:
            return MutationRequest(
                kind=CHANGE_KIND_ACTION, attempt_id=f"rat_{uuid.uuid4().hex}",
                correlation=correlation, action=change.action)
        if read.value is None:
            raise RunRefused(
                "current_value_unreadable",
                "what the target holds now could not be read, so no write may be guarded on it",
                {"run_id": run.run_id, "reason": read.reason,
                 "evidence_outcome": str(read.outcome)})
        if read.value == change.desired_value:  # pragma: no cover - proof already returned
            raise RunRefused("already_reconciled",
                             "the target already holds the value this change wanted",
                             {"run_id": run.run_id, "value": read.value})
        return MutationRequest(
            kind=CHANGE_KIND_FIELD, attempt_id=f"rat_{uuid.uuid4().hex}",
            correlation=correlation,
            expected_current=read.value, desired=change.desired_value)

    def _recovery_dispatch_marker(
        self, run: RunRecord, change: ChangeRecord, attempt: RecoveryAttemptRecord,
        request: MutationRequest,
    ) -> None:
        """Consume the authority and mark the dispatch durably. Then, and only then, send it.

        Both in one transaction, because a consumed confirmation and a recorded attempt are
        the two halves of "this write was authorised and it is about to happen"; a crash
        between them would leave one of them lying.
        """
        now = _now()
        with self._db.transaction():
            self._require_open_hold_locked(change, run, attempt.attempt_id)
            confirmation = self.confirmations.outstanding(
                run.run_id, CONFIRMATION_PURPOSE_RECOVERY_RETRY)
            if confirmation is None:
                raise RunRefused(
                    "recovery_confirmation_required",
                    "a retry that must write again needs authority nobody has granted yet; "
                    "the confirmation that authorised the original attempt is not reusable",
                    {"change_id": change.change_id, "run_id": run.run_id,
                     "method": run.preview.confirmation_method})
            if not self.confirmations.consume(
                confirmation_id=confirmation.confirmation_id,
                attempt_id=request.attempt_id, now=now,
            ):
                raise RunRefused(
                    "recovery_confirmation_already_consumed",
                    "this recovery authority was consumed by another attempt",
                    {"confirmation_id": confirmation.confirmation_id})
            self.recovery_attempts.update(attempt.attempt_id, {
                "confirmation_id": confirmation.confirmation_id,
                "execution_correlation": to_json(dict(request.correlation)),
                "mutation_attempt_id": request.attempt_id,
                "dispatch_began_at": now,
            })
            self._append(run.run_id, RunEvent.RECOVERY_CONFIRMATION_CONSUMED,
                         change_id=change.change_id, occurred_at=now,
                         detail={"attempt_id": attempt.attempt_id,
                                 "confirmation_id": confirmation.confirmation_id,
                                 "mutation_attempt_id": request.attempt_id})
            self._append(run.run_id, RunEvent.RECOVERY_MUTATION_DISPATCHED,
                         change_id=change.change_id, occurred_at=now,
                         detail={"attempt_id": attempt.attempt_id,
                                 "mutation_attempt_id": request.attempt_id,
                                 "kind": request.kind, "action": request.action,
                                 "expected_current": request.expected_current,
                                 "desired": request.desired})
        LOG.warning("recovery retry is dispatching a new mutation", extra={
            "change_id": change.change_id, "run_id": run.run_id,
            "attempt_id": attempt.attempt_id, "mutation_attempt_id": request.attempt_id})

    def _recovery_result(
        self, run: RunRecord, change: ChangeRecord, attempt: RecoveryAttemptRecord,
        report: MutationReport,
    ) -> RecoveryAttemptRecord:
        effect = host_effect_for(report.outcome)
        self._recovery_step(
            run, change, attempt, RunEvent.RECOVERY_MUTATION_RESULT,
            columns={"mutation_outcome": str(report.outcome),
                     "mutation_reason": report.reason,
                     "mutation_provider": report.provider,
                     "mutation_method": report.method,
                     "mutation_detail": to_json(report.detail),
                     "host_effect": str(effect)},
            detail={"attempt_id": attempt.attempt_id, "outcome": str(report.outcome),
                    "reason": report.reason, "provider": report.provider,
                    "host_effect": str(effect), **report.detail})
        return self._require_attempt(attempt.attempt_id, change)

    def _recovery_verification(
        self, run: RunRecord, change: ChangeRecord, attempt: RecoveryAttemptRecord,
        read: _ReadBack,
    ) -> RecoveryAttemptRecord:
        self._recovery_step(
            run, change, attempt, RunEvent.RECOVERY_VERIFICATION_RESULT,
            columns=_recovery_verification_columns(change, read),
            detail={"attempt_id": attempt.attempt_id, "outcome": str(read.outcome),
                    "reason": read.reason, "observation_id": read.observation_id,
                    "observed": read.observed})
        return self._require_attempt(attempt.attempt_id, change)

    def _complete_recovery(
        self, run: RunRecord, change: ChangeRecord, attempt: RecoveryAttemptRecord,
        outcome: RecoveryAttemptOutcome, detail: dict[str, Any],
    ) -> RecoveryOutcome:
        """The end state the Change wanted is established. Give the object back.

        The Change is not rewritten. It still says ``recovery_required`` and still says why,
        because that is what happened to it; what has changed is that a later attempt proved
        the end state, and the attempt row is where that is recorded.
        """
        now = _now()
        with self._db.transaction():
            self._require_open_hold_locked(change, run, attempt.attempt_id)
            self.recovery_attempts.update(attempt.attempt_id, {
                "outcome": str(outcome), "releases_hold": 1, "finished_at": now})
            self._append(run.run_id, RunEvent.RECOVERY_RETRY_FINISHED,
                         change_id=change.change_id, occurred_at=now,
                         detail={"attempt_id": attempt.attempt_id, "outcome": str(outcome),
                                 "hold_released": True, **detail})
            self._append(run.run_id, RunEvent.RECOVERY_HOLD_RELEASED,
                         change_id=change.change_id, occurred_at=now,
                         detail={"attempt_id": attempt.attempt_id,
                                 "released_by": str(RecoveryActionKind.RETRY),
                                 "object_id": change.object_id, "field": _lock_aspect(run),
                                 "outcome": str(outcome)})
            self.locks.release(run.run_id)
        LOG.info("recovery completed; the hold is released", extra={
            "change_id": change.change_id, "run_id": run.run_id,
            "attempt_id": attempt.attempt_id, "outcome": str(outcome),
            "change_result": change.result, "recovery_reason": change.recovery_reason})
        return self._recovery_outcome(change, attempt.attempt_id)

    def _settle_retry(
        self, run: RunRecord, change: ChangeRecord, attempt: RecoveryAttemptRecord,
        outcome: RecoveryAttemptOutcome,
    ) -> RecoveryOutcome:
        """The retry ended and proved nothing. The object stays held, and it has to."""
        self._recovery_step(
            run, change, attempt, RunEvent.RECOVERY_RETRY_FINISHED,
            columns={"outcome": str(outcome), "finished_at": _now()},
            detail={"attempt_id": attempt.attempt_id, "outcome": str(outcome),
                    "hold_released": False})
        LOG.error("recovery retry did not settle the hold", extra={
            "change_id": change.change_id, "run_id": run.run_id,
            "attempt_id": attempt.attempt_id, "outcome": str(outcome)})
        return self._recovery_outcome(change, attempt.attempt_id)

    def _refuse_recovery(
        self, run: RunRecord, change: ChangeRecord, attempt: RecoveryAttemptRecord,
        refusal: RunRefused,
    ) -> RunRefused:
        """Nothing was dispatched, so there is provably no new host effect. Record and refuse.

        The attempt is settled first and the refusal carries its id, so that "somebody tried
        and this is why it stopped" survives in the history rather than only in one HTTP
        response.
        """
        self._recovery_step(
            run, change, attempt, RunEvent.RECOVERY_RETRY_FINISHED,
            columns={"outcome": str(RecoveryAttemptOutcome.REFUSED),
                     "refusal_code": refusal.code, "finished_at": _now()},
            detail={"attempt_id": attempt.attempt_id,
                    "outcome": str(RecoveryAttemptOutcome.REFUSED),
                    "code": refusal.code, "hold_released": False,
                    "host_effect": "none", **refusal.detail})
        return RunRefused(refusal.code, refusal.message,
                          {**refusal.detail, "attempt_id": attempt.attempt_id,
                           "recovery_attempt_outcome": str(RecoveryAttemptOutcome.REFUSED),
                           "object_write_locked": self.locks.for_run(run.run_id) is not None})

    def _observe_for_recovery(self, run: RunRecord, change: ChangeRecord) -> _ReadBack:
        """A best-effort reading for a resolution. Never raises, never writes to the host."""
        executor = self._executors.get(OperationType(run.operation)) if _known_operation(
            run.operation) else None
        if executor is None:
            return _ReadBack(VerificationOutcome.OBSERVATION_UNAVAILABLE,
                             reason="execution_not_implemented")
        return self._read_back(run, change, executor, _applied_expectation(change))

    def _recovery_step(
        self, run: RunRecord, change: ChangeRecord, attempt: RecoveryAttemptRecord,
        event: RunEvent, *, columns: dict[str, Any], detail: dict[str, Any],
    ) -> None:
        """One durable recovery step: move the attempt, append one event. Atomically.

        The Run and the Change are deliberately not among the things this can move. Both
        finished; the recovery is a later event with its own row, and an engine able to reach
        back into either of them from here is an engine able to rewrite history by accident.
        """
        now = _now()
        with self._db.transaction():
            self.recovery_attempts.update(attempt.attempt_id, columns)
            self._append(run.run_id, event, change_id=change.change_id,
                         detail=detail, occurred_at=now)

    def _require_attempt(
        self, attempt_id: str, change: ChangeRecord
    ) -> RecoveryAttemptRecord:
        attempt = self.recovery_attempts.get(attempt_id, change.value_type)
        assert attempt is not None  # written by this service and never deleted
        return attempt

    def _recovery_outcome(self, change: ChangeRecord, attempt_id: str) -> RecoveryOutcome:
        settled = self._require_change(change.change_id)
        return RecoveryOutcome(
            change=settled,
            attempt=self._require_attempt(attempt_id, settled),
            hold=self.recovery_hold(settled),
        )

    # -------------------------------------------------------------------- preconditions

    def _executor(self, run: RunRecord) -> OperationExecutor:
        try:
            operation = OperationType(run.operation)
        except ValueError:
            raise RunRefused("unsupported_operation",
                             "LocalPlane does not implement this operation",
                             {"operation": run.operation}) from None
        executor = self._executors.get(operation)
        if executor is None:
            raise RunRefused("execution_not_implemented",
                             "LocalPlane cannot execute this operation",
                             {"operation": run.operation})
        return executor

    def _require_target(self, run: RunRecord) -> ObjectRecord:
        record = self._runs.objects.get(run.object_id)
        if record is None:
            raise RunRefused("object_not_found",
                             "the object this run targets no longer exists",
                             {"object_id": run.object_id})
        return record

    def _require_executable_preview(self, run: RunRecord) -> None:
        """The published plan must itself say execution was available and this plan eligible.

        This is what makes a Run planned by an earlier build non-executable no matter what else
        has changed. Such a preview says ``not_implemented`` and ``blocked`` — that is what its
        operator was shown, and the plan an operator was shown is the plan that may be
        executed. Nothing rewrites it into a newer opinion.
        """
        preview = run.preview
        executable = (
            str(ExecutionEligibility.ELIGIBLE),
            str(ExecutionEligibility.GUARDED),
            # A fourth path, and still not a softer one: reaching it required the plan to be
            # blocked by exactly one hazard and by nothing else, and it stays unexecutable
            # until an operator grants the separate authority that names it.
            str(ExecutionEligibility.SELF_IMPACT_OVERRIDE_REQUIRED),
        )
        if (preview.execution_availability != str(ExecutionAvailability.AVAILABLE)
                or preview.execution_eligibility not in executable):
            raise RunRefused(
                "preview_not_executable",
                "the plan this run published was not executable when it was made; plan again",
                {"run_id": run.run_id, "preview_id": preview.preview_id,
                 "execution_availability": preview.execution_availability,
                 "execution_eligibility": preview.execution_eligibility,
                 "blockers": list(preview.execution_blockers)})

    def _plan_now(
        self, run: RunRecord, management_path: ManagementPathVerdict,
        record: ObjectRecord | None = None,
        systemd_lifecycle_context: SystemdLifecycleContext | None = None,
    ) -> RunPlan:
        """Re-plan once, and refuse unless the published plan still describes what would happen.

        One re-plan answers three questions, so it is done once: can this still be planned at
        all, does it come out as the same document, and does executing it pass every gate under
        *this* request's evidence. Deriving them separately would mean planning twice and
        leaving two places for the rules to drift apart.

        The management-path check is stated explicitly rather than left to the digest
        comparison, because it is the one condition whose wrong answer ends the session that is
        asking — and a gate enforced only as a side effect of a digest is one a later change to
        the digest could remove silently.

        ``systemd_lifecycle_context`` is the fresh, request-scoped safety evidence an
        operation whose protection rests on it needs in order to be planned at all. It is
        acquired by the caller, from the transport this request actually arrived on, for the
        same reason the management-path verdict is: a gate re-derived from evidence belonging
        to an older request is not a gate about now.
        """
        record = record or self._require_target(run)
        replanned = self._runs.replan(
            OperationType(run.operation), record, management_path,
            systemd_lifecycle_context=systemd_lifecycle_context)
        if isinstance(replanned, PlanRefused):
            raise RunRefused(
                "preview_stale",
                "this operation can no longer be planned against current truth",
                {"run_id": run.run_id,
                 "reasons": [{"code": replanned.code, "detail": replanned.detail}]})

        validity = assess_validity(
            self._runs.published_plan(run), run.preview.preview_digest, replanned,
            run.preview.digest_version)
        if validity.stale:
            raise RunRefused(
                "preview_stale",
                "the plan this run published no longer describes what would happen; the remedy "
                "is a new run, not a rewritten one",
                {"run_id": run.run_id, "preview_id": run.preview.preview_id,
                 "reasons": [{"code": r.code, "detail": r.detail} for r in validity.reasons]})

        protection = replanned.protection
        definition = self._runs.definition(OperationType(run.operation))
        # **The two paths take mirror-image gates, and between them they leave no third
        # case.** An ordinary apply requires the target to be proven *not* to be the path
        # this request arrived over. A guarded apply requires it to be proven that it *is* —
        # the positive proof, because the guard is a mechanism against a specific hazard and
        # arming one where the relation is unresolved would mean dispatching a change
        # LocalPlane cannot justify and then a reversal it cannot justify either.
        #
        # `unknown` therefore satisfies neither, which is the whole reason the relation has
        # three values rather than two.
        #
        # Both are stated explicitly rather than left to the digest comparison, because this
        # is the one condition whose wrong answer ends the session that is asking, and a gate
        # enforced only as a side effect of a digest is one a later change to the digest
        # could remove silently. Both are asked only of operations that could affect a
        # management path: for one that could not, demanding proof about the path would be
        # demanding proof of an unrelated fact, the answer would be `unknown` on every
        # loopback deployment, and the refusal would protect nothing.
        #
        # **The relation is a question about a network interface, and it is asked only of
        # operations whose target is one.** ``ManagementPathVerdict.resource_id`` is always
        # an interface object — the path is resolved from the accepted socket's local
        # endpoint and corroborated by the kernel's route to the peer, and both land on an
        # interface — so "is this target that resource" is a real question about an
        # interface and is not a question about a systemd unit at all. An operation
        # targeting something else carries its protection in typed per-reason assessments,
        # deliberately leaves this relation ``unknown``, and is governed instead by the
        # stronger check two lines below: its reasons produce its blockers, its blockers
        # produce its eligibility, and that eligibility must be exactly the path its
        # published plan rests on.
        #
        # The discriminator is the operation's own declared target kind rather than the
        # shape of the assessment it produced. How many reasons an assessment happens to
        # carry is a representation detail; adding or removing one must not move an
        # operation between two safety models without anybody writing that down.
        asks_the_resource_relation = (
            definition.can_affect_management_path
            and definition.protection_uses_resource_relation
        )
        required = (
            ManagementPathRelation.ON_MANAGEMENT_PATH
            if _is_guarded(run)
            else ManagementPathRelation.NOT_ON_MANAGEMENT_PATH
        )
        if asks_the_resource_relation and protection.management_path is not required:
            raise RunRefused(
                "management_path_unproven"
                if protection.management_path is ManagementPathRelation.UNKNOWN
                else "target_is_management_path"
                if required is ManagementPathRelation.NOT_ON_MANAGEMENT_PATH
                else "target_is_not_management_path",
                "this request cannot prove the target stands to it the way this plan "
                "requires",
                {"run_id": run.run_id, "object_id": run.object_id,
                 "management_path": str(protection.management_path),
                 "required": str(required),
                 "reason": protection.reason,
                 "missing_evidence": list(protection.missing_evidence)})
        # **The published plan's path is the only one it may take.** A Run whose preview
        # rests on a self-impact override must still re-derive to one — a plan that has
        # since become ordinarily eligible, or blocked, or eligible under some other future
        # path, is not the document its authority was granted against.
        permitted = (
            (ExecutionEligibility.SELF_IMPACT_OVERRIDE_REQUIRED,)
            if _requires_self_impact_override(run)
            else (ExecutionEligibility.GUARDED,)
            if _is_guarded(run)
            else (ExecutionEligibility.ELIGIBLE,)
        )
        if replanned.execution.eligibility not in permitted:
            raise RunRefused("execution_blocked", "executing this plan is blocked",
                             {"run_id": run.run_id,
                              "blockers": list(replanned.execution.blockers),
                              "eligibility": str(replanned.execution.eligibility),
                              "availability": str(replanned.execution.availability)})
        return replanned

    # ------------------------------------------------------------------------ internals

    def _step(
        self,
        run_id: str,
        event: RunEvent,
        *,
        change_id: str | None = None,
        state: RunState | None = None,
        state_from: str | None = None,
        host_effect: str | None = None,
        finished_at: str | None = None,
        change: dict[str, Any] | None = None,
        detail: dict[str, Any] | None = None,
        release: bool = False,
    ) -> None:
        """One durable step: move the Run, move the Change, append one event. Atomically.

        Every transition in this module is this shape, so it is written once. The transcript
        entry and the state it describes land in the same transaction, which is what makes the
        history a record of what happened rather than a record of what was attempted.
        """
        now = _now()
        with self._db.transaction():
            if state is not None or host_effect is not None or finished_at is not None:
                self._runs.runs.set_state(
                    run_id=run_id,
                    state=str(state) if state is not None else self._state_of(run_id),
                    host_effect=host_effect,
                    finished_at=finished_at,
                )
            if change and change_id:
                self.changes.update(change_id, change)
            self._append(run_id, event, change_id=change_id, state_from=state_from,
                         state_to=str(state) if state is not None else None,
                         detail=detail, occurred_at=now)
            if release:
                self.locks.release(run_id)

    def _state_of(self, run_id: str) -> str:
        current = self._runs.runs.get(run_id)
        assert current is not None
        return current.state

    def _append(
        self, run_id: str, event: RunEvent, *, change_id: str | None = None,
        state_from: str | None = None, state_to: str | None = None,
        detail: dict[str, Any] | None = None, occurred_at: str | None = None,
    ) -> None:
        self.events.append(
            event_id=f"evt_{uuid.uuid4().hex}", run_id=run_id, change_id=change_id,
            event=str(event), occurred_at=occurred_at or _now(),
            state_from=state_from, state_to=state_to, detail=detail or {})

    def _reread(self, run: RunRecord) -> RunRecord:
        current = self._runs.runs.get(run.run_id)
        if current is None:
            raise RunRefused("run_not_found", "the run disappeared while it was being applied",
                             {"run_id": run.run_id})
        return current

    def _require_change(self, change_id: str) -> ChangeRecord:
        change = self.changes.get(change_id)
        assert change is not None  # written by this service and never deleted
        return change



def _known_operation(operation: str) -> bool:
    """Whether this build still has a name for what a stored Run was for.

    The store outlives the code that wrote to it, and a Change whose operation a later build
    dropped must still be *resolvable* — that is the escape hatch, and an escape hatch that
    stops working when a feature is removed is not one.
    """
    try:
        OperationType(operation)
    except ValueError:
        return False
    return True


def _recovery_identity(
    change: ChangeRecord, run: RunRecord, sequence: int
) -> dict[str, Any]:
    """What a recovery attempt is about. Frozen by a trigger once written."""
    return {
        "change_id": change.change_id,
        "run_id": run.run_id,
        "host_id": change.host_id,
        "object_id": change.object_id,
        "sequence": sequence,
    }


def _recovery_evidence_columns(change: ChangeRecord, read: _ReadBack) -> dict[str, Any]:
    """The reading taken *before* anything was written, and what the operation made of it."""
    return {
        "evidence_outcome": str(read.outcome),
        "evidence_observation_id": read.observation_id,
        "evidence_observed_value": (
            None if read.value is None
            else encode(ValueType(change.value_type or ""), read.value)),
        "evidence_observed_state": read.state,
        "evidence_reason": read.reason,
    }


def _recovery_verification_columns(change: ChangeRecord, read: _ReadBack) -> dict[str, Any]:
    """The reading taken *after* a new write. A separate question, so separate columns."""
    return {
        "verification_outcome": str(read.outcome),
        "verification_observation_id": read.observation_id,
        "verification_observed_value": (
            None if read.value is None
            else encode(ValueType(change.value_type or ""), read.value)),
        "verification_observed_state": read.state,
        "verification_reason": read.reason,
    }


def _require_same_intended_outcome(change: ChangeRecord, plan: RunPlan) -> None:
    """The re-plan must still be for the end state the original Change was for.

    **The guard against silent substitution**, and the one rule that makes a retry belong to
    the Change it names. Between the failure and the recovery an operator may have revised the
    intent, and re-planning against current truth would then produce a perfectly valid plan
    for a *different* value — which, written under this Change, would record that LocalPlane
    finished what it started when it did something else.

    What may legitimately differ is the starting point. A Change in recovery is one whose own
    execution may have moved the world, so the current value being other than the one
    published is what happened rather than a reason to refuse.

    Nothing here knows what kind of change it is looking at beyond the two shapes the engine
    already discriminates between; the values it compares are the ones the record itself keeps.
    """
    planned = plan.change
    if isinstance(planned, PlannedFieldChange):
        current = (change.change_kind == CHANGE_KIND_FIELD
                   and planned.field == change.field
                   and str(planned.value_type) == change.value_type
                   and planned.desired == change.desired_value)
        now: dict[str, Any] = {"change_kind": CHANGE_KIND_FIELD, "field": planned.field,
                               "desired": planned.desired}
        was: dict[str, Any] = {"change_kind": change.change_kind, "field": change.field,
                               "desired": change.desired_value}
    elif isinstance(planned, PlannedAction):
        current = (change.change_kind == CHANGE_KIND_ACTION
                   and planned.action == change.action
                   and planned.expected_state == change.expected_state)
        now = {"change_kind": CHANGE_KIND_ACTION, "action": planned.action,
               "expected_state": planned.expected_state}
        was = {"change_kind": change.change_kind, "action": change.action,
               "expected_state": change.expected_state}
    else:  # pragma: no cover - the union has two members and both are handled
        current, now, was = False, {}, {}
    if not current:
        raise RunRefused(
            "recovery_intent_changed",
            "what LocalPlane would do now is not what this change was for; recovery may not "
            "substitute a different outcome for the one it is recovering",
            {"change_id": change.change_id, "recorded": was, "would_plan_now": now})


def _lock_aspect(run: RunRecord) -> str:
    """What this Run holds the object's write lock on.

    A field change locks the field it reconciles. An action locks the aspect it acts on,
    which is not a controlled field and is not pretending to be one — LocalPlane retains no
    desired state for something it only acts on. Either way one Run at a time per object and
    aspect, enforced by the database.
    """
    if run.preview.change_kind == CHANGE_KIND_ACTION:
        return ACTION_LOCK_ASPECT
    return run.preview.field or ""


def _requires_self_impact_override(run: RunRecord) -> bool:
    """Whether this Run's published plan says a self-impact override is its only write path.

    Read from the **preview**, like :func:`_is_guarded` and for the same reason: the plan an
    operator was shown is the plan that may be executed, and a Run whose eligibility this
    process recomputed at apply time could execute a document nobody reviewed. Whether that
    plan still holds is re-derived in :meth:`ChangeService._plan_now`, where a plan whose
    path has moved comes out stale rather than quietly taking a different one here.
    """
    return run.preview.execution_eligibility == str(
        ExecutionEligibility.SELF_IMPACT_OVERRIDE_REQUIRED
    )


def _is_guarded(run: RunRecord) -> bool:
    """Whether this Run's published plan says the guarded path is the only write path.

    Read from the **preview**, not re-derived. The plan an operator was shown is the plan
    that may be executed, and a Run whose eligibility LocalPlane recomputed at apply time
    could execute a document nobody reviewed. What *is* re-derived, in :meth:`_plan_now`, is
    whether that plan still describes what would happen — and a plan whose eligibility has
    moved comes out stale there rather than silently taking a different path here.
    """
    return run.preview.execution_eligibility == str(ExecutionEligibility.GUARDED)


def _arms_recovery(run: RunRecord) -> bool:
    """Whether this Run has recovery to arm before it may write.

    Read from the published plan rather than from the operation's definition, deliberately:
    what an operator was shown is what governs, and a plan that said its recovery mode was
    ``none`` must not acquire a checkpoint because a later build changed its mind.
    """
    return run.preview.change_kind == CHANGE_KIND_FIELD


def _change_identity_columns(preview: RunPreviewRecord) -> dict[str, Any]:
    """What the Change is about, in whichever of the two shapes the plan published.

    Copied from the preview rather than re-derived: the Change records what the *published
    plan* said would happen, and a second derivation is a second thing that can disagree.
    """
    if preview.change_kind == CHANGE_KIND_ACTION:
        return {
            "change_kind": CHANGE_KIND_ACTION,
            "field": None, "value_type": None,
            "before_value": None, "desired_value": None,
            "action": preview.action,
            "observed_state": preview.observed_state,
            "expected_state": preview.expected_state,
        }
    value_type = ValueType(preview.value_type or "")
    return {
        "change_kind": CHANGE_KIND_FIELD,
        "field": preview.field,
        "value_type": preview.value_type,
        "before_value": encode(value_type, preview.current_value),
        "desired_value": encode(value_type, preview.desired_value),
        "action": None, "observed_state": None, "expected_state": None,
    }


def _change_log(preview: RunPreviewRecord) -> dict[str, Any]:
    """The same two shapes, rendered for a log line rather than for a column."""
    if preview.change_kind == CHANGE_KIND_ACTION:
        return {"change_kind": CHANGE_KIND_ACTION, "action": preview.action,
                "expected_state": preview.expected_state}
    return {"change_kind": CHANGE_KIND_FIELD, "field": preview.field,
            "before_value": preview.current_value, "desired_value": preview.desired_value}


def _apply_request(change: ChangeRecord) -> MutationRequest:
    """What the executor is being asked to carry out, assembled from the Change alone.

    From the Change and from nothing else, because the Change is the record that has to be
    answerable afterwards for what was dispatched. Nothing is re-derived from a plan, an
    intent or an observation at this point: the row says what was meant, and the row is what
    is sent.
    """
    if change.change_kind == CHANGE_KIND_ACTION:
        return MutationRequest(
            kind=CHANGE_KIND_ACTION,
            attempt_id=change.apply_attempt_id,
            correlation=change.execution_correlation,
            action=change.action,
        )
    return MutationRequest(
        kind=CHANGE_KIND_FIELD,
        attempt_id=change.apply_attempt_id,
        correlation=change.execution_correlation,
        expected_current=change.before_value,
        desired=change.desired_value,
    )


def _applied_expectation(
    change: ChangeRecord, correlation: Mapping[str, Any] | None = None
) -> Expectation:
    """What a reading must show for this change to have taken effect.

    ``correlation`` defaults to the Change's own, which is the baseline the original dispatch
    was judged against and the right one for asking whether the end state has been reached
    since. A recovery retry that dispatches a *new* mutation passes the correlation it just
    took instead, because for an operation whose proof rests on what the target looked like at
    dispatch, the older baseline would let the original attempt's effect prove the new one.

    Which of those two facts matters is the operation's business, not this module's: the
    engine hands over a baseline and the executor decides whether it has anything to say.
    """
    material = change.execution_correlation if correlation is None else correlation
    if change.change_kind == CHANGE_KIND_ACTION:
        return Expectation(
            kind=CHANGE_KIND_ACTION,
            correlation=material,
            state=change.expected_state,
        )
    return Expectation(
        kind=CHANGE_KIND_FIELD,
        correlation=material,
        field=change.field,
        value_type=change.value_type,
        value=change.desired_value,
    )


def _restored_expectation(change: ChangeRecord) -> Expectation:
    """What a reading must show for the previous value to be back. Field changes only."""
    return Expectation(
        kind=CHANGE_KIND_FIELD,
        correlation=change.execution_correlation,
        field=change.field,
        value_type=change.value_type,
        value=change.before_value,
    )


def _verification_columns(prefix: str, change: ChangeRecord, read: _ReadBack) -> dict[str, Any]:
    """What a read-back is recorded as: the judgement, the reading, and what it showed.

    The observed answer goes into the column of its own kind. A lifecycle state is not an
    integer and the STRICT table will not let it pretend to be one, which is the point of
    keeping two columns rather than widening one.
    """
    columns: dict[str, Any] = {
        f"{prefix}_outcome": str(read.outcome),
        f"{prefix}_observation_id": read.observation_id,
        f"{prefix}_observed_value": (
            None if read.value is None
            else encode(ValueType(change.value_type or ""), read.value)),
    }
    if prefix == "verification":
        columns["verification_reason"] = read.reason
        if change.change_kind == CHANGE_KIND_ACTION:
            columns["verification_observed_state"] = read.state
    return columns


def _recovery_reason(outcome: VerificationOutcome, report: MutationReport) -> RecoveryReason:
    """Why the restoration could not be proved, as specifically as the evidence allows.

    ``provider is None`` on a report is the structural marker that the answer did not come from
    the privileged path at all — the transport failed before the helper could reply — which is
    a different situation from a helper that answered and refused.
    """
    if outcome is VerificationOutcome.TARGET_ABSENT:
        return RecoveryReason.TARGET_ABSENT_AFTER_MUTATION
    if outcome is not VerificationOutcome.MISMATCH:
        return RecoveryReason.ROLLBACK_VERIFICATION_UNAVAILABLE
    if report.outcome is MutationOutcome.NOT_WRITTEN:
        return (RecoveryReason.ROLLBACK_NOT_DISPATCHED if report.provider is None
                else RecoveryReason.ROLLBACK_REFUSED)
    return RecoveryReason.ROLLBACK_VERIFICATION_FAILED


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
