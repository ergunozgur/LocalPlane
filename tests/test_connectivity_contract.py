from types import SimpleNamespace
import json
import os
import stat

import pytest

from localplane.agent.service import AgentService
from localplane.backend.app import create_app, classify_request
from localplane.backend.auth import Authentication
from localplane.backend.config import Settings
from localplane.protocol import connectivity
from localplane.protocol.diagnostic_policy import LoadedDiagnosticPolicy, load_diagnostic_policy, parse_diagnostic_policy
from localplane.protocol.wire import ProtocolError
from tests.conftest import AuthenticatedTestClient, TEST_MASTER_SECRET


@pytest.fixture
def client(tmp_path, monkeypatch):
    policy = parse_diagnostic_policy(b'{"version":1,"dns":["nas.lab.test."],"tcp":[{"address":"192.168.1.20","port":443}]}')
    monkeypatch.setattr("localplane.backend.app.load_diagnostic_policy", lambda: LoadedDiagnosticPolicy("valid", policy))
    monkeypatch.setattr("localplane.backend.api.routes._require_interface", lambda context, object_id: SimpleNamespace(host_id="host_fixture", object_id=object_id))
    settings = Settings(tmp_path / "db", tmp_path / "absent.sock", 1, 60, "WARNING", False)
    app = create_app(settings, authentication=Authentication(TEST_MASTER_SECRET, bind_host="127.0.0.1"))
    with AuthenticatedTestClient(app) as client:
        monkeypatch.setattr(app.state.context.client, "connectivity_route", lambda destination: {})
        def unavailable(context, record, loaded):
            from localplane.backend.connectivity import _empty, _policy
            from localplane.backend.api.connectivity_schemas import ConnectivitySnapshot
            return ConnectivitySnapshot(object_id=record.object_id, policy=_policy(loaded), **_empty("unavailable", "fixture"))
        monkeypatch.setattr("localplane.backend.api.routes.connectivity_service.refresh", unavailable)
        yield client


def test_snapshot_is_not_observed_and_actions_do_not_dispatch(client):
    base = "/api/v1/network/interfaces/obj/connectivity"
    snapshot = client.get(base).json()
    for section in ("profile", "applied", "kernel", "routes", "rules", "resolver", "client_resolver"):
        assert snapshot[section]["evidence"]["status"] == "not_observed"
        assert snapshot[section]["value"] is None
    assert client.post(base + "/observations/refresh").json()["kernel"]["evidence"]["status"] == "unavailable"
    assert client.get(base).json()["kernel"]["evidence"]["status"] == "not_observed"
    assert client.post(base + "/route", json={"destination": "192.168.1.20"}).json()["status"] == "unavailable"
    assert client.post(base + "/route", json={"destination": "example.org"}).status_code == 422
    assert classify_request("POST", base + "/diagnostics") == "record"
    digest = snapshot["policy"]["digest"]
    dns = {"kind": "dns", "name": "nas.lab.test.", "record_type": "A", "policy_digest": digest}
    result = client.post(base + "/diagnostics", json=dns).json()
    assert result["outcome"] == "unavailable" and result["started_at"] is None and result["answers"] is None
    assert client.post(base + "/diagnostics", json=dns | {"server": "192.168.1.1"}).status_code == 422
    assert client.post(base + "/diagnostics", json=dns | {"name": "elsewhere.test."}).json()["outcome"] == "denied"
    assert client.post(base + "/diagnostics", json=dns | {"policy_digest": "0" * 64}).json()["outcome"] == "policy_mismatch"
    tcp = {"kind": "tcp", "address": "192.168.1.20", "port": 443, "policy_digest": digest}
    assert client.post(base + "/diagnostics", json=tcp).json()["outcome"] == "unavailable"
    assert client.post(base + "/diagnostics", json=tcp | {"port": True}).status_code == 422
    assert client.post(base + "/diagnostics", json=tcp | {"port": 22}).json()["outcome"] == "denied"


