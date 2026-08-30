"""The closed systemd lifecycle transport: one verb, one unit, and nothing else reachable.

Step 4 of the self-impact vertical added the agent-side dispatch and stopped there; the
backend side landed afterwards — the systemd operations declare execution available, an
executor is registered, and an eligible plan reaches this transport on apply. What is
being tested is the transport itself, and mostly what it refuses:

* **the caller supplies an action and a unit name.** Not an object path, an interface, a
  member, a signature, a job id, a start mode, a timeout or a property — there is no
  parameter for any of them at any layer, and the mode is a module constant;
* **`GetUnit`, never `LoadUnit`**, and never the Manager's similarly-named lifecycle
  methods: the Unit object's own `Start`, `Stop` and `Restart` are the whole surface;
* **the wait is for one job**, correlated on both the returned job path and the canonical
  unit name, so a busy manager's other signals cannot be mistaken for this one's answer;
* **outcomes are never softened.** An error reply is a proof that no job was enqueued; a job
  that was enqueued and did not report completion is unknown, in every direction.

Every test drives a fake D-Bus transport. Nothing here touches the host's systemd.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jeepney.low_level import HeaderFields
from jeepney.wrappers import DBusErrorResponse

from localplane.agent.providers import systemd as systemd_provider
from localplane.agent.providers.systemd import (
    JOB_COMPLETION_TIMEOUT_S,
    JOB_RESULT_DONE,
    SYSTEMD_DISPATCH_MODE,
    SYSTEMD_LIFECYCLE_ACTIONS,
    SystemdInvalidUnitName,
    SystemdProvider,
    _LoadedUnitAbsent,
    validate_service_unit_name,
)
from localplane.protocol.wire import (
    METHODS,
    MUTATING_METHODS,
    SYSTEMD_SERVICE_LIFECYCLE_ACTIONS,
    SYSTEMD_SERVICE_LIFECYCLE_UNIT_SUFFIX,
    Method,
)
from tests.test_systemd import FakeSystemdState, _service, _unit


def _error_reply(name: str) -> DBusErrorResponse:
    """A real Jeepney error reply, because the provider branches on that exact type.

    An error *reply* is the authoritative negative: it is what the manager sends instead of
    a job path. Anything else that goes wrong is ambiguous, and the provider treats the two
    differently on purpose — so a fake that raised a look-alike would be testing the wrong
    branch and reporting the wrong outcome as passing.
    """
    header = SimpleNamespace(fields={HeaderFields.error_name: name})
    return DBusErrorResponse(SimpleNamespace(header=header, body=()))


@dataclass
class _Signal:
    body: tuple[Any, ...]


@dataclass
class DispatchState(FakeSystemdState):
    """A manager that hands out jobs and reports what became of them.

    The counterpart to the read-only fake, and deliberately a subclass of it: the dispatch
    path resolves and reads a unit through exactly the same calls the observation path uses,
    so a fake that framed those differently would be testing a different provider.
    """

    job_path: str = "/org/freedesktop/systemd1/job/17"
    job_id: int = 17
    job_result: str | None = JOB_RESULT_DONE
    dispatch_error: Exception | None = None
    subscribe_error: Exception | None = None
    #: Signals delivered before this dispatch's own, if any. A busy manager's other work.
    foreign_signals: list[_Signal] = field(default_factory=list)
    #: When true the job is enqueued and its completion is never reported.
    withhold_result: bool = False
    #: Raised by the wait itself, *after* a job path has already come back.
    wait_error: Exception | None = None
    #: Raised while the match is being torn down, also after a job exists.
    watch_close_error: Exception | None = None
    #: Raised while the connection is closing, later still.
    close_error: Exception | None = None
    #: What the manager does to the unit when a job runs. A fake that never moved the estate
    #: would let a verification pass against a reading that was already the answer.
    on_dispatch: Any = None


class DispatchConnection:
    """The read-only fake's calls, plus the four the dispatch path adds."""

    def __init__(self, state: DispatchState) -> None:
        from tests.test_systemd import FakeSystemdConnection

        self.state = state
        self._read = FakeSystemdConnection(state)

    def __enter__(self) -> "DispatchConnection":
        self._read.__enter__()
        return self

    def __exit__(self, *args: Any) -> None:
        self._read.__exit__(*args)
        if self.state.close_error is not None:
            raise self.state.close_error

    # ---------------------------------------------------------------- read-only, shared

    def __getattr__(self, name: str) -> Any:
        """Every read the observation path makes, answered by the read-only fake itself.

        Delegated rather than reimplemented so the dispatch path resolves and reads a unit
        through exactly the calls the observation path uses. A second copy of those answers
        would eventually differ from the first, and the difference would be invisible.
        """
        return getattr(self._read, name)

    # -------------------------------------------------------------------- the dispatch

    def subscribe(self, timeout_s: float | None = None) -> None:
        self.state.calls.append(("subscribe",))
        if self.state.subscribe_error is not None:
            raise self.state.subscribe_error

    def job_watch(self):
        state = self.state
        state.calls.append(("job_watch",))

        class _Watch:
            def result_for(self, job_path: str, unit_id: str, timeout_s: float):
                state.calls.append(("await_job", job_path, unit_id, timeout_s))
                if state.wait_error is not None:
                    raise state.wait_error
                for signal in state.foreign_signals:
                    _job_id, path, unit, _result = signal.body
                    assert not (str(path) == job_path and str(unit) == unit_id), (
                        "the fixture's foreign signals must not match this job"
                    )
                if state.withhold_result or state.job_result is None:
                    return None
                return state.job_id, state.job_result

        class _Ctx:
            def __enter__(self_inner):
                return _Watch()

            def __exit__(self_inner, *args: Any) -> None:
                state.calls.append(("job_watch_closed",))
                if state.watch_close_error is not None:
                    raise state.watch_close_error

        return _Ctx()

    def unit_lifecycle(
        self, object_path: str, action: str, timeout_s: float | None = None
    ) -> str:
        self.state.calls.append(("unit_lifecycle", object_path, action))
        if self.state.dispatch_error is not None:
            raise self.state.dispatch_error
        if self.state.on_dispatch is not None:
            self.state.on_dispatch(self.state)
        return self.state.job_path


