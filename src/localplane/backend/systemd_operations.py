"""The three closed systemd service lifecycle operations, and the executor behind them.

``start``, ``stop`` and ``restart`` against one already-loaded canonical service unit. They
differ in exactly three things — the verb dispatched, the state the target must be in for the
plan to make sense, and what a verification must see afterwards — so they are three
definitions over one planner and one executor. There is no fourth verb, in this module or in
the protocol, and adding one would be a visible change in several files.

**Nothing here can be steered from outside.** The verb comes from the operation the caller
*named*, out of a closed enum, never from a field in a request; the target comes from the
object id and resolves to a canonical unit name the agent re-checks against systemd's own
syntax; and there is no D-Bus object path, interface, member, mode, timeout or property
anywhere on the path.

**Authorization is systemd's, decided at dispatch.** Nothing is preflighted — an unprivileged
caller cannot reproduce the decision systemd makes with its own trusted unit and verb details
— so the preview says so and a refusal comes back as a truthful ``not_written``.

**There is no rollback, and that absence is deliberate and complete.** The inverse of
``start`` is not ``stop``: issuing the opposite verb because a verification failed is a second
change nobody asked for against a service whose state is already not what was expected. So
``recovery_mode`` is ``none``, no checkpoint is armed, no connection guard is offered, and a
change that cannot be proven ends ``recovery_required`` saying what was asked for and what was
seen instead.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from localplane.backend.agent_client import AgentClient, AgentError
from localplane.backend.db.repositories import ObjectRecord, ObjectRepository
from localplane.backend.domain.changes import (
    ExecutionRefused,
    Expectation,
    MutationOutcome,
    MutationReport,
    MutationRequest,
    ProofOfState,
    VerificationOutcome,
)
from localplane.backend.domain.guard import (
    GuardArmed,
    GuardAvailability,
    GuardPhase,
    GuardPlan,
    GuardRefused,
    GuardReport,
    GuardRequest,
)
from localplane.backend.domain.identity import OBJECT_KIND_SYSTEMD_UNIT
from localplane.backend.domain.policy import (
    OperationDefinition,
    assess_execution,
    assess_risk,
    effective_confirmation,
)
from localplane.backend.domain.runs import (
    ExecutionAvailability,
    OperationType,
    OwnershipAssessment,
    PlanEvidence,
    PlannedAction,
    PlanRefused,
    PublishedClaim,
    RecoveryMode,
    RecoveryPlan,
    RiskTier,
    RunPlan,
    VerificationPlan,
)
from localplane.backend.domain.self_impact import assess_backend_self_impact
from localplane.backend.domain.states import Freshness, derive_freshness
from localplane.backend.domain.systemd_lifecycle import (
    SYSTEMD_SERVICE_LIFECYCLE,
    AuthorizationAssessment,
    SystemdServiceAction,
    assess_lifecycle_protection,
    lifecycle_applicability,
)
from localplane.backend.ingest import ObservationCoordinator
from localplane.backend.runs import ObservationAttempt, PlanningContext, TypedOperation
from localplane.protocol.capabilities import CAPABILITY_SYSTEMD_UNITS_OBSERVE
from localplane.protocol.providers import PROVIDER_SYSTEMD


_OPERATION = {
    SystemdServiceAction.START: OperationType.SYSTEMD_SERVICE_START,
    SystemdServiceAction.STOP: OperationType.SYSTEMD_SERVICE_STOP,
    SystemdServiceAction.RESTART: OperationType.SYSTEMD_SERVICE_RESTART,
}
_EXPECTED = {
    SystemdServiceAction.START: "active",
    SystemdServiceAction.STOP: "inactive",
    SystemdServiceAction.RESTART: "active_with_new_invocation_id",
}
_SUMMARY = {
    SystemdServiceAction.START: (
        "Ask the system manager to start one existing canonical service unit and verify "
        "the resulting service state from a fresh targeted observation."
    ),
    SystemdServiceAction.STOP: (
        "Ask the system manager to stop one existing canonical service unit and verify "
        "the result without treating unit garbage collection as proof by itself."
    ),
    SystemdServiceAction.RESTART: (
        "Ask the system manager to restart one active canonical service unit and verify "
        "both the final state and a changed execution InvocationID."
    ),
}
_VERIFICATION = {
    SystemdServiceAction.START: (
        "fresh targeted systemd observation must prove a supported successful active "
        "service shape; job acceptance alone is not success"
    ),
    SystemdServiceAction.STOP: (
        "fresh targeted systemd evidence plus the correlated job result must prove the "
        "service stopped; GetUnit absence alone is not sufficient"
    ),
    SystemdServiceAction.RESTART: (
        "fresh targeted systemd observation must prove a supported active service shape "
        "and a non-null InvocationID different from the dispatch baseline"
    ),
}


def _definition(action: SystemdServiceAction) -> OperationDefinition:
    return OperationDefinition(
        operation=str(_OPERATION[action]),
        summary=_SUMMARY[action],
        target_kind=OBJECT_KIND_SYSTEMD_UNIT,
        base_risk=RiskTier.MEDIUM,
        destructive=False,
        can_affect_management_path=True,
        guarded_execution=False,
        recovery_mode=RecoveryMode.NONE,
        rollback_possible=False,
        required_capability=SYSTEMD_SERVICE_LIFECYCLE,
        acts_through_provider=PROVIDER_SYSTEMD,
        verification_condition=_VERIFICATION[action],
        execution_note=(
            "One closed systemd D-Bus dispatch against the canonical Unit.Id bound here: "
            "Manager.GetUnit resolves it, the Unit object's own Start, Stop or Restart is "
            "called with a fixed mode, and the exact returned job is awaited. Callers supply "
            "neither a unit name nor any D-Bus field. Authorization is decided by systemd at "
            "dispatch and is never preflighted; a refusal is a truthful not_written."
        ),
    )


def _planner(action: SystemdServiceAction, definition: OperationDefinition):
    operation = _OPERATION[action]

    def plan(context: PlanningContext) -> RunPlan | PlanRefused:
        record = context.object
        observation = record.observation
        if observation is None:
            return PlanRefused("no_observation", {})
        freshness, age = derive_freshness(
            observation.observed_at_dt,
            context.freshness_ttl_s,
            now=_parse(context.now),
        )
        if freshness is not Freshness.CURRENT:
            return PlanRefused(
                "observation_stale",
                {
                    "observation_id": observation.observation_id,
                    "freshness": str(freshness),
                    "age_seconds": age,
                    "ttl_seconds": context.freshness_ttl_s,
                },
            )
        if (
            observation.capability != CAPABILITY_SYSTEMD_UNITS_OBSERVE
            or observation.provider != PROVIDER_SYSTEMD
        ):
            return PlanRefused(
                "observation_source_incompatible",
                {
                    "expected_capability": CAPABILITY_SYSTEMD_UNITS_OBSERVE,
                    "expected_provider": PROVIDER_SYSTEMD,
                    "observation_capability": observation.capability,
                    "observation_provider": observation.provider,
                },
            )
        if record.identity_value != observation.facts.get("canonical_id"):
            return PlanRefused(
                "systemd_canonical_identity_mismatch",
                {
                    "identity_value": record.identity_value,
                    "observed_canonical_id": observation.facts.get("canonical_id"),
                },
            )

        lifecycle = context.systemd_lifecycle_context
        if lifecycle is None:
            return PlanRefused("systemd_lifecycle_context_missing", {})
        context_time = _parse(lifecycle.observed_at)
        if context_time is None:
            return PlanRefused("systemd_lifecycle_context_unreadable", {})
        context_freshness, context_age = derive_freshness(
            context_time,
            context.freshness_ttl_s,
            now=_parse(context.now),
        )
        if context_freshness is not Freshness.CURRENT:
            return PlanRefused(
                "systemd_lifecycle_context_stale",
                {
                    "freshness": str(context_freshness),
                    "age_seconds": context_age,
                    "ttl_seconds": context.freshness_ttl_s,
                },
            )
        if lifecycle.action is not action:
            return PlanRefused(
                "systemd_lifecycle_context_action_mismatch",
                {"expected": str(action), "observed": str(lifecycle.action)},
            )

        refusals = lifecycle_applicability(
            lifecycle, expected_canonical_id=record.identity_value
        )
        if refusals:
            return PlanRefused(
                "systemd_service_not_applicable",
                {"reasons": list(refusals), "canonical_unit": record.identity_value},
            )

        protection = assess_lifecycle_protection(lifecycle)
        # Derived *from* the protection above and changing nothing about it. What it adds
        # is which hazard inside that verdict is LocalPlane's own backend, and whether that
        # exact hazard is one a later authority could be issued against; no execution path
        # in this build reads it, and no authority exists for it to grant.
        self_impact = assess_backend_self_impact(lifecycle, protection)
        risk = assess_risk(
            definition,
            ownership_block_reason=None,
            ownership_gaps=(),
            protection=protection,
        )
        guard = GuardPlan(
            availability=GuardAvailability.UNAVAILABLE,
            reason="operation_has_no_safe_automatic_inverse",
            window_s=0,
            prerequisites=(),
            unmet=(),
            guarantee="no connection guard is offered for systemd lifecycle actions",
        )
        capability = definition.required_capability in context.evidence_sources
        execution = assess_execution(
            definition,
            # LocalPlane has code that would execute this. Whether *this plan* may run is a
            # separate question with its own answer below, and a plan can be perfectly
            # executable in principle and completely ineligible in fact.
            availability=ExecutionAvailability.AVAILABLE,
            provider=PROVIDER_SYSTEMD,
            capability_declared=capability,
            ownership_block_reason=None,
            ownership_gaps=(),
            protection=protection,
            guard=guard,
            self_impact=self_impact,
        )
        confirmation = effective_confirmation(
            definition,
            risk=risk,
            protection=protection,
            # A confirmation that can be satisfied, now that an executor exists. Whether the
            # plan may then proceed is still eligibility's answer, and eligibility is where
            # a protected target and the self-impact hazard are decided.
            execution_available=True,
        )
        service_state = str(lifecycle.target_facts.get("active_state") or "unknown")
        authorization = AuthorizationAssessment.not_preflighted(
            record.identity_value, action
        )
        return RunPlan(
            operation=operation,
            host_id=record.host_id,
            object_id=record.object_id,
            change=PlannedAction(
                action=str(action),
                observed_state=service_state,
                expected_state=_EXPECTED[action],
            ),
            evidence=PlanEvidence(
                observation_id=observation.observation_id,
                sweep_id=observation.sweep_id,
                observed_at=observation.observed_at,
            ),
            ownership=OwnershipAssessment(
                state="attributed",
                reason="systemd_is_authoritative_for_loaded_unit_runtime",
                claims=(
                    PublishedClaim(
                        relation="configured_by",
                        provider=PROVIDER_SYSTEMD,
                        instance=record.identity_value,
                        label=record.display_name,
                        confidence="confirmed",
                        external=True,
                    ),
                ),
                gaps=(),
                readings=dict(context.provider_readings),
            ),
            protection=protection,
            risk=risk,
            confirmation=confirmation,
            execution=execution,
            guard=guard,
            recovery=RecoveryPlan(
                mode=RecoveryMode.NONE,
                rollback_possible=False,
                armed=False,
                guarantee="none",
                reason=(
                    "start, stop and restart have no trustworthy unattended inverse; "
                    "Connection Guard and automatic rollback do not apply"
                ),
            ),
            verification=VerificationPlan(
                executed=False,
                capability=CAPABILITY_SYSTEMD_UNITS_OBSERVE,
                provider=PROVIDER_SYSTEMD,
                condition=definition.verification_condition,
            ),
            authorization=authorization,
            lifecycle_context=lifecycle,
            self_impact=self_impact,
        )

    return plan


#: The correlation an action carries to the boundary. Not rollback material — there is none
#: — but the identity the dispatch needs and the evidence a verification is judged against.
#: The canonical unit name is the identity; a D-Bus object path is a handle inside one
#: connection and is never persisted, never sent and never compared.
CORRELATION_UNIT = "unit_id"
CORRELATION_INVOCATION_ID = "invocation_id"
CORRELATION_ACTIVE_STATE = "active_state"
CORRELATION_SUB_STATE = "sub_state"

#: The sub-states a running service may be in for an ``active`` reading to count as the
#: supported successful shape. Anything else — ``start-pre``, ``auto-restart``, ``reload`` —
#: is a service mid-transition, and a plan is not verified by catching it in one.
_RUNNING_SUB_STATES = frozenset({"running", "exited"})


class SystemdLifecycleExecutor:
    """The execution half of the three systemd lifecycle operations.

    One class parameterised by the verb it carries out, because the three differ only in the
    verb and in what a verification must see. It decides nothing about policy: whether this
    unit may be acted on, whether an operator confirmed it, whether they also accepted that
    it might interrupt LocalPlane itself, and whether the plan was still current were all
    settled before any method here is called.

    **There is no inverse and no guard.** The opposite verb is not a rollback: issuing
    ``stop`` because a ``start`` could not be verified is a second change nobody asked for,
    against a service whose state is already not what was expected. A service that will not
    stay running wants an operator, not another instruction.
    """

    def __init__(
        self,
        action: SystemdServiceAction,
        client: AgentClient,
        coordinator: ObservationCoordinator,
        objects: ObjectRepository,
    ) -> None:
        self._action = action
        self._client = client
        self._coordinator = coordinator
        self._objects = objects

    # ----------------------------------------------------------------------- correlate

    def correlate(self, record: ObjectRecord) -> dict[str, Any]:
        """The material a dispatch needs, read from this object's newest reading.

        The canonical unit id is the identity — the same value LocalPlane's own object id is
        derived from, so there is no second identity system and no alias being trusted. The
        execution generation travels with it, because that is what a verification of a
        *restart* is judged against: a service running now that was running at dispatch has
        not been shown to have restarted, and only the reading taken at dispatch can settle
        it.

        Refusing here happens before the boundary and leaves no Change.
        """
        observation = record.observation
        if observation is None:
            raise ExecutionRefused("no_observation", {"object_id": record.object_id})
        canonical = observation.facts.get("canonical_id")
        if not isinstance(canonical, str) or canonical != record.identity_value:
            raise ExecutionRefused(
                "execution_identity_unreadable",
                {"object_id": record.object_id,
                 "observation_id": observation.observation_id,
                 "identity_value": record.identity_value,
                 "observed_canonical_id": canonical},
            )
        invocation = observation.facts.get(CORRELATION_INVOCATION_ID)
        if self._action is SystemdServiceAction.RESTART and not isinstance(invocation, str):
            # A restart with no baseline could never be proven afterwards, and dispatching a
            # change whose result is unverifiable by construction is not something to start.
            raise ExecutionRefused(
                "restart_baseline_invocation_id_missing",
                {"object_id": record.object_id,
                 "observation_id": observation.observation_id},
            )
        return {
            CORRELATION_UNIT: canonical,
            CORRELATION_INVOCATION_ID: invocation if isinstance(invocation, str) else None,
            CORRELATION_ACTIVE_STATE: observation.facts.get("active_state"),
            CORRELATION_SUB_STATE: observation.facts.get("sub_state"),
            "observation_id": observation.observation_id,
        }

    # -------------------------------------------------------------------------- mutate

    def mutate(self, request: MutationRequest) -> MutationReport:
        """Ask the system manager to carry out this operation's verb. Never raises.

        Every way this can go wrong is one of the three outcomes, because a caller that had
        to interpret an exception would be deciding what it meant about the host.

        The verb sent is **this executor's own**, not the one on the request: the request
        carries it because the Change recorded it, and the two are checked against each other
        so a Change built for one operation cannot be dispatched by another's executor. That
        check can only refuse.

        The agent's answer is taken exactly as given. It already distinguishes a refusal
        before any job existed from a job that was enqueued and whose end was never heard,
        and softening either here would discard the one distinction the whole path exists to
        preserve.
        """
        unit_id = str(request.correlation[CORRELATION_UNIT])
        if request.action != str(self._action):
            return MutationReport(
                outcome=MutationOutcome.NOT_WRITTEN,
                reason="action_mismatch",
                attempt_id=request.attempt_id,
                detail={"recorded": request.action, "executor": str(self._action)},
            )
        try:
            answer = self._client.systemd_service_lifecycle(
                attempt_id=request.attempt_id, unit_id=unit_id, action=str(self._action)
            )
        except AgentError as exc:
            # The branch that carries the whole distinction: the request either had not left
            # this process, in which case nothing can have happened, or it had, in which case
            # the manager may already have run a job. The client records which.
            return MutationReport(
                outcome=(
                    MutationOutcome.WRITE_UNKNOWN
                    if exc.dispatched
                    else MutationOutcome.NOT_WRITTEN
                ),
                reason=exc.code,
                attempt_id=request.attempt_id,
                detail={"agent_error": exc.as_dict(), "dispatched": exc.dispatched,
                        "unit_id": unit_id},
            )

        mutation = answer.get("mutation") or {}
        try:
            outcome = MutationOutcome(mutation.get("outcome"))
        except ValueError:
            # The agent answered with something this build cannot read. The request was
            # certainly dispatched, so the honest reading of an unintelligible answer is that
            # the manager may have acted on it.
            return MutationReport(
                outcome=MutationOutcome.WRITE_UNKNOWN,
                reason="unreadable_mutation_outcome",
                attempt_id=request.attempt_id,
                detail={"mutation": mutation, "unit_id": unit_id},
            )
        return MutationReport(
            outcome=outcome,
            reason=str(mutation.get("reason") or "unstated"),
            attempt_id=request.attempt_id,
            provider=mutation.get("provider"),
            provider_version=mutation.get("provider_version"),
            method=mutation.get("method"),
            detail={
                "unit_id": unit_id,
                "action": mutation.get("action"),
                "job_id": mutation.get("job_id"),
                "job_result": mutation.get("job_result"),
                "invocation_id_at_dispatch": mutation.get("invocation_id"),
                "manager_detail": mutation.get("detail") or {},
                "agent_instance_id": answer.get("agent_instance_id"),
            },
        )

    # --------------------------------------------------------------------------- prove

    def prove(self, facts: Mapping[str, Any], expectation: Expectation) -> ProofOfState:
        """Whether this reading proves the verb took effect.

        **A job that completed is not success.** ``done`` says the manager carried a
        transaction out; whether the service is now in the state the plan promised is a
        different question, and only a reading through the ordinary observation path answers
        it.

        **For ``restart`` the state is not enough, and this is where systemd's own evidence
        does the work.** A restart leaves a service active, and so does doing nothing at all
        to a service that was already active — indistinguishable from the state alone, which
        is exactly the kind of verification that verifies nothing. So the execution
        generation is compared with the one read at dispatch, and a restart is proven only if
        it moved. That is the manager's own record of the event, not an inference.
        """
        state = facts.get("active_state")
        if not isinstance(state, str) or not state:
            return ProofOfState(
                VerificationOutcome.VALUE_UNREADABLE, reason="active_state_unreadable"
            )
        if state != expectation.state and not (
            self._action is SystemdServiceAction.RESTART and state == "active"
        ):
            return ProofOfState(
                VerificationOutcome.MISMATCH, state=state, reason="observed_state_differs"
            )
        if self._action is not SystemdServiceAction.STOP:
            sub_state = facts.get("sub_state")
            if sub_state not in _RUNNING_SUB_STATES:
                # Active, and mid-transition. A service caught in `start-pre` or
                # `auto-restart` has not settled into the shape the plan promised.
                return ProofOfState(
                    VerificationOutcome.MISMATCH, state=state,
                    reason="service_sub_state_not_a_settled_running_shape",
                )
        if self._action is not SystemdServiceAction.RESTART:
            return ProofOfState(VerificationOutcome.VERIFIED, state=state)

        observed = facts.get(CORRELATION_INVOCATION_ID)
        if not isinstance(observed, str) or not observed:
            return ProofOfState(
                VerificationOutcome.VALUE_UNREADABLE, state=state,
                reason="invocation_id_unreadable",
            )
        baseline = expectation.correlation.get(CORRELATION_INVOCATION_ID)
        if observed == baseline:
            return ProofOfState(
                VerificationOutcome.MISMATCH, state=state,
                reason="service_did_not_start_a_new_execution",
            )
        return ProofOfState(VerificationOutcome.VERIFIED, state=state)

    # ------------------------------------------------------------------------- observe

    def observe(self, record: ObjectRecord) -> ObservationAttempt:
        """Re-read this unit through the ordinary targeted observation path.

        The *ordinary* one, deliberately: the same provider, the same normalisation and the
        same store every other judgement in LocalPlane rests on. A verification that trusted
        a private read would be checking the change against the code that made it.
        """
        try:
            result = self._coordinator.refresh_systemd_unit(record.identity_value)
        except AgentError as exc:
            return ObservationAttempt(
                record=None, failure="observation_unavailable", detail=exc.as_dict()
            )
        fresh = self._objects.get(record.object_id)
        if fresh is None or fresh.observation is None:
            return ObservationAttempt(
                record=None, failure="target_absent", detail={"object_id": record.object_id}
            )
        if fresh.observation.sweep_id != result.sweep_id:
            # The targeted read ran and did not see this unit — it was unloaded while the
            # change was in flight. Handing back its previous observation would let a
            # verification "prove" a state nobody has looked at since, which is precisely how
            # a unit that vanished mid-change would be reported as fine.
            return ObservationAttempt(
                record=None,
                failure="target_absent",
                detail={"object_id": record.object_id, "sweep_id": result.sweep_id,
                        "newest_observation_sweep_id": fresh.observation.sweep_id},
            )
        return ObservationAttempt(record=fresh)

    # ---------------------------------------------------------------- connection guard

    def arm_guard(self, request: GuardRequest) -> GuardArmed:
        """There is no connection guard for a lifecycle action, and there could not be.

        A guard is a *reversal* held on the host with a deadline. A service action has no
        reversal: the inverse of ``start`` is not ``stop``, it is a second change nobody
        asked for against a service whose state is already not what was expected.

        The refusal is here rather than assumed absent, so that a build which one day offered
        a guarded service change would have to write the reversal to remove it.
        """
        raise GuardRefused(
            "operation_has_no_unattended_reversal",
            {"action": str(self._action), "guard_id": request.guard_id},
        )

    def disarm_guard(self, guard_id: str) -> GuardReport:
        """Nothing was ever armed, so nothing is holding anything. Never raises."""
        return GuardReport(
            phase=GuardPhase.LOST,
            reason="operation_has_no_unattended_reversal",
            detail={"guard_id": guard_id, "action": str(self._action)},
        )

    def guard_status(self, guard_id: str) -> GuardReport:
        return self.disarm_guard(guard_id)


def build_executors(
    client: AgentClient, coordinator: ObservationCoordinator, objects: ObjectRepository
) -> dict[OperationType, SystemdLifecycleExecutor]:
    """Bind each systemd lifecycle operation to the executor that carries it out."""
    return {
        _OPERATION[action]: SystemdLifecycleExecutor(action, client, coordinator, objects)
        for action in SystemdServiceAction
    }


DEFINITIONS = {
    action: _definition(action) for action in SystemdServiceAction
}
OPERATIONS = {
    _OPERATION[action]: TypedOperation(
        definition=DEFINITIONS[action],
        planner=_planner(action, DEFINITIONS[action]),
    )
    for action in SystemdServiceAction
}


def _parse(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None

