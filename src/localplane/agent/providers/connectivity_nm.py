"""Narrow read-only NetworkManager connectivity adapter; no secrets or mutations."""
from __future__ import annotations

import time
import ipaddress
import struct
from typing import Any, Callable

from jeepney import DBusAddress, new_method_call
from jeepney.bus_messages import message_bus
from jeepney.bus import get_bus
from jeepney.io.blocking import Proxy, DBusConnection, DBusConnectionBase, prep_socket
from jeepney.io.common import MessageFilters
from jeepney.low_level import Parser
from jeepney.wrappers import Properties, unwrap_msg

SERVICE = "org.freedesktop.NetworkManager"
ROOT = "/org/freedesktop/NetworkManager"
MANAGER = SERVICE
DEVICE = SERVICE + ".Device"
ACTIVE = SERVICE + ".Connection.Active"
SETTINGS = SERVICE + ".Settings.Connection"
MAX_DEVICES = 256
MAX_CALLS = 32
TOTAL_SECONDS = 8.0
MAX_BUS_FRAME = 1024 * 1024


class _BoundedParser(Parser):
    """Check the fixed D-Bus header before Jeepney decodes a message body."""
    def get_next_message(self):
        if self.buf.bytes_buffered >= 16:
            header = self.buf.peek(16)
            if header[0:1] not in (b"l", b"B") or header[3] != 1 or header[1] not in (1, 2, 3, 4):
                raise ValueError("invalid D-Bus header")
            endian = "<" if header[0:1] == b"l" else ">"
            body = struct.unpack_from(endian + "I", header, 4)[0]
            fields = struct.unpack_from(endian + "I", header, 12)[0]
            size = (16 + fields + 7) // 8 * 8 + body
            if size > MAX_BUS_FRAME:
                raise ValueError("D-Bus frame exceeds limit")
        return super().get_next_message()

    def add_data(self, data, fds=()):
        if fds or self.buf.bytes_buffered + len(data) > MAX_BUS_FRAME + 4096:
            raise ValueError("D-Bus input exceeds limit")
        super().add_data(data, fds=fds)


class _BoundedConnection(DBusConnection):
    def __init__(self, sock, timeout):
        DBusConnectionBase.__init__(self, sock, enable_fds=False)
        self.parser = _BoundedParser()
        self._filters = MessageFilters()
        self.bus_proxy = Proxy(message_bus, self, timeout=timeout)
        try:
            self.unique_name = self.bus_proxy.Hello()[0]
        except Exception:
            self.close()
            raise


def _open_bounded_system_bus(timeout):
    deadline = time.monotonic() + timeout
    sock = prep_socket(get_bus("SYSTEM"), enable_fds=False, timeout=timeout)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        sock.close()
        raise TimeoutError("system bus connection deadline expired")
    return _BoundedConnection(sock, remaining)


def _variant(value: Any) -> Any:
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
        return value[1]
    raise ValueError("invalid provider variant")


