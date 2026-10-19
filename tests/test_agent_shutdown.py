"""Process lifecycle coverage for the agent's signal-owned server loop."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from localplane.protocol.wire import CODEC


_CHILD = """
import os
import time
from pathlib import Path
from localplane.agent import __main__ as agent

class Service:
    instance_id = "test-agent"
    capabilities = ()
    def handle(self, method, params):
        marker = os.environ.get("LOCALPLANE_TEST_ACTIVE")
        if marker:
            Path(marker).write_text("started")
            time.sleep(10)
        return {}

agent.AgentService = lambda **kwargs: Service()
raise SystemExit(agent.main())
"""


def _start_agent(tmp_path: Path, *, active: Path | None = None) -> tuple[subprocess.Popen, Path]:
    socket_path = tmp_path / "run" / "agent.sock"
    environment = os.environ | {
        "LOCALPLANE_AGENT_SOCKET": str(socket_path),
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
    }
    if active is not None:
        environment["LOCALPLANE_TEST_ACTIVE"] = str(active)
    process = subprocess.Popen([sys.executable, "-c", _CHILD], env=environment)
    _wait_for(lambda: socket_path.exists())
    if active is None:
        _wait_for(lambda: _agent_answers(socket_path))
    return process, socket_path


def _wait_for(predicate, timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("timed out waiting for agent lifecycle state")
        time.sleep(0.01)


def _agent_answers(socket_path: Path) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.1)
            client.connect(str(socket_path))
            client.sendall(CODEC.request("ready", "agent.hello"))
            return bool(client.recv(4096))
    except OSError:
        return False


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_idle_signal_removes_socket_and_allows_restart(tmp_path: Path, signum: signal.Signals):
    process, socket_path = _start_agent(tmp_path)
    process.send_signal(signum)
    assert process.wait(timeout=3) == 0
    assert not socket_path.exists()

    restarted, restarted_socket = _start_agent(tmp_path)
    restarted.send_signal(signum)
    assert restarted.wait(timeout=3) == 0
    assert not restarted_socket.exists()


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_active_request_does_not_deadlock_signal_shutdown(tmp_path: Path, signum: signal.Signals):
    active = tmp_path / "active"
    process, socket_path = _start_agent(tmp_path, active=active)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        client.sendall(CODEC.request("active", "agent.hello"))
        _wait_for(active.exists)
        process.send_signal(signum)
        assert process.wait(timeout=3) == 0
    assert not socket_path.exists()


def test_signal_during_startup_is_requested_before_serving(monkeypatch, tmp_path: Path):
    """A signal delivered while handlers are installed cannot start a stranded loop."""
    from localplane.agent import __main__ as agent

    calls = []

    class Server:
        def __init__(self, *_args):
            pass

        def handle_request(self):
            calls.append("request")

        def server_close(self):
            calls.append("close")

    monkeypatch.setattr(agent, "AgentService", lambda **_kwargs: object())
    monkeypatch.setattr(agent, "AgentServer", Server)
    monkeypatch.setattr(agent, "configure_logging", lambda *_args: None)

    def install(_signum, handler):
        handler(signal.SIGTERM, None)

    monkeypatch.setattr(agent.signal, "signal", install)
    monkeypatch.setenv("LOCALPLANE_AGENT_SOCKET", str(tmp_path / "agent.sock"))
    assert agent.main() == 0
    assert calls == ["close"]
