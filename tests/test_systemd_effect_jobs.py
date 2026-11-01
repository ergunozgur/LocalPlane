"""Provider-only potential-job closure regressions; no host bus or managed writes.

The mapping is grounded in systemd v255's dependency atoms and
transaction_add_job_and_dependencies(), not in uniform forward/reverse walks.
"""

from collections import Counter

import pytest

from localplane.agent.providers import systemd
from tests.test_systemd import FakeSystemdState, _provider, _service, _unit


TARGET = "target.service"


def estate(relations):
    return FakeSystemdState(
        units={name: _unit(name, relationships=edges) for name, edges in relations.items()},
        typed={TARGET: _service()},
    )


def graph(state, action="restart", **limits):
    result = _provider(state).observe_effect_graph(TARGET, action, **limits)
    assert not any(call[0] in {"load_unit", "unit_lifecycle", "subscribe"} for call in state.calls)
    assert len(result.units) <= 128 and len(result.edges) <= 2048
    return result


def looked_up(state):
    return {call[1] for call in state.calls if call[0] == "get_unit"}


@pytest.mark.parametrize("action", ["start", "restart"])
def test_shared_slice_start_job_does_not_walk_back_to_other_services(action):
    state = estate({
        TARGET: {"Requires": ["system.slice"]},
        "system.slice": {"Requires": ["-.slice"], "RequiredBy": ["agent.service", "backend.service"]},
        "-.slice": {"RequiredBy": ["system.slice", "other.slice"]},
        "agent.service": {}, "backend.service": {}, "other.slice": {},
    })
    result = graph(state, action)
    assert result.status == "complete" and result.gaps == ()
    assert set(result.units) == {TARGET, "system.slice", "-.slice"}
    assert not {"agent.service", "backend.service", "other.slice"} & looked_up(state)


@pytest.mark.parametrize("relation", ["RequiredBy", "RequisiteOf", "BoundBy", "ConsistsOf"])
@pytest.mark.parametrize("action", ["stop", "restart"])
def test_real_stop_and_restart_propagation_still_reaches_protected_units(relation, action):
    state = estate({TARGET: {relation: ["agent.service"]}, "agent.service": {}})
    result = graph(state, action)
    assert result.status == "complete"
    assert set(result.units) == {TARGET, "agent.service"}
    assert {"source": TARGET, "relation": relation, "target": "agent.service"} in result.edges


@pytest.mark.parametrize("relation", ["Conflicts", "ConflictedBy"])
@pytest.mark.parametrize("action", ["start", "restart"])
def test_start_conflict_switches_to_stop_then_walks_true_reverse_effects(relation, action):
    state = estate({
        TARGET: {relation: ["conflict.service"]},
        "conflict.service": {"RequiredBy": ["agent.service"]},
        "agent.service": {"Requires": ["only-inspected.slice"]},
        "only-inspected.slice": {"RequiredBy": ["unrelated.service"]},
        "unrelated.service": {},
    })
    result = graph(state, action)
    assert result.status == "complete"
    assert set(result.units) == {TARGET, "conflict.service", "agent.service"}
    assert "only-inspected.slice" in looked_up(state)
    assert "unrelated.service" not in looked_up(state)


@pytest.mark.parametrize("action", ["start", "restart"])
def test_forward_binds_to_is_start_not_restart_and_cannot_follow_bound_by(action):
    state = estate({
        TARGET: {"BindsTo": ["bound.service"]},
        "bound.service": {"BoundBy": ["agent.service"]}, "agent.service": {},
    })
    assert set(graph(state, action).units) == {TARGET, "bound.service"}
    assert "agent.service" not in looked_up(state)
    # The same bound unit DOES propagate when it is itself stopped/restarted.
    for verb in ("stop", "restart"):
        direct = _provider(state).observe_effect_graph("bound.service", verb)
        assert "agent.service" in direct.units


@pytest.mark.parametrize("relation", ["Requires", "Wants", "BindsTo", "Upholds"])
def test_forward_start_pullins_are_not_pruned_by_current_inactive_state(relation):
    state = estate({
        TARGET: {relation: ["dep.service"]},
        "dep.service": {"Requires": ["agent.service"]}, "agent.service": {},
    })
    state.units["dep.service"]["ActiveState"] = "inactive"
    result = graph(state)
    assert "agent.service" in result.units
    if relation == "Upholds":
        assert result.status == "partial"
        assert f"effect_graph.runtime_upholding:{TARGET}" in result.gaps


