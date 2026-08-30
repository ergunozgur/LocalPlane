"""Taking ownership of an observation.

The agent reports what the host said. This is where the backend decides what it means and
writes it down — the point at which a reading becomes LocalPlane's durable truth.

The judgements made here, and nowhere else:

* **identity** — which object this reading is about, and on what evidence;
* **management classification** — whether that object is one LocalPlane could be
  answerable for. Whether it *is* managed was decided by an operator through adopt, and a
  sweep never overturns that;
* **health** — what the link-layer evidence amounts to.

A managed object's reconciliation is brought up to date here too, because this is where the
evidence for it arrives. The comparison itself belongs to
:mod:`localplane.backend.management`; what happens here is that it runs against the
observation that was just written, in the same transaction.

A sweep is written in one transaction. A batch that fails halfway leaves nothing behind,
because objects whose ``last_seen_at`` advanced past observations that were never written
would be a quieter kind of wrong than an error.

A failed sweep is still recorded. An empty interface list has two very different causes —
nothing is there, or nobody looked successfully — and the sweep row is what lets a caller
tell them apart.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from localplane.backend.agent_client import AgentClient, AgentError
from localplane.backend.db.database import Database
from localplane.backend.db.repositories import (
    AgentRepository,
    HostRepository,
    ObjectRepository,
    ProviderObservationRepository,
    SweepRepository,
)
from localplane.backend.domain.docker import (
    classify_container_management,
    derive_container_health,
)
from localplane.backend.domain.identity import (
    OBJECT_KIND_DOCKER_CONTAINER,
    OBJECT_KIND_NETWORK_INTERFACE,
    OBJECT_KIND_SYSTEMD_UNIT,
    identify_container,
    identify_interface,
    identify_systemd_unit,
)
from localplane.backend.domain.network import InterfaceSignals, classify_management, derive_health
from localplane.backend.domain.provenance import ProviderEvidence, ProviderReadingView
from localplane.backend.domain.states import DEFAULT_FRESHNESS_TTL_S
from localplane.backend.domain.systemd import (
    classify_systemd_management,
    derive_systemd_health,
)
from localplane.backend.management import ManagementService
from localplane.protocol.capabilities import (
    CAPABILITY_DOCKER_CONTAINERS_OBSERVE,
    CAPABILITY_NETWORK_OBSERVE,
    CAPABILITY_NETWORK_PROVIDERS_OBSERVE,
    CAPABILITY_SYSTEMD_UNITS_OBSERVE,
    CapabilityStatus,
)
from localplane.protocol.providers import ProviderStatus
from localplane.protocol.wire import PROTOCOL_VERSION

LOG = logging.getLogger("localplane.backend.ingest")


@dataclass(frozen=True)
class IngestResult:
    """What one ingested sweep did. ``status`` comes from the agent's own derivation."""

    sweep_id: str
    host_id: str
    agent_instance_id: str | None
    status: str
    object_count: int
    observation_count: int
    missing: list[str] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    providers: list[dict[str, Any]] = field(default_factory=list)
    """One entry per provider consulted, with what it did. Empty when none was.

    Reported alongside the interface count rather than folded into it: a sweep that saw
    every link and could not reach the Docker daemon succeeded at the first job and not the
    second, and a caller is entitled to see both.
    """
    detail: dict[str, Any] = field(default_factory=dict)


