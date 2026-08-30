"""The typed operations LocalPlane knows how to plan, and the registry that binds them.

This is a concrete half of the seam. :mod:`localplane.backend.runs` owns Runs, previews,
digests and validity and knows nothing about networking; this module knows what an MTU is
and nothing about persistence. They meet at :class:`~localplane.backend.runs.TypedOperation`
— a definition plus a planner — and at :data:`OPERATIONS`, which is a dict rather than a
registry with a lifecycle, discovery or a plugin protocol.

**There are seven operations now and they arrived through the same seam.** The three
Docker container lifecycle actions live in
:mod:`localplane.backend.container_operations`, are declared and planned entirely there,
and the three systemd service lifecycle actions live in
:mod:`localplane.backend.systemd_operations`. Both are merged into :data:`OPERATIONS`
below. The systemd planners publish the complete safety and authorization assessment, and
an eligible Run they plan is executed by :class:`~localplane.backend.systemd_operations.SystemdLifecycleExecutor`,
registered through ``build_systemd_executors`` alongside the container executors.
Actions remain distinct from field reconciliation in the plan and persistence model.

**``network.interface.reconcile_mtu``** is this module's own operation. It reconciles the
runtime MTU of a managed interface *to the value its active intent already holds*, and it is
deliberately not able to do anything else:

* it takes no desired value from the caller. An operator who wants 1300 rather than the
  1500 the intent holds revises the intent first, through the endpoint that exists for
  exactly that, and then plans a Run against the authoritative version. A Run that could
  carry its own target would be a second way to change desired state, reachable without the
  concurrency check, the ownership gate and the version chain that the first one has;
* it takes no field name. The field is ``mtu`` because that is what the operation *is*;
* it takes no command, argv, provider, method or path. There is nothing in the request but
  a type and an object id, and there is nothing in the plan that resolves to an executable;
  the executor receives only the checkpointed value and applies it through the typed
  mutation path.

Everything it needs it derives from what LocalPlane already recorded: the object is
managed, its active intent controls ``mtu``, the newest compatible observation reports a
current value, and the two disagree. If any of those is not true there is no plan, and the
refusal says which one — including the case where they agree, which is a truthful no-op and
not a plan that would change nothing.

**And now it can execute.** :class:`InterfaceMtuExecutor` is the other half of the seam:
the three things the Change engine cannot do for itself, done here where the meaning of an
MTU lives. It correlates a target to the material a write needs — the kernel's interface
index, and the kernel's own name for it as a second guard — dispatches one typed mutation
through the agent to the privileged helper, and re-reads the interface through the ordinary
observation path so that verification rests on the same evidence pipeline as everything
else. It still has no command, no argv and no desired value of its own: the value comes
from the checkpoint, which took it from the intent.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from localplane.backend.agent_client import AgentClient, AgentError
from localplane.backend.container_operations import OPERATIONS as CONTAINER_OPERATIONS
from localplane.backend.container_operations import build_executors as build_container_executors
from localplane.backend.systemd_operations import OPERATIONS as SYSTEMD_OPERATIONS
from localplane.backend.systemd_operations import build_executors as build_systemd_executors
from localplane.backend.db.repositories import IntentRecord, ObjectRecord, ObjectRepository
from localplane.backend.domain.changes import (
    ExecutionRefused,
    Expectation,
    MutationOutcome,
    MutationReport,
    MutationRequest,
    ProofOfState,
    VerificationOutcome,
)
from localplane.backend.domain.intent import INTENT_SCHEMA_VERSION, ValueType, coerce
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
    PlannedFieldChange,
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
from localplane.backend.domain.guard import (
    GuardArmed,
    GuardPhase,
    GuardReport,
    GuardRequest,
    GuardRefused,
)
from localplane.helper.mtu import PROVIDER_NAME as HELPER_PROVIDER_NAME
from localplane.protocol.capabilities import (
    CAPABILITY_NETWORK_INTERFACE_MTU_GUARD,
    CAPABILITY_NETWORK_INTERFACE_SET_MTU,
)

#: The field this operation reconciles. Not a parameter: an operation that took a field
#: name would be a generic setter with a typed-looking wrapper around it, and the closed
#: vocabulary would stop being closed the moment somebody passed a name it did not expect.
MTU = "mtu"

#: The agent capability an execution needs. It is now a real declared capability — the only
#: mutating one LocalPlane has — and it is still checked rather than assumed: a preview
#: reports whether *this* agent probed it and found the privileged helper answering, which
#: is a fact about the host and not about what LocalPlane knows how to name.
CAPABILITY_SET_MTU = CAPABILITY_NETWORK_INTERFACE_SET_MTU

#: The agent capability a *guarded* execution needs, in addition to the one above. Declared
#: separately by the agent and checked separately here, because the two sides of this
#: product are versioned independently: an agent that can set an MTU and has never heard of
#: a guard simply does not list this, and a plan then publishes no guarded path rather than
#: arming something nobody is holding.
CAPABILITY_MTU_GUARD = CAPABILITY_NETWORK_INTERFACE_MTU_GUARD

#: The provider that performs the write is named because it is a real execution path.
#: Earlier schema versions CHECKed ``execution_provider`` NULL, because naming a plausible
#: command, binary or daemon would have been inventing an execution path. This one is not
#: plausible, it is the one that runs: a fixed ``RTM_NEWLINK`` carrying ``IFLA_MTU``, built
#: inside the privileged helper, with no argv anywhere on the path.
EXECUTION_PROVIDER = HELPER_PROVIDER_NAME

RECONCILE_MTU = OperationDefinition(
    operation=str(OperationType.NETWORK_INTERFACE_RECONCILE_MTU),
    summary=(
        "Write the retained intended MTU of a managed interface back to the runtime, so "
        "the host carries the value LocalPlane holds for it."
    ),
    target_kind="network.interface",
    # The MTU field on an uplink-bearing interface is rated `medium`, as is the
    # remediation that sets one `medium` too. An MTU that is wrong in the wrong direction
    # does not fail loudly: large frames are dropped somewhere on the path and everything
    # looks up. That is a medium-risk failure mode, not a low-risk one.
    base_risk=RiskTier.MEDIUM,
    # Not destructive. "Nothing to roll back" and "cannot be undone" are different things,
    # and this is neither: the previous value is a verified integer and writing it back is
    # a complete reversal.
    destructive=False,
    can_affect_management_path=True,
    # The management-path verdict resolves onto an interface object, so "is this target that
    # resource" is a question this operation's evidence actually answers.
    protection_uses_resource_relation=True,
    # And it is the one operation in this build that can be executed *behind* a connection
    # guard, because it is the one whose reversal is complete and needs nobody's decision:
    # the previous MTU is a verified integer and writing it back is the whole of putting it
    # right. An operation whose reversal needed a person could not be guarded — a deadline
    # that expires into "somebody should look at this" protects nobody.
    guarded_execution=True,
    recovery_mode=RecoveryMode.AUTO,
    rollback_possible=True,
    required_capability=CAPABILITY_SET_MTU,
    verification_condition=(
        "re-read this interface through the same capability and provider the intent is "
        "written against, and require its normalized mtu to equal the desired value"
    ),
    execution_note=(
        "The write is one fixed rtnetlink message — RTM_NEWLINK carrying IFLA_MTU — built "
        "inside a privileged helper that has exactly one mutating method and no way to "
        "send another message type. The unprivileged agent forwards a typed request to it "
        "over a socket that checks peer credentials; no command, argv or shell exists "
        "anywhere on the path, and the desired value comes from the active intent by way "
        "of the checkpoint, never from the caller."
    ),
)


def plan_interface_mtu_reconciliation(context: PlanningContext) -> RunPlan | PlanRefused:
    """Work out what reconciling this interface's MTU would involve, or refuse to guess.

    The preconditions are checked in the order they stop mattering, which is the same
    discipline adopt and revise follow and for the same reason: a caller told about
    observation freshness when the real problem is that the object was never adopted has
    been sent to fix the wrong thing.

    A refusal is not a failure of the operation, it is the operation's answer. The most
    important one is ``already_reconciled``: an MTU that already matches the intent has
    nothing to reconcile, and publishing a plan for it would put work in the record that
    would be reported as done without any being done.

    Ownership does **not** refuse. Adopt and revise refuse on it because both deepen a
    claim LocalPlane must not make; describing what a change would involve makes no claim
    and writes nothing, and a preview that says "MTU 1400 → 1500, and executing this is
    blocked because Docker configures the bridge" is more useful than a 409. The block is
    published on the plan, where it belongs.
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
    if record.management_state != ManagementState.MANAGED:
        return PlanRefused("not_managed", {"management_state": record.management_state})

    intent = context.intent
    if intent is None or intent.object_id != record.object_id:
        return PlanRefused(
            "active_intent_missing", {"active_intent_id": record.active_intent_id}
        )
    if intent.schema_version != INTENT_SCHEMA_VERSION:
        return PlanRefused(
            "intent_schema_unsupported",
            {
                "intent_id": intent.intent_id,
                "intent_schema_version": intent.schema_version,
                "supported_schema_version": INTENT_SCHEMA_VERSION,
            },
        )

    desired = _intended_mtu(intent)
    if desired is None:
        return PlanRefused(
            "field_not_controlled",
            {
                "field": MTU,
                "intent_id": intent.intent_id,
                "controlled_fields": sorted(f.field for f in intent.fields),
            },
        )

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
        observation.capability != intent.capability
        or observation.provider != intent.provider
    ):
        # The same rule reconciliation applies, for the same reason: the same field name
        # from a different source is not the same fact, and a plan that compared across
        # that boundary would say more about LocalPlane's plumbing than about the host.
        return PlanRefused(
            "observation_source_incompatible",
            {
                "intent_capability": intent.capability,
                "intent_provider": intent.provider,
                "observation_capability": observation.capability,
                "observation_provider": observation.provider,
            },
        )

    current = coerce(ValueType.INTEGER, observation.facts.get(MTU))
    if current is None:
        return PlanRefused(
            "current_value_unreadable",
            {
                "field": MTU,
                "observation_id": observation.observation_id,
                "fidelity": observation.fidelity,
                "gaps": observation.gaps,
                "observed": observation.facts.get(MTU),
            },
        )
    if current == desired:
        return PlanRefused(
            "already_reconciled",
            {
                "field": MTU,
                "value": current,
                "intent_id": intent.intent_id,
                "observation_id": observation.observation_id,
            },
        )

    change = PlannedFieldChange(
        field=MTU, value_type=ValueType.INTEGER, current=current, desired=desired
    )

    # Through the policy's gate rather than straight to the ownership block, so that every
    # operation asks the same question in the same place. This one names no provider it acts
    # through — the write is LocalPlane's own netlink message — so the answer is exactly what
    # it always was: a proven external owner blocks execution.
    block_reason, _owner = ownership_gate(RECONCILE_MTU, context.provenance)
    gaps = context.provenance.gaps
    # Whether *this* object is the one carrying the operator's connection. The verdict
    # about where that connection terminates was decided before the planner ran, from the
    # transport of the request being served; what happens here is only the relation, and
    # the case where the path itself is unresolved stays unresolved for every object rather
    # than becoming a confident negative for all but one.
    protection = assess_protection(
        RECONCILE_MTU,
        management_path=context.management_path,
        resource_id=record.object_id,
    )
    risk = assess_risk(
        RECONCILE_MTU,
        ownership_block_reason=block_reason,
        ownership_gaps=gaps,
        protection=protection,
    )
    # There is code that would execute this now, so `not_implemented` has stopped being
    # true of the build. Whether it can run *here* is a separate question and is answered
    # by `capability_declared`: the agent probes the privileged helper and reports what it
    # found, and a plan on a host with no helper is blocked on
    # `required_capability_undeclared` rather than on the build lacking the feature.
    # Whether a connection guard could be held for this target at all, decided by the same
    # generic policy every other operation would use. Two of its prerequisites are facts on
    # the table now — this operation has an unattended inverse, and the agent either
    # declares the guard capability or does not — and the rest are proved during the attempt.
    guard = assess_guard(
        RECONCILE_MTU,
        protection=protection,
        guard_capability_declared=CAPABILITY_MTU_GUARD in context.evidence_sources,
    )
    execution = assess_execution(
        RECONCILE_MTU,
        availability=ExecutionAvailability.AVAILABLE,
        provider=EXECUTION_PROVIDER,
        capability_declared=RECONCILE_MTU.required_capability in context.evidence_sources,
        ownership_block_reason=block_reason,
        ownership_gaps=gaps,
        protection=protection,
        guard=guard,
    )
    confirmation = effective_confirmation(
        RECONCILE_MTU, risk=risk, protection=protection, execution_available=True
    )

    return RunPlan(
        operation=OperationType.NETWORK_INTERFACE_RECONCILE_MTU,
        host_id=record.host_id,
        object_id=record.object_id,
        change=change,
        evidence=PlanEvidence(
            intent_id=intent.intent_id,
            intent_version=intent.version,
            intent_capability=intent.capability,
            intent_provider=intent.provider,
            observation_id=observation.observation_id,
            sweep_id=observation.sweep_id,
            observed_at=observation.observed_at,
            # The durable claim this plan answers, when LocalPlane has one open. Evidence,
            # not a precondition: a disagreement observed a moment ago is real whether or
            # not a sweep has yet turned it into a finding.
            drift_finding_id=context.open_drift.get(MTU),
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
            mode=RECONCILE_MTU.recovery_mode,
            # The material exists: the value the interface carries right now was read from
            # a current observation, and writing it back would be a complete reversal.
            rollback_possible=True,
            # And it is not armed, on any path. Nothing is holding this value, nothing
            # would restore it if a write went wrong, and no watchdog would fire if the
            # console disappeared. "A rollback exists in principle" is not "recovery is
            # established", and that difference is the whole point of the write-boundary rules.
            # A preview is published before anything is armed, and this one says so. The
            # checkpoint is written during apply, after the confirmation is consumed and
            # before the boundary is crossed; until then the only thing holding this value
            # is the observation the plan was made from.
            armed=False,
            guarantee="none",
            reason=(
                "nothing is armed while a plan is only published. Applying this writes a "
                "durable checkpoint holding the value below before any mutation becomes "
                "possible, and a checkpoint that cannot be written ends the run before the "
                "write boundary with no change record and no host write."
            ),
        ),
        verification=VerificationPlan(
            executed=False,
            capability=intent.capability,
            provider=intent.provider,
            condition=RECONCILE_MTU.verification_condition,
        ),
    )


