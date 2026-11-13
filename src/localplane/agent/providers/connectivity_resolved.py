"""Named read-only systemd-resolved adapter; no resolver traffic is generated."""
from __future__ import annotations

import ipaddress
import socket
import struct
import time
from typing import Any, Callable

from jeepney import DBusAddress, new_method_call
from jeepney.bus_messages import message_bus
from jeepney.bus import get_bus
from jeepney.io.blocking import Proxy, DBusConnection, DBusConnectionBase, prep_socket
from jeepney.io.common import MessageFilters
from jeepney.low_level import Parser
from jeepney.wrappers import DBusErrorResponse, Properties, unwrap_msg

SERVICE = "org.freedesktop.resolve1"
ROOT = "/org/freedesktop/resolve1"
MANAGER = SERVICE + ".Manager"
LINK = SERVICE + ".Link"
TOTAL_SECONDS = 6.0
MAX_CALLS = 24
MAX_ITEMS = 128
MAX_BUS_FRAME = 1024 * 1024


class _BoundedParser(Parser):
    """Reject malformed or oversized D-Bus headers before body decoding."""
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
    raise ValueError("invalid resolver variant")


class _ResolvedConnection:
    def __init__(self, deadline: float | None = None) -> None:
        self.connection = None
        self.deadline = min(time.monotonic() + TOTAL_SECONDS, deadline) if deadline is not None else time.monotonic() + TOTAL_SECONDS
        self.calls = 0

    def __enter__(self):
        self.connection = _open_bounded_system_bus(min(2.0, self.deadline - time.monotonic()))
        return self

    def __exit__(self, *args):
        if self.connection:
            self.connection.close()

    def _budget(self):
        self.calls += 1
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or self.calls > MAX_CALLS:
            raise TimeoutError("resolver observation limit")
        return min(2.0, remaining)

    def owner(self):
        owner = Proxy(message_bus, self.connection, timeout=self._budget()).GetNameOwner(SERVICE)[0]
        if not isinstance(owner, str) or not owner.startswith(":"):
            raise ValueError("invalid resolver owner")
        return owner

    def link(self, owner: str, ifindex: int) -> str:
        address = DBusAddress(ROOT, bus_name=owner, interface=MANAGER)
        reply = self.connection.send_and_get_reply(new_method_call(address, "GetLink", "i", (ifindex,)), timeout=self._budget())
        path = unwrap_msg(reply)[0]
        if not isinstance(path, str) or not path.startswith(ROOT + "/link/"):
            raise ValueError("invalid resolver link")
        return path

    def prop(self, owner: str, path: str, interface: str, name: str) -> Any:
        allowed = {(MANAGER, "DNS"), (MANAGER, "Domains"), (MANAGER, "Version"), (MANAGER, "CurrentDNSServer"),
                   (LINK, "DNS"), (LINK, "Domains"), (LINK, "DefaultRoute"), (LINK, "CurrentDNSServer")}
        if (interface, name) not in allowed or (path != ROOT and not path.startswith(ROOT + "/link/")):
            raise ValueError("invalid resolver property")
        address = DBusAddress(path, bus_name=owner, interface=interface)
        return _variant(Proxy(Properties(address), self.connection, timeout=self._budget()).get(name)[0])

    def resolve_hostname(self, owner: str, name: str, family: int) -> tuple:
        # DNS only, without synthetic or registered local zone answers.
        flags = 1 | (1 << 8) | (1 << 11) | (1 << 13)
        address = DBusAddress(ROOT, bus_name=owner, interface=MANAGER)
        message = new_method_call(address, "ResolveHostname", "isit", (0, name, family, flags))
        return unwrap_msg(self.connection.send_and_get_reply(message, timeout=self._budget()))


def resolve_dns(name: str, record_type: str, deadline: float, *, connection_factory=None):
    """One configured-host resolver request. No server, port or protocol override."""
    # The separately owned executor is transferred by the lead before this callback is wired.
    from localplane.agent.diagnostics import DnsAnswer
    from localplane.protocol.diagnostic_policy import _name
    try:
        if _name(name) != name or record_type not in ("A", "AAAA") or deadline <= time.monotonic():
            raise ValueError("invalid DNS request")
        family = socket.AF_INET if record_type == "A" else socket.AF_INET6
        factory = connection_factory or (lambda: _ResolvedConnection(deadline))
        with factory() as bus:
            owner = bus.owner()
            try:
                response = bus.resolve_hostname(owner, name, family)
            except DBusErrorResponse:
                if bus.owner() != owner:
                    return DnsAnswer(outcome="unavailable", reason="resolver_changed")
                raise
            if bus.owner() != owner:
                return DnsAnswer(outcome="unavailable", reason="resolver_changed")
            if len(response) != 3:
                raise ValueError("malformed resolver response")
            addresses, _canonical, flags = response
            if type(flags) is not int or not flags & 1 or flags & 0x1e or not isinstance(addresses, (list, tuple)) or len(addresses) > 4096:
                raise ValueError("unsupported resolver response")
            values = []
            for entry in addresses:
                value = _server(entry, True)
                if ipaddress.ip_address(value).version != (4 if family == socket.AF_INET else 6):
                    raise ValueError("resolver answer family mismatch")
                values.append(value)
            if not values:
                raise ValueError("empty resolver answer without NODATA evidence")
            return DnsAnswer(outcome="resolved", answers=tuple(values[:32]),
                             truncated=len(values) > 32, detail="Host resolver response; cache, CNAME and upstream behavior are provider-controlled.")
    except DBusErrorResponse as exc:
        statuses = {"org.freedesktop.resolve1.NoSuchRR": "no_data", "org.freedesktop.resolve1.DnsError.NXDOMAIN": "not_found",
                    "org.freedesktop.resolve1.DnsError.SERVFAIL": "server_failure", "org.freedesktop.resolve1.DnsError.REFUSED": "refused",
                    "org.freedesktop.DBus.Error.Timeout": "timeout"}
        return DnsAnswer(outcome=statuses.get(exc.name, "unavailable"), answers=None, truncated=None, detail="Host resolver returned an error.")
    except TimeoutError:
        return DnsAnswer(outcome="timeout", answers=None, truncated=None, detail="Host resolver response timed out; provider activity may continue.")
    except Exception:
        return DnsAnswer(outcome="unavailable", answers=None, truncated=None, detail="Host resolver evidence is unavailable or malformed.")