class Ingestor:
    """Writes agent output into the store. Owns every judgement made about it."""

    def __init__(self, database: Database, management: ManagementService | None = None) -> None:
        self._db = database
        self.hosts = HostRepository(database)
        self.agents = AgentRepository(database)
        self.objects = ObjectRepository(database)
        self.sweeps = SweepRepository(database)
        self.provider_readings = ProviderObservationRepository(database)
        # Reconciliation is not a separate pass over the store. New evidence about a
        # managed object arrives here and nowhere else, so the claims that evidence
        # justifies are brought up to date in the same transaction as the sweep — a
        # database that holds an observation while still asserting a drift that
        # observation disproved would be lying in the interval between the two writes.
        # The TTL this service is given governs adoption only, which ingest never does.
        self.management = management or ManagementService(database, DEFAULT_FRESHNESS_TTL_S)

    def ingest_handshake(self, hello: dict[str, Any]) -> tuple[str, str]:
        """Record the host and the agent instance. Returns ``(host_id, agent_instance_id)``."""
        now = _now()
        host = hello["host"]
        agent = hello["agent"]
        with self._db.transaction():
            host_id = self.hosts.upsert(host, now)
            agent_instance_id = self.agents.upsert(host_id, agent, PROTOCOL_VERSION, now)
            self.agents.replace_capabilities(agent_instance_id, hello.get("capabilities", []))
        LOG.info(
            "agent handshake recorded",
            extra={
                "host_id": host_id,
                "agent_instance_id": agent_instance_id,
                "privilege": agent["privilege"],
                "capabilities": {
                    c["capability"]: c["status"] for c in hello.get("capabilities", [])
                },
            },
        )
        return host_id, agent_instance_id

    def ingest_network_sweep(self, payload: dict[str, Any]) -> IngestResult:
        """Normalize, judge and persist one network observation sweep.

        The provider evidence collected in the same cycle, if any, lands in the same
        transaction and against the same sweep. That is what makes ownership auditable
        later: an intent names the observation it was captured from, the observation names
        its sweep, and the sweep holds what every provider said at that moment.

        Provider evidence is optional and its absence is never fatal. A host with no Docker,
        an agent too old to have the capability, a daemon that refused the socket — each is
        recorded and none of them costs LocalPlane its view of the host's links.
        """
        received_at = _now()
        batch = payload["observation"]
        host_id = payload["host_id"]
        agent_instance_id = payload.get("agent_instance_id")
        sweep_id = f"sweep_{uuid.uuid4().hex}"

        interfaces = batch.get("interfaces", [])
        observations: list[dict[str, Any]] = []
        objects: list[dict[str, Any]] = []

        for observed in interfaces:
            facts = observed["facts"]
            identity = identify_interface(
                host_id=host_id,
                name=facts["name"],
                mac_address=facts.get("mac_address"),
                mac_is_permanent=facts.get("mac_is_permanent"),
                device_path=facts.get("device_path"),
            )
            signals = InterfaceSignals(
                kind=facts.get("kind", "unknown"),
                admin_up=facts.get("admin_up"),
                operstate=facts.get("operstate"),
                carrier=facts.get("carrier"),
            )
            health = derive_health(signals)
            management = classify_management(signals)

            objects.append(
                {
                    "object_id": identity.object_id,
                    "host_id": host_id,
                    "kind": OBJECT_KIND_NETWORK_INTERFACE,
                    "identity_basis": str(identity.basis),
                    "identity_value": identity.value,
                    "identity_confidence": str(identity.confidence),
                    "display_name": facts["name"],
                    "management_state": str(management.state),
                    "management_reason": management.reason,
                    "now": received_at,
                }
            )
            observations.append(
                {
                    "observation_id": f"obs_{uuid.uuid4().hex}",
                    "sweep_id": sweep_id,
                    "host_id": host_id,
                    "object_id": identity.object_id,
                    "capability": batch["capability"],
                    "provider": batch["provider"],
                    "provider_version": batch["provider_version"],
                    "method": observed["method"],
                    "fidelity": observed["fidelity"],
                    "observed_at": observed["observed_at"],
                    "received_at": received_at,
                    "health_state": str(health.state),
                    "health_reason": health.reason,
                    "gaps": observed.get("gaps", []),
                    "facts": facts,
                    "evidence": observed.get("evidence", {}),
                }
            )

        provider_rows, provider_issues = _provider_rows(
            payload.get("providers"), payload.get("provider_error"), sweep_id, host_id, received_at
        )

        sweep = {
            "sweep_id": sweep_id,
            "host_id": host_id,
            "agent_instance_id": agent_instance_id,
            "capability": batch["capability"],
            "scope": batch.get("scope", "inventory"),
            "provider": batch["provider"],
            "provider_version": batch["provider_version"],
            "status": batch["status"],
            "started_at": batch["started_at"],
            "completed_at": batch["completed_at"],
            "received_at": received_at,
            "object_count": len(observations),
            "missing": batch.get("missing", []),
            # Provider trouble is reported on the sweep that tried, beside whatever the
            # interface read had to say. It never changes the sweep's own status: the
            # sweep observed the interfaces it set out to observe.
            "issues": list(batch.get("issues", [])) + provider_issues,
        }

        with self._db.transaction():
            self.sweeps.insert(sweep)
            for record in objects:
                self.objects.upsert(**record)
            for observation in observations:
                self.sweeps.insert_observation(observation)
            for reading in provider_rows:
                self.provider_readings.insert(reading)
            reconciled = self.management.evaluate_after_observation(
                [record["object_id"] for record in objects],
                received_at,
                # What *this* sweep collected, in the same transaction as the rows it came
                # from: a conflict opened now rests on the evidence sitting beside it. A
                # sweep that consulted nobody passes an empty set, which reads as "not
                # consulted" everywhere downstream — never as "asked, and it still holds".
                ProviderEvidence.of(_reading_views(provider_rows)),
            )

        LOG.info(
            "network sweep ingested",
            extra={
                "sweep_id": sweep_id,
                "host_id": host_id,
                "status": batch["status"],
                "objects": len(observations),
                "managed_reconciled": reconciled,
                "missing": batch.get("missing", []),
                "issues": len(batch.get("issues", [])) + len(provider_issues),
                "providers": {r["provider"]: r["status"] for r in provider_rows},
            },
        )
        return IngestResult(
            sweep_id=sweep_id,
            host_id=host_id,
            agent_instance_id=agent_instance_id,
            status=batch["status"],
            object_count=len(objects),
            observation_count=len(observations),
            missing=list(batch.get("missing", [])),
            issues=list(batch.get("issues", [])) + provider_issues,
            providers=[
                {
                    "provider": r["provider"],
                    "source": r["source"],
                    "status": r["status"],
                    "reason": r["reason"],
                    "version": r["provider_version"],
                }
                for r in provider_rows
            ],
        )


    def ingest_container_sweep(self, payload: dict[str, Any]) -> IngestResult:
        """Normalize, judge and persist one Docker container observation sweep.

        The *same* pipeline as an interface sweep, deliberately and without a second one
        beside it: one sweep row, one object per container, one append-only observation per
        object, one transaction. That is what makes a container a LocalPlane resource rather
        than a Docker page inside LocalPlane — its identity, its history, its freshness and
        its evidence all work the way every other object's do, and the Run engine can target
        it without learning what it is.

        The judgements made here are the same three, and they are Docker's facts read through
        LocalPlane's vocabulary: identity from the daemon's container id, health from the
        lifecycle state and the container's own health check, and a management classification
        that says a container is something LocalPlane could act on.

        **No ownership evidence is collected in this sweep and none is needed.** A container's
        provenance is not a correlation problem: the daemon that answered this request is the
        system that created and configures it, so the observation *is* the evidence, and
        ``derive_container_provenance`` reads it from the facts rather than from a separate
        provider reading that could be missing.
        """
        received_at = _now()
        batch = payload["containers"]
        host_id = payload["host_id"]
        agent_instance_id = payload.get("agent_instance_id")
        sweep_id = f"sweep_{uuid.uuid4().hex}"

        objects: list[dict[str, Any]] = []
        observations: list[dict[str, Any]] = []

        for observed in batch.get("containers", []):
            facts = observed["facts"]
            container_id = facts.get("container_id")
            if not isinstance(container_id, str) or not container_id:
                # An observation with no identity is not one LocalPlane can record. The
                # agent already reports this as an issue on the batch; dropping it here is
                # the same refusal an interface with no name would get.
                continue
            identity = identify_container(host_id=host_id, container_id=container_id)
            health = derive_container_health(facts)
            management = classify_container_management(facts)
            objects.append(
                {
                    "object_id": identity.object_id,
                    "host_id": host_id,
                    "kind": OBJECT_KIND_DOCKER_CONTAINER,
                    "identity_basis": str(identity.basis),
                    "identity_value": identity.value,
                    "identity_confidence": str(identity.confidence),
                    # The name an operator uses, not the id. Docker's own display name, and
                    # it may change under a stable identity — which is exactly why identity
                    # is not the name.
                    "display_name": facts.get("name") or container_id[:12],
                    "management_state": str(management.state),
                    "management_reason": management.reason,
                    "now": received_at,
                }
            )
            observations.append(
                {
                    "observation_id": f"obs_{uuid.uuid4().hex}",
                    "sweep_id": sweep_id,
                    "host_id": host_id,
                    "object_id": identity.object_id,
                    "capability": batch["capability"],
                    "provider": batch["provider"],
                    "provider_version": batch["provider_version"],
                    "method": observed["method"],
                    "fidelity": observed["fidelity"],
                    "observed_at": observed["observed_at"],
                    "received_at": received_at,
                    "health_state": str(health.state),
                    "health_reason": health.reason,
                    "gaps": observed.get("gaps", []),
                    "facts": facts,
                    "evidence": observed.get("evidence", {}),
                }
            )

        sweep = {
            "sweep_id": sweep_id,
            "host_id": host_id,
            "agent_instance_id": agent_instance_id,
            "capability": batch["capability"],
            "scope": batch.get("scope", "inventory"),
            "provider": batch["provider"],
            "provider_version": batch["provider_version"],
            "status": batch["status"],
            "started_at": batch["started_at"],
            "completed_at": batch["completed_at"],
            "received_at": received_at,
            "object_count": len(observations),
            "missing": [],
            "issues": list(batch.get("issues", [])),
        }

        with self._db.transaction():
            self.sweeps.insert(sweep)
            for record in objects:
                self.objects.upsert(**record)
            for observation in observations:
                self.sweeps.insert_observation(observation)

        LOG.info(
            "container sweep ingested",
            extra={
                "sweep_id": sweep_id,
                "host_id": host_id,
                "status": batch["status"],
                "objects": len(observations),
                "issues": len(batch.get("issues", [])),
                "provider_version": batch["provider_version"],
            },
        )
        return IngestResult(
            sweep_id=sweep_id,
            host_id=host_id,
            agent_instance_id=agent_instance_id,
            status=batch["status"],
            object_count=len(objects),
            observation_count=len(observations),
            issues=list(batch.get("issues", [])),
            providers=[
                {
                    "provider": batch["provider"],
                    "source": batch.get("source"),
                    "status": "ok" if batch["status"] != "failed" else "unavailable",
                    "reason": batch.get("reason"),
                    "version": batch["provider_version"],
                }
            ],
        )

    def ingest_systemd_sweep(self, payload: dict[str, Any]) -> IngestResult:
        """Persist a loaded-unit inventory or targeted read through the generic model.

        Relationships remain observation facts.  systemd is the runtime authority; there
        is no systemd state or relationship table to become a second service manager.
        Resolution is performed once from a same-request alias map plus one database query,
        and an unresolved reference never causes a placeholder object to be invented.
        """
        received_at = _now()
        batch = payload["units"]
        host_id = payload["host_id"]
        agent_instance_id = payload.get("agent_instance_id")
        sweep_id = f"sweep_{uuid.uuid4().hex}"
        observed_units = list(batch.get("units", []))

        identities: dict[str, Any] = {}
        aliases: dict[str, str] = {}
        for observed in observed_units:
            facts = observed.get("facts") or {}
            canonical_id = facts.get("canonical_id")
            if not isinstance(canonical_id, str) or not canonical_id:
                continue
            identity = identify_systemd_unit(host_id, canonical_id)
            identities[canonical_id] = identity
            aliases[canonical_id] = canonical_id
            for name in facts.get("names") or []:
                if isinstance(name, str) and name:
                    aliases.setdefault(name, canonical_id)

        historical_object_ids = self.objects.identity_map(host_id, OBJECT_KIND_SYSTEMD_UNIT)
        historical_object_ids.update(
            {canonical: identity.object_id for canonical, identity in identities.items()}
        )
        if batch.get("scope", "inventory") == "inventory":
            current_object_ids = {
                canonical: identity.object_id for canonical, identity in identities.items()
            }
        else:
            latest_inventory = self.sweeps.latest(
                host_id, CAPABILITY_SYSTEMD_UNITS_OBSERVE, scope="inventory"
            )
            inventory_members = (
                self.sweeps.object_ids(latest_inventory.sweep_id)
                if latest_inventory is not None
                else set()
            )
            current_object_ids = {
                canonical: object_id
                for canonical, object_id in historical_object_ids.items()
                if object_id in inventory_members
            }
            inventory_facts_by_object = (
                self.sweeps.facts_by_object(latest_inventory.sweep_id)
                if latest_inventory is not None
                else {}
            )
            for object_id, inventory_facts in inventory_facts_by_object.items():
                canonical = inventory_facts.get("canonical_id")
                if not isinstance(canonical, str) or current_object_ids.get(canonical) != object_id:
                    continue
                aliases.setdefault(canonical, canonical)
                for name in inventory_facts.get("names") or []:
                    if isinstance(name, str) and name:
                        aliases.setdefault(name, canonical)

        resolution = payload.get("agent_unit_resolution") or {}
        resolution_summary = _agent_unit_resolution_summary(resolution)
        resolved_agent_unit = resolution.get("canonical_id") if isinstance(resolution, dict) else None

        objects: list[dict[str, Any]] = []
        observations: list[dict[str, Any]] = []
        for observed in observed_units:
            raw_facts = observed.get("facts") or {}
            canonical_id = raw_facts.get("canonical_id")
            identity = identities.get(canonical_id)
            if identity is None:
                continue
            facts = dict(raw_facts)
            facts["relationships"] = _project_systemd_relationships(
                raw_facts.get("relationships"),
                aliases,
                historical_object_ids,
                current_object_ids,
            )
            if canonical_id == resolved_agent_unit and resolution_summary is not None:
                facts["agent_process_containment"] = resolution_summary

            health = derive_systemd_health(facts)
            management = classify_systemd_management(facts)
            objects.append(
                {
                    "object_id": identity.object_id,
                    "host_id": host_id,
                    "kind": OBJECT_KIND_SYSTEMD_UNIT,
                    "identity_basis": str(identity.basis),
                    "identity_value": identity.value,
                    "identity_confidence": str(identity.confidence),
                    "display_name": canonical_id,
                    "management_state": str(management.state),
                    "management_reason": management.reason,
                    "now": received_at,
                }
            )
            evidence = dict(observed.get("evidence") or {})
            if canonical_id == resolved_agent_unit and resolution_summary is not None:
                evidence["agent_process_containment"] = resolution_summary
            observations.append(
                {
                    "observation_id": f"obs_{uuid.uuid4().hex}",
                    "sweep_id": sweep_id,
                    "host_id": host_id,
                    "object_id": identity.object_id,
                    "capability": batch["capability"],
                    "provider": batch["provider"],
                    "provider_version": batch["provider_version"],
                    "method": observed["method"],
                    "fidelity": observed["fidelity"],
                    "observed_at": observed["observed_at"],
                    "received_at": received_at,
                    "health_state": str(health.state),
                    "health_reason": health.reason,
                    "gaps": observed.get("gaps", []),
                    "facts": facts,
                    "evidence": evidence,
                }
            )

        issues = list(batch.get("issues", []))
        status = batch.get("status", "failed")
        if resolution and resolution.get("status") != "resolved":
            issues.append(
                {
                    "source": "systemd.agent_unit",
                    "code": resolution.get("reason") or "agent_unit_unresolved",
                    "message": "the agent process's containing systemd unit was not resolved",
                    "detail": resolution_summary or {},
                }
            )

        sweep = {
            "sweep_id": sweep_id,
            "host_id": host_id,
            "agent_instance_id": agent_instance_id,
            "capability": batch["capability"],
            "scope": batch.get("scope", "inventory"),
            "provider": batch["provider"],
            "provider_version": batch["provider_version"],
            "status": status,
            "started_at": batch["started_at"],
            "completed_at": batch["completed_at"],
            "received_at": received_at,
            "object_count": len(observations),
            "missing": list(batch.get("missing", [])),
            "issues": issues,
        }

        with self._db.transaction():
            self.sweeps.insert(sweep)
            for record in objects:
                self.objects.upsert(**record)
            for observation in observations:
                self.sweeps.insert_observation(observation)

        detail = {
            key: batch.get(key)
            for key in (
                "scope",
                "requested_unit",
                "listed_count",
                "selected_count",
                "observed_count",
                "inventory_limit",
                "inventory_complete",
                "truncated",
                "cap_reached",
                "inventory_method",
            )
            if key in batch
        }
        detail["agent_unit_resolution"] = resolution_summary
        LOG.info(
            "systemd observation ingested",
            extra={
                "sweep_id": sweep_id,
                "host_id": host_id,
                "status": status,
                "objects": len(observations),
                "issues": len(issues),
                "provider_version": batch["provider_version"],
                "scope": batch.get("scope", "inventory"),
            },
        )
        return IngestResult(
            sweep_id=sweep_id,
            host_id=host_id,
            agent_instance_id=agent_instance_id,
            status=status,
            object_count=len(objects),
            observation_count=len(observations),
            missing=list(batch.get("missing", [])),
            issues=issues,
            providers=[
                {
                    "provider": batch["provider"],
                    "source": batch.get("source"),
                    "status": "ok" if status != "failed" else "unavailable",
                    "reason": batch.get("reason"),
                    "version": batch["provider_version"],
                }
            ],
            detail=detail,
        )


