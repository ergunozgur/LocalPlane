import json
import threading

import pytest

from localplane.maintenance import MaintenanceController, MaintenanceRefused
from localplane.agent.guard import GuardRegistry
from localplane.backend.agent_client import AgentClient, AgentError
from localplane.protocol.wire import Method


def controller(root):
    return MaintenanceController(root, service="test", lock_timeout_s=0.1)


def test_window_survives_new_controller_and_old_snapshot_is_refused(tmp_path):
    root = tmp_path / "maintenance"
    first = controller(root)
    first.initialize()
    window = first.open_window()
    assert json.loads((root / "window.json").read_text())["format_version"] == 1
    second = controller(root)
    assert second.state().window.window_id == window.window_id
    with pytest.raises(MaintenanceRefused):
        with second.admit("write"):
            pytest.fail("active fence admitted a write")
    binding = {"window_id": window.window_id, "generation": window.generation}
    with second.restore_lease(binding):
        pass
    second.close_window(window.window_id, accepted=True)
    replacement = second.open_window()
    assert replacement.window_id != window.window_id
    with pytest.raises(MaintenanceRefused):
        with second.restore_lease(binding):
            pytest.fail("old snapshot was admitted")


def test_inflight_writer_blocks_open_and_runtime_blocks_snapshot(tmp_path):
    ctl = controller(tmp_path / "maintenance")
    ctl.initialize()
    entered = threading.Event()
    release = threading.Event()

    def writer():
        with ctl.admit("write"):
            entered.set()
            release.wait(2)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert entered.wait(2)
        with pytest.raises(MaintenanceRefused):
            ctl.open_window()
    finally:
        release.set()
        thread.join(2)
    window = ctl.open_window()
    lifetime = ctl.service_lifetime()
    try:
        with pytest.raises(MaintenanceRefused):
            with ctl.snapshot_lease(window.window_id):
                pytest.fail("running service was ignored")
    finally:
        lifetime.close()
    with ctl.snapshot_lease(window.window_id):
        with pytest.raises(MaintenanceRefused):
            ctl.service_lifetime()


def test_corrupt_witness_fails_closed(tmp_path):
    ctl = controller(tmp_path / "maintenance")
    ctl.initialize()
    ctl.open_window()
    (ctl.root / "window.json").write_text("{broken")
    assert ctl.state().status == "unknown"
    with pytest.raises(MaintenanceRefused):
        with ctl.admit("write"):
            pytest.fail("corrupt witness admitted a write")


def test_missing_witness_is_not_a_new_install(tmp_path):
    ctl = controller(tmp_path / "maintenance")
    ctl.initialize()
    (ctl.root / "window.json").unlink()
    assert ctl.state().status == "unknown"
    with pytest.raises(MaintenanceRefused):
        ctl.open_window()
    with pytest.raises(MaintenanceRefused):
        ctl.initialize()


def test_armed_guard_keeps_admission_through_firing(tmp_path):
    ctl = controller(tmp_path / "maintenance")
    ctl.initialize()
    callbacks = []
    class Timer:
        def cancel(self):
            pass
    def timer_factory(delay, callback):
        callbacks.append(callback)
        return Timer()
    mutations = []
    def mutate(**kwargs):
        with pytest.raises(MaintenanceRefused):
            ctl.open_window()
        mutations.append(kwargs)
        return {"outcome": "written", "attempt_id": kwargs["attempt_id"],
                "ifindex": kwargs["ifindex"], "expected_current_mtu": kwargs["expected_current_mtu"],
                "desired_mtu": kwargs["desired_mtu"], "method": "netlink_rtm_newlink_mtu", "provider": "linux.link", "provider_version": "1", "source": "rtnetlink", "reason": "written", "detail": {}}
    guards = GuardRegistry("agent", mutate, timer_factory=timer_factory,
                           admission_factory=lambda: ctl.admit("guard"),
                           marker_factory=lambda: ctl.begin_executor("guard"))
    guards.arm(guard_id="g", attempt_id="a", ifindex=1,
               expected_interface_name="test", guarded_mtu=1400,
               restore_mtu=1500, window_s=5)
    with pytest.raises(MaintenanceRefused):
        ctl.open_window()
    callbacks[0]()
    assert len(mutations) == 1
    assert guards.status("g")["phase"] == "fired"
    assert ctl.open_window().active


