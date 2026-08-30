"""The Change Engine core: a Run, the plan it publishes, and whether that plan still holds.

**A Run is not a Change.** This is the distinction the whole module exists to keep, and it
is LocalPlane's defining distinction:

* A **Run** is an operator asking "what would it take to reconcile this, and would it be
  safe?". It is durable, it is auditable, and it says nothing about the host having moved,
  because nothing has. Creating one, reading its preview and cancelling it are all
  pre-write acts.
* A **Change** is the record that LocalPlane wrote to the host. It comes into existence at
  the first possible write boundary and not before — LocalPlane's first invariant is that a
  cancelled run carries no change record, and its second is that anything past ``arming`` must carry
  one.

That boundary is crossed now, in exactly one place: applying a Run. Publishing a plan does
not create a Change, confirming one does not, and arming one does not — none of those can
have moved anything about the host, and a Change that could exist before the host was
reachable would mean nothing. The record itself, its obligations and its three-valued
account of what became of the host live in :mod:`localplane.backend.domain.changes`; what
stays here is the Run, which is still the plan and not the act.

Management transitions, intent revisions and Runs are three different things and stay
three different things. Adopt and release move LocalPlane's stance; a revision moves which
version of its own intent an object points at; a Run plans an operation against whatever
those two have settled on. None of the three has ever written to a host.

**The state vocabulary is LocalPlane's, and only the truthful part of it is reachable.**
:class:`RunState` carries all fourteen names so that nothing downstream has to invent one
later. :data:`REACHABLE_RUN_STATES` is the subset this build can actually produce — thirteen
of them now — and the schema's CHECK enumerates exactly that subset, plus the pairings
between a state and what it may claim about the host: ``failed`` requires that nothing was
written, ``succeeded`` that something was. A store that could hold either without the other
could hold a claim no code path would be needed to make into a lie.

**The plan is content-addressed.** :func:`plan_digest` reduces a :class:`RunPlan` to a
canonical document and hashes it, so that "the operator confirmed this plan" can one day be
checked against "this is the plan about to run" rather than assumed. The digest deliberately
excludes anything that moves without the plan meaning anything different — timestamps,
identifiers of readings, presentation — and :func:`assess_validity` is the other half:
re-plan from current truth, and if the digest that comes out is not the digest that was
published, the published one is stale and the reasons say which of its bindings moved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Any, Mapping

from localplane.backend.domain.guard import GuardPlan
from localplane.backend.domain.intent import ValueType
from localplane.backend.domain.protection import ProtectionAssessment
from localplane.backend.domain.self_impact import BackendSelfImpactAssessment
from localplane.backend.domain.systemd_lifecycle import (
    AuthorizationAssessment,
    SystemdLifecycleContext,
)

#: Bumped when the canonical document below changes shape. Stored with every preview, so a
#: digest produced by a build that canonicalised differently is recognisable rather than
#: silently compared against one that did not — and so that an older preview's digest stays
#: *recomputable*, from its own row, under the rules it was made with.
#:
#: Version 2 added protection: a plan whose target is proven to carry the operator's
#: management path and one whose target is proven not to are different plans to review, and
#: two documents an operator must decide differently about may not share an identity.
#:
#: Version 3 adds the connection guard. A plan whose only write path is a guarded one is a
#: different decision from the same plan when it was simply blocked, and from the same plan
#: on a host that cannot hold a guard — three documents, three identities. It also carries
#: ``execution.eligibility`` gaining a third value, which the section already renders.
#:
#: Version 4 adds systemd lifecycle authorization, multi-reason protection and bounded
#: lifecycle context. These facts change what an operator would be confirming, so they are
#: content-addressed while versions 1–3 remain recomputable under their original forms.
#:
#: Version 5 binds the semantic ``docker-direct-unix-v1`` runtime-owner correlation.  It
#: deliberately excludes PIDs, pidfds, socket handles and raw cgroup paths while making a
#: different attestation, container generation, Engine or systemd invocation a new plan.
#:
#: Version 6 binds the backend self-impact assessment and the typed containment facts it is
#: derived from — which unit holds the connection this request arrived over, which holds the
#: agent, and what kind of unit each is. A plan that may interrupt LocalPlane itself and one
#: that provably does not are different decisions to review, and a plan whose eligibility for
#: a future self-impact authority differs is a different document under this form. Versions
#: 1–5 stay recomputable, and a preview published under one of them carries no self-impact
#: derivation at all.
PLAN_DIGEST_VERSION = 6

#: The first canonical form. Kept because previews published under it are still readable,
#: still valid history, and still have to verify against the digest they were published
#: with. Nothing produces it any more; :func:`canonical_plan` renders it on request.
LEGACY_DIGEST_VERSION = 1


class RunState(StrEnum):
    """The run lifecycle, in the order it is allowed to happen.

    ``draft → preview → awaiting_confirmation → arming → applying → verifying →
    guarded → succeeded``, with ``rolling_back → rollback_verifying → rolled_back`` and
    ``recovery_required`` after a write, and ``cancelled`` / ``failed`` only before one.

    The whole vocabulary is part of the product. What this build may *produce* is
    :data:`REACHABLE_RUN_STATES`.
    """

    DRAFT = "draft"
    PREVIEW = "preview"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    ARMING = "arming"
    APPLYING = "applying"
    VERIFYING = "verifying"
    GUARDED = "guarded"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"
    ROLLBACK_VERIFYING = "rollback_verifying"
    ROLLED_BACK = "rolled_back"
    RECOVERY_REQUIRED = "recovery_required"
    CANCELLED = "cancelled"


REACHABLE_RUN_STATES: tuple[RunState, ...] = (
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
"""The states this build can produce, and the only thirteen the schema will accept.