def _state(**overrides: Any) -> DispatchState:
    units = {"alpha.service": _unit("alpha.service"), "alpha.socket": _unit("alpha.socket")}
    typed = {"alpha.service": _service()}
    return DispatchState(units=units, typed=typed, **overrides)


def _dispatch(state: DispatchState, unit: str = "alpha.service", action: str = "restart"):
    provider = SystemdProvider(
        runtime_path=None, connection_factory=lambda: DispatchConnection(state)
    )
    return provider.dispatch_service_lifecycle(unit, action)


def _members(state: DispatchState) -> list[tuple[Any, ...]]:
    return [call for call in state.calls if call[0] == "unit_lifecycle"]


# ------------------------------------------------------------------ the closed vocabulary


def test_the_protocol_gains_exactly_one_mutating_method():
    assert Method.SYSTEMD_SERVICE_LIFECYCLE.value == "systemd.service_lifecycle"
    assert Method.SYSTEMD_SERVICE_LIFECYCLE.value in METHODS
    assert MUTATING_METHODS == {
        "network.set_interface_mtu",
        "network.arm_mtu_guard",
        "docker.container_lifecycle",
        "systemd.service_lifecycle",
    }
    # The action vocabulary is the one Part A already declared, not a second copy.
    assert SYSTEMD_SERVICE_LIFECYCLE_ACTIONS == ("start", "stop", "restart")
    assert SYSTEMD_SERVICE_LIFECYCLE_ACTIONS == SYSTEMD_LIFECYCLE_ACTIONS
    assert SYSTEMD_SERVICE_LIFECYCLE_UNIT_SUFFIX == ".service"


