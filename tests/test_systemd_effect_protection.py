"""Provider graph evidence must retain its safety meaning in backend planning."""

from __future__ import annotations

import pytest

from localplane.agent.systemd_lifecycle_context import LifecycleContextObservation
from localplane.backend.domain.protection import ProtectionReason, ProtectionStatus
from localplane.backend.domain.runs import (
    ExecutionEligibility,
    OperationType,
    PlanRefused,
)
from localplane.backend.domain.systemd_lifecycle import (
    SystemdLifecycleContext,
    SystemdServiceAction,
    assess_lifecycle_protection,
    lifecycle_applicability,
)
from localplane.backend.operations import OPERATIONS
from tests.test_systemd import FakeSystemdState, _provider, _service, _unit
from tests.test_systemd_lifecycle import _planning


NOW = "2026-11-02T09:00:00+00:00"


def _state(units: dict[str, dict]) -> FakeSystemdState:
    return FakeSystemdState(
        units=units,
        typed={name: _service() if name.endswith(".service") else {} for name in units},
    )


def _context_from_graph(
    graph,
    action: SystemdServiceAction,
    *,
    management: tuple[str, ...] = ("backend.service",),
    agent: str | None = "agent.service",
    management_complete: bool = True,
    agent_complete: bool = True,
    extra_gaps: tuple[str, ...] = (),
) -> SystemdLifecycleContext:
    """Use the provider's public graph result, not a second graph implementation."""
    assert graph.target is not None
    gaps = tuple(sorted({*graph.gaps, *extra_gaps}))
    complete = graph.complete and management_complete and agent_complete and not gaps
    raw = LifecycleContextObservation(
        status="complete" if complete else "partial",
        observed_at=NOW,
        target_unit="target.service",
        action=str(action),
        target_facts=dict(graph.target.facts),
        effect_units=graph.units,
        effect_edges=graph.edges,
        effect_complete=graph.complete,
        active_activation_sources=graph.active_activation_sources,
        active_upholding_sources=graph.active_upholding_sources,
        management_units=management,
        management_complete=management_complete,
        agent_unit=agent,
        agent_complete=agent_complete,
        gaps=gaps,
        evidence={"effect_graph": graph.as_dict()},
    ).as_dict()
    raw["observation_id"] = "obs_effect_contract"
    return SystemdLifecycleContext.from_dict(raw)


def _statuses(
    context: SystemdLifecycleContext,
) -> dict[ProtectionReason, ProtectionStatus]:
    return {
        entry.reason: entry.status
        for entry in assess_lifecycle_protection(context).assessed
    }


@pytest.mark.parametrize(
    "action", (SystemdServiceAction.START, SystemdServiceAction.RESTART)
)
def test_shared_slice_start_and_restart_are_disjoint_when_complete(
    action: SystemdServiceAction,
):
    target_active = "inactive" if action is SystemdServiceAction.START else "active"
    state = _state(
        {
            "target.service": _unit(
                "target.service",
                active=target_active,
                sub="dead" if target_active == "inactive" else "running",
                relationships={"Requires": ["system.slice"]},
            ),
            # A Start job for this slice must not become a reverse stop propagation.
            "system.slice": _unit(
                "system.slice",
                relationships={"RequiredBy": ["backend.service", "agent.service"]},
            ),
            "backend.service": _unit("backend.service"),
            "agent.service": _unit("agent.service"),
        }
    )
    graph = _provider(state).observe_effect_graph("target.service", str(action))
    context = _context_from_graph(graph, action)

    assert graph.complete
    assert set(graph.units) == {"target.service", "system.slice"}
    assert _statuses(context) == {
        ProtectionReason.MANAGEMENT_PATH: ProtectionStatus.CLEAR,
        ProtectionReason.LOCALPLANE_AGENT: ProtectionStatus.CLEAR,
    }