def test_unknown_executor_survives_restart_and_blocks_window(tmp_path):
    root = tmp_path / "maintenance"
    ctl = controller(root)
    ctl.initialize()
    with ctl.admit("dispatch"):
        ctl.begin_executor("test")
    restarted = controller(root)
    with pytest.raises(MaintenanceRefused, match="executor"):
        restarted.open_window()


@pytest.mark.parametrize("dispatched", [False, True])
def test_lost_agent_ipc_is_conservatively_blocked(tmp_path, monkeypatch, dispatched):
    ctl = controller(tmp_path / "maintenance")
    ctl.initialize()
    client = AgentClient(tmp_path / "unused.sock", maintenance=ctl)
    def fail(*args, **kwargs):
        raise AgentError("timeout", "synthetic", dispatched=dispatched)
    monkeypatch.setattr("localplane.backend.agent_client.ipc.call", fail)
    with pytest.raises(AgentError):
        client.call(Method.NETWORK_SET_INTERFACE_MTU, {})
    with pytest.raises(MaintenanceRefused, match="executor"):
        controller(ctl.root).open_window()


def test_unknown_response_and_capacity_never_evict_blockers(tmp_path, monkeypatch):
    ctl = controller(tmp_path / "maintenance")
    ctl.initialize()
    monkeypatch.setattr("localplane.maintenance.MAX_EXECUTORS", 1)
    client = AgentClient(tmp_path / "unused.sock", maintenance=ctl)
    monkeypatch.setattr("localplane.backend.agent_client.ipc.call",
                        lambda *args, **kwargs: {"mutation": {"outcome": "write_unknown"}})
    assert client.call(Method.NETWORK_SET_INTERFACE_MTU, {})["mutation"]["outcome"] == "write_unknown"
    with pytest.raises(AgentError, match="capacity"):
        client.call(Method.NETWORK_SET_INTERFACE_MTU, {})
    with pytest.raises(MaintenanceRefused, match="executor"):
        controller(ctl.root).open_window()


def test_sync_endpoint_lease_survives_caller_departure(tmp_path):
    from types import SimpleNamespace
    from fastapi import Request
    from localplane.backend.api.routes import MaintenanceRoute

    ctl = controller(tmp_path / "maintenance")
    ctl.initialize()
    entered, finish = threading.Event(), threading.Event()
    def endpoint(request: Request):
        entered.set()
        finish.wait(2)
        return {"ok": True}
    route = MaintenanceRoute("/api/v1/runs/{run_id}/apply", endpoint, methods=["POST"])
    request = SimpleNamespace(method="POST", url=SimpleNamespace(path="/api/v1/runs/r/apply"),
                              app=SimpleNamespace(state=SimpleNamespace(maintenance=ctl)))
    thread = threading.Thread(target=lambda: route.endpoint(request=request))
    thread.start()
    try:
        assert entered.wait(2)
        # The endpoint thread still holds its own lease regardless of its caller's lifetime.
        with pytest.raises(MaintenanceRefused):
            ctl.open_window()
    finally:
        finish.set()
        thread.join(2)
    assert ctl.open_window().active


def test_guard_unknown_firing_retains_marker_after_release(tmp_path):
    ctl = controller(tmp_path / "maintenance")
    ctl.initialize()
    callbacks = []
    class Timer:
        def cancel(self):
            pass
    def timer(delay, callback):
        callbacks.append(callback)
        return Timer()
    guards = GuardRegistry("agent", lambda **kw: {"outcome": "write_unknown"},
                           timer_factory=timer, admission_factory=lambda: ctl.admit("guard"),
                           marker_factory=lambda: ctl.begin_executor("guard"))
    guards.arm(guard_id="g", attempt_id="a", ifindex=1, expected_interface_name="fake",
               guarded_mtu=1400, restore_mtu=1500, window_s=5)
    callbacks[0]()
    assert guards.status("g")["phase"] == "fired"
    with pytest.raises(MaintenanceRefused, match="executor"):
        controller(ctl.root).open_window()


def test_guard_explicit_disarm_clears_marker(tmp_path):
    ctl = controller(tmp_path / "maintenance")
    ctl.initialize()
    class Timer:
        def cancel(self):
            pass
    guards = GuardRegistry("agent", lambda **kw: pytest.fail("must not fire"),
                           timer_factory=lambda delay, callback: Timer(),
                           admission_factory=lambda: ctl.admit("guard"),
                           marker_factory=lambda: ctl.begin_executor("guard"))
    guards.arm(guard_id="g", attempt_id="a", ifindex=1, expected_interface_name="fake",
               guarded_mtu=1400, restore_mtu=1500, window_s=5)
    assert guards.disarm("g")["phase"] == "disarmed"
    assert ctl.open_window().active