#: The whole operation vocabulary, and the only place a planner is bound to a type.
#: :class:`~localplane.backend.runs.RunService` is given this rather than importing it, so
#: the engine never depends on what it is planning against.
#:
#: Merged rather than nested: each subsystem declares its own operations in its own module
#: and this is where they become one closed set, so "what can LocalPlane be asked to do" is
#: still one lookup.
OPERATIONS: dict[OperationType, TypedOperation] = {
    OperationType.NETWORK_INTERFACE_RECONCILE_MTU: TypedOperation(
        definition=RECONCILE_MTU, planner=plan_interface_mtu_reconciliation
    ),
    **CONTAINER_OPERATIONS,
    **SYSTEMD_OPERATIONS,
}

#: The key on the checkpoint's opaque execution material that holds the kernel's index.
CORRELATION_IFINDEX = "ifindex"
#: And the kernel's own name for the link, carried as a second identity guard.
CORRELATION_NAME = "interface_name"


class InterfaceMtuExecutor:
    """The execution half of ``network.interface.reconcile_mtu``.

    Three methods, none of which decides anything about policy. Whether this object may be
    written to, whether an operator confirmed it, whether it carries the management path and
    what the desired value is were all settled before any of these is called.

    **What it contributes is the meaning of the target.** The Change engine holds a field
    name and two values and cannot turn those into something the kernel understands; this
    can, and does it from the same observation the checkpoint was armed against rather than
    from anything a caller said.
    """

    def __init__(
        self,
        client: AgentClient,
        coordinator: ObservationCoordinator,
        objects: ObjectRepository,
    ) -> None:
        self._client = client
        self._coordinator = coordinator
        self._objects = objects

    # ----------------------------------------------------------------------- correlate

    def correlate(self, record: ObjectRecord) -> dict[str, Any]:
        """The stable material a write needs, read from this object's newest observation.

        The kernel's interface **index** is the identity, because it is what the mutating
        message addresses and what the helper can check against the kernel without a race.
        The kernel's **name** travels with it as a second guard: an index can in principle
        be recycled onto a different link between the plan and the write, and for a link
        with no permanent address the name is exactly as strong as LocalPlane's own object
        identity — so it is a real check rather than one invented to have one.

        Refusing here happens during arming, before the boundary, and leaves no Change.
        """
        observation = record.observation
        if observation is None:
            raise ExecutionRefused("no_observation", {"object_id": record.object_id})
        ifindex = coerce(ValueType.INTEGER, observation.facts.get(CORRELATION_IFINDEX))
        if ifindex is None:
            raise ExecutionRefused(
                "execution_identity_unreadable",
                {
                    "object_id": record.object_id,
                    "observation_id": observation.observation_id,
                    "missing": CORRELATION_IFINDEX,
                },
            )
        name = observation.facts.get("name") or record.display_name
        return {
            CORRELATION_IFINDEX: int(ifindex),
            CORRELATION_NAME: name,
            "observation_id": observation.observation_id,
        }

    # -------------------------------------------------------------------------- mutate

    def mutate(self, request: MutationRequest) -> MutationReport:
        """Dispatch one MTU write through the agent to the privileged helper.

        Never raises. Every way this can go wrong is one of the three outcomes, because a
        caller that had to interpret an exception would be deciding what it meant about the
        host — which is the decision that must never be made by guessing.

        The one branch that carries the whole decision is the ``AgentError`` handler: the
        request either had not left this process, in which case nothing can have been
        written, or it had, in which case the kernel may already have accepted it. The agent
        client records which, and nothing here overrides it.
        """
        correlation = request.correlation
        attempt_id = request.attempt_id
        ifindex = int(correlation[CORRELATION_IFINDEX])
        name = correlation.get(CORRELATION_NAME)
        try:
            answer = self._client.set_interface_mtu(
                attempt_id=attempt_id,
                ifindex=ifindex,
                expected_current_mtu=int(request.expected_current or 0),
                desired_mtu=int(request.desired or 0),
                expected_interface_name=name if isinstance(name, str) else None,
            )
        except AgentError as exc:
            return MutationReport(
                outcome=(
                    MutationOutcome.WRITE_UNKNOWN
                    if exc.dispatched
                    else MutationOutcome.NOT_WRITTEN
                ),
                reason=exc.code,
                attempt_id=attempt_id,
                detail={
                    "agent_error": exc.as_dict(),
                    "dispatched": exc.dispatched,
                    "ifindex": ifindex,
                },
            )

        mutation = answer.get("mutation") or {}
        try:
            outcome = MutationOutcome(mutation.get("outcome"))
        except ValueError:
            # The agent answered with something this build cannot read. The request was
            # certainly dispatched, so the honest reading of an unintelligible answer is
            # that the write may have happened.
            return MutationReport(
                outcome=MutationOutcome.WRITE_UNKNOWN,
                reason="unreadable_mutation_outcome",
                attempt_id=attempt_id,
                detail={"mutation": mutation, "ifindex": ifindex},
            )
        return MutationReport(
            outcome=outcome,
            reason=str(mutation.get("reason") or "unstated"),
            attempt_id=attempt_id,
            provider=mutation.get("provider"),
            provider_version=mutation.get("provider_version"),
            method=mutation.get("method"),
            detail={
                "ifindex": ifindex,
                "observed_name": mutation.get("observed_name"),
                "observed_mtu": mutation.get("observed_mtu"),
                "kernel_errno": mutation.get("kernel_errno"),
                "kernel_error": mutation.get("kernel_error"),
                "replayed": mutation.get("replayed"),
                "helper_detail": mutation.get("detail") or {},
                "agent_instance_id": answer.get("agent_instance_id"),
            },
        )

    # ---------------------------------------------------------------- connection guard

    def arm_guard(self, request: GuardRequest) -> GuardArmed:
        """Ask the agent on this host to hold this change's reversal until a deadline.

        **The reversal is the inverse of the write, and it is fully determined here.** Its
        precondition is the value the change is about to leave behind and its desired value
        is the one the checkpoint holds — so the guard writes the previous MTU back, and
        only over the value LocalPlane put there. A guard that fires against a link the
        change never reached, or one somebody else has moved since, is refused by the
        privileged helper's own compare-and-set before a mutating frame is built. That is
        not a check somebody remembered to add; it is the same precondition every write on
        this path already carries, pointed the other way.

        Refuses rather than proceeds. A refusal here happens before the boundary and ends
        the Run with nothing written.
        """
        correlation = request.correlation
        try:
            answer = self._client.arm_mtu_guard(
                guard_id=request.guard_id,
                attempt_id=request.attempt_id,
                ifindex=int(correlation[CORRELATION_IFINDEX]),
                guarded_mtu=int(request.guarded_value),
                restore_mtu=int(request.restore_value),
                window_s=request.window_s,
                expected_interface_name=_name_of(correlation),
            )
        except AgentError as exc:
            raise GuardRefused(exc.code, {"agent_error": exc.as_dict()}) from None
        guard = answer.get("guard") or {}
        holder = answer.get("agent_instance_id")
        if guard.get("phase") != str(GuardPhase.ARMED) or not guard.get("expires_at"):
            # The agent answered without saying it is holding one. Treated as a refusal
            # rather than optimistically: "armed" means a component has undertaken to act,
            # and an answer that does not say so is not that undertaking.
            raise GuardRefused("guard_not_armed", {"guard": guard})
        return GuardArmed(
            holder_id=str(holder),
            expires_at=str(guard.get("expires_at")),
            detail={
                "armed_at": guard.get("armed_at"),
                "window_s": guard.get("window_s"),
                "ifindex": guard.get("ifindex"),
                "restore_mtu": guard.get("restore_mtu"),
                "guarded_mtu": guard.get("guarded_mtu"),
            },
        )

    def disarm_guard(self, guard_id: str) -> GuardReport:
        """Release a guard and report what became of it. Never raises."""
        return self._guard_report(guard_id, release=True)

    def guard_status(self, guard_id: str) -> GuardReport:
        """What a guard is doing, without releasing it. Never raises."""
        return self._guard_report(guard_id, release=False)

    def _guard_report(self, guard_id: str, *, release: bool) -> GuardReport:
        """One reading of a guard's phase, and the reversal's outcome when it fired.

        Every failure becomes ``unreachable`` rather than an exception, for the same reason
        every failure of :meth:`mutate` becomes an outcome: a caller forced to interpret an
        exception would be deciding what it meant about a mechanism that may have already
        acted, and this is the one place where "I could not ask" and "there is nothing
        holding it" must not be collapsed.
        """
        try:
            answer = (
                self._client.disarm_mtu_guard(guard_id)
                if release
                else self._client.mtu_guard_status(guard_id)
            )
        except AgentError as exc:
            return GuardReport(
                phase=GuardPhase.UNREACHABLE,
                reason=exc.code,
                detail={"agent_error": exc.as_dict(), "guard_id": guard_id},
            )
        guard = answer.get("guard") or {}
        try:
            phase = GuardPhase(guard.get("phase"))
        except ValueError:
            # An answer this build cannot read is not an answer about the guard.
            return GuardReport(
                phase=GuardPhase.UNREACHABLE,
                reason="unreadable_guard_phase",
                detail={"guard": guard},
            )
        mutation: MutationReport | None = None
        if phase is GuardPhase.FIRED:
            mutation = _reversal_report(request_id=guard_id, mutation=guard.get("mutation"))
        return GuardReport(
            phase=phase,
            holder_id=answer.get("agent_instance_id"),
            fired_at=guard.get("fired_at"),
            mutation=mutation,
            detail={
                "guard_id": guard_id,
                "expires_at": guard.get("expires_at"),
                "window_s": guard.get("window_s"),
            },
        )

    # --------------------------------------------------------------------------- prove

    def prove(self, facts: Mapping[str, Any], expectation: Expectation) -> ProofOfState:
        """Whether this reading proves the MTU LocalPlane wanted.

        One typed comparison, and the two ways it can fail to be one are named separately:
        a value that could not be read at all is not a mismatch, and reporting it as one
        would send an operator to look for a competing writer that may not exist.
        """
        value = coerce(ValueType(expectation.value_type or ""), facts.get(expectation.field))
        if value is None:
            return ProofOfState(VerificationOutcome.VALUE_UNREADABLE, reason="value_unreadable")
        if value == expectation.value:
            return ProofOfState(VerificationOutcome.VERIFIED, value=value)
        return ProofOfState(
            VerificationOutcome.MISMATCH, value=value, reason="observed_value_differs"
        )

    # ------------------------------------------------------------------------- observe

    def observe(self, record: ObjectRecord) -> ObservationAttempt:
        """Re-read this interface through the ordinary observation path.

        The *ordinary* path, and that is the point: the same sweep, the same provider, the
        same normalisation, the same store and the same finding lifecycle that every other
        judgement in LocalPlane rests on. Verification that trusted a private read would be
        checking the write against the code that made it.

        One consequence is deliberate and worth stating: the sweep this takes is a real
        observation, so a value that came back to match its intent resolves the drift
        finding here, named to *this* reading. The Change did not resolve anything; the
        evidence did.
        """
        name = record.display_name
        try:
            result = self._coordinator.refresh_network([name] if name else None)
        except AgentError as exc:
            return ObservationAttempt(
                record=None, failure="observation_unavailable", detail=exc.as_dict()
            )
        fresh = self._objects.get(record.object_id)
        if fresh is None or fresh.observation is None:
            return ObservationAttempt(
                record=None,
                failure="target_absent",
                detail={"object_id": record.object_id, "observed_name": name},
            )
        if fresh.observation.sweep_id != result.sweep_id:
            # The sweep ran and did not see this object. Its newest observation is from an
            # earlier one, and handing that back would let a verification "prove" a value
            # nobody has looked at since — which is precisely how an object that vanished
            # mid-change would be reported as fine. A reading that is not from *this* read
            # is not a read-back.
            return ObservationAttempt(
                record=None,
                failure="target_absent",
                detail={
                    "object_id": record.object_id,
                    "observed_name": name,
                    "sweep_id": result.sweep_id,
                    "newest_observation_sweep_id": fresh.observation.sweep_id,
                },
            )
        return ObservationAttempt(record=fresh)


