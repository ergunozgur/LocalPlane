"""Whether a lifecycle operation could interrupt the LocalPlane backend serving it.

LocalPlane is allowed to run inside Docker, so an operation may legitimately target the
infrastructure hosting LocalPlane's own backend — ``docker.service`` above all. The product
answer to that is not "then it can never be executed". It is to say, as precisely as the
evidence allows, whether this operation reaches LocalPlane itself, what kind of outage that
would be, and whether the exact hazard is one narrowly typed operator authority could later
cover.

**Nothing here decides anything about protection.** The assessment is derived *from* the
protection roll-up and the lifecycle context and returns a fourth document beside them.
``protected`` stays ``protected``, ``unknown`` stays ``unknown``, and every blocker those
produce is published unchanged. What this adds is an explanation of one specific hazard
inside them, and a single boolean saying whether that hazard — and nothing else — is the
only thing standing between this plan and an ordinary apply.

**``override_eligible`` is not permission and grants nothing.** No authority exists in this
build: no grant, no endpoint, no execution path reads this field. It is the *derivation*,
published in the preview so that what a later authority would be issued against is a
document an operator can read now.

**Eligibility is default-deny and the envelope is deliberately tiny.** One topology
qualifies: a backend proven by ``docker-direct-unix-v1`` to run in a container whose Engine
is owned by a systemd service that this operation's effect closure contains, with the
LocalPlane agent proven to be a normal host-side ``.service`` outside that closure and
nothing else about the plan unresolved. Everything else — an incomplete correlation, an
agent contained by anything other than a service, a second management-path unit in the
closure, one unrecognised gap — comes out ``False``, because the function only ever returns
``True`` from that one branch.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from localplane.protocol.docker_runtime_owner import (
    DOCKER_DIRECT_UNIX_CONTRACT,
    DOCKER_RUNTIME_OWNER_METHOD_VERSION,
    DOCKER_RUNTIME_OWNER_PROVIDER,
    RUNTIME_OWNER_UMBRELLA_GAP,
    DockerRuntimeOwnerStatus,
)
from localplane.backend.domain.protection import (
    ProtectionAssessment,
    ProtectionReason,
    ProtectionStatus,
)
from localplane.backend.domain.systemd_lifecycle import (
    SystemdLifecycleContext,
    SystemdServiceAction,
)


SELF_IMPACT_SUBJECT_BACKEND_RUNTIME = "localplane_backend_runtime"
"""The only subject this build assesses.

The LocalPlane *agent* is deliberately not one. Agent impact is a protection reason with its
own evidence chain and it is never something an operator authority in this product covers:
an agent that goes away takes the ability to observe, verify and recover with it, and there
is no honest story in which that is a temporary inconvenience the operator accepted.
"""

#: The systemd unit type an override-eligible LocalPlane agent must be contained by. A
#: ``.scope`` or ``.slice`` is a container runtime's containment, which systemd need not
#: publish an effect edge for — so a closure that looks disjoint from it has proven nothing.
HOST_SERVICE_UNIT_TYPE = "service"


class SelfImpactStatus(StrEnum):
    """Whether this operation reaches the backend serving the request. Four answers."""

    NOT_DETECTED = "not_detected"
    """The relationship was established and this operation does not reach the backend.

    Earned from evidence, never from an absence: something has to have proven where the
    backend runs before its absence from a closure means anything."""

    PROVEN = "proven"
    """The effect closure contains the unit this backend depends on to keep running."""

    POSSIBLE = "possible"
    """The backend runs inside a container runtime whose owning unit is not proven.

    Surfaced honestly and never override-eligible in this build. Deciding which incomplete
    runtime-owner states are "safe enough" to authorise past is a question with a real
    answer and no evidence for it yet; until there is, the honest report is that LocalPlane
    cannot rule this out and will not offer a way around it."""

    UNRESOLVED = "unresolved"
    """Nothing established where this backend runs, so nothing can be said either way."""


class SelfImpactOutage(StrEnum):
    """What kind of outage the operation could cause, if it reaches LocalPlane.

    ``restart`` and ``stop`` are not the same promise and are not described as if they
    were. Neither value claims LocalPlane *will* return: nothing here has verified a second
    path to this host, and a preview that implied one would be inventing the very thing an
    operator would rely on when it mattered.
    """

    NONE = "none"

    TEMPORARY_POSSIBLE = "temporary_possible"
    """A restart may interrupt LocalPlane, which may return once the runtime recovers."""

    INDEFINITE_POSSIBLE = "indefinite_possible"
    """A stop may take LocalPlane away with nothing scheduled to bring it back."""


@dataclass(frozen=True)
class BackendSelfImpactAssessment:
    """One subject, one status, and whether this exact hazard could ever be authorised."""

    subject: str
    status: SelfImpactStatus
    outage: SelfImpactOutage
    override_eligible: bool
    detail: str
    """The typed code for the status. Never prose, and what a caller branches on."""

    owner_unit_id: str | None = None
    """The unit whose interruption would take the backend with it, where proven."""

    envelope: str | None = None
    """The closed contract the proof came under, when it is one an authority could cover."""

    reasons: tuple[str, ...] = ()
    """Typed codes for every reason this is not override-eligible. Empty when it is."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "status": str(self.status),
            "outage": str(self.outage),
            "override_eligible": self.override_eligible,
            "detail": self.detail,
            "owner_unit_id": self.owner_unit_id,
            "envelope": self.envelope,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BackendSelfImpactAssessment":
        eligible = value.get("override_eligible")
        if not isinstance(eligible, bool):
            raise ValueError("self-impact override_eligible must be a boolean")
        raw_reasons = value.get("reasons", [])
        if not isinstance(raw_reasons, list) or any(
            not isinstance(reason, str) for reason in raw_reasons
        ):
            raise ValueError("self-impact reasons must be a string list")
        return cls(
            subject=str(value["subject"]),
            status=SelfImpactStatus(str(value["status"])),
            outage=SelfImpactOutage(str(value["outage"])),
            override_eligible=eligible,
            detail=str(value["detail"]),
            owner_unit_id=(
                str(value["owner_unit_id"])
                if value.get("owner_unit_id") is not None
                else None
            ),
            envelope=(
                str(value["envelope"]) if value.get("envelope") is not None else None
            ),
            reasons=tuple(str(reason) for reason in raw_reasons),
        )


