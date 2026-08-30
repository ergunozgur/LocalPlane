"""Acquire one request-scoped, read-only systemd lifecycle planning context.

The API hands this service facts derived from its accepted socket.  The service derives
management-provider names only from LocalPlane's existing provenance records, calls one
closed agent read, and ingests the returned target observation through Slice 11A's generic
targeted-observation seam.  It never accepts an HTTP model and never dispatches a job.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from localplane.backend.agent_client import AgentClient, AgentError
from localplane.backend.api.transport import RequestConnectionEvidence
from localplane.backend.db.repositories import ObjectRecord
from localplane.backend.domain.protection import ManagementPathVerdict
from localplane.backend.domain.provenance import OwnershipRelation
from localplane.backend.domain.systemd_lifecycle import (
    EffectEdge,
    SystemdLifecycleContext,
    SystemdServiceAction,
)
from localplane.backend.ingest import Ingestor
from localplane.backend.provenance import ProvenanceService


class SystemdLifecycleContextService:
    """The backend half of the Part A read seam."""

    def __init__(
        self,
        client: AgentClient,
        ingestor: Ingestor,
        provenance: ProvenanceService,
    ) -> None:
        self._client = client
        self._ingestor = ingestor
        self._provenance = provenance
        self._objects = ingestor.objects

    def observe(
        self,
        *,
        record: ObjectRecord,
        action: SystemdServiceAction,
        connection: RequestConnectionEvidence,
        management_path: ManagementPathVerdict,
    ) -> SystemdLifecycleContext:
        providers, provider_complete = self._management_providers(management_path)
        if not connection.usable:
            return self._unavailable(
                record,
                action,
                connection.reason or "accepted_tcp_tuple_unavailable",
            )
        wire_connection = {
            key: value
            for key, value in connection.as_dict().items()
            if key
            in {
                "family", "peer_ip", "peer_port", "local_ip", "local_port",
                "backend_netns_inode",
            }
        }
        try:
            answer = self._client.observe_systemd_lifecycle_context(
                target_unit_id=record.identity_value,
                action=str(action),
                connection=wire_connection,
                management_providers=list(providers),
                provider_evidence_complete=provider_complete,
            )
        except AgentError as exc:
            return self._unavailable(record, action, f"agent:{exc.code}")
        if answer.get("host_id") != record.host_id or not isinstance(answer.get("context"), dict):
            return self._unavailable(record, action, "malformed_lifecycle_context_response")

        raw = dict(answer["context"])
        self._ingest_target(answer, raw)
        current = self._objects.get(record.object_id)
        observation_id = (
            current.observation.observation_id
            if current is not None and current.observation is not None
            else None
        )
        raw["observation_id"] = observation_id
        try:
            return SystemdLifecycleContext.from_dict(raw)
        except (KeyError, TypeError, ValueError):
            return self._unavailable(record, action, "malformed_lifecycle_context_evidence")

    def _management_providers(
        self, verdict: ManagementPathVerdict
    ) -> tuple[tuple[str, ...], bool]:
        if not verdict.confirmed or verdict.resource_id is None:
            return (), False
        record = self._objects.get(verdict.resource_id)
        if record is None:
            return (), False
        provenance = self._provenance.for_object(record)
        providers = tuple(
            sorted(
                {
                    claim.owner.provider
                    for claim in provenance.claims_for(OwnershipRelation.CONFIGURED_BY)
                    if claim.owner.external
                }
            )
        )
        return providers, not provenance.gaps

    def _ingest_target(self, answer: dict[str, Any], raw: dict[str, Any]) -> None:
        graph = raw.get("evidence", {}).get("effect_graph", {})
        target = graph.get("target_observation") if isinstance(graph, dict) else None
        if not isinstance(target, dict):
            return
        observed_at = str(raw.get("observed_at") or _now())
        self._ingestor.ingest_systemd_sweep(
            {
                "host_id": answer["host_id"],
                "agent_instance_id": answer.get("agent_instance_id"),
                "units": {
                    "capability": "systemd.units.observe",
                    "provider": "systemd",
                    "provider_version": str(graph.get("provider_version") or "unknown"),
                    "source": "systemd.units",
                    "status": "ok",
                    "scope": "targeted",
                    "requested_unit": raw.get("target_unit"),
                    "started_at": observed_at,
                    "completed_at": observed_at,
                    "reason": None,
                    "units": [target],
                    "missing": [],
                    "issues": [],
                },
            }
        )

    @staticmethod
    def _unavailable(
        record: ObjectRecord, action: SystemdServiceAction, reason: str
    ) -> SystemdLifecycleContext:
        facts = (
            dict(record.observation.facts)
            if record.observation is not None
            else {"canonical_id": record.identity_value}
        )
        return SystemdLifecycleContext(
            status="partial",
            observed_at=_now(),
            target_unit=record.identity_value,
            action=action,
            target_facts=facts,
            effect_units=(record.identity_value,),
            effect_complete=False,
            management_complete=False,
            agent_complete=False,
            gaps=(reason,),
            evidence={"context_read": {"status": "unavailable", "reason": reason}},
            observation_id=(
                record.observation.observation_id if record.observation is not None else None
            ),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
