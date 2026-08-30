"""The read side of ownership: evidence out of the store, judgement out of the domain.

Nothing is cached and nothing is stored. Every answer here is computed from the newest
provider readings and the newest observation of the object, at the moment it is asked for —
the same discipline freshness and reconciliation are held to, and for the same reason: a
recorded ownership verdict would be a second copy of something that moves whenever either
input moves, and the copy is what an operator would end up reading.

The service is deliberately thin. Correlating a Docker network with a kernel link, deciding
what NetworkManager's posture towards a device amounts to, and refusing to conclude
anything from a name are all in :mod:`localplane.backend.domain.provenance`, where they are
pure functions with no database behind them.
"""

from __future__ import annotations

from localplane.backend.db.database import Database
from localplane.backend.db.repositories import ObjectRecord, ProviderObservationRepository
from localplane.backend.domain.identity import OBJECT_KIND_DOCKER_CONTAINER
from localplane.backend.domain.provenance import (
    AdoptionEligibility,
    OwnershipState,
    Provenance,
    ProviderEvidence,
    derive_adoption_eligibility,
    derive_container_provenance,
    derive_provenance,
)


class ProvenanceService:
    """Ownership and adoption eligibility for observed objects."""

    def __init__(self, database: Database) -> None:
        self.readings = ProviderObservationRepository(database)

    def evidence(self, host_id: str) -> ProviderEvidence:
        """The newest reading from each provider on this host."""
        return ProviderEvidence.of(self.readings.latest(host_id))

    def for_object(
        self, record: ObjectRecord, evidence: ProviderEvidence | None = None
    ) -> Provenance:
        """What is known about who owns ``record``.

        ``evidence`` is passed in when a caller is assessing many objects at once, so a
        list view reads the provider tables once rather than once per interface. It is the
        same evidence either way.
        """
        if record.observation is None:
            # There is nothing to correlate a provider record against. Not "nobody owns
            # it" — nobody has looked at it.
            return Provenance(OwnershipState.UNKNOWN, "no_observation")
        if evidence is None:
            evidence = self.evidence(record.host_id)
        # Dispatched on the kind of resource, because the *question* is the same and the
        # evidence for it is not. A link's owner has to be correlated from what several
        # daemons declare; a container's owner is the daemon that reported it, so the
        # observation is the evidence and there is nothing to join.
        if record.kind == OBJECT_KIND_DOCKER_CONTAINER:
            return derive_container_provenance(record.observation.facts, evidence)
        return derive_provenance(record.observation.facts, evidence)

    def eligibility(
        self, record: ObjectRecord, provenance: Provenance | None = None
    ) -> AdoptionEligibility:
        """Whether LocalPlane would take responsibility for ``record`` right now."""
        if provenance is None:
            provenance = self.for_object(record)
        return derive_adoption_eligibility(record.management_state, provenance)