_OUTAGE = {
    SystemdServiceAction.START: SelfImpactOutage.NONE,
    SystemdServiceAction.STOP: SelfImpactOutage.INDEFINITE_POSSIBLE,
    SystemdServiceAction.RESTART: SelfImpactOutage.TEMPORARY_POSSIBLE,
}


def assess_backend_self_impact(
    context: SystemdLifecycleContext, protection: ProtectionAssessment
) -> BackendSelfImpactAssessment:
    """Whether this operation reaches the backend serving the request, and how surely.

    Read from the typed, digest-bound context only. The free-form ``evidence`` mapping is
    deliberately not consulted: it is not part of the preview's identity, so a safety
    judgement resting on it would be one the digest could not detect a change in.
    """
    owner = context.runtime_owner
    closure = frozenset(context.effect_units)
    status, detail, owner_unit = _status_of(context, owner, closure)
    outage = (
        _OUTAGE[context.action]
        if status in (SelfImpactStatus.PROVEN, SelfImpactStatus.POSSIBLE)
        else SelfImpactOutage.NONE
    )
    reasons = _ineligibility(context, protection, status, owner, closure)
    return BackendSelfImpactAssessment(
        subject=SELF_IMPACT_SUBJECT_BACKEND_RUNTIME,
        status=status,
        outage=outage,
        override_eligible=not reasons,
        detail=detail,
        owner_unit_id=owner_unit,
        envelope=DOCKER_DIRECT_UNIX_CONTRACT if not reasons else None,
        reasons=reasons,
    )


def _status_of(
    context: SystemdLifecycleContext, owner: Any, closure: frozenset[str]
) -> tuple[SelfImpactStatus, str, str | None]:
    """Where the backend runs, and whether this closure reaches it.

    The order matters. A proven runtime owner answers the question outright in both
    directions; only after that does an unproven container context become the answer, and
    only after *that* does a backend contained directly by a systemd service. Anything the
    evidence does not settle stays ``unresolved`` rather than falling through to a negative.
    """
    if owner is not None and owner.status is DockerRuntimeOwnerStatus.RESOLVED:
        if owner.owner_unit_id in closure:
            return (
                SelfImpactStatus.PROVEN,
                "effect_closure_contains_proven_backend_runtime_owner",
                owner.owner_unit_id,
            )
        return (
            SelfImpactStatus.NOT_DETECTED,
            "effect_closure_disjoint_from_proven_backend_runtime_owner",
            owner.owner_unit_id,
        )

    if RUNTIME_OWNER_UMBRELLA_GAP in context.gaps:
        # The accepted connection terminates in a container context and which unit owns
        # that runtime was not established. The backend may or may not be downstream of
        # this closure and no reading here can say which.
        return (
            SelfImpactStatus.POSSIBLE,
            "backend_container_runtime_owner_unproven",
            None,
        )

    if (
        context.connection_unit is not None
        and context.connection_unit_type == HOST_SERVICE_UNIT_TYPE
    ):
        # The backend is contained by a systemd service directly, so the effect graph
        # speaks about it and its membership of the closure is the whole answer.
        if context.connection_unit in closure:
            return (
                SelfImpactStatus.PROVEN,
                "effect_closure_contains_backend_service_unit",
                context.connection_unit,
            )
        if context.effect_complete:
            return (
                SelfImpactStatus.NOT_DETECTED,
                "effect_closure_disjoint_from_backend_service_unit",
                context.connection_unit,
            )

    return (
        SelfImpactStatus.UNRESOLVED,
        "backend_runtime_containment_not_established",
        None,
    )