def test_same_canonical_unit_is_expanded_in_each_job_kind_and_cycles_terminate():
    state = estate({
        TARGET: {"Wants": ["both.service"], "Conflicts": ["conflict.service"]},
        "both.service": {"Requires": ["start-child.service"], "RequiredBy": ["stop-child.service"]},
        "conflict.service": {"RequiredBy": ["both.service"]},
        "start-child.service": {"Requires": [TARGET]},
        "stop-child.service": {"RequiredBy": ["both.service"]},
    })
    result = graph(state, "start", max_nodes=len(state.units))
    assert result.status == "complete" and set(result.units) == set(state.units)
    reads = Counter(call[1] for call in state.calls if call[0] == "unit_get_all")
    assert reads[state.path_for(TARGET)] == 2  # target observation plus closure
    assert all(count == 1 for path, count in reads.items() if path != state.path_for(TARGET))


@pytest.mark.parametrize("coexisting_start", [False, True])
def test_graceful_stop_conservatively_keeps_restart_start_and_conflict_effects(coexisting_start):
    state = estate({
        TARGET: {"PropagatesStopTo": ["graceful.service"],
                 "Wants": ["graceful.service"] if coexisting_start else []},
        "graceful.service": {"Requires": ["start-child.service"], "Conflicts": ["conflict.service"],
                             "RequiredBy": ["reverse.service"], "PropagatesStopTo": [TARGET]},
        "start-child.service": {}, "conflict.service": {"RequiredBy": ["agent.service"]},
        "reverse.service": {}, "agent.service": {},
    })
    state.units["graceful.service"]["ActiveState"] = "inactive"
    result = graph(state)
    assert result.status == "partial" and set(result.units) == set(state.units)
    assert f"effect_graph.graceful_stop:{TARGET}" in result.gaps


def test_requisite_verify_does_not_start_or_stop_children():
    state = estate({
        TARGET: {"Requisite": ["verify.service"]},
        "verify.service": {"Requires": ["start.service"], "RequiredBy": ["stop.service"],
                           "OnFailure": ["handler.service"]},
        "start.service": {}, "stop.service": {}, "handler.service": {},
    })
    result = graph(state)
    assert result.status == "complete"
    assert set(result.units) == {TARGET, "verify.service"}
    assert looked_up(state) == {TARGET, "verify.service"}


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_reverse_discoverability_and_ordering_are_not_job_causation(action):
    ignored = ["StopPropagatedFrom", "PartOf", "OnFailureOf", "OnSuccessOf", "Before", "After"]
    state = estate({TARGET: {relation: ["not-an-effect.service"] for relation in ignored}})
    result = graph(state, action)
    assert result.status == "complete" and result.units == (TARGET,) and result.edges == ()
    assert looked_up(state) == {TARGET}


def test_start_node_does_not_require_stop_only_or_zero_atom_properties():
    state = estate({TARGET: {"Requires": ["dep.service"]}, "dep.service": {}})
    for relation in ("RequiredBy", "RequisiteOf", "BoundBy", "ConsistsOf",
                     "PropagatesStopTo", "StopPropagatedFrom", "PartOf", "OnFailureOf", "OnSuccessOf"):
        del state.units["dep.service"][relation]
    assert graph(state).status == "complete"


@pytest.mark.parametrize("relation", ["Requires", "Wants", "BindsTo", "Upholds", "Requisite"])
@pytest.mark.parametrize("value", [False, True, None, "false"])
def test_released_forward_dependencies_are_inspected_not_given_stop_jobs(relation, value):
    state = estate({
        TARGET: {relation: ["released.slice"]},
        "released.slice": {"RequiredBy": ["agent.service"], "Requires": ["not-released.service"]},
        "agent.service": {}, "not-released.service": {},
    })
    state.units["released.slice"]["StopWhenUnneeded"] = value
    result = graph(state, "stop")
    assert result.status == ("complete" if value is False else "partial")
    assert "released.slice" in looked_up(state)
    assert not {"agent.service", "not-released.service"} & looked_up(state)
    assert result.edges == ()  # metadata is not represented as a causative job edge
    if value is False:
        assert result.units == (TARGET,)
    else:
        assert any("unneeded" in gap or "StopWhenUnneeded" in gap for gap in result.gaps)


