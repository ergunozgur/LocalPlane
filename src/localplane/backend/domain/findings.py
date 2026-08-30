"""Findings: the claims LocalPlane makes, as distinct from the states it derives.

A finding and a state are not the same kind of thing:

* ``reconciliation = drifted`` is a **comparison**. It is recomputed from the active intent
  and the newest observation every time anybody asks, and it has no memory.
* a **finding** is the durable record that LocalPlane noticed something, when it first
  noticed, what evidence it held, and how it ended. It is an interpretation, and it is
  allowed to be argued with.

The distinction is what lets both be honest. A drift that has been open for six days is one
finding with a first-seen time, not six days of identical alerts; and a finding is closed
because the evidence says it ended, not because a state column happened to be recomputed.

Identity is deterministic — the same disagreement about the same field of the same object
always maps to the same :func:`finding_key`, which is what makes "seen again" an update
rather than a new row. The row *id* is per episode: a finding that resolved and later
recurred is a new episode, so its first-seen time is the truth about this occurrence and
the earlier one survives as history.
"""

from __future__ import annotations

from enum import StrEnum
from hashlib import sha256

from localplane.backend.domain.intent import ValueType

FINDING_TYPE_INTERFACE_DRIFT = "network.interface.drift"

FINDING_TYPE_OWNERSHIP_CONFLICT = "network.interface.ownership_conflict"
"""LocalPlane retains intent for an object another system is demonstrably running.

Raised only for *managed* objects. An observed object owned by Docker is not a finding —
nothing is wrong, LocalPlane is simply watching something that belongs to somebody else,
and adoption is refused before it can become a claim. What makes this one worth recording
is that LocalPlane has already said it is answerable for the object, and that has stopped
being safe.

Ownership evidence never releases the object. Retained intent is an operator's decision and
a sweep does not overturn it; what a sweep can do is say, durably and with the evidence,
that the decision no longer holds.
"""


class FindingStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class FindingResolution(StrEnum):
    """Why a finding stopped being true. All three are outcomes, never deletions."""

    OBSERVED_MATCHES_INTENT = "observed_matches_intent"
    """An observation showed the controlled value agreeing again."""

    INTENT_REVISED = "intent_revised"
    """The operator revised the retained intent, and the disagreement went with it.

    Never used for a field whose intended value this revision left alone. If a value came
    back on its own while the operator was revising a different field, that is
    :attr:`OBSERVED_MATCHES_INTENT` and it names the observation that proved it — the two
    are different endings and only one of them is about the host.

    Nothing was applied. The schema keeps this honest: only ``observed_matches_intent`` may
    name a resolving observation, so this resolution structurally cannot claim the runtime
    was read and found to have been put right.
    """

    INTENT_RELEASED = "intent_released"
    """The object was released. There is no intent left for anything to disagree with."""


class OwnershipResolution(StrEnum):
    """Why an ownership conflict stopped being true."""

    OWNER_NO_LONGER_CLAIMS = "owner_no_longer_claims"
    """The provider was consulted again and no longer claims the object.

    Requires the provider to have actually answered. One that could not be read proves
    nothing, and a conflict is never closed on silence.
    """

    INTENT_RELEASED = "intent_released"
    """The object was released. LocalPlane is no longer answerable for it, so there is
    nothing left for another system's ownership to conflict with."""


def finding_key(host_id: str, object_id: str, finding_type: str, subject: str) -> str:
    """The stable logical identity of a claim, independent of when it was made."""
    digest = sha256(f"{host_id}|{object_id}|{finding_type}|{subject}".encode("utf-8")).hexdigest()
    return f"fkey_{digest[:32]}"


def render_value(value_type: ValueType, value: bool | int | None) -> str:
    if value is None:
        return "unknown"
    if value_type is ValueType.BOOLEAN:
        return "true" if value else "false"
    return str(value)


def drift_summary(
    display_name: str,
    subject: str,
    value_type: ValueType,
    intended: bool | int,
    observed: bool | int | None,
) -> str:
    """A sentence for a drift finding, derived from the typed evidence, never stored.

    Derived rather than persisted so it cannot describe evidence the row no longer holds.
    The typed columns are the claim; this is a rendering of them.
    """
    want = render_value(value_type, intended)
    if observed is None:
        return (
            f"{display_name}: {subject} could not be read at the last observation, "
            f"so LocalPlane cannot confirm it still differs from the intended {want}"
        )
    return (
        f"{display_name}: {subject} is {render_value(value_type, observed)} on the host, "
        f"and the retained intent says {want}"
    )


def ownership_conflict_summary(
    display_name: str,
    relation: str,
    owner_provider: str,
    owner_label: str | None,
    confidence: str,
) -> str:
    """A sentence for an ownership conflict, derived from the typed evidence, never stored.

    Says what LocalPlane holds and what the other system does, in that order, because the
    conflict is between the two and not a property of either.
    """
    owner = f"{owner_provider} ({owner_label})" if owner_label else owner_provider
    verb = "created" if relation == "created_by" else "is configuring"
    return (
        f"{display_name}: LocalPlane retains intent for this interface, and {owner} "
        f"{verb} it ({confidence})"
    )
