"""Synthetic transports only; no bus, interface inventory or network access."""
import json
import socket
import struct
from types import SimpleNamespace

import pytest
from jeepney.low_level import HeaderFields
from jeepney.wrappers import new_method_return

from localplane.agent.providers import connectivity_nm as nm, connectivity_resolved as resolved
from localplane.agent.providers.connectivity_collector import collect, kernel_addresses, client_resolver
from localplane.agent.service import AgentService
from localplane.backend.api.connectivity_schemas import ConnectivitySnapshot, ConnectivityPolicy


def variant(value):
    return ("v", value)


def settings():
    return {"connection": {"uuid": variant("uuid-1"), "id": variant("office")},
            "ipv4": {"address-data": variant([{"address": variant("192.168.1.2"), "prefix": variant(24)}]),
                     "dns-data": variant(["192.168.1.1"]),
                     "method": variant("auto"), "route-metric": variant(-1), "ignore-auto-dns": variant(False),
                     "dns-search": variant(["example.test", "~private.test"])},
            "wifi-security": {"psk": variant("must-not-escape")}}


class FakeNM:
    def __init__(self, *, changed=False, absent=False):
        self.changed = changed
        self.absent = absent
        self.generations = 0
        self.calls = []
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def owner(self): return ":1.50"
    def devices(self, owner): return [] if self.absent else [nm.ROOT + "/Devices/1"]
    def device_for_ip_interface(self, owner, name):
        if self.absent: raise LookupError()
        return nm.ROOT + "/Devices/1"
    def prop(self, owner, path, interface, name):
        self.calls.append((interface, name))
        return {(nm.MANAGER, "Version"): "1.46.0", (nm.DEVICE, "Interface"): "eth0",
                (nm.DEVICE, "IpInterface"): "eth0",
                (nm.DEVICE, "State"): 100,
                (nm.DEVICE, "ActiveConnection"): nm.ROOT + "/ActiveConnection/1",
                (nm.ACTIVE, "Uuid"): "uuid-1", (nm.ACTIVE, "Connection"): nm.ROOT + "/Settings/1",
                (nm.ACTIVE, "Devices"): [nm.ROOT + "/Devices/1"], (nm.ACTIVE, "State"): 2,
                (nm.SETTINGS, "Unsaved"): True, (nm.SETTINGS, "Flags"): 11}[(interface, name)]
    def settings(self, owner, path): return settings()
    def applied(self, owner, path):
        self.generations += 1
        return settings(), (self.generations if self.changed else 7)


def test_nm_normalizes_only_allowlisted_data_and_rechecks_generation():
    fake = FakeNM()
    result = nm.observe("eth0", lambda: fake)
    assert result["status"] == "observed" and result["applied"]["version"] == "7"
    assert result["profile"]["unsaved"] is True and result["profile"]["external"] is True and result["profile"]["generated"] is True
    assert result["profile"]["routing_domains"][1] == {"name": "private.test", "route_only": True}
    assert result["profile"]["ip_settings"][0]["method"] == "auto"
    assert result["profile"]["ip_settings"][0]["route_metric"] == -1
    assert "must-not-escape" not in json.dumps(result)
    assert fake.generations == 2 and all(name != "GetSecrets" for _, name in fake.calls)
    assert nm.observe("eth0", lambda: FakeNM(changed=True))["status"] == "changed"
    assert nm.observe("eth0", lambda: FakeNM(absent=True))["profile"] is None


def test_nm_legacy_dns_is_unknown_not_empty():
    value = settings()
    value["ipv4"].pop("dns-data")
    value["ipv4"]["dns"] = variant([123])
    _, fields = nm._settings_view(value)
    assert fields["dns_servers"] is None and fields["ip_settings"][0]["dns_servers"] is None


def test_nm_applied_denial_preserves_profile():
    class AppliedDenied(FakeNM):
        def applied(self, owner, path): raise PermissionError()
    result = nm.observe("eth0", lambda: AppliedDenied())
    assert result["profile"]["uuid"] == "uuid-1"
    assert result["applied"] is None and result["applied_status"] == "unavailable"


@pytest.mark.parametrize("device_count", [13, 256])
def test_nm_targeted_lookup_does_not_enumerate_and_applied_is_disabled(device_count):
    class ManyDevices(FakeNM):
        def devices(self, owner):
            raise AssertionError(f"must not enumerate {device_count} devices")
        def prop(self, owner, path, interface, name):
            if (interface, name) == (nm.MANAGER, "Version"):
                return "1.47.0"
            return super().prop(owner, path, interface, name)
    fake = ManyDevices()
    result = nm.observe("eth0", lambda: fake)
    assert result["profile"]["uuid"] == "uuid-1"
    assert result["applied"] is None and fake.generations == 0
    assert len(fake.calls) < nm.MAX_CALLS