class ObservationCoordinator:
    """Drives one observation cycle: handshake, observe, ingest.

    The handshake runs first every time, not only at startup. It is one cheap round trip,
    it keeps the recorded capability set matching the agent that is actually answering,
    and it guarantees the host row exists before an observation references it.
    """

    def __init__(self, client: AgentClient, ingestor: Ingestor) -> None:
        self._client = client
        self._ingestor = ingestor

    def refresh_network(self, names: list[str] | None = None) -> IngestResult:
        """Observe network interfaces now, ask the providers who owns them, and record both.

        Raises :class:`AgentError` when the agent cannot be reached or refuses the interface
        observation. Nothing is written in that case, and the caller is expected to report
        the failure rather than an empty estate.

        The provider evidence is *not* allowed to fail the same way. An agent without the
        capability, a Docker daemon that refuses its socket, a tailscaled that is not
        running — none of them stops the interfaces being observed, recorded and served.
        They are recorded as what they are: the ownership question, left open, with the
        reason attached.
        """
        hello = self._client.hello()
        host_id, _ = self._ingestor.ingest_handshake(hello)
        capabilities = {c["capability"]: c for c in hello.get("capabilities", [])}

        capability = capabilities.get(CAPABILITY_NETWORK_OBSERVE)
        if capability is None or capability["status"] == CapabilityStatus.UNAVAILABLE:
            raise AgentError(
                "capability_unavailable",
                f"{CAPABILITY_NETWORK_OBSERVE} is not available on this agent",
                {
                    "capability": CAPABILITY_NETWORK_OBSERVE,
                    "status": capability["status"] if capability else "absent",
                    "reason": capability.get("reason") if capability else None,
                    "host_id": host_id,
                },
            )

        payload = self._client.observe_interfaces(names)
        payload = {
            **payload,
            "observation": {
                **payload["observation"],
                "scope": "targeted" if names is not None else "inventory",
            },
        }
        providers, provider_error = self._observe_providers(capabilities)
        return self._ingestor.ingest_network_sweep(
            {**payload, "providers": providers, "provider_error": provider_error}
        )

    def refresh_containers(self) -> IngestResult:
        """Observe the containers on this host now, and record them.

        Its own cycle, separate from the interface one and for the same reason every provider
        read is separate: a Docker daemon that is slow, unreachable or refusing its socket
        must not cost LocalPlane its view of the host's links, and an agent with no Docker
        capability must not fail an observation of something else.

        Raises :class:`AgentError` when the agent cannot be reached or has no Docker
        capability. Nothing is written in that case: an empty container estate and an
        unreadable daemon are different answers, and a caller that cannot tell them apart
        will report one as the other.
        """
        hello = self._client.hello()
        host_id, _ = self._ingestor.ingest_handshake(hello)
        capabilities = {c["capability"]: c for c in hello.get("capabilities", [])}

        capability = capabilities.get(CAPABILITY_DOCKER_CONTAINERS_OBSERVE)
        if capability is None or capability["status"] == CapabilityStatus.UNAVAILABLE:
            raise AgentError(
                "capability_unavailable",
                f"{CAPABILITY_DOCKER_CONTAINERS_OBSERVE} is not available on this agent",
                {
                    "capability": CAPABILITY_DOCKER_CONTAINERS_OBSERVE,
                    "status": capability["status"] if capability else "absent",
                    "reason": capability.get("reason") if capability else None,
                    "host_id": host_id,
                },
            )

        payload = self._client.observe_containers()
        batch = payload["containers"]
        if batch.get("status") == "failed":
            # The agent reached the daemon's socket path and the daemon would not answer.
            # That is not an empty estate and it is not recorded as one.
            raise AgentError(
                "provider_error",
                f"docker could not be read: {batch.get('reason')}",
                {"provider": "docker", "reason": batch.get("reason"), **batch.get("detail", {})},
            )
        return self._ingestor.ingest_container_sweep(payload)

    def refresh_systemd_units(self) -> IngestResult:
        """Observe the bounded loaded-unit estate plus independent self-unit evidence."""
        hello, host_id, capabilities = self._handshake_for(
            CAPABILITY_SYSTEMD_UNITS_OBSERVE
        )
        payload = self._client.observe_systemd_units()
        try:
            resolution = self._client.resolve_agent_systemd_unit().get("resolution")
        except AgentError as exc:
            resolution = {
                "status": "failed",
                "method": "manager_get_unit_by_control_group",
                "observed_at": _now(),
                "reason": exc.code,
                "detail": exc.detail,
                "gaps": ["agent_unit_resolution"],
            }
        return self._ingestor.ingest_systemd_sweep(
            {**payload, "agent_unit_resolution": resolution}
        )

    def refresh_systemd_unit(self, unit_id: str) -> IngestResult:
        """Observe one loaded unit without running an inventory or loading a declaration."""
        self._handshake_for(CAPABILITY_SYSTEMD_UNITS_OBSERVE)
        payload = self._client.observe_systemd_unit(unit_id)
        targeted = payload["observation"]
        unit = targeted.get("unit")
        status = targeted.get("status")
        batch_status = "ok" if status in {"observed", "absent"} else "failed"
        payload = {
            "host_id": payload["host_id"],
            "agent_instance_id": payload.get("agent_instance_id"),
            "units": {
                "capability": targeted["capability"],
                "provider": targeted["provider"],
                "provider_version": targeted["provider_version"],
                "source": targeted["source"],
                "status": batch_status,
                "scope": "targeted",
                "requested_unit": targeted["requested_unit"],
                "started_at": targeted["started_at"],
                "completed_at": targeted["completed_at"],
                "reason": targeted.get("reason"),
                "units": [unit] if unit else [],
                "missing": [targeted["requested_unit"]] if status == "absent" else [],
                "issues": targeted.get("issues", []),
            },
        }
        return self._ingestor.ingest_systemd_sweep(payload)

    def _handshake_for(
        self, capability_name: str
    ) -> tuple[dict[str, Any], str, dict[str, dict[str, Any]]]:
        hello = self._client.hello()
        host_id, _ = self._ingestor.ingest_handshake(hello)
        capabilities = {c["capability"]: c for c in hello.get("capabilities", [])}
        capability = capabilities.get(capability_name)
        if capability is None or capability["status"] == CapabilityStatus.UNAVAILABLE:
            raise AgentError(
                "capability_unavailable",
                f"{capability_name} is not available on this agent",
                {
                    "capability": capability_name,
                    "status": capability["status"] if capability else "absent",
                    "reason": capability.get("reason") if capability else None,
                    "host_id": host_id,
                },
            )
        return hello, host_id, capabilities

    def _observe_providers(
        self, capabilities: dict[str, dict[str, Any]]
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Collect provider evidence, or the reason there is none. Never raises."""
        capability = capabilities.get(CAPABILITY_NETWORK_PROVIDERS_OBSERVE)
        if capability is None or capability["status"] == CapabilityStatus.UNAVAILABLE:
            return None, {
                "code": "capability_unavailable",
                "message": (
                    f"{CAPABILITY_NETWORK_PROVIDERS_OBSERVE} is not available on this agent, "
                    "so nothing can be established about who owns these objects"
                ),
                "detail": {
                    "capability": CAPABILITY_NETWORK_PROVIDERS_OBSERVE,
                    "status": capability["status"] if capability else "absent",
                    "reason": capability.get("reason") if capability else None,
                },
            }
        try:
            return self._client.observe_providers()["providers"], None
        except AgentError as exc:
            LOG.warning(
                "provider evidence not collected",
                extra={"code": exc.code, "error": exc.message},
            )
            return None, exc.as_dict()

    def handshake(self) -> dict[str, Any]:
        hello = self._client.hello()
        self._ingestor.ingest_handshake(hello)
        return hello


def _provider_rows(
    batch: dict[str, Any] | None,
    provider_error: dict[str, Any] | None,
    sweep_id: str,
    host_id: str,
    received_at: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Turn the agent's provider batch into rows, refusing anything incoherent.

    The store will not hold a reading that failed and carries records anyway, or one that
    failed without saying why — those are CHECK constraints, and they exist so that no code
    path can let a half-answer through. Enforcing them by rejecting the *sweep* would mean
    a quirk in one provider costing LocalPlane its view of every interface, so an incoherent
    reading is downgraded to an error reading with the reason saying what was wrong, and
    the sweep records the issue.
    """
    if batch is None:
        if provider_error is None:
            return [], []
        return [], [
            {
                "source": "agent.network.observe_providers",
                "code": str(provider_error.get("code", "provider_evidence_unavailable")),
                "message": str(
                    provider_error.get("message", "provider evidence could not be collected")
                ),
                "detail": provider_error.get("detail", {}),
            }
        ]

    rows: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = [dict(i) for i in batch.get("issues", [])]
    seen: set[tuple[str, str]] = set()

    for reading in batch.get("readings", []):
        provider = str(reading.get("provider", ""))
        source = str(reading.get("source", ""))
        if not provider or not source:
            issues.append(
                {
                    "source": "agent.network.observe_providers",
                    "code": "reading_unidentified",
                    "message": "a provider reading named neither a provider nor a source",
                    "detail": {},
                }
            )
            continue
        if (provider, source) in seen:
            issues.append(
                {
                    "source": source,
                    "code": "duplicate_reading",
                    "message": f"{provider} answered twice for {source} in one sweep",
                    "detail": {"provider": provider},
                }
            )
            continue
        seen.add((provider, source))

        status = str(reading.get("status", ""))
        reason = reading.get("reason")
        records = reading.get("records", [])
        if status not in tuple(ProviderStatus):
            issues.append(
                {
                    "source": source,
                    "code": "unknown_reading_status",
                    "message": f"{provider} reported a status this build does not know: {status!r}",
                    "detail": {"provider": provider, "status": status},
                }
            )
            status, reason, records = str(ProviderStatus.ERROR), "unknown_reading_status", []
        elif status != ProviderStatus.OK and (records or not reason):
            issues.append(
                {
                    "source": source,
                    "code": "incoherent_reading",
                    "message": (
                        f"{provider} reported {status} while carrying records or no reason"
                    ),
                    "detail": {"provider": provider, "status": status},
                }
            )
            status, reason, records = str(ProviderStatus.ERROR), "agent_reading_incoherent", []

        rows.append(
            {
                "provider_observation_id": f"pobs_{uuid.uuid4().hex}",
                "sweep_id": sweep_id,
                "host_id": host_id,
                "provider": provider,
                "source": source,
                "status": status,
                "reason": reason,
                "method": str(reading.get("method", "unknown")),
                "provider_version": reading.get("version"),
                "observed_at": str(reading.get("observed_at", received_at)),
                "received_at": received_at,
                "records": records,
                "detail": reading.get("detail", {}),
            }
        )
    return rows, issues


def _reading_views(rows: list[dict[str, Any]]) -> list[ProviderReadingView]:
    return [
        ProviderReadingView(
            provider=row["provider"],
            source=row["source"],
            status=row["status"],
            observed_at=row["observed_at"],
            reason=row["reason"],
            version=row["provider_version"],
            records=tuple(row["records"]),
            detail=row["detail"],
            provider_observation_id=row["provider_observation_id"],
            sweep_id=row["sweep_id"],
        )
        for row in rows
    ]


def _project_systemd_relationships(
    raw: Any,
    aliases: dict[str, str],
    historical_object_ids: dict[str, str],
    current_object_ids: dict[str, str],
) -> list[dict[str, Any]]:
    """Resolve typed edges against the coherent estate, not mere object history."""
    projected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for relationship in raw if isinstance(raw, list) else []:
        if not isinstance(relationship, dict):
            continue
        kind = relationship.get("kind")
        group = relationship.get("group")
        target = relationship.get("target_unit")
        if not all(isinstance(value, str) and value for value in (kind, group, target)):
            continue
        key = (kind, target)
        if key in seen:
            continue
        seen.add(key)
        canonical = aliases.get(target, target)
        target_object_id = historical_object_ids.get(canonical)
        current_target_id = current_object_ids.get(canonical)
        if current_target_id is not None:
            resolution = "resolved"
            estate_state: str | None = "current"
            target_object_id = current_target_id
        elif canonical.endswith((".service", ".socket", ".timer", ".target", ".path", ".mount")):
            resolution = "referenced"
            estate_state = "not_observed"
        else:
            resolution = "external"
            estate_state = None
        projected.append(
            {
                "kind": kind,
                "group": group,
                "target_unit": target,
                "canonical_target": canonical if canonical != target else None,
                "target_object_id": target_object_id,
                "resolution": resolution,
                "estate_state": estate_state,
                "source": "systemd.dbus",
            }
        )
    return projected


def _agent_unit_resolution_summary(resolution: Any) -> dict[str, Any] | None:
    if not isinstance(resolution, dict) or not resolution:
        return None
    return {
        "status": resolution.get("status"),
        "method": resolution.get("method"),
        "cgroup": resolution.get("cgroup"),
        "canonical_id": resolution.get("canonical_id"),
        "invocation_id": resolution.get("invocation_id"),
        "observed_at": resolution.get("observed_at"),
        "gaps": list(resolution.get("gaps") or []),
        "reason": resolution.get("reason"),
        "detail": dict(resolution.get("detail") or {}),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
