"""Synchronous, policy-gated DNS resolution and numeric TCP handshakes.

The resolve callback is an internal named-provider seam, not a request option.
This module starts no background work. A caller abandoning its response does not
release the shared slot: callback and socket cleanup must actually finish first.
"""

from __future__ import annotations

import errno
import ipaddress
import math
import socket
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Callable, Literal, Protocol

from localplane.protocol.diagnostic_policy import (
    InvalidDiagnosticPolicy,
    LoadedDiagnosticPolicy,
    _name,
    _target,
    tcp_target_permitted,
)

DnsOutcome = Literal[
    "resolved", "no_data", "not_found", "server_failure", "refused", "timeout",
    "unavailable", "denied", "policy_mismatch", "busy", "rate_limited", "error",
]
TcpOutcome = Literal[
    "connected", "refused", "timeout", "no_route", "permission_denied",
    "unavailable", "denied", "policy_mismatch", "busy", "rate_limited", "error",
]
RecordType = Literal["A", "AAAA"]
DnsReason = Literal["resolver_changed"]


@dataclass(frozen=True, slots=True)
class CurrentExclusions:
    """Current kernel guards and only genuine additive connection exclusions.

    The internal callback owns current host/agent/namespace validation. An empty
    management tuple means no additional justified connection facts, not universal
    discovery. The executor always merges the administrator's declared deny set.
    """
    local_addresses: tuple[str, ...] | None = None
    broadcast_addresses: tuple[str, ...] | None = None
    management_addresses: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class DnsAnswer:
    outcome: DnsOutcome
    answers: tuple[str, ...] | None = None
    truncated: bool | None = None
    detail: str = ""
    reason: DnsReason | None = None


def _stamp(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


@dataclass(frozen=True, slots=True)
class SocketEndpoint:
    address: str
    port: int

    def to_wire(self) -> dict[str, object]:
        return {"address": self.address, "port": self.port}


@dataclass(frozen=True, slots=True)
class DnsResult:
    name: str
    record_type: RecordType
    outcome: DnsOutcome
    detail: str
    # Private per-invocation proof. No default and no public wire field.
    completion_proven: bool = field(kw_only=True)
    vantage: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    answers: tuple[str, ...] | None = None
    truncated: bool | None = None
    kind: Literal["dns"] = field(default="dns", init=False)

    def to_wire(self) -> dict[str, object]:
        return {
            "kind": self.kind, "name": self.name, "record_type": self.record_type,
            "outcome": self.outcome, "vantage": self.vantage,
            "started_at": _stamp(self.started_at), "ended_at": _stamp(self.ended_at),
            "answers": list(self.answers) if self.answers is not None else None,
            "truncated": self.truncated, "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class TcpResult:
    address: str
    port: int
    outcome: TcpOutcome
    detail: str
    completion_proven: bool = field(kw_only=True)
    vantage: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    local: SocketEndpoint | None = None
    remote: SocketEndpoint | None = None
    kind: Literal["tcp"] = field(default="tcp", init=False)

    def to_wire(self) -> dict[str, object]:
        return {
            "kind": self.kind, "address": self.address, "port": self.port,
            "outcome": self.outcome, "vantage": self.vantage,
            "started_at": _stamp(self.started_at), "ended_at": _stamp(self.ended_at),
            "local": self.local.to_wire() if self.local is not None else None,
            "remote": self.remote.to_wire() if self.remote is not None else None,
            "detail": self.detail,
        }


class DiagnosticLimiter:
    """One slot and six admitted starts in the half-open rolling window (t-60, t]."""

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic):
        self._monotonic = monotonic
        self._lock = Lock()
        self._active = False
        self._starts: deque[float] = deque()

    def try_acquire(self) -> Literal["busy", "rate_limited"] | None:
        if not self._lock.acquire(blocking=False):
            return "busy"
        try:
            if self._active:
                return "busy"
            now = self._monotonic()
            if not math.isfinite(now):
                return "busy"
            while self._starts and now - self._starts[0] >= 60:
                self._starts.popleft()
            if len(self._starts) >= 6:
                return "rate_limited"
            self._starts.append(now)
            self._active = True
            return None
        finally:
            self._lock.release()

    def release(self) -> None:
        with self._lock:
            self._active = False

    def active(self) -> bool | None:
        """Read resource state, NOT request completion; never wait/reset/cancel."""
        if not self._lock.acquire(blocking=False):
            return None
        try:
            return self._active
        finally:
            self._lock.release()


class DiagnosticSocket(Protocol):
    def settimeout(self, value: float) -> None: ...
    def connect(self, address: tuple) -> None: ...
    def getsockname(self) -> tuple: ...
    def getpeername(self) -> tuple: ...
    def close(self) -> None: ...


_DNS_DETAILS = {
    "resolved": "Configured host resolver returned address answers; answering upstream is not established.",
    "no_data": "Configured host resolver reported no address data for this record type.",
    "not_found": "Configured host resolver reported that the name was not found.",
    "server_failure": "Configured host resolver reported a DNS server failure.",
    "refused": "Configured host resolver reported a refused DNS query.",
    "timeout": "Resolver deadline elapsed; this does not prove that provider-internal traffic stopped.",
    "unavailable": "Configured host DNS resolver is unavailable or unsupported; no NSS fallback was used.",
    "error": "Configured host resolver could not produce valid DNS evidence.",
}
_TCP_DETAILS = {
    "connected": "TCP handshake completed and the socket was closed; this is not service health.",
    "refused": "Connection was refused; the rejecting device or process is not identified.",
    "timeout": "Connection or response deadline elapsed; no firewall cause is established.",
    "no_route": "The socket reported network or host unreachable for this attempt.",
    "permission_denied": "The socket reported an operating-system permission denial.",
    "unavailable": "The required socket operation or address family is unavailable.",
    "error": "The TCP attempt failed without a more specific proven outcome.",
}


def _socket_outcome(error: Exception) -> TcpOutcome:
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, OSError):
        if error.errno == errno.ECONNREFUSED:
            return "refused"
        if error.errno == errno.ETIMEDOUT:
            return "timeout"
        if error.errno in {errno.ENETUNREACH, errno.EHOSTUNREACH}:
            return "no_route"
        if error.errno in {errno.EACCES, errno.EPERM}:
            return "permission_denied"
        if error.errno in {errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT, errno.ENOSYS, errno.EOPNOTSUPP}:
            return "unavailable"
    return "error"