@pytest.mark.parametrize("value", [True, None])
def test_conflict_stop_transitively_inspects_released_dependencies_even_on_start(value):
    state = estate({
        TARGET: {"Conflicts": ["stop.service"]},
        "stop.service": {"Requisite": ["released.service"]}, "released.service": {},
    })
    state.units["released.service"]["StopWhenUnneeded"] = value
    result = graph(state, "start")
    assert result.status == "partial"
    assert any(gap.endswith(":released.service") for gap in result.gaps)


@pytest.mark.parametrize("relation", ["OnFailure", "OnSuccess", "Triggers"])
@pytest.mark.parametrize("transitive", [False, True])
def test_conditional_handlers_and_triggers_are_bounded_footprints_never_false_complete(relation, transitive):
    source = "dep.service" if transitive else TARGET
    state = estate({TARGET: {"Requires": ["dep.service"]}, "dep.service": {},
                    "handler.service": {"Requires": ["outside-footprint.service"]}})
    state.units[source][relation] = ["handler.service"]
    # Even a known ordinary mode is not promised modelled; isolate or absent modes
    # cannot secretly acquire an exhaustive START closure either.
    state.units[source][relation + "JobMode"] = "isolate"
    result = graph(state)
    assert result.status == "partial"
    assert f"effect_graph.conditional:{relation}:{source}" in result.gaps
    assert "handler.service" in result.units
    assert "outside-footprint.service" not in looked_up(state)


@pytest.mark.parametrize("missing", ["OnFailure", "OnSuccess", "Triggers", "StopWhenUnneeded"])
def test_missing_runtime_effect_fields_do_not_mean_empty(missing):
    state = estate({TARGET: {"Requires": ["dep.service"]}, "dep.service": {}})
    del state.units["dep.service"][missing]
    result = graph(state)
    assert result.status == "partial"
    assert f"effect_graph.unsupported:{missing}:dep.service" in result.gaps


@pytest.mark.parametrize("transitive", [False, True])
@pytest.mark.parametrize("job", [None, (12, "/job/12"), (0, "/bad"), "bad"])
def test_pending_or_unreadable_runtime_jobs_remain_partial(transitive, job):
    state = estate({TARGET: {"Requires": ["dep.service"]}, "dep.service": {}})
    name = "dep.service" if transitive else TARGET
    state.units[name]["Job"] = job
    result = graph(state)
    assert result.status == "partial"
    assert any(gap.startswith("effect_graph.runtime_job_") and gap.endswith(":" + name)
               for gap in result.gaps)


@pytest.mark.parametrize("relation", ["TriggeredBy", "UpheldBy"])
def test_actual_target_reactivation_is_not_a_reverse_stop_job(relation):
    state = estate({TARGET: {relation: ["source.service"]},
                    "source.service": {"RequiredBy": ["agent.service"]}, "agent.service": {}})
    result = graph(state, "stop")
    assert result.status == "complete"
    assert "agent.service" not in looked_up(state)
    sources = result.active_activation_sources if relation == "TriggeredBy" else result.active_upholding_sources
    assert sources == ("source.service",)


@pytest.mark.parametrize("relation", ["TriggeredBy", "UpheldBy"])
@pytest.mark.parametrize("active", ["active", "inactive", "activating"])
def test_transitive_reactivation_requires_separate_runtime_evidence(relation, active):
    state = estate({TARGET: {"Requires": ["dep.service"]},
                    "dep.service": {relation: ["source.service"]}, "source.service": {}})
    state.units["source.service"]["ActiveState"] = active
    result = graph(state)
    assert result.status == ("complete" if active == "inactive" else "partial")
    assert result.active_activation_sources == () and result.active_upholding_sources == ()


def test_current_inactive_source_that_will_start_is_not_a_stable_negative():
    state = estate({TARGET: {"Requires": ["source.service"], "TriggeredBy": ["source.service"]},
                    "source.service": {}})
    state.units["source.service"]["ActiveState"] = "inactive"
    result = graph(state)
    assert result.status == "partial"
    assert "effect_graph.reactivation_source_in_closure:TriggeredBy:source.service" in result.gaps


