"""The agent over a real AF_UNIX socket, with a real client.

Two processes' worth of behaviour, in one process: a genuine socket, genuine framing,
genuine peer credential checks. What it does not test is privilege separation, because
there is none to test yet — the agent runs unprivileged and says so.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
from pathlib import Path
from typing import Iterator

import pytest

from localplane.agent.server import AgentServer
from localplane.agent.service import AgentService
from localplane.backend.agent_client import AgentClient, AgentError
from localplane.protocol.wire import PROTOCOL_VERSION, ErrorCode
from tests.conftest import FakeRunner


@pytest.fixture
def agent(
    tmp_path: Path,
    fake_root: Path,
    populated_sysfs: Path,
    working_runner: FakeRunner,
    absent_docker: Path,
):
    service = AgentService(
        root=fake_root,
        sysfs_net=populated_sysfs,
        runner=working_runner,
        docker_socket=absent_docker,
    )
    server = AgentServer(tmp_path / "run" / "agent.sock", service)
    thread = server.serve_in_thread()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture
def client(agent: AgentServer) -> AgentClient:
    return AgentClient(agent.socket_path, timeout_s=10)


# ------------------------------------------------------------------------------- startup


def test_the_agent_starts_and_listens(agent: AgentServer):
    assert agent.socket_path.exists()
    assert stat.S_ISSOCK(agent.socket_path.stat().st_mode)


def test_the_socket_is_not_world_reachable(agent: AgentServer):
    mode = stat.S_IMODE(agent.socket_path.stat().st_mode)
    assert mode == 0o600, f"socket mode is {oct(mode)}"


def test_the_socket_directory_is_private(agent: AgentServer):
    assert stat.S_IMODE(agent.socket_path.parent.stat().st_mode) == 0o700


def test_the_socket_is_removed_on_shutdown(
    tmp_path: Path,
    fake_root,
    populated_sysfs,
    working_runner,
    absent_docker: Path,
):
    service = AgentService(
        root=fake_root,
        sysfs_net=populated_sysfs,
        runner=working_runner,
        docker_socket=absent_docker,
    )
    server = AgentServer(tmp_path / "run2" / "agent.sock", service)
    path = server.socket_path
    server.server_close()
    assert not path.exists()


def test_a_stale_socket_is_replaced(
    tmp_path: Path,
    fake_root,
    populated_sysfs,
    working_runner,
    absent_docker: Path,
):
    path = tmp_path / "run3" / "agent.sock"
    path.parent.mkdir(parents=True)
    path.write_text("")  # not a socket, and nothing is listening
    service = AgentService(
        root=fake_root,
        sysfs_net=populated_sysfs,
        runner=working_runner,
        docker_socket=absent_docker,
    )
    server = AgentServer(path, service)
    try:
        assert stat.S_ISSOCK(path.stat().st_mode)
    finally:
        server.server_close()


def test_a_second_agent_will_not_take_a_live_socket(
    agent: AgentServer,
    fake_root,
    populated_sysfs,
    working_runner,
    absent_docker: Path,
):
    service = AgentService(
        root=fake_root,
        sysfs_net=populated_sysfs,
        runner=working_runner,
        docker_socket=absent_docker,
    )
    with pytest.raises(RuntimeError, match="already listening"):
        AgentServer(agent.socket_path, service)


# ------------------------------------------------------------------------------- methods


def test_hello_reports_identity_capabilities_and_privilege(client: AgentClient):
    hello = client.hello()
    assert hello["agent"]["transport"] == "af_unix"
    assert hello["agent"]["process_isolated"] is True
    assert hello["agent"]["privilege"] == ("root" if os.geteuid() == 0 else "unprivileged")
    # Every method that can change the host is named, and the assertion is the names
    # rather than a count: a count is what fails to notice a swap. `network.arm_mtu_guard`
    # is among them because arming a connection guard writes nothing and can cause a kernel
    # write with no further request. `systemd.service_lifecycle` is among them because it
    # asks the system manager to run a job — under whatever PolicyKit allows at the moment
    # it asks, which is a decision this process makes no attempt to predict.
    assert hello["agent"]["mutating_methods"] == [
        "docker.container_lifecycle",
        "network.arm_mtu_guard",
        "network.set_interface_mtu",
        "systemd.service_lifecycle",
    ]
    assert hello["host"]["host_id"].startswith("host_")
    assert {c["capability"] for c in hello["capabilities"]} == {
        "storage.filesystems.observe",
        "host.observe",
        "network.observe",
        "network.providers.observe",
        "network.route.observe",
        "network.interface.set_mtu",
        "network.interface.mtu_guard",
        "docker.containers.observe",
        "docker.container.lifecycle",
        "systemd.units.observe",
        "systemd.unit.journal",
        "systemd.service.lifecycle",
        "systemd.service.lifecycle_context.observe",
    }
    # And the agent itself holds no privilege for it: the write lives behind a socket in a
    # separate process, and this one reports what it actually is.
    assert hello["agent"]["helper_socket"]


def test_host_identify_returns_the_host(client: AgentClient):
    assert client.identify_host()["host"]["identity_basis"] == "machine_id"


def test_capabilities_list_reprobes(client: AgentClient):
    capabilities = {c["capability"]: c for c in client.list_capabilities()}
    assert capabilities["network.observe"]["status"] == "available"
    assert capabilities["network.observe"]["mutating"] is False


def test_observe_interfaces_returns_the_sweep(client: AgentClient):
    payload = client.observe_interfaces()
    observation = payload["observation"]
    assert observation["status"] == "ok"
    assert len(observation["interfaces"]) == 9
    assert payload["host_id"].startswith("host_")


def test_observe_interfaces_with_a_filter(client: AgentClient):
    observation = client.observe_interfaces(["eth0", "ghost0"])["observation"]
    assert [i["facts"]["name"] for i in observation["interfaces"]] == ["eth0"]
    assert observation["missing"] == ["ghost0"]


# ------------------------------------------------------------------------------- refusals


def test_an_unsupported_method_is_refused_with_a_code(client: AgentClient):
    with pytest.raises(AgentError) as caught:
        client.call("agent.hello", {"unexpected": 1})
    assert caught.value.code == ErrorCode.UNKNOWN_FIELD


def test_an_invalid_interface_name_is_refused(client: AgentClient):
    with pytest.raises(AgentError) as caught:
        client.observe_interfaces(["eth0; id"])
    assert caught.value.code == ErrorCode.INVALID_PARAMS


def test_a_non_list_names_parameter_is_refused(client: AgentClient):
    with pytest.raises(AgentError) as caught:
        client.call("network.observe_interfaces", {"names": "eth0"})
    assert caught.value.code == ErrorCode.INVALID_PARAMS


def test_a_raw_command_request_is_refused_by_the_agent(agent: AgentServer):
    """Sent past the client, straight onto the wire. There is no method to accept it."""
    reply = _send_raw(
        agent.socket_path,
        {
            "localplane_protocol": PROTOCOL_VERSION,
            "request_id": "r1",
            "method": "exec",
            "params": {"argv": ["id"]},
        },
    )
    assert reply["ok"] is False
    assert reply["error"]["code"] == "unsupported_method"


def test_an_unknown_envelope_field_is_refused_on_the_wire(agent: AgentServer):
    reply = _send_raw(
        agent.socket_path,
        {
            "localplane_protocol": PROTOCOL_VERSION,
            "request_id": "r1",
            "method": "agent.hello",
            "params": {},
            "command": "/bin/sh",
        },
    )
    assert reply["error"]["code"] == "unknown_field"


def test_a_wrong_protocol_version_is_refused(agent: AgentServer):
    reply = _send_raw(
        agent.socket_path,
        {"localplane_protocol": "0", "request_id": "r1", "method": "agent.hello", "params": {}},
    )
    assert reply["error"]["code"] == "protocol_version_unsupported"


def test_garbage_is_refused_without_killing_the_agent(agent: AgentServer, client: AgentClient):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(str(agent.socket_path))
        sock.sendall(b"this is not json\n")
        assert b"malformed_message" in sock.recv(65536)
    assert client.hello()["agent"]["transport"] == "af_unix", "the agent is still serving"


def test_the_agent_survives_an_abandoned_connection(agent: AgentServer, client: AgentClient):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(agent.socket_path))
    sock.close()
    assert client.hello()


def test_a_capability_that_is_unavailable_blocks_its_operation(
    tmp_path: Path,
    fake_root: Path,
    absent_docker: Path,
):
    """The gate is the probed capability, not the caller's optimism."""
    service = AgentService(
        root=fake_root,
        sysfs_net=tmp_path / "no-sysfs-here",
        runner=FakeRunner({}),
        docker_socket=absent_docker,
    )
    server = AgentServer(tmp_path / "run4" / "agent.sock", service)
    server.serve_in_thread()
    try:
        with pytest.raises(AgentError) as caught:
            AgentClient(server.socket_path).observe_interfaces()
        assert caught.value.code == ErrorCode.CAPABILITY_UNAVAILABLE
        assert caught.value.detail["reason"] == "sysfs_unreadable"
    finally:
        server.shutdown()
        server.server_close()


