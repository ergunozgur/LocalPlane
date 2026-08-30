"""The systemd lifecycle vertical: what a plan publishes, and what it takes to execute one."""

from __future__ import annotations

import ast
import inspect
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from localplane.agent.capabilities import _probe_systemd_lifecycle
from localplane.agent.providers.systemd import SystemdProvider
from localplane.backend.db.repositories import ObjectRecord, ObservationRecord
from localplane.backend.domain.management_path import ManagementPathVerdict
from localplane.backend.domain.protection import ProtectionReason, ProtectionStatus
from localplane.backend.domain.guard import GuardAvailability
from localplane.backend.domain.provenance import OwnershipState, Provenance
from localplane.backend.domain.runs import (
    PLAN_DIGEST_VERSION,
    ExecutionAvailability,
    ExecutionEligibility,
    OperationType,
    PlanRefused,
    RecoveryMode,
    RiskTier,
    canonical_plan,
    plan_digest,
)
from localplane.backend.domain.systemd_lifecycle import (
    SYSTEMD_MANAGE_UNITS_ACTION,
    SYSTEMD_SERVICE_LIFECYCLE,
    AuthorizationAssessment,
    EffectEdge,
    RuntimeOwnerCorrelation,
    RuntimeOwnerStatus,
    SystemdLifecycleContext,
    SystemdServiceAction,
    assess_lifecycle_protection,
    lifecycle_applicability,
)
from localplane.backend.operations import OPERATIONS, build_executors
from localplane.backend.runs import PlanningContext
from localplane.backend.systemd_operations import DEFINITIONS
from localplane.protocol.capabilities import (
    CAPABILITY_SYSTEMD_SERVICE_LIFECYCLE,
    CAPABILITY_SYSTEMD_SERVICE_LIFECYCLE_CONTEXT_OBSERVE,
    CapabilityStatus,
)
from localplane.protocol.wire import MUTATING_METHODS
from tests.test_systemd import (
    FakeSystemdConnection,
    FakeSystemdState,
    _UNIT_LIFECYCLE_INTROSPECTION,
    _provider,
    _service,
    _unit,
)


NOW = "2026-08-27T09:00:00+00:00"


def _facts(*, active: str = "active", service: dict | None = None, **updates):
    facts = {
        "canonical_id": "target.service",
        "names": ["target.service", "target-alias.service"],
        "unit_type": "service",
        "load_state": "loaded",
        "active_state": active,
        "sub_state": "running" if active == "active" else "dead",
        "unit_file_state": "enabled",
        "can_start": True,
        "can_stop": True,
        "refuse_manual_start": False,
        "refuse_manual_stop": False,
        "need_daemon_reload": False,
        "transient": False,
        "current_job": None,
        "invocation_id": "01" * 16 if active == "active" else None,
        "service": {
            "type": "simple",
            "remain_after_exit": False,
        },
    }
    if service is not None:
        facts["service"] = service
    facts.update(updates)
    return facts


def _context(
    action: SystemdServiceAction,
    *,
    facts: dict | None = None,
    units: tuple[str, ...] = ("target.service", "unrelated.service"),
    management: tuple[str, ...] = ("backend.service",),
    agent: str | None = "agent-holder.service",
    complete: bool = True,
    gaps: tuple[str, ...] = (),
    active_sources: tuple[str, ...] = (),
    active_upholders: tuple[str, ...] = (),
    runtime_owner: RuntimeOwnerCorrelation | None = None,
    connection_unit: str | None = None,
    connection_unit_type: str | None = None,
    agent_unit_type: str | None = None,
    evidence: dict | None = None,
) -> SystemdLifecycleContext:
    return SystemdLifecycleContext(
        status="complete" if complete and not gaps else "partial",
        observed_at=NOW,
        target_unit="target.service",
        action=action,
        target_facts=facts or _facts(active="inactive" if action is SystemdServiceAction.START else "active"),
        effect_units=units,
        effect_edges=(EffectEdge("target.service", "Requires", "unrelated.service"),),
        effect_complete=complete,
        active_activation_sources=active_sources,
        active_upholding_sources=active_upholders,
        management_units=management,
        management_complete=complete,
        connection_unit=connection_unit,
        connection_unit_type=connection_unit_type,
        agent_unit=agent,
        agent_complete=complete and agent is not None,
        agent_unit_type=agent_unit_type,
        gaps=gaps,
        evidence=evidence or {},
        runtime_owner=runtime_owner,
        observation_id="obs_context",
    )