def test_the_provider_exposes_no_way_to_name_a_dbus_call():
    """The dispatch takes a unit name and an action. Everything else is the module's."""
    signature = inspect.signature(SystemdProvider.dispatch_service_lifecycle)
    assert list(signature.parameters) == ["self", "unit_id", "action"]

    # Every D-Bus member this module can construct, read from the calls themselves rather
    # than from prose about them: `new_method_call`'s second argument *is* the member name.
    tree = ast.parse(Path(systemd_provider.__file__).read_text())
    members = {
        node.args[1].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "new_method_call"
        and len(node.args) > 1
        and isinstance(node.args[1], ast.Constant)
    }
    assert members == {
        # read-only, and every one of them predates the mutating verbs
        "ListUnitsByPatterns", "ListUnits", "GetUnit", "GetUnitByControlGroup",
        "GetUnitByPID", "GetUnitByPIDFD", "Subscribe",
        # the whole of the mutating surface
        "Start", "Stop", "Restart",
    }
    source = Path(systemd_provider.__file__).read_text()
    assert "subprocess" not in source and "systemctl" not in source


@pytest.mark.parametrize(
    "action, member",
    [("start", "Start"), ("stop", "Stop"), ("restart", "Restart")],
)
def test_each_action_maps_to_exactly_one_unit_member(action: str, member: str):
    """One verb, one member, and the mapping is a table rather than string arithmetic."""
    assert systemd_provider._LIFECYCLE_UNIT_METHOD_FOR[action] == member
    state = _state()
    result = _dispatch(state, action=action)
    assert result.outcome == "written"
    assert result.action == action
    assert _members(state) == [("unit_lifecycle", state.path_for("alpha.service"), action)]


