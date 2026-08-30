"""Comparing retained intent against what was last observed.

Reconciliation is a *fact*, not an opinion: two values were read and they either agree or
they do not. It exists only for managed objects, because an object with no retained intent
has nothing to disagree with — its reconciliation is ``None``, which is a different answer
from ``in_sync``.

Three rules decide what comes out:

* **The comparison is field-scoped and typed.** Only fields the intent actually controls
  are looked at. Nothing else in the observation participates, however tempting: a change
  in ``carrier`` is the host doing its job, and calling it drift would make LocalPlane cry
  wolf about an unplugged cable.
* **An unreadable field is unknown, never drift.** A value LocalPlane could not read is not
  evidence that the value changed. Manufacturing drift out of a failed read is the single
  most damaging thing a control plane can do here, because it is indistinguishable from
  the real thing until somebody investigates.
* **A proven disagreement outranks an unreadable field.** If ``mtu`` demonstrably differs
  while ``admin_up`` could not be read, the object *is* drifted: the disagreement was
  observed, and a second field nobody could read does not unprove it. The reverse does not
  hold — nothing that could not be read may be reported ``in_sync``.

So the precedence is ``drifted`` > ``unknown`` > ``in_sync``.

``applying`` is part of the vocabulary and is never returned here. Nothing applies yet, and
a state produced by an engine that does not exist would be a claim about work that is not
happening.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Sequence

from localplane.backend.domain.intent import ValueType, coerce
from localplane.backend.domain.states import ReconciliationState


class Comparison(StrEnum):
    """The outcome for one controlled field."""

    MATCHES = "matches"
    DIFFERS = "differs"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class IntendedField:
    field: str
    value_type: ValueType
    value: bool | int


@dataclass(frozen=True)
class ObservedSnapshot:
    """The newest observation, reduced to what a comparison needs."""

    observation_id: str
    sweep_id: str
    observed_at: str
    capability: str
    provider: str
    facts: dict[str, Any]


@dataclass(frozen=True)
class FieldComparison:
    """One typed, field-scoped verdict, carrying the evidence it rests on."""

    field: str
    value_type: ValueType
    intended: bool | int
    observed: bool | int | None
    comparison: Comparison
    reason: str


@dataclass(frozen=True)
class Reconciliation:
    state: ReconciliationState
    reason: str
    fields: tuple[FieldComparison, ...]
    observation_id: str | None
    sweep_id: str | None
    observed_at: str | None

    @property
    def drifted_fields(self) -> tuple[FieldComparison, ...]:
        return tuple(f for f in self.fields if f.comparison is Comparison.DIFFERS)


def reconcile(
    *,
    intent_capability: str,
    intent_provider: str,
    intended: Sequence[IntendedField],
    observation: ObservedSnapshot | None,
) -> Reconciliation:
    """Compare an object's active intent with its newest observation.

    An observation from a different provider is not compared at all. The same field name
    read from a different source is not necessarily the same fact, and a comparison across
    that boundary would produce drift that says more about LocalPlane's plumbing than about
    the host. Provider *version* is allowed to differ: that is the same contract, revised.
    """
    if observation is None:
        return Reconciliation(
            state=ReconciliationState.UNKNOWN,
            reason="no_observation",
            fields=tuple(
                FieldComparison(
                    field=i.field,
                    value_type=i.value_type,
                    intended=i.value,
                    observed=None,
                    comparison=Comparison.UNKNOWN,
                    reason="no_observation",
                )
                for i in intended
            ),
            observation_id=None,
            sweep_id=None,
            observed_at=None,
        )

    if (
        observation.capability != intent_capability
        or observation.provider != intent_provider
    ):
        return Reconciliation(
            state=ReconciliationState.UNKNOWN,
            reason="observation_source_incompatible",
            fields=tuple(
                FieldComparison(
                    field=i.field,
                    value_type=i.value_type,
                    intended=i.value,
                    observed=None,
                    comparison=Comparison.UNKNOWN,
                    reason="observation_source_incompatible",
                )
                for i in intended
            ),
            observation_id=observation.observation_id,
            sweep_id=observation.sweep_id,
            observed_at=observation.observed_at,
        )

    comparisons = tuple(compare_field(i, observation.facts) for i in intended)

    if not comparisons:
        # An intent that controls nothing cannot be written: adoption refuses an empty
        # capture. Reaching this would mean the store holds an intent no build can read,
        # and saying so is better than reporting agreement with nothing.
        state, reason = ReconciliationState.UNKNOWN, "intent_controls_no_fields"
    elif any(c.comparison is Comparison.DIFFERS for c in comparisons):
        state, reason = ReconciliationState.DRIFTED, "controlled_field_differs"
    elif any(c.comparison is Comparison.UNKNOWN for c in comparisons):
        state, reason = ReconciliationState.UNKNOWN, "controlled_field_not_observable"
    else:
        state, reason = ReconciliationState.IN_SYNC, "controlled_fields_match"

    return Reconciliation(
        state=state,
        reason=reason,
        fields=comparisons,
        observation_id=observation.observation_id,
        sweep_id=observation.sweep_id,
        observed_at=observation.observed_at,
    )


def compare_field(intended: IntendedField, facts: dict[str, Any]) -> FieldComparison:
    """Compare one controlled field against the observed facts."""
    raw = facts.get(intended.field)
    if raw is None:
        return FieldComparison(
            field=intended.field,
            value_type=intended.value_type,
            intended=intended.value,
            observed=None,
            comparison=Comparison.UNKNOWN,
            reason="observed_value_unreadable",
        )

    observed = coerce(intended.value_type, raw)
    if observed is None:
        # The source produced something, but not something of this field's type. That is a
        # provider fault, and it is not drift: LocalPlane still does not know the value.
        return FieldComparison(
            field=intended.field,
            value_type=intended.value_type,
            intended=intended.value,
            observed=None,
            comparison=Comparison.UNKNOWN,
            reason="observed_value_not_usable",
        )

    matches = observed == intended.value
    return FieldComparison(
        field=intended.field,
        value_type=intended.value_type,
        intended=intended.value,
        observed=observed,
        comparison=Comparison.MATCHES if matches else Comparison.DIFFERS,
        reason="observed_value_matches" if matches else "observed_value_differs",
    )
