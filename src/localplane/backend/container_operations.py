"""The three Docker container lifecycle operations, and the one executor behind them.

The second concrete half of the Change Engine seam, and the first *action*. Everything
before this reconciled a retained value: LocalPlane held a desired MTU, the runtime
disagreed, and executing meant writing the value LocalPlane already had. Starting a
container is not that. There is no retained value, nothing has drifted, and what executing
means is asking Docker to carry out a verb.

**Three operations, one implementation, and that is a fact about them rather than a
shortcut.** ``start``, ``stop`` and ``restart`` differ in exactly three things — the verb
they send, the state the target must be in for the plan to make sense, and what a
verification has to see afterwards — so they are three :class:`ContainerLifecycle`
definitions over one planner and one executor. A fourth verb is not one line away, because
there is no fourth in :class:`~localplane.backend.domain.docker.LifecycleAction` and no
fourth in the protocol's tuple; adding one would be a visible change in three files.

**Nothing here can be steered from outside.** The verb comes from the operation the caller
*named*, out of a closed enum, and never from a field in a request; the target comes from
the object id and resolves to a container id that the agent re-checks against Docker's own
id syntax; and there is no timeout, signal, payload or path anywhere on the path.

**There is no rollback, and that absence is deliberate and complete.** The inverse of
``start`` is not ``stop``: issuing the opposite verb because a verification failed would be
a second change nobody asked for, against a container whose state is already not what was
expected. A container that will not stay running wants an operator, not another
instruction. So ``recovery_mode`` is ``none``, no checkpoint is armed, the Change carries
no rollback columns — the store refuses one that does — and a change that cannot be proven
ends ``recovery_required`` saying what was asked for and what was seen instead.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from localplane.backend.agent_client import AgentClient, AgentError
from localplane.backend.db.repositories import ObjectRecord, ObjectRepository
from localplane.backend.domain.guard import (
    GuardArmed,
    GuardPhase,
    GuardRefused,
    GuardReport,
    GuardRequest,
)
from localplane.backend.domain.changes import (
    ExecutionRefused,
    Expectation,
    MutationOutcome,
    MutationReport,
    MutationRequest,
    ProofOfState,
    VerificationOutcome,
)
from localplane.backend.domain.docker import (
    OBJECT_KIND_DOCKER_CONTAINER,
    LifecycleAction,
    LifecycleState,
    is_running,
    parse_docker_instant,
)
from localplane.backend.domain.policy import (
    OperationDefinition,
    assess_execution,
    assess_guard,
    assess_protection,
    assess_risk,
    effective_confirmation,
    ownership_gate,
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
from localplane.backend.domain.states import Freshness, ManagementState, derive_freshness
from localplane.backend.ingest import ObservationCoordinator
from localplane.backend.runs import ObservationAttempt, PlanningContext, TypedOperation
from localplane.protocol.capabilities import (
    CAPABILITY_DOCKER_CONTAINER_LIFECYCLE,
    CAPABILITY_DOCKER_CONTAINERS_OBSERVE,
)
from localplane.protocol.providers import PROVIDER_DOCKER

#: The provider that carries out the change. Not a plausible-sounding name: it is the daemon
#: that receives the request, and it is also the system that already owns the container —
#: which is what makes these operations executable at all. See
#: :func:`~localplane.backend.domain.policy.ownership_gate`.
EXECUTION_PROVIDER = PROVIDER_DOCKER

#: What the checkpoint-shaped material carries for an action. Not rollback material — there
#: is none — but what the executor needs to reach the target and what a verification of a
#: restart is judged against.
CORRELATION_CONTAINER_ID = "container_id"
CORRELATION_STARTED_AT = "started_at"
CORRELATION_STATE = "state"


def _definition(action: LifecycleAction, operation: OperationType) -> OperationDefinition:
    """One lifecycle verb, declared. The three differ only in the sentences."""
    return OperationDefinition(
        operation=str(operation),
        summary=_SUMMARY[action],
        target_kind=OBJECT_KIND_DOCKER_CONTAINER,
        # Medium, and the reason is the host rather than the container. Starting a container
        # binds host ports and makes the daemon program packet-filter rules for them;
        # stopping one takes a workload away from whatever depends on it. The corresponding
        # Compose operations range from `low` to `medium`; this build rates all three
        # `medium`, because the cheapest of them still changes what the host is listening on
        # and LocalPlane has no evidence about what depends on that.
        base_risk=RiskTier.MEDIUM,
        # Not destructive. Nothing is removed: a stopped container keeps its writable layer,
        # its volumes and its configuration, and starting it again is available. "Nothing to
        # roll back" and "cannot be undone" are different things, and this is the first.
        destructive=False,
        # A container is not the resource the management-path model identifies. That model
        # answers which *network interface* carries the connection an operator arrived on,
        # and no container is one — so demanding a proof about it before acting on a
        # container would be demanding proof of an unrelated fact, and would make every
        # Docker operation impossible on a loopback deployment for no safety gained.
        #
        # What this does not cover is stated rather than implied: a container that provides
        # the operator's route — a host-network proxy, a VPN — could still end a session, and
        # LocalPlane has no evidence linking one to a management path. That is a real
        # limitation of this build and it is recorded as one.
        can_affect_management_path=False,
        recovery_mode=RecoveryMode.NONE,
        rollback_possible=False,
        required_capability=CAPABILITY_DOCKER_CONTAINER_LIFECYCLE,
        # The whole reason a container Docker created and configures can be operated at all.
        acts_through_provider=PROVIDER_DOCKER,
        verification_condition=_VERIFICATION[action],
        execution_note=(
            "The change is one POST to the Docker daemon's own API — "
            f"/containers/<id>/{action} — with the verb taken from this operation's "
            "definition and the id from the observation the plan was made against. There is "
            "no path, payload, command, argv, shell, signal or timeout that a caller can "
            "supply at any layer between the request and the daemon, and the agent refuses "
            "any verb not in the protocol's three-member tuple before a request exists."
        ),
    )


_SUMMARY: dict[LifecycleAction, str] = {
    LifecycleAction.START: (
        "Ask Docker to start this container, and prove from a fresh observation that it is "
        "running afterwards."
    ),
    LifecycleAction.STOP: (
        "Ask Docker to stop this container — its own grace period, then a kill — and prove "
        "from a fresh observation that it has exited."
    ),
    LifecycleAction.RESTART: (
        "Ask Docker to restart this container, and prove from a fresh observation both that "
        "it is running and that it started again rather than never having stopped."
    ),
}

_VERIFICATION: dict[LifecycleAction, str] = {
    LifecycleAction.START: (
        "re-observe this container through the Docker containers capability and require its "
        "lifecycle state to be running"
    ),
    LifecycleAction.STOP: (
        "re-observe this container through the Docker containers capability and require its "
        "lifecycle state to be exited"
    ),
    LifecycleAction.RESTART: (
        "re-observe this container through the Docker containers capability and require both "
        "that its lifecycle state is running and that the instant Docker records it as "
        "having started is later than the one observed when the change was dispatched — "
        "which is what tells a restart that happened from a container that never stopped"
    ),
}

#: What each verb must leave behind. Docker's own state names, because an operator reading a
#: LocalPlane plan should see the word they would see in ``docker ps``.
EXPECTED_STATE: dict[LifecycleAction, str] = {
    LifecycleAction.START: str(LifecycleState.RUNNING),
    LifecycleAction.STOP: str(LifecycleState.EXITED),
    LifecycleAction.RESTART: str(LifecycleState.RUNNING),
}

#: The states from which each verb has nothing to do, and the code that says so. A plan
#: LocalPlane would publish here is work that would be reported as done without any being
#: done — the same refusal ``already_reconciled`` is for a field that already matches.
_NO_OP: dict[LifecycleAction, tuple[frozenset[str], str]] = {
    LifecycleAction.START: (
        frozenset({str(LifecycleState.RUNNING)}), "container_already_running"
    ),
    LifecycleAction.STOP: (
        frozenset(
            {
                str(LifecycleState.EXITED),
                str(LifecycleState.CREATED),
                str(LifecycleState.DEAD),
            }
        ),
        "container_already_stopped",
    ),
    LifecycleAction.RESTART: (frozenset(), ""),
}

#: States in which the container is mid-transition and the daemon's answer to any verb would
#: be about a moving target. Refused for every verb, with the state named.
_TRANSIENT = frozenset({str(LifecycleState.RESTARTING), str(LifecycleState.REMOVING)})

#: ``start`` on a paused container is refused by Docker itself with a conflict, and
#: unpausing is not one of the three verbs this build has. Naming it is more useful than
#: dispatching something that will come back 409.
_PAUSED = str(LifecycleState.PAUSED)


def plan_container_lifecycle(
    action: LifecycleAction, operation: OperationType, definition: OperationDefinition
):
    """Build the planner for one verb. A closure over three constants, not a framework."""

    def plan(context: PlanningContext) -> RunPlan | PlanRefused:
        """What this verb would involve against this container, or the refusal.

        The preconditions are checked in the order they stop mattering, which is the
        discipline every other planner follows and for the same reason: a caller told about
        observation freshness when the real problem is that the container is already running
        has been sent to fix the wrong thing.
        """
        record = context.object

        if record.management_state == ManagementState.OBSERVE_ONLY:
            return PlanRefused(
                "object_observe_only",
                {
                    "management_state": record.management_state,
                    "reason": record.management_reason,
                },
            )

        observation = record.observation
        if observation is None:
            return PlanRefused("no_observation", {})
        freshness, age = derive_freshness(
            observation.observed_at_dt, context.freshness_ttl_s, now=_parse(context.now)
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
            observation.capability != CAPABILITY_DOCKER_CONTAINERS_OBSERVE
            or observation.provider != PROVIDER_DOCKER
        ):
            # The same rule reconciliation and every other planner applies: the same field
            # name from a different source is not the same fact.
            return PlanRefused(
                "observation_source_incompatible",
                {
                    "expected_capability": CAPABILITY_DOCKER_CONTAINERS_OBSERVE,
                    "expected_provider": PROVIDER_DOCKER,
                    "observation_capability": observation.capability,
                    "observation_provider": observation.provider,
                },
            )

        container_id = observation.facts.get(CORRELATION_CONTAINER_ID)
        if not isinstance(container_id, str) or not container_id:
            return PlanRefused(
                "container_id_unreadable", {"observation_id": observation.observation_id}
            )

        state = observation.facts.get("state")
        if not isinstance(state, str) or not state:
            return PlanRefused(
                "container_state_unreadable",
                {
                    "observation_id": observation.observation_id,
                    "gaps": observation.gaps,
                },
            )
        if state in _TRANSIENT:
            return PlanRefused("container_state_transient", {"state": state})
        if action is LifecycleAction.START and state == _PAUSED:
            return PlanRefused("container_paused", {"state": state})
        no_op_states, no_op_code = _NO_OP[action]
        if state in no_op_states:
            return PlanRefused(
                no_op_code,
                {"state": state, "observation_id": observation.observation_id},
            )

        change = PlannedAction(
            action=str(action),
            observed_state=state,
            expected_state=EXPECTED_STATE[action],
        )

        block_reason, _owner = ownership_gate(definition, context.provenance)
        gaps = context.provenance.gaps
        protection = assess_protection(
            definition, management_path=context.management_path, resource_id=record.object_id
        )
        risk = assess_risk(
            definition,
            ownership_block_reason=block_reason,
            ownership_gaps=gaps,
            protection=protection,
        )
        # A container lifecycle action has no unattended inverse — the opposite verb is a
        # second change nobody asked for, not a restoration — so no guard is offered, and
        # the generic assessment says which prerequisite is missing rather than this module
        # deciding for itself what "no guard" means.
        guard = assess_guard(
            definition, protection=protection, guard_capability_declared=False
        )
        execution = assess_execution(
            definition,
            availability=ExecutionAvailability.AVAILABLE,
            provider=EXECUTION_PROVIDER,
            capability_declared=definition.required_capability in context.evidence_sources,
            ownership_block_reason=block_reason,
            ownership_gaps=gaps,
            protection=protection,
            guard=guard,
        )
        confirmation = effective_confirmation(
            definition, risk=risk, protection=protection, execution_available=True
        )

        return RunPlan(
            operation=operation,
            host_id=record.host_id,
            object_id=record.object_id,
            change=change,
            evidence=PlanEvidence(
                observation_id=observation.observation_id,
                sweep_id=observation.sweep_id,
                observed_at=observation.observed_at,
                # No intent, no version, no drift. An action reconciles nothing, so there
                # is nothing for it to answer and no contract to compare it under — and the
                # store's own CHECK refuses a row that claims otherwise.
            ),
            ownership=OwnershipAssessment(
                state=str(context.provenance.state),
                reason=context.provenance.reason,
                claims=tuple(
                    PublishedClaim(
                        relation=str(claim.relation),
                        provider=claim.owner.provider,
                        instance=claim.owner.instance,
                        label=claim.owner.label,
                        confidence=str(claim.confidence),
                        external=claim.owner.external,
                    )
                    for claim in context.provenance.claims
                ),
                gaps=tuple(gaps),
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
                    "this operation has no inverse LocalPlane may perform. Issuing the "
                    "opposite verb because a verification failed would be a second change "
                    "nobody asked for, against a container whose state is already not what "
                    "was expected, and it can make the situation worse. Nothing is armed, "
                    "no checkpoint is written, and a change that cannot be proven ends in "
                    "recovery saying what was asked for and what was observed instead."
                ),
            ),
            verification=VerificationPlan(
                executed=False,
                capability=CAPABILITY_DOCKER_CONTAINERS_OBSERVE,
                provider=PROVIDER_DOCKER,
                condition=definition.verification_condition,
            ),
        )

    return plan


class ContainerLifecycleExecutor:
    """The execution half of the three lifecycle operations.

    One class, parameterised by the verb it carries out, because the three differ only in
    the verb and in what a verification must see. It decides nothing about policy: whether
    this container may be acted on, whether an operator confirmed it and whether the plan
    was still current were settled before any method here is called.
    """

    def __init__(
        self,
        action: LifecycleAction,
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
        """The material a lifecycle request needs, read from this object's newest reading.

        The container id is the identity — the same value LocalPlane's own object id is
        derived from, so there is no second identity system and no name being trusted. The
        lifecycle state and the instant Docker last started the container travel with it,
        because they are what a verification of a *restart* is judged against: a container
        that is running now and was running when the change was dispatched has not been
        shown to have restarted, and only the reading taken at dispatch can settle that.

        Refusing here happens before the boundary and leaves no Change.
        """
        observation = record.observation
        if observation is None:
            raise ExecutionRefused("no_observation", {"object_id": record.object_id})
        container_id = observation.facts.get(CORRELATION_CONTAINER_ID)
        if not isinstance(container_id, str) or not container_id:
            raise ExecutionRefused(
                "execution_identity_unreadable",
                {
                    "object_id": record.object_id,
                    "observation_id": observation.observation_id,
                    "missing": CORRELATION_CONTAINER_ID,
                },
            )
        return {
            CORRELATION_CONTAINER_ID: container_id,
            CORRELATION_STATE: observation.facts.get("state"),
            CORRELATION_STARTED_AT: observation.facts.get("started_at"),
            "observation_id": observation.observation_id,
        }

    # -------------------------------------------------------------------------- mutate

    def mutate(self, request: MutationRequest) -> MutationReport:
        """Ask the daemon to carry out this operation's verb. Never raises.

        Every way this can go wrong is one of the three outcomes, because a caller that had
        to interpret an exception would be deciding what it meant about the host.

        The verb sent is **this executor's own**, not the one on the request: the request
        carries it because the Change recorded it, and the two are checked against each
        other so that a Change built for one operation cannot be dispatched by another's
        executor. That check can only refuse.
        """
        container_id = str(request.correlation[CORRELATION_CONTAINER_ID])
        if request.action != str(self._action):
            return MutationReport(
                outcome=MutationOutcome.NOT_WRITTEN,
                reason="action_mismatch",
                attempt_id=request.attempt_id,
                detail={"recorded": request.action, "executor": str(self._action)},
            )
        try:
            answer = self._client.container_lifecycle(
                attempt_id=request.attempt_id,
                container_id=container_id,
                action=str(self._action),
            )
        except AgentError as exc:
            # The branch that carries the whole distinction: the request either had not left
            # this process, in which case nothing can have happened, or it had, in which case
            # the daemon may already have acted. The client records which and nothing here
            # overrides it.
            return MutationReport(
                outcome=(
                    MutationOutcome.WRITE_UNKNOWN
                    if exc.dispatched
                    else MutationOutcome.NOT_WRITTEN
                ),
                reason=exc.code,
                attempt_id=request.attempt_id,
                detail={
                    "agent_error": exc.as_dict(),
                    "dispatched": exc.dispatched,
                    "container_id": container_id,
                },
            )

        mutation = answer.get("mutation") or {}
        try:
            outcome = MutationOutcome(mutation.get("outcome"))
        except ValueError:
            # The agent answered with something this build cannot read. The request was
            # certainly dispatched, so the honest reading of an unintelligible answer is
            # that the change may have happened.
            return MutationReport(
                outcome=MutationOutcome.WRITE_UNKNOWN,
                reason="unreadable_mutation_outcome",
                attempt_id=request.attempt_id,
                detail={"mutation": mutation, "container_id": container_id},
            )
        return MutationReport(
            outcome=outcome,
            reason=str(mutation.get("reason") or "unstated"),
            attempt_id=request.attempt_id,
            provider=mutation.get("provider"),
            provider_version=mutation.get("provider_version"),
            method=mutation.get("method"),
            detail={
                "container_id": container_id,
                "action": mutation.get("action"),
                "http_status": mutation.get("http_status"),
                "daemon_detail": mutation.get("detail") or {},
                "agent_instance_id": answer.get("agent_instance_id"),
            },
        )

    # --------------------------------------------------------------------------- prove

    # ---------------------------------------------------------------- connection guard

    def arm_guard(self, request: GuardRequest) -> GuardArmed:
        """There is no connection guard for a lifecycle action, and there could not be.

        A guard is a *reversal* held on the host with a deadline. A container action has no
        reversal: the inverse of ``start`` is not ``stop``, it is a second change nobody
        asked for against a resource whose state is already not what was expected. A guard
        that fired into one of those would make a bad situation worse without anybody
        watching, which is the opposite of what the mechanism is for.

        The refusal is here rather than assumed absent, so that a build which one day
        offered a guarded container change would have to write the reversal to remove it.
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

    def prove(self, facts: Mapping[str, Any], expectation: Expectation) -> ProofOfState:
        """Whether this reading proves the verb took effect.

        **For ``start`` and ``stop`` the state is the whole proof.** The daemon acknowledged
        a request; an independent observation showing the container in the state the plan
        promised is what turns that into a result.

        **For ``restart`` the state is not enough, and this is where Docker's own evidence
        does the work.** A restart leaves a container running, and so does doing nothing at
        all to a container that was already running — the two are indistinguishable from the
        state alone, which is exactly the kind of "verification" that verifies nothing. So
        the instant Docker records the container as having last started is compared with the
        one observed when the change was dispatched, and a restart is proven only if it
        moved. That is the daemon's own authoritative record of the event, not an inference.
        """
        state = facts.get("state")
        if not isinstance(state, str) or not state:
            return ProofOfState(
                VerificationOutcome.VALUE_UNREADABLE, reason="container_state_unreadable"
            )
        if state != expectation.state:
            return ProofOfState(
                VerificationOutcome.MISMATCH, state=state, reason="observed_state_differs"
            )
        # The daemon's two answers must agree before either is treated as a result.
        if is_running(facts) is None:
            return ProofOfState(
                VerificationOutcome.VALUE_UNREADABLE,
                state=state,
                reason="daemon_state_and_running_flag_disagree",
            )
        if self._action is not LifecycleAction.RESTART:
            return ProofOfState(VerificationOutcome.VERIFIED, state=state)

        before = expectation.correlation.get(CORRELATION_STARTED_AT)
        after = facts.get("started_at")
        if not isinstance(after, str) or not after:
            return ProofOfState(
                VerificationOutcome.VALUE_UNREADABLE,
                state=state,
                reason="started_at_unreadable",
            )
        if not _started_again(before, after):
            return ProofOfState(
                VerificationOutcome.MISMATCH,
                state=state,
                reason="container_did_not_start_again",
            )
        return ProofOfState(VerificationOutcome.VERIFIED, state=state)

    # ------------------------------------------------------------------------- observe

    def observe(self, record: ObjectRecord) -> ObservationAttempt:
        """Re-read this container through the ordinary observation path.

        The *ordinary* path, and that is the point: the same sweep, the same provider, the
        same normalisation and the same store every other judgement in LocalPlane rests on.
        Verification that trusted a private read would be checking the change against the
        code that made it.
        """
        try:
            result = self._coordinator.refresh_containers()
        except AgentError as exc:
            return ObservationAttempt(
                record=None, failure="observation_unavailable", detail=exc.as_dict()
            )
        fresh = self._objects.get(record.object_id)
        if fresh is None or fresh.observation is None:
            return ObservationAttempt(
                record=None,
                failure="target_absent",
                detail={"object_id": record.object_id},
            )
        if fresh.observation.sweep_id != result.sweep_id:
            # The sweep ran and did not see this container — it was removed while the change
            # was in flight. Handing back its previous observation would let a verification
            # "prove" a state nobody has looked at since, which is precisely how a container
            # that vanished mid-change would be reported as fine.
            return ObservationAttempt(
                record=None,
                failure="target_absent",
                detail={
                    "object_id": record.object_id,
                    "sweep_id": result.sweep_id,
                    "newest_observation_sweep_id": fresh.observation.sweep_id,
                },
            )
        return ObservationAttempt(record=fresh)


