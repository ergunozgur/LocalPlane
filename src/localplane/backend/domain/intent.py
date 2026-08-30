"""What LocalPlane is prepared to intend.

Adoption records the *currently verified* value of the fields LocalPlane understands well
enough to be answerable for. It is not a snapshot of the observation: an observation is
full of things that are true and not intendable — what the carrier is doing, how many
packets went past, how recently anyone looked. Serialising those as desired state would
manufacture intent nobody expressed, and would then produce drift the moment the host did
something perfectly normal.

**The adoptable set is two fields, and the bar for entry is deliberately high.** A field
qualifies only when all four hold:

* it is *writable* — there is a link-layer operation that sets it, so intending it could
  one day mean something;
* it is *not an outcome* — the kernel does not change it on its own in response to the
  world. ``carrier`` and ``operstate`` fail here: they are the link reporting what happened
  to it;
* it is *scalar and typed*, so two values can be compared without a normalisation policy
  that would itself need to be justified;
* it is *directly observed* by the current provider, so "unknown" is distinguishable from
  a value.

``admin_up`` and ``mtu`` pass. What was considered and left out:

``addresses``
    Writable, and the obvious next candidate — but a DHCP lease is not a desired address,
    and an intent that captured one would report drift at the next renewal. Addresses need
    a model that separates a configured address from an acquired one, and the source that
    would say which is the provider that configures them. There is no such provider yet.
``mac_address``
    Writable on most drivers, but it is identity-bearing here: ``permanent_mac`` is the
    strongest basis :mod:`~localplane.backend.domain.identity` has, and an intent that
    could move it would let LocalPlane intend an object into being a different object.
``master``
    Bridge and bond membership is writable, but the observed value is a kernel *name*,
    which is the least durable identity LocalPlane has. Intending it needs the topology to
    be modelled as relationships between object ids.
``speed_mbps``, ``duplex``
    Negotiated with the link partner. Settable through ethtool, but what is observed is the
    result of a negotiation, not the setting — the two are not the same field.
``operstate``, ``carrier``, ``carrier_changes``, ``statistics``, ``ifindex``, ``name``,
``link_kind``, ``arphrd_type``, ``is_physical``, ``device_path``
    Not writable, or assigned by the kernel, or transient. None of them are intent.

Growing this set is a schema decision — :data:`INTENT_SCHEMA_VERSION` moves with it — and
each addition has to answer the four questions above on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Sequence

#: The shape of a captured field set. Stored on every intent so an intent written by a
#: build that understood a different set of fields is recognisable rather than silently
#: compared against.
INTENT_SCHEMA_VERSION = 1


class ValueType(StrEnum):
    """The types an intended value may have. Both encode as integers in the store."""

    BOOLEAN = "boolean"
    INTEGER = "integer"


@dataclass(frozen=True)
class AdoptableField:
    name: str
    value_type: ValueType


ADOPTABLE_INTERFACE_FIELDS: tuple[AdoptableField, ...] = (
    AdoptableField("admin_up", ValueType.BOOLEAN),
    AdoptableField("mtu", ValueType.INTEGER),
)

ADOPTABLE_INTERFACE_FIELDS_BY_NAME: dict[str, AdoptableField] = {
    f.name: f for f in ADOPTABLE_INTERFACE_FIELDS
}


@dataclass(frozen=True)
class CapturedField:
    """One verified value, ready to become intent."""

    field: str
    value_type: ValueType
    value: bool | int


@dataclass(frozen=True)
class UncapturableField:
    """A controlled field whose current value cannot honestly be turned into intent."""

    field: str
    value_type: ValueType
    reason: str
    observed: Any = None


@dataclass(frozen=True)
class Capture:
    """The result of trying to read intent out of an observation.

    Either every adoptable field yielded a verified value, or the capture is refused. There
    is no partial adoption: an intent that controls one field because the other could not
    be read would silently narrow what LocalPlane is answerable for, and nothing downstream
    would show that it had been narrowed.
    """

    fields: tuple[CapturedField, ...]
    uncapturable: tuple[UncapturableField, ...]

    @property
    def complete(self) -> bool:
        return not self.uncapturable


def capture_interface_intent(facts: dict[str, Any]) -> Capture:
    """Read the adoptable fields out of one observation's normalized facts.

    A field that is absent, ``None``, of the wrong type or outside the range the kernel
    could have produced is *not* captured. Nothing is defaulted and nothing is guessed —
    an unreadable ``flags`` file means LocalPlane does not know whether the link is up, and
    adopting "up" because it is the usual answer would be inventing the operator's
    intention for them.
    """
    captured: list[CapturedField] = []
    uncapturable: list[UncapturableField] = []

    for field in ADOPTABLE_INTERFACE_FIELDS:
        raw = facts.get(field.name)
        if raw is None:
            uncapturable.append(
                UncapturableField(field.name, field.value_type, "observed_value_unreadable")
            )
            continue
        value = coerce(field.value_type, raw)
        if value is None:
            uncapturable.append(
                UncapturableField(
                    field.name, field.value_type, "observed_value_not_usable", observed=raw
                )
            )
            continue
        captured.append(CapturedField(field.name, field.value_type, value))

    return Capture(tuple(captured), tuple(uncapturable))


def coerce(value_type: ValueType, raw: Any) -> bool | int | None:
    """Return ``raw`` as ``value_type``, or ``None`` if it is not that type.

    ``bool`` is deliberately rejected where an integer is wanted. In Python ``True`` is an
    ``int``, so a plain ``isinstance`` check would accept an admin-state flag as an MTU.
    """
    if value_type is ValueType.BOOLEAN:
        return raw if isinstance(raw, bool) else None
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    # An MTU the kernel reported is a positive integer. Zero and negatives are not values
    # a link can have; treating them as intent would put an unreachable target in the store.
    return raw if raw > 0 else None


def encode(value_type: ValueType, value: bool | int) -> int:
    return int(value) if value_type is ValueType.BOOLEAN else int(value)


def decode(value_type: ValueType, stored: int) -> bool | int:
    return bool(stored) if value_type is ValueType.BOOLEAN else int(stored)


# ------------------------------------------------------------------------------ revision

#: The value :data:`intents.origin` holds on every row. 0002 CHECKed that column to a
#: single value and SQLite cannot widen a CHECK without rebuilding the table, so the column
#: is frozen and is not the answer to "how did this version come to exist". The events are:
#: an ``adopt`` row in ``management_transitions``, or a row in ``intent_revisions``. See
#: migration 0004, and :class:`~localplane.backend.db.repositories.IntentRepository`, which
#: derives the origin it reports so that nothing above the store reads the frozen column.
STORED_INTENT_ORIGIN = "adopt"


class RevisionKind(StrEnum):
    """The two ways a managed object's retained intent may be revised.

    They are kept apart because they are different acts, and a history that could not tell
    them apart afterwards would be unauditable. ``revise`` is the operator stating a value
    they want; ``adopt_runtime`` is the operator stating that what is on the host right now
    is what they wanted all along. The first is a claim about the future and needs no
    agreement from the machine. The second is a claim about the present, and is only
    honest if the present was actually read — which is why it refuses on an unverifiable
    observation and an explicit revision does not.
    """

    EXPLICIT = "revise"
    RUNTIME_ADOPTION = "adopt_runtime"


@dataclass(frozen=True)
class FieldChange:
    """One controlled value moving from what was intended to what is now intended."""

    field: str
    value_type: ValueType
    was: bool | int
    now: bool | int


@dataclass(frozen=True)
class RevisionPlan:
    """The complete controlled field set a revision would retain, and how it got there.

    ``fields`` is the whole set, never a delta. A revision replaces one immutable version
    with another, and a version that recorded only what moved would leave the rest of the
    desired state to be reassembled by walking the chain — which is how "what does
    LocalPlane want" becomes a question with more than one answer.
    """

    kind: RevisionKind
    fields: tuple[CapturedField, ...]
    changed: tuple[FieldChange, ...]
    carried_forward: tuple[str, ...]
    """Controlled fields the operator did not name, kept at their existing intended value.

    Not the same as "unchanged": an operator who restates a value they are already
    intending has named it, and that is worth being able to see afterwards.
    """


@dataclass(frozen=True)
class RevisionRefused:
    """Why a revision cannot be planned. Distinct codes, because the remedies differ."""

    code: str
    detail: dict[str, Any]


def plan_explicit_revision(
    current: Sequence[CapturedField], requested: Mapping[str, Any]
) -> RevisionPlan | RevisionRefused:
    """Plan a revision from values the operator supplied.

    Every requested field is checked against two things and refused rather than dropped if
    either fails: it must be a field LocalPlane understands at all, and it must be one the
    active intent already controls. The second is what stops a revision from quietly
    widening what LocalPlane is answerable for — growing the controlled set is an adoption
    decision made against verified evidence, not something a request body may do.

    Fields the operator did not name keep the value they had. Silently dropping them would
    narrow the intent, and nothing downstream would show that it had been narrowed.
    """
    controlled = {f.field: f for f in current}
    if not requested:
        return RevisionRefused(
            "empty_revision",
            {
                "message": "no desired value was supplied",
                "controlled_fields": sorted(controlled),
            },
        )

    planned: dict[str, CapturedField] = dict(controlled)
    changed: list[FieldChange] = []
    for field, raw in requested.items():
        known = ADOPTABLE_INTERFACE_FIELDS_BY_NAME.get(field)
        if known is None:
            return RevisionRefused(
                "unsupported_field",
                {
                    "field": field,
                    "supported_fields": [f.name for f in ADOPTABLE_INTERFACE_FIELDS],
                },
            )
        existing = controlled.get(field)
        if existing is None:
            return RevisionRefused(
                "field_not_controlled",
                {"field": field, "controlled_fields": sorted(controlled)},
            )
        value = coerce(existing.value_type, raw)
        if value is None:
            return RevisionRefused(
                "invalid_field_value",
                {
                    "field": field,
                    "expected_type": str(existing.value_type),
                    "supplied": raw,
                },
            )
        planned[field] = CapturedField(field, existing.value_type, value)
        if value != existing.value:
            changed.append(FieldChange(field, existing.value_type, existing.value, value))

    if not changed:
        return RevisionRefused(
            "revision_changes_nothing",
            {
                "message": "every supplied value is already the intended one",
                "intended": {f.field: f.value for f in current},
            },
        )
    return RevisionPlan(
        kind=RevisionKind.EXPLICIT,
        fields=tuple(planned[f.field] for f in current),
        changed=tuple(changed),
        carried_forward=tuple(f.field for f in current if f.field not in requested),
    )


def plan_runtime_revision(
    current: Sequence[CapturedField], capture: Capture
) -> RevisionPlan | RevisionRefused:
    """Plan a revision that makes the currently verified runtime the desired state.

    Only the fields the active intent already controls are taken, and only where the
    observation produced a usable value. A controlled field that could not be read refuses
    the whole revision: "adopt what is there" is a statement about what is there, and it
    cannot be made about a value nobody could see. Nothing is defaulted, and a field the
    observation happens to carry that this intent does not control is left alone.
    """
    controlled = {f.field: f for f in current}
    observed = {f.field: f for f in capture.fields}

    missing = [f for f in capture.uncapturable if f.field in controlled]
    # A field the capture explicitly could not read is already named above. This catches
    # the other case: one the capture did not consider at all, which is what an intent
    # written against a wider field set than this build understands would produce.
    named = {f.field for f in missing}
    unseen = [name for name in controlled if name not in observed and name not in named]
    if missing or unseen:
        return RevisionRefused(
            "controlled_values_unverified",
            {
                "fields": [
                    {
                        "field": f.field,
                        "value_type": str(f.value_type),
                        "reason": f.reason,
                        "observed": f.observed,
                    }
                    for f in missing
                ]
                + [
                    {
                        "field": name,
                        "value_type": str(controlled[name].value_type),
                        "reason": "observed_value_unreadable",
                        "observed": None,
                    }
                    for name in unseen
                ],
            },
        )

    changed = [
        FieldChange(name, field.value_type, field.value, observed[name].value)
        for name, field in controlled.items()
        if observed[name].value != field.value
    ]
    if not changed:
        return RevisionRefused(
            "revision_changes_nothing",
            {
                "message": "the observed runtime is already the intended state",
                "intended": {f.field: f.value for f in current},
            },
        )
    return RevisionPlan(
        kind=RevisionKind.RUNTIME_ADOPTION,
        fields=tuple(observed[f.field] for f in current),
        changed=tuple(changed),
        # Nothing is carried forward: every value here was read from the observation,
        # including the ones that happen to match what was already intended.
        carried_forward=(),
    )
