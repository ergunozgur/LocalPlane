"""Adopt, release, revise, and the claims that follow from them.

This is where LocalPlane changes its own mind about an object, and nowhere else. Two of the
four are transitions on the management axis:

* **Adopt** — ``observed → managed``. LocalPlane reads the fields it is prepared to be
  answerable for out of a current observation and retains them as a new intent version.
* **Release** — ``managed → observed``. LocalPlane stops pointing at the intent it held.
  Every version stays in the store; what ends is the claim, not the history.

The other two are not. **Revision** starts and ends at ``managed``: what moves is which
version of its own intent the object points at, and the management state is untouched.

* **Revise** — the operator supplies a new desired value for a field LocalPlane already
  controls. Fields they do not name keep the value they had.
* **Adopt the runtime** — the operator declares that what is currently observed is what
  they wanted. Only currently verified values are taken, and a controlled field nobody
  could read refuses the whole thing.

Both write a new immutable intent version and move ``objects.active_intent_id`` to it. The
version they replace is untouched and stays readable forever, and the second truthful
answer to drift — "the machine is right, my declaration was out of date" — becomes
available without anything being applied.

**None of the four touches the host.** Adopt is not "make it so": the values it records are
the values that were already there, which is why adopting can never break anything and why
it needs no rollback. Release is not a rollback either — it does not put anything back,
because nothing was ever written. An object released while its link is down stays down. And
a revision moves a claim, not an interface: an MTU intended at 1400 is still an MTU nobody
has set.

Everything here that writes does so inside one transaction, and the schema is the thing
enforcing that the result is coherent: a managed object without an active intent is not a
state this code has to avoid producing, it is a state the database will not accept.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from localplane.backend.db.database import Database
from localplane.backend.db.repositories import (
    FindingRecord,
    FindingRepository,
    IntentRecord,
    IntentRepository,
    ObjectRecord,
    ObjectRepository,
    ObservationRecord,
    OwnershipFindingRepository,
)
from localplane.backend.domain.findings import (
    FINDING_TYPE_INTERFACE_DRIFT,
    FINDING_TYPE_OWNERSHIP_CONFLICT,
    FindingResolution,
    OwnershipResolution,
    finding_key,
)
from localplane.backend.domain.intent import (
    INTENT_SCHEMA_VERSION,
    Capture,
    CapturedField,
    RevisionKind,
    RevisionPlan,
    RevisionRefused,
    ValueType,
    capture_interface_intent,
    encode,
    plan_explicit_revision,
    plan_runtime_revision,
)
from localplane.backend.domain.network import InterfaceSignals, classify_management
from localplane.backend.domain.provenance import (
    OwnershipRelation,
    Provenance,
    ProviderEvidence,
    ownership_block,
)
from localplane.backend.domain.reconciliation import (
    Comparison,
    IntendedField,
    ObservedSnapshot,
    Reconciliation,
    reconcile,
)
from localplane.backend.domain.states import Freshness, ManagementState, derive_freshness
from localplane.backend.provenance import ProvenanceService
from localplane.protocol.providers import ProviderStatus

LOG = logging.getLogger("localplane.backend.management")

#: Recorded as the object's management reason once it is managed. The classification
#: reason ("management_candidate") described why it *could* be adopted; this says why it
#: is managed, which is a different sentence.
ADOPTED_REASON = "adopted"
RELEASED_REASON = "released"

#: One sentence per way a revision can be refused before it is planned. The domain returns
#: a code and the typed detail behind it; the prose lives here, next to the other refusals,
#: so that a caller sees the same shape whatever declined the request.
_REVISION_REFUSALS: dict[str, str] = {
    "empty_revision": (
        "a revision has to say what is now wanted; there is no desired value in this "
        "request to retain"
    ),
    "unsupported_field": (
        "this is not a field LocalPlane can intend, and it will not be silently ignored — "
        "the supported set is the one adoption captures"
    ),
    "field_not_controlled": (
        "the active intent does not control this field, and a revision may not start "
        "controlling it — that is an adoption decision, made against verified evidence"
    ),
    "invalid_field_value": (
        "the value supplied is not of this field's type; an admin state is not an MTU and "
        "neither is coerced into the other"
    ),
    "revision_changes_nothing": (
        "this revision would retain exactly what is already retained; a version that "
        "records no decision would make the version chain say less, not more"
    ),
    "controlled_values_unverified": (
        "a field LocalPlane controls could not be read from the newest observation, so the "
        "runtime cannot be adopted as the desired state"
    ),
}


class ManagementRefused(RuntimeError):
    """A transition was refused, with a reason a caller can branch on.

    Refusal is structured for the same reason agent failure is: "this object is watch-only"
    and "nobody has observed it recently enough" are different conditions with different
    fixes, and a caller that cannot tell them apart will offer the wrong remedy.
    """

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


@dataclass(frozen=True)
class TransitionOutcome:
    """What a transition did — to LocalPlane, and (always) not to the host."""

    transition: str
    object_id: str
    host_id: str
    from_state: str
    to_state: str
    intent: IntentRecord
    reconciliation: Reconciliation | None
    transition_id: str
    occurred_at: str
    provenance: Provenance | None = None
    host_effect: str = "none"


@dataclass(frozen=True)
class RevisionOutcome:
    """What a revision did — to LocalPlane's retained intent, and to nothing else."""

    kind: RevisionKind
    object_id: str
    host_id: str
    management_state: str
    management_reason: str
    previous_intent: IntentRecord
    intent: IntentRecord
    plan: RevisionPlan
    reconciliation: Reconciliation
    findings_resolved: tuple[FindingRecord, ...]
    findings_opened: tuple[FindingRecord, ...]
    provenance: Provenance
    revision_id: str
    occurred_at: str
    host_effect: str = "none"