# ------------------------------------------------------------------------- client failure


def test_lifecycle_uses_a_fixed_typed_budget_without_changing_other_calls(monkeypatch):
    import inspect
    from localplane import ipc
    from localplane.protocol.wire import (
        SYSTEMD_LIFECYCLE_CLIENT_TIMEOUT_S, SYSTEMD_LIFECYCLE_JOB_TIMEOUT_S,
        SYSTEMD_LIFECYCLE_DBUS_TIMEOUT_S,
    )
    calls = []
    def record(path, codec, timeout, method, params, *args, **kwargs):
        calls.append((str(method), timeout, params))
        return {}
    monkeypatch.setattr(ipc, "call", record)
    client = AgentClient("unused.sock", timeout_s=0.001)
    client.systemd_service_lifecycle(attempt_id="a", unit_id="alpha.service", action="start")
    client.hello()
    client.container_lifecycle(attempt_id="a", container_id="b", action="start")
    assert SYSTEMD_LIFECYCLE_CLIENT_TIMEOUT_S == 150.0
    assert SYSTEMD_LIFECYCLE_CLIENT_TIMEOUT_S == (
        SYSTEMD_LIFECYCLE_JOB_TIMEOUT_S + 10 * SYSTEMD_LIFECYCLE_DBUS_TIMEOUT_S + 10
    )
    assert calls == [
        ("systemd.service_lifecycle", 150.0,
         {"attempt_id": "a", "unit_id": "alpha.service", "action": "start"}),
        ("agent.hello", 0.001, None),
        ("docker.container_lifecycle", 0.001,
         {"attempt_id": "a", "container_id": "b", "action": "start"}),
    ]
    assert list(inspect.signature(client.systemd_service_lifecycle).parameters) == [
        "attempt_id", "unit_id", "action",
    ]