def _runtime_owner(**updates) -> RuntimeOwnerCorrelation:
    values = {
        "contract_version": "docker-direct-unix-v1",
        "method_version": 1,
        "provider": "docker",
        "status": RuntimeOwnerStatus.RESOLVED,
        "attestation_fingerprint": "sha256:" + "a" * 64,
        "endpoint": "/run/docker-direct.sock",
        "container_id": "b" * 64,
        "container_started_at": "2026-08-28T10:00:00.123456789Z",
        "engine_id": "opaque-engine-id",
        "direct_transport_verified": True,
        "peer_service_main_verified": True,
        "owner_unit_id": "docker-engine.service",
        "owner_invocation_id": "02" * 16,
        "execution_cgroup_relation": "ancestor",
        "gaps": (),
    }
    values.update(updates)
    return RuntimeOwnerCorrelation(**values)


def _record(facts: dict) -> ObjectRecord:
    observation = ObservationRecord(
        observation_id="obs_target",
        sweep_id="sweep_target",
        capability="systemd.units.observe",
        provider="systemd",
        provider_version="255",
        method="dbus_org.freedesktop.systemd1",
        fidelity="complete",
        observed_at=NOW,
        received_at=NOW,
        health_state="healthy",
        health_reason="service_running",
        gaps=[],
        facts=facts,
    )
    return ObjectRecord(
        object_id="obj_target",
        host_id="host_test",
        kind="systemd.unit",
        identity_basis="systemd_unit_id",
        identity_value="target.service",
        identity_confidence="high",
        display_name="target.service",
        management_state="observe_only",
        management_reason="systemd_runtime_authoritative",
        active_intent_id=None,
        first_seen_at=NOW,
        last_seen_at=NOW,
        observation=observation,
    )


def _planning(
    action: SystemdServiceAction,
    lifecycle: SystemdLifecycleContext | None = None,
) -> PlanningContext:
    lifecycle = lifecycle if lifecycle is not None else _context(action)
    return PlanningContext(
        now=NOW,
        freshness_ttl_s=60,
        object=_record(dict(lifecycle.target_facts)),
        intent=None,
        provenance=Provenance(
            state=OwnershipState.UNKNOWN,
            reason="systemd_runtime_authority",
        ),
        provider_readings={},
        evidence_sources=frozenset({CAPABILITY_SYSTEMD_SERVICE_LIFECYCLE}),
        open_drift={},
        management_path=ManagementPathVerdict(
            resource_id=None,
            reason="legacy_management_path_not_used_for_systemd_protection",
        ),
        systemd_lifecycle_context=lifecycle,
    )


def test_operation_registry_has_exact_closed_systemd_actions_and_one_executor_each():
    expected = {
        OperationType.SYSTEMD_SERVICE_START,
        OperationType.SYSTEMD_SERVICE_STOP,
        OperationType.SYSTEMD_SERVICE_RESTART,
    }
    assert expected <= set(OPERATIONS)
    assert {str(definition.operation) for definition in DEFINITIONS.values()} == {
        "systemd.service.start", "systemd.service.stop", "systemd.service.restart"
    }
    assert all(definition.target_kind == "systemd.unit" for definition in DEFINITIONS.values())
    assert all(definition.base_risk is RiskTier.MEDIUM for definition in DEFINITIONS.values())
    assert all(definition.recovery_mode is RecoveryMode.NONE for definition in DEFINITIONS.values())
    assert all(not definition.rollback_possible for definition in DEFINITIONS.values())
    # Three operations, three executors, and no fourth verb anywhere: the vocabulary is the
    # allowlist, and an executor exists for exactly the operations that declare one.
    executors = build_executors(object(), object(), object())
    assert expected <= set(executors)
    assert {
        operation for operation in executors if str(operation).startswith("systemd.")
    } == expected