def _observed_endpoint(sock: DiagnosticSocket, family: int, *, peer: bool) -> SocketEndpoint | None:
    try:
        value = sock.getpeername() if peer else sock.getsockname()
        expected = 2 if family == socket.AF_INET else 4
        if type(value) is not tuple or len(value) != expected:
            return None
        address, port = value[:2]
        if type(address) is not str or "%" in address or type(port) is not int or not 1 <= port <= 65535:
            return None
        ip = ipaddress.ip_address(address)
        if ip.version != (4 if family == socket.AF_INET else 6) or ip.is_unspecified:
            return None
        if expected == 4 and (type(value[2]) is not int or type(value[3]) is not int or value[3] != 0):
            return None
        return SocketEndpoint(str(ip), port)
    except Exception:
        return None


class DiagnosticExecutor:
    """Internal typed calls; malformed argument types raise a sanitized ValueError.

    The outer wire rejects unknown fields and malformed request types. No policy
    file, host observation, resolver server or interface selector is read here.
    """

    def __init__(
        self,
        loaded_policy: LoadedDiagnosticPolicy,
        limiter: DiagnosticLimiter,
        read_exclusions: Callable[[], CurrentExclusions | None],
        resolve: Callable[[str, RecordType, float], DnsAnswer] | None,
        *,
        vantage: str,
        socket_factory: Callable[[int, int], DiagnosticSocket] = socket.socket,
        monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        if not isinstance(loaded_policy, LoadedDiagnosticPolicy):
            raise ValueError("invalid loaded diagnostic policy")
        if type(vantage) is not str or not vantage or len(vantage) > 256:
            raise ValueError("invalid diagnostic vantage")
        self._policy = loaded_policy
        self._limiter = limiter
        self._read_exclusions = read_exclusions
        self._resolve = resolve
        self._vantage = vantage
        self._socket_factory = socket_factory
        self._monotonic = monotonic
        self._utc_now = utc_now

    def _policy_refusal(self, digest: str) -> tuple[Literal["denied", "policy_mismatch"], str] | None:
        if self._policy.status != "valid" or self._policy.policy is None:
            return "denied", "Active diagnostic policy is missing, unsafe, invalid or unreadable."
        if digest != self._policy.policy.digest:
            return "policy_mismatch", "Loaded diagnostic policy identity does not match the request."
        return None

    def _deadline(self, absolute_monotonic_deadline: float | None) -> float:
        """Internal same-clock ceiling; never a request-supplied extension."""
        if absolute_monotonic_deadline is not None:
            try:
                valid = type(absolute_monotonic_deadline) in (int, float) and math.isfinite(absolute_monotonic_deadline)
            except OverflowError:
                valid = False
            if not valid:
                raise ValueError("invalid internal diagnostic deadline")
        deadline = self._monotonic() + 5
        return deadline if absolute_monotonic_deadline is None else min(deadline, absolute_monotonic_deadline)

    def _tcp_permitted(self, digest: str, address: str, port: int) -> bool:
        try:
            current = self._read_exclusions()
            if type(current) is not CurrentExclusions:
                return False
            sets = (current.local_addresses, current.broadcast_addresses, current.management_addresses)
            if any(type(values) is not tuple for values in sets):
                return False
            # The callback must provide complete bounded current evidence, not an
            # unbounded iterable or a lazily read host source.
            if sum(len(values) for values in sets) > 4096:
                return False
            if any(type(value) is not str or "%" in value for values in sets for value in values):
                return False
            return tcp_target_permitted(self._policy, digest, address, port,
                local_addresses=current.local_addresses, broadcast_addresses=current.broadcast_addresses,
                management_addresses=current.management_addresses)
        except Exception:
            return False

    def dns(self, name: str, record_type: RecordType, policy_digest: str, *, absolute_monotonic_deadline: float | None = None) -> DnsResult:
        if type(name) is not str or len(name) > 254 or type(record_type) is not str or record_type not in ("A", "AAAA") or type(policy_digest) is not str:
            raise ValueError("invalid DNS diagnostic request")
        deadline = self._deadline(absolute_monotonic_deadline)
        refusal = self._policy_refusal(policy_digest)
        if refusal:
            return DnsResult(name, record_type, *refusal, completion_proven=True)
        try:
            canonical = _name(name)
        except InvalidDiagnosticPolicy:
            return DnsResult(name, record_type, "denied", "DNS name is not an eligible exact target.", completion_proven=True)
        if canonical not in self._policy.policy.dns:
            return DnsResult(canonical, record_type, "denied", "DNS name is not independently authorized by policy.", completion_proven=True)
        if self._resolve is None:
            return DnsResult(canonical, record_type, "unavailable", _DNS_DETAILS["unavailable"], completion_proven=True)
        if self._monotonic() >= deadline:
            return DnsResult(canonical, record_type, "timeout", _DNS_DETAILS["timeout"], completion_proven=True)
        admission = self._limiter.try_acquire()
        if admission:
            return DnsResult(canonical, record_type, admission, "Diagnostic admission refused; no query was started.", completion_proven=True)
        started = None
        answer = None
        outcome: DnsOutcome = "error"
        answers = None
        truncated = None
        resolver_changed = False
        try:
            if self._monotonic() < deadline:
                started = self._utc_now()
                try:
                    answer = self._resolve(canonical, record_type, deadline)
                    if type(answer) is not DnsAnswer or answer.outcome not in _DNS_DETAILS:
                        raise ValueError("invalid normalized resolver outcome")
                    if type(answer.detail) is not str or len(answer.detail) > 512:
                        raise ValueError("unbounded normalized resolver detail")
                    if answer.truncated is not None and type(answer.truncated) is not bool:
                        raise ValueError("invalid normalized truncation")
                    if answer.reason is not None and (type(answer.reason) is not str or answer.reason != "resolver_changed"):
                        raise ValueError("invalid normalized resolver reason")
                    if answer.answers is not None:
                        if type(answer.answers) is not tuple or len(answer.answers) > 32:
                            raise ValueError("unbounded normalized answers")
                        normalized = []
                        for value in answer.answers:
                            if type(value) is not str or len(value) > 45 or "%" in value:
                                raise ValueError("invalid numeric answer")
                            ip = ipaddress.ip_address(value)
                            if ip.version != (4 if record_type == "A" else 6):
                                raise ValueError("answer family mismatch")
                            normalized.append(str(ip))
                        answers = tuple(normalized)
                    if (answer.outcome == "resolved" and not answers) or (answer.outcome != "resolved" and answers):
                        raise ValueError("inconsistent normalized answers")
                    outcome, truncated = answer.outcome, answer.truncated
                    if answer.reason == "resolver_changed":
                        outcome, answers, truncated = "unavailable", None, None
                        resolver_changed = True
                except TimeoutError:
                    outcome, answers, truncated = "timeout", None, None
                except Exception:
                    outcome, answers, truncated = "error", None, None
            if self._monotonic() >= deadline:
                outcome, answers, truncated = "timeout", None, None
            ended = self._utc_now() if started is not None else None
            # Only normalized outcome/addresses become public facts. Callback detail
            # and exception text may contain private paths or raw provider payloads.
            detail = "resolver_changed: Resolver identity changed during execution; returned answers are not usable evidence." if resolver_changed and outcome == "unavailable" else _DNS_DETAILS[outcome]
            return DnsResult(canonical, record_type, outcome, detail,
                self._vantage if started is not None else None, started, ended, answers, truncated, completion_proven=True)
        finally:
            self._limiter.release()

    def tcp(self, address: str, port: int, policy_digest: str, *, absolute_monotonic_deadline: float | None = None) -> TcpResult:
        if type(address) is not str or len(address) > 45 or type(port) is not int or not 1 <= port <= 65535 or type(policy_digest) is not str:
            raise ValueError("invalid TCP diagnostic request")
        deadline = self._deadline(absolute_monotonic_deadline)
        refusal = self._policy_refusal(policy_digest)
        if refusal:
            return TcpResult(address, port, *refusal, completion_proven=True)
        if type(self._policy.policy.version) is not int or self._policy.policy.version != 2 or self._policy.policy.control_plane_complete is not True:
            return TcpResult(address, port, "unavailable", "TCP diagnostics require a complete administrator-attested version-2 control-plane declaration.", completion_proven=True)
        try:
            target = _target({"address": address, "port": port})
        except InvalidDiagnosticPolicy:
            return TcpResult(address, port, "denied", "TCP address is not an eligible numeric target.", completion_proven=True)
        if target not in self._policy.policy.tcp:
            return TcpResult(target.address, port, "denied", "TCP endpoint is not independently authorized by policy.", completion_proven=True)
        if not self._tcp_permitted(policy_digest, target.address, port):
            return TcpResult(target.address, port, "denied", "Current local, broadcast or management exclusions are unknown, invalid or exclude this target.", completion_proven=True)
        if self._monotonic() >= deadline:
            return TcpResult(target.address, port, "timeout", _TCP_DETAILS["timeout"], completion_proven=True)
        admission = self._limiter.try_acquire()
        if admission:
            return TcpResult(target.address, port, admission, "Diagnostic admission refused; no connection was started.", completion_proven=True)
        sock = None
        started = None
        local = remote = None
        cleanup_ok = True
        outcome: TcpOutcome = "timeout"
        family = socket.AF_INET6 if ":" in target.address else socket.AF_INET
        try:
            if self._monotonic() < deadline:
                started = self._utc_now()
                try:
                    sock = self._socket_factory(family, socket.SOCK_STREAM)
                    remaining = deadline - self._monotonic()
                    if remaining > 0:
                        sock.settimeout(min(3.0, remaining))
                        if self._monotonic() < deadline:
                            endpoint = (target.address, port, 0, 0) if family == socket.AF_INET6 else (target.address, port)
                            if sock.connect(endpoint) is not None:
                                raise ValueError("invalid synchronous socket completion")
                            outcome = "connected"
                except Exception as error:
                    outcome = _socket_outcome(error)
                if sock is not None:
                    local = _observed_endpoint(sock, family, peer=False)
                    if outcome == "connected":
                        remote = _observed_endpoint(sock, family, peer=True)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    # We cannot establish that the descriptor was cleaned up. Fail
                    # closed instead of allowing unlimited replacement attempts.
                    cleanup_ok = False
            if cleanup_ok:
                self._limiter.release()
        if not cleanup_ok:
            outcome = "error"
            detail = "Socket cleanup could not be established; further diagnostics remain blocked."
        else:
            if self._monotonic() >= deadline:
                outcome = "timeout"
            detail = _TCP_DETAILS[outcome]
        return TcpResult(target.address, port, outcome, detail,
            self._vantage if started is not None else None, started,
            self._utc_now() if started is not None else None, local, remote, completion_proven=cleanup_ok)
