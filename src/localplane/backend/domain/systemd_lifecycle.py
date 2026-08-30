"""Pure domain rules for the systemd service lifecycle foundation.

Nothing in this module contacts systemd, opens a socket, or dispatches a job.  It owns the
closed action vocabulary and interprets already-normalised evidence gathered by the agent.
That division is important: the writer landed, and it consumes these same object, effect,
protection and applicability facts — ``systemd_operations`` plans and dispatches on the
types owned here — rather than a second lifecycle model.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Mapping

from localplane.protocol.docker_runtime_owner import (
    DOCKER_DIRECT_UNIX_CONTRACT,
    DOCKER_EXECUTION_CGROUP_RELATIONS,
    DOCKER_RUNTIME_OWNER_GAPS,
    DOCKER_RUNTIME_OWNER_METHOD_VERSION,
    DOCKER_RUNTIME_OWNER_PROVIDER,
    DockerRuntimeOwnerStatus,
)
from localplane.backend.domain.protection import (
    ManagementPathRelation,
    ProtectionReason,
    ProtectionStatus,
    ReasonAssessment,
    roll_up_protection,
)


SYSTEMD_SERVICE_LIFECYCLE = "systemd.service.lifecycle"
SYSTEMD_SERVICE_LIFECYCLE_CONTEXT_OBSERVE = (
    "systemd.service.lifecycle_context.observe"
)
SYSTEMD_LIFECYCLE_AUTHORITY = "systemd"
SYSTEMD_MANAGE_UNITS_ACTION = "org.freedesktop.systemd1.manage-units"


class SystemdServiceAction(StrEnum):
    """The entire initial lifecycle vocabulary.  There is no free-text variant."""

    START = "start"
    STOP = "stop"
    RESTART = "restart"


class AuthorizationState(StrEnum):
    """What preview established about one eventual dispatch."""

    NOT_PREFLIGHTED = "not_preflighted"


RuntimeOwnerStatus = DockerRuntimeOwnerStatus


@dataclass(frozen=True)
class RuntimeOwnerCorrelation:
    """Digestable semantic runtime-owner proof, excluding transient kernel handles."""

    contract_version: str
    method_version: int
    provider: str
    status: RuntimeOwnerStatus
    attestation_fingerprint: str | None = None
    endpoint: str | None = None
    container_id: str | None = None
    container_started_at: str | None = None
    engine_id: str | None = None
    direct_transport_verified: bool = False
    peer_service_main_verified: bool = False
    owner_unit_id: str | None = None
    owner_invocation_id: str | None = None
    execution_cgroup_relation: str | None = None
    gaps: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.contract_version != DOCKER_DIRECT_UNIX_CONTRACT:
            raise ValueError("unknown runtime-owner contract")
        if self.method_version != DOCKER_RUNTIME_OWNER_METHOD_VERSION:
            raise ValueError("unknown runtime-owner method version")
        if self.provider != DOCKER_RUNTIME_OWNER_PROVIDER:
            raise ValueError("unknown runtime-owner provider")
        if any(gap not in DOCKER_RUNTIME_OWNER_GAPS for gap in self.gaps):
            raise ValueError("unknown runtime-owner gap")
        if tuple(sorted(set(self.gaps))) != self.gaps:
            raise ValueError("runtime-owner gaps must be unique and sorted")
        if self.execution_cgroup_relation not in (
            None,
            *DOCKER_EXECUTION_CGROUP_RELATIONS,
        ):
            raise ValueError("unknown runtime-owner cgroup relation")
        _validate_runtime_owner_text_shapes(self)
        if self.status is RuntimeOwnerStatus.RESOLVED:
            required = (
                self.attestation_fingerprint,
                self.endpoint,
                self.container_id,
                self.container_started_at,
                self.engine_id,
                self.owner_unit_id,
                self.owner_invocation_id,
                self.execution_cgroup_relation,
            )
            if (
                any(not isinstance(item, str) or not item for item in required)
                or not self.direct_transport_verified
                or not self.peer_service_main_verified
                or self.gaps
            ):
                raise ValueError("resolved runtime-owner correlation is incomplete")
        elif (
            self.direct_transport_verified
            or self.peer_service_main_verified
            or any(
                value is not None
                for value in (
                    self.container_id,
                    self.container_started_at,
                    self.engine_id,
                    self.owner_unit_id,
                    self.owner_invocation_id,
                    self.execution_cgroup_relation,
                )
            )
            or not self.gaps
        ):
            raise ValueError("incomplete runtime-owner correlation claims authority")

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "method_version": self.method_version,
            "provider": self.provider,
            "status": str(self.status),
            "attestation_fingerprint": self.attestation_fingerprint,
            "endpoint": self.endpoint,
            "container_id": self.container_id,
            "container_started_at": self.container_started_at,
            "engine_id": self.engine_id,
            "direct_transport_verified": self.direct_transport_verified,
            "peer_service_main_verified": self.peer_service_main_verified,
            "owner_unit_id": self.owner_unit_id,
            "owner_invocation_id": self.owner_invocation_id,
            "execution_cgroup_relation": self.execution_cgroup_relation,
            "gaps": list(self.gaps),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RuntimeOwnerCorrelation":
        expected = {
            "contract_version",
            "method_version",
            "provider",
            "status",
            "attestation_fingerprint",
            "endpoint",
            "container_id",
            "container_started_at",
            "engine_id",
            "direct_transport_verified",
            "peer_service_main_verified",
            "owner_unit_id",
            "owner_invocation_id",
            "execution_cgroup_relation",
            "gaps",
        }
        if set(value) != expected:
            raise ValueError("runtime-owner correlation has unknown or missing fields")
        method_version = value.get("method_version")
        if isinstance(method_version, bool) or not isinstance(method_version, int):
            raise ValueError("runtime-owner method_version must be an integer")
        direct_transport = value.get("direct_transport_verified")
        service_main = value.get("peer_service_main_verified")
        if not isinstance(direct_transport, bool) or not isinstance(service_main, bool):
            raise ValueError("runtime-owner verdicts must be booleans")
        raw_gaps = value.get("gaps")
        if not isinstance(raw_gaps, list) or any(
            not isinstance(gap, str) for gap in raw_gaps
        ):
            raise ValueError("runtime-owner gaps must be a string list")
        return cls(
            contract_version=str(value["contract_version"]),
            method_version=method_version,
            provider=str(value["provider"]),
            status=RuntimeOwnerStatus(str(value["status"])),
            attestation_fingerprint=_optional_text(value.get("attestation_fingerprint")),
            endpoint=_optional_text(value.get("endpoint")),
            container_id=_optional_text(value.get("container_id")),
            container_started_at=_optional_text(value.get("container_started_at")),
            engine_id=_optional_text(value.get("engine_id")),
            direct_transport_verified=direct_transport,
            peer_service_main_verified=service_main,
            owner_unit_id=_optional_text(value.get("owner_unit_id")),
            owner_invocation_id=_optional_text(value.get("owner_invocation_id")),
            execution_cgroup_relation=_optional_text(
                value.get("execution_cgroup_relation")
            ),
            gaps=tuple(raw_gaps),
        )


@dataclass(frozen=True)
class AuthorizationAssessment:
    """The exact, deliberately non-authorising systemd preview assessment.

    systemd supplies trusted ``unit`` and ``verb`` details when it asks PolicyKit during
    dispatch.  The unprivileged LocalPlane agent cannot reproduce that exact decision in a
    non-mutating preflight, so preview records that limitation instead of dressing an
    empty-details PolicyKit answer up as authority.
    """

    state: AuthorizationState
    exact: bool
    authority: str
    decision_point: str
    action_id: str
    canonical_target: str
    verb: SystemdServiceAction
    reason: str

    @classmethod
    def not_preflighted(
        cls, canonical_target: str, verb: SystemdServiceAction
    ) -> "AuthorizationAssessment":
        return cls(
            state=AuthorizationState.NOT_PREFLIGHTED,
            exact=False,
            authority=SYSTEMD_LIFECYCLE_AUTHORITY,
            decision_point="dispatch",
            action_id=SYSTEMD_MANAGE_UNITS_ACTION,
            canonical_target=canonical_target,
            verb=verb,
            reason="unprivileged_caller_cannot_supply_trusted_polkit_unit_verb_details",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": str(self.state),
            "exact": self.exact,
            "authority": self.authority,
            "decision_point": self.decision_point,
            "action_id": self.action_id,
            "canonical_target": self.canonical_target,
            "verb": str(self.verb),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AuthorizationAssessment":
        return cls(
            state=AuthorizationState(str(value["state"])),
            exact=bool(value["exact"]),
            authority=str(value["authority"]),
            decision_point=str(value["decision_point"]),
            action_id=str(value["action_id"]),
            canonical_target=str(value["canonical_target"]),
            verb=SystemdServiceAction(str(value["verb"])),
            reason=str(value["reason"]),
        )


@dataclass(frozen=True)
class EffectEdge:
    """One effective systemd relationship used in the potential-effect closure."""

    source: str
    relation: str
    target: str

    def as_dict(self) -> dict[str, str]:
        return {"source": self.source, "relation": self.relation, "target": self.target}


@dataclass(frozen=True)
class SystemdLifecycleContext:
    """One fresh, bounded and request-scoped read used to plan a lifecycle action.

    This is observation evidence, not a systemd mirror.  Unit facts remain authoritative
    in Slice 11A's generic observations.  The context captures only the safety-significant
    proof made for one target, one action and one accepted TCP connection.
    """

    status: str
    observed_at: str
    target_unit: str
    action: SystemdServiceAction
    target_facts: Mapping[str, Any] = field(default_factory=dict)
    effect_units: tuple[str, ...] = ()
    effect_edges: tuple[EffectEdge, ...] = ()
    effect_complete: bool = False
    active_activation_sources: tuple[str, ...] = ()
    active_upholding_sources: tuple[str, ...] = ()
    management_units: tuple[str, ...] = ()
    management_complete: bool = False
    connection_unit: str | None = None
    """Which management unit is the one containing the backend this request reached.

    A member of ``management_units`` already; typed on its own because the set does not say
    which of its members hosts LocalPlane, and "this reaches a unit on the management path"
    and "this reaches the unit LocalPlane runs in" are different sentences.
    """

    connection_unit_type: str | None = None
    agent_unit: str | None = None
    agent_complete: bool = False
    agent_unit_type: str | None = None
    """The type of the agent's containing unit, carried rather than assumed.

    A resolution that succeeds says which unit contains the agent, not what kind of
    containment it is — and a ``.scope`` is a runtime's, which systemd need not publish an
    effect edge for.
    """

    gaps: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)
    runtime_owner: RuntimeOwnerCorrelation | None = None
    observation_id: str | None = None

    def __post_init__(self) -> None:
        owner = self.runtime_owner
        if owner is None:
            return
        if owner.status is RuntimeOwnerStatus.RESOLVED:
            if owner.owner_unit_id not in self.management_units:
                raise ValueError(
                    "resolved runtime owner is absent from management_units"
                )
        else:
            if self.management_complete:
                raise ValueError(
                    "incomplete runtime owner cannot complete the management path"
                )
            if "management_path.runtime_owner_unproven" not in self.gaps:
                raise ValueError("incomplete runtime owner is missing the umbrella gap")
            if not set(owner.gaps).issubset(self.gaps):
                raise ValueError("runtime-owner granular gaps are missing from context")

    @property
    def complete(self) -> bool:
        return (
            self.status == "complete"
            and self.effect_complete
            and self.management_complete
            and self.agent_complete
            and not self.gaps
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "observed_at": self.observed_at,
            "target_unit": self.target_unit,
            "action": str(self.action),
            "target_facts": dict(self.target_facts),
            "effect_units": list(self.effect_units),
            "effect_edges": [edge.as_dict() for edge in self.effect_edges],
            "effect_complete": self.effect_complete,
            "active_activation_sources": list(self.active_activation_sources),
            "active_upholding_sources": list(self.active_upholding_sources),
            "management_units": list(self.management_units),
            "management_complete": self.management_complete,
            "connection_unit": self.connection_unit,
            "connection_unit_type": self.connection_unit_type,
            "agent_unit": self.agent_unit,
            "agent_complete": self.agent_complete,
            "agent_unit_type": self.agent_unit_type,
            "gaps": list(self.gaps),
            "evidence": dict(self.evidence),
            "runtime_owner": (
                self.runtime_owner.as_dict() if self.runtime_owner is not None else None
            ),
            "observation_id": self.observation_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SystemdLifecycleContext":
        raw_runtime_owner = value.get("runtime_owner")
        if raw_runtime_owner is not None and not isinstance(
            raw_runtime_owner, Mapping
        ):
            raise ValueError("runtime_owner must be an object or null")
        edges = tuple(
            EffectEdge(
                source=str(edge["source"]),
                relation=str(edge["relation"]),
                target=str(edge["target"]),
            )
            for edge in value.get("effect_edges", ())
            if isinstance(edge, Mapping)
        )
        return cls(
            status=str(value.get("status", "failed")),
            observed_at=str(value.get("observed_at", "")),
            target_unit=str(value.get("target_unit", "")),
            action=SystemdServiceAction(str(value.get("action", "start"))),
            target_facts=(
                dict(value.get("target_facts", {}))
                if isinstance(value.get("target_facts"), Mapping)
                else {}
            ),
            effect_units=tuple(str(v) for v in value.get("effect_units", ())),
            effect_edges=edges,
            effect_complete=bool(value.get("effect_complete")),
            active_activation_sources=tuple(
                str(v) for v in value.get("active_activation_sources", ())
            ),
            active_upholding_sources=tuple(
                str(v) for v in value.get("active_upholding_sources", ())
            ),
            management_units=tuple(str(v) for v in value.get("management_units", ())),
            management_complete=bool(value.get("management_complete")),
            connection_unit=_optional_text(value.get("connection_unit")),
            connection_unit_type=_optional_text(value.get("connection_unit_type")),
            agent_unit=(
                str(value["agent_unit"]) if value.get("agent_unit") is not None else None
            ),
            agent_complete=bool(value.get("agent_complete")),
            agent_unit_type=_optional_text(value.get("agent_unit_type")),
            gaps=tuple(str(v) for v in value.get("gaps", ())),
            evidence=(
                dict(value.get("evidence", {}))
                if isinstance(value.get("evidence"), Mapping)
                else {}
            ),
            runtime_owner=(
                RuntimeOwnerCorrelation.from_dict(raw_runtime_owner)
                if isinstance(raw_runtime_owner, Mapping)
                else None
            ),
            observation_id=(
                str(value["observation_id"])
                if value.get("observation_id") is not None
                else None
            ),
        )


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("optional lifecycle text fields must be strings or null")
    return value


def _validate_runtime_owner_text_shapes(value: RuntimeOwnerCorrelation) -> None:
    if value.attestation_fingerprint is not None and not re.fullmatch(
        r"sha256:[0-9a-f]{64}", value.attestation_fingerprint
    ):
        raise ValueError("runtime-owner attestation fingerprint is malformed")
    if value.endpoint is not None and (
        not value.endpoint.startswith("/")
        or value.endpoint == "/"
        or any(character in value.endpoint for character in "\x00\r\n")
        or posixpath.normpath(value.endpoint) != value.endpoint
        or "//" in value.endpoint
        or any(part in {".", ".."} for part in PurePosixPath(value.endpoint).parts)
    ):
        raise ValueError("runtime-owner endpoint is malformed")
    if value.container_id is not None and not re.fullmatch(
        r"[0-9a-f]{64}", value.container_id
    ):
        raise ValueError("runtime-owner container id is malformed")
    if value.container_started_at is not None:
        try:
            started = datetime.fromisoformat(value.container_started_at)
        except ValueError as exc:
            raise ValueError("runtime-owner StartedAt is malformed") from exc
        if (
            "T" not in value.container_started_at
            or started.tzinfo is None
            or started.year <= 1
        ):
            raise ValueError("runtime-owner StartedAt is malformed")
    if value.owner_unit_id is not None and (
        not value.owner_unit_id.endswith(".service")
        or len(value.owner_unit_id) > 255
        or any(character in value.owner_unit_id for character in "/\x00\r\n")
    ):
        raise ValueError("runtime-owner Unit.Id is malformed")
    if value.owner_invocation_id is not None and (
        not re.fullmatch(r"[0-9a-f]{32}", value.owner_invocation_id)
        or set(value.owner_invocation_id) == {"0"}
    ):
        raise ValueError("runtime-owner InvocationID is malformed")


def assess_lifecycle_protection(context: SystemdLifecycleContext):
    """Evaluate management-path and LocalPlane-agent protection independently.

    ``clear`` is earned only by a complete disjointness proof.  An empty management-unit
    set with incomplete correlation is unknown, never a negative.
    """

    closure = frozenset(context.effect_units)
    management_intersection = sorted(closure.intersection(context.management_units))
    if management_intersection:
        management_status = ProtectionStatus.PROTECTED
        management_detail = "effect_closure_contains_management_path_unit"
    elif context.management_complete and context.effect_complete:
        management_status = ProtectionStatus.CLEAR
        management_detail = "effect_closure_disjoint_from_management_path_units"
    else:
        management_status = ProtectionStatus.UNKNOWN
        management_detail = "management_path_effect_correlation_incomplete"

    if context.agent_unit is not None and context.agent_unit in closure:
        agent_status = ProtectionStatus.PROTECTED
        agent_detail = "effect_closure_contains_localplane_agent_unit"
    elif context.agent_complete and context.effect_complete and context.agent_unit:
        agent_status = ProtectionStatus.CLEAR
        agent_detail = "effect_closure_disjoint_from_localplane_agent_unit"
    else:
        agent_status = ProtectionStatus.UNKNOWN
        agent_detail = "localplane_agent_effect_correlation_incomplete"

    evidence_common = {
        "target_unit": context.target_unit,
        "action": str(context.action),
        "effect_units": sorted(context.effect_units),
        "effect_edges": [e.as_dict() for e in context.effect_edges],
        "effect_complete": context.effect_complete,
        "context_gaps": sorted(context.gaps),
    }
    assessed = (
        ReasonAssessment(
            reason=ProtectionReason.MANAGEMENT_PATH,
            status=management_status,
            detail=management_detail,
            evidence_id=context.observation_id,
            observed_at=context.observed_at,
            evidence={
                **evidence_common,
                "management_units": sorted(context.management_units),
                "intersection": management_intersection,
                "management_complete": context.management_complete,
            },
            missing_evidence=(
                ()
                if management_status is not ProtectionStatus.UNKNOWN
                else tuple(sorted(context.gaps or ("management_path_unit",)))
            ),
        ),
        ReasonAssessment(
            reason=ProtectionReason.LOCALPLANE_AGENT,
            status=agent_status,
            detail=agent_detail,
            evidence_id=context.observation_id,
            observed_at=context.observed_at,
            evidence={
                **evidence_common,
                "agent_unit": context.agent_unit,
                "agent_complete": context.agent_complete,
            },
            missing_evidence=(
                ()
                if agent_status is not ProtectionStatus.UNKNOWN
                else tuple(sorted(context.gaps or ("localplane_agent_unit",)))
            ),
        ),
    )
    missing = tuple(
        sorted({gap for entry in assessed for gap in entry.missing_evidence})
    )
    return roll_up_protection(
        assessed,
        # A systemd unit is not a network-interface object.  The legacy relation is kept
        # unknown while the typed reason above carries the actual service-level proof.
        management_path=ManagementPathRelation.UNKNOWN,
        missing_evidence=missing,
    )


KNOWN_SERVICE_TYPES = frozenset(
    {"simple", "exec", "forking", "notify", "notify-reload", "dbus", "idle", "oneshot"}
)
TRANSITIONAL_ACTIVE_STATES = frozenset(
    {"activating", "deactivating", "reloading", "refreshing", "maintenance"}
)
MASKED_UNIT_FILE_STATES = frozenset({"masked", "masked-runtime"})


def lifecycle_applicability(
    context: SystemdLifecycleContext,
    *,
    expected_canonical_id: str,
) -> tuple[str, ...]:
    """Return typed refusal reasons for the conservative initial service subset."""

    facts = context.target_facts
    service = facts.get("service") if isinstance(facts.get("service"), Mapping) else {}
    reasons: list[str] = []

    canonical = facts.get("canonical_id")
    if context.target_unit != expected_canonical_id or canonical != expected_canonical_id:
        reasons.append("canonical_unit_mismatch")
    if facts.get("unit_type") != "service" or not expected_canonical_id.endswith(".service"):
        reasons.append("target_not_service")
    if facts.get("load_state") != "loaded":
        reasons.append("unit_not_loaded")
    if facts.get("transient") is not False:
        reasons.append("transient_or_unknown_service")
    if facts.get("current_job") is not None:
        reasons.append("systemd_job_in_progress")

    active = facts.get("active_state")
    if active in TRANSITIONAL_ACTIVE_STATES or active not in {"active", "inactive", "failed"}:
        reasons.append("service_state_not_stable_or_understood")

    service_type = service.get("type")
    if service_type not in KNOWN_SERVICE_TYPES:
        reasons.append("service_type_not_understood")
    if service_type == "oneshot" and service.get("remain_after_exit") is not True:
        reasons.append("oneshot_without_remain_after_exit")

    action = context.action
    start_needed = action in {SystemdServiceAction.START, SystemdServiceAction.RESTART}
    stop_needed = action in {SystemdServiceAction.STOP, SystemdServiceAction.RESTART}

    if action is SystemdServiceAction.START and active not in {"inactive", "failed"}:
        reasons.append("start_requires_inactive_or_failed")
    if action is SystemdServiceAction.STOP and active != "active":
        reasons.append("stop_requires_active")
    if action is SystemdServiceAction.RESTART and active != "active":
        reasons.append("restart_requires_active")

    if start_needed:
        if facts.get("unit_file_state") in MASKED_UNIT_FILE_STATES:
            reasons.append("service_masked")
        if facts.get("can_start") is not True:
            reasons.append("can_start_not_proven")
        if facts.get("refuse_manual_start") is not False:
            reasons.append("manual_start_not_permitted")
        if facts.get("need_daemon_reload") is not False:
            reasons.append("daemon_reload_needed_or_unknown")
    if stop_needed:
        if facts.get("can_stop") is not True:
            reasons.append("can_stop_not_proven")
        if facts.get("refuse_manual_stop") is not False:
            reasons.append("manual_stop_not_permitted")

    if action is SystemdServiceAction.RESTART and not facts.get("invocation_id"):
        reasons.append("restart_baseline_invocation_id_missing")
    if stop_needed and context.active_activation_sources:
        reasons.append("active_trigger_may_reactivate_service")
    if stop_needed and context.active_upholding_sources:
        reasons.append("active_upholder_may_reactivate_service")

    return tuple(dict.fromkeys(reasons))