@pytest.mark.parametrize("dispatched", [True, False])
def test_agent_helper_transport_uncertainty_keeps_marker(tmp_path, dispatched):
    from localplane.agent.service import AgentService
    from localplane.helper.client import HelperError

    ctl = controller(tmp_path / "maintenance")
    ctl.initialize()
    class FakeHelper:
        def set_interface_mtu(self, **kwargs):
            raise HelperError("timeout", "synthetic", dispatched=dispatched)
    agent = AgentService.__new__(AgentService)
    agent._maintenance = ctl
    agent._helper = FakeHelper()
    agent._helper_socket = tmp_path / "not-a-real-socket"
    result = agent._set_interface_mtu(attempt_id="a", ifindex=1, expected_current_mtu=1500,
                                      desired_mtu=1400, expected_interface_name="fake")
    assert result["outcome"] in {"not_written", "write_unknown"}
    with pytest.raises(MaintenanceRefused, match="executor"):
        controller(ctl.root).open_window()


def test_offline_leases_refuse_unresolved_marker(tmp_path):
    ctl = controller(tmp_path / "maintenance")
    ctl.initialize()
    window = ctl.open_window()
    # Simulate a surviving externally-created blocker in an already active window.
    (ctl.root / "executor_test").write_text("unknown")
    with pytest.raises(MaintenanceRefused, match="executor"):
        with ctl.snapshot_lease(window.window_id):
            pytest.fail("snapshot admitted an unknown executor")
    with pytest.raises(MaintenanceRefused, match="executor"):
        with ctl.restore_lease({"window_id": window.window_id}):
            pytest.fail("restore admitted an unknown executor")


def test_missing_root_state_read_creates_nothing(tmp_path):
    root = tmp_path / "missing" / "maintenance"
    assert controller(root).state().status == "unknown"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("method,params,mutation", [
    (Method.DOCKER_CONTAINER_LIFECYCLE,
     {"attempt_id": "a", "container_id": "c", "action": "start"},
     {"attempt_id": "a", "container_id": "c", "action": "start", "outcome": "not_written", "http_status": 304, "provider": "docker", "method": "unix_socket_http", "reason": "already_in_that_state", "detail": {}}),
    (Method.SYSTEMD_SERVICE_LIFECYCLE,
     {"attempt_id": "a", "unit_id": "x.service", "action": "start"},
     {"attempt_id": "a", "unit_id": "x.service", "action": "start", "outcome": "not_written", "job_id": None, "job_result": None, "reason": "authorization_denied", "detail": {"manager_scope": "system", "transport": "system_bus_dbus", "dispatch_interface": "org.freedesktop.systemd1.Unit", "authorization_decision_point": "dispatch", "dbus_error": "org.example.Denied"}}),
])
def test_correlated_adapter_refusals_clear_backend_marker(tmp_path, monkeypatch, method, params, mutation):
    ctl = controller(tmp_path / "maintenance")
    ctl.initialize()
    client = AgentClient(tmp_path / "unused.sock", maintenance=ctl)
    monkeypatch.setattr("localplane.backend.agent_client.ipc.call",
                        lambda *a, **k: {"agent_instance_id": "agent", "mutation": mutation})
    client.call(method, params)
    assert ctl.open_window().active


@pytest.mark.parametrize("defect", ["attempt", "target", "action", "missing_agent", "unknown"])
def test_wrong_or_ambiguous_mutation_cannot_clear_marker(tmp_path, monkeypatch, defect):
    ctl = controller(tmp_path / "maintenance")
    ctl.initialize()
    client = AgentClient(tmp_path / "unused.sock", maintenance=ctl)
    params = {"attempt_id": "a", "container_id": "c", "action": "start"}
    mutation = {**params, "outcome": "written", "http_status": 204, "provider": "docker", "method": "unix_socket_http", "reason": "daemon_acknowledged", "detail": {}}
    result = {"agent_instance_id": "agent", "mutation": mutation}
    if defect == "attempt": mutation["attempt_id"] = "other"
    if defect == "target": mutation["container_id"] = "other"
    if defect == "action": mutation["action"] = "stop"
    if defect == "missing_agent": result.pop("agent_instance_id")
    if defect == "unknown": mutation["outcome"] = "write_unknown"
    monkeypatch.setattr("localplane.backend.agent_client.ipc.call", lambda *a, **k: result)
    client.call(Method.DOCKER_CONTAINER_LIFECYCLE, params)
    with pytest.raises(MaintenanceRefused, match="executor"):
        ctl.open_window()


