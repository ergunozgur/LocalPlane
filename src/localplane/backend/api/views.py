"""Assembling API responses from stored records.

Everything here is a pure translation from a record to a schema. The only computation is
freshness, which is derived at read time on purpose: a stored freshness would be wrong
the moment nothing happened.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import AbstractSet, Any, Sequence

from localplane.backend.api import schemas
from localplane.backend.db.repositories import (
    AgentRecord,
    CapabilityRecord,
    ChangeRecord,
    CheckpointRecord,
    ConfirmationRecord,
    FindingRecord,
    HostRecord,
    IntentRecord,
    ObjectRecord,
    OwnershipFindingRecord,
    RecoveryAttemptRecord,
    RunEventRecord,
    RunGuardRecord,
    RunRecord,
    SweepRecord,
    TransitionRecord,
)
from localplane.backend.changes import RecoveryHold
from localplane.backend.domain.changes import (
    HostEffect,
    RecoveryHoldState,
    VerificationOutcome,
)
from localplane.backend.domain.findings import (
    FINDING_TYPE_OWNERSHIP_CONFLICT,
    drift_summary,
    ownership_conflict_summary,
)
from localplane.backend.domain.guard import GuardPhase
from localplane.backend.domain.intent import ValueType
from localplane.backend.domain.management_path import (
    ManagementPathObservation,
    TransportEvidence,
)
from localplane.backend.domain.protection import (
    ProtectionAssessment,
    ManagementPathVerdict,
)
from localplane.backend.domain.provenance import (
    AdoptionEligibility,
    ConsultedSource,
    OwnershipClaim,
    OwnershipRelation,
    Provenance,
)
from localplane.backend.domain.policy import OperationDefinition
from localplane.backend.domain.reconciliation import Reconciliation
from localplane.backend.domain.runs import (
    ExecutionEligibility,
    PlannedAction,
    PlannedFieldChange,
    PlanValidity,
    RunPlan,
)
from localplane.backend.domain.docker import parse_docker_instant
from localplane.backend.domain.states import derive_freshness
from localplane.backend.domain.systemd import derive_systemd_health


def host_view(record: HostRecord, ttl_s: float) -> schemas.Host:
    last_seen = _parse(record.last_seen_at)
    freshness, age = derive_freshness(last_seen, ttl_s)
    return schemas.Host(
        host_id=record.host_id,
        identity_basis=record.identity_basis,
        identity_confidence=record.identity_confidence,
        hostname=record.hostname,
        configured_hostname=record.configured_hostname,
        boot_id=record.boot_id,
        os_id=record.os_id,
        os_version_id=record.os_version_id,
        os_pretty_name=record.os_pretty_name,
        kernel_name=record.kernel_name,
        kernel_release=record.kernel_release,
        architecture=record.architecture,
        identity_gaps=record.identity_gaps,
        first_seen_at=_parse(record.first_seen_at),
        last_seen_at=last_seen,
        freshness=freshness,
        age_seconds=age,
    )


def agent_identity_view(record: AgentRecord) -> schemas.AgentIdentity:
    return schemas.AgentIdentity(
        agent_instance_id=record.agent_instance_id,
        agent_version=record.agent_version,
        protocol_version=record.protocol_version,
        transport=record.transport,
        process_isolated=record.process_isolated,
        privilege=record.privilege,
        effective_uid=record.effective_uid,
        pid=record.pid,
        started_at=_parse(record.started_at),
        last_contact_at=_parse(record.last_contact_at),
    )


def capability_view(record: CapabilityRecord) -> schemas.Capability:
    return schemas.Capability(
        capability=record.capability,
        version=record.version,
        status=record.status,
        mutating=record.mutating,
        summary=record.summary,
        reason=record.reason,
        detail=record.detail,
        discovered_at=_parse(record.discovered_at),
    )


def capability_view_from_payload(payload: dict[str, Any]) -> schemas.Capability:
    return schemas.Capability(
        capability=payload["capability"],
        version=payload["version"],
        status=payload["status"],
        mutating=payload["mutating"],
        summary=payload["summary"],
        reason=payload.get("reason"),
        detail=payload.get("detail", {}),
        discovered_at=_parse(payload["discovered_at"]),
    )


def sweep_view(record: SweepRecord) -> schemas.Sweep:
    return schemas.Sweep(
        sweep_id=record.sweep_id,
        capability=record.capability,
        scope=record.scope,
        provider=record.provider,
        provider_version=record.provider_version,
        status=record.status,
        started_at=_parse(record.started_at),
        completed_at=_parse(record.completed_at),
        received_at=_parse(record.received_at),
        object_count=record.object_count,
        missing=record.missing,
        issues=[schemas.ProviderIssue(**issue) for issue in record.issues],
        agent_instance_id=record.agent_instance_id,
    )


def interface_view(
    record: ObjectRecord,
    ttl_s: float,
    latest_inventory_members: AbstractSet[str] | None,
    *,
    intent: IntentRecord | None,
    reconciliation: Reconciliation | None,
    provenance: Provenance,
    eligibility: AdoptionEligibility,
) -> schemas.NetworkInterface:
    """Assemble one interface.

    ``intent``, ``reconciliation``, ``provenance`` and ``eligibility`` are required
    arguments with no defaults on purpose. A caller that forgot to look them up would
    otherwise render a managed object as though it had no intent and could not drift, or a
    Docker-owned bridge as though nobody had ever asked — which are the most expensive
    quiet lies this API could tell, so the signature does not let anyone forget.
    """
    observation = record.observation
    facts: dict[str, Any] = observation.facts if observation else {}

    observation_view: schemas.Observation | None = None
    health: schemas.Health | None = None
    observed_in_latest_sweep: bool | None = None

    if observation is not None:
        observed_at = observation.observed_at_dt
        freshness, age = derive_freshness(observed_at, ttl_s)
        observation_view = schemas.Observation(
            observation_id=observation.observation_id,
            sweep_id=observation.sweep_id,
            observed_at=observed_at or datetime.now(timezone.utc),
            received_at=_parse(observation.received_at),
            freshness=freshness,
            age_seconds=age,
            provider=observation.provider,
            provider_version=observation.provider_version,
            method=observation.method,
            capability=observation.capability,
            fidelity=observation.fidelity,
            gaps=observation.gaps,
        )
        health = schemas.Health(
            state=observation.health_state, reason=observation.health_reason
        )
        if latest_inventory_members is not None:
            observed_in_latest_sweep = record.object_id in latest_inventory_members

    addresses = facts.get("addresses")
    statistics = facts.get("statistics")

    return schemas.NetworkInterface(
        object_id=record.object_id,
        kind=record.kind,
        name=record.display_name,
        interface_kind=facts.get("kind", "unknown"),
        identity=schemas.Identity(
            basis=record.identity_basis,
            value=record.identity_value,
            confidence=record.identity_confidence,
        ),
        management=schemas.Management(
            state=record.management_state, reason=record.management_reason
        ),
        ownership=ownership_view(provenance, eligibility),
        # null for anything that is not managed: with no retained intent there is nothing
        # to have drifted from, which is a different answer from in_sync.
        reconciliation=reconciliation.state if reconciliation is not None else None,
        intent=intent_summary_view(intent) if intent is not None else None,
        health=health,
        observation=observation_view,
        observed_in_latest_sweep=observed_in_latest_sweep,
        link=_link_view(facts) if observation is not None else None,
        addresses=None if addresses is None else [schemas.Address(**a) for a in addresses],
        statistics=None if statistics is None else schemas.Statistics(**statistics),
        first_seen_at=_parse(record.first_seen_at),
        last_seen_at=_parse(record.last_seen_at),
    )


def container_view(
    record: ObjectRecord,
    ttl_s: float,
    latest_inventory_members: AbstractSet[str] | None,
    *,
    provenance: Provenance,
    eligibility: AdoptionEligibility,
) -> schemas.DockerContainer:
    """Assemble one container.

    ``provenance`` and ``eligibility`` are required with no defaults for the same reason
    they are on an interface: a caller that forgot to look them up would render a container
    as though nobody had ever asked who owns it, and for a container the answer is never in
    doubt — it is Docker's, which is what refuses adoption and what makes the lifecycle
    operations executable.

    There is no ``intent`` and no ``reconciliation`` argument, and their absence is the
    model rather than an omission: LocalPlane retains no desired state for a container, so
    it cannot drift, and a field saying ``in_sync`` would be a claim nothing supports.
    """
    observation = record.observation
    facts: dict[str, Any] = observation.facts if observation else {}

    observation_view: schemas.Observation | None = None
    health: schemas.Health | None = None
    observed_in_latest_sweep: bool | None = None

    if observation is not None:
        observed_at = observation.observed_at_dt
        freshness, age = derive_freshness(observed_at, ttl_s)
        observation_view = schemas.Observation(
            observation_id=observation.observation_id,
            sweep_id=observation.sweep_id,
            observed_at=observed_at or datetime.now(timezone.utc),
            received_at=_parse(observation.received_at),
            freshness=freshness,
            age_seconds=age,
            provider=observation.provider,
            provider_version=observation.provider_version,
            method=observation.method,
            capability=observation.capability,
            fidelity=observation.fidelity,
            gaps=observation.gaps,
        )
        health = schemas.Health(
            state=observation.health_state, reason=observation.health_reason
        )
        if latest_inventory_members is not None:
            observed_in_latest_sweep = record.object_id in latest_inventory_members

    container_health = facts.get("health") or {}
    restart_policy = facts.get("restart_policy") or {}

    return schemas.DockerContainer(
        object_id=record.object_id,
        kind=record.kind,
        name=record.display_name,
        container_id=record.identity_value,
        short_id=facts.get("short_id") or record.identity_value[:12],
        identity=schemas.Identity(
            basis=record.identity_basis,
            value=record.identity_value,
            confidence=record.identity_confidence,
        ),
        management=schemas.Management(
            state=record.management_state, reason=record.management_reason
        ),
        ownership=ownership_view(provenance, eligibility),
        health=health,
        observation=observation_view,
        observed_in_latest_sweep=observed_in_latest_sweep,
        image=schemas.ContainerImage(
            reference=facts.get("image"), image_id=facts.get("image_id")
        ),
        created_at=parse_docker_instant(facts.get("created_at")),
        runtime=schemas.ContainerRuntime(
            state=facts.get("state"),
            running=facts.get("running"),
            paused=facts.get("paused"),
            restarting=facts.get("restarting"),
            exit_code=facts.get("exit_code"),
            error=facts.get("error"),
            oom_killed=facts.get("oom_killed"),
            pid=facts.get("pid"),
            started_at=parse_docker_instant(facts.get("started_at")),
            finished_at=parse_docker_instant(facts.get("finished_at")),
            restart_count=facts.get("restart_count"),
        ),
        container_health=schemas.ContainerHealth(
            checked=bool(container_health.get("checked")),
            status=container_health.get("status"),
            failing_streak=container_health.get("failing_streak"),
        ),
        restart_policy=schemas.ContainerRestartPolicy(
            name=restart_policy.get("name"),
            maximum_retry_count=restart_policy.get("maximum_retry_count"),
        ),
        network_mode=facts.get("network_mode"),
        networks=[schemas.ContainerNetwork(**n) for n in facts.get("networks") or []],
        ports=[schemas.ContainerPort(**p) for p in facts.get("ports") or []],
        mounts=[schemas.ContainerMount(**m) for m in facts.get("mounts") or []],
        labels=facts.get("labels") or {},
        labels_dropped=facts.get("labels_dropped") or 0,
        log_driver=facts.get("log_driver"),
        platform=facts.get("platform"),
        first_seen_at=_parse(record.first_seen_at),
        last_seen_at=_parse(record.last_seen_at),
    )


def container_logs_view(object_id: str, logs: dict[str, Any]) -> schemas.ContainerLogs:
    return schemas.ContainerLogs(
        object_id=object_id,
        container_id=logs["container_id"],
        read_at=_parse(logs["read_at"]),
        requested_lines=logs["requested_lines"],
        line_count=logs["line_count"],
        truncated=logs["truncated"],
        line_limit=logs["line_limit"],
        byte_limit=logs["byte_limit"],
        source=logs["source"],
        lines=[
            schemas.ContainerLogLine(
                timestamp=parse_docker_instant(entry.get("timestamp")),
                stream=entry.get("stream", "unknown"),
                message=entry.get("message", ""),
            )
            for entry in logs["lines"]
        ],
    )


def container_stats_view(object_id: str, stats: dict[str, Any]) -> schemas.ContainerStats:
    return schemas.ContainerStats(
        object_id=object_id,
        container_id=stats["container_id"],
        read_at=_parse(stats["read_at"]),
        sampled_at=parse_docker_instant(stats.get("sampled_at")),
        **{
            key: stats.get(key)
            for key in (
                "cpu_percent",
                "online_cpus",
                "memory_usage_bytes",
                "memory_usage_raw_bytes",
                "memory_limit_bytes",
                "memory_percent",
                "network_rx_bytes",
                "network_tx_bytes",
                "block_read_bytes",
                "block_write_bytes",
                "pids",
                "pids_limit",
            )
        },
        gaps=stats.get("gaps") or [],
    )


def systemd_trigger_context(
    records: Sequence[ObjectRecord], current_object_ids: AbstractSet[str]
) -> dict[str, tuple[str, ...]]:
    """Current active/listening socket evidence keyed by the service it activates."""
    active_sockets = {
        record.object_id: record.display_name
        for record in records
        if record.object_id in current_object_ids
        if record.observation is not None
        and record.observation.facts.get("unit_type") == "socket"
        and record.observation.facts.get("active_state") == "active"
        and record.observation.facts.get("sub_state") in {"listening", "running"}
    }
    triggered: dict[str, set[str]] = {}
    for record in records:
        if record.observation is None:
            continue
        for relationship in record.observation.facts.get("relationships") or []:
            if not isinstance(relationship, dict) or relationship.get("group") != "activation":
                continue
            if relationship.get("estate_state") != "current":
                continue
            target_id = relationship.get("target_object_id")
            if relationship.get("kind") == "Triggers" and record.object_id in active_sockets:
                if isinstance(target_id, str) and target_id in current_object_ids:
                    triggered.setdefault(target_id, set()).add(active_sockets[record.object_id])
            elif relationship.get("kind") == "TriggeredBy" and isinstance(target_id, str):
                socket_name = active_sockets.get(target_id)
                if socket_name is not None:
                    triggered.setdefault(record.object_id, set()).add(socket_name)
    return {object_id: tuple(sorted(names)) for object_id, names in triggered.items()}


def systemd_unit_view(
    record: ObjectRecord,
    ttl_s: float,
    latest_inventory_members: AbstractSet[str] | None,
    *,
    active_socket_triggers: Sequence[str] = (),
) -> schemas.SystemdUnit:
    observation = record.observation
    facts: dict[str, Any] = observation.facts if observation else {}
    observation_view: schemas.Observation | None = None
    health: schemas.Health | None = None
    observed_in_latest_sweep: bool | None = None
    if observation is not None:
        observed_at = observation.observed_at_dt
        freshness, age = derive_freshness(observed_at, ttl_s)
        observation_view = schemas.Observation(
            observation_id=observation.observation_id,
            sweep_id=observation.sweep_id,
            observed_at=observed_at or datetime.now(timezone.utc),
            received_at=_parse(observation.received_at),
            freshness=freshness,
            age_seconds=age,
            provider=observation.provider,
            provider_version=observation.provider_version,
            method=observation.method,
            capability=observation.capability,
            fidelity=observation.fidelity,
            gaps=observation.gaps,
        )
        verdict = derive_systemd_health(
            facts, active_socket_triggers=active_socket_triggers
        )
        health = schemas.Health(state=verdict.state, reason=verdict.reason)
        if latest_inventory_members is not None:
            observed_in_latest_sweep = record.object_id in latest_inventory_members

    def typed(name: str, model: Any) -> Any:
        value = facts.get(name)
        return model(**value) if isinstance(value, dict) else None

    containment = facts.get("agent_process_containment")
    return schemas.SystemdUnit(
        object_id=record.object_id,
        kind=record.kind,
        canonical_id=facts.get("canonical_id") or record.identity_value,
        names=facts.get("names"),
        description=facts.get("description"),
        unit_type=facts.get("unit_type") or "unknown",
        identity=schemas.Identity(
            basis=record.identity_basis,
            value=record.identity_value,
            confidence=record.identity_confidence,
        ),
        management=schemas.Management(
            state=record.management_state, reason=record.management_reason
        ),
        health=health,
        observation=observation_view,
        observed_in_latest_sweep=observed_in_latest_sweep,
        load_state=facts.get("load_state"),
        active_state=facts.get("active_state"),
        sub_state=facts.get("sub_state"),
        unit_file_state=facts.get("unit_file_state"),
        unit_file_preset=facts.get("unit_file_preset"),
        can_start=facts.get("can_start"),
        can_stop=facts.get("can_stop"),
        can_reload=facts.get("can_reload"),
        refuse_manual_start=facts.get("refuse_manual_start"),
        refuse_manual_stop=facts.get("refuse_manual_stop"),
        need_daemon_reload=facts.get("need_daemon_reload"),
        fragment_path=facts.get("fragment_path"),
        source_path=facts.get("source_path"),
        drop_in_paths=facts.get("drop_in_paths"),
        transient=facts.get("transient"),
        template=facts.get("template"),
        current_job=(
            schemas.SystemdJob(**facts["current_job"])
            if isinstance(facts.get("current_job"), dict)
            else None
        ),
        invocation_id=facts.get("invocation_id"),
        timestamps=dict(facts.get("timestamps") or {}),
        relationships=[
            schemas.SystemdRelationship(**relationship)
            for relationship in facts.get("relationships") or []
        ],
        service=typed("service", schemas.SystemdServiceFacts),
        socket=typed("socket", schemas.SystemdSocketFacts),
        timer=typed("timer", schemas.SystemdTimerFacts),
        path=typed("path", schemas.SystemdPathFacts),
        mount=typed("mount", schemas.SystemdMountFacts),
        agent_process_containment=(
            schemas.SystemdAgentContainment(**containment)
            if isinstance(containment, dict)
            else None
        ),
        first_seen_at=_parse(record.first_seen_at),
        last_seen_at=_parse(record.last_seen_at),
    )


def _link_view(facts: dict[str, Any]) -> schemas.Link:
    return schemas.Link(
        ifindex=facts.get("ifindex"),
        mtu=facts.get("mtu"),
        mac_address=facts.get("mac_address"),
        mac_is_permanent=facts.get("mac_is_permanent"),
        admin_up=facts.get("admin_up"),
        operstate=facts.get("operstate"),
        carrier=facts.get("carrier"),
        speed_mbps=facts.get("speed_mbps"),
        duplex=facts.get("duplex"),
        link_kind=facts.get("link_kind"),
        arphrd_type=facts.get("arphrd_type"),
        is_physical=facts.get("is_physical"),
        device_path=facts.get("device_path"),
        master=facts.get("master"),
        carrier_changes=facts.get("carrier_changes"),
    )


def _parse(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


# -------------------------------------------------------------------------- management


def intent_view(record: IntentRecord, active_intent_id: str | None) -> schemas.Intent:
    return schemas.Intent(
        intent_id=record.intent_id,
        object_id=record.object_id,
        host_id=record.host_id,
        version=record.version,
        supersedes=record.supersedes,
        schema_version=record.schema_version,
        origin=record.origin,
        revision=(
            schemas.IntentRevision(
                revision_id=record.revision.revision_id,
                kind=record.revision.kind,
                host_effect=record.revision.host_effect,
                occurred_at=_parse(record.revision.occurred_at),
            )
            if record.revision is not None
            else None
        ),
        active=record.intent_id == active_intent_id,
        captured_from=schemas.ObservationRef(
            observation_id=record.observation_id,
            sweep_id=record.sweep_id,
            observed_at=_parse(record.observed_at),
            capability=record.capability,
            provider=record.provider,
            provider_version=record.provider_version,
        ),
        created_at=_parse(record.created_at),
        controlled_fields=[
            schemas.IntentField(field=f.field, value_type=f.value_type, value=f.value)
            for f in record.fields
        ],
    )


def intent_summary_view(record: IntentRecord) -> schemas.IntentSummary:
    return schemas.IntentSummary(
        intent_id=record.intent_id,
        version=record.version,
        created_at=_parse(record.created_at),
        controlled_fields=[f.field for f in record.fields],
    )


def reconciliation_view(
    result: Reconciliation, intent: IntentRecord
) -> schemas.Reconciliation:
    """Render a comparison.

    ``as_of`` is now, not the observation time: this comparison was performed for this
    request. How old the *evidence* is is a separate fact, and it is on the observation
    reference where it belongs.
    """
    observation = None
    if result.observation_id is not None and result.sweep_id is not None:
        observation = schemas.ObservationRef(
            observation_id=result.observation_id,
            sweep_id=result.sweep_id,
            observed_at=_parse(result.observed_at or intent.observed_at),
            capability=intent.capability,
            provider=intent.provider,
            provider_version=intent.provider_version,
        )
    return schemas.Reconciliation(
        state=result.state,
        reason=result.reason,
        fields=[
            schemas.FieldComparison(
                field=f.field,
                value_type=str(f.value_type),
                intended=f.intended,
                observed=f.observed,
                comparison=str(f.comparison),
                reason=f.reason,
            )
            for f in result.fields
        ],
        observation=observation,
        as_of=datetime.now(timezone.utc),
    )


def transition_view(record: TransitionRecord) -> schemas.ManagementTransition:
    return schemas.ManagementTransition(
        transition_id=record.transition_id,
        transition=record.transition,
        from_state=record.from_state,
        to_state=record.to_state,
        intent_id=record.intent_id,
        observation_id=record.observation_id,
        host_effect=record.host_effect,
        occurred_at=_parse(record.occurred_at),
    )


def finding_view(record: FindingRecord, object_name: str) -> schemas.Finding:
    intended_type = ValueType(record.intended_type)
    observed = None
    if record.observed_value is not None and record.observed_type is not None:
        observed = schemas.TypedValue(
            value_type=record.observed_type, value=record.observed_value
        )
    return schemas.Finding(
        finding_id=record.finding_id,
        finding_key=record.finding_key,
        host_id=record.host_id,
        object_id=record.object_id,
        object_name=object_name,
        finding_type=record.finding_type,
        subject=record.subject,
        status=record.status,
        summary=drift_summary(
            object_name,
            record.subject,
            intended_type,
            record.intended_value,
            record.observed_value,
        ),
        evidence=schemas.FindingEvidence(
            intent_id=record.intent_id,
            field=record.subject,
            intended=schemas.TypedValue(
                value_type=record.intended_type, value=record.intended_value
            ),
            observed=observed,
            comparison=record.comparison,
            reason=record.reason,
            observation=record.observation_id,
            sweep=record.sweep_id,
        ),
        first_seen_at=_parse(record.first_seen_at),
        last_seen_at=_parse(record.last_seen_at),
        updated_at=_parse(record.updated_at),
        resolved_at=_parse(record.resolved_at) if record.resolved_at else None,
        resolution=record.resolution,
        resolved_by_observation_id=record.resolved_by_observation_id,
    )


# -------------------------------------------------------------------------- ownership


def owner_view(claim: OwnershipClaim) -> schemas.Owner:
    return schemas.Owner(
        provider=claim.owner.provider,
        instance=claim.owner.instance,
        label=claim.owner.label,
        version=claim.owner.version,
    )


def claim_summary_view(claim: OwnershipClaim) -> schemas.OwnershipClaimSummary:
    return schemas.OwnershipClaimSummary(
        relation=str(claim.relation),
        owner=owner_view(claim),
        confidence=str(claim.confidence),
        reason=claim.reason,
        evidence_sources=sorted({e.source for e in claim.evidence}),
    )


def eligibility_view(eligibility: AdoptionEligibility) -> schemas.AdoptionEligibility:
    blocked = eligibility.blocked_by
    return schemas.AdoptionEligibility(
        eligible=eligibility.eligible,
        reason=eligibility.reason,
        blocked_by=(
            schemas.Owner(
                provider=blocked.provider,
                instance=blocked.instance,
                label=blocked.label,
                version=blocked.version,
            )
            if blocked is not None
            else None
        ),
        evidence_gaps=list(eligibility.evidence_gaps),
    )


def ownership_view(
    provenance: Provenance, eligibility: AdoptionEligibility
) -> schemas.Ownership:
    """The compact assessment carried by every interface.

    One claim per relation, and ``None`` when there is not exactly one: two systems
    claiming to configure the same object is a condition to report, not one to resolve by
    picking a winner. ``reason`` says ``conflicting_claims`` when that happens, and the
    detail resource lists both.
    """
    created = provenance.owner_for(OwnershipRelation.CREATED_BY)
    configured = provenance.owner_for(OwnershipRelation.CONFIGURED_BY)
    return schemas.Ownership(
        state=str(provenance.state),
        reason=provenance.reason,
        created_by=claim_summary_view(created) if created is not None else None,
        configured_by=claim_summary_view(configured) if configured is not None else None,
        evidence_gaps=list(provenance.gaps),
        adoption=eligibility_view(eligibility),
    )


def provenance_view(
    record: ObjectRecord,
    provenance: Provenance,
    eligibility: AdoptionEligibility,
    ttl_s: float,
) -> schemas.Provenance:
    """The full assessment, with every claim's evidence and every source's outcome."""
    observation = record.observation
    return schemas.Provenance(
        object_id=record.object_id,
        name=record.display_name,
        management=schemas.Management(
            state=record.management_state, reason=record.management_reason
        ),
        state=str(provenance.state),
        reason=provenance.reason,
        claims=[
            schemas.OwnershipClaim(
                relation=str(claim.relation),
                owner=owner_view(claim),
                confidence=str(claim.confidence),
                reason=claim.reason,
                evidence=[
                    schemas.OwnershipEvidenceItem(
                        source=item.source,
                        kind=item.kind,
                        detail=item.detail,
                        observed_at=_parse(item.observed_at) if item.observed_at else None,
                    )
                    for item in claim.evidence
                ],
            )
            for claim in provenance.claims
        ],
        sources=[_source_view(source, ttl_s) for source in provenance.sources],
        adoption=eligibility_view(eligibility),
        observation=(
            schemas.ObservationRef(
                observation_id=observation.observation_id,
                sweep_id=observation.sweep_id,
                observed_at=_parse(observation.observed_at),
                capability=observation.capability,
                provider=observation.provider,
                provider_version=observation.provider_version,
            )
            if observation is not None
            else None
        ),
        as_of=datetime.now(timezone.utc),
    )


def _source_view(source: ConsultedSource, ttl_s: float) -> schemas.ConsultedSource:
    """Freshness is derived here, per source.

    A Docker reading from a minute ago and an interface observation from a second ago are
    different evidence with different ages, and an assessment that averaged them would
    hide exactly the skew somebody would want to see.
    """
    observed_at = _parse(source.observed_at) if source.observed_at else None
    freshness, age = (None, None)
    if observed_at is not None:
        freshness, age = derive_freshness(observed_at, ttl_s)
    return schemas.ConsultedSource(
        source=source.source,
        provider=source.provider,
        status=source.status,
        outcome=source.outcome,
        gap=source.gap,
        observed_at=observed_at,
        freshness=freshness,
        age_seconds=age,
        detail=source.detail,
    )


def ownership_finding_view(
    record: OwnershipFindingRecord, object_name: str
) -> schemas.Finding:
    return schemas.Finding(
        finding_id=record.finding_id,
        finding_key=record.finding_key,
        host_id=record.host_id,
        object_id=record.object_id,
        object_name=object_name,
        finding_type=record.finding_type,
        subject=record.subject,
        status=record.status,
        summary=ownership_conflict_summary(
            object_name,
            record.subject,
            record.owner_provider,
            record.owner_label,
            record.confidence,
        ),
        evidence=schemas.OwnershipFindingEvidence(
            intent_id=record.intent_id,
            relation=record.subject,
            owner=schemas.Owner(
                provider=record.owner_provider,
                instance=record.owner_instance,
                label=record.owner_label,
                version=None,
            ),
            confidence=record.confidence,
            evidence_source=record.evidence_source,
            reason=record.reason,
            provider_observation=record.provider_observation_id,
            observation=record.observation_id,
            sweep=record.sweep_id,
        ),
        first_seen_at=_parse(record.first_seen_at),
        last_seen_at=_parse(record.last_seen_at),
        updated_at=_parse(record.updated_at),
        resolved_at=_parse(record.resolved_at) if record.resolved_at else None,
        resolution=record.resolution,
        resolved_by_provider_observation_id=record.resolved_by_provider_observation_id,
    )


# --------------------------------------------------------------------------------- runs


_UNKNOWN_OPERATION_SUMMARY = (
    "this build no longer implements this operation, so what it would have done can only "
    "be read from the plan it published"
)


def run_view(
    run: RunRecord,
    plan: RunPlan,
    validity: PlanValidity,
    *,
    object_name: str,
    definition: OperationDefinition | None,
    note: str,
    confirmation: ConfirmationRecord | None = None,
    self_impact_override: ConfirmationRecord | None = None,
    checkpoint: CheckpointRecord | None = None,
    guard: RunGuardRecord | None = None,
    change: ChangeRecord | None = None,
    events: Sequence[RunEventRecord] = (),
    write_locked: bool = False,
) -> schemas.Run:
    """One Run and the whole of the plan it published.

    ``plan`` is the published document rebuilt from the row that stored it, not a fresh
    derivation — reading a Run must return what was shown, not what would be decided now.
    ``validity`` is the separately derived answer to whether that document still holds.
    """
    return schemas.Run(
        run_id=run.run_id,
        host_id=run.host_id,
        object_id=run.object_id,
        object_name=object_name,
        operation=run.operation,
        state=run.state,
        created_at=_parse(run.created_at),
        cancelled_at=_parse(run.cancelled_at) if run.cancelled_at else None,
        finished_at=_parse(run.finished_at) if run.finished_at else None,
        # `written` and nothing else. A change whose write may or may not have landed is
        # reported through `host_effect`, because a boolean cannot hold that answer and
        # rendering it as `true` or as `false` would both be false.
        host_mutated=run.host_effect == str(HostEffect.WRITTEN),
        host_effect=run.host_effect,
        change_created=change is not None,
        change_id=change.change_id if change else None,
        confirmation=confirmation_view(confirmation) if confirmation else None,
        checkpoint=checkpoint_view(checkpoint) if checkpoint else None,
        guard=guard_view(guard, checkpoint) if guard else None,
        # The transcript is listed once, on the Run, and not again inside the embedded
        # Change: one representation of one history. `GET /changes/{id}` carries it there.
        change=(
            change_view(change, object_name=object_name, write_locked=write_locked)
            if change
            else None
        ),
        events=[run_event_view(event) for event in events],
        note=note,
        preview=run_preview_view(
            run,
            plan,
            validity,
            object_name=object_name,
            definition=definition,
            confirmation=confirmation,
            self_impact_override=self_impact_override,
        ),
    )


def run_summary_view(
    run: RunRecord,
    validity: PlanValidity,
    *,
    object_name: str,
    change_id: str | None = None,
) -> schemas.RunSummary:
    preview = run.preview
    return schemas.RunSummary(
        run_id=run.run_id,
        host_id=run.host_id,
        object_id=run.object_id,
        object_name=object_name,
        operation=run.operation,
        state=run.state,
        created_at=_parse(run.created_at),
        cancelled_at=_parse(run.cancelled_at) if run.cancelled_at else None,
        finished_at=_parse(run.finished_at) if run.finished_at else None,
        host_effect=run.host_effect,
        change_created=change_id is not None,
        change_id=change_id,
        preview=schemas.RunPreviewSummary(
            preview_id=preview.preview_id,
            preview_digest=preview.preview_digest,
            published_at=_parse(preview.published_at),
            kind=preview.change_kind,
            field=preview.field,
            current=preview.current_value,
            desired=preview.desired_value,
            action=preview.action,
            observed_state=preview.observed_state,
            expected_state=preview.expected_state,
            risk_tier=preview.risk_tier,
            confirmation_required=preview.confirmation_required,
            execution_availability=preview.execution_availability,
            execution_eligibility=preview.execution_eligibility,
            blockers=list(preview.execution_blockers),
            validity=plan_validity_view(validity),
        ),
    )


def _planned_change_view(plan: RunPlan, object_name: str) -> schemas.PlannedChange:
    """WHAT a plan would do, in whichever of the two shapes it is.

    The half that does not apply is null rather than absent, so the document has one shape
    for every operation — and ``kind`` is what a reader branches on rather than probing for
    which fields happen to be populated.
    """
    common = {"object_id": plan.object_id, "object_name": object_name,
              "expected_after": plan.change.expected_after}
    if isinstance(plan.change, PlannedAction):
        return schemas.PlannedChange(
            **common,
            kind="action",
            action=plan.change.action,
            observed_state=plan.change.observed_state,
            expected_state=plan.change.expected_state,
        )
    return schemas.PlannedChange(
        **common,
        kind="field",
        field=plan.change.field,
        value_type=str(plan.change.value_type),
        current=plan.change.current,
        desired=plan.change.desired,
    )


def run_preview_view(
    run: RunRecord,
    plan: RunPlan,
    validity: PlanValidity,
    *,
    object_name: str,
    definition: OperationDefinition | None,
    confirmation: ConfirmationRecord | None = None,
    self_impact_override: ConfirmationRecord | None = None,
) -> schemas.RunPreview:
    """The published plan, answering what · why · how · evidence · risk · verify · recover.

    Three values are rendered here rather than stored: what the host would read back, what
    recovery would put back, and what verification would have to see. All three are the two
    ends of the planned change under different names, and carrying a second copy of each
    would let a future bug produce a document that disagreed with itself.
    """
    preview = run.preview
    return schemas.RunPreview(
        preview_id=preview.preview_id,
        preview_digest=preview.preview_digest,
        digest_version=preview.digest_version,
        published_at=_parse(preview.published_at),
        operation=schemas.RunOperation(
            type=str(plan.operation),
            summary=definition.summary if definition else _UNKNOWN_OPERATION_SUMMARY,
            target_kind=definition.target_kind if definition else "unknown",
        ),
        what=_planned_change_view(plan, object_name),
        why=schemas.PlanRationale(
            intent_id=plan.evidence.intent_id,
            intent_version=plan.evidence.intent_version,
            reason=(
                "controlled_field_differs"
                if isinstance(plan.change, PlannedFieldChange)
                else "operator_requested_action"
            ),
            drift_finding_id=plan.evidence.drift_finding_id,
        ),
        how=schemas.PlanExecution(
            availability=str(plan.execution.availability),
            eligibility=str(plan.execution.eligibility),
            blockers=list(plan.execution.blockers),
            provider=plan.execution.provider,
            required_capability=plan.execution.required_capability,
            capability_declared_by_agent=plan.execution.capability_declared_by_agent,
            note=definition.execution_note if definition else _UNKNOWN_OPERATION_SUMMARY,
        ),
        evidence=schemas.PlanEvidence(
            intent=(
                schemas.PlanIntentRef(
                    intent_id=plan.evidence.intent_id,
                    version=plan.evidence.intent_version or 0,
                    capability=plan.evidence.intent_capability or "",
                    provider=plan.evidence.intent_provider or "",
                )
                if plan.evidence.intent_id is not None
                else None
            ),
            observation=schemas.PlanObservationRef(
                observation_id=plan.evidence.observation_id,
                sweep_id=plan.evidence.sweep_id,
                observed_at=_parse(plan.evidence.observed_at),
            ),
            ownership=schemas.PlanOwnership(
                state=plan.ownership.state,
                reason=plan.ownership.reason,
                claims=[
                    schemas.PublishedOwnershipClaim(
                        relation=claim.relation,
                        provider=claim.provider,
                        instance=claim.instance,
                        label=claim.label,
                        confidence=claim.confidence,
                        external=claim.external,
                    )
                    for claim in plan.ownership.claims
                ],
                evidence_gaps=list(plan.ownership.gaps),
                provider_readings=dict(plan.ownership.readings),
            ),
        ),
        risk=schemas.PlanRisk(
            tier=str(plan.risk.tier),
            factors=[
                schemas.RiskFactor(code=f.code, floor=str(f.floor), detail=f.detail)
                for f in plan.risk.factors
            ],
        ),
        protection=schemas.PlanProtection(
            status=str(plan.protection.status),
            reasons=sorted(str(r) for r in plan.protection.reasons),
            unresolved=sorted(str(r) for r in plan.protection.unresolved),
            management_path=str(plan.protection.management_path),
            reason=plan.protection.reason,
            missing_evidence=list(plan.protection.missing_evidence),
            evidence_id=plan.protection.evidence_id,
            evidence_observed_at=_parse(plan.protection.evidence_observed_at)
            if plan.protection.evidence_observed_at
            else None,
            assessed=[
                schemas.PlanProtectionReason(
                    reason=str(entry.reason),
                    status=str(entry.status),
                    detail=entry.detail,
                    evidence_id=entry.evidence_id,
                    observed_at=_parse(entry.observed_at) if entry.observed_at else None,
                    evidence=dict(entry.evidence),
                    missing_evidence=list(entry.missing_evidence),
                )
                for entry in plan.protection.assessed
            ],
        ),
        authorization=(
            schemas.PlanAuthorization(**plan.authorization.as_dict())
            if plan.authorization is not None
            else None
        ),
        systemd_lifecycle_context=(
            schemas.PlanSystemdLifecycleContext(
                status=plan.lifecycle_context.status,
                target_unit=plan.lifecycle_context.target_unit,
                action=str(plan.lifecycle_context.action),
                effect_units=list(plan.lifecycle_context.effect_units),
                effect_edges=[
                    schemas.PlanEffectEdge(**edge.as_dict())
                    for edge in plan.lifecycle_context.effect_edges
                ],
                effect_complete=plan.lifecycle_context.effect_complete,
                active_activation_sources=list(
                    plan.lifecycle_context.active_activation_sources
                ),
                active_upholding_sources=list(
                    plan.lifecycle_context.active_upholding_sources
                ),
                management_units=list(plan.lifecycle_context.management_units),
                management_complete=plan.lifecycle_context.management_complete,
                connection_unit=plan.lifecycle_context.connection_unit,
                connection_unit_type=plan.lifecycle_context.connection_unit_type,
                agent_unit=plan.lifecycle_context.agent_unit,
                agent_complete=plan.lifecycle_context.agent_complete,
                agent_unit_type=plan.lifecycle_context.agent_unit_type,
                gaps=list(plan.lifecycle_context.gaps),
                restart_baseline_invocation_id=(
                    plan.lifecycle_context.target_facts.get("invocation_id")
                    if isinstance(
                        plan.lifecycle_context.target_facts.get("invocation_id"), str
                    )
                    else None
                ),
                runtime_owner=(
                    schemas.PlanRuntimeOwnerCorrelation(
                        **plan.lifecycle_context.runtime_owner.as_dict()
                    )
                    if plan.lifecycle_context.runtime_owner is not None
                    else None
                ),
            )
            if plan.lifecycle_context is not None
            else None
        ),
        self_impact=(
            schemas.PlanSelfImpact(
                **plan.self_impact.as_dict(),
                # Facts about the Run rather than about the immutable document: whether the
                # published plan's only write path is this authority, and whether somebody
                # has granted it. Nothing was issued either way.
                required=(
                    plan.execution.eligibility
                    is ExecutionEligibility.SELF_IMPACT_OVERRIDE_REQUIRED
                ),
                granted=self_impact_override is not None,
                consumed=(
                    self_impact_override is not None and self_impact_override.consumed
                ),
            )
            if plan.self_impact is not None
            else None
        ),
        confirmation=schemas.PlanConfirmation(
            required=plan.confirmation.required,
            method=str(plan.confirmation.method),
            source=plan.confirmation.source,
            reasons=list(plan.confirmation.reasons),
            policy=plan.confirmation.policy,
            token_issued=plan.confirmation.token_issued,
            # A fact about the Run, not about the immutable document: whether somebody has
            # actually confirmed this plan. Nothing was issued either way.
            satisfied=confirmation is not None,
            satisfiable=plan.confirmation.satisfiable,
            unsatisfiable_reason=plan.confirmation.unsatisfiable_reason,
        ),
        verification=schemas.PlanVerification(
            executed=plan.verification.executed,
            capability=plan.verification.capability,
            provider=plan.verification.provider,
            field=getattr(plan.change, "field", None),
            expect=plan.change.expected_after,
            condition=plan.verification.condition,
        ),
        guard=schemas.PlanGuard(
            availability=str(plan.guard.availability),
            reason=plan.guard.reason,
            window_s=plan.guard.window_s,
            prerequisites=[str(g) for g in plan.guard.prerequisites],
            unmet=[str(g) for g in plan.guard.unmet],
            # Never anything else on a published plan, and the store CHECKs the column.
            armed=False,
            guarantee=plan.guard.guarantee,
        ),
        recovery=schemas.PlanRecovery(
            mode=str(plan.recovery.mode),
            rollback_possible=plan.recovery.rollback_possible,
            # Rendered from the change rather than stored a second time, and null wherever
            # there is nothing to restore — which for an action is always.
            restores_field=(
                plan.change.field
                if plan.recovery.rollback_possible and isinstance(plan.change, PlannedFieldChange)
                else None
            ),
            restores_value=(
                plan.change.current
                if plan.recovery.rollback_possible and isinstance(plan.change, PlannedFieldChange)
                else None
            ),
            armed=plan.recovery.armed,
            guarantee=plan.recovery.guarantee,
            reason=plan.recovery.reason,
        ),
        validity=plan_validity_view(validity),
    )


def confirmation_view(record: ConfirmationRecord) -> schemas.RunConfirmation:
    return schemas.RunConfirmation(
        confirmation_id=record.confirmation_id,
        preview_id=record.preview_id,
        preview_digest=record.preview_digest,
        required_method=record.required_method,
        method=record.method,
        typed_statement=record.typed_statement,
        policy=record.policy,
        source=record.source,
        satisfied_at=_parse(record.satisfied_at),
        consumed=record.consumed,
        consumed_at=_parse(record.consumed_at) if record.consumed_at else None,
        consumed_by_attempt_id=record.consumed_by_attempt_id,
    )


def guard_view(
    record: RunGuardRecord, checkpoint: CheckpointRecord | None
) -> schemas.RunGuard:
    """The connection guard as it stands, with its phase from the record and not the clock.

    ``phase`` is what the holder last said, or ``arming`` / ``armed`` where nothing has
    settled it. ``window_lapsed`` is the one derived field, and it is deliberately *not*
    allowed to change the phase: a deadline passing means the reversal has probably
    happened, and "probably" is not something this API states as a fact about a host. What
    turns it into an answer is asking the holder, which is a `POST`.
    """
    if record.settled_phase is not None:
        phase = record.settled_phase
    elif record.armed:
        phase = str(GuardPhase.ARMED)
    else:
        phase = str(GuardPhase.ARMING)
    lapsed: bool | None = None
    if record.expires_at and record.settled_at is None:
        expires = _parse(record.expires_at)
        lapsed = expires is not None and expires <= datetime.now(timezone.utc)
    return schemas.RunGuard(
        guard_id=record.guard_id,
        phase=phase,
        holder_id=record.holder_id,
        window_s=record.window_s,
        armed_at=_parse(record.armed_at) if record.armed_at else None,
        expires_at=_parse(record.expires_at) if record.expires_at else None,
        window_lapsed=lapsed,
        restores_value=checkpoint.before_value if checkpoint else None,
        reversal_attempt_id=record.reversal_attempt_id,
        kept_at=_parse(record.kept_at) if record.kept_at else None,
        kept_evidence_id=record.kept_evidence_id,
        settled_at=_parse(record.settled_at) if record.settled_at else None,
        settled_reason=record.settled_reason,
        fired_at=_parse(record.fired_at) if record.fired_at else None,
        reversal_outcome=record.reversal_outcome,
        reversal_reason=record.reversal_reason,
    )


def checkpoint_view(record: CheckpointRecord) -> schemas.RunCheckpoint:
    return schemas.RunCheckpoint(
        checkpoint_id=record.checkpoint_id,
        field=record.field,
        restores_value=record.before_value,
        desired_value=record.desired_value,
        observation_id=record.observation_id,
        observed_at=_parse(record.observed_at),
        intent_id=record.intent_id,
        intent_version=record.intent_version,
        management_path=record.protection_management_path,
        evidence_id=record.protection_evidence_id,
        execution_correlation=dict(record.execution_correlation),
        armed_at=_parse(record.armed_at),
    )


def run_event_view(record: RunEventRecord) -> schemas.RunEventView:
    return schemas.RunEventView(
        sequence=record.sequence,
        event=record.event,
        state_from=record.state_from,
        state_to=record.state_to,
        occurred_at=_parse(record.occurred_at),
        change_id=record.change_id,
        detail=dict(record.detail),
    )


#: What a recovery-required Change has *not* established, by the reason it could not.
#: Written out rather than generated, because "we do not know whether our write landed" and
#: "we do not know whether our restoration landed" send an operator to different places.
_UNKNOWN_BY_REASON: dict[str, list[str]] = {
    "apply_write_unknown": [
        "whether LocalPlane's write reached the host",
        "what the host held when the process was interrupted",
    ],
    "rollback_write_unknown": [
        "whether the restoration reached the host",
        "whether the value now on the host is the one before the change",
    ],
    "rollback_not_dispatched": [
        "whether the original write reached the host",
        "the restoration never reached the privileged path, so nothing put the value back",
    ],
    "rollback_refused": [
        "the privileged path refused the restoration, so the value was not put back",
    ],
    "rollback_verification_failed": [
        "the value read back after the restoration is not the value before the change",
    ],
    "rollback_verification_unavailable": [
        "whether the restoration took effect — the host could not be read afterwards",
    ],
    "pre_rollback_read_unavailable": [
        "what the host holds now, so no restoration could be attempted safely",
    ],
    "target_absent_after_mutation": [
        "the object could not be observed after the change; it may no longer exist",
    ],
    "action_not_proven": [
        "the action was carried out and the resource is not in the state it promised",
        "no opposite verb was issued: the inverse of an action is a second change nobody "
        "asked for, against a resource whose state is already not what was expected",
    ],
    "action_verification_unavailable": [
        "whether the action produced the state it promised — the resource could not be read "
        "afterwards",
    ],
}


#: What a hold offers an operator next, by the state it is in. Written out rather than
#: assembled from conditionals scattered through a renderer: "what can I do about this" is the
#: question a recovery view exists to answer, and it should be one list a reader can trust.
_RETRY = "retry"
_CONFIRM_RETRY = "confirm_retry"
_RESOLVE = "resolve"


def recovery_attempt_view(
    record: RecoveryAttemptRecord, change: ChangeRecord
) -> schemas.RecoveryAttemptView:
    """One recovery action, with its two readings kept apart.

    ``evidence`` was taken before anything could be written and ``verification`` after a new
    write. Merging them would lose the difference between "the end state was already there"
    and "the end state is there because we wrote it again", which is the difference an
    operator most needs from this record.
    """
    return schemas.RecoveryAttemptView(
        attempt_id=record.attempt_id,
        sequence=record.sequence,
        kind=record.kind,
        started_at=_parse(record.started_at),
        finished_at=_parse(record.finished_at) if record.finished_at else None,
        outcome=record.outcome,
        refusal_code=record.refusal_code,
        releases_hold=record.releases_hold,
        management_path=record.protection_management_path,
        protection_evidence_id=record.protection_evidence_id,
        evidence=schemas.ChangeVerification(
            outcome=record.evidence_outcome,
            observation_id=record.evidence_observation_id,
            observed_value=record.evidence_observed_value,
            observed_state=record.evidence_observed_state,
            expected_value=change.desired_value,
            expected_state=change.expected_state,
            reason=record.evidence_reason,
        ),
        mutation=(
            None if record.mutation_attempt_id is None
            else schemas.ChangeMutation(
                outcome=record.mutation_outcome,
                reason=record.mutation_reason,
                provider=record.mutation_provider,
                method=record.mutation_method,
                attempt_id=record.mutation_attempt_id,
                dispatch_began_at=(
                    _parse(record.dispatch_began_at) if record.dispatch_began_at else None
                ),
                settled_at=_parse(record.finished_at) if record.finished_at else None,
                detail=dict(record.mutation_detail),
            )
        ),
        host_effect=record.host_effect,
        verification=(
            None if record.mutation_attempt_id is None
            else schemas.ChangeVerification(
                outcome=record.verification_outcome,
                observation_id=record.verification_observation_id,
                observed_value=record.verification_observed_value,
                observed_state=record.verification_observed_state,
                expected_value=change.desired_value,
                expected_state=change.expected_state,
                reason=record.verification_reason,
            )
        ),
        confirmation_id=record.confirmation_id,
        operator_statement=record.operator_statement,
        note=record.note,
    )


def _available_recovery_actions(
    hold: RecoveryHold, authority: ConfirmationRecord | None
) -> list[str]:
    """What an operator may do about this hold right now.

    ``retry`` is always offered while a hold is open, because the first thing it does is
    look — and looking is often the whole answer. ``confirm_retry`` is offered only while no
    authority is outstanding, because authority does not accumulate.
    """
    if hold.state is not RecoveryHoldState.UNRESOLVED:
        return []
    actions = [_RETRY]
    if authority is None:
        actions.append(_CONFIRM_RETRY)
    return [*actions, _RESOLVE]


def _last_observed(attempts: Sequence[RecoveryAttemptRecord]) -> dict[str, Any]:
    """The newest reading any recovery action took. Empty until one has been taken.

    **Which phase is newest is decided by whether verification was attempted, not by whether
    it produced an observation.** A post-write read that could not be taken at all —
    ``observation_unavailable``, ``target_absent`` — has no observation id, and choosing the
    phase on that column would silently fall back to the *pre-write* evidence and report a
    reading from before the mutation as the latest thing LocalPlane saw. That is the one
    answer this field must never give: the operator would be shown the state before a write
    that has since happened.
    """
    for record in reversed(attempts):
        source = (("verification", record.verification_outcome,
                   record.verification_observation_id, record.verification_observed_value,
                   record.verification_observed_state, record.verification_reason)
                  if record.verification_outcome != str(VerificationOutcome.NOT_ATTEMPTED)
                  else ("evidence", record.evidence_outcome, record.evidence_observation_id,
                        record.evidence_observed_value, record.evidence_observed_state,
                        record.evidence_reason))
        phase, outcome, observation_id, value, state, reason = source
        if outcome == str(VerificationOutcome.NOT_ATTEMPTED):
            continue
        return {
            "attempt_id": record.attempt_id, "phase": phase, "outcome": outcome,
            "observation_id": observation_id,
            "value": value, "state": state, "reason": reason,
            "proves_intended_state": outcome == str(VerificationOutcome.VERIFIED),
        }
    return {}


def change_view(
    record: ChangeRecord,
    *,
    object_name: str,
    events: Sequence[RunEventRecord] = (),
    write_locked: bool = False,
    hold: RecoveryHold | None = None,
    attempts: Sequence[RecoveryAttemptRecord] = (),
    authority: ConfirmationRecord | None = None,
) -> schemas.Change:
    """One Change, and everything that is known and not known about what it did."""
    hold = hold or RecoveryHold(
        state=(RecoveryHoldState.UNRESOLVED if record.recovery_required
               else RecoveryHoldState.NOT_REQUIRED),
        reason=record.recovery_reason,
        object_write_locked=write_locked,
    )
    return schemas.Change(
        change_id=record.change_id,
        run_id=record.run_id,
        preview_id=record.preview_id,
        checkpoint_id=record.checkpoint_id,
        host_id=record.host_id,
        object_id=record.object_id,
        object_name=object_name,
        operation=record.operation,
        change_kind=record.change_kind,
        field=record.field,
        before_value=record.before_value,
        desired_value=record.desired_value,
        action=record.action,
        observed_state=record.observed_state,
        expected_state=record.expected_state,
        created_at=_parse(record.created_at),
        host_effect=record.host_effect,
        host_mutated=record.host_effect == str(HostEffect.WRITTEN),
        mutation=schemas.ChangeMutation(
            outcome=record.mutation_outcome,
            reason=record.mutation_reason,
            provider=record.mutation_provider,
            method=record.mutation_method,
            attempt_id=record.apply_attempt_id,
            dispatch_began_at=(
                _parse(record.dispatch_began_at) if record.dispatch_began_at else None
            ),
            settled_at=_parse(record.settled_at) if record.settled_at else None,
            detail=dict(record.mutation_detail),
        ),
        verification=schemas.ChangeVerification(
            outcome=record.verification_outcome,
            observation_id=record.verification_observation_id,
            observed_value=record.verification_observed_value,
            observed_state=record.verification_observed_state,
            expected_value=record.desired_value,
            expected_state=record.expected_state,
            reason=record.verification_reason,
        ),
        rollback=schemas.ChangeRollback(
            required=record.rollback_required,
            attempt_id=record.rollback_attempt_id,
            dispatch_began_at=(
                _parse(record.rollback_dispatch_began_at)
                if record.rollback_dispatch_began_at
                else None
            ),
            outcome=record.rollback_outcome,
            reason=record.rollback_reason,
            restores_value=record.before_value,
            verification=schemas.ChangeVerification(
                outcome=record.rollback_verification_outcome,
                observation_id=record.rollback_verification_observation_id,
                observed_value=record.rollback_verification_observed_value,
                expected_value=record.before_value,
                reason=None,
            ),
            detail=dict(record.rollback_detail),
        ),
        recovery=schemas.ChangeRecovery(
            required=record.recovery_required,
            state=str(hold.state),
            reason=record.recovery_reason,
            known={
                "before_value": record.before_value,
                "desired_value": record.desired_value,
                "mutation_outcome": record.mutation_outcome,
                "rollback_outcome": record.rollback_outcome,
                "last_read_value": (
                    record.rollback_verification_observed_value
                    if record.rollback_verification_observed_value is not None
                    else record.verification_observed_value
                ),
                "last_read_observation_id": (
                    record.rollback_verification_observation_id
                    or record.verification_observation_id
                ),
            },
            unknown=_UNKNOWN_BY_REASON.get(record.recovery_reason or "", []),
            object_write_locked=hold.object_write_locked,
            released_at=_parse(hold.released_at) if hold.released_at else None,
            released_by=hold.released_by,
            released_by_attempt_id=hold.released_by_attempt_id,
            last_observed=_last_observed(attempts),
            attempts=[recovery_attempt_view(a, record) for a in attempts],
            authority=(
                None if authority is None
                else schemas.RecoveryAuthority(
                    confirmation_id=authority.confirmation_id,
                    required_method=authority.required_method,
                    method=authority.method,
                    policy=authority.policy,
                    source=authority.source,
                    satisfied_at=_parse(authority.satisfied_at),
                )
            ),
            available_actions=_available_recovery_actions(hold, authority),
        ),
        result=record.result,
        finished_at=_parse(record.finished_at) if record.finished_at else None,
        events=[run_event_view(event) for event in events],
    )


def change_summary_view(
    record: ChangeRecord, *, object_name: str, recovery_state: str,
) -> schemas.ChangeSummary:
    """A Change as a list renders it, with whether its hold is still open.

    ``recovery_state`` is passed in rather than derived here, because deriving it needs the
    attempt history and a renderer that reached into the store would be a read path with a
    query hidden inside it.
    """
    return schemas.ChangeSummary(
        change_id=record.change_id,
        run_id=record.run_id,
        object_id=record.object_id,
        object_name=object_name,
        operation=record.operation,
        change_kind=record.change_kind,
        field=record.field,
        before_value=record.before_value,
        desired_value=record.desired_value,
        action=record.action,
        expected_state=record.expected_state,
        created_at=_parse(record.created_at),
        finished_at=_parse(record.finished_at) if record.finished_at else None,
        host_effect=record.host_effect,
        mutation_outcome=record.mutation_outcome,
        verification_outcome=record.verification_outcome,
        rollback_outcome=record.rollback_outcome,
        result=record.result,
        recovery_required=record.recovery_required,
        recovery_reason=record.recovery_reason,
        recovery_state=recovery_state,
    )


def plan_validity_view(validity: PlanValidity) -> schemas.PlanValidity:
    return schemas.PlanValidity(
        state=str(validity.state),
        reasons=[
            schemas.ValidityReason(code=reason.code, detail=reason.detail)
            for reason in validity.reasons
        ],
        as_of=datetime.now(timezone.utc),
    )


# ----------------------------------------------------------------------- management path


_PROTECTION_NOTE = (
    "Protection is a different axis from ownership: ownership asks whose this object is, "
    "protection asks what changing it would put at risk. `clear` is scoped to the reasons "
    "this build implements and is not a word for `safe`."
)

_OBSERVED_NOTE = (
    "Read-only with respect to the host. The kernel was asked which route it would use to "
    "reach this connection's peer — a query, not a change — and nothing was created, "
    "modified, brought up or brought down."
)

_NOT_RECORDED_NOTE = (
    "Nothing was recorded and the host was not contacted: this transport cannot establish "
    "a management path, so there was no evidence to take. A record that looks like evidence "
    "and proves nothing is worse than no record at all."
)


def transport_view(transport: TransportEvidence) -> schemas.TransportEvidenceView:
    return schemas.TransportEvidenceView(
        peer_address=transport.peer_address,
        peer_family=transport.peer_family,
        local_endpoint_address=transport.local_address,
        local_endpoint_family=transport.local_family,
        usable=transport.usable,
        reason=transport.unusable_reason,
    )


def management_path_evidence_view(
    observation: ManagementPathObservation,
) -> schemas.ManagementPathEvidence:
    route = observation.route
    return schemas.ManagementPathEvidence(
        observation_id=observation.observation_id,
        host_id=observation.host_id,
        observed_at=_parse(observation.observed_at),
        agent_instance_id=observation.agent_instance_id,
        capability=observation.capability,
        provider=observation.provider,
        provider_version=observation.provider_version,
        method=observation.method,
        transport_peer_address=observation.transport_peer_address,
        local_endpoint_address=observation.local_endpoint_address,
        family=observation.local_endpoint_family,
        route=schemas.RouteEvidenceView(
            status=route.status,
            reason=route.reason,
            family=route.family,
            destination=route.destination,
            destination_prefix_length=route.destination_prefix_length,
            preferred_source=route.preferred_source,
            gateway=route.gateway,
            oif_index=route.oif_index,
            table=route.table,
            route_type=route.route_type,
            scope=route.scope,
            protocol=route.protocol,
            priority=route.priority,
            error=route.error,
        ),
    )


def management_path_view(
    verdict: ManagementPathVerdict,
    *,
    host_id: str,
    transport: TransportEvidence,
    object_name: str | None,
    evidence: ManagementPathObservation | None,
    ttl_s: float,
) -> schemas.ManagementPath:
    """The management path as it stands for the connection this request arrived on."""
    return schemas.ManagementPath(
        host_id=host_id,
        state="confirmed" if verdict.confirmed else "unresolved",
        object_id=verdict.resource_id,
        object_name=object_name,
        reason=verdict.reason,
        missing_evidence=list(verdict.missing_evidence),
        transport=transport_view(transport),
        evidence=None if evidence is None else management_path_evidence_view(evidence),
        evidence_ttl_seconds=ttl_s,
        as_of=datetime.now(timezone.utc),
    )


def object_protection_view(
    protection: ProtectionAssessment, *, object_id: str, object_name: str
) -> schemas.ObjectProtection:
    return schemas.ObjectProtection(
        object_id=object_id,
        object_name=object_name,
        status=str(protection.status),
        reasons=sorted(str(r) for r in protection.reasons),
        unresolved=sorted(str(r) for r in protection.unresolved),
        management_path=str(protection.management_path),
        reason=protection.reason,
        missing_evidence=list(protection.missing_evidence),
        assessed=[
            schemas.ProtectionReasonView(
                reason=str(entry.reason),
                status=str(entry.status),
                detail=entry.detail,
                evidence_id=entry.evidence_id,
                observed_at=_parse(entry.observed_at) if entry.observed_at else None,
            )
            for entry in protection.assessed
        ],
        # Reasons are resource- and operation-specific.  A lifecycle-only reason must not
        # silently broaden what an interface's ``clear`` verdict claims was evaluated.
        implemented_reasons=sorted({str(entry.reason) for entry in protection.assessed}),
        note=_PROTECTION_NOTE,
        as_of=datetime.now(timezone.utc),
    )