def test_agent_contract_rejects_extras_and_never_executes(tmp_path, monkeypatch):
    from tests.connectivity_diagnostic_fixtures import diagnostic_rig
    rig = diagnostic_rig(tmp_path, monkeypatch)
    try:
        prepared = rig.values("dns")
        ticket = rig.agent._diagnostic_preflight(prepared)["ticket"]
        values = {k: v for k, v in prepared.items() if k != "kind"} | {"ticket": ticket}
        with pytest.raises(ProtocolError):
            rig.agent._diagnostic_dns(values | {"expected_agent_instance_id": "other"})
        with pytest.raises(ProtocolError):
            rig.agent._diagnostic_dns(values | {"server": "1.1.1.1"})
        assert not rig.queries and not rig.sockets
        assert rig.agent._diagnostic_dns(values)["result"]["outcome"] == "resolved"
        with pytest.raises(ProtocolError):
            rig.agent._diagnostic_dns(values)
        assert len(rig.queries) == 1
        with pytest.raises(ValueError):
            connectivity.tcp_request({"address": "192.168.1.1", "port": True, "policy_digest": "a" * 64})
    finally:
        rig.ctx.database.close()


def test_backend_diagnostic_client_has_only_closed_methods():
    from localplane.backend.agent_client import AgentClient
    client = AgentClient.__new__(AgentClient)
    calls = []
    client.call = lambda method, params, **options: calls.append((method, params, options)) or {}
    params = {"ticket": "a" * 32}
    client.diagnostic_execute("dns", params, timeout_s=4.0)
    client.diagnostic_execute("tcp", params, timeout_s=10.0)
    assert calls[0][0] == "network.diagnostic_dns" and calls[1][0] == "network.diagnostic_tcp"
    assert calls[0][1] == calls[1][1] == params
    assert calls[0][2]["timeout_s"] == 4.0 and calls[1][2]["timeout_s"] == 5.0
    with pytest.raises(ValueError):
        client.diagnostic_execute("shell", params, timeout_s=1.0)