class _NMConnection:
    """Private transport exposes only the six NetworkManager questions used here."""

    def __init__(self) -> None:
        self.connection = None
        self.deadline = time.monotonic() + TOTAL_SECONDS
        self.calls = 0

    def __enter__(self):
        self.connection = _open_bounded_system_bus(min(2.0, self.deadline - time.monotonic()))
        return self

    def __exit__(self, *args):
        if self.connection:
            self.connection.close()

    def _budget(self) -> float:
        self.calls += 1
        remaining = self.deadline - time.monotonic()
        if self.calls > MAX_CALLS or remaining <= 0:
            raise TimeoutError("NetworkManager observation limit")
        return min(2.0, remaining)

    def owner(self) -> str:
        value = Proxy(message_bus, self.connection, timeout=self._budget()).GetNameOwner(SERVICE)[0]
        if not isinstance(value, str) or not value.startswith(":"):
            raise ValueError("invalid daemon owner")
        return value

    def devices(self, owner: str) -> list:
        address = DBusAddress(ROOT, bus_name=owner, interface=MANAGER)
        message = new_method_call(address, "GetDevices")
        result = unwrap_msg(self.connection.send_and_get_reply(message, timeout=self._budget()))[0]
        if not isinstance(result, (list, tuple)) or len(result) > MAX_DEVICES:
            raise ValueError("invalid device list")
        return list(result)

    def device_for_ip_interface(self, owner: str, name: str) -> str:
        if not isinstance(name, str) or not name or len(name.encode("utf-8")) > 15 or "/" in name:
            raise ValueError("invalid kernel interface")
        address = DBusAddress(ROOT, bus_name=owner, interface=MANAGER)
        reply = self.connection.send_and_get_reply(new_method_call(address, "GetDeviceByIpIface", "s", (name,)), timeout=self._budget())
        path = unwrap_msg(reply)[0]
        if not isinstance(path, str) or not path.startswith(ROOT + "/Devices/"):
            raise ValueError("invalid device handle")
        return path

    def prop(self, owner: str, path: str, interface: str, name: str) -> Any:
        allowed = {(MANAGER, "Version"), (DEVICE, "Interface"), (DEVICE, "IpInterface"), (DEVICE, "ActiveConnection"), (DEVICE, "State"),
                   (ACTIVE, "Uuid"), (ACTIVE, "Connection"), (ACTIVE, "Devices"), (ACTIVE, "State"),
                   (SETTINGS, "Unsaved"), (SETTINGS, "Flags")}
        if (interface, name) not in allowed or not isinstance(path, str) or not path.startswith(ROOT + "/") and path != ROOT:
            raise ValueError("invalid NetworkManager property")
        address = DBusAddress(path, bus_name=owner, interface=interface)
        return _variant(Proxy(Properties(address), self.connection, timeout=self._budget()).get(name)[0])

    def settings(self, owner: str, path: str) -> dict:
        if not isinstance(path, str) or not path.startswith(ROOT + "/Settings/"):
            raise ValueError("invalid settings handle")
        address = DBusAddress(path, bus_name=owner, interface=SETTINGS)
        reply = self.connection.send_and_get_reply(new_method_call(address, "GetSettings"), timeout=self._budget())
        return unwrap_msg(reply)[0]

    def applied(self, owner: str, device: str) -> tuple:
        if not isinstance(device, str) or not device.startswith(ROOT + "/Devices/"):
            raise ValueError("invalid device handle")
        address = DBusAddress(device, bus_name=owner, interface=DEVICE)
        reply = self.connection.send_and_get_reply(new_method_call(address, "GetAppliedConnection", "u", (0,)), timeout=self._budget())
        body = unwrap_msg(reply)
        if len(body) != 2:
            raise ValueError("invalid applied connection")
        return body[0], body[1]


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and len(value) <= 255 and "\x00" not in value else None


