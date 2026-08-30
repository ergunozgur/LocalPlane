"""Planning, publishing, reading and cancelling Runs. No host is touched here, ever.

This is the Change Engine's pre-write half, and it still stops exactly where the write
boundary begins:

    request → understand current truth → typed operation → plan → preview → ✗

Confirming, arming, applying, verifying and rolling back are all in
:mod:`localplane.backend.changes`, deliberately in a different file. Everything in *this*
one is a read or a write to LocalPlane's own records: it opens no socket to the host,
refreshes no observation and dispatches nothing, which is what makes re-planning safe to do
on a `GET` in order to answer whether a published plan still holds. What it provides is the
trusted path everything after it comes through: a closed operation vocabulary, a plan
derived only from what LocalPlane already knows, an immutable published preview with a
content-addressed identity, and an honest answer to whether that identity still describes
what would happen.

**Nothing in this module knows what a network interface is.** It assembles a
:class:`PlanningContext` out of records that exist for every kind of object LocalPlane
manages, hands it to whichever planner the operation names, and stores what comes back. The
planners — and the definitions that go with them — are supplied by the caller
(:mod:`localplane.backend.operations` today), so the engine never imports the thing it is
planning against. That is the whole of the seam: enough for a second operation to arrive
without the Run model changing, and not a plugin framework.

**Creating a Run changes no truth about the host or about LocalPlane's stance towards it.**
It does not resolve a drift finding, revise an intent, adopt or release anything, or settle
an ownership conflict. A preview saying "this operation would reconcile MTU" is not
remediation — remediation is a verified write, and one of those needs an apply, a
confirmation, a checkpoint and an observation that proves the value afterwards.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol

from localplane.backend.db.database import Database, to_json
from localplane.backend.db.repositories import (
    CHANGE_KIND_ACTION,
    CHANGE_KIND_FIELD,
    AgentRepository,
    FindingRepository,
    IntentRecord,
    IntentRepository,
    ObjectRecord,
    ObjectRepository,
    RunEventRepository,
    RunPreviewRecord,
    RunRecord,
    RunRepository,
)
from localplane.backend.domain.changes import (
    Expectation,
    MutationReport,
    MutationRequest,
    ProofOfState,
    RunEvent,
)
from localplane.backend.domain.policy import OperationDefinition
from localplane.backend.domain.guard import (
    GuardArmed,
    GuardAvailability,
    GuardPlan,
    GuardPrerequisite,
    GuardReport,
    GuardRequest,
)
from localplane.backend.domain.protection import (
    ManagementPathRelation,
    ManagementPathVerdict,
    ProtectionAssessment,
    ProtectionReason,
    ProtectionStatus,
    ReasonAssessment,
)
from localplane.backend.domain.provenance import Provenance
from localplane.backend.domain.runs import (
    PLAN_DIGEST_VERSION,
    ConfirmationMethod,
    ConfirmationRequirement,
    ExecutionAssessment,
    ExecutionAvailability,
    ExecutionEligibility,
    OperationType,
    OwnershipAssessment,
    PlanEvidence,
    PlannedAction,
    PlannedChange,
    PlannedFieldChange,
    PlanRefused,
    PlanValidity,
    PublishedClaim,
    RecoveryMode,
    RecoveryPlan,
    RiskAssessment,
    RiskFactor,
    RiskTier,
    RunPlan,
    RunState,
    VerificationPlan,
    assess_validity,
    plan_digest,
)
from localplane.backend.domain.intent import ValueType, decode, encode
from localplane.backend.domain.self_impact import BackendSelfImpactAssessment
from localplane.backend.domain.systemd_lifecycle import (
    AuthorizationAssessment,
    SystemdLifecycleContext,
)
from localplane.backend.provenance import ProvenanceService

LOG = logging.getLogger("localplane.backend.runs")


class RunRefused(RuntimeError):
    """A Run could not be created or moved, with a reason a caller can branch on.

    Structured for the same reason a refused adoption is: "this object is not managed" and
    "the value you want is the value it already has" are different conditions with
    different remedies, and a caller that cannot tell them apart will offer the wrong one.
    """

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


@dataclass(frozen=True)
class PlanningContext:
    """Everything a planner may look at. All of it already recorded; none of it live.

    A planner is a pure function of this. It does not reach the host, does not open a
    socket, does not refresh an observation and does not write — which is what makes
    re-planning safe to do on a read in order to answer whether a published plan still
    holds.
    """

    now: str
    freshness_ttl_s: float
    object: ObjectRecord
    intent: IntentRecord | None
    provenance: Provenance
    provider_readings: Mapping[str, str | None]
    evidence_sources: frozenset[str]
    """The evidence LocalPlane can currently obtain, by name.

    Today this is exactly the set of capabilities the agent declared. It is passed rather
    than assumed so that a question like "is this object on the management path" is
    answered from what LocalPlane can actually see, and reports what it is missing when it
    cannot see enough.
    """

    open_drift: Mapping[str, str]
    """Open drift findings for this object, by controlled field name."""

    management_path: ManagementPathVerdict
    """Which object carries the operator's management path for the request being served.

    Host-scoped and decided before a planner runs, from the transport that request actually
    arrived on. It is passed in rather than looked up so that a planner stays a pure
    function of its context — and so that listing fifty Runs decides the question once
    rather than fifty times and cannot answer it inconsistently within one response."""

    systemd_lifecycle_context: SystemdLifecycleContext | None = None
    """Fresh request-scoped safety evidence for a systemd service action, when applicable.

    Acquired before the pure planner is called.  It is never populated from an API schema
    and contains no execution parameter or D-Bus transport handle.
    """


class OperationHandler(Protocol):
    """One typed operation: what it declares about itself, and how it plans."""

    @property
    def definition(self) -> OperationDefinition: ...

    def plan(self, context: PlanningContext) -> RunPlan | PlanRefused: ...


Planner = Callable[[PlanningContext], "RunPlan | PlanRefused"]


@dataclass(frozen=True)
class TypedOperation:
    """The seam, in its entirety: a declaration and a pure function that plans.

    Small on purpose. A second operation needs one of these and nothing else — no
    registration protocol, no lifecycle, no discovery — and the Run persistence model does
    not move to accommodate it. What it may *not* do is bring its own execution path: a
    planner returns a plan, and there is nowhere for it to return a command to.

    Executing is a *separate* seam — :class:`OperationExecutor` — supplied separately,
    because a planner is a pure function of recorded truth and an executor is the one thing
    in this product that is not. Keeping them apart is what lets planning, re-planning and
    validity all run on a read path that provably cannot write.
    """

    definition: OperationDefinition
    planner: Planner

    def plan(self, context: PlanningContext) -> RunPlan | PlanRefused:
        return self.planner(context)


@dataclass(frozen=True)
class ObservationAttempt:
    """What came of re-reading a target through the ordinary observation path.

    Three cases, kept apart because they lead to different endings: the reading was taken
    and the target is here, the reading was taken and the target is gone, or the reading
    could not be taken at all. Collapsing the last two would let "the agent is down" be
    reported as "the object disappeared", which after a possible write is the difference
    between two very different things to tell an operator.
    """

    record: ObjectRecord | None
    failure: str | None = None
    detail: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.detail is None:
            object.__setattr__(self, "detail", {})

    @property
    def taken(self) -> bool:
        return self.failure is None


class OperationExecutor(Protocol):
    """The other half of the seam: the three things the engine cannot do for itself.

    Each is deliberately generic. The engine hands over identifiers and values it read from
    its own records and gets back a report; what any of it *means* — which kernel object,
    which mechanism, which units — belongs to the concrete operation and never travels up
    here. That is what keeps the arming, dispatch, verification and rollback logic in one
    place while the thing being changed stays replaceable.
    """

    def correlate(self, record: ObjectRecord) -> dict[str, Any]:
        """The stable material needed to reach this target, for the checkpoint.

        Raises :class:`~localplane.backend.domain.changes.ExecutionRefused` when the target
        cannot be identified well enough to write to safely. That refusal happens during
        arming, before the boundary, and leaves no Change.
        """
        ...

    def mutate(self, request: MutationRequest) -> MutationReport:
        """Dispatch one typed mutation and report the typed outcome. Never raises.

        Every failure it can have is one of the three outcomes, because a caller that had
        to catch an exception here would have to decide what the exception meant about the
        host — and that decision is exactly the one that must not be made by guessing.

        The request carries whichever of the two shapes applies — a value to write, or a
        declared verb to carry out — and the executor takes only the one its operation is.
        """
        ...

    def observe(self, record: ObjectRecord) -> ObservationAttempt:
        """Re-read this target through the ordinary observation path.

        The *ordinary* one, deliberately: verification that trusted a private read would be
        verifying against the same code that wrote, and a Change is verified by the same
        evidence pipeline everything else in LocalPlane is judged by.
        """
        ...

    def prove(
        self, facts: Mapping[str, Any], expectation: Expectation
    ) -> ProofOfState:
        """Whether this reading proves the intended end state, and what it showed instead.

        *What counts as proof* belongs to the operation, which is why it is here and not in
        the engine. Proving a value is a comparison of one number. Proving that an action
        happened can need more than the resulting state — a restart that leaves a container
        running has proven nothing on its own, because it was running before — and only the
        operation knows what the additional evidence is.

        The engine has already established that the reading was taken, that it is from this
        read, and that it came from the capability and provider the published plan named. All
        that is left is the judgement.
        """
        ...

    def arm_guard(self, request: GuardRequest) -> GuardArmed:
        """Ask the host side to hold this change's reversal until a deadline.

        Raises :class:`~localplane.backend.domain.guard.GuardRefused` when it will not, and
        that refusal happens **before the write boundary**: the Run ends with no Change and
        nothing written, exactly as a checkpoint that cannot be written does.

        What the engine hands over is the checkpoint's own material — the correlation, the
        value the change will leave behind, the value to put back — plus a window it read
        from policy. What it gets back is the holder's identity and the deadline the holder
        itself will act on. The engine never computes a deadline: that is a fact about a
        clock in another process, and a second answer to it would differ by exactly the skew
        between a container and its host.
        """
        ...

    def disarm_guard(self, guard_id: str) -> GuardReport:
        """Release a guard, and report what became of it. Never raises.

        Like :meth:`mutate`, every failure is an outcome rather than an exception, because a
        caller that had to interpret an exception would be deciding what it meant about a
        mechanism that may have already acted. A holder that cannot be reached is
        ``unreachable``; one that does not know the guard is ``lost``; one that already
        fired reports what its reversal did.
        """
        ...

    def guard_status(self, guard_id: str) -> GuardReport:
        """What a guard is doing, without releasing it. Never raises.

        The read a restart makes. Separate from :meth:`disarm_guard` because a backend
        coming back has to find out where a guard stands *without* cancelling protection
        that is still running.
        """
        ...


@dataclass(frozen=True)
class RunOutcome:
    """A Run that was created, and the plan it published."""

    run: RunRecord
    plan: RunPlan
    validity: PlanValidity


class RunService:
    """Owns Runs: planning them, publishing them, reading them and cancelling them."""

    def __init__(
        self,
        database: Database,
        freshness_ttl_s: float,
        handlers: Mapping[OperationType, OperationHandler],
        provenance: ProvenanceService | None = None,
    ) -> None:
        self._db = database
        self._freshness_ttl_s = freshness_ttl_s
        self._handlers = dict(handlers)
        self.runs = RunRepository(database)
        self.events = RunEventRepository(database)
        self.objects = ObjectRepository(database)
        self.intents = IntentRepository(database)
        self.findings = FindingRepository(database)
        self.agents = AgentRepository(database)
        self.provenance = provenance or ProvenanceService(database)

    @property
    def operations(self) -> tuple[OperationType, ...]:
        return tuple(self._handlers)

    def definition(self, operation: OperationType) -> OperationDefinition:
        return self._resolve(operation).definition

    # ------------------------------------------------------------------------- create

    def create(
        self,
        operation: OperationType,
        record: ObjectRecord,
        management_path: ManagementPathVerdict,
        systemd_lifecycle_context: SystemdLifecycleContext | None = None,
    ) -> RunOutcome:
        """Plan the operation against ``record`` and publish the preview. Atomically.

        The plan is worked out twice: once before the write lock so an impossible request
        costs nothing and rolls nothing back, and once under it, because between the two an
        intent may have been revised, the object released or a sweep landed with a
        different value — and all three change what the plan says. What is published is
        the second one, so the preview describes the truth it was actually stored against.

        A refusal leaves nothing behind. There is no Run row for a request that could not
        be planned: a row saying "somebody asked and it was impossible" is, a week later,
        indistinguishable from a plan that was made and abandoned.
        """
        handler = self._resolve(operation)
        now = _now()
        self._plan_or_refuse(
            handler,
            self._context(record, now, management_path, systemd_lifecycle_context),
        )

        run_id = f"run_{uuid.uuid4().hex}"
        preview_id = f"prv_{uuid.uuid4().hex}"

        with self._db.transaction():
            current = self.objects.get(record.object_id)
            if current is None:
                raise RunRefused(
                    "object_not_found",
                    "the object disappeared while a run was being planned for it",
                    {"object_id": record.object_id},
                )
            now = _now()
            plan = self._plan_or_refuse(
                handler,
                self._context(current, now, management_path, systemd_lifecycle_context),
            )
            digest = plan_digest(plan)
            self.runs.insert_preview(_preview_row(preview_id, digest, plan, now))
            self.runs.insert_run(
                run_id=run_id,
                host_id=plan.host_id,
                object_id=plan.object_id,
                operation=str(plan.operation),
                state=str(RunState.PREVIEW),
                preview_id=preview_id,
                created_at=now,
            )
            # The transcript starts here, so that a Run's history is complete from the act
            # that created it rather than from the first thing that happened to it.
            self.events.append(
                event_id=f"evt_{uuid.uuid4().hex}",
                run_id=run_id,
                event=str(RunEvent.RUN_PLANNED),
                occurred_at=now,
                state_to=str(RunState.PREVIEW),
                detail={
                    "preview_id": preview_id,
                    "preview_digest": digest,
                    **_change_summary(plan.change),
                    "change_created": False,
                    "host_effect": "none",
                },
            )
            stored = self.runs.get(run_id)
            assert stored is not None  # written in this transaction

        LOG.info(
            "run planned",
            extra={
                "run_id": run_id,
                "preview_id": preview_id,
                "preview_digest": digest,
                "operation": str(plan.operation),
                "object_id": plan.object_id,
                "host_id": plan.host_id,
                **_change_summary(plan.change),
                "intent_id": plan.evidence.intent_id,
                "observation_id": plan.evidence.observation_id,
                "protection_status": str(plan.protection.status),
                "management_path": str(plan.protection.management_path),
                "risk": str(plan.risk.tier),
                "execution_availability": str(plan.execution.availability),
                "execution_eligibility": str(plan.execution.eligibility),
                "execution_blockers": list(plan.execution.blockers),
                "recovery_armed": plan.recovery.armed,
                "host_effect": "none",
            },
        )
        return RunOutcome(
            run=stored,
            plan=plan,
            validity=self.validity(
                stored, management_path, systemd_lifecycle_context=systemd_lifecycle_context
            ),
        )

    # --------------------------------------------------------------------------- read

    def get(self, run_id: str) -> RunRecord | None:
        return self.runs.get(run_id)

    def list_for_host(
        self,
        host_id: str,
        *,
        state: str | None = None,
        object_id: str | None = None,
        limit: int = 100,
    ) -> list[RunRecord]:
        return self.runs.list_for_host(
            host_id, state=state, object_id=object_id, limit=limit
        )

    def published_plan(self, run: RunRecord) -> RunPlan:
        """Reassemble the plan exactly as it was published, from the row that stored it."""
        return _plan_from_record(run)

    def replan(
        self,
        operation: OperationType,
        record: ObjectRecord,
        management_path: ManagementPathVerdict,
        systemd_lifecycle_context: SystemdLifecycleContext | None = None,
    ) -> RunPlan | PlanRefused:
        """Plan ``operation`` against ``record`` from current truth. Pure; writes nothing.

        Public because execution needs it. Re-deriving the gates for the request that is
        about to write is not the same act as comparing digests, and a gate enforced only
        as a side effect of a digest comparison is one that a later change to the digest
        could remove without anybody noticing.
        """
        handler = self._handlers.get(operation)
        if handler is None:
            return PlanRefused(
                "unsupported_operation",
                {
                    "operation": str(operation),
                    "supported_operations": sorted(str(o) for o in self._handlers),
                },
            )
        return handler.plan(
            self._context(
                record, _now(), management_path, systemd_lifecycle_context
            )
        )

    def validity(
        self,
        run: RunRecord,
        management_path: ManagementPathVerdict,
        *,
        systemd_lifecycle_context: SystemdLifecycleContext | None = None,
    ) -> PlanValidity:
        """Whether the published plan still describes what would happen if it ran.

        Pure. It re-plans from what LocalPlane has already recorded and compares the digest
        that comes out with the one that was published; it contacts nothing, refreshes
        nothing and writes nothing, including the timestamps of the records it reads. A
        reader asking whether a plan still holds must not be able to change the answer for
        the next reader — and must certainly not be able to change the plan.

        A Run whose operation this build no longer implements is stale rather than an
        error: the store outlives the code that wrote to it, and the honest answer to "does
        this plan still hold" when nothing can evaluate it is no.

        Both sides are canonicalised under the digest version the preview was published
        with, so a plan published by an earlier build is compared against what it would say
        now under its own rules rather than being declared stale by a change in how
        documents are hashed.
        """
        published = self.published_plan(run)
        if systemd_lifecycle_context is None:
            # A GET remains store-only.  Lifecycle proof is request-scoped and therefore
            # expires under the ordinary freshness horizon; until then, using the exact
            # immutable proof the preview stored lets validity detect target observation
            # changes without secretly contacting the host.
            systemd_lifecycle_context = published.lifecycle_context
        handler = (
            self._handlers.get(OperationType(run.operation))
            if _known(run.operation)
            else None
        )
        if handler is None:
            return assess_validity(
                published,
                run.preview.preview_digest,
                PlanRefused(
                    "unsupported_operation",
                    {
                        "operation": run.operation,
                        "supported_operations": sorted(str(o) for o in self._handlers),
                    },
                ),
                run.preview.digest_version,
            )
        record = self.objects.get(run.object_id)
        if record is None:
            return assess_validity(
                published,
                run.preview.preview_digest,
                PlanRefused("object_not_found", {"object_id": run.object_id}),
                run.preview.digest_version,
            )
        replanned = handler.plan(
            self._context(
                record, _now(), management_path, systemd_lifecycle_context
            )
        )
        return assess_validity(
            published, run.preview.preview_digest, replanned, run.preview.digest_version
        )

    # ------------------------------------------------------------------------- cancel

    #: The two states a Run may be cancelled from, and both are before the write boundary.
    #: There is deliberately no third. Cancelling during an apply leaves the interrupted
    #: step's effect unknown and sends the run to recovery, and cancelling during a
    #: rollback is not a rollback — so cancellation after the boundary
    #: is *refused* here rather than implemented as a state assignment. Setting
    #: ``state = 'cancelled'`` over a mutation that may have happened is precisely the lie
    #: the write-boundary rules make unstorable, and the schema agrees: a cancelled Run must
    #: carry ``host_effect = 'none'``.
    CANCELLABLE = (RunState.PREVIEW, RunState.AWAITING_CONFIRMATION)

    def cancel(self, run: RunRecord) -> RunRecord:
        """Cancel a Run before the write boundary. Nothing else moves.

        The easy cancellation, and the only one: nothing has been written, so nothing has to
        be put back. No Change is created, the host is untouched, the retained intent is
        untouched, reconciliation and findings are untouched, and the published preview
        stays exactly where it is — a cancelled plan is still a record of what somebody was
        shown and decided against.

        A Run that has been confirmed but not applied is still on this side of the boundary
        and cancels the same way; its confirmation is left recorded and unconsumed, because
        what happened is that somebody confirmed and then changed their mind, and a history
        that erased the first half would be a worse record than one that keeps both.

        Once an apply is under way this refuses. Cancelling during an apply, a verification
        or a rollback would have to answer what became of the step it interrupted, and the
        honest answer is that LocalPlane does not know — which is recovery, not
        cancellation. Interruption is not designed in this build, and refusing is how that
        is said out loud.
        """
        now = _now()
        with self._db.transaction():
            current = self.runs.get(run.run_id)
            if current is None:
                raise RunRefused(
                    "run_not_found",
                    "the run disappeared while it was being cancelled",
                    {"run_id": run.run_id},
                )
            if current.state not in {str(s) for s in self.CANCELLABLE}:
                raise RunRefused(
                    "run_not_cancellable",
                    "this run is not in a state that can be cancelled",
                    {
                        "run_id": current.run_id,
                        "state": current.state,
                        "cancellable_states": [str(s) for s in self.CANCELLABLE],
                    },
                )
            self.runs.cancel(run_id=current.run_id, now=now)
            self.events.append(
                event_id=f"evt_{uuid.uuid4().hex}",
                run_id=current.run_id,
                event=str(RunEvent.RUN_CANCELLED),
                occurred_at=now,
                state_from=current.state,
                state_to=str(RunState.CANCELLED),
                detail={"change_created": False, "host_effect": "none"},
            )
            cancelled = self.runs.get(current.run_id)
            assert cancelled is not None  # updated in this transaction

        LOG.info(
            "run cancelled",
            extra={
                "run_id": cancelled.run_id,
                "preview_id": cancelled.preview.preview_id,
                "object_id": cancelled.object_id,
                "operation": cancelled.operation,
                "change_created": False,
                "host_effect": "none",
            },
        )
        return cancelled

    # ---------------------------------------------------------------------- internals

    def _resolve(self, operation: OperationType) -> OperationHandler:
        handler = self._handlers.get(operation)
        if handler is None:
            raise RunRefused(
                "unsupported_operation",
                "LocalPlane does not implement this operation",
                {
                    "operation": str(operation),
                    "supported_operations": sorted(str(o) for o in self._handlers),
                },
            )
        return handler

    def _plan_or_refuse(
        self, handler: OperationHandler, context: PlanningContext
    ) -> RunPlan:
        planned = handler.plan(context)
        if isinstance(planned, PlanRefused):
            raise RunRefused(
                planned.code,
                _REFUSALS.get(planned.code, "this operation cannot be planned right now"),
                {"object_id": context.object.object_id, **planned.detail},
            )
        return planned

    def _context(
        self,
        record: ObjectRecord,
        now: str,
        management_path: ManagementPathVerdict,
        systemd_lifecycle_context: SystemdLifecycleContext | None = None,
    ) -> PlanningContext:
        """Assemble what a planner is allowed to see. Reads only; nothing is refreshed."""
        evidence = self.provenance.evidence(record.host_id)
        agent = self.agents.most_recent(record.host_id)
        capabilities = (
            frozenset(
                c.capability
                for c in self.agents.capabilities(agent.agent_instance_id)
                if c.status == "available"
            )
            if agent is not None
            else frozenset()
        )
        return PlanningContext(
            now=now,
            freshness_ttl_s=self._freshness_ttl_s,
            object=record,
            intent=self.intents.get(record.active_intent_id or ""),
            provenance=self.provenance.for_object(record, evidence),
            provider_readings={
                provider: reading.provider_observation_id
                for provider, reading in evidence.readings.items()
            },
            evidence_sources=capabilities,
            open_drift={
                finding.subject: finding.finding_id
                for finding in self.findings.open_for_object(record.object_id)
            },
            management_path=management_path,
            systemd_lifecycle_context=systemd_lifecycle_context,
        )


def _known(operation: str) -> bool:
    return operation in {str(o) for o in OperationType}


#: One sentence per way planning can be refused. The planners return a code and the typed
#: detail behind it; the prose lives here so that a caller sees the same shape whatever
#: declined the request, exactly as management refusals do.
_REFUSALS: dict[str, str] = {
    "object_observe_only": (
        "this object is observe-only; LocalPlane will never write to it, so there is "
        "nothing to plan"
    ),
    "not_managed": (
        "this object is not managed; adopt it before planning an operation that would "
        "reconcile it to a desired state LocalPlane does not hold"
    ),
    "active_intent_missing": "this object is managed but its active intent could not be read",
    "intent_schema_unsupported": (
        "this intent was written against a different controlled field set, so this build "
        "cannot say what reconciling it would mean"
    ),
    "field_not_controlled": (
        "the active intent does not control this field, so there is no intended value to "
        "reconcile towards; adopting the field is a decision made against verified evidence"
    ),
    "no_observation": (
        "this object has never been observed, so LocalPlane does not know what it would "
        "be changing"
    ),
    "observation_stale": (
        "the newest observation is not current enough to plan against; a plan made from an "
        "expired reading would describe a host nobody has looked at recently"
    ),
    "observation_source_incompatible": (
        "the newest observation comes from a different source than the intent, and the "
        "same field name from a different source is not the same fact"
    ),
    "current_value_unreadable": (
        "the value this operation would change could not be read, so LocalPlane cannot say "
        "what it would be changing it from — and an unreadable value is unknown, never a "
        "safe assumption"
    ),
    "already_reconciled": (
        "the runtime already carries the intended value; there is nothing to reconcile, "
        "and a plan that changed nothing would be work reported as done without any being "
        "done"
    ),
    "unsupported_operation": "LocalPlane does not implement this operation",
}


# ------------------------------------------------------------------- record translation


def _change_summary(change: PlannedChange) -> dict[str, Any]:
    """One plan's change, rendered for a transcript entry or a log line.

    The transcript and the log are read by people, so both shapes render into the same
    small set of keys and the kind is always among them. It is a rendering, never a source
    of truth: what the plan actually is lives in the columns :func:`_change_columns` writes.
    """
    if isinstance(change, PlannedAction):
        return {
            "change_kind": CHANGE_KIND_ACTION,
            "action": change.action,
            "observed_state": change.observed_state,
            "expected_state": change.expected_state,
        }
    return {
        "change_kind": CHANGE_KIND_FIELD,
        "field": change.field,
        "current_value": change.current,
        "desired_value": change.desired,
    }


def _change_columns(change: PlannedChange) -> dict[str, Any]:
    """The plan's change, as the columns of whichever half it belongs to.

    Both halves are written on every row and one of them is entirely null, which is what the
    store's paired CHECKs require — an action carrying a value or a field change carrying a
    verb is not a row this schema will hold. Writing the nulls explicitly rather than
    omitting the keys keeps one shape for the insert and makes the emptiness deliberate.
    """
    if isinstance(change, PlannedAction):
        return {
            "change_kind": CHANGE_KIND_ACTION,
            "field": None,
            "value_type": None,
            "current_value": None,
            "desired_value": None,
            "action": change.action,
            "observed_state": change.observed_state,
            "expected_state": change.expected_state,
        }
    return {
        "change_kind": CHANGE_KIND_FIELD,
        "field": change.field,
        "value_type": str(change.value_type),
        "current_value": encode(change.value_type, change.current),
        "desired_value": encode(change.value_type, change.desired),
        "action": None,
        "observed_state": None,
        "expected_state": None,
    }


def _change_from_record(preview: RunPreviewRecord) -> PlannedChange:
    """The inverse, read from the discriminator rather than by probing for nulls."""
    if preview.change_kind == CHANGE_KIND_ACTION:
        return PlannedAction(
            action=preview.action or "",
            observed_state=preview.observed_state or "",
            expected_state=preview.expected_state or "",
        )
    value_type = ValueType(preview.value_type or "")
    return PlannedFieldChange(
        field=preview.field or "",
        value_type=value_type,
        current=decode(value_type, int(preview.current_value or 0)),
        desired=decode(value_type, int(preview.desired_value or 0)),
    )


def _preview_row(preview_id: str, digest: str, plan: RunPlan, published_at: str) -> dict[str, Any]:
    """The published plan as columns.

    Everything the digest covers is stored, so recomputing it needs the row and nothing
    else. What is *not* stored is what the digest deliberately leaves out and what would
    be a second copy of a fact already here — the value recovery would restore is the
    current value, and the value verification would have to see is the desired one.
    """
    return {
        "preview_id": preview_id,
        "preview_digest": digest,
        "digest_version": PLAN_DIGEST_VERSION,
        "operation": str(plan.operation),
        **_change_columns(plan.change),
        "intent_id": plan.evidence.intent_id,
        "intent_version": plan.evidence.intent_version,
        "intent_capability": plan.evidence.intent_capability,
        "intent_provider": plan.evidence.intent_provider,
        "observation_id": plan.evidence.observation_id,
        "sweep_id": plan.evidence.sweep_id,
        "observed_at": plan.evidence.observed_at,
        "drift_finding_id": plan.evidence.drift_finding_id,
        "ownership_state": plan.ownership.state,
        "ownership_reason": plan.ownership.reason,
        "ownership_claims": to_json(
            [
                {
                    "relation": c.relation,
                    "provider": c.provider,
                    "instance": c.instance,
                    "label": c.label,
                    "confidence": c.confidence,
                    "external": c.external,
                }
                for c in plan.ownership.claims
            ]
        ),
        "ownership_gaps": to_json(list(plan.ownership.gaps)),
        "provider_readings": to_json(dict(plan.ownership.readings)),
        "protection_status": str(plan.protection.status),
        "protection_reasons": to_json(sorted(str(r) for r in plan.protection.reasons)),
        "protection_unresolved": to_json(sorted(str(r) for r in plan.protection.unresolved)),
        "protection_management_path": str(plan.protection.management_path),
        "protection_reason": plan.protection.reason,
        "protection_missing_evidence": to_json(list(plan.protection.missing_evidence)),
        # The record the judgement was made from. Bound, not hashed: a later observation
        # proving the same thing has confirmed the plan rather than changed it.
        "protection_evidence_id": (
            None if plan.lifecycle_context is not None else plan.protection.evidence_id
        ),
        "protection_evidence_observed_at": (
            None
            if plan.lifecycle_context is not None
            else plan.protection.evidence_observed_at
        ),
        "protection_assessments": to_json(
            [
                {
                    "reason": str(entry.reason),
                    "status": str(entry.status),
                    "detail": entry.detail,
                    "evidence_id": entry.evidence_id,
                    "observed_at": entry.observed_at,
                    "evidence": dict(entry.evidence),
                    "missing_evidence": list(entry.missing_evidence),
                }
                for entry in plan.protection.assessed
            ]
        ),
        "authorization_assessment": (
            None if plan.authorization is None else to_json(plan.authorization.as_dict())
        ),
        "lifecycle_context": (
            None if plan.lifecycle_context is None else to_json(plan.lifecycle_context.as_dict())
        ),
        "self_impact": (
            None if plan.self_impact is None else to_json(plan.self_impact.as_dict())
        ),
        "risk_tier": str(plan.risk.tier),
        "risk_factors": to_json(
            [
                {"code": f.code, "floor": str(f.floor), "detail": f.detail}
                for f in plan.risk.factors
            ]
        ),
        "confirmation_required": int(plan.confirmation.required),
        "confirmation_method": str(plan.confirmation.method),
        "confirmation_source": plan.confirmation.source,
        "confirmation_reasons": to_json(list(plan.confirmation.reasons)),
        "confirmation_policy": plan.confirmation.policy,
        "confirmation_token_issued": int(plan.confirmation.token_issued),
        "execution_availability": str(plan.execution.availability),
        "execution_eligibility": str(plan.execution.eligibility),
        "execution_blockers": to_json(list(plan.execution.blockers)),
        "execution_provider": plan.execution.provider,
        "required_capability": plan.execution.required_capability,
        "capability_declared": int(plan.execution.capability_declared_by_agent),
        "guard_availability": str(plan.guard.availability),
        "guard_reason": plan.guard.reason,
        "guard_window_s": plan.guard.window_s,
        "guard_prerequisites": to_json([str(g) for g in plan.guard.prerequisites]),
        "guard_unmet": to_json([str(g) for g in plan.guard.unmet]),
        "guard_guarantee": plan.guard.guarantee,
        # Never anything else, on any path, and the store CHECKs it: a published plan says
        # what arming would establish, and a document that could claim a live guard would
        # be one an operator could read as protection that does not exist yet.
        "guard_armed": 0,
        "recovery_mode": str(plan.recovery.mode),
        "recovery_rollback_possible": int(plan.recovery.rollback_possible),
        "recovery_armed": int(plan.recovery.armed),
        "recovery_guarantee": plan.recovery.guarantee,
        "recovery_reason": plan.recovery.reason,
        "verification_capability": plan.verification.capability,
        "verification_provider": plan.verification.provider,
        "verification_condition": plan.verification.condition,
        "verification_executed": int(plan.verification.executed),
        "published_at": published_at,
    }


def _plan_from_record(run: RunRecord) -> RunPlan:
    """The published plan, rebuilt from the row that stored it.

    The inverse of :func:`_preview_row`, and the reason the digest can be recomputed from
    the store alone: a plan that had to be re-derived through today's planner to be checked
    would not be content-addressed, it would merely be repeatable.
    """
    preview: RunPreviewRecord = run.preview
    return RunPlan(
        operation=OperationType(preview.operation),
        host_id=run.host_id,
        object_id=run.object_id,
        change=_change_from_record(preview),
        evidence=PlanEvidence(
            intent_id=preview.intent_id,
            intent_version=preview.intent_version,
            intent_capability=preview.intent_capability,
            intent_provider=preview.intent_provider,
            observation_id=preview.observation_id,
            sweep_id=preview.sweep_id,
            observed_at=preview.observed_at,
            drift_finding_id=preview.drift_finding_id,
        ),
        ownership=OwnershipAssessment(
            state=preview.ownership_state,
            reason=preview.ownership_reason,
            claims=tuple(
                PublishedClaim(
                    relation=c["relation"],
                    provider=c["provider"],
                    instance=c.get("instance"),
                    label=c.get("label"),
                    confidence=c["confidence"],
                    external=c["external"],
                )
                for c in preview.ownership_claims
            ),
            gaps=tuple(preview.ownership_gaps),
            readings=dict(preview.provider_readings),
        ),
        protection=_protection_from_record(preview),
        risk=RiskAssessment(
            tier=RiskTier(preview.risk_tier),
            factors=tuple(
                RiskFactor(code=f["code"], floor=RiskTier(f["floor"]), detail=f["detail"])
                for f in preview.risk_factors
            ),
        ),
        confirmation=ConfirmationRequirement(
            required=preview.confirmation_required,
            method=ConfirmationMethod(preview.confirmation_method),
            source=preview.confirmation_source,
            reasons=tuple(preview.confirmation_reasons),
            policy=preview.confirmation_policy,
            token_issued=preview.confirmation_token_issued,
            # Derived rather than stored: a plan is satisfiable when execution is
            # available, and no row here may say execution is available.
            satisfiable=preview.execution_availability == str(ExecutionAvailability.AVAILABLE),
            unsatisfiable_reason=(
                None
                if preview.execution_availability == str(ExecutionAvailability.AVAILABLE)
                else "execution_not_implemented"
            ),
        ),
        execution=ExecutionAssessment(
            availability=ExecutionAvailability(preview.execution_availability),
            eligibility=ExecutionEligibility(preview.execution_eligibility),
            blockers=tuple(preview.execution_blockers),
            provider=preview.execution_provider,
            required_capability=preview.required_capability,
            capability_declared_by_agent=preview.capability_declared,
        ),
        guard=GuardPlan(
            availability=GuardAvailability(preview.guard_availability),
            reason=preview.guard_reason,
            window_s=preview.guard_window_s,
            prerequisites=tuple(
                GuardPrerequisite(g) for g in preview.guard_prerequisites
            ),
            unmet=tuple(GuardPrerequisite(g) for g in preview.guard_unmet),
            guarantee=preview.guard_guarantee,
        ),
        recovery=RecoveryPlan(
            mode=RecoveryMode(preview.recovery_mode),
            rollback_possible=preview.recovery_rollback_possible,
            armed=preview.recovery_armed,
            guarantee=preview.recovery_guarantee,
            reason=preview.recovery_reason,
        ),
        verification=VerificationPlan(
            executed=preview.verification_executed,
            capability=preview.verification_capability,
            provider=preview.verification_provider,
            condition=preview.verification_condition,
        ),
        authorization=(
            AuthorizationAssessment.from_dict(preview.authorization_assessment)
            if preview.authorization_assessment is not None
            else None
        ),
        lifecycle_context=(
            SystemdLifecycleContext.from_dict(preview.lifecycle_context)
            if preview.lifecycle_context is not None
            else None
        ),
        self_impact=(
            BackendSelfImpactAssessment.from_dict(preview.self_impact)
            if preview.self_impact is not None
            else None
        ),
    )


def _protection_from_record(preview: RunPreviewRecord) -> ProtectionAssessment:
    """The published protection assessment, rebuilt from the columns that stored it.

    The per-reason breakdown is reconstructed rather than stored a second time: with the
    reasons that applied, the ones left unresolved and the roll-up all recorded, each
    reason's own status follows from which list it is in, and a second copy of a fact is a
    second thing that can disagree.

    Rows written before protection existed carry the relation they were published with and
    nothing else, and rebuild exactly as what they were: unknown, nothing proven, the
    management-path reason unresolved.
    """
    reasons = tuple(ProtectionReason(r) for r in preview.protection_reasons)
    unresolved = tuple(ProtectionReason(r) for r in preview.protection_unresolved)
    if preview.protection_assessments:
        assessed = tuple(
            ReasonAssessment(
                reason=ProtectionReason(entry["reason"]),
                status=ProtectionStatus(entry["status"]),
                detail=str(entry["detail"]),
                evidence_id=entry.get("evidence_id"),
                observed_at=entry.get("observed_at"),
                evidence=dict(entry.get("evidence") or {}),
                missing_evidence=tuple(entry.get("missing_evidence") or ()),
            )
            for entry in preview.protection_assessments
        )
    else:
        assessed = tuple(
            ReasonAssessment(
                reason=reason,
                status=(
                    ProtectionStatus.PROTECTED
                    if reason in reasons
                    else ProtectionStatus.UNKNOWN
                    if reason in unresolved
                    else ProtectionStatus.CLEAR
                ),
                detail=preview.protection_reason,
                evidence_id=preview.protection_evidence_id,
                observed_at=preview.protection_evidence_observed_at,
            )
            for reason in (reasons + unresolved or (ProtectionReason.MANAGEMENT_PATH,))
        )
    return ProtectionAssessment(
        status=ProtectionStatus(preview.protection_status),
        reasons=reasons,
        unresolved=unresolved,
        assessed=assessed,
        management_path=ManagementPathRelation(preview.protection_management_path),
        reason=preview.protection_reason,
        missing_evidence=tuple(preview.protection_missing_evidence),
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