@pytest.mark.parametrize("order", [["dep.service", "alias.service"], ["alias.service", "dep.service"]])
def test_alias_gap_is_sticky_even_if_canonical_node_was_already_read(order):
    state = estate({TARGET: {"Requires": order}, "dep.service": {}})
    state.aliases["alias.service"] = "dep.service"
    result = graph(state)
    assert result.status == "partial"
    assert "effect_graph.alias_reference:alias.service" in result.gaps
    assert set(result.units) == {TARGET, "dep.service"}


@pytest.mark.parametrize("failure", ["absent", "id", "load", "unreadable"])
def test_unresolved_or_unreadable_transitive_identity_cannot_be_complete(failure):
    state = estate({TARGET: {"Requires": ["dep.service"]}, "dep.service": {}})
    if failure == "absent":
        del state.units["dep.service"]
    elif failure == "id":
        state.units["dep.service"]["Id"] = None
    elif failure == "load":
        state.units["dep.service"]["LoadState"] = "not-found"
    else:
        state.fail_units.add("dep.service")
    assert graph(state).status == "partial"


def test_node_limit_bounds_metadata_and_job_reads_together():
    units = {f"dep{i}.service": {} for i in range(140)}
    state = estate({TARGET: {"Requires": list(units)}, **units})
    result = graph(state, "stop", max_nodes=10000)
    assert result.status == "partial" and "effect_graph.node_limit" in result.gaps
    assert len(looked_up(state)) == 128
    assert sum(call[0] == "unit_get_all" for call in state.calls) == 129


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_depth_limit_applies_to_cross_kind_and_metadata_work(action):
    state = estate({TARGET: {"Conflicts": ["child.service"], "RequiredBy": ["child.service"],
                             "Requires": ["child.service"]}, "child.service": {}})
    result = graph(state, action, max_depth=0)
    assert result.status == "partial" and "effect_graph.depth_limit" in result.gaps
    assert looked_up(state) == {TARGET}


def test_depth_cap_cannot_be_raised_above_twelve():
    names = [TARGET] + [f"dep{i}.service" for i in range(15)]
    state = estate({name: {"Requires": [names[i + 1]]} if i + 1 < len(names) else {}
                    for i, name in enumerate(names)})
    result = graph(state, max_depth=10000)
    assert result.status == "partial" and "effect_graph.depth_limit" in result.gaps
    assert len(result.units) == 13
    assert result.detail["max_depth"] == 12


def test_edge_limit_remains_fail_closed_across_job_kinds():
    state = estate({TARGET: {"Requires": ["a.service"], "Conflicts": ["b.service"]},
                    "a.service": {"Requires": ["b.service"]},
                    "b.service": {"RequiredBy": ["a.service"]}})
    result = graph(state, max_edges=2)
    assert result.status == "partial" and "effect_graph.edge_limit" in result.gaps
    assert len(result.edges) == 2


def test_duplicate_reference_work_has_a_finite_budget_too():
    class CountedList(list):
        consumed = 0
        def __iter__(self):
            for item in super().__iter__():
                self.consumed += 1
                yield item
    repeated = CountedList(["dep.service"] * 10000)
    # Measure the new traversal, separately from the unchanged target observation's
    # inventory normalisation of its own relationships.
    state = estate({TARGET: {"Requires": ["dep.service"]}, "dep.service": {"Requires": repeated}})
    result = graph(state, "start", max_edges=2)
    assert result.status == "partial" and "effect_graph.edge_limit" in result.gaps
    assert repeated.consumed <= 2 * len(systemd._EFFECT_WORK_KINDS) + 1


def test_single_deadline_also_bounds_additional_states_and_cached_work():
    state = estate({TARGET: {"Requires": ["dep.service"], "Conflicts": ["dep.service"]},
                    "dep.service": {"RequiredBy": [TARGET]}})
    ticks = []
    def clock():
        ticks.append(None)
        return len(ticks) * 0.05
    result = _provider(state, monotonic=clock).observe_effect_graph(TARGET, "restart", timeout_s=0.6)
    assert result.status == "partial" and "effect_graph.read_failed" in result.gaps
    assert all(timeout is not None and 0 < timeout <= 0.6 for timeout in state.timeouts)
    assert len(ticks) < 20