def _settings_view(settings: Any) -> tuple[dict, dict]:
    """Copy only published connection/IP fields; never emit other settings."""
    if not isinstance(settings, dict) or len(settings) > 64:
        raise ValueError("malformed settings")
    connection = settings.get("connection", {})
    if not isinstance(connection, dict) or len(connection) > 128:
        raise ValueError("malformed connection")
    def get(mapping: dict, key: str):
        return _variant(mapping[key]) if key in mapping else None
    uuid, label = _text(get(connection, "uuid")), _text(get(connection, "id"))
    addresses, dns, domains, routes, ip_settings = [], [], [], [], []
    addresses_known = dns_known = domains_known = routes_known = False
    legacy_dns_unknown = False
    for key in ("ipv4", "ipv6"):
        group = settings.get(key)
        if group is None:
            continue
        if not isinstance(group, dict) or len(group) > 128:
            raise ValueError("malformed IP settings")
        method = _text(get(group, "method"))
        ignore_dns = get(group, "ignore-auto-dns")
        priority, route_metric = get(group, "dns-priority"), get(group, "route-metric")
        if (ignore_dns is not None and type(ignore_dns) is not bool) or (priority is not None and type(priority) is not int) or (route_metric is not None and type(route_metric) is not int):
            raise ValueError("malformed IP settings values")
        ip_setting = {"family": key, "method": method, "ignore_auto_dns": ignore_dns,
                      "dns_priority": priority, "route_metric": route_metric, "gateway": _text(get(group, "gateway")), "dns_servers": None}
        ip_settings.append(ip_setting)
        raw_addresses = get(group, "address-data")
        if raw_addresses is not None:
            if not isinstance(raw_addresses, list) or len(raw_addresses) > 128:
                raise ValueError("malformed addresses")
            addresses_known = True
            for entry in raw_addresses:
                if not isinstance(entry, dict):
                    raise ValueError("malformed address")
                address = _text(get(entry, "address"))
                prefix = get(entry, "prefix")
                if address is None or type(prefix) is not int or not 0 <= prefix <= (32 if key == "ipv4" else 128):
                    raise ValueError("malformed address")
                parsed = ipaddress.ip_address(address)
                if parsed.version != (4 if key == "ipv4" else 6):
                    raise ValueError("address family mismatch")
                addresses.append({"address": str(parsed), "prefix": prefix})
        raw_dns = get(group, "dns-data")
        if raw_dns is not None:
            if not isinstance(raw_dns, list) or len(raw_dns) > 128:
                raise ValueError("malformed DNS")
            dns_known = True
            family_dns = []
            for entry in raw_dns:
                address = _text(entry)
                if address is None:
                    raise ValueError("malformed DNS")
                dns.append(address)
                family_dns.append(address)
            ip_setting["dns_servers"] = family_dns
        elif "dns" in group:
            legacy_dns_unknown = True
        raw_routes = get(group, "route-data")
        if raw_routes is not None:
            if not isinstance(raw_routes, list) or len(raw_routes) > 128:
                raise ValueError("malformed routes")
            routes_known = True
            for entry in raw_routes:
                if not isinstance(entry, dict):
                    raise ValueError("malformed route")
                destination, prefix = _text(get(entry, "dest")), get(entry, "prefix")
                metric, gateway, table = get(entry, "metric"), _text(get(entry, "next-hop")), get(entry, "table")
                if destination is None or type(prefix) is not int or not 0 <= prefix <= (32 if key == "ipv4" else 128) or (metric is not None and (type(metric) is not int or metric < 0)) or (table is not None and (type(table) is not int or table < 0)):
                    raise ValueError("malformed route")
                dest_ip = ipaddress.ip_address(destination)
                gateway_ip = ipaddress.ip_address(gateway) if gateway is not None else None
                if dest_ip.version != (4 if key == "ipv4" else 6) or (gateway_ip is not None and gateway_ip.version != dest_ip.version):
                    raise ValueError("route family mismatch")
                destination = str(dest_ip)
                gateway = str(gateway_ip) if gateway_ip is not None else None
                routes.append({"family": key, "destination": f"{destination}/{prefix}", "table": str(table) if table is not None else None,
                               "route_type": None, "scope": None, "protocol": None, "metric": metric,
                               "nexthops": [{"gateway": gateway, "interface": None, "weight": None, "ifindex": None, "object_id": None}] if gateway else None})
        raw_domains = get(group, "dns-search")
        if raw_domains is not None:
            if not isinstance(raw_domains, list) or len(raw_domains) > 128 or any(_text(x) is None for x in raw_domains):
                raise ValueError("malformed DNS domains")
            domains_known = True
            domains.extend({"name": x[1:] if x.startswith("~") else x, "route_only": x.startswith("~")} for x in raw_domains)
    return {"uuid": uuid, "name": label}, {"addresses": addresses if addresses_known else None,
            "dns_servers": dns if dns_known and not legacy_dns_unknown else None, "routing_domains": domains if domains_known else None,
            "routes": routes if routes_known else None, "search_domains": None,
            "ip_settings": ip_settings if ip_settings else None}


