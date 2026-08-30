"""The write boundary: what a Change is, and the three truths a mutation can produce.

**A Change is the durable record that LocalPlane entered a path on which a host write may
occur.** It is not a plan, not a confirmation, not a checkpoint, not a management
transition and not an intent revision. Each of those is a decision about LocalPlane's own
records; a Change is the moment LocalPlane stopped deciding and started acting. It comes
into existence at the boundary and not before — a preview does not create one, a
confirmation does not create one, and arming does not create one, because none of those can
have changed anything.

**The boundary is crossed before the write, not after it.** That ordering is the whole of
the crash-safety argument, and it costs one deliberate window: between the record saying
"a mutation is about to be dispatched" and the record saying what became of it, there is an
interval in which a process death leaves the question open. The window is not hidden. It is
made visible by the ``dispatch_began_at`` column, and :func:`outcome_on_recovery` is the rule
that a Change which says dispatch began and does not say what happened is
:data:`MutationOutcome.WRITE_UNKNOWN` — conservatively, on every restart, without needing
to be right about anything else.

**Three outcomes, and the third is not a failure mode of the other two.**
:class:`MutationOutcome` is the vocabulary that separates "nothing happened" from "we do not
know". They are different facts with different obligations: the first releases the target
and ends the run, the second obliges LocalPlane to restore and prove, or to say out loud
that it cannot.

**The outcome is never derived from the resulting value.** Reading the target back afterwards
answers *what is it now*. It does not answer *did our write occur* — a value that already
matched, a second writer and a successful write are indistinguishable from the number
alone. The two questions have separate answers here and nothing collapses them.

Nothing in this module knows what kind of thing is being changed. It is given outcomes and
it says what they mean, which is what lets the same vocabulary cover a change to a service,
a file or a container when those arrive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class MutationOutcome(StrEnum):
    """What became of one dispatched mutation. Three truths, never interchangeable."""

    NOT_WRITTEN = "not_written"
    """Provably no effect. Execution failed before the point at which the target system
    could have accepted the mutation: a precondition refused it, the request was never
    dispatched, or the target answered with a definite error instead of an
    acknowledgement."""

    WRITTEN = "written"
    """The privileged path received an authoritative acknowledgement, correlated to this
    request, that the target system accepted the write."""

    WRITE_UNKNOWN = "write_unknown"
    """The mutation may have taken effect and LocalPlane cannot prove whether it did.

    Not an error and not a failure: a state of the evidence. It obliges recovery rather
    than a report, and it is never converted into either of the other two by observing the
    result — see the module docstring for why that would be a different question."""


class HostEffect(StrEnum):
    """What a durable record may claim about the host. Widened from one value to three.

    Every event table in LocalPlane carries this. Earlier schema versions CHECKed every
    one of them to ``none`` — structurally incapable of claiming a write. Exactly one table may
    now say otherwise, and it may say one of two further things: the host was written, or
    it may have been. There is no value here that means "probably not".
    """

    NONE = "none"
    WRITTEN = "written"
    WRITE_UNKNOWN = "write_unknown"


#: The mapping is total and it is one-way. An outcome decides what the record may claim;
#: nothing reads a claim back to conclude an outcome.
_HOST_EFFECT: dict[MutationOutcome, HostEffect] = {
    MutationOutcome.NOT_WRITTEN: HostEffect.NONE,
    MutationOutcome.WRITTEN: HostEffect.WRITTEN,
    MutationOutcome.WRITE_UNKNOWN: HostEffect.WRITE_UNKNOWN,
}


def host_effect_for(outcome: MutationOutcome) -> HostEffect:
    """What a record may claim about the host, given what became of the mutation.

    ``not_written`` maps to ``none`` and that is not a contradiction with the Change
    existing: a Change records that LocalPlane *entered* the write path, and a refusal that
    happened inside the privileged path — a precondition that did not hold, a target that
    answered with an error — is a crossing of the boundary with a proven-empty effect. The
    two facts are deliberately separate columns.
    """
    return _HOST_EFFECT[outcome]




def outcome_on_recovery(
    dispatch_began: bool, recorded: MutationOutcome | None
) -> MutationOutcome:
    """What a Change means on restart, when nothing finished writing its result.

    The rule is deliberately conservative and deliberately simple, because it has to be
    right without any other evidence being available:

    * a recorded outcome is the answer — it was written by the path that knew;
    * dispatch began and nothing recorded an outcome — ``write_unknown``. The process died
      somewhere between handing the request over and learning what happened, and there is
      no honest fourth answer;
    * dispatch never began — ``not_written``. The record says the request had not been sent
      when the process was last able to speak, and that is a proof, not an assumption.
    """
    if recorded is not None:
        return recorded
    return MutationOutcome.WRITE_UNKNOWN if dispatch_began else MutationOutcome.NOT_WRITTEN


class VerificationOutcome(StrEnum):
    """Whether a fresh reading proved the target reached the value that was wanted.

    A successful mutation acknowledgement is not this. The acknowledgement says the target
    system accepted a request; verification says an independent read, through the ordinary
    observation path, found the value the intent holds. Everything that is not a proof is
    named separately, because "could not read it" and "read it and it is wrong" lead to the
    same recovery and are not the same fact in a record.
    """

    NOT_ATTEMPTED = "not_attempted"
    VERIFIED = "verified"
    MISMATCH = "mismatch"
    VALUE_UNREADABLE = "value_unreadable"
    OBSERVATION_UNAVAILABLE = "observation_unavailable"
    SOURCE_INCOMPATIBLE = "source_incompatible"
    TARGET_ABSENT = "target_absent"

    @property
    def proved(self) -> bool:
        return self is VerificationOutcome.VERIFIED


class ChangeResult(StrEnum):
    """How a Change ended. Four endings, and one of them is not an ending."""

    IN_FLIGHT = "in_flight"
    """The boundary was crossed and nothing has settled it yet. Persisted so that a process
    that dies mid-apply leaves a record that says so rather than a record that says
    nothing."""

    SUCCEEDED = "succeeded"
    """Written, and an independent reading proved the wanted value."""

    FAILED = "failed"
    """Provably nothing was written. The only ending permitted after a ``not_written``
    mutation, and never permitted after a ``write_unknown`` one."""

    ROLLED_BACK = "rolled_back"
    """The previous value was restored and a fresh reading proved it. Never claimed from a
    rollback acknowledgement alone."""

    RECOVERY_REQUIRED = "recovery_required"
    """LocalPlane cannot prove a safe final state after a mutation that may have happened.
    A first-class truthful ending, not an error path — and the one this whole vocabulary
    exists so that nothing has to lie its way out of."""


class RecoveryReason(StrEnum):
    """Why a Change ended in :data:`ChangeResult.RECOVERY_REQUIRED`. Typed, never prose."""

    APPLY_WRITE_UNKNOWN = "apply_write_unknown"

    APPLY_INTERRUPTED_AFTER_WRITE = "apply_interrupted_after_write"
    """The dispatch recorded that it wrote, and the process died before the Run ended.

    Deliberately not :data:`APPLY_WRITE_UNKNOWN`. That one says nobody knows whether the
    write occurred; this one says it is *known* to have occurred and what happened next —
    the verification, or the restoration a failed verification would have started — is what
    nobody knows. Collapsing the two would take a fact LocalPlane had established and
    report it as unestablished, and an operator reading the record afterwards could not
    tell which of the two had happened.

    Nothing is retried and nothing is put back here: a record nobody has looked at since a
    crash is not authority to write to a host. The object stays held until somebody does.
    """

    ROLLBACK_WRITE_UNKNOWN = "rollback_write_unknown"
    ROLLBACK_NOT_DISPATCHED = "rollback_not_dispatched"
    ROLLBACK_REFUSED = "rollback_refused"
    ROLLBACK_VERIFICATION_FAILED = "rollback_verification_failed"
    ROLLBACK_VERIFICATION_UNAVAILABLE = "rollback_verification_unavailable"
    PRE_ROLLBACK_READ_UNAVAILABLE = "pre_rollback_read_unavailable"
    TARGET_ABSENT_AFTER_MUTATION = "target_absent_after_mutation"

    ACTION_NOT_PROVEN = "action_not_proven"
    """An action was carried out and the resulting state is not the one it promised.

    Distinct from the rollback reasons above because there was no rollback and there could
    not be one: an action has no inverse LocalPlane may perform on its own. Issuing the
    opposite verb because a verification failed is not a restoration — it is a second change
    nobody asked for, against a resource whose state is already not what was expected, and it
    can make the situation worse. So the honest ending is to say what was asked for, what was
    observed, and that the two do not agree."""

    ACTION_VERIFICATION_UNAVAILABLE = "action_verification_unavailable"
    """An action was carried out and the resulting state could not be read at all."""

    GUARD_REVERSAL_UNPROVEN = "guard_reversal_unproven"
    """A connection guard fired and what the target holds afterwards cannot be established.

    The guarded counterpart of the rollback reasons above, and one code rather than three
    because the reversal was dispatched by the guard rather than by this process: what the
    record can say is that the guard acted and that nothing proves the result. The guard's
    own outcome — ``not_written``, ``written`` or ``write_unknown`` — is recorded beside it
    in the rollback columns it belongs in, so the more specific answer is still there for a
    reader who wants it."""

    GUARD_LOST = "guard_lost"
    """The component holding a connection guard no longer knows it.

    The change is applied, the window is irrelevant, and nothing is going to put it back.
    An ending that must never be confused with a guard that fired: one says a reversal was
    attempted, this one says none ever will be."""

    GUARD_UNREACHABLE = "guard_unreachable"
    """A connection guard could not be interrogated, so its outcome is unknown.

    The most conservative of the three and the one that must not be softened: the reversal
    may be about to happen, may have happened, or may never happen."""

    GUARD_RELEASED_WITHOUT_RECORDED_PROOF = "guard_released_without_recorded_proof"
    """A guard was released and what released it was never written down.

    The narrow crash window in keeping a guarded change: the holder was told to stand down
    and the process died before the evidence that justified it reached the store. Nothing
    was reverted and the operation's own result was proved before the hold began — but
    ``succeeded`` would be claiming a proof LocalPlane cannot produce, so the honest ending
    is to say the hold is unresolved and let a person look."""


class RecoveryActionKind(StrEnum):
    """The two ways out of a recovery hold, and they are not two flavours of one thing.

    LocalPlane distinguishes them. A **retry** asks LocalPlane to try again to reach the
    end state the original Change wanted, and it may write to the host. A **resolution** is a person
    saying they have handled the situation, and it may not — it claims nothing about what
    the host holds, and the store refuses a resolution row that carries any mutation at all.
    """

    RETRY = "retry"
    RESOLVE = "resolve"


class RecoveryAttemptOutcome(StrEnum):
    """What became of one recovery action. Nine members, and three of them release the hold.

    Deliberately *not* the Change's own vocabulary. ``succeeded`` and ``failed`` are answers
    about the original execution, and a recovery attempt is a later event with its own
    answer; reusing the words would invite a reader to conclude the Change had changed its
    mind about what happened, which is the one thing the write-boundary rules exist to prevent.
    """

    IN_FLIGHT = "in_flight"
    """An attempt is under way. Persisted before anything can be dispatched, so a process
    that dies mid-recovery leaves a row saying so rather than a row saying nothing."""

    PROVEN = "proven"
    """A fresh reading proved the original operation's required end state, and **nothing was
    written**. The best ending there is: the situation resolved itself, or the uncertain
    write had in fact landed, and LocalPlane established that by looking rather than by
    acting."""

    VERIFIED = "verified"
    """A new mutation was dispatched, acknowledged, and an independent reading proved the end
    state. The only ending in which a retry both wrote and may say the outcome was reached."""

    NOT_WRITTEN = "not_written"
    """A new mutation was dispatched and the execution path proved it wrote nothing. The
    original uncertainty is untouched, so the hold stays."""

    WRITE_UNKNOWN = "write_unknown"
    """A new mutation may have landed and nothing can say. A second unprovable write on top
    of an unprovable one; the hold stays, and it has to."""

    NOT_PROVEN = "not_proven"
    """Written, and the reading afterwards did not prove the end state. The hold stays."""

    REFUSED = "refused"
    """Nothing was dispatched, so there is provably no new host effect. Everything that stops
    a retry before it can write ends here, with the typed reason beside it: the operation is
    no longer applicable, a gate does not pass now, the intended outcome has been changed
    under it, the target could not be read, or nobody has authorised a second write."""

    INTERRUPTED = "interrupted"
    """The process died during the attempt and it was settled conservatively on restart.
    Never a release, never a retry: LocalPlane does not act again on the strength of a record
    nobody has looked at since a crash."""

    RESOLVED = "resolved"
    """A person released the hold. Says nothing whatever about the host."""

    @property
    def settled(self) -> bool:
        return self is not RecoveryAttemptOutcome.IN_FLIGHT

    @property
    def releases_hold(self) -> bool:
        """Whether this ending gives the object back.

        Three of the nine, and the reason each does is different: ``proven`` and ``verified``
        because the end state the Change wanted is now established by evidence, and
        ``resolved`` because a human took responsibility for it. Nothing else may, and the
        store CHECKs the column to exactly these three.
        """
        return self in _RELEASES_HOLD


_RELEASES_HOLD = frozenset({
    RecoveryAttemptOutcome.PROVEN,
    RecoveryAttemptOutcome.VERIFIED,
    RecoveryAttemptOutcome.RESOLVED,
})


class RecoveryHoldState(StrEnum):
    """Whether a Change is still holding its object, derived and never stored.

    One fact, one home: the hold *is* the durable lock plus the append-only attempt history,
    and a column restating it would be a second thing to keep in step. ``resolved`` does not
    mean the Change succeeded and never will — the Change goes on saying ``recovery_required``
    for as long as it exists, because that is what happened.
    """

    NOT_REQUIRED = "not_required"
    UNRESOLVED = "unresolved"
    RESOLVED = "resolved"


def recovery_outcome_for(
    mutation: MutationOutcome, verification: VerificationOutcome
) -> RecoveryAttemptOutcome:
    """What a retry that dispatched a mutation ended as. Total, and one-way.

    The same discipline the apply path follows: an outcome decides what the record may say,
    and nothing reads the record back to conclude an outcome. ``written`` is the only branch
    where a verification is consulted at all — after ``write_unknown`` a reading answers what
    the host holds, which is a different question from whether this write occurred.
    """
    if mutation is MutationOutcome.NOT_WRITTEN:
        return RecoveryAttemptOutcome.NOT_WRITTEN
    if mutation is MutationOutcome.WRITE_UNKNOWN:
        return RecoveryAttemptOutcome.WRITE_UNKNOWN
    return (
        RecoveryAttemptOutcome.VERIFIED
        if verification.proved
        else RecoveryAttemptOutcome.NOT_PROVEN
    )


class RunEvent(StrEnum):
    """The append-only transcript's vocabulary. Closed, and every member is a fact.

    A transcript exists because there are now transitions worth recording per se: a Run can
    be confirmed, armed, dispatched, verified, rolled back and abandoned, and the row alone
    can no longer answer when each of those happened or in what order. What it is *not* is
    a log. Free-form debug text is not authoritative state; these names are, and human
    detail travels beside them rather than instead of them.
    """

    RUN_PLANNED = "run_planned"
    RUN_CANCELLED = "run_cancelled"

    CONFIRMATION_SATISFIED = "confirmation_satisfied"
    CONFIRMATION_REQUIRED = "confirmation_required"
    CONFIRMATION_CONSUMED = "confirmation_consumed"

    APPLY_REFUSED = "apply_refused"

    ARMING_STARTED = "arming_started"
    CHECKPOINT_WRITTEN = "checkpoint_written"
    ARMING_FAILED = "arming_failed"

    TARGET_CORRELATED = "target_correlated"
    """An action's preparation: the target was identified and no recovery was armed.

    Deliberately not ``arming_started``. Arming means establishing recovery, and an action
    has none to establish — a transcript that borrowed the name would read like an arming
    that never completed, which is a different and much worse thing to have happened."""

    TARGET_CORRELATION_FAILED = "target_correlation_failed"
    """The target could not be identified. Before the boundary, and nothing was dispatched."""

    WRITE_BOUNDARY_CROSSED = "write_boundary_crossed"
    MUTATION_DISPATCHED = "mutation_dispatched"
    MUTATION_RESULT = "mutation_result"

    VERIFICATION_STARTED = "verification_started"
    VERIFICATION_RESULT = "verification_result"

    ROLLBACK_STARTED = "rollback_started"
    ROLLBACK_MUTATION_DISPATCHED = "rollback_mutation_dispatched"
    ROLLBACK_MUTATION_RESULT = "rollback_mutation_result"
    ROLLBACK_VERIFICATION_STARTED = "rollback_verification_started"
    ROLLBACK_VERIFICATION_RESULT = "rollback_verification_result"

    GUARD_ARMED = "guard_armed"
    """The host side confirmed it is holding a reversal, and said when it expires.

    Written before the write boundary, like ``checkpoint_written``, and for the same
    reason: it is the last thing that happens while it is still true that nothing about the
    host could have moved."""

    GUARD_ARMING_FAILED = "guard_arming_failed"
    """No guard was established. Before the boundary, and nothing was dispatched."""

    GUARD_HOLD_STARTED = "guard_hold_started"
    """The change was written and verified, and the guard's deadline is now what decides.

    The one state in this product where LocalPlane is waiting for something it cannot
    cause: a request that proves the operator can still reach it over the object that was
    just changed."""

    GUARD_CONNECTION_PROVED = "guard_connection_proved"
    """A request arrived whose own transport re-proved the management path over this target.

    The only evidence that can keep a guarded change, and it is deliberately not something
    LocalPlane can observe about the object: reading the value back says what the host
    holds, not who can still talk to it."""

    GUARD_KEEP_REFUSED = "guard_keep_refused"
    """Somebody asked to keep a guarded change and the request could not do it."""

    GUARD_SETTLED = "guard_settled"
    """What became of the guard, collected from the component that was holding it."""

    RECOVERY_REQUIRED = "recovery_required"
    RUN_FINISHED = "run_finished"

    RECOVERY_RETRY_STARTED = "recovery_retry_started"
    """An operator asked LocalPlane to try again to reach the end state the Change wanted.

    After the Run finished. Everything from here on happened later than ``run_finished`` and
    the names say so — a transcript in which a recovery's dispatch borrowed
    ``mutation_dispatched`` would read as a second apply of the same plan."""

    RECOVERY_EVIDENCE_RESULT = "recovery_evidence_result"
    """What a fresh reading, taken before anything was written, made of the end state.

    The step that stops a retry from being a second write nobody needed: if this proves the
    original operation's required end state, recovery completes and no mutation occurs."""

    RECOVERY_CONFIRMATION_SATISFIED = "recovery_confirmation_satisfied"
    RECOVERY_CONFIRMATION_CONSUMED = "recovery_confirmation_consumed"
    RECOVERY_MUTATION_DISPATCHED = "recovery_mutation_dispatched"
    RECOVERY_MUTATION_RESULT = "recovery_mutation_result"
    RECOVERY_VERIFICATION_RESULT = "recovery_verification_result"
    RECOVERY_RETRY_FINISHED = "recovery_retry_finished"

    RECOVERY_RESOLVED = "recovery_resolved"
    """A person said they have dealt with the situation. No host was contacted to write."""

    RECOVERY_HOLD_RELEASED = "recovery_hold_released"
    """The object's durable write lock was given up, and by which of the two acts.

    Its own event rather than a detail on the other two, because it is the fact an operator
    most needs to find, it can arrive from either action, and a release recorded only inside
    the name of whatever happened to cause it is a release that is hard to audit."""