def build_executors(
    client: AgentClient, coordinator: ObservationCoordinator, objects: ObjectRepository
) -> dict[OperationType, InterfaceMtuExecutor]:
    """Bind each operation to the executor that can carry it out.

    Separate from :data:`OPERATIONS` because a planner is a pure function of recorded truth
    and an executor is not: it holds an agent client and an observation coordinator, and it
    is the only thing in the product with a path to the host that writes. Keeping the two
    dictionaries apart is what lets planning, re-planning and validity stay on a read path
    that structurally cannot reach either.
    """
    executor = InterfaceMtuExecutor(client, coordinator, objects)
    return {
        OperationType.NETWORK_INTERFACE_RECONCILE_MTU: executor,
        **build_container_executors(client, coordinator, objects),
        **build_systemd_executors(client, coordinator, objects),
    }


def _name_of(correlation: Mapping[str, Any]) -> str | None:
    name = correlation.get(CORRELATION_NAME)
    return name if isinstance(name, str) else None


def _reversal_report(*, request_id: str, mutation: Any) -> MutationReport:
    """A guard's reversal, in the same three-valued vocabulary every other write uses.

    A guard that fired and cannot say what its reversal did is ``write_unknown``: it
    dispatched, and an answer this build cannot read is not evidence that nothing happened.
    """
    if not isinstance(mutation, dict):
        return MutationReport(
            outcome=MutationOutcome.WRITE_UNKNOWN,
            reason="unreadable_guard_reversal",
            attempt_id=request_id,
            detail={"mutation": mutation},
        )
    try:
        outcome = MutationOutcome(mutation.get("outcome"))
    except ValueError:
        return MutationReport(
            outcome=MutationOutcome.WRITE_UNKNOWN,
            reason="unreadable_guard_reversal",
            attempt_id=str(mutation.get("attempt_id") or request_id),
            detail={"mutation": mutation},
        )
    return MutationReport(
        outcome=outcome,
        reason=str(mutation.get("reason") or "unstated"),
        attempt_id=str(mutation.get("attempt_id") or request_id),
        provider=mutation.get("provider"),
        provider_version=mutation.get("provider_version"),
        method=mutation.get("method"),
        detail={
            "ifindex": mutation.get("ifindex"),
            "observed_name": mutation.get("observed_name"),
            "observed_mtu": mutation.get("observed_mtu"),
            "kernel_errno": mutation.get("kernel_errno"),
            "kernel_error": mutation.get("kernel_error"),
            "helper_detail": mutation.get("detail") or {},
        },
    )


def _intended_mtu(intent: IntentRecord) -> int | None:
    """The MTU this intent holds, or ``None`` when it does not control one.

    Read from the retained intent and from nowhere else. This is the single fact that
    stops a Run from becoming a second route to changing desired state.
    """
    for field in intent.fields:
        if field.field == MTU:
            return coerce(ValueType.INTEGER, field.value)
    return None


def _parse(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None
