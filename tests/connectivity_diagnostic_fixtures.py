"""Real orchestration and executor, exclusively injected operating-system I/O."""
import json
import time
from types import SimpleNamespace

from localplane.agent import service
from localplane.agent.diagnostic_registry import DiagnosticRegistry
from localplane.agent.diagnostics import DiagnosticExecutor, DiagnosticLimiter, DnsAnswer
from localplane.agent.providers import diagnostic_exclusions
from localplane.backend.agent_client import AgentError
from localplane.backend.config import Settings
from localplane.protocol.diagnostic_policy import LoadedDiagnosticPolicy, parse_diagnostic_policy
from tests.conftest import AuthenticatedTestClient, create_authenticated_app
from tests.test_connectivity_store_isolation import fixture_context


def diagnostic_rig(tmp_path, monkeypatch, *, document=None):
    ctx, object_id, boot = fixture_context(tmp_path)
    if document is None:
        document = {"version": 2, "dns": ["nas.lab.test."],
                    "tcp": [{"address": address, "port": 443} for address in ("192.168.247.2", "fd42:6::2")],
                    "control_plane": {"complete": True, "addresses": ["192.168.247.254"]}}
    loaded = LoadedDiagnosticPolicy("valid", parse_diagnostic_policy(json.dumps(document).encode()))
    rig = SimpleNamespace(ctx=ctx, object_id=object_id, boot=boot, loaded=loaded, sockets=[], queries=[],
                          local=("192.168.247.1", "fd42:6::1"), broadcast=("192.168.247.3",),
                          namespace="net:[42]", clock_namespace="time:[42]", error=None, close_error=False,
                          collected=0, execute_calls=0, fail_execute=None, status_override=None)
    monkeypatch.setattr(diagnostic_exclusions, "kernel_context", lambda: (boot, rig.namespace))
    monkeypatch.setattr(diagnostic_exclusions, "clock_namespace", lambda: rig.clock_namespace)
    def collect(**kwargs):
        rig.collected += 1
        return diagnostic_exclusions.KernelExclusions(boot, rig.namespace, rig.local, rig.broadcast)
    monkeypatch.setattr(diagnostic_exclusions, "collect", collect)
    class Socket:
        def __init__(self, family, kind):
            self.peer = None
            self.closed = False
            rig.sockets.append(self)
        def settimeout(self, value):
            assert 0 < value <= 3
            self.timeout = value
        def connect(self, value):
            self.peer = value
            if rig.error:
                raise rig.error
        def getsockname(self):
            return ("fd42:6::1", 32000, 0, 0) if len(self.peer) == 4 else ("192.168.247.1", 32000)
        def getpeername(self):
            return self.peer
        def close(self):
            if rig.close_error:
                raise OSError("fixture cleanup unknown")
            self.closed = True
    def executor(*args, **kwargs):
        return DiagnosticExecutor(*args, **kwargs, socket_factory=Socket)
    monkeypatch.setattr(service, "DiagnosticExecutor", executor)
    monkeypatch.setattr(service, "_DIAGNOSTIC_LIMITER", DiagnosticLimiter())
    monkeypatch.setattr("localplane.backend.api.routes._BACKEND_DIAGNOSTICS", DiagnosticLimiter())
    def resolve(name, record_type, deadline):
        rig.queries.append((name, record_type))
        return DnsAnswer("resolved", ("192.168.247.2",) if record_type == "A" else ("fd42:6::2",), False)
    monkeypatch.setattr(service, "resolve_dns", resolve)
    agent = service.AgentService.__new__(service.AgentService)
    agent._identity = lambda: SimpleNamespace(host_id="host", boot_id=boot)
    agent._instance_id = "agent"
    agent._maintenance = None
    agent._diagnostic_policy = loaded
    agent._diagnostic_registry = DiagnosticRegistry()
    rig.agent = agent
    rig.prepare = lambda params, **kwargs: agent._diagnostic_preflight(params)
    def execute(kind, params, **kwargs):
        rig.execute_calls += 1
        if rig.fail_execute:
            return rig.fail_execute(kind, params)
        return agent._diagnostic_dns(params) if kind == "dns" else agent._diagnostic_tcp(params)
    rig.execute = execute
    def status(**kwargs):
        kwargs.pop("timeout_s", None)
        if rig.status_override is not None:
            return rig.status_override(kwargs)
        return agent._diagnostic_status(kwargs)
    rig.status = status
    monkeypatch.setattr("localplane.backend.app.load_diagnostic_policy", lambda: loaded)
    settings = Settings(tmp_path / "fixture.db", tmp_path / "absent.sock", 1, 60, "WARNING", False)
    rig.app = create_authenticated_app(settings, ctx.database)
    rig.client = AuthenticatedTestClient(rig.app)
    rig.path = f"/api/v1/network/interfaces/{object_id}/connectivity/diagnostics"
    def connect_client():
        client = rig.app.state.context.client
        monkeypatch.setattr(client, "diagnostic_preflight", rig.prepare)
        monkeypatch.setattr(client, "diagnostic_execute", rig.execute)
        monkeypatch.setattr(client, "diagnostic_status", rig.status)
    rig.connect_client = connect_client
    def body(kind="tcp", address="192.168.247.2"):
        target = {"address": address, "port": 443} if kind == "tcp" else {"name": "nas.lab.test.", "record_type": "A"}
        return {"kind": kind, "policy_digest": loaded.policy.digest, **target}
    rig.body = body
    rig.values = lambda kind="tcp": {**body(kind), "expected_host_id": "host", "expected_agent_instance_id": "agent",
        "expected_namespace": rig.namespace, "expected_clock_namespace": rig.clock_namespace,
        "absolute_monotonic_deadline": time.monotonic() + 5}
    return rig