#: The two shapes a change can take, and the discriminator every record of one carries.
#: A **field** change moves one controlled value to another; an **action** asks for a
#: declared verb to be carried out and states what must hold afterwards. They are not the
#: same thing wearing different names — an action has no previous value, so it has nothing
#: to restore, and forcing one into the other's shape is how a model starts saying things
#: that are not true. The store CHECKs both halves against this.
CHANGE_KIND_FIELD = "field"
CHANGE_KIND_ACTION = "action"

#: What an action Run holds the object's write lock on. Not a controlled field — LocalPlane
#: retains no desired state for something it only acts on — but the aspect being serialised,
#: so that two Runs cannot be acting on one object's lifecycle at once. One value for every
#: action this build has; a future action about something other than a lifecycle would name
#: its own aspect rather than reuse this one.
ACTION_LOCK_ASPECT = "lifecycle"


@dataclass(frozen=True)
class MutationRequest:
    """What the engine is asking an executor to carry out. Two shapes, one discriminator.

    The engine assembles this from what it has already recorded and hands it over; it never
    learns what any of it means. ``correlation`` is the executor's own material — the
    identifier it needs to reach the target, and for an action the evidence a verification
    of that action will be judged against — and it is opaque here on purpose.
    """

    kind: str
    attempt_id: str
    correlation: Mapping[str, Any]

    expected_current: bool | int | None = None
    """The value the target is believed to hold. A field change only; it is the race guard
    the privileged path re-checks against reality before it writes."""

    desired: bool | int | None = None
    """The value to write. A field change only."""

    action: str | None = None
    """The declared verb to carry out. An action only, and it comes from the operation's own
    definition rather than from anything a caller supplied."""