def test_agent_tcp_configured_target_fails_closed_without_current_exclusions(tmp_path, monkeypatch):
    from tests.connectivity_diagnostic_fixtures import diagnostic_rig
    from localplane.agent.providers import diagnostic_exclusions
    rig = diagnostic_rig(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(diagnostic_exclusions, "collect", lambda **kw: (_ for _ in ()).throw(ValueError("unknown")))
        assert rig.agent._diagnostic_preflight(rig.values()) == {"status": "unavailable"}
        assert not rig.sockets
    finally:
        rig.ctx.database.close()


def test_route_result_uses_kernel_egress_without_assuming_selected_interface(client, monkeypatch):
    facts = {"family": "inet", "destination": "192.168.1.20", "destination_prefix_length": 32,
             "preferred_source": "192.168.1.2", "gateway": None, "oif_index": 7,
             "table": 254, "route_type": "unicast", "scope": "link", "protocol": "kernel", "priority": None}
    observation = {"provider": "linux.route", "provider_version": "1", "capability": "network.route.observe",
                   "source": "rtnetlink", "method": "netlink_rtm_getroute", "destination": "192.168.1.20",
                   "family": "inet", "status": "resolved", "reason": None, "route": facts, "error": None,
                   "observed_at": "2026-11-09T12:00:00+00:00"}
    monkeypatch.setattr(client.app.state.context.client, "connectivity_route", lambda _: {"host_id": "host_fixture", "agent_instance_id": "agent1", "route": observation})
    result = client.post("/api/v1/network/interfaces/selected/connectivity/route", json={"destination": "192.168.1.20"}).json()
    assert result["status"] == "selected" and result["egress_ifindex"] == 7
    assert result["egress_object_id"] is None and result["metric"] is None
    assert result["source"] == "192.168.1.2"
    assert classify_request("POST", "/api/v1/network/interfaces/selected/connectivity/route") == "read"
    monkeypatch.setattr(client.app.state.context.client, "connectivity_route", lambda _: {"host_id": "different_host", "agent_instance_id": "agent1", "route": observation})
    assert client.post("/api/v1/network/interfaces/selected/connectivity/route", json={"destination": "192.168.1.20"}).json()["status"] == "error"


def test_agent_route_dispatch_uses_validated_numeric_request_only():
    service = AgentService.__new__(AgentService)
    service._require_capability = lambda capability: None
    service._instance_id = "agent1"
    service._identity = lambda: SimpleNamespace(host_id="host1")
    service._connectivity_routes = SimpleNamespace(observe_route=lambda destination: SimpleNamespace(as_dict=lambda: {"destination": destination}))
    assert service._connectivity_route({"destination": "2001:db8::1"})["route"]["destination"] == "2001:db8::1"
    with pytest.raises(ProtocolError):
        service._connectivity_route({"destination": "example.test"})


def test_protected_loader_fixture_and_failure_states(tmp_path, monkeypatch):
    # Make a disposable tree appear root-owned to the loader, without privilege or
    # changing real ownership. The actual openat/no-follow and mode checks still run.
    import localplane.protocol.diagnostic_policy as module
    fstat = os.fstat
    def fake_fstat(fd):
        info = fstat(fd)
        return SimpleNamespace(st_uid=0, st_gid=123, st_mode=info.st_mode, st_nlink=info.st_nlink)
    monkeypatch.setattr(module.os, "fstat", fake_fstat)
    monkeypatch.setattr(module.grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=123))
    # /tmp is intentionally unsafe on this machine; replace only the synthetic metadata.
    def safe_fstat(fd):
        info = fake_fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            info.st_mode &= ~0o022
        return info
    monkeypatch.setattr(module.os, "fstat", safe_fstat)
    path = tmp_path / "policy.json"
    assert load_diagnostic_policy(str(path)).status == "missing"
    path.write_text(json.dumps({"version": 1, "dns": ["nas.lab.test."], "tcp": []}))
    path.chmod(0o640)
    loaded = load_diagnostic_policy(str(path))
    assert loaded.status == "valid" and loaded.policy.dns == ("nas.lab.test.",)
    monkeypatch.setattr(module, "_has_access_acl", lambda fd: True)
    assert load_diagnostic_policy(str(path)).status == "unsafe"
    monkeypatch.setattr(module, "_has_access_acl", lambda fd: False)
    path.write_text("invalid")
    assert load_diagnostic_policy(str(path)).status == "invalid"
    path.chmod(0o666)
    assert load_diagnostic_policy(str(path)).status == "unsafe"
    link = tmp_path / "link.json"
    link.symlink_to(path)
    assert load_diagnostic_policy(str(link)).status == "unsafe"
    parent_link = tmp_path / "linked-dir"
    parent_link.symlink_to(tmp_path, target_is_directory=True)
    assert load_diagnostic_policy(str(parent_link / "policy.json")).status == "unsafe"
    assert load_diagnostic_policy("relative.json").status == "unsafe"
    monkeypatch.setenv("LOCALPLANE_DIAGNOSTIC_POLICY_PATH", str(path))
    assert load_diagnostic_policy().status == "unsafe"


def test_agent_loads_once_at_construction(monkeypatch):
    policy = parse_diagnostic_policy(b'{"version":1,"dns":[],"tcp":[]}')
    calls = []
    monkeypatch.setattr("localplane.agent.service.load_diagnostic_policy", lambda: calls.append(1) or LoadedDiagnosticPolicy("valid", policy))
    monkeypatch.setattr(AgentService, "_discover", lambda self: ())
    service = AgentService()
    assert calls == [1]
    assert service._diagnostic_policy.policy.digest == policy.digest
    service._identity = lambda: SimpleNamespace(host_id="fixture-host")
    with pytest.raises(ProtocolError):
        service._diagnostic_tcp({"address": "192.168.1.2", "port": 443, "policy_digest": policy.digest, "expected_host_id": "fixture-host", "expected_agent_instance_id": service._instance_id})
    assert calls == [1]


def test_backend_policy_is_loaded_once(client, monkeypatch):
    base = "/api/v1/network/interfaces/obj/connectivity"
    before = client.get(base).json()["policy"]
    monkeypatch.setattr("localplane.backend.app.load_diagnostic_policy", lambda: (_ for _ in ()).throw(AssertionError("reloaded")))
    assert client.get(base).json()["policy"] == before