@pytest.mark.parametrize("answer_after,closed", [(91.0, False), (140.0, False), (151.0, False), (1.0, True)])
def test_lifecycle_transport_waits_beyond_generic_budget_but_preserves_loss_truth(
    monkeypatch, answer_after, closed,
):
    """Real IPC framing/error classification with a deterministic socket clock.

    No 150-second sleep or host connection: the scripted reader enforces the actual
    timeout passed to the socket, including a response after the provider's job budget.
    """
    import io
    from contextlib import contextmanager
    from localplane import ipc
    requests = []

    class ScriptedSocket:
        def __enter__(self):
            self.writer = io.BytesIO()
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, timeout):
            self.timeout = timeout

        def connect(self, path):
            pass

        @contextmanager
        def makefile(self, mode):
            yield self.writer if mode == "wb" else self

        def readline(self, size):
            request = json.loads(self.writer.getvalue())
            requests.append(request)
            if answer_after > self.timeout:
                raise socket.timeout()
            if closed:
                return b""
            return json.dumps({
                "localplane_protocol": PROTOCOL_VERSION,
                "request_id": request["request_id"], "ok": True,
                "result": {"outcome": "written", "reason": "job_completed"},
            }).encode() + b"\n"

    monkeypatch.setattr(ipc.socket, "socket", lambda *a, **k: ScriptedSocket())
    client = AgentClient("unused.sock")
    def dispatch():
        return client.systemd_service_lifecycle(
            attempt_id="attempt", unit_id="alpha.service", action="start",
        )
    if answer_after > 150 or closed:
        with pytest.raises(AgentError) as caught:
            dispatch()
        assert caught.value.dispatched is True
        assert caught.value.code == (ErrorCode.AGENT_UNAVAILABLE if closed else ErrorCode.TIMEOUT)
    else:
        assert dispatch() == {"outcome": "written", "reason": "job_completed"}
    assert len(requests) == 1  # no automatic retry
    assert requests[0]["params"] == {
        "attempt_id": "attempt", "unit_id": "alpha.service", "action": "start",
    }