@pytest.mark.parametrize("action", list(SystemdServiceAction))
def test_a_systemd_plan_is_executable_and_still_publishes_no_authorization(
    action: SystemdServiceAction,
):
    """Execution exists; permission is still systemd's to decide when it is asked.

    Availability and authorization answer different questions and always did. LocalPlane now
    has code that would carry this out, which is what `available` says. Whether systemd will
    let it is decided by PolicyKit at dispatch, with trusted unit and verb details an
    unprivileged caller cannot reproduce — so the preview goes on saying `not_preflighted`,
    and a refusal comes back later as a truthful `not_written`.

    The default fixture's target is not on any management path and has no self-impact, so
    this plan is ordinarily eligible. What it still does not have is a rollback, a guard or
    an armed recovery.
    """
    operation = OperationType(f"systemd.service.{action}")
    plan = OPERATIONS[operation].plan(_planning(action))
    assert not isinstance(plan, PlanRefused)
    assert plan.authorization == AuthorizationAssessment.not_preflighted(
        "target.service", action
    )
    assert plan.authorization.action_id == SYSTEMD_MANAGE_UNITS_ACTION
    assert plan.authorization.exact is False
    assert plan.authorization.decision_point == "dispatch"
    assert plan.execution.availability is ExecutionAvailability.AVAILABLE
    assert plan.execution.provider == "systemd"
    assert plan.recovery.mode is RecoveryMode.NONE
    assert plan.recovery.rollback_possible is False
    assert plan.recovery.armed is False
    assert plan.guard.availability is GuardAvailability.UNAVAILABLE
    assert plan.guard.window_s == 0
    assert plan.verification.executed is False


def test_authorization_assessment_round_trips_without_becoming_permission():
    assessment = AuthorizationAssessment.not_preflighted(
        "target.service", SystemdServiceAction.RESTART
    )
    restored = AuthorizationAssessment.from_dict(assessment.as_dict())
    assert restored == assessment
    assert assessment.as_dict() == {
        "state": "not_preflighted",
        "exact": False,
        "authority": "systemd",
        "decision_point": "dispatch",
        "action_id": "org.freedesktop.systemd1.manage-units",
        "canonical_target": "target.service",
        "verb": "restart",
        "reason": "unprivileged_caller_cannot_supply_trusted_polkit_unit_verb_details",
    }


def test_lifecycle_capability_is_available_without_claiming_authorization():
    class Diag:
        def probe(self):
            return {"status": "available", "reason": "fixture"}

    state = FakeSystemdState(
        units={"target.service": _unit("target.service")},
        typed={"target.service": _service()},
    )
    lifecycle, context = _probe_systemd_lifecycle(_provider(state), Diag())
    assert lifecycle.status is CapabilityStatus.AVAILABLE
    assert lifecycle.mutating is True
    assert lifecycle.detail["authorization_preflight"] == "not_preflighted"
    assert lifecycle.detail["loaded_unit_lookup"] == "Manager.GetUnit"
    assert lifecycle.detail["dispatch_interface"] == "org.freedesktop.systemd1.Unit"
    assert {"GetUnit", "Subscribe"} <= set(lifecycle.detail["manager_methods"])
    assert {"Start", "Stop", "Restart"} <= set(lifecycle.detail["unit_methods"])
    assert "JobRemoved" in lifecycle.detail["manager_signals"]
    assert lifecycle.reason is None
    assert context.status is CapabilityStatus.AVAILABLE
    assert context.mutating is False
    runtime_detail = context.detail["runtime_owner"]
    assert runtime_detail["contract"] == "docker-direct-unix-v1"
    assert runtime_detail["implementation"] == "supported"
    assert runtime_detail["kernel_peer_pidfd"]["status"] in {
        "available",
        "unavailable",
    }
    assert runtime_detail["systemd_pidfd_containment"]["status"] == "unavailable"
    assert runtime_detail["trusted_attestation"]["status"] in {
        "trusted",
        "unavailable",
        "untrusted",
    }
    assert runtime_detail["observation_required_for_completeness"] is True
    assert ("get_unit", "target.service") in state.calls
    assert any(call[0] == "unit_introspect" for call in state.calls)

    state.unit_lifecycle_introspection = _UNIT_LIFECYCLE_INTROSPECTION.replace(
        '<arg type="o" direction="out"/></method>\n    <method name="Stop">',
        '</method>\n    <method name="Stop">',
        1,
    )
    unavailable, still_readable = _probe_systemd_lifecycle(_provider(state), Diag())
    assert unavailable.status is CapabilityStatus.UNAVAILABLE
    assert "signature:unit_method:Start" in unavailable.detail["missing"]
    assert still_readable.status is CapabilityStatus.AVAILABLE


