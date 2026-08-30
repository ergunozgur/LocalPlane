"""The provider boundary for network observation.

The seam is deliberately one method wide. ``observe_interfaces`` reads; there is no
``apply``, no ``set`` and no ``execute`` on this interface, and a mutating provider will
be a different protocol with its own preview, verification and recovery obligations
rather than an extra method bolted onto this one.

Every field that a source did not supply is ``None``. ``None`` and a zero value are not
interchangeable here: a link with no addresses reports ``addresses=()``, while a link
whose address source could not be consulted reports ``addresses=None``. Downstream code
is entitled to rely on that difference.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, Sequence, runtime_checkable


class Fidelity(StrEnum):
    """How much of an observation's intended evidence was actually collected."""

    COMPLETE = "complete"
    """Every source the provider wanted answered."""

    PARTIAL = "partial"
    """A whole source was unavailable; the fields it would have supplied are ``None``."""

    DEGRADED = "degraded"
    """A source answered, but a field it is required to carry could not be read.

    A field the kernel simply does not know — the speed of a link with no carrier, the
    carrier of a link that is administratively down — is not degradation. That is the
    host answering accurately, and it is recorded in ``gaps`` with the value left
    ``None``.
    """


class SweepStatus(StrEnum):
    """The outcome of one observation sweep, derived from what was collected."""

    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True)
class InterfaceAddress:
    """One address configured on a link."""

    family: str
    address: str
    prefix_length: int
    scope: str | None = None
    dynamic: bool | None = None
    valid_lifetime_s: int | None = None
    preferred_lifetime_s: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "address": self.address,
            "prefix_length": self.prefix_length,
            "scope": self.scope,
            "dynamic": self.dynamic,
            "valid_lifetime_s": self.valid_lifetime_s,
            "preferred_lifetime_s": self.preferred_lifetime_s,
        }


@dataclass(frozen=True)
class InterfaceStatistics:
    """Kernel counters. Cumulative since the link last came up, not a rate."""

    rx_bytes: int | None = None
    tx_bytes: int | None = None
    rx_packets: int | None = None
    tx_packets: int | None = None
    rx_errors: int | None = None
    tx_errors: int | None = None
    rx_dropped: int | None = None
    tx_dropped: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rx_bytes": self.rx_bytes,
            "tx_bytes": self.tx_bytes,
            "rx_packets": self.rx_packets,
            "tx_packets": self.tx_packets,
            "rx_errors": self.rx_errors,
            "tx_errors": self.tx_errors,
            "rx_dropped": self.rx_dropped,
            "tx_dropped": self.tx_dropped,
        }


@dataclass(frozen=True)
class InterfaceFacts:
    """Normalized link facts. Judgement-free: no health, no management state, no intent."""

    name: str
    kind: str
    ifindex: int | None = None
    link_kind: str | None = None
    arphrd_type: int | None = None
    mac_address: str | None = None
    mac_is_permanent: bool | None = None
    mtu: int | None = None
    admin_up: bool | None = None
    operstate: str | None = None
    carrier: bool | None = None
    speed_mbps: int | None = None
    duplex: str | None = None
    is_physical: bool | None = None
    device_path: str | None = None
    master: str | None = None
    carrier_changes: int | None = None
    addresses: tuple[InterfaceAddress, ...] | None = None
    statistics: InterfaceStatistics | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "ifindex": self.ifindex,
            "link_kind": self.link_kind,
            "arphrd_type": self.arphrd_type,
            "mac_address": self.mac_address,
            "mac_is_permanent": self.mac_is_permanent,
            "mtu": self.mtu,
            "admin_up": self.admin_up,
            "operstate": self.operstate,
            "carrier": self.carrier,
            "speed_mbps": self.speed_mbps,
            "duplex": self.duplex,
            "is_physical": self.is_physical,
            "device_path": self.device_path,
            "master": self.master,
            "carrier_changes": self.carrier_changes,
            "addresses": (
                None if self.addresses is None else [a.as_dict() for a in self.addresses]
            ),
            "statistics": None if self.statistics is None else self.statistics.as_dict(),
        }


@dataclass(frozen=True)
class ObservedInterface:
    """One interface as observed: the facts, and the evidence they were derived from."""

    facts: InterfaceFacts
    method: str
    fidelity: Fidelity
    observed_at: str
    evidence: dict[str, Any] = field(default_factory=dict)
    gaps: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "facts": self.facts.as_dict(),
            "observed_at": self.observed_at,
            "method": self.method,
            "fidelity": str(self.fidelity),
            "evidence": self.evidence,
            "gaps": list(self.gaps),
        }


