"""API to real agent guards/executor; no live kernel, bus, socket or DNS."""
import errno
import time

import pytest

from localplane.backend.agent_client import AgentError
from localplane.protocol.wire import ProtocolError
from tests.connectivity_diagnostic_fixtures import diagnostic_rig


@pytest.fixture
def rig(tmp_path, monkeypatch):
    value = diagnostic_rig(tmp_path, monkeypatch)
    with value.client:
        value.connect_client()
        yield value
    value.ctx.database.close()


@pytest.mark.parametrize("address", ["192.168.247.2", "fd42:6::2"])
def test_api_real_agent_executor_private_success_and_observed_tuple(rig, address):
    answer = rig.client.post(rig.path, json=rig.body(address=address))
    assert answer.status_code == 200, answer.text
    result = answer.json()
    assert result["outcome"] == "connected", result
    assert result["remote"] == {"address": address, "port": 443}
    assert result["local"]["port"] == 32000
    assert result["vantage"] == "agent network namespace net:[42]"
    assert len(rig.sockets) == 1 and rig.sockets[0].closed and rig.collected == 2
    assert not rig.queries and "completion_proven" not in result


def test_dns_is_independent_and_both_kinds_share_limits(rig):
    result = rig.client.post(rig.path, json=rig.body("dns")).json()
    assert result["outcome"] == "resolved", result
    assert len(rig.queries) == 1 and not rig.sockets
    for _ in range(5):
        assert rig.client.post(rig.path, json=rig.body()).json()["outcome"] == "connected"
    assert rig.client.post(rig.path, json=rig.body("dns")).json()["outcome"] == "rate_limited"
    assert len(rig.queries) == 1 and len(rig.sockets) == 5


@pytest.mark.parametrize("code,outcome", [(errno.ECONNREFUSED, "refused"), (errno.ENETUNREACH, "no_route"), (errno.EACCES, "permission_denied"), (errno.ETIMEDOUT, "timeout")])
def test_proven_socket_outcomes(rig, code, outcome):
    rig.error = OSError(code, "private fixture detail")
    result = rig.client.post(rig.path, json=rig.body()).json()
    assert result["outcome"] == outcome, result
    assert len(rig.sockets) == 1 and rig.sockets[0].closed and result["remote"] is None
    assert "private fixture" not in result["detail"]


@pytest.mark.parametrize("guard", ["local", "broadcast", "agent-policy", "namespace", "unknown", "malformed"])
def test_live_and_independent_agent_guard_refusals_no_socket(rig, monkeypatch, guard):
    from localplane.agent.providers import diagnostic_exclusions
    from localplane.protocol.diagnostic_policy import LoadedDiagnosticPolicy, parse_diagnostic_policy
    if guard == "local":
        rig.local += ("192.168.247.2",)
    elif guard == "broadcast":
        rig.broadcast += ("192.168.247.2",)
    elif guard == "agent-policy":
        rig.agent._diagnostic_policy = LoadedDiagnosticPolicy("valid", parse_diagnostic_policy(b'{"version":1,"dns":[],"tcp":[]}'))
    elif guard == "namespace":
        original = rig.prepare
        def changed(params, **kwargs):
            params = {**params, "expected_namespace": "net:[99]"}
            return original(params, **kwargs)
        monkeypatch.setattr(rig.app.state.context.client, "diagnostic_preflight", changed)
    elif guard == "unknown":
        monkeypatch.setattr(diagnostic_exclusions, "collect", lambda **kwargs: (_ for _ in ()).throw(ValueError("unknown")))
    else:
        monkeypatch.setattr(rig.app.state.context.client, "diagnostic_preflight", lambda *args, **kwargs: {"complete": True})
    result = rig.client.post(rig.path, json=rig.body()).json()
    assert result["outcome"] not in ("connected", "resolved"), result
    assert not rig.sockets


