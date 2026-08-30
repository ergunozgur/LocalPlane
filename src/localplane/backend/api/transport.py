"""Reading the transport a request actually arrived on.

**The whole of this module is four lines of substance and one rule**: the peer and the
local endpoint come from the ASGI connection scope, which the server fills in from the
accepted socket — ``getpeername`` and ``getsockname`` — and from nowhere else.

Headers are not consulted. Not ``X-Forwarded-For``, not ``X-Real-IP``, not ``Forwarded``,
not ``Host``, and not any header a future deployment might invent. Every one of them is
written by whoever is asking, and a management path proven from something the caller wrote
is a request that has been believed rather than evidence that has been gathered. There is
no configuration flag here that turns them on, because a flag is a thing somebody sets in a
hurry: a trusted-proxy model is a design with its own evidence, its own review and its own
design work, and until it exists the honest answer behind a reverse proxy is that the direct peer
is the proxy and the management path is unknown.

The scope's values are strings and may be anything, including a name a test server made up
or ``None`` on a Unix-socket listener. Classifying them — parsing, normalising an
IPv4-mapped address, refusing loopback, link-local and wildcard — is
:func:`~localplane.backend.domain.management_path.read_transport`'s job, which is pure and
knows nothing about HTTP. This module only fetches.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from typing import Any, Mapping

from localplane.backend.domain.management_path import TransportEvidence, read_transport


@dataclass(frozen=True)
class RequestConnectionEvidence:
    """Private exact TCP evidence from the accepted ASGI transport.

    Unlike reusable management-path evidence this deliberately retains both ports.  The
    agent needs all four endpoint coordinates to identify exactly one accepted kernel
    socket.  It is not a request model and is never populated from HTTP data or headers.
    """

    status: str
    family: str | None = None
    peer_ip: str | None = None
    peer_port: int | None = None
    local_ip: str | None = None
    local_port: int | None = None
    backend_netns_inode: int | None = None
    reason: str | None = None

    @property
    def usable(self) -> bool:
        return self.status == "observed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "family": self.family,
            "peer_ip": self.peer_ip,
            "peer_port": self.peer_port,
            "local_ip": self.local_ip,
            "local_port": self.local_port,
            "backend_netns_inode": self.backend_netns_inode,
            "reason": self.reason,
        }


def transport_of(request: Any) -> TransportEvidence:
    """The evidence this request's own connection carries about how it arrived."""
    return transport_from_scope(getattr(request, "scope", {}) or {})


def request_connection_of(request: Any) -> RequestConnectionEvidence:
    """Read the exact accepted TCP tuple; headers and request data are unreachable here."""
    return request_connection_from_scope(getattr(request, "scope", {}) or {})


def request_connection_from_scope(
    scope: Mapping[str, Any], *, netns_inode: int | None = None
) -> RequestConnectionEvidence:
    """Normalise a TCP ASGI scope into exact kernel lookup coordinates.

    Test servers and Unix listeners commonly provide names or omit ports.  Those are
    explicit unsupported evidence, not guessed values.  IPv4-mapped IPv6 addresses are
    normalised to IPv4 only when both endpoints map, so the family and packed addresses
    remain coherent.
    """
    peer = _tcp_endpoint(scope.get("client"))
    local = _tcp_endpoint(scope.get("server"))
    if peer is None or local is None:
        return RequestConnectionEvidence(
            status="unsupported", reason="accepted_tcp_tuple_unavailable"
        )
    peer_address, peer_port = peer
    local_address, local_port = local
    peer_mapped = getattr(peer_address, "ipv4_mapped", None)
    local_mapped = getattr(local_address, "ipv4_mapped", None)
    if peer_mapped is not None and local_mapped is not None:
        peer_address, local_address = peer_mapped, local_mapped
    if peer_address.version != local_address.version:
        return RequestConnectionEvidence(
            status="unsupported", reason="accepted_tcp_family_mismatch"
        )
    if netns_inode is None:
        try:
            netns_inode = os.stat("/proc/self/ns/net").st_ino
        except OSError:
            return RequestConnectionEvidence(
                status="unsupported", reason="backend_network_namespace_unreadable"
            )
    return RequestConnectionEvidence(
        status="observed",
        family="ipv4" if peer_address.version == 4 else "ipv6",
        peer_ip=str(peer_address),
        peer_port=peer_port,
        local_ip=str(local_address),
        local_port=local_port,
        backend_netns_inode=netns_inode,
    )


def transport_from_scope(scope: Mapping[str, Any]) -> TransportEvidence:
    """Classify an ASGI scope's ``client`` and ``server`` pair.

    ``client`` is the remote peer of the accepted connection and ``server`` is the local
    endpoint it terminated on. The second is the load-bearing one: an operator reaching
    ``192.0.2.215`` has demonstrated that their connection lands on whichever object carries
    that address, which is a far stronger statement than anything their own address alone
    could support.

    A server bound to a wildcard reports the *accepted* socket's local address here, not
    the bind address, which is what makes this usable at all. A server bound to a Unix
    socket has no addresses to report and says so by leaving them out — an absence this
    reads as an absence rather than as a default.
    """
    return read_transport(_address(scope.get("client")), _address(scope.get("server")))


def _address(endpoint: Any) -> str | None:
    """The address half of an ASGI ``(host, port)`` pair, if there is one.

    The port is deliberately dropped. It is ephemeral on the peer side and fixed on the
    server side, and neither says anything about which object a connection terminated on —
    while keeping it would tie every piece of stored evidence to a single TCP connection
    and make reuse impossible, which would turn every read into a write.
    """
    if isinstance(endpoint, (list, tuple)) and endpoint:
        host = endpoint[0]
        return host if isinstance(host, str) and host else None
    return None


def _tcp_endpoint(
    endpoint: Any,
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, int] | None:
    if not isinstance(endpoint, (list, tuple)) or len(endpoint) < 2:
        return None
    host, port = endpoint[0], endpoint[1]
    if not isinstance(host, str) or isinstance(port, bool) or not isinstance(port, int):
        return None
    if not 1 <= port <= 65535:
        return None
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]), port
    except ValueError:
        return None