``preview`` is where a Run starts, because creating one *is* planning one. From there an
apply passes through ``awaiting_confirmation`` — where it stays if there is no confirmation
to consume — then ``arming`` while the checkpoint and, for a guarded change, the guard are
established, then ``applying`` at the write boundary, and then either ``verifying`` and
``succeeded``, or ``failed`` where nothing was written, or the three rollback states and
``rolled_back`` / ``recovery_required``.

``guarded`` sits between ``verifying`` and ``succeeded`` and it is reachable now. It means
the change was written and verified against the object carrying the operator's own
connection, a host-side guard is holding a reversal, and the one thing still outstanding is
whether the operator can still reach LocalPlane at all. It is a *waiting* state whose
deadline belongs to a component the backend does not contain, which is why nothing here
resolves it on a timer.

**One of the fourteen states remains unreachable.** ``draft`` is the composer's "I am still reading"
state and has nothing to mean in an API where creating a Run publishes its plan.
"""

TERMINAL_RUN_STATES: tuple[RunState, ...] = (
    RunState.SUCCEEDED,
    RunState.FAILED,
    RunState.ROLLED_BACK,
    RunState.RECOVERY_REQUIRED,
    RunState.CANCELLED,
)
"""Where a Run stops. ``recovery_required`` is among them and is not a failure.

It is the truthful ending for a Run that may have changed a host and cannot prove what it
left behind, and it is terminal in the sense that this build will not move it further on its
own — the object's write lock stays held until somebody looks."""


class OperationType(StrEnum):
    """The closed vocabulary of typed operations. Membership is the whole allowlist.

    An operation is a *name with a meaning LocalPlane implements*, never a command, an
    argv, a provider method or a field path supplied by a caller. There is deliberately no
    escape hatch: no ``custom``, no ``other``, no free-text variant. A request naming
    anything not in this enum is refused before it reaches a planner, and the API's request
    model is typed to the same set so the refusal happens at the edge as well.

    Seven members, of two kinds. The first reconciles a retained value; the six container
    and service entries are *actions* — see :class:`PlannedAction` for why that distinction
    is carried through the whole model rather than dressed up as a field change.
    """

    NETWORK_INTERFACE_RECONCILE_MTU = "network.interface.reconcile_mtu"
    DOCKER_CONTAINER_START = "docker.container.start"
    DOCKER_CONTAINER_STOP = "docker.container.stop"
    DOCKER_CONTAINER_RESTART = "docker.container.restart"
    SYSTEMD_SERVICE_START = "systemd.service.start"
    SYSTEMD_SERVICE_STOP = "systemd.service.stop"
    SYSTEMD_SERVICE_RESTART = "systemd.service.restart"