def test_the_mode_is_a_constant_and_reaches_the_bus_as_one():
    """`fail` rather than `replace`: a dispatch must not cancel somebody else's job."""
    assert SYSTEMD_DISPATCH_MODE == "fail"
    source = inspect.getsource(systemd_provider._UnitMessages)
    assert source.count("SYSTEMD_DISPATCH_MODE") == 3
    assert '"replace"' not in source and "'replace'" not in source
    # And it is not reachable as a parameter from any layer above.
    tree = ast.parse(Path(systemd_provider.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in (
            "dispatch_service_lifecycle", "unit_lifecycle"
        ):
            assert "mode" not in [a.arg for a in node.args.args]


def test_the_unit_is_resolved_with_get_unit_before_anything_is_dispatched():
    """`GetUnit` asks about a unit the manager already has; `LoadUnit` would pull an
    unloaded declaration into its estate as a side effect of asking."""
    state = _state()
    assert _dispatch(state).outcome == "written"
    names = [call[0] for call in state.calls]
    assert names.index("subscribe") < names.index("get_unit")
    assert names.index("job_watch") < names.index("unit_lifecycle")
    assert names.index("get_unit") < names.index("unit_lifecycle")
    assert "load_unit" not in names


def test_the_subscription_and_the_match_are_established_before_the_mutation():
    """A job that finishes quickly must not outrun the listener that would hear it."""
    state = _state()
    _dispatch(state)
    names = [call[0] for call in state.calls]
    assert names.index("subscribe") < names.index("unit_lifecycle")
    assert names.index("job_watch") < names.index("unit_lifecycle")


def test_no_transport_handle_survives_the_dispatch():
    """Object paths are evidence inside one connection and identity nowhere."""
    state = _state()
    result = _dispatch(state)
    rendered = repr(result.as_dict())
    assert "/org/freedesktop/systemd1/unit/" not in rendered
    assert state.job_path not in rendered
    assert result.job_id == state.job_id
    assert result.unit_id == "alpha.service"


# ------------------------------------------------------------------- refused before a job


@pytest.mark.parametrize(
    "unit",
    ["alpha.socket", "alpha", "../etc/passwd", "alpha.service\n", "", "a" * 300],
    ids=["socket", "no_suffix", "traversal", "newline", "empty", "too_long"],
)
def test_a_unit_that_is_not_a_canonical_service_never_reaches_the_bus(unit: str):
    state = _state()
    result = _dispatch(state, unit=unit)
    assert result.outcome == "not_written"
    assert result.reason == "invalid_service_unit_name"
    assert state.calls == []


def test_an_action_outside_the_closed_set_never_reaches_the_bus():
    state = _state()
    result = _dispatch(state, action="reload")
    assert result.outcome == "not_written"
    assert result.reason == "unsupported_lifecycle_action"
    assert state.calls == []


def test_a_unit_the_manager_has_not_loaded_is_a_proof_that_nothing_happened():
    state = _state()
    state.units.pop("alpha.service")
    result = _dispatch(state)
    assert result.outcome == "not_written"
    assert result.reason == "unit_not_loaded"
    assert _members(state) == []


def test_an_alias_resolving_to_another_unit_stops_before_dispatch():
    """The manager's own `Id` has to be the canonical name that was asked for."""
    state = _state()
    # The manager resolved the name to an object whose own `Id` is a different unit.
    state.units["alpha.service"]["Id"] = "beta.service"
    result = _dispatch(state)
    assert result.outcome == "not_written"
    assert result.reason == "dispatch_identity_not_canonical"
    assert _members(state) == []


def test_a_unit_that_is_not_loaded_by_its_own_load_state_stops_before_dispatch():
    state = _state()
    state.units["alpha.service"]["LoadState"] = "not-found"
    result = _dispatch(state)
    assert result.outcome == "not_written"
    assert result.reason == "dispatch_identity_not_canonical"
    assert _members(state) == []


@pytest.mark.parametrize(
    "error_name",
    [
        "org.freedesktop.DBus.Error.AccessDenied",
        "org.freedesktop.DBus.Error.InteractiveAuthorizationRequired",
    ],
)
def test_an_authorization_refusal_is_not_written(error_name: str):
    """systemd decides at dispatch, nothing preflights it, and a refusal is a proof.

    An error *reply* is what the manager sends instead of a job path, never as well as one,
    so no job was enqueued and the host did not move.
    """
    state = _state(dispatch_error=_error_reply(error_name))
    result = _dispatch(state)
    assert result.outcome == "not_written"
    assert result.reason == "authorization_denied"
    assert result.detail["dbus_error"] == error_name
    assert result.detail["authorization_preflight"] == "not_preflighted"
    assert result.detail["authorization_decision_point"] == "dispatch"


def test_any_other_error_reply_is_also_a_proof_that_no_job_was_enqueued():
    state = _state(dispatch_error=_error_reply("org.freedesktop.systemd1.NoSuchUnit"))
    result = _dispatch(state)
    assert result.outcome == "not_written"
    assert result.reason == "dispatch_refused"


def test_a_manager_that_cannot_be_reached_at_all_is_not_written():
    state = _state(subscribe_error=OSError("bus is gone"))
    result = _dispatch(state)
    assert result.outcome == "not_written"
    assert result.reason == "systemd_unreachable"
    assert _members(state) == []


# ------------------------------------------------------------------ after a job exists


def test_an_ambiguous_failure_after_the_call_left_is_write_unknown():
    """The request went and nothing intelligible came back. Neither answer is available."""
    state = _state(dispatch_error=TimeoutError("no reply"))
    result = _dispatch(state)
    assert result.outcome == "write_unknown"
    assert result.reason == "dispatch_answer_untrustworthy"


def test_a_job_whose_completion_is_never_heard_is_write_unknown():
    """Giving up listening is not evidence: the job may have completed perfectly."""
    state = _state(withhold_result=True)
    result = _dispatch(state)
    assert result.outcome == "write_unknown"
    assert result.reason == "job_result_not_observed"
    assert result.detail["timeout_s"] == JOB_COMPLETION_TIMEOUT_S
    assert ("await_job", state.job_path, "alpha.service", JOB_COMPLETION_TIMEOUT_S) in (
        state.calls
    )


def test_only_done_is_written():
    state = _state(job_result="done")
    result = _dispatch(state)
    assert result.outcome == "written"
    assert result.reason == "job_completed"
    assert result.job_result == "done"


@pytest.mark.parametrize(
    "job_result",
    ["failed", "dependency", "timeout", "canceled", "collected", "skipped",
     "a-result-this-build-has-never-seen"],
)
def test_every_other_job_result_is_write_unknown(job_result: str):
    """Including `skipped`, whose exact guarantees this build has not established.

    A start that failed may have run half its execution and a stop that timed out may have
    killed processes. `not_written` is a proof, and none of these are proofs.
    """
    state = _state(job_result=job_result)
    result = _dispatch(state)
    assert result.outcome == "write_unknown"
    assert result.reason == "job_did_not_complete"
    assert result.job_result == job_result


def test_the_dispatch_carries_the_baseline_a_restart_will_be_verified_against():
    """A service running before and running after has not been shown to have restarted."""
    state = _state()
    result = _dispatch(state)
    assert result.invocation_id == "01" * 16
    assert result.active_state == "active"


# --------------------------------------------------- the phase boundary, once a job exists


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param({"wait_error": OSError("the bus went away")}, id="receive_failed"),
        pytest.param({"wait_error": ConnectionResetError("peer reset")}, id="reset"),
        pytest.param({"wait_error": RuntimeError("filter is gone")}, id="filter_gone"),
        pytest.param({"watch_close_error": OSError("RemoveMatch failed")}, id="match_teardown"),
        pytest.param({"close_error": OSError("close failed")}, id="connection_close"),
    ],
)
def test_a_failure_after_the_job_path_can_never_be_not_written(failure: dict[str, Any]):
    """**The regression.** Once the manager has returned a job path, the job may be running.

    Everything that can still go wrong afterwards — receiving the completion signal, tearing
    the match down, closing the connection — happens while a transaction this process asked
    for is in flight. Reporting any of it as `not_written` would tell the Change engine that
    the host provably did not move, which is a claim about systemd that nothing here has,
    and it would end a Run `failed` over a service that may well have restarted.
    """
    state = _state(**failure)
    result = _dispatch(state)

    assert result.outcome == "write_unknown"
    assert result.outcome != "not_written"
    assert result.reason == "job_result_unobservable"
    # And the job really had been enqueued: the boundary was crossed before this failed.
    assert _members(state) == [("unit_lifecycle", state.path_for("alpha.service"), "restart")]
    # The dispatch-time baseline survives the failure, because a later verification needs it.
    assert result.invocation_id == "01" * 16


