"""Guarded passive lookup identity; synthetic kernel data only."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from localplane.agent import service
from localplane.backend.api import connectivity_schemas as schema
from localplane.backend.connectivity import route_egress
from localplane.backend.domain.identity import identify_interface
from tests.test_connectivity_store_isolation import fixture_context


def identity(boot, *, name="eth0", mac=None):
    return {"ifindex": 2, "name": name, "mac_address": mac, "mac_is_permanent": mac is not None,
            "device_path": None, "kernel_device": "1:2", "sysfs_device": "/fixture/" + name,
            "boot_id": boot, "network_namespace": "net:[42]"}


def route(index=2):
    return {"provider": "linux.route", "provider_version": "1", "capability": "network.route.observe",
            "source": "rtnetlink", "method": "netlink_rtm_getroute", "destination": "192.168.247.2",
            "family": "inet", "status": "resolved", "reason": None, "error": None,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "route": {"family": "inet", "destination": "192.168.247.2", "destination_prefix_length": 32,
                      "preferred_source": "192.168.247.1", "gateway": None, "oif_index": index,
                      "table": 254, "route_type": "unicast", "scope": "link", "protocol": "kernel", "priority": None}}


def test_agent_lookup_is_inside_identity_guard_and_keeps_route_if_identity_changes(monkeypatch):
    boot = "11111111-1111-4111-8111-111111111111"
    agent = service.AgentService.__new__(service.AgentService)
    agent._identity = lambda: SimpleNamespace(host_id="host", boot_id=boot)
    agent._instance_id = "agent"
    agent._sysfs_net = Path("/fixture")
    agent._require_capability = lambda _: None
    events = []
    agent._connectivity_routes = SimpleNamespace(observe_route=lambda _: events.append("route") or SimpleNamespace(as_dict=route))
    monkeypatch.setattr(service, "kernel_identity", lambda *_: events.append("identity") or identity(boot))
    answer = agent._connectivity_route({"destination": "192.168.247.2"})
    assert events == ["route", "identity", "route", "identity"]
    assert answer["egress_identity"]["name"] == "eth0"
    changed = iter([identity(boot), identity(boot, name="recreated")])
    monkeypatch.setattr(service, "kernel_identity", lambda *_: next(changed))
    answer = agent._connectivity_route({"destination": "192.168.247.2"})
    assert answer["egress_identity"] is None and answer["route"]["status"] == "resolved"


def test_name_only_is_label_not_object_relationship_and_wrong_namespace_is_unknown(tmp_path, monkeypatch):
    from localplane.agent.providers import diagnostic_exclusions
    ctx, object_id, boot = fixture_context(tmp_path)
    try:
        monkeypatch.setattr(diagnostic_exclusions, "kernel_context", lambda: (boot, "net:[42]"))
        answer = schema.AgentRouteAnswer(agent_instance_id="agent", host_id="host", route=route(), egress_identity=identity(boot))
        assert route_egress(ctx, answer) == ("eth0", None)
        assert object_id is not None
        monkeypatch.setattr(diagnostic_exclusions, "kernel_context", lambda: (boot, "net:[99]"))
        assert route_egress(ctx, answer) == (None, None)
    finally:
        ctx.database.close()


def test_durable_identity_link_requires_current_exact_ifindex_host_agent_inventory(tmp_path, monkeypatch):
    from localplane.agent.providers import diagnostic_exclusions
    ctx, _, boot = fixture_context(tmp_path)
    try:
        mac = "02:11:22:33:44:55"
        derived = identify_interface("host", "eth0", mac, True, None)
        now = datetime.now(timezone.utc).isoformat()
        ctx.ingestor.objects.upsert(derived.object_id, "host", "network.interface", derived.basis, derived.value,
                                   derived.confidence, "eth0", "observed", "fixture", now)
        ctx.ingestor.sweeps.insert_observation({"observation_id": "hardware", "sweep_id": "inventory", "host_id": "host",
            "object_id": derived.object_id, "capability": "network.observe", "provider": "fixture", "provider_version": "1",
            "method": "fixture", "fidelity": "complete", "observed_at": now, "received_at": now,
            "health_state": "unknown", "health_reason": "fixture", "facts": {"ifindex": 2, "name": "eth0"}})
        monkeypatch.setattr(diagnostic_exclusions, "kernel_context", lambda: (boot, "net:[42]"))
        answer = schema.AgentRouteAnswer(agent_instance_id="agent", host_id="host", route=route(), egress_identity=identity(boot, mac=mac))
        assert route_egress(ctx, answer) == ("eth0", derived.object_id)
        assert route_egress(ctx, answer.model_copy(update={"agent_instance_id": "other"})) == (None, None)
        changed = answer.model_copy(update={"route": schema.AgentRouteObservation.model_validate(route(3))})
        assert route_egress(ctx, changed) == (None, None)
    finally:
        ctx.database.close()