def test_manager_lifecycle_methods_do_not_substitute_for_unit_contract():
    class Diag:
        def probe(self):
            return {"status": "available", "reason": "fixture"}

    state = FakeSystemdState(
        units={"target.service": _unit("target.service")},
        typed={"target.service": _service()},
    )
    state.manager_lifecycle_introspection = (
        state.manager_lifecycle_introspection.replace(
            '<method name="Subscribe"/>',
            '<method name="StartUnit"/><method name="StopUnit"/>'
            '<method name="RestartUnit"/><method name="Subscribe"/>',
        )
    )
    state.unit_lifecycle_introspection = (
        state.unit_lifecycle_introspection.replace(
            '<method name="Restart"><arg type="s" direction="in"/>'
            '<arg type="o" direction="out"/></method>',
            "",
        )
    )
    lifecycle, _context_capability = _probe_systemd_lifecycle(
        _provider(state), Diag()
    )
    assert lifecycle.status is CapabilityStatus.UNAVAILABLE
    assert "unit_method:Restart" in lifecycle.detail["missing"]
    assert lifecycle.detail["manager_methods"] == ["GetUnit", "Subscribe"]
    assert "StartUnit" not in lifecycle.detail["manager_methods"]


@pytest.mark.parametrize(
    ("units", "management", "agent", "complete", "expected", "statuses"),
    [
        (
            ("target.service", "backend.service"),
            ("backend.service",),
            "agent-holder.service",
            True,
            ProtectionStatus.PROTECTED,
            (ProtectionStatus.PROTECTED, ProtectionStatus.CLEAR),
        ),
        (
            ("target.service", "agent-holder.service"),
            ("backend.service",),
            "agent-holder.service",
            True,
            ProtectionStatus.PROTECTED,
            (ProtectionStatus.CLEAR, ProtectionStatus.PROTECTED),
        ),
        (
            ("target.service",),
            ("backend.service",),
            "agent-holder.service",
            True,
            ProtectionStatus.CLEAR,
            (ProtectionStatus.CLEAR, ProtectionStatus.CLEAR),
        ),
        (
            ("target.service",),
            (),
            None,
            False,
            ProtectionStatus.UNKNOWN,
            (ProtectionStatus.UNKNOWN, ProtectionStatus.UNKNOWN),
        ),
    ],
)
def test_management_path_and_agent_protection_are_independent(
    units, management, agent, complete, expected, statuses
):
    lifecycle = _context(
        SystemdServiceAction.STOP,
        units=units,
        management=management,
        agent=agent,
        complete=complete,
        gaps=() if complete else ("containment_evidence_missing",),
    )
    protection = assess_lifecycle_protection(lifecycle)
    assert protection.status is expected
    by_reason = {entry.reason: entry for entry in protection.assessed}
    assert by_reason[ProtectionReason.MANAGEMENT_PATH].status is statuses[0]
    assert by_reason[ProtectionReason.LOCALPLANE_AGENT].status is statuses[1]