class FakeResolved:
    def __init__(self, changed=False): self.changed = changed; self.count = 0
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def owner(self):
        self.count += 1
        return ":1.2" if not self.changed or self.count == 1 else ":1.3"
    def link(self, owner, index): return resolved.ROOT + "/link/_2"
    def prop(self, owner, path, interface, name):
        return {(resolved.MANAGER, "Version"): "255", (resolved.MANAGER, "DNS"): [(0, socket.AF_INET, b"\x08\x08\x08\x08"), (2, socket.AF_INET, b"\xc0\xa8\x01\x01")],
                (resolved.MANAGER, "Domains"): [(0, "global.test", False), (2, "link.test", True)],
                (resolved.MANAGER, "CurrentDNSServer"): (2, socket.AF_INET, b"\xc0\xa8\x01\x01"),
                (resolved.LINK, "DNS"): [(socket.AF_INET, b"\xc0\xa8\x01\x01")],
                (resolved.LINK, "Domains"): [("link.test", True)], (resolved.LINK, "DefaultRoute"): True,
                (resolved.LINK, "CurrentDNSServer"): (socket.AF_INET, b"\xc0\xa8\x01\x01")}[(interface, name)]


def test_resolved_global_link_and_route_only_are_distinct():
    result = resolved.observe(2, "eth0", lambda: FakeResolved())
    assert result["status"] == "observed"
    assert result["value"]["global_servers"] == ["8.8.8.8"]
    assert result["value"]["current_server_ifindex"] == 2 and result["value"]["current_server"] == "192.168.1.1"
    link = result["value"]["links"][0]
    assert link["servers"] == ["192.168.1.1"] and link["routing_domains"][0]["route_only"]
    assert result["value"]["global_routing_domains"] == [{"name": "global.test", "route_only": False}]
    assert resolved.observe(2, "eth0", lambda: FakeResolved(changed=True))["status"] == "changed"


def test_resolved_optional_property_failures_do_not_erase_other_sources():
    class Older(FakeResolved):
        def prop(self, owner, path, interface, name):
            if name in {"Version", "DefaultRoute"}:
                raise LookupError("unsupported")
            return super().prop(owner, path, interface, name)
    result = resolved.observe(2, "eth0", lambda: Older())
    assert result["status"] == "observed" and result["version"] is None
    assert result["value"]["links"][0]["dns_default_route"] is None
    assert result["value"]["links"][0]["servers"] == ["192.168.1.1"]
    assert len(result["gaps"]) == 4


def test_client_stub_and_kernel_addresses_are_fixture_only(tmp_path):
    path = tmp_path / "resolv.conf"
    path.write_text("nameserver 127.0.0.53\nsearch private.test\n")
    assert client_resolver(path) == {"client_nameservers": ["127.0.0.53"], "client_mode": "stub"}
    data = b'[{"ifindex":2,"ifname":"eth0","addr_info":[{"local":"192.168.1.2","prefixlen":24}]}]'
    assert kernel_addresses({"ifindex": 2, "name": "eth0"}, lambda argv: data)["addresses"][0]["prefix"] == 24


def test_collector_sections_and_kernel_identity_recheck():
    identity = {"ifindex": 2, "name": "eth0", "boot_id": "fixture-boot", "network_namespace": "net:[1]", "kernel_device": "1:2"}
    kwargs = {"identity_reader": lambda index: identity.copy(), "address_reader": lambda value: {"addresses": []},
              "inventory_reader": lambda: {k: {"value": [], "completeness": "complete", "gaps": []} for k in ("routes", "rules")},
              "nm_reader": lambda name: nm.observe(name, lambda: FakeNM()),
              "resolver_reader": lambda index, name: resolved.observe(index, name, lambda: FakeResolved()),
              "client_reader": lambda: {"client_nameservers": ["127.0.0.53"], "client_mode": "stub"}}
    result = collect(2, **kwargs)
    assert result["status"] == "observed"
    assert result["sections"]["kernel"]["value"]["addresses"] == []
    snapshot = ConnectivitySnapshot(object_id="obj", policy=ConnectivityPolicy(status="missing", digest=None, dns=[], tcp=[]), **result["sections"])
    assert snapshot.profile.value.uuid == "uuid-1" and snapshot.resolver.value.links[0].dns_default_route is True
    identities = iter([identity, identity | {"kernel_device": "1:3"}])
    kwargs["identity_reader"] = lambda index: next(identities)
    changed = collect(2, **kwargs)
    assert changed["status"] == "changed" and changed["sections"] is None