def test_an_error_reply_arriving_after_the_job_path_is_not_a_pre_dispatch_refusal():
    """The same window, with the one exception type that *is* a proof on the other side.

    An error reply to the dispatch call means no job was enqueued. The identical exception
    arriving while a job is already running means nothing of the kind — so the classification
    is by phase, not by type, and this is the case that would break a type-based one.
    """
    state = _state(wait_error=_error_reply("org.freedesktop.DBus.Error.AccessDenied"))
    result = _dispatch(state)

    assert result.outcome == "write_unknown"
    assert result.reason == "job_result_unobservable"
    assert result.reason != "authorization_denied"
    assert result.reason != "systemd_refused_before_dispatch"


def test_the_same_exception_is_not_written_before_the_job_and_unknown_after_it():
    """One exception, two answers, and the only difference is which side of the line it fell.

    Stated as one test because it is one rule: the phase decides, and nothing else does.
    """
    before = _dispatch(_state(subscribe_error=OSError("the bus went away")))
    after = _dispatch(_state(wait_error=OSError("the bus went away")))

    assert before.outcome == "not_written"
    assert before.reason == "systemd_unreachable"
    assert after.outcome == "write_unknown"
    assert after.reason == "job_result_unobservable"


def test_a_pre_dispatch_error_reply_is_still_a_proof():
    """The behaviour the fix must not weaken: refused before a job existed stays a proof."""
    state = _state()
    state.units.pop("alpha.service")
    assert _dispatch(state).outcome == "not_written"

    refused = _state(subscribe_error=_error_reply("org.freedesktop.DBus.Error.AccessDenied"))
    result = _dispatch(refused)
    assert result.outcome == "not_written"
    assert result.reason == "systemd_refused_before_dispatch"
    assert _members(refused) == []