@pytest.mark.parametrize(
    ("action", "updates", "service", "active_sources", "reason"),
    [
        (SystemdServiceAction.START, {"active_state": "active"}, None, (), "start_requires_inactive_or_failed"),
        (SystemdServiceAction.STOP, {"active_state": "inactive"}, None, (), "stop_requires_active"),
        (SystemdServiceAction.RESTART, {"active_state": "failed"}, None, (), "restart_requires_active"),
        (SystemdServiceAction.START, {"transient": True}, None, (), "transient_or_unknown_service"),
        (SystemdServiceAction.START, {"need_daemon_reload": True}, None, (), "daemon_reload_needed_or_unknown"),
        (SystemdServiceAction.START, {"unit_file_state": "masked"}, None, (), "service_masked"),
        (SystemdServiceAction.RESTART, {"invocation_id": None}, None, (), "restart_baseline_invocation_id_missing"),
        (
            SystemdServiceAction.STOP,
            {},
            {"type": "oneshot", "remain_after_exit": False},
            (),
            "oneshot_without_remain_after_exit",
        ),
        (SystemdServiceAction.STOP, {}, None, ("target.socket",), "active_trigger_may_reactivate_service"),
    ],
)
def test_conservative_applicability_refuses_unsupported_shapes(
    action, updates, service, active_sources, reason
):
    active = "inactive" if action is SystemdServiceAction.START else "active"
    facts = _facts(active=active, service=service, **updates)
    lifecycle = _context(action, facts=facts, active_sources=active_sources)
    assert reason in lifecycle_applicability(
        lifecycle, expected_canonical_id="target.service"
    )


def test_oneshot_remain_after_exit_and_restart_baseline_are_supported():
    oneshot = _context(
        SystemdServiceAction.STOP,
        facts=_facts(
            active="active",
            service={"type": "oneshot", "remain_after_exit": True},
        ),
    )
    assert lifecycle_applicability(oneshot, expected_canonical_id="target.service") == ()
    restart = _context(SystemdServiceAction.RESTART)
    assert lifecycle_applicability(restart, expected_canonical_id="target.service") == ()


def test_digest_v5_binds_runtime_owner_while_v4_remains_recomputable():
    plan = OPERATIONS[OperationType.SYSTEMD_SERVICE_RESTART].plan(
        _planning(SystemdServiceAction.RESTART)
    )
    assert not isinstance(plan, PlanRefused)
    # Version 5 is still a form this build renders and verifies against; it is no longer the
    # current one, which is exactly what keeping it recomputable is for.
    assert PLAN_DIGEST_VERSION >= 5
    legacy = {version: plan_digest(plan, version) for version in (1, 2, 3)}
    changed = replace(
        plan,
        authorization=replace(plan.authorization, canonical_target="changed.service"),
        lifecycle_context=replace(
            plan.lifecycle_context,
            effect_units=(*plan.lifecycle_context.effect_units, "new-effect.service"),
        ),
    )
    assert {version: plan_digest(changed, version) for version in (1, 2, 3)} == legacy
    assert plan_digest(changed, 4) != plan_digest(plan, 4)
    canonical = canonical_plan(plan, 4)
    assert canonical["authorization"]["state"] == "not_preflighted"
    assert canonical["lifecycle_context"]["target_facts"]["invocation_id"] == "01" * 16
    assert canonical["protection"]["assessed"]

    owner_plan = replace(
        plan,
        lifecycle_context=replace(
            plan.lifecycle_context,
            runtime_owner=_runtime_owner(),
            management_units=("backend.scope", "docker-engine.service"),
        ),
    )
    assert plan_digest(owner_plan, 4) != plan_digest(plan, 4)
    without_owner = replace(owner_plan.lifecycle_context, runtime_owner=None)
    assert plan_digest(owner_plan, 4) == plan_digest(
        replace(owner_plan, lifecycle_context=without_owner), 4
    )
    assert plan_digest(owner_plan, 5) != plan_digest(
        replace(owner_plan, lifecycle_context=without_owner), 5
    )
    assert canonical_plan(owner_plan, 4)["lifecycle_context"].get("runtime_owner") is None
    assert canonical_plan(owner_plan, 5)["lifecycle_context"]["runtime_owner"] == (
        _runtime_owner().as_dict()
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("attestation_fingerprint", "sha256:" + "c" * 64),
        ("endpoint", "/run/other-docker.sock"),
        ("container_id", "d" * 64),
        ("container_started_at", "2026-08-28T10:01:00Z"),
        ("engine_id", "another-engine"),
        ("owner_unit_id", "other-engine.service"),
        ("owner_invocation_id", "03" * 16),
        ("execution_cgroup_relation", "equal"),
    ],
)
def test_digest_v5_changes_for_each_runtime_owner_semantic(field: str, value: str):
    plan = OPERATIONS[OperationType.SYSTEMD_SERVICE_RESTART].plan(
        _planning(SystemdServiceAction.RESTART)
    )
    assert not isinstance(plan, PlanRefused)
    owner = _runtime_owner()
    baseline = replace(
        plan,
        lifecycle_context=replace(
            plan.lifecycle_context,
            runtime_owner=owner,
            management_units=("backend.service", "docker-engine.service"),
        ),
    )
    changed = replace(
        baseline,
        lifecycle_context=replace(
            baseline.lifecycle_context,
            runtime_owner=replace(owner, **{field: value}),
            management_units=(
                "backend.service",
                value if field == "owner_unit_id" else "docker-engine.service",
            ),
        ),
    )
    assert plan_digest(changed, 5) != plan_digest(baseline, 5)