@pytest.mark.parametrize("phase", ["fired", "disarmed", "armed_then_fired", "wrong_holder", "wrong_attempt"])
def test_guard_reply_requires_original_guard_holder_and_attempt(tmp_path, monkeypatch, phase):
    ctl = controller(tmp_path / "maintenance")
    ctl.initialize()
    client = AgentClient(tmp_path / "unused.sock", maintenance=ctl)
    params = {"guard_id": "g", "attempt_id": "a", "ifindex": 1,
              "guarded_mtu": 1400, "restore_mtu": 1500, "window_s": 5}
    mutation = {"attempt_id": "a", "ifindex": 1, "expected_current_mtu": 1400,
                "desired_mtu": 1500, "method": "netlink_rtm_newlink_mtu", "provider": "linux.link", "provider_version": "1", "source": "rtnetlink", "reason": "written", "outcome": "written", "detail": {}}
    guard = {**params, "holder_id": "holder", "phase": "disarmed" if phase == "disarmed" else "fired", "mutation": mutation}
    if phase == "wrong_holder": guard["holder_id"] = "other"
    if phase == "wrong_attempt": guard["attempt_id"] = "other"
    if phase == "armed_then_fired": guard["phase"] = "armed"
    monkeypatch.setattr("localplane.backend.agent_client.ipc.call", lambda *a, **k: {"agent_instance_id": "holder", "guard": dict(guard)})
    client.call(Method.NETWORK_ARM_MTU_GUARD, params)
    if phase == "armed_then_fired":
        with pytest.raises(MaintenanceRefused): ctl.open_window()
        guard["phase"] = "fired"
        client.call(Method.NETWORK_MTU_GUARD_STATUS, {"guard_id": "g"})
    if phase in {"wrong_holder", "wrong_attempt"}:
        with pytest.raises(MaintenanceRefused, match="executor"): ctl.open_window()
    else:
        assert ctl.open_window().active


@pytest.mark.parametrize("kind", ["docker", "systemd"])
def test_agent_definite_adapter_refusal_clears_its_marker(tmp_path, kind):
    from types import SimpleNamespace
    from localplane.agent.service import AgentService
    from localplane.agent.providers.docker import LifecycleResult
    from localplane.agent.providers.systemd import LifecycleDispatchResult

    ctl = controller(tmp_path / "maintenance")
    ctl.initialize()
    agent = AgentService.__new__(AgentService)
    agent._maintenance = ctl
    agent._instance_id = "agent"
    agent._require_capability = lambda *args: None
    agent._identity = lambda: SimpleNamespace(host_id="host")
    agent._docker_engine_version = lambda: None
    agent._systemd_manager_version = lambda: None
    if kind == "docker":
        agent._docker = SimpleNamespace(lifecycle=lambda target, action:
            LifecycleResult("not_written", "already_in_that_state", action, target, 304, {}))
        result = agent._docker_container_lifecycle({"attempt_id": "a", "container_id": "abc", "action": "start"})
    else:
        agent._lifecycle_receipts_lock = threading.Lock()
        agent._lifecycle_receipts = {}
        agent._systemd = SimpleNamespace(dispatch_service_lifecycle=lambda target, action:
            LifecycleDispatchResult(outcome="not_written", reason="authorization_denied", action=action,
                unit_id=target, detail={"manager_scope": "system", "transport": "system_bus_dbus", "dispatch_interface": "org.freedesktop.systemd1.Unit", "authorization_decision_point": "dispatch", "dbus_error": "org.example.Denied"}))
        result = agent._systemd_service_lifecycle({"attempt_id": "a", "unit_id": "x.service", "action": "start"})
    assert result["mutation"]["outcome"] == "not_written"
    assert ctl.open_window().active


