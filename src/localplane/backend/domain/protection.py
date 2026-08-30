"""The protected-resource core: whether a thing is protected, why, and on what evidence.

**A protected resource is one LocalPlane must not casually change, for a stated reason.**
That is a different question from ownership, and collapsing the two would lose both. A
resource may be owned by LocalPlane and protected; it may be owned by somebody else and not
protected at all. Ownership answers "whose is this"; protection answers "what does changing
it put at risk".

**Two typed reasons exist, and callers evaluate only the ones applicable to their
resource.** The management path protects the resource carrying the connection an operator
is reaching LocalPlane over. Systemd lifecycle additionally protects the agent's own
containing unit through fresh cgroup/systemd evidence. A network-interface judgement still
assesses only the former; adding a lifecycle-only reason must not broaden what its ``clear``
claim means.

Other reasons are foreseeable and deliberately absent until evidence exists: LocalPlane's
database, its agent channel, and storage that other things depend on. A reason with no
evidence behind it is a placeholder, and a placeholder in a safety model is worse than an
absence — it makes ``clear`` mean "the reasons I bothered to implement did not apply".

**Nothing here knows what kind of resource it is judging.** There is no address in this
module, no host, no operating system and no notion of a device. It is given assessed
reasons and it rolls them up. What it deliberately does hold is the rule that the roll-up
must be honest:

* ``protected`` — at least one implemented reason is *proven* to apply.
* ``unknown`` — no reason is proven and at least one could not be settled. Not
  ``clear``: "LocalPlane could not tell" and "LocalPlane checked and it does not apply"
  are different sentences and only one of them is a green light.
* ``clear`` — every implemented reason was evaluated and none applies.

``clear`` therefore always carries the same silent caveat: *for the reasons this build
implements*. The API says so in as many words rather than letting the single word carry it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class ProtectionStatus(StrEnum):
    """Whether a resource is protected, in three honest values."""

    PROTECTED = "protected"
    """At least one implemented protection reason is proven to apply."""

    CLEAR = "clear"
    """Every implemented protection reason was evaluated and none applies.

    Never "safe". It is scoped to the reasons this build can evaluate, and it says nothing
    about ownership, about whether an operation is a good idea, or about whether executing
    it is allowed — which is a separate assessment with its own blockers."""

    UNKNOWN = "unknown"
    """A reason could not be settled, so protection cannot be ruled out.

    Unknown is a state of the evidence, not a failure. It is preferable to a confident
    answer LocalPlane has not earned, and everything downstream treats it as what it is:
    not proof of safety."""


class ProtectionReason(StrEnum):
    """Why a resource is protected. Each reason is assessed independently."""

    MANAGEMENT_PATH = "management_path"
    """This resource carries the connection an operator is reaching LocalPlane over.

    Proven only from the transport a request actually arrived on, corroborated by the
    kernel's own routing answer, and never from a name, a default route, a header a caller
    supplied, or a claim in a request body."""

    LOCALPLANE_AGENT = "localplane_agent"
    """The operation's conservative effect closure contains LocalPlane's own agent unit.

    Proven from fresh ``/proc/self/cgroup`` → systemd containment evidence, never from a
    process or service name.  Failure to resolve that chain is ``unknown``.
    """


class ManagementPathRelation(StrEnum):
    """How one resource stands to the operator's management path.

    Three values, and the third is a real answer. Read the first two as *target* and *not
    target*: the resource either is the one carrying the path or demonstrably is not.
    """

    ON_MANAGEMENT_PATH = "on_management_path"
    """The management path is proven, and it is this resource. The *target*."""

    NOT_ON_MANAGEMENT_PATH = "not_on_management_path"
    """The management path is proven, and it is some other resource. *Not* the target."""

    UNKNOWN = "unknown"
    """The management path itself is unresolved.

    Every resource is ``unknown`` in this case, including the ones that are obviously not
    carrying it. Marking the rest ``not_on_management_path`` while the path is unresolved
    would be inventing a negative from an absence, and a fleet of confident negatives with
    one unresolved positive among them is exactly how the wrong thing gets changed."""


@dataclass(frozen=True)
class ManagementPathVerdict:
    """Which resource carries the management path right now — or why nothing does.

    Host-scoped and resource-independent: the question "where does the operator's
    connection terminate" has one answer per request, and every resource's *relation* to
    that answer is derived from it. Deriving it once and applying it many times is what
    stops two objects on one host from being told different things about the same path.

    ``resource_id`` is the whole of the positive answer. It is ``None`` unless the path was
    proven, and ``reason`` then carries the typed code saying which piece of evidence was
    missing, unusable or in conflict.
    """

    resource_id: str | None
    reason: str
    evidence_id: str | None = None
    observed_at: str | None = None
    missing_evidence: tuple[str, ...] = ()

    @property
    def confirmed(self) -> bool:
        return self.resource_id is not None


@dataclass(frozen=True)
class ReasonAssessment:
    """One implemented protection reason, evaluated against one resource.

    ``detail`` is a typed code, never prose: it is what a caller branches on to tell
    "proven to be the target" from "the operation could not affect this anyway" from each
    of the several distinct ways the evidence can fail to settle the question.

    ``evidence_id`` and ``observed_at`` name the record the judgement was made from, so
    that a preview published today stays answerable tomorrow. They are audit, not identity:
    a later record proving exactly the same thing is not a different judgement.
    """

    reason: ProtectionReason
    status: ProtectionStatus
    detail: str
    evidence_id: str | None = None
    observed_at: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    missing_evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProtectionAssessment:
    """Whether this resource is protected, why, and what the answer rests on.

    Built by :func:`assess_protection` from the reasons that were evaluated. Both the
    roll-up and the parts are kept: an operator shown ``unknown`` needs to know which
    reason is unsettled, and a build that later implements a second reason must not make
    the first one's answer harder to find.
    """

    status: ProtectionStatus
    reasons: tuple[ProtectionReason, ...]
    """The reasons proven to apply. Empty unless ``status`` is ``protected``."""

    unresolved: tuple[ProtectionReason, ...]
    """The reasons whose evidence could not be settled. Empty unless ``status`` is
    ``unknown``."""

    assessed: tuple[ReasonAssessment, ...]
    """Every implemented reason, with what was decided about it and from what."""

    management_path: ManagementPathRelation
    """How this resource stands to the management path, independently of whether that
    makes it protected. An operation that could not affect the path leaves this at whatever
    the evidence says while protection comes out ``clear``: the two are different facts and
    only one of them is about the operation."""

    reason: str
    """The typed code for the roll-up — the most specific thing that can be said about why
    protection is what it is."""

    missing_evidence: tuple[str, ...]
    """What would settle an unresolved reason, named rather than worked around."""

    @property
    def protected(self) -> bool:
        return self.status is ProtectionStatus.PROTECTED

    @property
    def evidence_id(self) -> str | None:
        """The record the decisive reason was assessed from, when there is one."""
        for entry in self.assessed:
            if entry.evidence_id is not None:
                return entry.evidence_id
        return None

    @property
    def evidence_observed_at(self) -> str | None:
        for entry in self.assessed:
            if entry.evidence_id is not None:
                return entry.observed_at
        return None


#: The typed code for a resource the management path was proven *not* to be.
NOT_THE_MANAGEMENT_PATH = "not_the_management_path"

#: The typed code for a resource whose management-path relation cannot matter, because the
#: operation being judged could not affect a management path whatever the relation is.
CANNOT_AFFECT_MANAGEMENT_PATH = "operation_cannot_affect_management_path"


def assess_management_path_reason(
    verdict: ManagementPathVerdict, *, resource_id: str, applicable: bool
) -> tuple[ReasonAssessment, ManagementPathRelation]:
    """Evaluate the management-path reason for one resource, and state its relation.

    Two facts come out, and they are not the same fact:

    * the **relation** is about the resource — is this the one carrying the path, is it
      demonstrably not, or is the path itself unresolved. It does not depend on what is
      being done to the resource;
    * the **reason assessment** is about the judgement being made — an operation that could
      not affect a management path is ``clear`` for this reason even when the relation is
      ``on_management_path``, because the reason is asking what changing it would put at
      risk and the answer is nothing.

    The unresolved case is the one that must not be softened. When the path itself is
    unknown, *every* resource is ``unknown``, including ones a reader would swear are not
    carrying it. A negative invented from an absence is still an invention, and it is the
    kind an operator acts on.
    """
    if verdict.confirmed:
        relation = (
            ManagementPathRelation.ON_MANAGEMENT_PATH
            if verdict.resource_id == resource_id
            else ManagementPathRelation.NOT_ON_MANAGEMENT_PATH
        )
    else:
        relation = ManagementPathRelation.UNKNOWN

    if not applicable:
        return (
            ReasonAssessment(
                reason=ProtectionReason.MANAGEMENT_PATH,
                status=ProtectionStatus.CLEAR,
                detail=CANNOT_AFFECT_MANAGEMENT_PATH,
                evidence_id=verdict.evidence_id,
                observed_at=verdict.observed_at,
            ),
            relation,
        )

    if relation is ManagementPathRelation.ON_MANAGEMENT_PATH:
        status, detail = ProtectionStatus.PROTECTED, verdict.reason
    elif relation is ManagementPathRelation.NOT_ON_MANAGEMENT_PATH:
        status, detail = ProtectionStatus.CLEAR, NOT_THE_MANAGEMENT_PATH
    else:
        status, detail = ProtectionStatus.UNKNOWN, verdict.reason

    return (
        ReasonAssessment(
            reason=ProtectionReason.MANAGEMENT_PATH,
            status=status,
            detail=detail,
            evidence_id=verdict.evidence_id,
            observed_at=verdict.observed_at,
        ),
        relation,
    )


def assess_resource_protection(
    verdict: ManagementPathVerdict,
    *,
    resource_id: str,
    applicable: bool = True,
) -> ProtectionAssessment:
    """The interface-style protection judgement, evaluating its management-path reason.

    The generic roll-up accepts multiple reasons, but applicability belongs to the subsystem:
    systemd lifecycle supplies both management-path and agent-unit assessments through its
    own evidence chain, while this reusable resource judgement remains one evaluation.

    ``applicable`` is how a *judgement* narrows a reason without narrowing the fact. An
    operation that could not affect a management path is ``clear`` for this reason while
    the relation still says what the evidence says, because "this resource is the
    management path" and "changing it this way endangers the management path" are different
    sentences and only the second one is about the operation.
    """
    reason, relation = assess_management_path_reason(
        verdict, resource_id=resource_id, applicable=applicable
    )
    return roll_up_protection(
        (reason,),
        management_path=relation,
        missing_evidence=(
            verdict.missing_evidence if reason.status is ProtectionStatus.UNKNOWN else ()
        ),
    )


def roll_up_protection(
    assessed: tuple[ReasonAssessment, ...],
    *,
    management_path: ManagementPathRelation,
    missing_evidence: tuple[str, ...] = (),
) -> ProtectionAssessment:
    """Roll up the evaluated reasons into one status, without softening any of them.

    The order is the whole rule. A proven reason wins outright; failing that, an unsettled
    one keeps the answer at ``unknown``; only when every reason has been evaluated and none
    applies is the resource ``clear``. There is no path by which an unsettled reason
    becomes a clean bill of health, which is the single thing this function exists to
    guarantee.

    An empty set of reasons is refused rather than reported ``clear``. "Nothing was
    evaluated" and "everything was evaluated and nothing applied" are the two sentences a
    protection model must never confuse, and a build that removed its last reason should
    fail loudly instead of quietly turning every resource green.
    """
    if not assessed:
        raise ValueError("a protection assessment must evaluate at least one reason")

    protected = tuple(e.reason for e in assessed if e.status is ProtectionStatus.PROTECTED)
    unresolved = tuple(e.reason for e in assessed if e.status is ProtectionStatus.UNKNOWN)

    if protected:
        decisive = next(e for e in assessed if e.status is ProtectionStatus.PROTECTED)
        status = ProtectionStatus.PROTECTED
    elif unresolved:
        decisive = next(e for e in assessed if e.status is ProtectionStatus.UNKNOWN)
        status = ProtectionStatus.UNKNOWN
    else:
        decisive = assessed[0]
        status = ProtectionStatus.CLEAR

    return ProtectionAssessment(
        status=status,
        reasons=protected,
        unresolved=unresolved,
        assessed=assessed,
        management_path=management_path,
        reason=decisive.detail,
        missing_evidence=missing_evidence,
    )
