"""The connection guard: what it is, what arms it, and what its settlement means.

**A connection guard is a durable, host-side, pre-armed reversal with a deadline.** It is
not a label put on a risky change, not a stronger confirmation and not a check performed
after a write. It is a mechanism, and the only thing it promises is this:

    the time for which a change can hold a resource in a state that has cut the operator
    off from LocalPlane is bounded by the guard window, and that bound is enforced by a
    component that does not depend on the operator's connection, on the request, on the
    backend process, or on the machine the backend is running on.

Everything else in this module exists to keep that sentence honest.

**Why a bounded reversal is safer than a refusal.** Today a change to the resource carrying
the operator's own path is blocked, which is safe and is also the reason a whole class of
work cannot be done at all. Guarded execution does not make the change less dangerous; it
makes the danger *recoverable without the operator*, which is the only property that
distinguishes a survivable mistake from the 2026-07-22 lockout.

**What proves the connection survived.** Nothing LocalPlane can observe about a resource
proves that an operator can still reach it: reading the value back says what the host holds,
not who can talk to it. The only honest proof is a *request that arrives*, whose own
transport re-establishes the management path by the same two-source rule the path is always
proven by, and resolves it to the very resource that was changed. That proof travels over
the path under test, which is what makes it a proof rather than a story about some other
channel. Its absence is what fires the guard.

**Absence, not failure.** The guard is not looking for evidence that something went wrong.
It fires because nothing arrived, which is exactly the shape of the failure it exists for:
an operator whose path is gone does not send an error, they send nothing.

**What the guard cannot do.** It cannot tell "the operator was cut off" from "the operator
did not come back in time", and it does not pretend to: both lapse the window and both are
reverted. Erring towards reversal is the only direction that is safe when the difference
cannot be established, and the record says the window lapsed rather than claiming a
diagnosis.

Nothing here knows what kind of resource it is guarding, what a reversal consists of, or
how one is dispatched. It is given phases and reports and it decides what they mean.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from localplane.backend.domain.changes import MutationOutcome, MutationReport


class GuardPhase(StrEnum):
    """Where an armed guard stands. Six values, and none of them is a judgement."""

    ARMING = "arming"
    """The host side was asked to hold a guard and has not confirmed it.

    A crash window, deliberately visible: the request may have been accepted, and until the
    answer is recorded LocalPlane does not know whether anything is holding this change.
    Nothing may be dispatched from here."""

    ARMED = "armed"
    """The host side has confirmed it holds the guard and stated when it expires.

    The only phase from which a guarded mutation may be dispatched."""

    DISARMED = "disarmed"
    """The guard was released before its deadline and performed no reversal."""

    FIRED = "fired"
    """The deadline passed with no proof, and the guard attempted its reversal.

    ``fired`` says the guard acted. It does *not* say the reversal landed — that is the
    reversal's own three-valued outcome, and it is carried separately for the same reason
    a mutation outcome is never derived from the state afterwards."""

    LOST = "lost"
    """The host side no longer knows this guard.

    The component holding it restarted, or the guard aged out of its memory. Whatever the
    change did to the resource is still done and nothing is watching it any more."""

    UNREACHABLE = "unreachable"
    """The guard could not be interrogated at all, so its phase is unknown.

    Distinct from :attr:`LOST`, which is an answer. This is the absence of one, and the two
    must not be collapsed: one says the guard is gone, the other says LocalPlane cannot
    tell whether it is."""


#: The phases in which a guard is still holding something and must not be assumed spent.
LIVE_GUARD_PHASES: tuple[GuardPhase, ...] = (GuardPhase.ARMING, GuardPhase.ARMED)

#: The phases a guard can settle into. A settled guard is holding nothing.
SETTLED_GUARD_PHASES: tuple[GuardPhase, ...] = (
    GuardPhase.DISARMED,
    GuardPhase.FIRED,
    GuardPhase.LOST,
    GuardPhase.UNREACHABLE,
)


class GuardAvailability(StrEnum):
    """Whether guarded execution exists for a plan at all."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class GuardPrerequisite(StrEnum):
    """What must be true before a guard may be armed. Each is proven, never assumed.

    Published on the plan so an operator can see which one is missing, and re-proved from
    fresh evidence at apply time so the plan they were shown cannot be what authorises the
    write. A prerequisite that could only be checked at planning time would be a promise
    about the past.
    """

    OPERATION_IS_REVERSIBLE = "operation_is_reversible"
    """The operation has a complete inverse LocalPlane may perform unattended.

    An operation whose reversal needs a decision has no guard: a deadline that expires into
    "somebody should look at this" protects nobody. This is the same property
    ``recovery_mode = auto`` already states, read here for a different purpose."""

    TARGET_IS_PROVEN_MANAGEMENT_PATH = "target_is_proven_management_path"
    """The target is *proven* to carry this request's management path.

    The positive proof, not the absence of a negative. A guard for a target whose relation
    is unknown would be a mechanism armed against a hazard nobody has established, and its
    reversal would be a host write nobody could justify."""

    RECOVERY_MATERIAL_IS_ARMED = "recovery_material_is_armed"
    """A durable checkpoint holds the verified value the guard would restore."""

    GUARD_CAPABILITY_DECLARED = "guard_capability_declared"
    """The host side declares it can hold a guard, having been asked rather than assumed."""

    HOST_SIDE_GUARD_ACCEPTED = "host_side_guard_accepted"
    """The host side has answered that it is holding this guard, with a deadline.

    The only prerequisite that cannot be evaluated before the attempt, and the one the
    others exist to make reachable. A guard LocalPlane asked for and did not get is not a
    guard, and the difference is the whole of what ``armed`` means."""


