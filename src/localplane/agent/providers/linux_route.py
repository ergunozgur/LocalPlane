"""The Linux route provider: one question, asked of the kernel directly.

**What it answers.** "If this host sent a packet to *this* address right now, which
interface would it leave by?" That is `RTM_GETROUTE` — the same query `ip route get`
makes — and it is the corroboration the backend needs before it will believe that a
particular interface carries the connection an operator is reaching LocalPlane over.

**Why netlink rather than a command.** The invariant everywhere else in this agent is that
no caller-supplied value reaches an argv. A route lookup needs a target address, and that
address comes from the transport metadata of a request — so running ``ip route get <addr>``
would be the first place a value derived from outside reached a command line. There is no
version of that which is worth the risk. Instead the query is assembled in-process as a
netlink message: the address is parsed by :mod:`ipaddress` and re-serialised from its
packed form, so what reaches the kernel is sixteen bytes of address, not text. Nothing here
spawns a process, and there is no code path that could.

**Why it cannot write.** The socket carries exactly one message type out — ``RTM_GETROUTE``
— assembled by :func:`_request`, which is the only function that builds a frame and takes
no message type as an argument. ``RTM_NEWROUTE`` appears as an integer here because it is
what the *kernel* replies with; ``RTM_DELROUTE``, ``RTM_NEWLINK`` and ``RTM_SETLINK`` do
not appear at all. There is no generic ``netlink.call(type, payload)``, and adding one
would be a visible change to this file.

**Facts only.** What comes back is what the kernel said: an interface *index*, a preferred
source, a gateway, a table, a type and a scope. This module does not know what a LocalPlane
object is, does not turn an index into a name, and does not decide that anything is
protected. Correlating an index with an object is a judgement, and judgements are the
backend's.

**What it cannot answer.** The kernel's route choice depends on more than a destination:
policy rules, ``fwmark``, the socket's UID, VRF membership, a bound source address and the
network namespace can all select a different table. This query carries none of those, so it
reports the route an unmarked socket with no bound source would take. Where that is not the
route the operator's connection actually followed, the answer is corroboration that will
disagree with the other evidence — and disagreement is reported as disagreement, not
resolved in favour of whichever source spoke last.
"""

from __future__ import annotations

import errno as errno_module
import ipaddress
import os
import socket
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Protocol

from localplane.protocol.capabilities import CAPABILITY_NETWORK_ROUTE_OBSERVE

PROVIDER_NAME = "linux.route"
PROVIDER_VERSION = "1"
SOURCE = "rtnetlink"
METHOD = "netlink_rtm_getroute"

#: The address the capability probe asks about. A module constant, never a parameter:
#: probing must not be a way to reach an address of somebody else's choosing, and loopback
#: is the one destination every Linux host has a route to.
PROBE_DESTINATION = "127.0.0.1"

DEFAULT_TIMEOUT_S = 2.0
RECEIVE_BYTES = 32 * 1024

# --- netlink, from linux/netlink.h and linux/rtnetlink.h --------------------------------

NETLINK_ROUTE = 0
NLM_F_REQUEST = 0x01

NLMSG_ERROR = 0x02
NLMSG_DONE = 0x03

#: The only message this provider sends. There is no second one.
RTM_GETROUTE = 26
#: The message the kernel answers a lookup with. Received, never sent.
RTM_NEWROUTE = 24

_NLMSGHDR = struct.Struct("=IHHII")
_RTMSG = struct.Struct("=BBBBBBBBI")
_RTATTR = struct.Struct("=HH")

RTA_DST = 1
RTA_SRC = 2
RTA_OIF = 4
RTA_GATEWAY = 5
RTA_PRIORITY = 6
RTA_PREFSRC = 7
RTA_TABLE = 15

_FAMILY_NAMES = {socket.AF_INET: "inet", socket.AF_INET6: "inet6"}

#: ``rtm_type``. The kernel's own vocabulary, reported as it named it.
_ROUTE_TYPES = {
    0: "unspec", 1: "unicast", 2: "local", 3: "broadcast", 4: "anycast",
    5: "multicast", 6: "blackhole", 7: "unreachable", 8: "prohibit",
    9: "throw", 10: "nat", 11: "xresolve",
}
#: ``rtm_scope``.
_ROUTE_SCOPES = {0: "universe", 200: "site", 253: "link", 254: "host", 255: "nowhere"}
#: ``rtm_protocol`` — who installed the route.
_ROUTE_PROTOCOLS = {
    0: "unspec", 1: "redirect", 2: "kernel", 3: "boot", 4: "static",
    16: "dhcp", 42: "babel", 186: "bgp", 187: "isis", 188: "ospf", 189: "rip", 192: "eigrp",
}

