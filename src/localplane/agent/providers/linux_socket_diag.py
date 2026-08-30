"""Narrow, read-only Linux TCP socket correlation through SOCK_DIAG.

The adapter sends exactly one kernel request type: ``SOCK_DIAG_BY_FAMILY`` for TCP
``ESTABLISHED`` sockets in one caller-selected *typed family*.  It has no generic netlink
surface, no arbitrary message number, no spawned-command fallback, and no namespace-entry
path.  Results are bounded and an answer is accepted only when both address/port pairs
match exactly and exactly one socket supplies ``INET_DIAG_CGROUP_ID``.
"""

from __future__ import annotations

import errno
import ipaddress
import os
import socket
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Final


NETLINK_SOCK_DIAG: Final = 4
SOCK_DIAG_BY_FAMILY: Final = 20
INET_DIAG_CGROUP_ID: Final = 21
IPPROTO_TCP: Final = 6
TCP_ESTABLISHED: Final = 1

NLM_F_REQUEST: Final = 0x01
NLM_F_ROOT: Final = 0x100
NLM_F_MATCH: Final = 0x200
NLM_F_DUMP: Final = NLM_F_ROOT | NLM_F_MATCH
NLMSG_ERROR: Final = 0x02
NLMSG_DONE: Final = 0x03

DEFAULT_TIMEOUT_S: Final = 2.0
MAX_DIAG_MESSAGES: Final = 4096
MAX_DIAG_BYTES: Final = 8 * 1024 * 1024
MAX_CGROUP_DIRECTORIES: Final = 16384

_NLMSG = struct.Struct("=IHHII")
_REQ_HEAD = struct.Struct("=BBBBI")
_SOCKID_PORTS = struct.Struct("!HH")
_SOCKID_TAIL = struct.Struct("=III")
_DIAG_HEAD = struct.Struct("=BBBB")
_DIAG_TAIL = struct.Struct("=IIIII")
_NLA = struct.Struct("=HH")
_U64 = struct.Struct("=Q")
_I32 = struct.Struct("=i")


@dataclass(frozen=True)
class TcpSocketTuple:
    family: str
    peer_ip: str
    peer_port: int
    local_ip: str
    local_port: int

    def __post_init__(self) -> None:
        peer = ipaddress.ip_address(self.peer_ip)
        local = ipaddress.ip_address(self.local_ip)
        expected = 4 if self.family == "ipv4" else 6 if self.family == "ipv6" else 0
        if expected == 0 or peer.version != expected or local.version != expected:
            raise ValueError("socket tuple family and addresses must agree")
        if not (1 <= self.peer_port <= 65535 and 1 <= self.local_port <= 65535):
            raise ValueError("socket tuple ports must be between 1 and 65535")


@dataclass(frozen=True)
class SocketDiagMatch:
    cgroup_id: int
    socket_inode: int
    uid: int


@dataclass(frozen=True)
class SocketDiagResult:
    status: str
    reason: str
    match: SocketDiagMatch | None = None
    match_count: int = 0
    inspected_count: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "match_count": self.match_count,
            "inspected_count": self.inspected_count,
            "match": (
                None
                if self.match is None
                else {
                    "cgroup_id": self.match.cgroup_id,
                    "socket_inode": self.match.socket_inode,
                    "uid": self.match.uid,
                }
            ),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CgroupPathResult:
    status: str
    reason: str
    path: str | None = None
    inspected_count: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "path": self.path,
            "inspected_count": self.inspected_count,
            "detail": self.detail,
        }