def test_fresh_agent_recheck_catches_change_after_backend_preflight(rig, monkeypatch):
    original = rig.execute
    def changed(kind, params, **kwargs):
        rig.local += ("192.168.247.2",)
        return original(kind, params, **kwargs)
    monkeypatch.setattr(rig.app.state.context.client, "diagnostic_execute", changed)
    result = rig.client.post(rig.path, json=rig.body()).json()
    assert result["outcome"] == "denied", result
    assert not rig.sockets


def test_uncertain_dispatch_never_recovers_from_global_idle(rig):
    saved = {}
    def timeout(kind, params):
        saved.update(params)
        raise AgentError("timeout", "fixture", dispatched=True)
    rig.fail_execute = timeout
    assert rig.client.post(rig.path, json=rig.body()).json()["outcome"] == "unavailable"
    assert rig.client.post(rig.path, json=rig.body()).json()["outcome"] == "busy"
    assert not rig.sockets  # Earlier accepted request has not entered the executor yet.
    rig.fail_execute = None
    result = rig.agent._diagnostic_tcp(saved)
    assert result["completion_proven"] is True and len(rig.sockets) == 1
    assert rig.client.post(rig.path, json=rig.body()).json()["outcome"] == "connected"
    assert len(rig.sockets) == 2
    with pytest.raises(ProtocolError):
        rig.agent._diagnostic_tcp(saved)
    assert len(rig.sockets) == 2


def test_failed_cleanup_does_not_prove_completion(rig):
    rig.close_error = True
    result = rig.client.post(rig.path, json=rig.body()).json()
    assert result["outcome"] == "error", result
    assert rig.client.post(rig.path, json=rig.body("dns")).json()["outcome"] == "busy"
    assert len(rig.sockets) == 1 and not rig.queries


def test_expired_unclaimed_ticket_cannot_start_later():
    from localplane.agent.diagnostic_registry import DiagnosticRegistry
    clock = [0.0]
    registry = DiagnosticRegistry(clock=lambda: clock[0])
    ticket = registry.issue(binding="target", deadline=2)
    clock[0] = 3
    assert registry.status(ticket) == "expired"
    assert not registry.begin(ticket, binding="target")
    assert registry.issue(binding="new") is not None


def test_backend_and_agent_digest_disagreement_prevent_connection(rig, monkeypatch):
    from localplane.protocol.diagnostic_policy import LoadedDiagnosticPolicy, parse_diagnostic_policy
    assert rig.client.post(rig.path, json=rig.body() | {"policy_digest": "0" * 64}).json()["outcome"] == "policy_mismatch"
    assert not rig.sockets and rig.collected == 0
    original = rig.execute
    def change_policy(kind, params, **kwargs):
        rig.agent._diagnostic_policy = LoadedDiagnosticPolicy("valid", parse_diagnostic_policy(b'{"version":1,"dns":[],"tcp":[]}'))
        return original(kind, params, **kwargs)
    monkeypatch.setattr(rig.app.state.context.client, "diagnostic_execute", change_policy)
    result = rig.client.post(rig.path, json=rig.body()).json()
    assert result["outcome"] == "policy_mismatch", result
    assert not rig.sockets


def test_ticket_is_bound_to_exact_target_deadline_and_policy(rig):
    prepared = rig.values()
    ticket = rig.agent._diagnostic_preflight(prepared)["ticket"]
    values = {k: v for k, v in prepared.items() if k != "kind"} | {"ticket": ticket}
    for changed in ({"port": 444}, {"absolute_monotonic_deadline": time.monotonic() + 4}, {"policy_digest": "0" * 64}):
        with pytest.raises(ProtocolError):
            rig.agent._diagnostic_tcp(values | changed)
    assert not rig.sockets
    assert rig.agent._diagnostic_tcp(values)["result"]["outcome"] == "connected"
    assert len(rig.sockets) == 1