def test_missing_nm_and_resolver_preserve_good_kernel_sections():
    identity = {"ifindex": 2, "name": "eth0", "boot_id": "b", "network_namespace": "net:[1]", "kernel_device": "1:2"}
    def unavailable(*args): raise OSError("fixture unavailable")
    result = collect(2, identity_reader=lambda _: identity, address_reader=lambda _: {"addresses": []},
                     inventory_reader=lambda: {k: {"value": [], "completeness": "complete", "gaps": []} for k in ("routes", "rules")},
                     nm_reader=unavailable, resolver_reader=unavailable,
                     client_reader=lambda: {"client_nameservers": [], "client_mode": "unknown"})
    assert result["sections"]["profile"]["evidence"]["status"] == "unavailable"
    assert result["sections"]["resolver"]["evidence"]["status"] == "unavailable"
    assert result["sections"]["routes"]["value"] == []


def test_agent_observation_holds_host_identity_across_collection(monkeypatch):
    service = AgentService.__new__(AgentService)
    service._identity = lambda: SimpleNamespace(host_id="host", boot_id="boot")
    service._instance_id = "agent"
    monkeypatch.setattr("localplane.agent.service.collect_connectivity", lambda index: {"status": "observed", "sections": {"kernel": {"evidence": {}}}})
    result = service._connectivity_observe({"ifindex": 2})
    assert result["observation"]["sections"]["kernel"]["evidence"]["host_id"] == "host"


def test_agent_refuses_second_collection_while_slot_is_held(monkeypatch):
    from localplane.agent.service import _CONNECTIVITY_COLLECTION_SLOT
    service = AgentService.__new__(AgentService)
    monkeypatch.setattr("localplane.agent.service.collect_connectivity", lambda index: (_ for _ in ()).throw(AssertionError("must not start")))
    assert _CONNECTIVITY_COLLECTION_SLOT.acquire(blocking=False)
    try:
        assert service._connectivity_observe({"ifindex": 2})["status"] == "unavailable"
    finally:
        _CONNECTIVITY_COLLECTION_SLOT.release()


def test_named_bus_transport_never_accepts_arbitrary_nm_property(monkeypatch):
    calls = []
    class FakeConnection:
        def send_and_get_reply(self, message, timeout):
            member = message.header.fields[HeaderFields.member]
            calls.append(member)
            if member == "GetDeviceByIpIface":
                return new_method_return(message, "o", (nm.ROOT + "/Devices/1",))
            return new_method_return(message, "ao", ([nm.ROOT + "/Devices/1"],))
        def close(self): pass
    monkeypatch.setattr(nm, "_open_bounded_system_bus", lambda timeout: FakeConnection())
    with nm._NMConnection() as bus:
        assert bus.devices(":1.2") == [nm.ROOT + "/Devices/1"]
        assert bus.device_for_ip_interface(":1.2", "eth0") == nm.ROOT + "/Devices/1"
        with pytest.raises(ValueError):
            bus.prop(":1.2", nm.ROOT, nm.MANAGER, "GetSecrets")
    assert calls == ["GetDevices", "GetDeviceByIpIface"]


def test_named_bus_transport_only_requests_resolved_getlink(monkeypatch):
    calls = []
    class FakeConnection:
        def send_and_get_reply(self, message, timeout):
            calls.append(message.header.fields[HeaderFields.member])
            return new_method_return(message, "o", (resolved.ROOT + "/link/_2",))
        def close(self): pass
    monkeypatch.setattr(resolved, "_open_bounded_system_bus", lambda timeout: FakeConnection())
    with resolved._ResolvedConnection() as bus:
        assert bus.link(":1.2", 2) == resolved.ROOT + "/link/_2"
        with pytest.raises(ValueError):
            bus.prop(":1.2", resolved.ROOT, resolved.MANAGER, "FlushCaches")
    assert calls == ["GetLink"]


@pytest.mark.parametrize("module", [nm, resolved])
@pytest.mark.parametrize("marker,endian", [(b"l", "<"), (b"B", ">")])
def test_bus_parser_rejects_declared_huge_frames_before_decode(module, marker, endian):
    parser = module._BoundedParser()
    header = struct.pack(endian + "cBBBIII", marker, 2, 0, 1, module.MAX_BUS_FRAME + 1, 1, 0)
    parser.add_data(header[:8])
    assert parser.get_next_message() is None
    parser.add_data(header[8:])
    with pytest.raises(ValueError, match="frame exceeds"):
        parser.get_next_message()