def test_digest_v5_binds_incomplete_status_gaps_and_management_result():
    plan = OPERATIONS[OperationType.SYSTEMD_SERVICE_RESTART].plan(
        _planning(SystemdServiceAction.RESTART)
    )
    assert not isinstance(plan, PlanRefused)
    resolved = replace(
        plan,
        lifecycle_context=replace(
            plan.lifecycle_context,
            runtime_owner=_runtime_owner(),
            management_units=("backend.service", "docker-engine.service"),
        ),
    )
    incomplete_owner = RuntimeOwnerCorrelation(
        contract_version="docker-direct-unix-v1",
        method_version=1,
        provider="docker",
        status=RuntimeOwnerStatus.INCOMPLETE,
        attestation_fingerprint="sha256:" + "a" * 64,
        endpoint="/run/docker-direct.sock",
        gaps=("management_path.runtime_owner.container_not_found",),
    )
    incomplete = replace(
        resolved,
        lifecycle_context=replace(
            resolved.lifecycle_context,
            status="partial",
            runtime_owner=incomplete_owner,
            management_complete=False,
            gaps=(
                "management_path.runtime_owner.container_not_found",
                "management_path.runtime_owner_unproven",
            ),
        ),
    )
    assert plan_digest(incomplete, 5) != plan_digest(resolved, 5)


def test_digest_v5_excludes_ephemeral_runtime_transport_evidence():
    plan = OPERATIONS[OperationType.SYSTEMD_SERVICE_RESTART].plan(
        _planning(SystemdServiceAction.RESTART)
    )
    assert not isinstance(plan, PlanRefused)
    first = replace(
        plan,
        lifecycle_context=replace(
            plan.lifecycle_context,
            runtime_owner=_runtime_owner(),
            management_units=("backend.service", "docker-engine.service"),
            evidence={
                "peer_pid": 41,
                "pidfd": 9,
                "socket_inode": 100,
                "raw_cgroup_path": "/docker/a",
            },
        ),
    )
    second = replace(
        first,
        lifecycle_context=replace(
            first.lifecycle_context,
            observed_at="2026-08-28T10:02:00+00:00",
            evidence={
                "peer_pid": 84,
                "pidfd": 12,
                "socket_inode": 200,
                "raw_cgroup_path": "/docker/recreated",
            },
        ),
    )
    assert plan_digest(first, 5) == plan_digest(second, 5)


def test_runtime_owner_round_trips_with_lifecycle_context_persistence_shape():
    context = _context(
        SystemdServiceAction.STOP,
        runtime_owner=_runtime_owner(),
        management=("backend.scope", "docker-engine.service"),
    )
    restored = SystemdLifecycleContext.from_dict(context.as_dict())
    assert restored == context