@dataclass(frozen=True)
class GuardPlan:
    """What a published plan says about guarded execution. Nothing here is armed.

    Rendered onto the preview and hashed into its digest, because a plan that could execute
    behind a guard and one that could not are different documents to review. What is
    deliberately *not* hashed is any identifier or instant — the same rule the protection
    section follows, for the same reason.
    """

    availability: GuardAvailability
    reason: str
    """The typed code for the roll-up: why guarded execution is or is not offered."""

    window_s: int
    """How long the guard would hold, in seconds. A policy constant, never a caller input."""

    prerequisites: tuple[GuardPrerequisite, ...]
    """Every prerequisite this build evaluates, in the order it evaluates them."""

    unmet: tuple[GuardPrerequisite, ...]
    """The ones that could not be proven when the plan was published."""

    guarantee: str
    """What arming would actually establish, in one sentence, for a reader."""

    @property
    def available(self) -> bool:
        return self.availability is GuardAvailability.AVAILABLE


#: The typed reason guarded execution is offered.
GUARD_AVAILABLE = "guarded_execution_available"

#: The typed reason a plan needs no guard: its target is not the management path, so
#: ordinary execution is the path and a guard would be a mechanism against no hazard.
GUARD_NOT_REQUIRED = "guard_not_required"

#: The typed reason guarded execution cannot be offered for an operation that has no
#: complete inverse LocalPlane may perform without asking anybody.
GUARD_OPERATION_NOT_REVERSIBLE = "operation_has_no_unattended_reversal"

#: The typed reason guarded execution cannot be offered because the host side does not
#: declare it can hold one.
GUARD_CAPABILITY_UNDECLARED = "guard_capability_undeclared"

#: The typed reason a target whose management-path relation is unresolved gets no guard.
GUARD_TARGET_UNPROVEN = "management_path_unproven"


@dataclass(frozen=True)
class GuardRequest:
    """What the engine asks a host side to hold. Generic, like :class:`MutationRequest`.

    Every field comes from the durable checkpoint written a moment earlier, except the
    window, which comes from policy. There is nothing here an API caller could have
    supplied and nothing an operation could invent: the reversal is the change's own
    inverse, with the value the change will leave behind as the state it expects to find.

    That last part is why a guard can only undo what its change did. A guard that fires
    against a target the change never reached, or one a third party has since moved, is
    refused by the execution path before anything is written — not by a check somebody
    remembered to add, but because the reversal's precondition does not hold.
    """

    guard_id: str
    attempt_id: str
    """The identity the reversal's own dispatch will carry, minted before it happens.

    Fixed here rather than at firing time so the record can name the write in advance and
    so a privileged path that remembers attempts recognises it as one attempt rather than
    as a new one each time it is asked."""

    correlation: Mapping[str, Any]
    """The executor's own material for reaching the target. Opaque to everything above."""

    guarded_value: bool | int
    """What the change will leave behind, and therefore what the reversal expects to find."""

    restore_value: bool | int
    """What the checkpoint holds, and therefore what the reversal writes."""

    window_s: int