#: The three operations, each a definition and a planner over the same closure.
LIFECYCLE_OPERATIONS: dict[OperationType, LifecycleAction] = {
    OperationType.DOCKER_CONTAINER_START: LifecycleAction.START,
    OperationType.DOCKER_CONTAINER_STOP: LifecycleAction.STOP,
    OperationType.DOCKER_CONTAINER_RESTART: LifecycleAction.RESTART,
}

DEFINITIONS: dict[OperationType, OperationDefinition] = {
    operation: _definition(action, operation)
    for operation, action in LIFECYCLE_OPERATIONS.items()
}

OPERATIONS: dict[OperationType, TypedOperation] = {
    operation: TypedOperation(
        definition=DEFINITIONS[operation],
        planner=plan_container_lifecycle(action, operation, DEFINITIONS[operation]),
    )
    for operation, action in LIFECYCLE_OPERATIONS.items()
}


def build_executors(
    client: AgentClient, coordinator: ObservationCoordinator, objects: ObjectRepository
) -> dict[OperationType, ContainerLifecycleExecutor]:
    """Bind each lifecycle operation to the executor that carries it out."""
    return {
        operation: ContainerLifecycleExecutor(action, client, coordinator, objects)
        for operation, action in LIFECYCLE_OPERATIONS.items()
    }


def _started_again(before: Any, after: str) -> bool:
    """Whether Docker's record of the last start moved. The proof a restart happened.

    Compared as instants where both parse, so that "later" means later rather than
    lexicographically larger, and as text where they do not — two different values from the
    same daemon in the same format are two different starts either way. A container that was
    not running before has no previous instant at all, and any instant then proves it
    started.
    """
    if not isinstance(before, str) or not before:
        return True
    if before == after:
        return False
    was, now = parse_docker_instant(before), parse_docker_instant(after)
    if was is None or now is None:
        return True
    return now > was


def _parse(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None