@dataclass(frozen=True)
class ProviderIssue:
    """Something a provider could not do, named precisely enough to act on."""

    source: str
    code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class InterfaceObservationBatch:
    """One sweep. ``status`` is derived in :meth:`derive_status`, never asserted."""

    provider: str
    provider_version: str
    capability: str
    started_at: str
    completed_at: str
    interfaces: tuple[ObservedInterface, ...] = ()
    missing: tuple[str, ...] = ()
    issues: tuple[ProviderIssue, ...] = ()

    @property
    def status(self) -> SweepStatus:
        return self.derive_status()

    def derive_status(self) -> SweepStatus:
        if not self.interfaces and self.issues:
            return SweepStatus.FAILED
        if self.issues or any(i.fidelity is not Fidelity.COMPLETE for i in self.interfaces):
            return SweepStatus.PARTIAL
        return SweepStatus.OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_version": self.provider_version,
            "capability": self.capability,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": str(self.derive_status()),
            "interfaces": [i.as_dict() for i in self.interfaces],
            "missing": list(self.missing),
            "issues": [i.as_dict() for i in self.issues],
        }


@runtime_checkable
class NetworkProvider(Protocol):
    """Read normalized network interface state from one source of host truth."""

    name: str
    version: str

    def observe_interfaces(
        self, names: Sequence[str] | None = None
    ) -> InterfaceObservationBatch:
        """Observe every interface, or only ``names`` when given.

        Names are a filter over what was discovered. A requested name that does not exist
        on the host is reported in ``missing`` — it is never synthesised, and it never
        becomes an argument to anything.
        """


# --------------------------------------------------------------------------------------
# command execution seam
# --------------------------------------------------------------------------------------


class CommandFailure(StrEnum):
    """Why a command produced no output at all.

    Kept apart from the error text because the distinction is load-bearing: a binary that
    is not installed says something true about this host — that system is not here — while
    one that timed out or could not be executed says only that LocalPlane could not look.
    A caller that cannot tell those apart will report an absent daemon as an unreadable
    one, or worse, the other way round.
    """

    NOT_FOUND = "not_found"
    TIMEOUT = "timeout"
    OS_ERROR = "os_error"


@dataclass(frozen=True)
class CommandResult:
    """The full transcript of one command, including the exact argv that ran.

    The argv is recorded because an operator is entitled to see what LocalPlane ran on
    their host. It is *recorded*, not *accepted*: no caller can influence it.
    """

    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    error: str | None = None
    failure: CommandFailure | None = None
    """Set whenever the command did not run at all. ``None`` once it ran, however it exited."""

    @property
    def ok(self) -> bool:
        return self.error is None and self.returncode == 0

    def as_dict(self, include_stdout: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "argv": list(self.argv),
            "returncode": self.returncode,
            "stderr": self.stderr.strip()[:2048] or None,
            "error": self.error,
            "failure": str(self.failure) if self.failure else None,
        }
        if include_stdout:
            payload["stdout"] = self.stdout
        return payload


class CommandRunner(Protocol):
    """Runs a fixed argv. Exists so tests can drive provider failure paths."""

    def run(self, argv: Sequence[str], timeout_s: float = 5.0) -> CommandResult: ...


class SubprocessRunner:
    """The real runner: no shell, fixed argv, bounded time, deterministic locale."""

    _ENV = {"LC_ALL": "C", "LANG": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}

    def run(self, argv: Sequence[str], timeout_s: float = 5.0) -> CommandResult:
        argv = tuple(argv)
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, shell=False by construction
                argv,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                shell=False,
                env=dict(self._ENV),
                check=False,
            )
        except FileNotFoundError as exc:
            return CommandResult(
                argv, None, "", "", f"not found: {exc.filename}", CommandFailure.NOT_FOUND
            )
        except subprocess.TimeoutExpired:
            return CommandResult(
                argv, None, "", "", f"timed out after {timeout_s}s", CommandFailure.TIMEOUT
            )
        except OSError as exc:
            return CommandResult(argv, None, "", "", str(exc), CommandFailure.OS_ERROR)
        return CommandResult(argv, completed.returncode, completed.stdout, completed.stderr)