class ManagementService:
    """Owns the management axis: what is adopted, what is released, and what follows."""

    def __init__(
        self,
        database: Database,
        freshness_ttl_s: float,
        provenance: ProvenanceService | None = None,
    ) -> None:
        self._db = database
        self._freshness_ttl_s = freshness_ttl_s
        self.objects = ObjectRepository(database)
        self.intents = IntentRepository(database)
        self.findings = FindingRepository(database)
        self.ownership_findings = OwnershipFindingRepository(database)
        self.provenance = provenance or ProvenanceService(database)

    # ------------------------------------------------------------------------- adopt

    def adopt(self, record: ObjectRecord) -> TransitionOutcome:
        """Retain the currently verified writable state of ``record`` as intent.

        Refuses rather than guesses, and every refusal names a condition with its own
        remedy: a loopback device will never be adoptable, a stale observation only needs
        somebody to look again.
        """
        now = _now()
        # Refuse the ordinary cases before taking the write lock, so that being told
        # "loopback cannot be adopted" costs nothing and rolls nothing back.
        self._verify_adoptable(record)

        intent_id = f"int_{uuid.uuid4().hex}"
        transition_id = f"trn_{uuid.uuid4().hex}"

        with self._db.transaction():
            # Re-read and re-verify under the write lock. Between the check above and this
            # line another adopt may have landed, or a sweep may have replaced the
            # observation this intent would be taken from — and intent has to be the
            # values verified *now*, not the ones verified a moment ago. A refusal here
            # means something raced, which is exactly when it is worth being loud.
            current = self.objects.get(record.object_id)
            if current is None:
                raise ManagementRefused(
                    "object_not_found",
                    "the object disappeared while it was being adopted",
                    {"object_id": record.object_id},
                )
            observation, capture, provenance = self._verify_adoptable(current)

            version = self.intents.next_version(current.object_id)
            previous = self.intents.history(current.object_id)
            self.intents.insert(
                intent_id=intent_id,
                object_id=current.object_id,
                host_id=current.host_id,
                version=version,
                supersedes=previous[0].intent_id if previous else None,
                schema_version=INTENT_SCHEMA_VERSION,
                capability=observation.capability,
                provider=observation.provider,
                provider_version=observation.provider_version,
                observation_id=observation.observation_id,
                sweep_id=observation.sweep_id,
                observed_at=observation.observed_at,
                created_at=now,
                fields=[
                    (f.field, str(f.value_type), encode(f.value_type, f.value))
                    for f in capture.fields
                ],
            )
            self.objects.set_managed(current.object_id, intent_id, ADOPTED_REASON)
            self.intents.record_transition(
                transition_id=transition_id,
                object_id=current.object_id,
                host_id=current.host_id,
                transition="adopt",
                from_state=ManagementState.OBSERVED,
                to_state=ManagementState.MANAGED,
                intent_id=intent_id,
                observation_id=observation.observation_id,
                occurred_at=now,
            )
            intent = self.intents.get(intent_id)
            assert intent is not None  # written in this transaction
            # Derived immediately: an operator should not have to wait for the next sweep
            # to be told what LocalPlane now makes of the object. It is in_sync by
            # construction — the intent was taken from this very observation — but it is
            # computed rather than asserted, so a bug in the comparison shows up here.
            reconciliation = self._evaluate(current, intent, now)

        LOG.info(
            "object adopted",
            extra={
                "object_id": current.object_id,
                "host_id": current.host_id,
                "intent_id": intent_id,
                "intent_version": version,
                "fields": [f.field for f in capture.fields],
                "observation_id": observation.observation_id,
                "reconciliation": str(reconciliation.state),
                "ownership": str(provenance.state),
                "ownership_reason": provenance.reason,
                "evidence_gaps": list(provenance.gaps),
                "host_effect": "none",
            },
        )
        return TransitionOutcome(
            transition="adopt",
            object_id=current.object_id,
            host_id=current.host_id,
            from_state=str(ManagementState.OBSERVED),
            to_state=str(ManagementState.MANAGED),
            intent=intent,
            reconciliation=reconciliation,
            transition_id=transition_id,
            occurred_at=now,
            provenance=provenance,
        )

    def _verify_adoptable(
        self, record: ObjectRecord
    ) -> tuple[ObservationRecord, Capture, Provenance]:
        """Every reason an object cannot become intent, in the order they stop mattering.

        Returns the observation the intent would be taken from, the values captured from
        it, and what is known about who owns it. Raises :class:`ManagementRefused`
        otherwise; it never returns a partial answer, because an intent controlling one
        field because the other could not be read would narrow what LocalPlane is
        answerable for without saying so.

        Ownership is checked after freshness on purpose. Refusing to adopt something Docker
        is running is a permanent condition and the most useful thing to be told — but it
        has to be decided from an observation that is current, or LocalPlane would be
        allowing or refusing adoption on the strength of facts that have expired.
        """
        if record.management_state == ManagementState.MANAGED:
            raise ManagementRefused(
                "already_managed",
                "this object is already managed; adopt is the transition from observed",
                {
                    "object_id": record.object_id,
                    "management_state": record.management_state,
                    "active_intent_id": record.active_intent_id,
                },
            )
        if record.management_state == ManagementState.OBSERVE_ONLY:
            raise ManagementRefused(
                "object_observe_only",
                "this object is observe-only and cannot be adopted",
                {
                    "object_id": record.object_id,
                    "management_state": record.management_state,
                    "reason": record.management_reason,
                },
            )

        observation = record.observation
        if observation is None:
            raise ManagementRefused(
                "no_observation",
                "this object has never been observed, so there is nothing to retain",
                {"object_id": record.object_id},
            )

        # Re-derive the classification from the observation about to become intent, rather
        # than trusting the stored stance alone. The two are written together by the
        # ingestor, so they should agree; if they ever do not, the observation in front of
        # us is the better evidence and adoption is the wrong moment to find out.
        classification = classify_management(
            InterfaceSignals(
                kind=observation.facts.get("kind", "unknown"),
                admin_up=observation.facts.get("admin_up"),
                operstate=observation.facts.get("operstate"),
                carrier=observation.facts.get("carrier"),
            )
        )
        if classification.state == ManagementState.OBSERVE_ONLY:
            raise ManagementRefused(
                "object_observe_only",
                "the newest observation classifies this object as observe-only",
                {
                    "object_id": record.object_id,
                    "reason": classification.reason,
                    "observation_id": observation.observation_id,
                },
            )

        freshness, age = derive_freshness(observation.observed_at_dt, self._freshness_ttl_s)
        if freshness is not Freshness.CURRENT:
            raise ManagementRefused(
                "observation_stale",
                "the newest observation is not current enough to be turned into intent",
                {
                    "object_id": record.object_id,
                    "observation_id": observation.observation_id,
                    "freshness": str(freshness),
                    "age_seconds": age,
                    "ttl_seconds": self._freshness_ttl_s,
                },
            )

        provenance = self.provenance.for_object(record)
        block = ownership_block(provenance)
        if block is not None:
            reason, owner = block
            raise ManagementRefused(
                reason,
                "another system owns this object, and LocalPlane has no write model that "
                "could share it — adopting would make LocalPlane answerable for something "
                "it must not touch",
                {
                    "object_id": record.object_id,
                    "ownership_state": str(provenance.state),
                    "ownership_reason": provenance.reason,
                    "owner": (
                        {
                            "provider": owner.provider,
                            "instance": owner.instance,
                            "label": owner.label,
                            "version": owner.version,
                        }
                        if owner is not None
                        else None
                    ),
                    "evidence": [
                        {"source": e.source, "kind": e.kind, "detail": e.detail}
                        for claim in provenance.claims
                        if claim.owner.external
                        for e in claim.evidence
                    ],
                    "evidence_gaps": list(provenance.gaps),
                },
            )

        capture = capture_interface_intent(observation.facts)
        if not capture.complete:
            raise ManagementRefused(
                "controlled_values_unverified",
                "a field LocalPlane would control could not be read from the newest "
                "observation, so it cannot be recorded as intent",
                {
                    "object_id": record.object_id,
                    "observation_id": observation.observation_id,
                    "fidelity": observation.fidelity,
                    "gaps": observation.gaps,
                    "fields": [
                        {
                            "field": f.field,
                            "value_type": str(f.value_type),
                            "reason": f.reason,
                            "observed": f.observed,
                        }
                        for f in capture.uncapturable
                    ],
                },
            )
        return observation, capture, provenance

    # ---------------------------------------------------------------------- revision

    def revise_intent(
        self,
        record: ObjectRecord,
        *,
        fields: Mapping[str, Any],
        expected_intent_id: str,
        expected_version: int | None = None,
    ) -> RevisionOutcome:
        """Retain new desired values the operator supplied. The host is not touched.

        Only fields the active intent already controls may be given a value, and a field
        the operator does not mention keeps the one it had. Growing the controlled set is
        an adoption decision made against verified evidence; a revision may change what
        LocalPlane wants, not what it is answerable for.
        """
        return self._revise(
            record,
            kind=RevisionKind.EXPLICIT,
            requested=fields,
            expected_intent_id=expected_intent_id,
            expected_version=expected_version,
        )

    def adopt_runtime_as_intent(
        self,
        record: ObjectRecord,
        *,
        expected_intent_id: str,
        expected_version: int | None = None,
    ) -> RevisionOutcome:
        """Make the currently verified runtime the retained desired state.

        This is the operator saying "the machine is right and my declaration was out of
        date". It is a statement about the present, so it is only honest if the present was
        actually read: a controlled field the newest observation could not produce refuses
        the whole revision rather than being filled in from the version being replaced.

        Nothing is applied, and nothing needs to be — every value recorded here is one the
        host already has.
        """
        return self._revise(
            record,
            kind=RevisionKind.RUNTIME_ADOPTION,
            requested={},
            expected_intent_id=expected_intent_id,
            expected_version=expected_version,
        )

    def _revise(
        self,
        record: ObjectRecord,
        *,
        kind: RevisionKind,
        requested: Mapping[str, Any],
        expected_intent_id: str,
        expected_version: int | None,
    ) -> RevisionOutcome:
        """One revision, atomically.

        The new version, the pointer that activates it, the event that explains it and the
        findings it settles land together or not at all. If anything here fails, the
        version being replaced is still the active one and every claim resting on it is
        still exactly as it was — which is why the pointer is moved rather than the old row
        edited, and why nothing is written outside this transaction.
        """
        now = _now()
        # Refuse the ordinary cases before taking the write lock. Being told that an
        # observed object has no intent to revise should cost nothing and roll nothing back.
        self._verify_revisable(record, expected_intent_id, expected_version)

        intent_id = f"int_{uuid.uuid4().hex}"
        revision_id = f"rev_{uuid.uuid4().hex}"

        with self._db.transaction():
            # Re-read and re-verify under the write lock, exactly as adopt does. Between
            # the check above and this line another revision may have landed, the object
            # may have been released, or a sweep may have replaced the observation this
            # revision is being made against — and all three change the answer.
            current = self.objects.get(record.object_id)
            if current is None:
                raise ManagementRefused(
                    "object_not_found",
                    "the object disappeared while its intent was being revised",
                    {"object_id": record.object_id},
                )
            active, observation, provenance = self._verify_revisable(
                current, expected_intent_id, expected_version
            )
            plan = self._plan_revision(current, kind, active, observation, requested)

            self.intents.insert(
                intent_id=intent_id,
                object_id=current.object_id,
                host_id=current.host_id,
                version=self.intents.next_version(current.object_id),
                supersedes=active.intent_id,
                schema_version=INTENT_SCHEMA_VERSION,
                # The contract comes from the observation this revision was made against,
                # which _verify_revisable has already established is the same contract the
                # version being replaced was written under. An intent whose capability or
                # provider drifted from its predecessor's would be comparable against
                # different evidence without anybody having said so.
                capability=observation.capability,
                provider=observation.provider,
                provider_version=observation.provider_version,
                observation_id=observation.observation_id,
                sweep_id=observation.sweep_id,
                observed_at=observation.observed_at,
                created_at=now,
                fields=[
                    (f.field, str(f.value_type), encode(f.value_type, f.value))
                    for f in plan.fields
                ],
            )
            self.objects.set_active_intent(current.object_id, intent_id)
            self.intents.record_revision(
                revision_id=revision_id,
                object_id=current.object_id,
                host_id=current.host_id,
                kind=str(plan.kind),
                intent_id=intent_id,
                occurred_at=now,
            )
            intent = self.intents.get(intent_id)
            assert intent is not None  # written in this transaction

            open_before = {f.finding_id for f in self.findings.open_for_object(current.object_id)}
            reconciliation = self._evaluate(
                current, intent, now, revised={c.field for c in plan.changed}
            )
            open_after = {f.finding_id for f in self.findings.open_for_object(current.object_id)}

            resolved = tuple(
                found
                for finding_id in sorted(open_before - open_after)
                if (found := self.findings.get(finding_id)) is not None
            )
            opened = tuple(
                found
                for finding_id in sorted(open_after - open_before)
                if (found := self.findings.get(finding_id)) is not None
            )

        LOG.info(
            "intent revised",
            extra={
                "object_id": current.object_id,
                "host_id": current.host_id,
                "revision_kind": str(plan.kind),
                "revision_id": revision_id,
                "superseded_intent_id": active.intent_id,
                "superseded_version": active.version,
                "intent_id": intent_id,
                "intent_version": intent.version,
                "changed": [c.field for c in plan.changed],
                "carried_forward": list(plan.carried_forward),
                "observation_id": observation.observation_id,
                "reconciliation": str(reconciliation.state),
                "findings_resolved": [f.finding_id for f in resolved],
                "findings_opened": [f.finding_id for f in opened],
                "host_effect": "none",
            },
        )
        return RevisionOutcome(
            kind=plan.kind,
            object_id=current.object_id,
            host_id=current.host_id,
            # Unchanged, and reported so that a caller can see it is unchanged. A revision
            # is not a transition: the object was managed before and is managed after.
            management_state=str(ManagementState.MANAGED),
            management_reason=current.management_reason,
            previous_intent=active,
            intent=intent,
            plan=plan,
            reconciliation=reconciliation,
            findings_resolved=resolved,
            findings_opened=opened,
            provenance=provenance,
            revision_id=revision_id,
            occurred_at=now,
        )

    def _verify_revisable(
        self, record: ObjectRecord, expected_intent_id: str, expected_version: int | None
    ) -> tuple[IntentRecord, ObservationRecord, Provenance]:
        """Every reason an intent cannot be revised, in the order they stop mattering.

        Returns the intent being replaced, the observation the revision is made against and
        what is known about who owns the object. Raises :class:`ManagementRefused`
        otherwise, with a code per condition.

        The order is not arbitrary:

        * **Management first.** Revision is defined only for an object LocalPlane already
          manages, and "this is a loopback device" and "this object has never been adopted"
          are different sentences with different remedies.
        * **Then the concurrency check**, because it decides *whose* decision is being
          recorded. A caller working from a version somebody has already replaced should be
          told that, not told something about observation freshness that would send them
          off to fix the wrong thing.
        * **Then the evidence.** A current observation is required for both kinds, and for
          two different reasons. Adopting the runtime is a claim about what is on the host,
          which cannot be made from a reading nobody vouches for. An explicit revision does
          not read the runtime at all — but it is a decision an operator made while looking
          at one, and recording which reading that was is what makes the decision auditable
          afterwards. Neither is a case for guessing.
        * **Then ownership**, on the same evidence, for the same reason adopt checks it.
        """
        if record.management_state == ManagementState.OBSERVE_ONLY:
            raise ManagementRefused(
                "object_observe_only",
                "this object is observe-only; LocalPlane holds no intent for it to revise",
                {
                    "object_id": record.object_id,
                    "management_state": record.management_state,
                    "reason": record.management_reason,
                },
            )
        if record.management_state != ManagementState.MANAGED:
            raise ManagementRefused(
                "not_managed",
                "this object is not managed; adopt it before revising what is intended "
                "for it",
                {
                    "object_id": record.object_id,
                    "management_state": record.management_state,
                },
            )

        active = self.intents.get(record.active_intent_id or "")
        if active is None or active.object_id != record.object_id:
            # Unreachable through the schema: the CHECK on `objects` will not hold a
            # managed row without an active intent, and the trigger will not let that
            # intent belong to another object. Raised rather than assumed away because a
            # store somebody has edited by hand should produce an error, not a traceback.
            raise ManagementRefused(
                "active_intent_missing",
                "this object is managed but its active intent could not be read",
                {
                    "object_id": record.object_id,
                    "active_intent_id": record.active_intent_id,
                },
            )
        if active.schema_version != INTENT_SCHEMA_VERSION:
            raise ManagementRefused(
                "intent_schema_unsupported",
                "this intent was written against a different controlled field set, so this "
                "build cannot say what revising it would mean",
                {
                    "object_id": record.object_id,
                    "intent_id": active.intent_id,
                    "intent_schema_version": active.schema_version,
                    "supported_schema_version": INTENT_SCHEMA_VERSION,
                },
            )
        if active.intent_id != expected_intent_id or (
            expected_version is not None and active.version != expected_version
        ):
            raise ManagementRefused(
                "intent_revision_conflict",
                "the intent this revision was written against is no longer the one in "
                "force; read it again and decide against the current desired state",
                {
                    "object_id": record.object_id,
                    "expected_intent_id": expected_intent_id,
                    "expected_version": expected_version,
                    "active_intent_id": active.intent_id,
                    "active_version": active.version,
                },
            )

        observation = record.observation
        if observation is None:
            raise ManagementRefused(
                "no_observation",
                "this object has never been observed, so there is nothing to revise "
                "against",
                {"object_id": record.object_id},
            )
        freshness, age = derive_freshness(observation.observed_at_dt, self._freshness_ttl_s)
        if freshness is not Freshness.CURRENT:
            raise ManagementRefused(
                "observation_stale",
                "the newest observation is not current enough to revise intent against",
                {
                    "object_id": record.object_id,
                    "observation_id": observation.observation_id,
                    "freshness": str(freshness),
                    "age_seconds": age,
                    "ttl_seconds": self._freshness_ttl_s,
                },
            )
        if (
            observation.capability != active.capability
            or observation.provider != active.provider
        ):
            # Reconciliation refuses to compare across this boundary, so an intent written
            # against one contract and activated while another is producing the
            # observations would be permanently unknown. Refusing here says why, once,
            # instead of leaving somebody to work it out from a reconciliation that never
            # resolves.
            raise ManagementRefused(
                "observation_source_incompatible",
                "the newest observation comes from a different source than the intent "
                "being revised, and the same field name from a different source is not "
                "the same fact",
                {
                    "object_id": record.object_id,
                    "intent_capability": active.capability,
                    "intent_provider": active.provider,
                    "observation_capability": observation.capability,
                    "observation_provider": observation.provider,
                },
            )

        provenance = self.provenance.for_object(record)
        block = ownership_block(provenance)
        if block is not None:
            reason, owner = block
            raise ManagementRefused(
                reason,
                "another system owns this object, and LocalPlane has no write model that "
                "could share it — deepening a claim it must not act on would make the "
                "conflict harder to see, not easier",
                {
                    "object_id": record.object_id,
                    "ownership_state": str(provenance.state),
                    "ownership_reason": provenance.reason,
                    "owner": (
                        {
                            "provider": owner.provider,
                            "instance": owner.instance,
                            "label": owner.label,
                            "version": owner.version,
                        }
                        if owner is not None
                        else None
                    ),
                    "evidence_gaps": list(provenance.gaps),
                },
            )
        return active, observation, provenance

    def _plan_revision(
        self,
        record: ObjectRecord,
        kind: RevisionKind,
        active: IntentRecord,
        observation: ObservationRecord,
        requested: Mapping[str, Any],
    ) -> RevisionPlan:
        """Work out the complete field set this revision would retain, or refuse."""
        current = tuple(
            CapturedField(f.field, ValueType(f.value_type), f.value) for f in active.fields
        )
        planned: RevisionPlan | RevisionRefused = (
            plan_explicit_revision(current, requested)
            if kind is RevisionKind.EXPLICIT
            else plan_runtime_revision(current, capture_interface_intent(observation.facts))
        )
        if isinstance(planned, RevisionRefused):
            raise ManagementRefused(
                planned.code,
                _REVISION_REFUSALS[planned.code],
                {
                    "object_id": record.object_id,
                    "intent_id": active.intent_id,
                    "observation_id": observation.observation_id,
                    **planned.detail,
                },
            )
        return planned

    # ----------------------------------------------------------------------- release

    def release(self, record: ObjectRecord) -> TransitionOutcome:
        """Stop retaining intent for ``record``. The host is left exactly as it is."""
        now = _now()
        if record.management_state != ManagementState.MANAGED:
            raise ManagementRefused(
                "not_managed",
                "this object is not managed; release is the transition from managed",
                {
                    "object_id": record.object_id,
                    "management_state": record.management_state,
                },
            )

        transition_id = f"trn_{uuid.uuid4().hex}"
        with self._db.transaction():
            # Same reason as adopt: the object is re-read under the write lock, and the
            # intent being retired is resolved from *that* read. Naming the intent the
            # caller happened to see would, in a race, record the release of a version
            # that had already been released.
            current = self._require_still(
                record.object_id, ManagementState.MANAGED, "not_managed"
            )
            intent = self.intents.get(current.active_intent_id or "")
            if intent is None:
                # Unreachable through the schema: `objects` will not hold a managed row
                # whose active_intent_id does not resolve. Raised rather than assumed away
                # because a store somebody has edited by hand should produce an error, not
                # a traceback.
                raise ManagementRefused(
                    "active_intent_missing",
                    "this object is managed but its active intent could not be read",
                    {
                        "object_id": current.object_id,
                        "active_intent_id": current.active_intent_id,
                    },
                )
            self.objects.set_observed(record.object_id, RELEASED_REASON)
            self.intents.record_transition(
                transition_id=transition_id,
                object_id=record.object_id,
                host_id=record.host_id,
                transition="release",
                from_state=ManagementState.MANAGED,
                to_state=ManagementState.OBSERVED,
                intent_id=intent.intent_id,
                observation_id=None,
                occurred_at=now,
            )
            # Every open drift claim about this object rested on an intent that is no
            # longer in force. Resolving them says that truthfully; deleting them would
            # erase the fact that LocalPlane was right about something for a while.
            resolved = self.findings.open_for_object(record.object_id)
            for finding in resolved:
                self.findings.resolve(
                    finding_id=finding.finding_id,
                    resolution=str(FindingResolution.INTENT_RELEASED),
                    resolved_by_observation_id=None,
                    now=now,
                )
            # Ownership conflicts end the same way, and for a stronger reason: the conflict
            # was between another system's ownership and LocalPlane's retained intent, and
            # only one of those two things has gone. The object is still Docker's; it is
            # simply no longer also LocalPlane's problem.
            conflicts = self.ownership_findings.open_for_object(record.object_id)
            for finding in conflicts:
                self.ownership_findings.resolve(
                    finding_id=finding.finding_id,
                    resolution=str(OwnershipResolution.INTENT_RELEASED),
                    resolved_by_provider_observation_id=None,
                    now=now,
                )

        LOG.info(
            "object released",
            extra={
                "object_id": record.object_id,
                "host_id": record.host_id,
                "intent_id": intent.intent_id,
                "intent_version": intent.version,
                "findings_resolved": len(resolved),
                "ownership_conflicts_resolved": len(conflicts),
                "host_effect": "none",
            },
        )
        return TransitionOutcome(
            transition="release",
            object_id=record.object_id,
            host_id=record.host_id,
            from_state=str(ManagementState.MANAGED),
            to_state=str(ManagementState.OBSERVED),
            intent=intent,
            # Nothing is retained, so there is nothing to reconcile against. Not in_sync.
            reconciliation=None,
            transition_id=transition_id,
            occurred_at=now,
        )

    # ---------------------------------------------------------------- reconciliation

    def reconciliation_for(
        self, record: ObjectRecord, intent: IntentRecord | None
    ) -> Reconciliation | None:
        """The read path. Pure: computes, never writes, never observes.

        Returns ``None`` for anything that is not managed. That is the difference between
        "there is no intent to disagree with" and ``in_sync``, and collapsing the two would
        make an unmanaged object look verified.
        """
        if record.management_state != ManagementState.MANAGED or intent is None:
            return None
        return reconcile(
            intent_capability=intent.capability,
            intent_provider=intent.provider,
            intended=_intended(intent),
            observation=_snapshot(record),
        )

    def evaluate_after_observation(
        self, object_ids: list[str], now: str, evidence: ProviderEvidence
    ) -> int:
        """Update findings for the managed objects among ``object_ids``.

        Called from the ingestor inside the sweep's own transaction, so a sweep and the
        claims it justifies land together. An object that was not re-read by this sweep is
        not evaluated: no new evidence arrived about it, and re-stating an old comparison
        as if it were fresh is the thing this whole model is built to avoid.

        Both kinds of claim are brought up to date here — drift, against retained intent,
        and ownership conflict, against what the providers said in the same sweep. Neither
        changes the object's management state. A sweep does not adopt and it does not
        release; evidence that arrives while an operator is not looking may open a finding,
        and that is the whole of what it may do.

        ``evidence`` is what *this* sweep collected, and it is a required argument. Passing
        the newest readings from the store instead would let a sweep that consulted nobody
        re-confirm a conflict on months-old evidence — the same falsehood as re-stating an
        old comparison as though somebody had just looked. A sweep with no provider
        evidence is entitled to say nothing, and an empty set is how it says it.

        Returns the number of objects evaluated.
        """
        managed = self.objects.managed(object_ids)
        if not managed:
            return 0
        intents = self.intents.active_for([m.object_id for m in managed])
        for record in managed:
            intent = intents.get(record.object_id)
            if intent is None:
                continue  # structurally impossible; see the CHECK on objects
            self._evaluate(record, intent, now)
            self._evaluate_ownership(record, intent, evidence, now)
        return len(managed)

    def _evaluate_ownership(
        self,
        record: ObjectRecord,
        intent: IntentRecord,
        evidence: ProviderEvidence,
        now: str,
    ) -> Provenance:
        """Open, refresh or resolve the ownership conflicts for one managed object.

        One finding per relation, keyed deterministically, exactly as drift is keyed per
        controlled field: ``created_by`` and ``configured_by`` are different claims about
        the object and a system that stops doing one may still be doing the other.

        The subtle case is the same one drift has, and it is handled the same way. A
        conflict is resolved only when the provider that claimed the object was consulted
        again and stopped claiming it. A provider that could not be read this time proves
        nothing: the finding stays open, ``updated_at`` moves, ``last_seen_at`` does not,
        and the pair says "still open, last proven then, looked since and could not tell".
        """
        provenance = self.provenance.for_object(record, evidence)
        observation = record.observation

        for relation in OwnershipRelation:
            key = finding_key(
                record.host_id, record.object_id, FINDING_TYPE_OWNERSHIP_CONFLICT, str(relation)
            )
            existing = self.ownership_findings.open_for_key(key)
            claims = [c for c in provenance.claims_for(relation) if c.owner.external]

            if claims:
                claim = claims[0]
                reading = evidence.get(claim.owner.provider)
                evidence_source = claim.evidence[0].source if claim.evidence else claim.reason
                if existing is None:
                    self.ownership_findings.insert(
                        finding_id=f"own_{uuid.uuid4().hex}",
                        finding_key=key,
                        host_id=record.host_id,
                        object_id=record.object_id,
                        finding_type=FINDING_TYPE_OWNERSHIP_CONFLICT,
                        subject=str(relation),
                        intent_id=intent.intent_id,
                        owner_provider=claim.owner.provider,
                        owner_instance=claim.owner.instance,
                        owner_label=claim.owner.label,
                        confidence=str(claim.confidence),
                        evidence_source=evidence_source,
                        reason=claim.reason,
                        provider_observation_id=(
                            reading.provider_observation_id if reading else None
                        ),
                        observation_id=observation.observation_id if observation else None,
                        sweep_id=observation.sweep_id if observation else None,
                        now=now,
                    )
                else:
                    self.ownership_findings.update_evidence(
                        finding_id=existing.finding_id,
                        intent_id=intent.intent_id,
                        owner_provider=claim.owner.provider,
                        owner_instance=claim.owner.instance,
                        owner_label=claim.owner.label,
                        confidence=str(claim.confidence),
                        evidence_source=evidence_source,
                        reason=claim.reason,
                        provider_observation_id=(
                            reading.provider_observation_id if reading else None
                        ),
                        observation_id=observation.observation_id if observation else None,
                        sweep_id=observation.sweep_id if observation else None,
                        now=now,
                    )
                continue

            if existing is None:
                continue

            reading = evidence.get(existing.owner_provider)
            if reading is None or reading.status not in (ProviderStatus.OK, ProviderStatus.ABSENT):
                self.ownership_findings.touch(finding_id=existing.finding_id, now=now)
                continue
            self.ownership_findings.resolve(
                finding_id=existing.finding_id,
                resolution=str(OwnershipResolution.OWNER_NO_LONGER_CLAIMS),
                resolved_by_provider_observation_id=reading.provider_observation_id,
                now=now,
            )
        return provenance

    # ------------------------------------------------------------------------ internals

    def _require_still(
        self, object_id: str, expected: ManagementState, code: str
    ) -> ObjectRecord:
        """Re-read the object under the write lock and confirm it has not moved."""
        current = self.objects.get(object_id)
        if current is None or current.management_state != expected:
            raise ManagementRefused(
                code,
                "another change to this object landed first",
                {
                    "object_id": object_id,
                    "expected_state": str(expected),
                    "management_state": current.management_state if current else None,
                    "active_intent_id": current.active_intent_id if current else None,
                },
            )
        return current

    def _evaluate(
        self,
        record: ObjectRecord,
        intent: IntentRecord,
        now: str,
        revised: set[str] | None = None,
    ) -> Reconciliation:
        """Compare, then bring the durable claims into line with what the comparison found.

        The comparison itself is pure and lives in the domain. What happens here is the
        lifecycle: a disagreement that is new opens a finding, one that is still there
        updates the one that exists, one that has gone is resolved with the observation
        that proved it, and one that could no longer be read leaves the finding open and
        says so. Only the last of those is subtle, and it is the one that matters: an
        unreadable value is not evidence that a problem went away.

        ``revised`` names the fields whose *intended* value this evaluation just changed,
        and it exists so that a drift ending is attributed to whichever of the two things
        actually moved. A disagreement that ended because the operator revised the intent
        did not end because the host was put right, and resolving it with the observation
        that happens to agree would read as though somebody had fixed something. A field
        the revision left alone is not in this set even when it is evaluated in the same
        breath: if its value came back on its own, that is the observation's doing and it
        is recorded as the observation's doing.
        """
        revised = revised or set()
        result = reconcile(
            intent_capability=intent.capability,
            intent_provider=intent.provider,
            intended=_intended(intent),
            observation=_snapshot(record),
        )
        for comparison in result.fields:
            key = finding_key(
                record.host_id, record.object_id, FINDING_TYPE_INTERFACE_DRIFT, comparison.field
            )
            existing = self.findings.open_for_key(key)

            if comparison.comparison is Comparison.MATCHES:
                if existing is not None:
                    moved = comparison.field in revised
                    self.findings.resolve(
                        finding_id=existing.finding_id,
                        resolution=str(
                            FindingResolution.INTENT_REVISED
                            if moved
                            else FindingResolution.OBSERVED_MATCHES_INTENT
                        ),
                        # A revised intent has no observation that proved the claim ended,
                        # because nothing about the host ended it. The schema agrees: only
                        # observed_matches_intent may name one.
                        resolved_by_observation_id=None if moved else result.observation_id,
                        now=now,
                    )
                continue

            if existing is None:
                if comparison.comparison is Comparison.UNKNOWN:
                    # No claim is opened on an unreadable value. "LocalPlane cannot see
                    # this field" is a fact about the observation, and it is already
                    # reported as reconciliation=unknown with the gaps that explain it —
                    # asserting a disagreement nobody observed would be a fabrication.
                    continue
                self.findings.insert(
                    finding_id=f"fnd_{uuid.uuid4().hex}",
                    finding_key=key,
                    host_id=record.host_id,
                    object_id=record.object_id,
                    finding_type=FINDING_TYPE_INTERFACE_DRIFT,
                    subject=comparison.field,
                    intent_id=intent.intent_id,
                    intended_type=str(comparison.value_type),
                    intended_value=encode(comparison.value_type, comparison.intended),
                    observed_type=str(comparison.value_type),
                    observed_value=encode(comparison.value_type, comparison.observed),
                    comparison=str(comparison.comparison),
                    reason=comparison.reason,
                    observation_id=result.observation_id,
                    sweep_id=result.sweep_id,
                    now=now,
                )
                continue

            confirmed = comparison.comparison is Comparison.DIFFERS
            self.findings.update_evidence(
                finding_id=existing.finding_id,
                intent_id=intent.intent_id,
                intended_type=str(comparison.value_type),
                intended_value=encode(comparison.value_type, comparison.intended),
                observed_type=str(comparison.value_type) if confirmed else None,
                observed_value=(
                    encode(comparison.value_type, comparison.observed) if confirmed else None
                ),
                comparison=str(comparison.comparison),
                reason=comparison.reason,
                observation_id=result.observation_id,
                sweep_id=result.sweep_id,
                now=now,
                confirmed=confirmed,
            )
        return result


def _intended(intent: IntentRecord) -> list[IntendedField]:
    return [
        IntendedField(field=f.field, value_type=ValueType(f.value_type), value=f.value)
        for f in intent.fields
    ]


def _snapshot(record: ObjectRecord) -> ObservedSnapshot | None:
    observation = record.observation
    if observation is None:
        return None
    return ObservedSnapshot(
        observation_id=observation.observation_id,
        sweep_id=observation.sweep_id,
        observed_at=observation.observed_at,
        capability=observation.capability,
        provider=observation.provider,
        facts=observation.facts,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