def test_disjoint_graph_with_incomplete_correlations_is_unknown_and_ineligible():
    state = _state(
        {
            "target.service": _unit("target.service", active="inactive", sub="dead"),
            "backend.service": _unit("backend.service"),
            "agent.service": _unit("agent.service"),
        }
    )
    graph = _provider(state).observe_effect_graph("target.service", "start")
    context = _context_from_graph(
        graph,
        SystemdServiceAction.START,
        management_complete=False,
        agent_complete=False,
        extra_gaps=("management_path.proof_missing", "localplane_agent.proof_missing"),
    )

    assert _statuses(context) == {
        ProtectionReason.MANAGEMENT_PATH: ProtectionStatus.UNKNOWN,
        ProtectionReason.LOCALPLANE_AGENT: ProtectionStatus.UNKNOWN,
    }
    plan = OPERATIONS[OperationType.SYSTEMD_SERVICE_START].plan(
        _planning(SystemdServiceAction.START, context)
    )
    assert not isinstance(plan, PlanRefused)
    assert plan.execution.eligibility is ExecutionEligibility.BLOCKED


@pytest.mark.parametrize(
    "relation", ("RequiredBy", "RequisiteOf", "BoundBy", "ConsistsOf")
)
@pytest.mark.parametrize(
    "action", (SystemdServiceAction.STOP, SystemdServiceAction.RESTART)
)
@pytest.mark.parametrize("protected", ("management", "agent"))
def test_genuine_reverse_stop_couplings_remain_protected_independently(
    relation: str, action: SystemdServiceAction, protected: str
):
    protected_unit = "backend.service" if protected == "management" else "agent.service"
    state = _state(
        {
            "target.service": _unit(
                "target.service", relationships={relation: [protected_unit]}
            ),
            "backend.service": _unit("backend.service"),
            "agent.service": _unit("agent.service"),
        }
    )
    graph = _provider(state).observe_effect_graph("target.service", str(action))
    context = _context_from_graph(graph, action)
    statuses = _statuses(context)

    assert graph.complete
    assert protected_unit in graph.units
    assert statuses[ProtectionReason.MANAGEMENT_PATH] is (
        ProtectionStatus.PROTECTED
        if protected == "management"
        else ProtectionStatus.CLEAR
    )
    assert statuses[ProtectionReason.LOCALPLANE_AGENT] is (
        ProtectionStatus.PROTECTED if protected == "agent" else ProtectionStatus.CLEAR
    )


@pytest.mark.parametrize("protected", ("management", "agent"))
def test_start_conflict_then_stop_reverse_dependency_is_protected(protected: str):
    protected_unit = "backend.service" if protected == "management" else "agent.service"
    state = _state(
        {
            "target.service": _unit(
                "target.service",
                active="inactive",
                sub="dead",
                relationships={"Conflicts": ["conflict.service"]},
            ),
            "conflict.service": _unit(
                "conflict.service", relationships={"RequiredBy": [protected_unit]}
            ),
            "backend.service": _unit("backend.service"),
            "agent.service": _unit("agent.service"),
        }
    )
    graph = _provider(state).observe_effect_graph("target.service", "start")
    context = _context_from_graph(graph, SystemdServiceAction.START)

    assert graph.complete
    assert protected_unit in graph.units
    assert _statuses(context)[
        ProtectionReason.MANAGEMENT_PATH
        if protected == "management"
        else ProtectionReason.LOCALPLANE_AGENT
    ] is ProtectionStatus.PROTECTED


@pytest.mark.parametrize(
    ("relation", "reverse"), (("Requires", "RequiredBy"), ("BindsTo", "BoundBy"))
)
@pytest.mark.parametrize(
    "action", (SystemdServiceAction.START, SystemdServiceAction.RESTART)
)
def test_forward_start_effects_do_not_pull_reverse_stop_dependents(
    relation: str, reverse: str, action: SystemdServiceAction
):
    target_active = "inactive" if action is SystemdServiceAction.START else "active"
    state = _state(
        {
            "target.service": _unit(
                "target.service",
                active=target_active,
                sub="dead" if target_active == "inactive" else "running",
                relationships={relation: ["dependency.service"]},
            ),
            "dependency.service": _unit(
                "dependency.service", relationships={reverse: ["backend.service"]}
            ),
            "backend.service": _unit("backend.service"),
            "agent.service": _unit("agent.service"),
        }
    )
    graph = _provider(state).observe_effect_graph("target.service", str(action))
    context = _context_from_graph(graph, action)

    assert graph.complete
    assert set(graph.units) == {"target.service", "dependency.service"}
    assert (
        _statuses(context)[ProtectionReason.MANAGEMENT_PATH]
        is ProtectionStatus.CLEAR
    )