@pytest.mark.parametrize("module", [nm, resolved])
def test_bus_parser_rejects_invalid_marker_and_unexpected_fds(module):
    parser = module._BoundedParser()
    parser.add_data(b"x" + b"\0" * 15)
    with pytest.raises(ValueError, match="header"):
        parser.get_next_message()
    with pytest.raises(ValueError, match="input"):
        module._BoundedParser().add_data(b"", fds=(1,))


@pytest.mark.parametrize("module", [nm, resolved])
@pytest.mark.parametrize("marker,endian", [(b"l", "<"), (b"B", ">")])
def test_bus_parser_rejects_huge_header_fields_before_any_decode(module, marker, endian, monkeypatch):
    from jeepney.low_level import Message
    decoded = []
    monkeypatch.setattr(Message, "from_buffer", lambda *args, **kwargs: decoded.append(True))
    parser = module._BoundedParser()
    parser.add_data(struct.pack(endian + "cBBBIII", marker, 2, 0, 1, 0, 1, module.MAX_BUS_FRAME + 1))
    with pytest.raises(ValueError):
        parser.get_next_message()
    assert decoded == []


@pytest.mark.parametrize("module", [nm, resolved])
def test_hello_installs_bounded_parser_before_proxy_call(module, monkeypatch):
    monkeypatch.setattr(module.DBusConnectionBase, "__init__", lambda self, sock, enable_fds: None)
    class FakeProxy:
        def __init__(self, address, connection, timeout):
            assert isinstance(connection.parser, module._BoundedParser)
        def Hello(self): return (":1.10",)
    monkeypatch.setattr(module, "Proxy", FakeProxy)
    connection = module._BoundedConnection(object(), 1.0)
    assert connection.unique_name == ":1.10"


def test_resolved_dns_callback_is_dns_only_bounded_and_no_real_bus(monkeypatch):
    import time
    class Bus:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def owner(self): return ":1.4"
        def resolve_hostname(self, owner, name, family):
            assert name == "nas.lab.test." and family == socket.AF_INET
            return ([(2, socket.AF_INET, b"\xc0\xa8\x01\x01")] * 33, name, 1)
    answer = resolved.resolve_dns("nas.lab.test.", "A", time.monotonic() + 5, connection_factory=Bus)
    assert answer.outcome == "resolved" and len(answer.answers) == 32 and answer.truncated
    class NonDns(Bus):
        def resolve_hostname(self, owner, name, family): return ([(2, socket.AF_INET, b"\xc0\xa8\x01\x01")], name, 2)
    assert resolved.resolve_dns("nas.lab.test.", "A", time.monotonic() + 5, connection_factory=NonDns).outcome == "unavailable"


def test_installed_jeepney_authenticator_rejects_overlong_fragmented_data():
    from jeepney.auth import Authenticator, AuthenticationError
    auth = Authenticator(enable_fds=False)
    for _ in range(8):
        auth.feed(b"x" * 1024)
    assert len(auth.buffer) == 8192
    with pytest.raises(AuthenticationError):
        auth.feed(b"x" * 1024)
    assert len(auth.buffer) == 9216
    auth = Authenticator(enable_fds=False)
    with pytest.raises(AuthenticationError):
        auth.feed(b"OK " + b"x" * 8189 + b"\r\ntrailing")


@pytest.mark.parametrize("error_name,outcome", [
    ("org.freedesktop.DBus.Error.Timeout", "timeout"),
    ("org.freedesktop.resolve1.NoSuchRR", "no_data"),
    ("org.freedesktop.resolve1.DnsError.NXDOMAIN", "not_found"),
    ("org.freedesktop.resolve1.DnsError.SERVFAIL", "server_failure"),
    ("org.freedesktop.resolve1.DnsError.REFUSED", "refused"),
    ("org.freedesktop.resolve1.Other", "unavailable"),
])
def test_resolver_maps_only_named_errors(error_name, outcome):
    import time
    from jeepney.wrappers import DBusErrorResponse
    class Bus:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def owner(self): return ":1.4"
        def resolve_hostname(self, owner, name, family):
            message = SimpleNamespace(header=SimpleNamespace(fields={HeaderFields.error_name: error_name}), body=("fixture",))
            raise DBusErrorResponse(message)
    assert resolved.resolve_dns("nas.lab.test.", "A", time.monotonic() + 5, connection_factory=Bus).outcome == outcome