def test_an_absent_agent_is_a_structured_error_not_an_empty_result(tmp_path: Path):
    with pytest.raises(AgentError) as caught:
        AgentClient(tmp_path / "nothing.sock").hello()
    assert caught.value.code == ErrorCode.AGENT_UNAVAILABLE


def test_a_connection_that_answers_nothing_is_an_error(tmp_path: Path):
    path = tmp_path / "silent.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def accept_and_close() -> None:
        connection, _ = listener.accept()
        connection.close()

    threading.Thread(target=accept_and_close, daemon=True).start()
    try:
        with pytest.raises(AgentError) as caught:
            AgentClient(path, timeout_s=5).hello()
        assert caught.value.code == ErrorCode.AGENT_UNAVAILABLE
    finally:
        listener.close()


def test_a_mismatched_request_id_is_refused(tmp_path: Path):
    """Correlation is checked, so a stale answer cannot be read as the current one."""
    path = tmp_path / "confused.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def answer_wrongly() -> None:
        connection, _ = listener.accept()
        with connection.makefile("rb") as reader, connection.makefile("wb") as writer:
            reader.readline()
            writer.write(
                json.dumps(
                    {
                        "localplane_protocol": PROTOCOL_VERSION,
                        "request_id": "somebody-elses-request",
                        "ok": True,
                        "result": {},
                    }
                ).encode()
                + b"\n"
            )
            writer.flush()
        connection.close()

    threading.Thread(target=answer_wrongly, daemon=True).start()
    try:
        with pytest.raises(AgentError) as caught:
            AgentClient(path, timeout_s=5).hello()
        assert caught.value.code == ErrorCode.MALFORMED_MESSAGE
    finally:
        listener.close()


def test_an_unauthorized_peer_uid_is_refused(
    tmp_path: Path,
    fake_root,
    populated_sysfs,
    working_runner,
    absent_docker: Path,
):
    """SO_PEERCRED is checked before a byte of the request is parsed."""
    service = AgentService(
        root=fake_root,
        sysfs_net=populated_sysfs,
        runner=working_runner,
        docker_socket=absent_docker,
    )
    server = AgentServer(
        tmp_path / "run5" / "agent.sock", service, allowed_uids={os.geteuid() + 12345}
    )
    server.serve_in_thread()
    try:
        with pytest.raises(AgentError) as caught:
            AgentClient(server.socket_path, timeout_s=5).hello()
        assert caught.value.code == ErrorCode.UNAUTHORIZED_PEER
    finally:
        server.shutdown()
        server.server_close()


def _send_raw(socket_path: Path, payload: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(str(socket_path))
        sock.sendall(json.dumps(payload).encode() + b"\n")
        with sock.makefile("rb") as reader:
            return json.loads(reader.readline())


def test_an_uncorrelated_result_is_still_refused(tmp_path: Path):
    """The sentinel exempts refusals from correlation, never data."""
    from localplane.protocol.wire import UNCORRELATED_REQUEST_ID

    path = tmp_path / "uncorrelated.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def answer_uncorrelated_success() -> None:
        connection, _ = listener.accept()
        with connection.makefile("rb") as reader, connection.makefile("wb") as writer:
            reader.readline()
            writer.write(
                json.dumps(
                    {
                        "localplane_protocol": PROTOCOL_VERSION,
                        "request_id": UNCORRELATED_REQUEST_ID,
                        "ok": True,
                        "result": {"host": {}},
                    }
                ).encode()
                + b"\n"
            )
            writer.flush()
        connection.close()

    threading.Thread(target=answer_uncorrelated_success, daemon=True).start()
    try:
        with pytest.raises(AgentError) as caught:
            AgentClient(path, timeout_s=5).hello()
        assert caught.value.code == ErrorCode.MALFORMED_MESSAGE
    finally:
        listener.close()
