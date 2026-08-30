"""One evaluator for risk, confirmation and execution eligibility.

LocalPlane keeps the policy in one module rather than scattering booleans through the
operations that need them: *the safety rule belongs to the host, not to whoever wrote the
operation.* An operation may declare that it wants a confirmation. It may not declare that
it does not need one. Five medium-risk operations ask for ``confirm.required = false`` and
are confirmed anyway, and the drawer says **by
policy** — the demonstration being that the operation's opinion is not what decides.

So everything an operation gets to say about itself is in :class:`OperationDefinition`, and
everything decided *about* it is decided here, from evidence:

* **Risk** is derived, never declared alone. An operation carries a base tier; ownership
  and protection can raise it and nothing can lower it. Unknown safety evidence sets a
  floor rather than being ignored — an operation LocalPlane cannot prove is off the
  management path is not low risk merely because nobody proved it *is*.
* **Confirmation** is required from medium risk, typed for anything that can remove the
  management path, typed for anything that cannot be undone.
* **Eligibility** is separate from all of it. A plan can be perfectly describable and
  completely ineligible, and saying both is more useful than refusing to say the first.

Nothing here knows what a network interface is. It is given assessments and a definition
and it returns judgements, which is what lets the same policy cover an operation on a
service, a file or a container when those arrive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from localplane.backend.domain.guard import (
    GUARD_AVAILABLE,
    GUARD_CAPABILITY_UNDECLARED,
    GUARD_NOT_REQUIRED,
    GUARD_OPERATION_NOT_REVERSIBLE,
    GUARD_TARGET_UNPROVEN,
    GuardAvailability,
    GuardPlan,
    GuardPrerequisite,
)
from localplane.backend.domain.provenance import OwnerIdentity, Provenance, ownership_block
from localplane.backend.domain.protection import (
    ManagementPathRelation,
    ManagementPathVerdict,
    ProtectionAssessment,
    ProtectionReason,
    ProtectionStatus,
    assess_resource_protection,
)
from localplane.backend.domain.self_impact import BackendSelfImpactAssessment
from localplane.backend.domain.runs import (
    RISK_ORDER,
    ConfirmationMethod,
    ConfirmationRequirement,
    ExecutionAssessment,
    ExecutionAvailability,
    ExecutionEligibility,
    RecoveryMode,
    RiskAssessment,
    RiskFactor,
    RiskTier,
)

#: The policy, in one sentence, stored on every preview so that a plan reviewed under one
#: policy is not silently re-read under a later one. Older previews are a pending semantic
#: upgrade, not current state.
CONFIRMATION_POLICY = (
    "required for medium and high risk, for anything that can remove the management path, "
    "and for anything that cannot be undone"
)

#: Confirmation is required from this tier upwards. This constant is the
#: confirmation-risk threshold.
CONFIRM_FROM_RISK = RiskTier.MEDIUM

#: The tier a target *proven* to carry the operator's management path carries. LocalPlane
#: rates a change to the port an operator is reached over ``high`` and puts its connection
#: guard on it; nothing about this build's inability to arm that guard makes such a change safer, so
#: the tier remains high and the guard's absence is reported separately.
MANAGEMENT_PATH_TARGET_RISK = RiskTier.HIGH

#: How long a connection guard holds, in seconds, and it is a **constant**.
#:
#: Not a request field, not a query parameter, not a setting and not a per-operation value:
#: a caller who could choose the window could choose one long enough that the guard never
#: usefully fires, which is a guard in name only. The guard model splits the timing in
#: two — a budget while a change applies plus sixty seconds to keep it — and this build
#: arms once with one absolute deadline covering both phases, so the constant is the sum rather than
#: either half.
GUARD_WINDOW_S = 120

#: The tier an *unproven* management path floors a plan at. Not as high as a proven one, and
#: never low: "LocalPlane could not rule this out" is weaker than "LocalPlane proved it" and
#: is not remotely the same as "LocalPlane proved it is not".
MANAGEMENT_PATH_UNPROVEN_RISK = RiskTier.MEDIUM


@dataclass(frozen=True)
class OperationDefinition:
    """Everything an operation declares about itself. None of it is a safety decision.

    The fields are read by the policy below and by nothing else. An operation that wanted
    to be treated as safer than the policy makes it would have to change the policy, in
    this file, where the change is visible.
    """

    operation: str
    summary: str
    target_kind: str

    base_risk: RiskTier
    """The tier this operation carries before any evidence about the target. A floor, not
    a ceiling: ownership and protection raise it and nothing lowers it."""

    destructive: bool
    """True only when the effect cannot be undone. "Nothing to roll back" is *not* this —
    a restart takes nothing away and so has nothing to put back, and treating the two as
    the same makes routine work annoying enough to route around."""

    can_affect_management_path: bool
    """Whether an object of this kind could be carrying the operator's path at all. False
    would settle the question without evidence; it is not a claim about a *particular*
    object, which is what :func:`assess_protection` is for."""

    recovery_mode: RecoveryMode
    rollback_possible: bool
    required_capability: str
    """The agent capability an execution of this operation would need. Deliberately not in
    :data:`localplane.protocol.capabilities.CAPABILITIES`: that list describes what the
    agent can do, and adding a name to it because LocalPlane knows the concept is exactly
    the assumption the protocol forbids."""

    protection_uses_resource_relation: bool = False
    """Whether this operation's protection is decided by the *resource relation* — "is this
    target the one carrying the operator's path" — rather than by typed per-reason findings.

    Two safety models, and an operation says which one judges it. The relation model answers
    a question about the one resource the management-path verdict resolves onto, and it is
    meaningful only for operations whose target is that kind of thing. An operation targeting
    anything else carries its proof in per-reason assessments, deliberately leaves the
    relation unresolved, and would be refused by a gate demanding a relation its evidence was
    never about.

    Declared rather than inferred. Deriving it from the shape of an assessment — how many
    reasons it happens to carry — would move an operation between two safety models the day
    somebody added or removed a reason, without anybody writing that down. Declaring it is
    not a safety decision either: it selects which evidence answers the question, and the
    answer still comes from the evidence."""

    guarded_execution: bool = False
    """Whether this operation can be executed behind a connection guard.

    An operation may declare this only if it has a **complete inverse LocalPlane may perform
    unattended** — which is the same property ``recovery_mode = auto`` states, read for a
    different purpose, and :func:`assess_guard` refuses to offer a guard without it rather
    than trusting the declaration.

    Like every other field here it is not a safety decision. Declaring it does not make a
    guard exist: the host has to say it can hold one, the target has to be proven to be the
    management path, a checkpoint has to be on disk, and the host side has to accept the
    arming. What the declaration says is only that this *kind* of change is one a guard
    could put back at all."""

    acts_through_provider: str | None = None
    """The provider whose own interface this operation executes through, if any.

    ``None`` means LocalPlane performs the change itself, by its own mechanism, and an
    external owner of the target is therefore a *second writer* — which is the whole reason
    the ownership gate exists. Naming a provider here says the opposite: the change is made
    by asking that provider to make it, so the provider remains the single writer and its
    own claim on the target is not a conflict.

    It never widens the gate. A claim naming any other provider still blocks, and
    conflicting claims still block. See :func:`ownership_gate`, which is the only place this
    field is read."""

    confirmation_requested: bool = False
    confirmation_reason: str | None = None

    verification_condition: str = ""
    execution_note: str = ""
    extra_risk_factors: tuple[RiskFactor, ...] = field(default_factory=tuple)


# ------------------------------------------------------------------------------ ownership


def ownership_gate(
    definition: OperationDefinition, provenance: Provenance
) -> tuple[str | None, OwnerIdentity | None]:
    """Whether ownership stands in the way of executing this operation, and why.

    One line of code and one paragraph of argument, and the argument is the point. The
    ownership block exists because a change LocalPlane makes *by its own mechanism* to
    something another system is running makes LocalPlane a second writer, and two writers
    disagreeing about one value is a fault that presents as intermittent and is diagnosed
    late. That is a statement about the mechanism, not about the target.

    An operation that executes through the owner's own interface has no such problem: the
    owner performs the change, remains the only thing writing, and keeps its own record of
    what it did. Refusing that would not be caution — it would be refusing to operate
    anything that any provider manages, which is most of what an operator needs to do.

    Adoption is unaffected, and deliberately: it is a different act with a different
    argument. Adopting means retaining a desired state and reconciling towards it, which is
    a claim about the future that a provider holding the same configuration would contest.
    """
    return _split(ownership_block(provenance, acting_through=definition.acts_through_provider))


def _split(
    block: tuple[str, OwnerIdentity | None] | None,
) -> tuple[str | None, OwnerIdentity | None]:
    return block if block is not None else (None, None)


# ----------------------------------------------------------------------------- protection


def assess_protection(
    definition: OperationDefinition,
    *,
    management_path: ManagementPathVerdict,
    resource_id: str,
) -> ProtectionAssessment:
    """Whether this target is protected, evaluating every reason this build implements.

    One reason is implemented — the management path — so the roll-up has one input, and the
    shape is the point: a second reason arrives as a second assessed reason and nothing else
    in this file moves.

    The verdict about *where* the management path is arrives already decided. It is
    host-scoped, derived from the transport a request actually came in on and corroborated
    by the kernel's own answer, and it is not this module's business how. What is decided
    here is the relation: whether this particular target is that path, is demonstrably not,
    or cannot be told apart from it because the path itself is unresolved.

    What must not happen — and the reason the unresolved case is carried through rather than
    defaulted away — is a target being called ``clear`` because nobody could prove it was the
    management path. The 2026-07-22 lockout is what that reads like afterwards.
    """
    return assess_resource_protection(
        management_path,
        resource_id=resource_id,
        applicable=definition.can_affect_management_path,
    )


# ---------------------------------------------------------------------------------- guard


def assess_guard(
    definition: OperationDefinition,
    *,
    protection: ProtectionAssessment,
    guard_capability_declared: bool,
) -> GuardPlan:
    """Whether guarded execution exists for this plan, and which prerequisite is missing.

    **Guarded execution is offered for exactly one situation**: a target *proven* to carry
    the management path this request arrived on. That is the mirror image of the ordinary
    apply gate, and the two together leave no room for the third case — a relation that
    cannot be established gets neither path, which is the whole point of having three
    values for the relation rather than two.

    A guard is a mechanism against a specific hazard. Arming one for a target that is
    proven *not* to be the path would be holding a reversal against nothing, and arming one
    where the relation is unknown would be worse: LocalPlane would be dispatching a change
    it cannot justify, and then a reversal it cannot justify either.

    Two prerequisites are evaluated here and a third is not. Whether the operation has an
    unattended inverse and whether the host declares it can hold a guard are facts already
    on the table when a plan is made. Whether the host side *accepts* the arming is not, and
    it is published as a prerequisite precisely so a reader can see that the plan does not
    claim it — it is proven during the attempt, and a guard LocalPlane asked for and did not
    get ends the Run before the boundary with nothing written.
    """
    prerequisites = (
        GuardPrerequisite.OPERATION_IS_REVERSIBLE,
        GuardPrerequisite.TARGET_IS_PROVEN_MANAGEMENT_PATH,
        GuardPrerequisite.GUARD_CAPABILITY_DECLARED,
        GuardPrerequisite.RECOVERY_MATERIAL_IS_ARMED,
        GuardPrerequisite.HOST_SIDE_GUARD_ACCEPTED,
    )
    guarantee = (
        "a reversal to the value below, held by the agent on this host with a "
        f"{GUARD_WINDOW_S}s deadline, dispatched with no further request from anybody if "
        "nothing proves within that window that this console can still be reached over "
        "this object"
    )

    def refuse(reason: str, unmet: tuple[GuardPrerequisite, ...]) -> GuardPlan:
        return GuardPlan(
            availability=GuardAvailability.UNAVAILABLE,
            reason=reason,
            window_s=GUARD_WINDOW_S,
            prerequisites=prerequisites,
            unmet=unmet,
            guarantee=guarantee,
        )

    reversible = (
        definition.guarded_execution
        and definition.rollback_possible
        and definition.recovery_mode is RecoveryMode.AUTO
    )
    if not definition.can_affect_management_path or (
        protection.management_path is ManagementPathRelation.NOT_ON_MANAGEMENT_PATH
    ):
        # Nothing to guard against. Reported as unavailable with its own reason rather than
        # as a missing prerequisite: the ordinary path is open and a guard would be a
        # mechanism armed against a hazard that is not there.
        return refuse(GUARD_NOT_REQUIRED, ())
    if not reversible:
        return refuse(
            GUARD_OPERATION_NOT_REVERSIBLE, (GuardPrerequisite.OPERATION_IS_REVERSIBLE,)
        )
    if protection.management_path is not ManagementPathRelation.ON_MANAGEMENT_PATH:
        return refuse(
            GUARD_TARGET_UNPROVEN, (GuardPrerequisite.TARGET_IS_PROVEN_MANAGEMENT_PATH,)
        )
    if not guard_capability_declared:
        return refuse(
            GUARD_CAPABILITY_UNDECLARED, (GuardPrerequisite.GUARD_CAPABILITY_DECLARED,)
        )
    return GuardPlan(
        availability=GuardAvailability.AVAILABLE,
        reason=GUARD_AVAILABLE,
        window_s=GUARD_WINDOW_S,
        prerequisites=prerequisites,
        unmet=(),
        guarantee=guarantee,
    )


# ----------------------------------------------------------------------------------- risk


def assess_risk(
    definition: OperationDefinition,
    *,
    ownership_block_reason: str | None,
    ownership_gaps: Sequence[str],
    protection: ProtectionAssessment,
) -> RiskAssessment:
    """The tier this plan carries, and every piece of evidence that decided it.

    Each factor states the *floor* it sets; the tier is the highest of them. Written this
    way so that a reader can see not only that a plan is high risk but which single fact
    would have to change for it not to be — and so that a factor which merely fails to
    lower the tier is still visible, because "LocalPlane could not rule this out" is the
    kind of evidence that quietly disappears otherwise.
    """
    factors: list[RiskFactor] = [
        RiskFactor(
            "operation_base_risk",
            definition.base_risk,
            f"{definition.operation} carries {definition.base_risk} risk before any "
            "evidence about the target",
        ),
        *definition.extra_risk_factors,
    ]

    if ownership_block_reason == "conflicting_ownership_claims":
        factors.append(
            RiskFactor(
                "conflicting_ownership_claims",
                RiskTier.HIGH,
                "two systems claim the same relation to this object, so LocalPlane cannot "
                "say which of them a write would be fighting",
            )
        )
    elif ownership_block_reason is not None:
        factors.append(
            RiskFactor(
                ownership_block_reason,
                RiskTier.HIGH,
                "another system is demonstrably running this object; a write would be a "
                "second writer, not a change of owner",
            )
        )

    if ownership_gaps:
        factors.append(
            RiskFactor(
                "ownership_evidence_incomplete",
                RiskTier.MEDIUM,
                "a source that could have claimed this object left the question open: "
                + ", ".join(sorted(ownership_gaps)),
            )
        )

    if definition.can_affect_management_path:
        # The same discriminator :func:`assess_execution` uses, for the same reason: which
        # safety model an operation is judged under is a fact it declares, not a shape its
        # assessment happens to have. Risk and eligibility must never disagree about it.
        if not definition.protection_uses_resource_relation:
            # The typed model answers in full: proven, unsettled, or evaluated and clear.
            # It never falls through to the relation branches below, whose `unknown` is a
            # statement about a question this operation's evidence was never about.
            if protection.status is ProtectionStatus.PROTECTED:
                for reason in protection.reasons:
                    factors.append(
                        RiskFactor(
                            f"protected:{reason}",
                            MANAGEMENT_PATH_TARGET_RISK,
                            "the operation's effect closure intersects the protected "
                            f"{reason} unit",
                        )
                    )
            elif protection.status is ProtectionStatus.UNKNOWN:
                factors.append(
                    RiskFactor(
                        "protection_evidence_incomplete",
                        MANAGEMENT_PATH_UNPROVEN_RISK,
                        "at least one lifecycle protection reason could not be settled",
                    )
                )
        elif protection.management_path is ManagementPathRelation.ON_MANAGEMENT_PATH:
            factors.append(
                RiskFactor(
                    "target_is_management_path",
                    MANAGEMENT_PATH_TARGET_RISK,
                    "this object is proven to carry the path this console is being reached "
                    "over; a change to it can end the session that is making it",
                )
            )
        elif protection.management_path is ManagementPathRelation.UNKNOWN:
            factors.append(
                RiskFactor(
                    "management_path_unproven",
                    MANAGEMENT_PATH_UNPROVEN_RISK,
                    "LocalPlane cannot prove this object does not carry the path it is being "
                    "reached over, and an unproven negative is not a safe one",
                )
            )

    if definition.destructive:
        factors.append(
            RiskFactor(
                "irreversible_operation",
                RiskTier.HIGH,
                "this operation cannot be undone",
            )
        )

    tier = max((f.floor for f in factors), key=lambda t: RISK_ORDER[t])
    return RiskAssessment(tier=tier, factors=tuple(factors))


# --------------------------------------------------------------------------- confirmation


def effective_confirmation(
    definition: OperationDefinition,
    *,
    risk: RiskAssessment,
    protection: ProtectionAssessment,
    execution_available: bool,
) -> ConfirmationRequirement:
    """What confirming this plan would take. The one place that decides.

    An operation's own request is honoured and recorded as ``source: operation``; a
    requirement the policy adds is recorded as ``source: policy`` with the reason, so that
    "no confirmation" is a decision with an argument behind it rather than a gap.

    **Nothing is issued, on any path.** ``token_issued`` is ``False`` and the store CHECKs
    the column to zero. A confirmation in this product is a durable row naming one Run and
    one published plan, consumed once by an apply of that Run; there is no bearer value, so
    there is nothing a caller could present anywhere else. Minting one — even a
    harmless-looking one — is how a blocked plan acquires the means to progress to an apply
    review, which is the failure the historical rule exists to prevent.

    ``satisfiable`` is a different question and it now has a real answer: whether execution
    exists for this operation at all. A requirement nobody could ever satisfy and one that
    is waiting to be satisfied are different states, and conflating them was only truthful
    while the second could not occur.
    """
    reasons: list[str] = []
    required = definition.confirmation_requested
    if required and definition.confirmation_reason:
        reasons.append(definition.confirmation_reason)

    if RISK_ORDER[risk.tier] >= RISK_ORDER[CONFIRM_FROM_RISK]:
        if not required:
            reasons.append(
                f"{risk.tier} risk — policy requires confirmation from {CONFIRM_FROM_RISK}"
            )
        required = True

    typed = False
    if definition.can_affect_management_path and (
        protection.management_path is not ManagementPathRelation.NOT_ON_MANAGEMENT_PATH
    ):
        # Typed confirmation on the management path applies to both states that are not a
        # proven negative. A target proven to be the path obviously qualifies; one whose
        # relation is unresolved qualifies too, because the requirement exists to protect
        # against the case LocalPlane cannot rule out.
        if not required:
            reasons.append(
                "this object carries the path this console is reached over"
                if protection.management_path is ManagementPathRelation.ON_MANAGEMENT_PATH
                else "this change could remove the path this console is reached over"
            )
        required = True
        typed = True

    if definition.destructive:
        if not required:
            reasons.append("this cannot be undone")
        required = True
        typed = True

    if not required:
        return ConfirmationRequirement(
            required=False,
            method=ConfirmationMethod.NONE,
            source="policy",
            reasons=("low risk, no management-path impact, reversible",),
            policy=CONFIRMATION_POLICY,
            token_issued=False,
            satisfiable=execution_available,
            unsatisfiable_reason=None if execution_available else "execution_not_implemented",
        )

    return ConfirmationRequirement(
        required=True,
        method=ConfirmationMethod.TYPED if typed else ConfirmationMethod.ACKNOWLEDGE,
        source="operation" if definition.confirmation_requested else "policy",
        reasons=tuple(reasons) or ("policy",),
        policy=CONFIRMATION_POLICY,
        # Never, on any path. See the docstring: a confirmation is a row, not a token.
        token_issued=False,
        satisfiable=execution_available,
        unsatisfiable_reason="execution_not_implemented" if not execution_available else None,
    )


# ------------------------------------------------------------------------------ execution


EXECUTION_NOT_IMPLEMENTED = "execution_not_implemented"

#: The blocker a target proven to carry this request's management path publishes. It blocks
#: the ordinary apply path and always will; whether a guarded path exists beside it is a
#: separate question with its own assessment.
TARGET_IS_MANAGEMENT_PATH = "target_is_management_path"

#: The blocker a target whose relation could not be established publishes. It is a different
#: code from the one above on purpose, and it opens no path at all.
MANAGEMENT_PATH_UNPROVEN = "management_path_unproven"

#: The one protection blocker a self-impact override can be the answer to, spelled out so the
#: branch below compares against a name rather than reconstructing a string. It is the *proven*
#: management-path reason under the typed multi-reason model; an unresolved one produces
#: ``protection_unresolved:management_path`` instead and opens nothing.
SELF_IMPACT_OVERRIDE_BLOCKER = f"protected:{ProtectionReason.MANAGEMENT_PATH}"


def assess_execution(
    definition: OperationDefinition,
    *,
    availability: ExecutionAvailability,
    provider: str | None,
    capability_declared: bool,
    ownership_block_reason: str | None,
    ownership_gaps: Sequence[str],
    protection: ProtectionAssessment,
    guard: GuardPlan,
    self_impact: BackendSelfImpactAssessment | None = None,
) -> ExecutionAssessment:
    """Whether this plan would be allowed to run, and everything standing in the way.

    Every blocker is listed, not just the first. An operator who fixes the ownership
    conflict and comes back to find a protection blocker they were never told about has
    been given a plan in instalments; the whole set is cheap to compute and is the only
    version of the answer worth having.

    This is stricter than adoption, deliberately. Adopt records values the host already
    has, so an unexamined source is reported and does not refuse it. A write would be
    LocalPlane acting, and a source that might have named another owner and could not be
    read is not a clean bill of health.

    ``self_impact`` is read for one purpose and cannot widen anything: it may turn a plan
    that is blocked *solely* because it could interrupt LocalPlane's own backend into one
    with a second path that an operator can open explicitly. It removes no blocker, changes
    no protection verdict, and is ignored entirely where anything else is also in the way.
    """
    blockers: list[str] = []
    if availability is not ExecutionAvailability.AVAILABLE:
        blockers.append(EXECUTION_NOT_IMPLEMENTED)
    if not capability_declared:
        blockers.append("required_capability_undeclared")
    if ownership_block_reason is not None:
        blockers.append(ownership_block_reason)
    if ownership_gaps:
        blockers.append("ownership_evidence_incomplete")

    ordinary_only = tuple(dict.fromkeys(blockers))
    on_the_path = False
    if definition.can_affect_management_path:
        # **Which safety model this operation is judged under, stated rather than counted.**
        #
        # The management-path *relation* is a question about a network interface:
        # `ManagementPathVerdict.resource_id` is always an interface object, resolved from
        # the accepted socket's local endpoint and corroborated by the kernel's route to the
        # peer. Asking "is this target that resource" is meaningful for an interface and is
        # not a question about a systemd unit, whose assessment leaves that relation
        # `unknown` on purpose and carries its proof in typed per-reason findings instead.
        #
        # The discriminator is the operation's own declared target kind. How many reasons an
        # assessment happens to carry is a representation detail, and a reason added or
        # removed later must not move an operation between two safety models without
        # anybody writing that down. `changes._plan_now` gates on the same fact, so the
        # model chosen when a plan is published is the model it is re-derived under.
        if not definition.protection_uses_resource_relation:
            # The typed model answers in full — proven, unsettled, or evaluated and clear —
            # and never falls through to the relation branches below. Falling through would
            # publish `management_path_unproven` for a plan whose protection was settled, on
            # the strength of a relation its evidence deliberately leaves `unknown`.
            if protection.status is ProtectionStatus.PROTECTED:
                blockers.extend(f"protected:{reason}" for reason in protection.reasons)
            elif protection.status is ProtectionStatus.UNKNOWN:
                blockers.extend(
                    f"protection_unresolved:{reason}" for reason in protection.unresolved
                )
        elif protection.management_path is ManagementPathRelation.ON_MANAGEMENT_PATH:
            # Proven, and therefore a *different* blocker from an unproven one. Reporting
            # "unproven" here would be a plain falsehood, and it is the falsehood that
            # matters most: an operator who reads it as "LocalPlane has not looked yet"
            # would go and prove the very thing that is already proven against them.
            #
            # It blocks the *ordinary* path unconditionally and always will: an apply that
            # reached the target carrying the operator's own connection with nothing
            # holding a reversal is the 2026-07-22 lockout. What it no longer does is end
            # the conversation — see the guarded branch below.
            blockers.append(TARGET_IS_MANAGEMENT_PATH)
            on_the_path = True
        elif protection.management_path is ManagementPathRelation.UNKNOWN:
            blockers.append(MANAGEMENT_PATH_UNPROVEN)

    protection_only = tuple(blockers[len(ordinary_only):])
    if not blockers:
        eligibility = ExecutionEligibility.ELIGIBLE
    elif on_the_path and guard.available and not ordinary_only:
        # The one situation with a third answer. Ordinary execution is blocked because the
        # target is proven to carry this request's management path — and *only* because of
        # that: `ordinary_only` being empty is the statement that nothing else is in the
        # way, so a plan that is also ownership-blocked or missing its write capability
        # stays `blocked` and a guard does not rescue it. The blockers are published
        # unchanged, because what stands in the way of an ordinary apply has not stopped
        # being true; what has changed is that another path exists.
        eligibility = ExecutionEligibility.GUARDED
    elif (
        self_impact is not None
        and self_impact.override_eligible
        and not ordinary_only
        and protection_only == (SELF_IMPACT_OVERRIDE_BLOCKER,)
    ):
        # The fourth answer, and the same shape of argument as the guarded one above.
        #
        # `ordinary_only` empty says nothing outside protection is in the way. The
        # protection blockers being *exactly* the proven management-path one says the only
        # thing in the way is a hazard the derivation has already established to be
        # LocalPlane's own backend and nothing else — so a plan that is additionally
        # blocked by an unresolved reason, by the agent, or by anything a later build adds
        # stays `blocked`, and this branch never sees it. The derivation is asked as well as
        # the blockers, and it is the stricter of the two: `possible`, `unresolved`, an
        # agent outside a host service and any unrecognised gap all make it `False`.
        #
        # Nothing is authorised here and no blocker is removed. What changes is that a
        # second path exists, and an operator has to open it deliberately.
        eligibility = ExecutionEligibility.SELF_IMPACT_OVERRIDE_REQUIRED
    else:
        eligibility = ExecutionEligibility.BLOCKED

    return ExecutionAssessment(
        availability=availability,
        eligibility=eligibility,
        blockers=tuple(dict.fromkeys(blockers)),
        provider=provider,
        required_capability=definition.required_capability,
        capability_declared_by_agent=capability_declared,
    )