def _ineligibility(
    context: SystemdLifecycleContext,
    protection: ProtectionAssessment,
    status: SelfImpactStatus,
    owner: Any,
    closure: frozenset[str],
) -> tuple[str, ...]:
    """Every reason this hazard is not one a single typed authority could cover.

    Built as a list of refusals rather than a chain of permissions, so that a condition
    nobody thought to check leaves the plan ineligible rather than eligible. The caller
    treats a non-empty result as the answer.
    """
    reasons: list[str] = []

    if status is not SelfImpactStatus.PROVEN:
        # `possible` is included here on purpose: it is surfaced honestly and never
        # authorised in this build.
        reasons.append(f"self_impact_status_not_proven:{status}")

    if (
        owner is None
        or owner.status is not DockerRuntimeOwnerStatus.RESOLVED
        or owner.contract_version != DOCKER_DIRECT_UNIX_CONTRACT
        or owner.method_version != DOCKER_RUNTIME_OWNER_METHOD_VERSION
        or owner.provider != DOCKER_RUNTIME_OWNER_PROVIDER
        or not owner.owner_unit_id
    ):
        reasons.append("runtime_owner_outside_supported_self_impact_envelope")
    elif sorted(closure.intersection(context.management_units)) != [owner.owner_unit_id]:
        # The closure reaches the management path somewhere other than through the
        # backend's own runtime owner — another provider's unit, the connection's own
        # containing service. That is a different hazard with a different consequence, and
        # an authority about LocalPlane disappearing does not speak to it.
        reasons.append("effect_closure_touches_management_path_beyond_backend_runtime")

    if not _agent_is_a_proven_host_service(context, closure):
        reasons.append("localplane_agent_not_proven_outside_closure_as_host_service")
    elif not _reason_is_clear(protection, ProtectionReason.LOCALPLANE_AGENT):
        # The context facts and the protection roll-up have to agree. Requiring the reason
        # itself to be `clear` means this can never be eligible off the back of evidence
        # the protection model was not willing to call a proof.
        reasons.append("localplane_agent_protection_reason_not_clear")

    if not (context.effect_complete and context.management_complete and context.agent_complete):
        reasons.append("lifecycle_context_incomplete")
    if context.gaps:
        reasons.append("lifecycle_context_carries_unresolved_gaps")

    if protection.status is not ProtectionStatus.PROTECTED:
        reasons.append(f"protection_status_not_protected:{protection.status}")
    elif tuple(protection.reasons) != (ProtectionReason.MANAGEMENT_PATH,):
        reasons.append("protection_carries_reasons_beyond_the_management_path")

    return tuple(dict.fromkeys(reasons))


def _agent_is_a_proven_host_service(
    context: SystemdLifecycleContext, closure: frozenset[str]
) -> bool:
    """Whether the agent is a normal host-side service this closure provably misses.

    Every clause is a positive proof, and the unit *type* is one of them. Resolving the
    agent's containing unit says which unit contains it, not what kind of containment that
    is: a ``.scope`` belongs to a container runtime, systemd need not publish an edge from
    that runtime's daemon to it, and a closure that looks disjoint from a scope has
    therefore proven nothing about whether the agent survives.

    This build does not correlate a containerised agent's runtime owner and does not
    pretend to. Such an agent is simply never override-eligible, which is the supported
    production topology stated as a rule rather than as an assumption.
    """
    return (
        context.agent_complete
        and bool(context.agent_unit)
        and context.agent_unit_type == HOST_SERVICE_UNIT_TYPE
        and context.agent_unit not in closure
        and context.effect_complete
    )


def _reason_is_clear(
    protection: ProtectionAssessment, reason: ProtectionReason
) -> bool:
    """Whether one assessed reason was evaluated and proven not to apply."""
    return any(
        entry.reason is reason and entry.status is ProtectionStatus.CLEAR
        for entry in protection.assessed
    )
