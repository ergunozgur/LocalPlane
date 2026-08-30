"""The state axes.

These are four independent axes, and collapsing any two of them is how a control plane
starts lying:

* **Management** is LocalPlane's stance towards an object. Three values, no more.
  ``adopt`` and ``release`` are transitions between them, not states.
* **Reconciliation** compares an object against retained intent. It applies *only* to
  managed objects. An observed object has no intent to disagree with, so it does not
  drift — its reconciliation is ``None``, which is different from ``in_sync``.
* **Health** is how the object is doing. A managed object can be failed; an observed one
  can be healthy. Management says nothing about health and health says nothing about
  management.
* **Freshness** is how recently LocalPlane looked. It is derived from the observation
  timestamp at read time and never stored, because a stored freshness is stale by
  definition — that is exactly the duplicated-truth bug it would create.

``unknown`` is a real answer everywhere it appears and is never substituted for a
plausible one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum


class ManagementState(StrEnum):
    """LocalPlane's stance towards an object."""

    OBSERVE_ONLY = "observe_only"
    """Read at every observation, never written. Adoption is not offered."""

    OBSERVED = "observed"
    """Known and readable, with no retained intent. A candidate for adoption."""

    MANAGED = "managed"
    """Intent is retained for this object, and LocalPlane is answerable for it."""


class ReconciliationState(StrEnum):
    """How a managed object compares with its retained intent."""

    IN_SYNC = "in_sync"
    DRIFTED = "drifted"
    APPLYING = "applying"
    UNKNOWN = "unknown"


class HealthState(StrEnum):
    """How the object is doing, independently of who manages it."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


class Freshness(StrEnum):
    """How recently LocalPlane looked at the object."""

    CURRENT = "current"
    STALE = "stale"
    NEVER_OBSERVED = "never_observed"


DEFAULT_FRESHNESS_TTL_S = 60.0


def derive_freshness(
    observed_at: datetime | None,
    ttl_s: float = DEFAULT_FRESHNESS_TTL_S,
    now: datetime | None = None,
) -> tuple[Freshness, float | None]:
    """Return ``(freshness, age_seconds)`` for an observation timestamp.

    A future timestamp yields a negative age and still counts as current: clock skew
    between the agent and the backend is a real condition, and pretending an observation
    is stale because of it would be its own falsehood.
    """
    if observed_at is None:
        return Freshness.NEVER_OBSERVED, None
    now = now or datetime.now(timezone.utc)
    age = (now - observed_at).total_seconds()
    return (Freshness.CURRENT if age <= ttl_s else Freshness.STALE), age