@dataclass(frozen=True)
class GuardArmed:
    """The host side is holding a guard, and this is what it said about it."""

    holder_id: str
    """Which instance of the host-side component is holding it.

    Load-bearing rather than descriptive: a guard is held in a process, and an answer from
    a *different* instance than the one that armed it is not a report about this guard. It
    is how :attr:`GuardPhase.LOST` is told from a guard that is genuinely still held."""

    expires_at: str
    """When the holder says the window ends. The holder's clock, not LocalPlane's.

    Recorded as what it is — a statement by the component that will act on it — rather than
    recomputed locally, because the deadline that matters is the one the guard will use."""

    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GuardRefused(Exception):
    """The host side would not hold a guard, and nothing has been written.

    Raised only before the write boundary. A refusal here ends the Run with no Change, for
    the same reason a checkpoint that cannot be written does: the code that could perform a
    write had not been reached.
    """

    code: str
    detail: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - exercised through the code, not the text
        return self.code


@dataclass(frozen=True)
class GuardReport:
    """What became of one guard, as the component holding it describes it.

    ``mutation`` is present only for a guard that fired, and it is the reversal's own typed
    outcome — ``not_written``, ``written`` or ``write_unknown`` — never derived from what
    the resource holds afterwards.
    """

    phase: GuardPhase
    holder_id: str | None = None
    fired_at: str | None = None
    mutation: MutationReport | None = None
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def settled(self) -> bool:
        return self.phase in SETTLED_GUARD_PHASES

    @property
    def reverted(self) -> bool:
        """Whether the guard's reversal is claimed to have reached the host.

        Deliberately not "the resource is back": that needs a reading, and this is a report
        about a dispatch. The distinction is the same one the write boundary makes.
        """
        return (
            self.phase is GuardPhase.FIRED
            and self.mutation is not None
            and self.mutation.outcome is MutationOutcome.WRITTEN
        )


class GuardSettlement(StrEnum):
    """What a settled guard means for the Run that armed it. Five endings, five records.

    This is the complete, deliberately unambiguous mapping, and each member
    corresponds to a distinct combination of durable facts rather than to a feeling about
    how bad the situation is.
    """

    KEPT = "kept"
    """The window did not lapse, the connection was proven, and the guard was released.

    The only settlement that lets a guarded Run succeed, and it requires the operation's own
    proof as well — the guard answers for the connection and never for the result."""

    REVERTED = "reverted"
    """The window lapsed, the guard's reversal was written, and a reading proves it.

    The Run ends ``rolled_back``: the guard performed the restoration the checkpoint exists
    for, and the same evidence rule applies to it as to a restoration LocalPlane performs
    itself — an acknowledgement is not a restoration until something reads it back."""

    REVERSAL_UNPROVEN = "reversal_unproven"
    """The guard fired and what the resource holds afterwards cannot be established.

    Covers a reversal that provably wrote nothing, one that may or may not have landed, and
    one that was written and could not be read back. All three are the same statement about
    what LocalPlane knows, and the typed reason on the Change says which of them it was."""

    GUARD_LOST = "guard_lost"
    """The holder no longer knows the guard, so nothing was watching this change.

    Never an ending that claims success. What the resource holds now is whatever the change
    left there, and no mechanism is going to move it."""

    GUARD_UNREACHABLE = "guard_unreachable"
    """The guard could not be interrogated, so its phase is unknown.

    The most conservative ending and the one that must never be softened: the reversal may
    be about to happen, may have happened, or may never happen."""


def settle(report: GuardReport, *, reversal_proved: bool) -> GuardSettlement:
    """What a settled guard's report means, given whether a reading proved the reversal.

    The order is the rule. A guard that was disarmed within its window is the only path to
    ``kept``; a guard that fired is judged on the reversal's own outcome *and* on a reading,
    never on either alone; and a guard whose phase could not be established is unreachable
    rather than assumed to be anything.

    ``reversal_proved`` is supplied rather than derived here because proving it means
    reading the resource, which this module has no way to do and no business knowing how to.
    """
    if report.phase is GuardPhase.DISARMED:
        return GuardSettlement.KEPT
    if report.phase is GuardPhase.FIRED:
        if report.reverted and reversal_proved:
            return GuardSettlement.REVERTED
        return GuardSettlement.REVERSAL_UNPROVEN
    if report.phase is GuardPhase.LOST:
        return GuardSettlement.GUARD_LOST
    return GuardSettlement.GUARD_UNREACHABLE