class LinuxSocketDiag:
    """Perform one exact TCP lookup in this process's current network namespace."""

    def __init__(
        self,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_messages: int = MAX_DIAG_MESSAGES,
        max_bytes: int = MAX_DIAG_BYTES,
        socket_factory: Callable[..., socket.socket] = socket.socket,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._timeout_s = timeout_s
        self._max_messages = max_messages
        self._max_bytes = max_bytes
        self._socket_factory = socket_factory
        self._monotonic = monotonic

    def network_namespace_inode(self) -> int | None:
        try:
            return os.stat("/proc/self/ns/net").st_ino
        except OSError:
            return None

    def probe(self) -> dict[str, Any]:
        """Establish that the read-only transport and unified cgroup estate exist."""
        inode = self.network_namespace_inode()
        if inode is None:
            return {"status": "unavailable", "reason": "network_namespace_unreadable"}
        if not Path("/sys/fs/cgroup/cgroup.controllers").exists():
            return {
                "status": "unavailable",
                "reason": "unified_cgroup_v2_unavailable",
                "network_namespace_inode": inode,
            }
        try:
            channel = self._socket_factory(
                socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_SOCK_DIAG
            )
            channel.close()
        except OSError as exc:
            return {
                "status": "unavailable",
                "reason": "sock_diag_unavailable",
                "network_namespace_inode": inode,
                "errno": exc.errno,
            }
        return {
            "status": "available",
            "reason": "sock_diag_read_transport_available",
            "network_namespace_inode": inode,
            "cgroup_mode": "v2",
        }

    def lookup(self, query: TcpSocketTuple) -> SocketDiagResult:
        family = socket.AF_INET if query.family == "ipv4" else socket.AF_INET6
        sequence = (os.getpid() ^ time.monotonic_ns()) & 0xFFFFFFFF
        request = encode_request(query, sequence=sequence)
        deadline = self._monotonic() + self._timeout_s
        inspected = 0
        received_bytes = 0
        matches: list[SocketDiagMatch | None] = []

        try:
            channel = self._socket_factory(
                socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_SOCK_DIAG
            )
            try:
                channel.bind((0, 0))
                channel.send(request)
                done = False
                while not done:
                    remaining = deadline - self._monotonic()
                    if remaining <= 0:
                        return SocketDiagResult(
                            "failed", "socket_diag_deadline_expired",
                            match_count=len(matches), inspected_count=inspected,
                        )
                    channel.settimeout(remaining)
                    frame = channel.recv(65535)
                    received_bytes += len(frame)
                    if received_bytes > self._max_bytes:
                        return SocketDiagResult(
                            "failed", "socket_diag_byte_limit_exceeded",
                            match_count=len(matches), inspected_count=inspected,
                            detail={"limit": self._max_bytes},
                        )
                    for message_type, message_sequence, payload in iter_netlink(frame):
                        if message_sequence != sequence:
                            continue
                        if message_type == NLMSG_DONE:
                            done = True
                            break
                        if message_type == NLMSG_ERROR:
                            code = _I32.unpack_from(payload)[0] if len(payload) >= 4 else -errno.EIO
                            if code:
                                return SocketDiagResult(
                                    "failed", "socket_diag_kernel_error",
                                    match_count=len(matches), inspected_count=inspected,
                                    detail={"errno": abs(code), "error": os.strerror(abs(code))},
                                )
                            continue
                        if message_type != SOCK_DIAG_BY_FAMILY:
                            continue
                        inspected += 1
                        if inspected > self._max_messages:
                            return SocketDiagResult(
                                "failed", "socket_diag_result_limit_exceeded",
                                match_count=len(matches), inspected_count=inspected,
                                detail={"limit": self._max_messages},
                            )
                        decoded = decode_diag_message(payload)
                        if decoded is None or decoded["family"] != family:
                            continue
                        if _matches(query, decoded):
                            cgroup_id = decoded.get("cgroup_id")
                            matches.append(
                                None
                                if not isinstance(cgroup_id, int) or cgroup_id <= 0
                                else SocketDiagMatch(
                                    cgroup_id=cgroup_id,
                                    socket_inode=int(decoded["inode"]),
                                    uid=int(decoded["uid"]),
                                )
                            )
            finally:
                channel.close()
        except (OSError, TimeoutError) as exc:
            return SocketDiagResult(
                "failed", "socket_diag_read_failed",
                match_count=len(matches), inspected_count=inspected,
                detail={"error_type": type(exc).__name__, "error": str(exc)},
            )

        if not matches:
            return SocketDiagResult(
                "not_found", "exact_tcp_socket_not_found", inspected_count=inspected
            )
        if len(matches) > 1:
            return SocketDiagResult(
                "ambiguous", "exact_tcp_socket_ambiguous",
                match_count=len(matches), inspected_count=inspected,
            )
        if matches[0] is None:
            return SocketDiagResult(
                "unsupported", "inet_diag_cgroup_id_unavailable",
                match_count=1, inspected_count=inspected,
            )
        return SocketDiagResult(
            "matched", "exact_tcp_socket_matched",
            match=matches[0], match_count=1, inspected_count=inspected,
        )


def encode_request(query: TcpSocketTuple, *, sequence: int) -> bytes:
    """Encode the single request this adapter is capable of sending."""
    family = socket.AF_INET if query.family == "ipv4" else socket.AF_INET6
    source = _address_bytes(query.local_ip, query.family)
    destination = _address_bytes(query.peer_ip, query.family)
    sockid = (
        _SOCKID_PORTS.pack(query.local_port, query.peer_port)
        + source
        + destination
        + _SOCKID_TAIL.pack(0, 0xFFFFFFFF, 0xFFFFFFFF)
    )
    body = _REQ_HEAD.pack(
        family, IPPROTO_TCP, 0, 0, 1 << TCP_ESTABLISHED
    ) + sockid
    return _NLMSG.pack(
        _NLMSG.size + len(body),
        SOCK_DIAG_BY_FAMILY,
        NLM_F_REQUEST | NLM_F_DUMP,
        sequence,
        0,
    ) + body


def iter_netlink(frame: bytes):
    offset = 0
    while offset + _NLMSG.size <= len(frame):
        length, message_type, _flags, sequence, _pid = _NLMSG.unpack_from(frame, offset)
        if length < _NLMSG.size or offset + length > len(frame):
            break
        yield message_type, sequence, frame[offset + _NLMSG.size : offset + length]
        offset += _align4(length)


def decode_diag_message(payload: bytes) -> dict[str, Any] | None:
    if len(payload) < 72:
        return None
    family, state, _timer, _retrans = _DIAG_HEAD.unpack_from(payload, 0)
    sport, dport = _SOCKID_PORTS.unpack_from(payload, 4)
    src = payload[8:24]
    dst = payload[24:40]
    interface, _cookie0, _cookie1 = _SOCKID_TAIL.unpack_from(payload, 40)
    _expires, _rqueue, _wqueue, uid, inode = _DIAG_TAIL.unpack_from(payload, 52)
    attributes: dict[int, bytes] = {}
    offset = 72
    while offset + _NLA.size <= len(payload):
        length, kind = _NLA.unpack_from(payload, offset)
        if length < _NLA.size or offset + length > len(payload):
            break
        attributes[kind] = payload[offset + _NLA.size : offset + length]
        offset += _align4(length)
    cgroup = attributes.get(INET_DIAG_CGROUP_ID)
    return {
        "family": family,
        "state": state,
        "local_ip": _decode_address(src, family),
        "local_port": sport,
        "peer_ip": _decode_address(dst, family),
        "peer_port": dport,
        "interface": interface,
        "uid": uid,
        "inode": inode,
        "cgroup_id": _U64.unpack_from(cgroup)[0] if cgroup and len(cgroup) >= 8 else None,
    }


def resolve_cgroup_path(
    cgroup_id: int,
    *,
    root: str | Path = "/sys/fs/cgroup",
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_directories: int = MAX_CGROUP_DIRECTORIES,
    monotonic: Callable[[], float] = time.monotonic,
) -> CgroupPathResult:
    """Resolve a cgroup-v2 kernel id to its absolute unified hierarchy path.

    On cgroup v2 ``INET_DIAG_CGROUP_ID`` is the kernfs inode exposed by ``stat``.  Walking
    the mounted hierarchy is read-only and bounded.  No result is accepted on cgroup v1,
    where this identity/path relationship is not the contract.
    """
    root_path = Path(root)
    if not (root_path / "cgroup.controllers").exists():
        return CgroupPathResult("unsupported", "unified_cgroup_v2_unavailable")
    deadline = monotonic() + timeout_s
    pending = [root_path]
    inspected = 0
    while pending:
        if monotonic() >= deadline:
            return CgroupPathResult(
                "failed", "cgroup_walk_deadline_expired", inspected_count=inspected
            )
        current = pending.pop()
        inspected += 1
        if inspected > max_directories:
            return CgroupPathResult(
                "failed", "cgroup_walk_limit_exceeded", inspected_count=inspected,
                detail={"limit": max_directories},
            )
        try:
            if current.stat().st_ino == cgroup_id:
                relative = current.relative_to(root_path)
                path = "/" if not relative.parts else "/" + relative.as_posix()
                return CgroupPathResult(
                    "resolved", "cgroup_path_resolved", path=path,
                    inspected_count=inspected,
                )
            with os.scandir(current) as entries:
                children = [Path(entry.path) for entry in entries if entry.is_dir(follow_symlinks=False)]
            pending.extend(sorted(children, reverse=True))
        except OSError as exc:
            return CgroupPathResult(
                "failed", "cgroup_walk_failed", inspected_count=inspected,
                detail={"path": str(current), "error_type": type(exc).__name__, "error": str(exc)},
            )
    return CgroupPathResult(
        "not_found", "cgroup_id_not_found", inspected_count=inspected,
        detail={"cgroup_id": cgroup_id},
    )


def _matches(query: TcpSocketTuple, decoded: dict[str, Any]) -> bool:
    return (
        decoded.get("state") == TCP_ESTABLISHED
        and decoded.get("local_ip") == str(ipaddress.ip_address(query.local_ip))
        and decoded.get("local_port") == query.local_port
        and decoded.get("peer_ip") == str(ipaddress.ip_address(query.peer_ip))
        and decoded.get("peer_port") == query.peer_port
    )


def _address_bytes(value: str, family: str) -> bytes:
    packed = ipaddress.ip_address(value).packed
    return packed + (b"\0" * 12 if family == "ipv4" else b"")


def _decode_address(raw: bytes, family: int) -> str | None:
    try:
        if family == socket.AF_INET:
            return str(ipaddress.ip_address(raw[:4]))
        if family == socket.AF_INET6:
            return str(ipaddress.ip_address(raw[:16]))
    except ValueError:
        pass
    return None


def _align4(value: int) -> int:
    return (value + 3) & ~3