@dataclass(frozen=True)
class Expectation:
    """What a fresh reading must show for an intended end state to be proven.

    Handed to the executor together with a reading, because *what counts as proof* belongs
    to the operation. Proving an MTU is a comparison of one integer; proving a restart is a
    comparison of a lifecycle state **and** of the instant the target was last started,
    because a container that is running now and was running then has not been shown to have
    restarted. The engine cannot know either of those and does not try to.
    """

    kind: str
    correlation: Mapping[str, Any]
    field: str | None = None
    value_type: str | None = None
    value: bool | int | None = None
    state: str | None = None


@dataclass(frozen=True)
class ProofOfState:
    """Whether a reading proved the expectation, and what it actually showed.

    ``outcome`` is the vocabulary the store already holds. ``value`` and ``state`` are the
    two shapes an observed answer comes in, and exactly one of them is populated — the same
    split the record itself keeps, so a reader is never guessing which column means anything.
    """

    outcome: VerificationOutcome
    value: bool | int | None = None
    state: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class MutationReport:
    """What the privileged path said about one dispatched mutation.

    A report, not a verdict. It says what the executor was told and by whom; whether the
    target now holds the wanted value is a separate question with a separate answer, and
    nothing here may be read as having answered it.
    """

    outcome: MutationOutcome
    reason: str
    attempt_id: str
    provider: str | None = None
    provider_version: str | None = None
    method: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)



@dataclass(frozen=True)
class ExecutionRefused(Exception):
    """The executor could not build a mutation at all, and nothing was dispatched.

    Distinct from a mutation that came back ``not_written``: this one never reached the
    privileged path, so it happens *before* the boundary and leaves no Change behind.
    """

    code: str
    detail: dict[str, Any] = field(default_factory=dict)