def test_preparation_and_execution_share_one_deadline(rig, monkeypatch):
    seen = []
    prepare, execute = rig.prepare, rig.execute
    def preparation(params, **kwargs):
        seen.append(params["absolute_monotonic_deadline"])
        assert 0 < kwargs["timeout_s"] <= 5
        return prepare(params, **kwargs)
    def execution(kind, params, **kwargs):
        assert params["absolute_monotonic_deadline"] == seen[0]
        assert 0 < kwargs["timeout_s"] <= 5
        return execute(kind, params, **kwargs)
    monkeypatch.setattr(rig.app.state.context.client, "diagnostic_preflight", preparation)
    monkeypatch.setattr(rig.app.state.context.client, "diagnostic_execute", execution)
    assert rig.client.post(rig.path, json=rig.body()).json()["outcome"] == "connected"
    assert len(rig.sockets) == 1 and rig.sockets[0].timeout <= 3


def test_budget_expired_during_preparation_never_dispatches(rig, monkeypatch):
    from types import SimpleNamespace
    from localplane.backend import diagnostics
    offset = [0]
    original_clock = time.monotonic
    monkeypatch.setattr(diagnostics, "time", SimpleNamespace(monotonic=lambda: original_clock() + offset[0]))
    prepare = rig.prepare
    def preparation(params, **kwargs):
        result = prepare(params, **kwargs)
        offset[0] = 6
        return result
    monkeypatch.setattr(rig.app.state.context.client, "diagnostic_preflight", preparation)
    assert rig.client.post(rig.path, json=rig.body()).json()["outcome"] == "timeout"
    assert not rig.sockets and rig.execute_calls == 0


@pytest.mark.parametrize("mismatch", ["unknown", "host", "agent", "ticket"])
def test_uncertainty_requires_specific_verified_terminal_ticket(rig, mismatch):
    def timeout(kind, params):
        raise AgentError("timeout", "fixture", dispatched=True)
    rig.fail_execute = timeout
    assert rig.client.post(rig.path, json=rig.body()).json()["outcome"] == "unavailable"
    def bad_status(values):
        response = {"host_id": values["expected_host_id"], "agent_instance_id": values["expected_agent_instance_id"],
                    "ticket": values["ticket"], "state": "completed"}
        if mismatch == "unknown":
            response["state"] = "unknown"
        else:
            key = {"host": "host_id", "agent": "agent_instance_id", "ticket": "ticket"}[mismatch]
            response[key] = "wrong"
        return response
    rig.status_override = bad_status
    assert rig.client.post(rig.path, json=rig.body()).json()["outcome"] == "busy"
    assert rig.execute_calls == 1 and not rig.sockets


def test_resolver_owner_change_has_fixed_reason_without_raw_provider_detail():
    from localplane.agent.providers.connectivity_resolved import resolve_dns
    class Bus:
        def __init__(self): self.owners = iter([":1.2", ":1.3"])
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def owner(self): return next(self.owners)
        def resolve_hostname(self, *args): return ([], "nas.lab.test.", 1)
    result = resolve_dns("nas.lab.test.", "A", time.monotonic() + 5, connection_factory=Bus)
    assert result.outcome == "unavailable" and result.reason == "resolver_changed" and result.answers is None


@pytest.mark.parametrize("declaration", [None, {"complete": False, "addresses": []}, {"complete": True, "addresses": ["192.168.247.2"]}])
def test_missing_incomplete_and_deny_over_explicit_allow(tmp_path, monkeypatch, declaration):
    document = {"version": 1 if declaration is None else 2, "dns": ["nas.lab.test."], "tcp": [{"address": "192.168.247.2", "port": 443}]}
    if declaration is not None:
        document["control_plane"] = declaration
    value = diagnostic_rig(tmp_path, monkeypatch, document=document)
    try:
        with value.client:
            value.connect_client()
            result = value.client.post(value.path, json=value.body()).json()
            assert result["outcome"] in ("unavailable", "denied"), result
            assert not value.sockets
            assert value.client.post(value.path, json=value.body("dns")).json()["outcome"] == "resolved"
    finally:
        value.ctx.database.close()