def _server(value: Any, with_index: bool) -> str:
    if not isinstance(value, (list, tuple)) or len(value) != (3 if with_index else 2):
        raise ValueError("invalid DNS server")
    family, raw = value[-2:]
    if not isinstance(family, int) or isinstance(family, bool) or family not in (socket.AF_INET, socket.AF_INET6):
        raise ValueError("invalid DNS family")
    if not isinstance(raw, (list, bytes, bytearray)):
        raise ValueError("invalid DNS address")
    address = ipaddress.ip_address(bytes(raw))
    if address.version != (4 if family == socket.AF_INET else 6):
        raise ValueError("DNS family mismatch")
    return str(address)


def _servers(value: Any, with_index: bool) -> list[str]:
    if not isinstance(value, (list, tuple)) or len(value) > MAX_ITEMS:
        raise ValueError("invalid DNS server list")
    result = []
    for item in value:
        address = _server(item, with_index)
        if with_index and (type(item[0]) is not int or item[0] < 0):
            raise ValueError("invalid DNS interface index")
        if not with_index or item[0] == 0:
            result.append(address)
    return result


def _global_current(value: Any) -> tuple[int, str]:
    address = _server(value, True)
    if type(value[0]) is not int or value[0] < 0:
        raise ValueError("invalid DNS server index")
    return value[0], address


def _domains(value: Any, with_index: bool) -> list[dict]:
    if not isinstance(value, (list, tuple)) or len(value) > MAX_ITEMS:
        raise ValueError("invalid DNS domains")
    result = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != (3 if with_index else 2):
            raise ValueError("invalid DNS domain")
        name, route_only = item[-2:]
        if not isinstance(name, str) or len(name) > 255 or type(route_only) is not bool:
            raise ValueError("invalid DNS domain")
        if with_index and (type(item[0]) is not int or item[0] < 0):
            raise ValueError("invalid DNS domain index")
        if with_index and item[0] != 0:
            continue
        result.append({"name": name, "route_only": route_only})
    return result


def observe(ifindex: int, interface_name: str, connection_factory: Callable = _ResolvedConnection) -> dict:
    gaps: list[str] = []
    version = None
    try:
        with connection_factory() as bus:
            owner = bus.owner()
            link = bus.link(owner, ifindex)
            def optional(path, interface, prop, transform):
                try:
                    return transform(bus.prop(owner, path, interface, prop))
                except Exception:
                    gaps.append(f"Resolver {prop} unavailable or unsupported.")
                    return None
            version = optional(ROOT, MANAGER, "Version", lambda x: x if isinstance(x, str) and len(x) <= 128 else None)
            global_servers = optional(ROOT, MANAGER, "DNS", lambda x: _servers(x, True))
            global_domains = optional(ROOT, MANAGER, "Domains", lambda x: _domains(x, True))
            global_current = optional(ROOT, MANAGER, "CurrentDNSServer", _global_current)
            servers = optional(link, LINK, "DNS", lambda x: _servers(x, False))
            domains = optional(link, LINK, "Domains", lambda x: _domains(x, False))
            default_route = optional(link, LINK, "DefaultRoute", lambda x: x if type(x) is bool else (_ for _ in ()).throw(ValueError()))
            current = optional(link, LINK, "CurrentDNSServer", lambda x: _server(x, False))
            if bus.owner() != owner or bus.link(owner, ifindex) != link:
                return {"status": "changed", "version": version, "value": None, "gaps": ["Resolver owner or link changed during collection."]}
            if all(value is None for value in (global_servers, global_domains, global_current, servers, domains, default_route, current)):
                return {"status": "unavailable", "version": version, "value": None, "gaps": gaps or ["No resolver properties could be read."]}
            value = {"global_servers": global_servers, "global_domains": None, "global_routing_domains": global_domains,
                     "current_server": global_current[1] if global_current else None,
                     "current_server_ifindex": global_current[0] if global_current else None,
                     "links": [{"interface": interface_name, "servers": servers, "domains": None,
                                "routing_domains": domains, "current_server": current, "dns_default_route": default_route}]}
            gaps.append("Resolver properties were sampled sequentially; no atomic configuration generation is available.")
            gaps.append("Legacy DNS properties do not expose port or TLS server name; current server does not prove which server answered a query.")
            return {"status": "observed", "version": version, "value": value, "gaps": gaps}
    except Exception:
        return {"status": "unavailable", "version": version, "value": None, "gaps": ["Resolver service unavailable, denied or changed."]}