def test_guard_timer_can_fire_before_arm_reply_without_leaking_marker(tmp_path, monkeypatch):
    ctl = controller(tmp_path / "maintenance")
    ctl.initialize()
    class Timer:
        def cancel(self): pass
    def timer(delay, callback):
        callback()
        return Timer()
    def mutate(**k):
        return {"attempt_id": k["attempt_id"], "ifindex": k["ifindex"],
                "expected_current_mtu": k["expected_current_mtu"], "desired_mtu": k["desired_mtu"],
                "outcome": "written", "method": "netlink_rtm_newlink_mtu", "provider": "linux.link", "provider_version": "1", "source": "rtnetlink", "reason": "written", "detail": {}}
    guards = GuardRegistry("holder", mutate, timer_factory=timer,
                           admission_factory=lambda: ctl.admit("guard"),
                           marker_factory=lambda: ctl.begin_executor("guard"))
    params = {"guard_id": "g", "attempt_id": "a", "ifindex": 1, "guarded_mtu": 1400,
              "restore_mtu": 1500, "window_s": 5, "expected_interface_name": "fake"}
    def call(*args, **kwargs):
        return {"agent_instance_id": "holder", "guard": guards.arm(**params)}
    monkeypatch.setattr("localplane.backend.agent_client.ipc.call", call)
    client = AgentClient(tmp_path / "unused.sock", maintenance=ctl)
    assert client.call(Method.NETWORK_ARM_MTU_GUARD, params)["guard"]["phase"] == "fired"
    assert ctl.open_window().active


def test_guard_status_wrong_holder_or_attempt_cannot_settle_original(tmp_path, monkeypatch):
    ctl = controller(tmp_path / "maintenance")
    ctl.initialize()
    client = AgentClient(tmp_path / "unused.sock", maintenance=ctl)
    params = {"guard_id": "g", "attempt_id": "a", "ifindex": 1,
              "guarded_mtu": 1400, "restore_mtu": 1500, "window_s": 5}
    guard = {**params, "holder_id": "holder", "phase": "armed", "mutation": None}
    response = {"agent_instance_id": "holder", "guard": guard}
    monkeypatch.setattr("localplane.backend.agent_client.ipc.call", lambda *a, **k: response)
    client.call(Method.NETWORK_ARM_MTU_GUARD, params)
    guard.update(phase="disarmed", holder_id="wrong")
    client.call(Method.NETWORK_MTU_GUARD_STATUS, {"guard_id": "g"})
    guard.update(holder_id="holder", attempt_id="wrong")
    client.call(Method.NETWORK_MTU_GUARD_STATUS, {"guard_id": "g"})
    with pytest.raises(MaintenanceRefused): ctl.open_window()
    guard["attempt_id"] = "a"
    client.call(Method.NETWORK_MTU_GUARD_STATUS, {"guard_id": "g"})
    assert ctl.open_window().active


def test_restore_transaction_interruption_exact_resume_and_completion(tmp_path):
    ctl = controller(tmp_path / "maintenance")
    ctl.initialize()
    window = ctl.open_window()
    binding = {"window_id": window.window_id, "generation": window.generation}
    with pytest.raises(RuntimeError):
        with ctl.restore_transaction(binding, snapshot_id="snapshot-a") as transaction:
            transaction_id = transaction.transaction_id
            assert transaction.window == window
            raise RuntimeError("synthetic interrupted restore")
    assert ctl.state().refusal["code"] == "maintenance_restore_incomplete"
    with pytest.raises(MaintenanceRefused, match="restore"):
        with ctl.startup_lease(): pass
    with pytest.raises(MaintenanceRefused, match="restore"):
        ctl.close_window(window.window_id, accepted=True)
    with pytest.raises(MaintenanceRefused, match="restore"):
        with ctl.snapshot_lease(window.window_id): pass
    with pytest.raises(MaintenanceRefused, match="restore"):
        with ctl.restore_lease(binding): pass
    with pytest.raises(MaintenanceRefused):
        with ctl.restore_transaction(binding, snapshot_id="snapshot-b"): pass
    with pytest.raises(MaintenanceRefused):
        with ctl.restore_transaction({**binding, "generation": window.generation + 1}, snapshot_id="snapshot-a"): pass
    restarted = controller(ctl.root)
    with restarted.restore_transaction(binding, snapshot_id="snapshot-a") as resumed:
        assert resumed.transaction_id == transaction_id
        with pytest.raises(MaintenanceRefused): restarted.service_lifetime()
        resumed.complete()
    with pytest.raises(MaintenanceRefused): resumed.complete()
    assert restarted.state().refusal is None
    restarted.close_window(window.window_id, accepted=True)