def test_not_written_is_unreachable_from_every_post_dispatch_failure(monkeypatch):
    """Swept rather than enumerated: no exception type gets `not_written` after a job path.

    A future edit that reclassified one of these by type rather than by phase would have to
    get past this, and a new exception class does not slip through a list it is not on.
    """
    for exception in (
        OSError("io"), ConnectionResetError("reset"), TimeoutError("late"),
        RuntimeError("state"), ValueError("shape"), KeyError("missing"),
        _error_reply("org.freedesktop.DBus.Error.AccessDenied"),
        _error_reply("org.freedesktop.systemd1.NoSuchUnit"),
    ):
        result = _dispatch(_state(wait_error=exception))
        assert result.outcome == "write_unknown", exception
        assert result.reason == "job_result_unobservable", exception


# ------------------------------------------------------------------- job correlation


def test_the_wait_correlates_on_both_the_job_path_and_the_unit():
    state = _state()
    _dispatch(state)
    awaited = [call for call in state.calls if call[0] == "await_job"]
    assert awaited == [("await_job", state.job_path, "alpha.service", JOB_COMPLETION_TIMEOUT_S)]


def test_a_foreign_job_removed_signal_is_not_this_dispatch_s_answer():
    """Driven against the real correlation code rather than the fake's shortcut."""
    watch = systemd_provider._JobWatch(
        _RecordingConnection(
            [
                _Signal((9, "/org/freedesktop/systemd1/job/9", "other.service", "done")),
                _Signal((17, "/org/freedesktop/systemd1/job/17", "other.service", "done")),
                _Signal((5, "/org/freedesktop/systemd1/job/5", "alpha.service", "done")),
                _Signal((17, "/org/freedesktop/systemd1/job/17", "alpha.service", "failed")),
            ]
        ),
        queue=None,
    )
    assert watch.result_for(
        "/org/freedesktop/systemd1/job/17", "alpha.service", 5.0
    ) == (17, "failed")


def test_a_malformed_signal_body_is_skipped_rather_than_read():
    watch = systemd_provider._JobWatch(
        _RecordingConnection(
            [
                _Signal(("nonsense",)),
                _Signal((1, "/org/freedesktop/systemd1/job/17", "alpha.service", "done")),
            ]
        ),
        queue=None,
    )
    assert watch.result_for(
        "/org/freedesktop/systemd1/job/17", "alpha.service", 5.0
    ) == (1, "done")


def test_a_signal_that_never_arrives_ends_the_wait_without_an_answer():
    watch = systemd_provider._JobWatch(_RecordingConnection([]), queue=None)
    assert watch.result_for("/job/17", "alpha.service", 0.05) is None


class _RecordingConnection:
    """Delivers scripted signals, then behaves like a bus that has gone quiet."""

    def __init__(self, signals: list[_Signal]) -> None:
        self._signals = list(signals)

    def recv_until_filtered(self, queue: Any, timeout: float | None = None) -> Any:
        if not self._signals:
            raise TimeoutError("no further signals")
        return self._signals.pop(0)


# ----------------------------------------------------------------------- the name rule


def test_the_name_rule_is_one_implementation_used_on_both_sides():
    validate_service_unit_name("alpha.service")
    for bad in ("alpha.socket", "alpha", "alpha.service\n", "a/b.service", ""):
        with pytest.raises(SystemdInvalidUnitName):
            validate_service_unit_name(bad)


def test_a_loaded_unit_absent_error_is_the_provider_s_own_typed_one():
    """The read path's absence signal, reused rather than re-derived from a D-Bus name."""
    state = _state()
    state.units.pop("alpha.service")
    connection = DispatchConnection(state)
    with pytest.raises(_LoadedUnitAbsent):
        connection.get_unit_path("alpha.service")