class RiskTier(StrEnum):
    """The three risk tiers. LocalPlane defines no fourth."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


RISK_ORDER: dict[RiskTier, int] = {RiskTier.LOW: 0, RiskTier.MEDIUM: 1, RiskTier.HIGH: 2}


class ConfirmationMethod(StrEnum):
    """How a confirmation would be satisfied, if there were anything to satisfy it with."""

    NONE = "none"
    ACKNOWLEDGE = "acknowledge"
    TYPED = "typed"
    POLICY = "policy"
    """An automatic run has nobody to type; the standing policy that started it authorises
    it, and the record says which. There is no automation in this build, so nothing
    produces this — it is here because leaving it out would invite a later build to
    reinvent it as an exemption rather than as a different way of satisfying the same
    requirement."""


class ExecutionAvailability(StrEnum):
    """Whether LocalPlane could execute this operation at all."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    """Implemented, but not usable here — no provider, no capability, no privilege."""

    NOT_IMPLEMENTED = "not_implemented"
    """LocalPlane has no code that would execute this. No planner in this build publishes
    it — all seven operations declare an executor — so it survives only on previews stored
    by an earlier build, and as the value this build would publish again for an operation it
    could no longer execute."""


class ExecutionEligibility(StrEnum):
    """Whether this particular plan would be allowed to execute, and by which path."""

    ELIGIBLE = "eligible"
    """The ordinary apply path is open: nothing about this plan is holding it back."""

    GUARDED = "guarded"
    """**Ordinary execution is blocked and the guarded path is the only one that exists.**

    Not a softer ``blocked`` and not a stronger ``eligible``. It is a third answer because
    there are three situations, and collapsing it into either of the others would state a
    falsehood: into ``eligible`` it would say an ordinary apply may proceed against the
    object carrying the operator's own connection, and into ``blocked`` it would say there
    is no way to do this work at all.

    A plan reaches it only when every guard prerequisite the build can evaluate before an
    attempt is already proven — including that the target *is* the management path, which
    is the hazard the guard exists for. A target whose relation is unknown never reaches
    it."""

    SELF_IMPACT_OVERRIDE_REQUIRED = "self_impact_override_required"
    """**Ordinary execution is blocked, and one narrowly typed operator authority is the
    only path that exists.** A fourth answer for the same reason ``guarded`` is a third:
    collapsing it into ``blocked`` would say there is no way to do this work at all, and
    into ``eligible`` that an ordinary apply may proceed against infrastructure hosting
    LocalPlane itself.

    It says nothing has been authorised and nothing has been softened. Every blocker the
    plan published is still published, the protection verdict is still exactly what the
    evidence made it, and the plan cannot be applied until an operator grants the override
    *in addition to* the confirmation the plan already required.

    A plan reaches it only where the self-impact derivation says this exact hazard is one
    such an authority could cover — a backend proven under ``docker-direct-unix-v1`` to be
    downstream of a unit in this closure, with nothing else on the management path in it,
    the agent proven to be a host service outside it, and no gap anywhere. A `possible`
    impact, an unresolved one, or any blocker unrelated to that hazard leaves the plan
    ``blocked``."""

    BLOCKED = "blocked"


class RecoveryMode(StrEnum):
    """The three recovery modes: what *would* put the object back, not what did."""

    AUTO = "auto"
    """A checkpoint is restored and read back. Available in principle for a scalar field
    whose previous value was verified."""

    OPERATOR = "operator"
    """The operation shows what exists and a person chooses."""

    NONE = "none"
    """There is no rollback for this operation, and the record says so."""


class PlanValidityState(StrEnum):
    CURRENT = "current"
    STALE = "stale"


# --------------------------------------------------------------------------- plan content


@dataclass(frozen=True)
class PlannedFieldChange:
    """What would change: one controlled field, typed, with both ends stated.

    Scoped to a single field because both this build's operations-to-be are. An operation
    that moves several fields at once needs its own representation and its own migration —
    the same decision ``findings`` made about evidence in 0002, for the same reason.
    """

    field: str
    value_type: ValueType
    current: bool | int
    desired: bool | int

    @property
    def expected_after(self) -> bool | int:
        """What the host would read back if this were applied and verified.

        Equal to ``desired`` for a field the kernel stores as it is given. It is a distinct
        question — an operation whose provider normalises, rounds or clamps a value would
        answer it differently — and it is answered here rather than left to a caller to
        assume the two are the same.
        """
        return self.desired


@dataclass(frozen=True)
class PlannedAction:
    """WHAT would be done: one declared verb, and the state it must produce.

    The second kind of change, and it exists because the first one cannot describe it
    honestly. A field change is a disagreement between a retained value and a runtime one,
    and reconciling it means writing the retained value; ``restart`` is neither. There is no
    retained value for a container, nothing has drifted, and the state a restart is expected
    to leave behind — ``running`` — is frequently the state it was observed in. Forcing that
    into ``current`` and ``desired`` would mean either a plan whose two ends are equal,
    which the store rightly refuses, or inventing a difference that is not there.

    So an action states three things: the verb, the lifecycle state the resource was
    observed in, and the state that must hold afterwards for the action to have worked.
    ``observed_state`` is not a "current value" to be written over — it is what the operator
    was looking at when they decided, and it is what a verification of a restart is judged
    against, because a container that is running now and was running then has not been shown
    to have restarted.

    **The evidence a verification needs travels separately.** ``expected_state`` is what the
    plan promises; proving a restart also needs the instant the container was last started,
    which is a fact about the moment of dispatch rather than about the plan, and it is
    carried on the Change rather than hashed into the plan's identity.
    """

    action: str
    observed_state: str
    expected_state: str

    @property
    def expected_after(self) -> str:
        """What the resource must be observed in for this to have worked.

        The same question :attr:`PlannedFieldChange.expected_after` answers, and answered
        separately for the same reason: what an operation *asks for* and what a verification
        must *see* are different questions, and an operation whose provider settles somewhere
        other than where it was pointed would answer them differently.
        """
        return self.expected_state


#: What a plan says would happen. Two shapes, discriminated where they are stored and where
#: they are rendered, and never merged: a reader that had to inspect which fields were null
#: to find out what kind of change it was looking at is a reader that will get it wrong.
PlannedChange = PlannedFieldChange | PlannedAction


@dataclass(frozen=True)
class PublishedClaim:
    """One ownership claim, as it stood when the plan was published."""

    relation: str
    provider: str
    instance: str | None
    label: str | None
    confidence: str
    external: bool


@dataclass(frozen=True)
class OwnershipAssessment:
    """What was known about who owns the target, at planning time.

    Published rather than re-derived, because a plan is a decision made against the
    evidence that existed when it was made. Re-deriving it on every read would silently
    re-date the judgement, and a preview whose ownership section improved overnight would
    be a different plan wearing the same identity.
    """

    state: str
    reason: str
    claims: tuple[PublishedClaim, ...]
    gaps: tuple[str, ...]
    readings: Mapping[str, str | None]
    """The provider reading each consulted source was assessed from, by provider name.

    Audit only: it is what makes "why did LocalPlane think Docker owns this" answerable
    afterwards. Not part of the digest — see :func:`canonical_plan`.
    """


@dataclass(frozen=True)
class RiskFactor:
    """One piece of evidence that raised the tier, or that failed to lower it."""

    code: str
    floor: RiskTier
    detail: str


@dataclass(frozen=True)
class RiskAssessment:
    tier: RiskTier
    factors: tuple[RiskFactor, ...]


@dataclass(frozen=True)
class ConfirmationRequirement:
    """What confirming this would take — never a confirmation, and never a token.

    ``token_issued`` is ``False`` on every path. A blocked plan must not be able to hand
    out something that could later be presented as authority to write; the rule is that
    blocked work issues no confirmation token at all, and this build's store CHECK
    enforces the field at ``False`` whatever the plan says. Describing the requirement is
    not issuing one.
    """

    required: bool
    method: ConfirmationMethod
    source: str
    """``policy`` — the host policy required it. ``operation`` — the operation asked, and
    policy allowed it. An operation may make the requirement stronger and never weaker."""

    reasons: tuple[str, ...]
    policy: str
    token_issued: bool
    satisfiable: bool
    unsatisfiable_reason: str | None


@dataclass(frozen=True)
class ExecutionAssessment:
    """Whether this could run, and everything standing in the way."""

    availability: ExecutionAvailability
    eligibility: ExecutionEligibility
    blockers: tuple[str, ...]
    provider: str | None
    """The provider that would perform the write, when one is truthfully known. Every
    planner whose operation has an executor names it — the privileged helper for MTU
    reconciliation, the Docker Engine for container actions, systemd for service actions.
    It is ``None`` where no provider truthfully owns the write, and naming a plausible one
    — a command, a binary, a daemon — would be inventing an execution path."""

    required_capability: str
    capability_declared_by_agent: bool


@dataclass(frozen=True)
class RecoveryPlan:
    """What recovery would look like, and the fact that none of it is armed.

    ``armed`` is ``False`` on every path, and the schema will not store anything else.
    "A rollback exists in principle" and "recovery is established and will fire" are
    different claims, and that difference is the whole point of the write-boundary rules.
    """

    mode: RecoveryMode
    rollback_possible: bool
    armed: bool
    guarantee: str
    reason: str


@dataclass(frozen=True)
class VerificationPlan:
    """What a future verification would have to observe. Nothing observed it."""

    executed: bool
    capability: str
    provider: str
    condition: str


@dataclass(frozen=True)
class PlanEvidence:
    """Exactly which records this plan was derived from.

    The intent is ``None`` for an action, and structurally so: LocalPlane retains no desired
    state for a container, so there is no version to cite, no contract to compare under and
    no drift for the plan to answer. Four nullable fields would be a weaker statement than
    the store's own CHECK, which refuses an action carrying any of them.
    """

    observation_id: str
    sweep_id: str
    observed_at: str
    drift_finding_id: str | None = None
    intent_id: str | None = None
    intent_version: int | None = None
    intent_capability: str | None = None
    intent_provider: str | None = None


@dataclass(frozen=True)
class RunPlan:
    """The whole of what a preview publishes. Immutable once published."""

    operation: OperationType
    host_id: str
    object_id: str
    change: PlannedChange
    evidence: PlanEvidence
    ownership: OwnershipAssessment
    protection: ProtectionAssessment
    risk: RiskAssessment
    confirmation: ConfirmationRequirement
    execution: ExecutionAssessment
    guard: GuardPlan
    recovery: RecoveryPlan
    verification: VerificationPlan
    authorization: AuthorizationAssessment | None = None
    lifecycle_context: SystemdLifecycleContext | None = None
    self_impact: BackendSelfImpactAssessment | None = None
    """Whether executing this would reach the backend publishing it. Derived, never a grant.

    Beside ``protection`` rather than inside it, because it explains one hazard within a
    verdict it must not be able to move: a plan carrying this section is still exactly as
    protected as it was without one."""


@dataclass(frozen=True)
class PlanRefused:
    """Why no plan could be made. No Run is created, and nothing is written.

    A refused request must not leave a Run behind. A row saying "somebody asked and it was
    impossible" would be indistinguishable, a week later, from a plan that was made and
    abandoned — and the API already answers the question with a code and its detail.
    """

    code: str
    detail: dict[str, Any]


# ------------------------------------------------------------------------------- identity


def canonical_plan(plan: RunPlan, version: int = PLAN_DIGEST_VERSION) -> dict[str, Any]:
    """The plan reduced to what makes it *this* plan, in the shape ``version`` defines.

    Everything here answers "would executing this mean something different?". Two things
    are deliberately left out, and both omissions are load-bearing:

    * **Timestamps and reading identifiers.** ``published_at``, ``observed_at``, the
      observation id and the provider reading ids all move without the plan changing. A
      digest that included them would make every preview stale at the next sweep, including
      the sweeps that confirmed nothing had moved — and a staleness signal that fires
      constantly is one nobody reads. What the observation *said* is in: ``change.current``
      is the value it produced, and a sweep that changes it changes the digest, which is
      exactly the rule: a newer observation matters when it changes the
      current value.
    * **Values restated from elsewhere.** What recovery would put back is
      ``change.current`` and what verification would have to see is ``change.desired``.
      Both are rendered for a reader from the change itself rather than carried twice,
      because two copies of one fact can disagree and a digest over both would still
      verify while they did.

    Ordering is total: keys are sorted at serialisation and every list here is either
    already ordered by construction or sorted below, so two equal plans cannot differ by
    the order somebody happened to build a set in.

    ``version`` selects the canonical form. A preview published under version 1 has to keep
    verifying against the digest it was published with, so the older shape is rendered on
    request rather than deleted — history stays checkable under the rules it was made with,
    and nothing is silently re-canonicalised into a document its author never saw.
    """
    document = {
        "digest_version": version,
        "operation": str(plan.operation),
        "target": {"host_id": plan.host_id, "object_id": plan.object_id},
        "change": _canonical_change(plan.change),
        "intent": {
            "intent_id": plan.evidence.intent_id,
            "version": plan.evidence.intent_version,
            "capability": plan.evidence.intent_capability,
            "provider": plan.evidence.intent_provider,
        },
        "ownership": {
            "state": plan.ownership.state,
            "reason": plan.ownership.reason,
            "claims": sorted(
                (
                    {
                        "relation": c.relation,
                        "provider": c.provider,
                        "instance": c.instance,
                        "label": c.label,
                        "confidence": c.confidence,
                        "external": c.external,
                    }
                    for c in plan.ownership.claims
                ),
                key=lambda c: (c["relation"], c["provider"], c["instance"] or ""),
            ),
            "gaps": sorted(plan.ownership.gaps),
        },
        "protection": _canonical_protection(plan.protection, version),
        "risk": {
            "tier": str(plan.risk.tier),
            "factors": sorted(
                ({"code": f.code, "floor": str(f.floor)} for f in plan.risk.factors),
                key=lambda f: f["code"],
            ),
        },
        "confirmation": {
            "required": plan.confirmation.required,
            "method": str(plan.confirmation.method),
            "source": plan.confirmation.source,
            "reasons": sorted(plan.confirmation.reasons),
            "token_issued": plan.confirmation.token_issued,
        },
        "execution": {
            "availability": str(plan.execution.availability),
            "eligibility": str(plan.execution.eligibility),
            "blockers": sorted(plan.execution.blockers),
            "provider": plan.execution.provider,
            "required_capability": plan.execution.required_capability,
            "capability_declared_by_agent": plan.execution.capability_declared_by_agent,
        },
        "recovery": {
            "mode": str(plan.recovery.mode),
            "rollback_possible": plan.recovery.rollback_possible,
            "armed": plan.recovery.armed,
        },
        "verification": {
            "capability": plan.verification.capability,
            "provider": plan.verification.provider,
            "condition": plan.verification.condition,
            "executed": plan.verification.executed,
        },
    }
    if version >= 3:
        document["guard"] = _canonical_guard(plan.guard)
    if version >= 4:
        document["authorization"] = (
            None if plan.authorization is None else plan.authorization.as_dict()
        )
        document["lifecycle_context"] = _canonical_lifecycle_context(
            plan.lifecycle_context, version
        )
    if version >= 6:
        document["self_impact"] = (
            None
            if plan.self_impact is None
            else {
                **plan.self_impact.as_dict(),
                "reasons": sorted(plan.self_impact.reasons),
            }
        )
    return document


def _canonical_lifecycle_context(
    context: SystemdLifecycleContext | None,
    version: int,
) -> dict[str, Any] | None:
    """Safety-significant lifecycle proof, excluding moving record metadata."""
    if context is None:
        return None
    facts = context.target_facts
    service = facts.get("service") if isinstance(facts.get("service"), Mapping) else {}
    applicability_facts = {
        key: facts.get(key)
        for key in (
            "canonical_id",
            "unit_type",
            "load_state",
            "active_state",
            "sub_state",
            "unit_file_state",
            "can_start",
            "can_stop",
            "refuse_manual_start",
            "refuse_manual_stop",
            "need_daemon_reload",
            "transient",
            "current_job",
            "invocation_id",
        )
    }
    applicability_facts["service"] = {
        "type": service.get("type"),
        "remain_after_exit": service.get("remain_after_exit"),
    }
    canonical = {
        "status": context.status,
        "target_unit": context.target_unit,
        "action": str(context.action),
        "target_facts": applicability_facts,
        "effect_units": sorted(context.effect_units),
        "effect_edges": sorted(
            (edge.as_dict() for edge in context.effect_edges),
            key=lambda edge: (edge["source"], edge["relation"], edge["target"]),
        ),
        "effect_complete": context.effect_complete,
        "active_activation_sources": sorted(context.active_activation_sources),
        "active_upholding_sources": sorted(context.active_upholding_sources),
        "management_units": sorted(context.management_units),
        "management_complete": context.management_complete,
        "agent_unit": context.agent_unit,
        "agent_complete": context.agent_complete,
        "gaps": sorted(context.gaps),
    }
    if version >= 5:
        canonical["runtime_owner"] = (
            None
            if context.runtime_owner is None
            else {
                **context.runtime_owner.as_dict(),
                "gaps": sorted(context.runtime_owner.gaps),
            }
        )
    if version >= 6:
        # Which unit holds the backend and which holds the agent, and what kind each is.
        # Already inside `management_units` and `agent_unit`; bound separately because a
        # self-impact derivation rests on *which* member is which, and a fact a safety
        # judgement rests on has to be one the digest can detect a change in.
        canonical["connection_unit"] = context.connection_unit
        canonical["connection_unit_type"] = context.connection_unit_type
        canonical["agent_unit_type"] = context.agent_unit_type
    return canonical


def _canonical_guard(guard: GuardPlan) -> dict[str, Any]:
    """The connection-guard section. What it would be, never what it is.

    The window is in because a guard that would hold for two minutes and one that would
    hold for ten are materially different promises to review. Nothing identifying is in —
    a guard has no identity until one is armed, and arming happens long after a plan is
    published.
    """
    return {
        "availability": str(guard.availability),
        "reason": guard.reason,
        "window_s": guard.window_s,
        "prerequisites": sorted(str(p) for p in guard.prerequisites),
        "unmet": sorted(str(p) for p in guard.unmet),
    }


def _canonical_change(change: PlannedChange) -> dict[str, Any]:
    """The change section, in the shape its kind defines.

    ``kind`` is in the document rather than left implicit, so a field change and an action
    cannot collide in the hash by having the same values in differently-named slots — and so
    that a reader of a canonical document can tell what they are looking at without knowing
    the operation.

    For an action, ``observed_state`` is in for the same reason ``current`` is in for a field
    change: it is what the observation said, and a plan made against a container that has
    since stopped is a different plan. The instant it was last started is *not* in, because
    it is evidence about the moment of dispatch rather than about what the plan would do, and
    hashing it would make every restart plan stale the moment anything restarted the
    container — including the operator, by applying the plan.
    """
    if isinstance(change, PlannedAction):
        return {
            "kind": "action",
            "action": change.action,
            "observed_state": change.observed_state,
            "expected_state": change.expected_state,
        }
    return {
        "kind": "field",
        "field": change.field,
        "value_type": str(change.value_type),
        "current": change.current,
        "desired": change.desired,
    }


def _canonical_protection(protection: ProtectionAssessment, version: int) -> dict[str, Any]:
    """The protection section, in the shape its digest version defines.

    Version 1 carried the management-path relation and the evidence that was missing —
    which was all there was to carry, because nothing could establish the path.

    Version 2 adds the roll-up: the status, the reasons proven to apply and the ones left
    unresolved. Those are what an operator is actually being asked to weigh, and a plan
    whose target is proven to carry the management path must not be able to share an
    identity with one whose target is proven not to.

    What is left out of both is which *record* proved it. ``evidence_id`` and the time it
    was observed move whenever somebody refreshes, and a fresh observation that proves the
    same path has not changed the plan — it has confirmed it. The evidence is bound to the
    preview by a column with a foreign key, which is a different thing from being hashed.
    """
    section: dict[str, Any] = {
        "management_path": str(protection.management_path),
        "reason": protection.reason,
        "missing_evidence": sorted(protection.missing_evidence),
    }
    if version >= 2:
        section["status"] = str(protection.status)
        section["reasons"] = sorted(str(r) for r in protection.reasons)
        section["unresolved"] = sorted(str(r) for r in protection.unresolved)
    if version >= 4:
        section["assessed"] = sorted(
            (
                {
                    "reason": str(entry.reason),
                    "status": str(entry.status),
                    "detail": entry.detail,
                    "evidence": dict(entry.evidence),
                    "missing_evidence": sorted(entry.missing_evidence),
                }
                for entry in protection.assessed
            ),
            key=lambda entry: entry["reason"],
        )
    return section


def plan_digest(plan: RunPlan, version: int = PLAN_DIGEST_VERSION) -> str:
    """The immutable identity of a plan's content, under one canonical form.

    ``sort_keys`` makes the serialisation independent of the order the document was built
    in, and the separators keep it independent of formatting. The algorithm is named in the
    value because a future build that has to verify an old digest needs to know how it was
    made without guessing — and ``version`` is stored beside it for the same reason.
    """
    document = json.dumps(
        canonical_plan(plan, version),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return f"sha256:{sha256(document.encode('utf-8')).hexdigest()}"


# ------------------------------------------------------------------------------- validity


@dataclass(frozen=True)
class ValidityReason:
    code: str
    detail: dict[str, Any]


@dataclass(frozen=True)
class PlanValidity:
    """Whether a published plan still describes what would happen if it ran.

    Derived at read time from what LocalPlane has already recorded, and never stored:
    a stored validity would be wrong the moment a sweep landed, which is the same reason
    freshness, reconciliation and ownership are all derived too.
    """

    state: PlanValidityState
    reasons: tuple[ValidityReason, ...]

    @property
    def stale(self) -> bool:
        return self.state is PlanValidityState.STALE


#: Sections of the canonical document, and the reason code a difference in each produces.
#: Ordered by how much a reader needs to hear about it first: what the plan would do, then
#: what it rests on, then the judgements made from that.
_SECTION_REASONS: tuple[tuple[str, str], ...] = (
    ("change", "planned_values_changed"),
    ("intent", "intent_replaced"),
    ("ownership", "ownership_changed"),
    ("protection", "protection_changed"),
    ("authorization", "authorization_changed"),
    ("lifecycle_context", "lifecycle_context_changed"),
    ("self_impact", "self_impact_changed"),
    ("risk", "risk_changed"),
    ("confirmation", "confirmation_changed"),
    ("execution", "execution_changed"),
    ("guard", "guard_changed"),
    ("recovery", "recovery_changed"),
    ("verification", "verification_changed"),
    ("target", "target_changed"),
    ("operation", "operation_changed"),
)


def assess_validity(
    published: RunPlan,
    published_digest: str,
    replanned: RunPlan | PlanRefused,
    digest_version: int = PLAN_DIGEST_VERSION,
) -> PlanValidity:
    """Compare a published plan with what planning the same operation would produce now.

    Two ways a preview goes stale, and they are reported differently on purpose:

    * **The plan can no longer be made at all** — the object was released, the intent was
      revised to the value the host already has, the controlled value stopped being
      readable. The refusal code *is* the reason, because it is the most specific thing
      that can be said.
    * **The plan can still be made but comes out different** — the digest moved, and the
      sections that differ name what moved.

    Both sides are canonicalised under the digest version the preview was *published*
    with, so an older plan is compared with what it would say now under its own rules
    rather than being declared stale by a change in how documents are hashed. A newer form
    that genuinely records something the older one could not — protection, in version 2 —
    still shows up, because the facts it added move the older section too.

    Nothing is written here, and nothing may be. A reader asking whether a plan still holds
    must not be able to change the answer for the next reader.
    """
    if isinstance(replanned, PlanRefused):
        return PlanValidity(
            PlanValidityState.STALE,
            (ValidityReason(replanned.code, dict(replanned.detail)),),
        )

    current_digest = plan_digest(replanned, digest_version)
    if current_digest == published_digest:
        return PlanValidity(PlanValidityState.CURRENT, ())

    was = canonical_plan(published, digest_version)
    now = canonical_plan(replanned, digest_version)
    reasons = [
        ValidityReason(code, {"was": was[section], "now": now[section]})
        for section, code in _SECTION_REASONS
        # A section a digest version does not render is absent from both documents, which
        # is what keeps an older preview comparable under its own rules rather than being
        # declared stale by a section its author never saw.
        if section in was and was[section] != now.get(section)
    ]
    if not reasons:
        # The digest moved without any section differing: the canonicalisation itself
        # changed under this preview, which is what PLAN_DIGEST_VERSION exists to make
        # visible rather than to paper over.
        reasons = [
            ValidityReason(
                "plan_digest_changed",
                {"published": published_digest, "current": current_digest},
            )
        ]
    return PlanValidity(PlanValidityState.STALE, tuple(reasons))