def test_effect_graph_is_typed_bounded_and_excludes_ordering_edges():
    target = _unit(
        "target.service",
        relationships={
            "Requires": ["dependency.service"],
            "Before": ["ordering-only.service"],
        },
    )
    dependency = _unit("dependency.service")
    state = FakeSystemdState(
        units={"target.service": target, "dependency.service": dependency},
        typed={"target.service": _service(), "dependency.service": _service()},
    )
    graph = _provider(state).observe_effect_graph("target.service", "start")
    assert graph.status == "complete"
    assert set(graph.units) == {"target.service", "dependency.service"}
    assert graph.edges == (
        {"source": "target.service", "relation": "Requires", "target": "dependency.service"},
    )
    assert not any(call[:2] == ("get_unit", "ordering-only.service") for call in state.calls)

    state.units["dependency.service"]["Requires"] = ["third.service"]
    state.units["third.service"] = _unit("third.service")
    state.typed["third.service"] = _service()
    bounded = _provider(state).observe_effect_graph(
        "target.service", "start", max_nodes=2
    )
    assert bounded.status == "partial"
    assert "effect_graph.node_limit" in bounded.gaps


def test_missing_effect_property_and_alias_target_never_become_clear():
    target = _unit(
        "target.service", relationships={"Requires": ["not-loaded.service"]}
    )
    del target["Upholds"]
    state = FakeSystemdState(
        units={"target.service": target},
        typed={"target.service": _service()},
        aliases={"target-alias.service": "target.service"},
    )
    graph = _provider(state).observe_effect_graph("target.service", "start")
    assert graph.status == "partial"
    assert "effect_graph.unsupported:Upholds:target.service" in graph.gaps
    assert "effect_graph.unresolved:not-loaded.service" in graph.gaps
    alias = _provider(state).observe_effect_graph("target-alias.service", "start")
    assert (alias.status, alias.reason, alias.canonical_target) == (
        "failed", "canonical_target_mismatch", "target.service"
    )


def test_active_systemd_trigger_is_reported_as_reactivation_evidence():
    target = _unit(
        "target.service", relationships={"TriggeredBy": ["target.socket"]}
    )
    state = FakeSystemdState(
        units={
            "target.service": target,
            "target.socket": _unit("target.socket", active="active", sub="listening"),
        },
        typed={"target.service": _service(), "target.socket": {}},
    )
    graph = _provider(state).observe_effect_graph("target.service", "stop")
    assert graph.status == "complete"
    assert graph.active_activation_sources == ("target.socket",)


def test_upheld_by_is_typed_continuous_reactivation_evidence():
    active_target = _unit(
        "target.service", relationships={"UpheldBy": ["upholder.service"]}
    )
    active_state = FakeSystemdState(
        units={
            "target.service": active_target,
            "upholder.service": _unit("upholder.service", active="active"),
        },
        typed={"target.service": _service(), "upholder.service": _service()},
    )
    active_graph = _provider(active_state).observe_effect_graph(
        "target.service", "stop"
    )
    assert active_graph.status == "complete"
    assert active_graph.active_upholding_sources == ("upholder.service",)
    context = _context(
        SystemdServiceAction.STOP,
        active_upholders=active_graph.active_upholding_sources,
    )
    assert "active_upholder_may_reactivate_service" in lifecycle_applicability(
        context, expected_canonical_id="target.service"
    )

    inactive_target = _unit(
        "target.service", relationships={"UpheldBy": ["upholder.service"]}
    )
    inactive_state = FakeSystemdState(
        units={
            "target.service": inactive_target,
            "upholder.service": _unit("upholder.service", active="inactive", sub="dead"),
        },
        typed={"target.service": _service(), "upholder.service": _service()},
    )
    inactive_graph = _provider(inactive_state).observe_effect_graph(
        "target.service", "stop"
    )
    assert inactive_graph.status == "complete"
    assert inactive_graph.active_upholding_sources == ()


@pytest.mark.parametrize(
    ("relation", "source"),
    (("TriggeredBy", "target.socket"), ("UpheldBy", "upholder.service")),
)
def test_stable_inactive_reactivation_source_without_job_is_a_valid_negative(
    relation: str, source: str
):
    target = _unit("target.service", relationships={relation: [source]})
    state = FakeSystemdState(
        units={
            "target.service": target,
            source: _unit(source, active="inactive", sub="dead"),
        },
        typed={"target.service": _service(), source: {}},
    )
    graph = _provider(state).observe_effect_graph("target.service", "stop")
    assert graph.status == "complete"
    assert graph.active_activation_sources == ()
    assert graph.active_upholding_sources == ()
    assert not any("reactivation_source_" in gap for gap in graph.gaps)