def test_corrupt_restore_marker_blocks_even_known_active_startup(tmp_path):
    ctl = controller(tmp_path / "maintenance")
    ctl.initialize()
    window = ctl.open_window()
    (ctl.root / "restore.json").write_text("invalid")
    assert ctl.state().refusal["code"] == "maintenance_restore_incomplete"
    with pytest.raises(MaintenanceRefused) as exc:
        with ctl.startup_lease(): pass
    assert exc.value.code == "maintenance_restore_incomplete"
    with pytest.raises(MaintenanceRefused): ctl.close_window(window.window_id, accepted=True)
    with pytest.raises(MaintenanceRefused):
        with ctl.restore_transaction({"window_id": window.window_id, "generation": window.generation}, snapshot_id="s"): pass


def test_restore_marker_survives_process_death(tmp_path):
    import subprocess
    import sys
    ctl = controller(tmp_path / "maintenance")
    ctl.initialize()
    window = ctl.open_window()
    script = ("import os,sys; from localplane.maintenance import MaintenanceController; "
              "c=MaintenanceController(sys.argv[1],service='child'); "
              "b={'window_id':sys.argv[2],'generation':int(sys.argv[3])}; "
              "tx=c.restore_transaction(b,snapshot_id='snap'); tx.__enter__(); os._exit(19)")
    child = subprocess.run([sys.executable, "-c", script, str(ctl.root), window.window_id,
                            str(window.generation)], check=False)
    assert child.returncode == 19
    assert ctl.state().refusal["code"] == "maintenance_restore_incomplete"
    with ctl.restore_transaction({"window_id": window.window_id, "generation": window.generation},
                                 snapshot_id="snap") as tx:
        tx.complete()
    assert ctl.state().refusal is None


def test_executor_bookkeeping_contention_does_not_leak_completed_markers(tmp_path):
    from localplane.maintenance import _locked, EXECUTOR_LOCK_FILENAME
    ctl = controller(tmp_path / "maintenance")
    ctl.initialize()
    errors = []
    ready = threading.Event()
    def operation():
        ready.set()
        try:
            token = ctl.begin_executor("synthetic")
            assert token.settle()
        except BaseException as exc:
            errors.append(exc)
    with _locked(ctl.root / EXECUTOR_LOCK_FILENAME, exclusive=True, blocking=True,
                 timeout_s=1, root=ctl.root):
        threads = [threading.Thread(target=operation) for _ in range(6)]
        for thread in threads: thread.start()
        assert ready.wait(1)
        import time
        time.sleep(0.15)
    for thread in threads: thread.join(5)
    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert ctl.open_window().active


@pytest.mark.parametrize("code", ["maintenance_busy", "maintenance_window_active"])
def test_agent_preserves_actual_maintenance_refusal_code(tmp_path, code):
    from types import SimpleNamespace
    from localplane.agent.service import AgentService
    from localplane.maintenance import _locked, CONTROL_LOCK_FILENAME
    from localplane.protocol.wire import ProtocolError
    from localplane.backend.api.routes import _status_for
    ctl = controller(tmp_path / "maintenance")
    ctl.initialize()
    agent = AgentService.__new__(AgentService)
    agent._maintenance = ctl
    agent._require_capability = lambda *_: None
    agent._guards = SimpleNamespace(arm=lambda **kwargs: pytest.fail("guard should not arm"))
    if code == "maintenance_window_active":
        ctl.open_window()
        lock = None
    else:
        lock = _locked(ctl.root / CONTROL_LOCK_FILENAME, exclusive=True, blocking=True,
                       timeout_s=0, root=ctl.root)
        lock.__enter__()
    try:
        with pytest.raises(ProtocolError) as exc:
            agent._network_arm_mtu_guard({"guard_id": "g", "attempt_id": "a", "ifindex": 1,
                "guarded_mtu": 1400, "restore_mtu": 1500, "window_s": 5})
        client = AgentClient(tmp_path / "unused.sock", maintenance=ctl)
        with pytest.raises(AgentError) as client_exc:
            client.call(Method.NETWORK_SET_INTERFACE_MTU, {})
        assert client_exc.value.code == code
    finally:
        if lock is not None: lock.__exit__(None, None, None)
    assert exc.value.code == code
    assert _status_for(AgentError(code, "synthetic")) == 503