#: Kernel errno values that mean "there is no route", as opposed to "the lookup failed".
#: The difference matters: the first is an answer about the host's routing table and the
#: second is LocalPlane not having managed to ask.
_UNREACHABLE = {
    errno_module.ENETUNREACH: "network_unreachable",
    errno_module.EHOSTUNREACH: "host_unreachable",
    errno_module.ENETDOWN: "network_down",
    errno_module.EACCES: "route_prohibited",
    errno_module.EINVAL: "route_lookup_rejected",
}


class RouteLookupStatus(StrEnum):
    """What became of one lookup. Four outcomes, and they are not interchangeable."""

    RESOLVED = "resolved"
    """The kernel returned a route. Its contents are in :class:`RouteFacts`."""

    UNREACHABLE = "unreachable"
    """The kernel answered, and its answer was that it has no route to the address."""

    FAILED = "failed"
    """The query was sent and something about the exchange did not work."""

    UNAVAILABLE = "unavailable"
    """The query could not be made at all — no netlink socket on this host."""


class InvalidRouteDestination(ValueError):
    """The destination is not an address a route could be looked up for."""


class NetlinkUnavailable(Exception):
    """No netlink socket could be opened at all on this host."""

    def __init__(self, reason: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


class NetlinkFailed(Exception):
    """A netlink socket exists and the exchange did not work."""

    def __init__(self, reason: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


class RouteQuery(Protocol):
    """The one I/O this provider does: send a query frame, return the raw reply.

    A seam of exactly the same shape as the command runner the other providers use, and for
    the same reason: the transport is the part a test cannot exercise every branch of on a
    real machine — a host with no route, a kernel that refuses, a truncated reply — while
    the parsing and the judgement above it are the parts worth exercising every run. What
    it may *not* do is choose the message: the frame arrives already built, by the only
    function in this module that can build one.
    """

    def __call__(self, request: bytes) -> bytes: ...


@dataclass(frozen=True)
class RouteFacts:
    """One route, exactly as the kernel described it. No interpretation.

    ``oif_index`` is an interface *index*, not a name. Turning it into a name would be a
    second correlation done in the wrong place: the backend already holds an observation of
    every interface with the index the kernel gave it, and joining on that index is an
    identity join against a stable object. Joining on a name would not be.

    Every field is optional because the kernel supplies what applies. A route through a
    directly connected network carries no gateway; a multipath route may carry no single
    egress interface. An absent attribute is reported as ``None`` and never as a zero.
    """

    family: str
    destination: str | None
    destination_prefix_length: int | None
    preferred_source: str | None
    gateway: str | None
    oif_index: int | None
    table: int | None
    route_type: str | None
    scope: str | None
    protocol: str | None
    priority: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "destination": self.destination,
            "destination_prefix_length": self.destination_prefix_length,
            "preferred_source": self.preferred_source,
            "gateway": self.gateway,
            "oif_index": self.oif_index,
            "table": self.table,
            "route_type": self.route_type,
            "scope": self.scope,
            "protocol": self.protocol,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class RouteObservation:
    """The result of one lookup: what was asked, what happened, and what came back."""

    destination: str
    family: str
    status: RouteLookupStatus
    observed_at: str
    reason: str | None = None
    route: RouteFacts | None = None
    error: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": PROVIDER_NAME,
            "provider_version": PROVIDER_VERSION,
            "capability": CAPABILITY_NETWORK_ROUTE_OBSERVE,
            "source": SOURCE,
            "method": METHOD,
            "destination": self.destination,
            "family": self.family,
            "status": str(self.status),
            "reason": self.reason,
            "route": None if self.route is None else self.route.as_dict(),
            "error": self.error,
            "observed_at": self.observed_at,
        }


class LinuxRouteProvider:
    """Ask the kernel for the route to one address. Read-only, in-process, no subprocess."""

    name = PROVIDER_NAME
    version = PROVIDER_VERSION

    def __init__(
        self, timeout_s: float = DEFAULT_TIMEOUT_S, query: RouteQuery | None = None
    ) -> None:
        self._timeout_s = timeout_s
        self._query = query if query is not None else KernelRouteQuery(timeout_s)

    def observe_route(self, destination: str) -> RouteObservation:
        """Look up the route to ``destination``.

        The destination is parsed into an :class:`ipaddress` object before anything else
        happens, and what is put on the wire is its ``packed`` form. A string that is not
        an address never reaches the kernel, and no string reaches it at all.
        """
        address = _parse_destination(destination)
        family = socket.AF_INET if address.version == 4 else socket.AF_INET6
        observed_at = _now()
        canonical = str(address)

        try:
            reply = self._query(_request(address, family))
        except NetlinkUnavailable as exc:
            return RouteObservation(
                destination=canonical,
                family=_FAMILY_NAMES[family],
                status=RouteLookupStatus.UNAVAILABLE,
                reason=exc.reason,
                error=exc.detail,
                observed_at=observed_at,
            )
        except NetlinkFailed as exc:
            return RouteObservation(
                destination=canonical,
                family=_FAMILY_NAMES[family],
                status=RouteLookupStatus.FAILED,
                reason=exc.reason,
                error=exc.detail,
                observed_at=observed_at,
            )

        return _interpret(reply, canonical, family, observed_at)


class KernelRouteQuery:
    """The real thing: one netlink datagram out, one back. The whole of this module's I/O.

    The socket is opened, used and closed per query. A route lookup happens a handful of
    times a minute at most, and a held socket would add a class of stale-descriptor bug in
    exchange for nothing measurable.

    The reply is required to come from the kernel — netlink source port zero. Any process
    on this host may open a netlink socket, and a message from one of them is not evidence
    about the routing table.
    """

    def __init__(self, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout_s = timeout_s

    def __call__(self, request: bytes) -> bytes:
        try:
            sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_ROUTE)
        except (AttributeError, OSError) as exc:
            raise NetlinkUnavailable("netlink_unavailable", {"error": _describe(exc)}) from exc
        try:
            sock.settimeout(self._timeout_s)
            try:
                sock.bind((0, 0))
                sock.send(request)
                payload, sender = sock.recvfrom(RECEIVE_BYTES)
            except socket.timeout as exc:
                raise NetlinkFailed("netlink_timeout", {"timeout_s": self._timeout_s}) from exc
            except OSError as exc:
                raise NetlinkFailed("netlink_io_error", {"error": _describe(exc)}) from exc
        finally:
            sock.close()

        if not isinstance(sender, tuple) or sender[0] != 0:
            raise NetlinkFailed(
                "netlink_reply_not_from_kernel", {"sender_port_id": _sender_port(sender)}
            )
        return payload


def probe_route(
    timeout_s: float = DEFAULT_TIMEOUT_S, query: RouteQuery | None = None
) -> RouteObservation:
    """Run the capability's own read, against a fixed destination. Used by discovery."""
    return LinuxRouteProvider(timeout_s=timeout_s, query=query).observe_route(PROBE_DESTINATION)


# ------------------------------------------------------------------------------ internals


def _parse_destination(destination: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if not isinstance(destination, str):
        raise InvalidRouteDestination("destination must be a string")
    try:
        address = ipaddress.ip_address(destination)
    except ValueError as exc:
        raise InvalidRouteDestination(f"not an IP address: {destination!r}") from exc
    if getattr(address, "scope_id", None) is not None:
        # A scoped address names an interface in its text form, and this query has no
        # field to carry one. Refusing is better than looking up an address whose meaning
        # depends on a scope the kernel was never told about.
        raise InvalidRouteDestination(f"scoped addresses are not supported: {destination!r}")
    return address


def _request(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address, family: int
) -> bytes:
    """Build the one message this provider sends: ``RTM_GETROUTE`` for a single address.

    The message type is not a parameter. This function is the only thing in the module
    that produces a netlink frame, and the only type it can produce is a query.
    """
    packed = address.packed
    header = _RTMSG.pack(family, len(packed) * 8, 0, 0, 0, 0, 0, 0, 0)
    attribute = _attribute(RTA_DST, packed)
    body = header + attribute
    return _NLMSGHDR.pack(
        _NLMSGHDR.size + len(body), RTM_GETROUTE, NLM_F_REQUEST, 1, 0
    ) + body


def _attribute(kind: int, payload: bytes) -> bytes:
    raw = _RTATTR.pack(_RTATTR.size + len(payload), kind) + payload
    return raw + b"\0" * (-len(raw) % 4)


def _interpret(
    reply: bytes, destination: str, family: int, observed_at: str
) -> RouteObservation:
    """Turn the kernel's answer into facts, or into the reason there are none."""
    try:
        messages = list(_messages(reply))
    except NetlinkFailed as exc:
        return RouteObservation(
            destination=destination,
            family=_FAMILY_NAMES[family],
            status=RouteLookupStatus.FAILED,
            reason=exc.reason,
            error=exc.detail,
            observed_at=observed_at,
        )

    for kind, body in messages:
        if kind == NLMSG_ERROR:
            (code,) = struct.unpack_from("=i", body, 0)
            if code == 0:
                continue  # an acknowledgement, not a failure
            number = -code
            detail = {"errno": number, "message": os.strerror(number)}
            reason = _UNREACHABLE.get(number)
            if reason is not None:
                return RouteObservation(
                    destination=destination,
                    family=_FAMILY_NAMES[family],
                    status=RouteLookupStatus.UNREACHABLE,
                    reason=reason,
                    error=detail,
                    observed_at=observed_at,
                )
            return RouteObservation(
                destination=destination,
                family=_FAMILY_NAMES[family],
                status=RouteLookupStatus.FAILED,
                reason="netlink_error",
                error=detail,
                observed_at=observed_at,
            )
        if kind == RTM_NEWROUTE:
            facts = _route_facts(body)
            if facts is None:
                break
            return RouteObservation(
                destination=destination,
                family=_FAMILY_NAMES[family],
                status=RouteLookupStatus.RESOLVED,
                route=facts,
                observed_at=observed_at,
            )

    return RouteObservation(
        destination=destination,
        family=_FAMILY_NAMES[family],
        status=RouteLookupStatus.FAILED,
        reason="no_route_in_reply",
        error={"message_types": sorted({kind for kind, _ in messages})},
        observed_at=observed_at,
    )


def _messages(reply: bytes):
    """Split a netlink datagram into ``(type, body)`` pairs, refusing a malformed one."""
    offset = 0
    while offset + _NLMSGHDR.size <= len(reply):
        length, kind, _flags, _seq, _pid = _NLMSGHDR.unpack_from(reply, offset)
        if length < _NLMSGHDR.size or offset + length > len(reply):
            raise NetlinkFailed(
                "malformed_netlink_response",
                {"declared_length": length, "remaining": len(reply) - offset},
            )
        if kind == NLMSG_DONE:
            return
        yield kind, reply[offset + _NLMSGHDR.size : offset + length]
        offset += (length + 3) & ~3


def _route_facts(body: bytes) -> RouteFacts | None:
    if len(body) < _RTMSG.size:
        return None
    (
        family,
        destination_prefix_length,
        _source_prefix_length,
        _tos,
        table,
        protocol,
        scope,
        route_type,
        _flags,
    ) = _RTMSG.unpack_from(body, 0)
    if family not in _FAMILY_NAMES:
        return None

    attributes = _attributes(body, _RTMSG.size)
    return RouteFacts(
        family=_FAMILY_NAMES[family],
        destination=_address(attributes.get(RTA_DST), family),
        destination_prefix_length=destination_prefix_length,
        preferred_source=_address(attributes.get(RTA_PREFSRC), family),
        gateway=_address(attributes.get(RTA_GATEWAY), family),
        oif_index=_uint32(attributes.get(RTA_OIF)),
        # RTA_TABLE carries the real identifier; rtm_table is a byte and saturates at 252
        # for tables beyond it, so the attribute wins where the kernel supplied one.
        table=_uint32(attributes.get(RTA_TABLE), default=table),
        route_type=_ROUTE_TYPES.get(route_type, str(route_type)),
        scope=_ROUTE_SCOPES.get(scope, str(scope)),
        protocol=_ROUTE_PROTOCOLS.get(protocol, str(protocol)),
        priority=_uint32(attributes.get(RTA_PRIORITY)),
    )


def _attributes(body: bytes, offset: int) -> dict[int, bytes]:
    found: dict[int, bytes] = {}
    while offset + _RTATTR.size <= len(body):
        length, kind = _RTATTR.unpack_from(body, offset)
        if length < _RTATTR.size or offset + length > len(body):
            break
        found[kind] = body[offset + _RTATTR.size : offset + length]
        offset += (length + 3) & ~3
    return found


def _address(payload: bytes | None, family: int) -> str | None:
    if payload is None:
        return None
    try:
        return socket.inet_ntop(family, payload)
    except (OSError, ValueError):
        return None


def _uint32(payload: bytes | None, default: int | None = None) -> int | None:
    if payload is None or len(payload) < 4:
        return default
    return int(struct.unpack_from("=I", payload, 0)[0])


def _sender_port(sender: Any) -> Any:
    if isinstance(sender, tuple) and sender:
        return sender[0]
    return None


def _describe(exc: OSError | Exception) -> str:
    if isinstance(exc, OSError):
        return exc.strerror or str(exc)
    return str(exc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