@pytest.mark.parametrize(
    ("relation", "source"),
    (("TriggeredBy", "target.socket"), ("UpheldBy", "upholder.service")),
)
@pytest.mark.parametrize(
    ("active_state", "job", "gap_kind", "expected_active"),
    (
        ("activating", (0, "/"), "state_changing", False),
        ("future-active-state", (0, "/"), "state_unknown", False),
        (None, (0, "/"), "state_unreadable", False),
        (
            "inactive",
            (17, "/org/freedesktop/systemd1/job/17"),
            "job_pending",
            False,
        ),
        (
            "active",
            (18, "/org/freedesktop/systemd1/job/18"),
            "job_pending",
            True,
        ),
        ("inactive", None, "job_unreadable", False),
    ),
)
def test_changing_or_unknown_reactivation_source_makes_graph_partial(
    relation: str,
    source: str,
    active_state: str | None,
    job: tuple[int, str] | None,
    gap_kind: str,
    expected_active: bool,
):
    target = _unit("target.service", relationships={relation: [source]})
    source_properties = _unit(source, active=active_state or "inactive")
    if active_state is None:
        del source_properties["ActiveState"]
    if job is None:
        del source_properties["Job"]
    else:
        source_properties["Job"] = job
    state = FakeSystemdState(
        units={"target.service": target, source: source_properties},
        typed={"target.service": _service(), source: {}},
    )
    graph = _provider(state).observe_effect_graph("target.service", "stop")
    assert graph.status == "partial"
    assert (
        f"effect_graph.reactivation_source_{gap_kind}:{relation}:{source}"
        in graph.gaps
    )
    active_sources = (
        graph.active_activation_sources
        if relation == "TriggeredBy"
        else graph.active_upholding_sources
    )
    assert active_sources == ((source,) if expected_active else ())


def test_missing_upheld_by_support_is_an_explicit_stop_graph_gap():
    target = _unit("target.service")
    del target["UpheldBy"]
    state = FakeSystemdState(
        units={"target.service": target},
        typed={"target.service": _service()},
    )
    graph = _provider(state).observe_effect_graph("target.service", "stop")
    assert graph.status == "partial"
    assert "effect_graph.unsupported:UpheldBy:target.service" in graph.gaps
    assert graph.active_upholding_sources == ()


def test_the_only_mutating_systemd_members_are_the_three_on_the_unit_object():
    """The systemd transport added `Start`, `Stop` and `Restart` and nothing beside them.

    Asserted from the calls the module can actually construct — `new_method_call`'s second
    argument *is* the D-Bus member — rather than from prose about them. The Manager's
    similarly named lifecycle methods and `EnqueueUnitJob` stay absent: the Unit object is
    the dispatch contract Part A proved by introspection, and `LoadUnit` stays absent
    because looking at an unloaded declaration must not change the manager's estate.

    The backend side of this is open now: the systemd operations declare execution
    available and an executor is registered, which the planner tests above assert, and
    this transport is the wire it dispatches through.
    """
    import localplane.agent.providers.systemd as provider_module

    tree = ast.parse(Path(inspect.getfile(provider_module)).read_text())
    members = {
        node.args[1].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "new_method_call"
        and len(node.args) > 1
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    }
    assert members & {"Start", "Stop", "Restart"} == {"Start", "Stop", "Restart"}
    assert not members & {
        "StartUnit", "StopUnit", "RestartUnit", "ReloadUnit", "TryRestartUnit",
        "ReloadOrRestartUnit", "EnqueueUnitJob", "LoadUnit", "KillUnit",
        "ResetFailedUnit", "SetUnitProperties", "StartTransientUnit",
        "EnableUnitFiles", "DisableUnitFiles", "MaskUnitFiles", "UnmaskUnitFiles",
        "Reload", "Reexecute",
    }
    assert {
        method for method in MUTATING_METHODS if method.startswith("systemd.")
    } == {"systemd.service_lifecycle"}
    # The backend capability name is unchanged and is not the protocol method's name.
    assert SYSTEMD_SERVICE_LIFECYCLE == "systemd.service.lifecycle"