@pytest.mark.parametrize(
    "case", ("alias", "bounds", "stop_when_unneeded", "handler", "queue")
)
def test_partial_effect_evidence_never_earns_clear_protection(case: str):
    action = SystemdServiceAction.STOP
    target = _unit("target.service")
    units = {
        "target.service": target,
        "backend.service": _unit("backend.service"),
        "agent.service": _unit("agent.service"),
    }
    kwargs = {}
    if case == "alias":
        target["RequiredBy"] = ["backend-alias.service"]
        state = _state(units)
        state.aliases["backend-alias.service"] = "backend.service"
    elif case == "bounds":
        target["RequiredBy"] = ["backend.service"]
        state = _state(units)
        kwargs["max_nodes"] = 1
    elif case == "stop_when_unneeded":
        target["Requires"] = ["released.service"]
        released = _unit("released.service")
        released["StopWhenUnneeded"] = True
        state = _state({**units, "released.service": released})
    elif case == "handler":
        target["OnFailure"] = ["handler.service"]
        target["OnFailureJobMode"] = "isolate"
        state = _state({**units, "handler.service": _unit("handler.service")})
    else:
        target["Job"] = (19, "/org/freedesktop/systemd1/job/19")
        state = _state(units)

    graph = _provider(state).observe_effect_graph(
        "target.service", str(action), **kwargs
    )
    context = _context_from_graph(graph, action)

    assert not graph.complete
    assert all(
        status is not ProtectionStatus.CLEAR
        for status in _statuses(context).values()
    )


@pytest.mark.parametrize(
    "action", (SystemdServiceAction.STOP, SystemdServiceAction.RESTART)
)
@pytest.mark.parametrize("relation", ("TriggeredBy", "UpheldBy"))
@pytest.mark.parametrize("source_active", (False, True))
def test_reactivation_sources_keep_stop_and_restart_applicability_conservative(
    action: SystemdServiceAction, relation: str, source_active: bool
):
    source = "source.socket" if relation == "TriggeredBy" else "source.service"
    source_unit = _unit(
        source,
        active="active" if source_active else "inactive",
        sub="listening" if source_active and source.endswith(".socket") else "dead",
    )
    state = _state(
        {
            "target.service": _unit(
                "target.service", relationships={relation: [source]}
            ),
            source: source_unit,
            "backend.service": _unit("backend.service"),
            "agent.service": _unit("agent.service"),
        }
    )
    graph = _provider(state).observe_effect_graph("target.service", str(action))
    context = _context_from_graph(graph, action)
    reasons = lifecycle_applicability(context, expected_canonical_id="target.service")
    expected = (
        "active_trigger_may_reactivate_service"
        if relation == "TriggeredBy"
        else "active_upholder_may_reactivate_service"
    )

    active_sources = (
        graph.active_activation_sources
        if relation == "TriggeredBy"
        else graph.active_upholding_sources
    )
    if not source_active:
        assert graph.complete
        assert source not in active_sources
        assert expected not in reasons
        return

    assert source in active_sources
    assert expected in reasons
    if not graph.complete:
        assert graph.gaps
        assert all(
            status is not ProtectionStatus.CLEAR
            for status in _statuses(context).values()
        )


def test_provider_graph_and_lifecycle_context_keep_their_public_dictionary_shapes():
    state = _state(
        {
            "target.service": _unit(
                "target.service",
                active="inactive",
                sub="dead",
                relationships={"Requires": ["dependency.service"]},
            ),
            "dependency.service": _unit("dependency.service"),
            "backend.service": _unit("backend.service"),
            "agent.service": _unit("agent.service"),
        }
    )
    graph = _provider(state).observe_effect_graph("target.service", "start")
    context = _context_from_graph(graph, SystemdServiceAction.START)

    assert set(graph.as_dict()) == {
        "status", "observed_at", "requested_unit", "action", "provider_version",
        "canonical_target", "target", "units", "edges", "active_activation_sources",
        "active_upholding_sources", "gaps", "reason", "detail",
    }
    assert SystemdLifecycleContext.from_dict(context.as_dict()) == context