def observe(interface_name: str, connection_factory: Callable = _NMConnection) -> dict:
    """Find a daemon-verified active profile; caller rechecks kernel identity around it."""
    empty = {"profile": None, "applied": None, "version": None, "status": "unavailable", "gaps": []}
    try:
        with connection_factory() as bus:
            owner = bus.owner()
            try:
                version = _text(bus.prop(owner, ROOT, MANAGER, "Version"))
            except Exception:
                version = None
            device = bus.device_for_ip_interface(owner, interface_name)
            control_interface = bus.prop(owner, device, DEVICE, "Interface")
            if not isinstance(control_interface, str) or bus.prop(owner, device, DEVICE, "IpInterface") != interface_name:
                raise ValueError("device interface association mismatch")
            device_state = bus.prop(owner, device, DEVICE, "State")
            if type(device_state) is not int or device_state != 100:
                raise ValueError("device is not activated")
            active = bus.prop(owner, device, DEVICE, "ActiveConnection")
            if not isinstance(active, str) or active == "/":
                return empty | {"version": version, "status": "unavailable", "gaps": ["No verified active NetworkManager connection."]}
            uuid = _text(bus.prop(owner, active, ACTIVE, "Uuid"))
            connection = bus.prop(owner, active, ACTIVE, "Connection")
            active_devices = bus.prop(owner, active, ACTIVE, "Devices")
            state = bus.prop(owner, active, ACTIVE, "State")
            if not uuid or not isinstance(connection, str) or connection == "/" or not isinstance(active_devices, (list, tuple)) or device not in active_devices or state != 2:
                raise ValueError("active association incomplete")
            settings = bus.settings(owner, connection)
            identity, configured = _settings_view(settings)
            if identity["uuid"] != uuid:
                raise ValueError("profile identity changed")
            try:
                unsaved = bus.prop(owner, connection, SETTINGS, "Unsaved")
            except Exception:
                unsaved = None
            try:
                flags = bus.prop(owner, connection, SETTINGS, "Flags")
                if type(flags) is not int or flags < 0:
                    flags = None
            except Exception:
                flags = None
            generation = None
            generation_after = None
            applied = None
            applied_gap = []
            try:
                # Upstream 1.46.0 serializes this response with NON_SECRET. No other
                # release is assumed to have the same guarantee.
                if version != "1.46.0":
                    raise ValueError("applied connection disabled pending secret-exclusion guarantee")
                applied_raw, generation = bus.applied(owner, device)
                applied_identity, applied = _settings_view(applied_raw)
                if applied_identity["uuid"] not in (None, uuid):
                    raise ValueError("applied identity mismatch")
                _, generation_after = bus.applied(owner, device)
            except Exception:
                applied = None
                applied_gap = ["Applied connection is disabled for an unverified NetworkManager version." if version != "1.46.0" else "Applied connection is unavailable or unsupported."]
            identity_after, configured_after = _settings_view(bus.settings(owner, connection))
            try:
                flags_after = bus.prop(owner, connection, SETTINGS, "Flags") if flags is not None else None
                unsaved_after = bus.prop(owner, connection, SETTINGS, "Unsaved") if unsaved is not None else None
            except Exception:
                return empty | {"version": version, "status": "changed", "gaps": ["Profile flags could not be rechecked."]}
            if flags_after != flags or unsaved_after != unsaved or (applied is not None and (type(generation) is not int or generation != generation_after)) or identity_after != identity or configured_after != configured or bus.owner() != owner or bus.device_for_ip_interface(owner, interface_name) != device or bus.prop(owner, device, DEVICE, "Interface") != control_interface or bus.prop(owner, device, DEVICE, "IpInterface") != interface_name or bus.prop(owner, device, DEVICE, "State") != device_state or bus.prop(owner, device, DEVICE, "ActiveConnection") != active or bus.prop(owner, active, ACTIVE, "Connection") != connection or bus.prop(owner, active, ACTIVE, "Uuid") != uuid or bus.prop(owner, active, ACTIVE, "Devices") != active_devices or bus.prop(owner, active, ACTIVE, "State") != state:
                return empty | {"version": version, "status": "changed", "gaps": ["NetworkManager association or applied generation changed during collection."]}
            profile = {"provider": "NetworkManager", "uuid": uuid, "name": identity["name"], "version": None,
                       "device_identity": device, "device_ifindex": None, "active_connection_identity": active,
                       "state": "unknown", "unsaved": unsaved if type(unsaved) is bool else None,
                       "generated": bool(flags & 0x02) if flags is not None else None,
                       "external": bool(flags & 0x08) if flags is not None else None, **configured}
            common_gaps = ["Some IP settings are not established."]
            if configured["dns_servers"] is None:
                common_gaps.append("Legacy DNS encodings are not decoded; configured DNS is unknown.")
            if flags is None:
                common_gaps.append("Generated and external flags are unavailable.")
            if version is None:
                common_gaps.append("NetworkManager version is unavailable.")
            return {"profile": profile, "applied": {"provider": "NetworkManager", "version": str(generation), **applied} if applied is not None else None,
                    "version": version, "status": "observed", "gaps": common_gaps,
                    "applied_status": "observed" if applied is not None else "unavailable", "applied_gaps": common_gaps + applied_gap}
    except Exception:
        return empty | {"gaps": ["NetworkManager evidence was unavailable, malformed or denied."]}
