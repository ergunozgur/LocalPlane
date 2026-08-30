"""Read the system systemd manager through its official D-Bus API.

This module is the complete transport boundary.  It is deliberately the only production
module that imports Jeepney, and every D-Bus destination, object path, interface, member,
signature and property name is private to it.  Callers can ask four typed questions and
cannot construct a D-Bus call.

The provider is observation-only.  In particular, targeted observation uses ``GetUnit``
and never ``LoadUnit``: looking at an unloaded declaration must not change the manager's
in-memory estate.  Unit object paths returned by systemd are handles used only while the
connection that obtained them is open.  They are evidence, never identity, and are not
accepted by any public method.

The normalised shape is intentionally tolerant of version skew.  A very small core proves
that an object is a systemd unit; every other property is feature-detected.  Missing
properties become ``None`` plus a named gap, while unknown future state/result strings are
preserved as strings for the backend to judge conservatively.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Final, Iterable

# Dependency boundary: no other production module imports Jeepney directly.
from jeepney import DBusAddress, MessageGenerator, new_method_call
from jeepney.bus import get_bus
from jeepney.bus_messages import MatchRule, message_bus
from jeepney.io.blocking import DBusConnection, DBusConnectionBase, Proxy, prep_socket
from jeepney.io.common import MessageFilters
from jeepney.wrappers import DBusErrorResponse, Introspectable, Properties

from localplane.protocol.capabilities import CAPABILITY_SYSTEMD_UNITS_OBSERVE
from localplane.protocol.providers import PROVIDER_SYSTEMD, SOURCE_SYSTEMD_UNITS

SYSTEMD_DESTINATION: Final = "org.freedesktop.systemd1"
SYSTEMD_MANAGER_PATH: Final = "/org/freedesktop/systemd1"
SYSTEMD_MANAGER_INTERFACE: Final = "org.freedesktop.systemd1.Manager"
SYSTEMD_UNIT_INTERFACE: Final = "org.freedesktop.systemd1.Unit"
SYSTEMD_SERVICE_INTERFACE: Final = "org.freedesktop.systemd1.Service"
SYSTEMD_SOCKET_INTERFACE: Final = "org.freedesktop.systemd1.Socket"
SYSTEMD_TIMER_INTERFACE: Final = "org.freedesktop.systemd1.Timer"
SYSTEMD_PATH_INTERFACE: Final = "org.freedesktop.systemd1.Path"
SYSTEMD_MOUNT_INTERFACE: Final = "org.freedesktop.systemd1.Mount"

SUPPORTED_UNIT_SUFFIXES: Final = (
    ".service",
    ".socket",
    ".timer",
    ".target",
    ".path",
    ".mount",
)
UNIT_PATTERNS: Final = tuple(f"*{suffix}" for suffix in SUPPORTED_UNIT_SUFFIXES)

# The measured development host has 303 selected loaded units.  512 leaves useful
# headroom while bounding two GetAll calls per unit.  The full manager list is read before
# this limit is applied, so exact truncation and omitted count are always reported.
MAX_UNITS: Final = 512
DBUS_CALL_TIMEOUT_S: Final = 5.0
INVENTORY_TIMEOUT_S: Final = 30.0

_MANAGER_CORE_PROPERTIES: Final = ("Version", "SystemState")
_MANAGER_OPTIONAL_PROPERTIES: Final = (
    "Features",
    "Virtualization",
    "Architecture",
    "Tainted",
)

METHOD: Final = "systemd_dbus"
INVENTORY_METHOD: Final = "manager_list_units_by_patterns"
TARGETED_METHOD: Final = "manager_get_unit"
SELF_UNIT_METHOD: Final = "manager_get_unit_by_control_group"

_NO_SUCH_UNIT_ERRORS: Final = frozenset(
    {
        "org.freedesktop.systemd1.NoSuchUnit",
    }
)
_UNKNOWN_METHOD_ERRORS: Final = frozenset(
    {
        "org.freedesktop.DBus.Error.UnknownMethod",
        "org.freedesktop.DBus.Error.UnknownInterface",
    }
)
_UINT64_MAX: Final = (1 << 64) - 1
_UNIT_NAME = re.compile(r"^[^/\x00\r\n]{1,255}$")

_TYPE_INTERFACES: Final = {
    "service": SYSTEMD_SERVICE_INTERFACE,
    "socket": SYSTEMD_SOCKET_INTERFACE,
    "timer": SYSTEMD_TIMER_INTERFACE,
    "path": SYSTEMD_PATH_INTERFACE,
    "mount": SYSTEMD_MOUNT_INTERFACE,
}

_RELATIONSHIP_GROUPS: Final = {
    "Requires": "requirement",
    "Wants": "requirement",
    "Requisite": "requirement",
    "BindsTo": "requirement",
    "PartOf": "requirement",
    "Before": "ordering",
    "After": "ordering",
    "Conflicts": "conflict",
    "Triggers": "activation",
    "TriggeredBy": "activation",
    "OnFailure": "outcome",
    "OnSuccess": "outcome",
    "OnFailureOf": "outcome",
    "OnSuccessOf": "outcome",
    # Lifecycle effect evidence.  These are optional across systemd versions and stay
    # feature-detected; absence is a lifecycle-context gap, not an 11A unit-read failure.
    "Upholds": "requirement",
    "UpheldBy": "reverse_requirement",
    "RequiredBy": "reverse_requirement",
    "RequisiteOf": "reverse_requirement",
    "BoundBy": "reverse_requirement",
    "ConsistsOf": "reverse_requirement",
    "PropagatesStopTo": "stop_propagation",
    "StopPropagatedFrom": "stop_propagation",
    "ConflictedBy": "conflict",
}
_LIFECYCLE_ONLY_RELATIONSHIPS: Final = frozenset(
    {
        "Upholds", "UpheldBy", "RequiredBy", "RequisiteOf", "BoundBy", "ConsistsOf",
        "PropagatesStopTo", "StopPropagatedFrom", "ConflictedBy",
    }
)

_UNIT_OPTIONAL_PROPERTIES: Final = (
    "Names",
    "Description",
    "UnitFileState",
    "UnitFilePreset",
    "CanStart",
    "CanStop",
    "CanReload",
    "RefuseManualStart",
    "RefuseManualStop",
    "NeedDaemonReload",
    "FragmentPath",
    "SourcePath",
    "DropInPaths",
    "Transient",
    "Job",
    "InvocationID",
    "StateChangeTimestamp",
    "StateChangeTimestampMonotonic",
    "InactiveExitTimestamp",
    "InactiveExitTimestampMonotonic",
    "ActiveEnterTimestamp",
    "ActiveEnterTimestampMonotonic",
    "ActiveExitTimestamp",
    "ActiveExitTimestampMonotonic",
    "InactiveEnterTimestamp",
    "InactiveEnterTimestampMonotonic",
    "Requires",
    "Wants",
    "Requisite",
    "BindsTo",
    "PartOf",
    "Before",
    "After",
    "Conflicts",
    "Triggers",
    "TriggeredBy",
    "OnFailure",
    "OnSuccess",
    "OnFailureOf",
    "OnSuccessOf",
    "Upholds",
    "UpheldBy",
    "RequiredBy",
    "RequisiteOf",
    "BoundBy",
    "ConsistsOf",
    "PropagatesStopTo",
    "StopPropagatedFrom",
    "ConflictedBy",
    "StopWhenUnneeded",
)

SYSTEMD_LIFECYCLE_ACTIONS: Final = ("start", "stop", "restart")

#: The exact Unit-interface method each closed action calls. The mapping is the whole of the
#: translation from a LocalPlane verb to a D-Bus member: there is no other member reachable
#: from this module, and a caller supplies the action, never the name on the right.
_LIFECYCLE_UNIT_METHOD_FOR: Final[dict[str, str]] = {
    "start": "Start",
    "stop": "Stop",
    "restart": "Restart",
}

#: The job mode every dispatch uses, and it is a **constant**.
#:
#: ``fail`` refuses the request if it would require cancelling a job already queued for this
#: unit. ``replace`` would cancel that other job — somebody else's transaction, decided
#: elsewhere for reasons this process cannot see — and turn a conflict into a silent
#: overwrite. The conservative answer is to refuse and let a person look, which is also what
#: the backend's applicability rules already assume by refusing a unit with a job in flight.
#:
#: It is not a parameter at any layer. A caller who could choose the mode could choose to
#: cancel other people's work.
SYSTEMD_DISPATCH_MODE: Final = "fail"

#: The only unit type the mutating path will act on. Checked from the name before a socket
#: exists, and again against the manager's own ``Id`` after it resolves one.
SYSTEMD_LIFECYCLE_UNIT_TYPE: Final = "service"

#: How long a dispatch waits for its own job to finish before giving up on hearing about it.
#: Giving up is not a failure and is never read as one: it produces ``write_unknown``.
JOB_COMPLETION_TIMEOUT_S: Final = 90.0

#: How many pending signals the job watch will hold. A burst larger than this evicts the
#: oldest, which can only cost this dispatch its own answer — reported as ``write_unknown``.
JOB_SIGNAL_BUFFER: Final = 256

#: The one job result that means the manager carried the transaction out.
JOB_RESULT_DONE: Final = "done"

#: D-Bus error names that mean systemd refused before any job existed. They are not the whole
#: set and do not need to be: *every* error reply to the dispatch call means no job was
#: enqueued, and these are named only so the reason recorded is specific where it can be.
_AUTHORIZATION_ERRORS: Final = frozenset(
    {
        "org.freedesktop.DBus.Error.AccessDenied",
        "org.freedesktop.DBus.Error.InteractiveAuthorizationRequired",
        "org.freedesktop.DBus.Error.AuthFailed",
    }
)
EFFECT_GRAPH_MAX_NODES: Final = 128
EFFECT_GRAPH_MAX_DEPTH: Final = 12
EFFECT_GRAPH_MAX_EDGES: Final = 2048
EFFECT_GRAPH_TIMEOUT_S: Final = 10.0
_LIFECYCLE_MANAGER_METHODS: Final = frozenset({"Subscribe", "GetUnit"})
_LIFECYCLE_UNIT_METHODS: Final = frozenset({"Start", "Stop", "Restart"})
_LIFECYCLE_MANAGER_SIGNALS: Final = frozenset({"JobRemoved"})
_PROVIDER_DBUS_NAMES: Final = {
    "networkmanager": "org.freedesktop.NetworkManager",
}
_REACTIVATION_RELATIONSHIPS: Final = frozenset({"TriggeredBy", "UpheldBy"})
_REACTIVATION_STABLE_NEGATIVE_STATES: Final = frozenset({"inactive", "failed"})
_REACTIVATION_CHANGING_STATES: Final = frozenset(
    {"activating", "deactivating", "reloading", "refreshing", "maintenance"}
)

_START_EFFECT_RELATIONSHIPS: Final = frozenset(
    {
        "Requires", "Wants", "BindsTo", "Upholds", "Triggers", "TriggeredBy",
        "Conflicts", "ConflictedBy", "OnFailure", "OnSuccess", "OnFailureOf",
        "OnSuccessOf",
    }
)
_STOP_EFFECT_RELATIONSHIPS: Final = frozenset(
    {
        "UpheldBy", "RequiredBy", "RequisiteOf", "BoundBy", "ConsistsOf", "PropagatesStopTo",
        "StopPropagatedFrom", "Triggers", "TriggeredBy", "Conflicts", "ConflictedBy",
        "OnFailure", "OnSuccess", "OnFailureOf", "OnSuccessOf",
    }
)

_SERVICE_PROPERTIES: Final = (
    "Type",
    "MainPID",
    "ControlPID",
    "ExecMainPID",
    "Result",
    "ExecMainCode",
    "ExecMainStatus",
    "Restart",
    "RestartUSec",
    "NRestarts",
    "RemainAfterExit",
    "GuessMainPID",
    "ExecMainStartTimestampMonotonic",
    "WatchdogUSec",
    "WatchdogTimestampMonotonic",
    "WatchdogSignal",
    "WatchdogPID",
    "ControlGroup",
)
_SOCKET_PROPERTIES: Final = (
    "Listen",
    "Accept",
    "NAccepted",
    "NConnections",
    "NRefused",
    "Result",
    "TriggerLimitIntervalUSec",
    "TriggerLimitBurst",
)
_TIMER_PROPERTIES: Final = (
    "Unit",
    "NextElapseUSecRealtime",
    "NextElapseUSecMonotonic",
    "LastTriggerUSec",
    "LastTriggerUSecMonotonic",
    "Persistent",
    "RandomizedDelayUSec",
    "FixedRandomDelay",
    "AccuracyUSec",
    "Result",
)
_PATH_PROPERTIES: Final = (
    "Unit",
    "Paths",
    "MakeDirectory",
    "DirectoryMode",
    "Result",
)
_MOUNT_PROPERTIES: Final = (
    "What",
    "Where",
    "Type",
    "ControlPID",
    "DirectoryMode",
    "Result",
    "SloppyOptions",
    "LazyUnmount",
    "ForceUnmount",
    "TimeoutUSec",
)


class SystemdInvalidUnitName(ValueError):
    """A targeted unit identifier outside this provider's closed six-type scope."""


class _LoadedUnitAbsent(LookupError):
    """Manager.GetUnit's exact NoSuchUnit answer, distinct from every read failure."""


class _InventoryDeadline(TimeoutError):
    """The provider's overall loaded-unit read budget has expired."""


@dataclass(frozen=True)
class SystemdFailure(Exception):
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ManagerObservation:
    status: str
    observed_at: str
    version: str | None = None
    facts: dict[str, Any] = field(default_factory=dict)
    gaps: tuple[str, ...] = ()
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "observed_at": self.observed_at,
            "version": self.version,
            "facts": self.facts,
            "gaps": list(self.gaps),
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class UnitObservation:
    canonical_id: str
    facts: dict[str, Any]
    observed_at: str
    gaps: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def fidelity(self) -> str:
        return "complete" if not self.gaps else "partial"

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": METHOD,
            "fidelity": self.fidelity,
            "observed_at": self.observed_at,
            "gaps": list(self.gaps),
            "facts": self.facts,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class UnitBatch:
    status: str
    started_at: str
    completed_at: str
    provider_version: str | None = None
    units: tuple[UnitObservation, ...] = ()
    listed_count: int | None = None
    selected_count: int = 0
    inventory_limit: int = MAX_UNITS
    inventory_complete: bool = False
    truncated: bool = False
    cap_reached: bool = False
    inventory_method: str | None = None
    issues: tuple[dict[str, Any], ...] = ()
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability": CAPABILITY_SYSTEMD_UNITS_OBSERVE,
            "provider": PROVIDER_SYSTEMD,
            "provider_version": self.provider_version or "unknown",
            "source": SOURCE_SYSTEMD_UNITS,
            "method": METHOD,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "reason": self.reason,
            "units": [unit.as_dict() for unit in self.units],
            "listed_count": self.listed_count,
            "selected_count": self.selected_count,
            "observed_count": len(self.units),
            "inventory_limit": self.inventory_limit,
            "inventory_complete": self.inventory_complete,
            "truncated": self.truncated,
            "cap_reached": self.cap_reached,
            "inventory_method": self.inventory_method,
            "issues": [dict(issue) for issue in self.issues],
            "detail": self.detail,
        }


@dataclass(frozen=True)
class TargetedUnitObservation:
    status: str
    requested_unit: str
    started_at: str
    completed_at: str
    provider_version: str | None = None
    unit: UnitObservation | None = None
    reason: str | None = None
    issues: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability": CAPABILITY_SYSTEMD_UNITS_OBSERVE,
            "provider": PROVIDER_SYSTEMD,
            "provider_version": self.provider_version or "unknown",
            "source": SOURCE_SYSTEMD_UNITS,
            "method": TARGETED_METHOD,
            "scope": "targeted",
            "status": self.status,
            "requested_unit": self.requested_unit,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "reason": self.reason,
            "unit": self.unit.as_dict() if self.unit else None,
            "issues": [dict(issue) for issue in self.issues],
        }


@dataclass(frozen=True)
class AgentUnitResolution:
    status: str
    observed_at: str
    method: str = SELF_UNIT_METHOD
    cgroup: str | None = None
    canonical_id: str | None = None
    invocation_id: str | None = None
    unit: UnitObservation | None = None
    gaps: tuple[str, ...] = ()
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": PROVIDER_SYSTEMD,
            "status": self.status,
            "method": self.method,
            "observed_at": self.observed_at,
            "cgroup": self.cgroup,
            "canonical_id": self.canonical_id,
            "invocation_id": self.invocation_id,
            "unit": self.unit.as_dict() if self.unit else None,
            "gaps": list(self.gaps),
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ProcessUnitResolution:
    """systemd's containing-unit answer for one caller-held process pidfd."""

    status: str
    observed_at: str
    method: str = "manager_get_unit_by_pidfd"
    canonical_id: str | None = None
    invocation_id: str | None = None
    unit: UnitObservation | None = None
    gaps: tuple[str, ...] = ()
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": PROVIDER_SYSTEMD,
            "status": self.status,
            "method": self.method,
            "observed_at": self.observed_at,
            "canonical_id": self.canonical_id,
            "invocation_id": self.invocation_id,
            "unit": self.unit.as_dict() if self.unit else None,
            "gaps": list(self.gaps),
            "reason": self.reason,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class PidfdUnitContractObservation:
    status: str
    observed_at: str
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "observed_at": self.observed_at,
            "reason": self.reason,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class LifecycleContractObservation:
    """Read-only introspection of the mechanisms Part B would require."""

    status: str
    observed_at: str
    manager_methods: tuple[str, ...] = ()
    unit_methods: tuple[str, ...] = ()
    manager_signals: tuple[str, ...] = ()
    introspection_unit: str | None = None
    missing: tuple[str, ...] = ()
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "observed_at": self.observed_at,
            "manager_methods": list(self.manager_methods),
            "unit_methods": list(self.unit_methods),
            "manager_signals": list(self.manager_signals),
            "introspection_unit": self.introspection_unit,
            "missing": list(self.missing),
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class LifecycleDispatchResult:
    """What became of one dispatched systemd job. Three outcomes, never interchangeable.

    A *report*, not a verdict. It says what the manager was asked, what it answered and what
    its own job record said — never whether the service is now in the state somebody wanted,
    which is a separate question answered by a separate reading through the ordinary
    observation path.

    **No transport handle appears here.** The unit object path and the job object path are
    both resolved inside one connection and are meaningless outside it; the numeric job id
    and the canonical unit name are what a later reader can actually use, and are what is
    kept.
    """

    outcome: str
    """``not_written``, ``written`` or ``write_unknown`` — the same three words the backend's
    write boundary already speaks, decided here by what systemd said rather than inferred
    there from an exception."""

    reason: str
    action: str
    unit_id: str
    job_id: int | None = None
    job_result: str | None = None
    invocation_id: str | None = None
    """The unit's execution generation as it stood *at dispatch*. Evidence a later
    verification of a restart needs, because a restart cannot be proven from the resulting
    state alone — a service that was running before and is running now has shown nothing."""

    active_state: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "action": self.action,
            "unit_id": self.unit_id,
            "job_id": self.job_id,
            "job_result": self.job_result,
            "invocation_id": self.invocation_id,
            "active_state": self.active_state,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class EffectGraphObservation:
    """Bounded potential-effect evidence for one canonical service and closed action."""

    status: str
    observed_at: str
    requested_unit: str
    action: str
    provider_version: str | None = None
    canonical_target: str | None = None
    target: UnitObservation | None = None
    units: tuple[str, ...] = ()
    edges: tuple[dict[str, str], ...] = ()
    active_activation_sources: tuple[str, ...] = ()
    active_upholding_sources: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return self.status == "complete" and not self.gaps

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "observed_at": self.observed_at,
            "requested_unit": self.requested_unit,
            "action": self.action,
            "provider_version": self.provider_version,
            "canonical_target": self.canonical_target,
            "target": self.target.as_dict() if self.target else None,
            "units": list(self.units),
            "edges": [dict(edge) for edge in self.edges],
            "active_activation_sources": list(self.active_activation_sources),
            "active_upholding_sources": list(self.active_upholding_sources),
            "gaps": list(self.gaps),
            "reason": self.reason,
            "detail": self.detail,
        }


class _UnitMessages(MessageGenerator):
    """The three lifecycle members, on one manager-returned Unit object, and nothing else.

    Deliberately a separate generator from the manager's. It is constructed with an object
    path the provider resolved itself through ``Manager.GetUnit``, it exposes exactly the
    three members the closed action vocabulary maps onto, and the mode it passes is the
    module constant. There is no method here that takes a member name.
    """

    interface = SYSTEMD_UNIT_INTERFACE

    def start(self) -> Any:
        return new_method_call(self, "Start", "s", (SYSTEMD_DISPATCH_MODE,))

    def stop(self) -> Any:
        return new_method_call(self, "Stop", "s", (SYSTEMD_DISPATCH_MODE,))

    def restart(self) -> Any:
        return new_method_call(self, "Restart", "s", (SYSTEMD_DISPATCH_MODE,))


class _ManagerMessages(MessageGenerator):
    interface = SYSTEMD_MANAGER_INTERFACE

    def subscribe(self) -> Any:
        return new_method_call(self, "Subscribe")

    def list_units_by_patterns(self) -> Any:
        return new_method_call(
            self, "ListUnitsByPatterns", "asas", ([], list(UNIT_PATTERNS))
        )

    def list_units(self) -> Any:
        return new_method_call(self, "ListUnits")

    def get_unit(self, unit_id: str) -> Any:
        return new_method_call(self, "GetUnit", "s", (unit_id,))

    def get_unit_by_control_group(self, control_group: str) -> Any:
        return new_method_call(
            self, "GetUnitByControlGroup", "s", (control_group,)
        )

    def get_unit_by_pid(self, pid: int) -> Any:
        return new_method_call(self, "GetUnitByPID", "u", (pid,))

    def get_unit_by_pidfd(self, pidfd: int) -> Any:
        return new_method_call(self, "GetUnitByPIDFD", "h", (pidfd,))


class _DeadlineDBusConnection(DBusConnection):
    """Jeepney blocking connection whose initial Hello has an explicit timeout.

    Jeepney 0.9's public ``open_dbus_connection`` bounds authentication but constructs a
    blocking connection whose D-Bus Hello has no timeout.  The provider advertises an
    overall inventory deadline, so leaving that one call unbounded would make the claim
    false.  This is the same Jeepney connection setup with only that timeout supplied;
    no message vocabulary escapes this module.
    """

    def __init__(
        self, sock: Any, hello_timeout_s: float, *, enable_fds: bool = False
    ) -> None:
        DBusConnectionBase.__init__(self, sock, enable_fds=enable_fds)
        self._filters = MessageFilters()
        self.bus_proxy = Proxy(message_bus, self, timeout=hello_timeout_s)
        try:
            self.unique_name = self.bus_proxy.Hello()[0]
        except Exception:
            self.close()
            raise


def _open_bounded_system_bus(
    timeout_s: float, *, enable_fds: bool = False
) -> DBusConnection:
    """Bound authentication and Hello together by one provider transport budget."""
    deadline = time.monotonic() + timeout_s
    sock = prep_socket(get_bus("SYSTEM"), enable_fds=enable_fds, timeout=timeout_s)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        sock.close()
        raise TimeoutError("system bus connection deadline expired")
    return _DeadlineDBusConnection(sock, remaining, enable_fds=enable_fds)


class _JobWatch:
    """One dispatch's view of ``JobRemoved``, correlated on both identifying fields.

    A job path alone is not enough and neither is a unit name alone: paths are recycled by
    the manager and a unit can have several jobs over one window. Both must match, and a
    signal that fails either is not this dispatch's answer — it is skipped and the wait
    continues, because it belongs to somebody else.
    """

    def __init__(self, connection: Any, queue: Any) -> None:
        self._connection = connection
        self._queue = queue

    def result_for(
        self, job_path: str, unit_id: str, timeout_s: float
    ) -> tuple[int, str] | None:
        """The job id and result string for this exact job, or ``None`` on the deadline.

        ``None`` says nothing about what happened to the host: the job may have completed
        perfectly while this process was not listening. It is the caller's job to report
        that as unknown rather than as failure.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                message = self._connection.recv_until_filtered(
                    self._queue, timeout=remaining
                )
            except TimeoutError:
                return None
            body = getattr(message, "body", ())
            if len(body) != 4:
                continue
            job_id, path, unit, result = body
            if str(path) == job_path and str(unit) == unit_id:
                return int(job_id), str(result)


class _JeepneySystemdConnection:
    """Provider-private Jeepney adapter.  It exposes no general call primitive."""

    def __init__(self, timeout_s: float, *, enable_fds: bool = False) -> None:
        self._timeout_s = timeout_s
        self._enable_fds = enable_fds
        self._connection: Any | None = None
        self._manager = _ManagerMessages(SYSTEMD_MANAGER_PATH, SYSTEMD_DESTINATION)

    def __enter__(self) -> "_JeepneySystemdConnection":
        self._connection = _open_bounded_system_bus(
            self._timeout_s, enable_fds=self._enable_fds
        )
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def manager_properties(self, timeout_s: float | None = None) -> dict[str, Any]:
        return self._properties(
            SYSTEMD_MANAGER_PATH, SYSTEMD_MANAGER_INTERFACE, timeout_s=timeout_s
        )

    def manager_introspection(self, timeout_s: float | None = None) -> str:
        reply = self._proxy(
            Introspectable(SYSTEMD_MANAGER_PATH, SYSTEMD_DESTINATION)
        ).Introspect(
            _timeout=self._timeout_s if timeout_s is None else timeout_s
        )
        return str(reply[0])

    def unit_introspection(
        self, object_path: str, timeout_s: float | None = None
    ) -> str:
        """Introspect one manager-returned, already-loaded Unit transport handle."""
        reply = self._proxy(
            Introspectable(object_path, SYSTEMD_DESTINATION)
        ).Introspect(
            _timeout=self._timeout_s if timeout_s is None else timeout_s
        )
        return str(reply[0])

    def list_loaded_units(
        self, timeout_s: float | None = None
    ) -> tuple[list[tuple[Any, ...]], str]:
        budget = self._timeout_s if timeout_s is None else timeout_s
        started = time.monotonic()
        proxy = self._proxy(self._manager)
        try:
            rows = proxy.list_units_by_patterns(_timeout=budget)[0]
            return list(rows), INVENTORY_METHOD
        except DBusErrorResponse as exc:
            if exc.name not in _UNKNOWN_METHOD_ERRORS:
                raise
            # ListUnits is part of the oldest practical systemd D-Bus contract.  Filtering
            # locally preserves support for managers predating ListUnitsByPatterns without
            # branching on a distribution or guessed version.
            remaining = budget - (time.monotonic() - started)
            if remaining <= 0:
                raise _InventoryDeadline("loaded-unit list deadline expired")
            rows = proxy.list_units(_timeout=remaining)[0]
            return list(rows), "manager_list_units_filtered"

    def get_unit_path(self, unit_id: str, timeout_s: float | None = None) -> str:
        try:
            return str(
                self._proxy(self._manager).get_unit(
                    unit_id,
                    _timeout=self._timeout_s if timeout_s is None else timeout_s,
                )[0]
            )
        except DBusErrorResponse as exc:
            if exc.name in _NO_SUCH_UNIT_ERRORS:
                raise _LoadedUnitAbsent(unit_id) from exc
            raise

    def get_unit_by_control_group(
        self, control_group: str, timeout_s: float | None = None
    ) -> str:
        return str(
            self._proxy(self._manager).get_unit_by_control_group(
                control_group,
                _timeout=self._timeout_s if timeout_s is None else timeout_s,
            )[0]
        )

    def get_unit_by_pid(self, pid: int, timeout_s: float | None = None) -> str:
        return str(
            self._proxy(self._manager).get_unit_by_pid(
                pid, _timeout=self._timeout_s if timeout_s is None else timeout_s
            )[0]
        )

    def get_unit_by_pidfd(
        self, pidfd: int, timeout_s: float | None = None
    ) -> tuple[str, str, Any]:
        reply = self._proxy(self._manager).get_unit_by_pidfd(
            pidfd, _timeout=self._timeout_s if timeout_s is None else timeout_s
        )
        return str(reply[0]), str(reply[1]), reply[2]

    def dbus_name_owner_pid(
        self, bus_name: str, timeout_s: float | None = None
    ) -> tuple[str, int]:
        """Resolve one provider-owned, provider-private well-known bus name passively."""
        budget = self._timeout_s if timeout_s is None else timeout_s
        proxy = self._proxy(message_bus)
        owner = str(proxy.GetNameOwner(bus_name, _timeout=budget)[0])
        pid = int(proxy.GetConnectionUnixProcessID(owner, _timeout=budget)[0])
        return owner, pid

    def subscribe(self, timeout_s: float | None = None) -> None:
        """Ask the manager to emit job signals to this connection. Changes no unit.

        Called *before* anything is dispatched. systemd emits ``JobRemoved`` to subscribed
        peers, so subscribing after the call would be a race whose loser reports
        ``write_unknown`` for a job that finished perfectly well.
        """
        self._proxy(self._manager).subscribe(
            _timeout=self._timeout_s if timeout_s is None else timeout_s
        )

    @contextmanager
    def job_watch(self) -> Any:
        """Match ``Manager.JobRemoved`` for the life of one dispatch, then stop matching.

        The match is established before the mutating call is sent, for the same reason the
        subscription is. What comes out is a watch that can be asked about *one* job, by the
        path the manager returned and the unit name that was asked for — never "the next
        signal", which on a busy manager is somebody else's.
        """
        if self._connection is None:
            raise RuntimeError("systemd D-Bus connection is not open")
        rule = MatchRule(
            type="signal",
            sender=SYSTEMD_DESTINATION,
            interface=SYSTEMD_MANAGER_INTERFACE,
            member="JobRemoved",
            path=SYSTEMD_MANAGER_PATH,
        )
        self._proxy(message_bus).AddMatch(rule, _timeout=self._timeout_s)
        with self._connection.filter(rule, bufsize=JOB_SIGNAL_BUFFER) as queue:
            yield _JobWatch(self._connection, queue)

    def unit_lifecycle(
        self, object_path: str, action: str, timeout_s: float | None = None
    ) -> str:
        """Call one of three members on one Unit object. Returns the job's object path.

        The member comes from the closed action map and the mode from the module constant;
        neither is an argument to this method and neither can be reached from outside it.
        A job path coming back means the manager *enqueued* a job — not that anything has
        happened yet, which is what the caller's wait is for.
        """
        member = _LIFECYCLE_UNIT_METHOD_FOR[action]
        messages = _UnitMessages(object_path, SYSTEMD_DESTINATION)
        call = getattr(self._proxy(messages), member.lower())
        return str(call(_timeout=self._timeout_s if timeout_s is None else timeout_s)[0])

    def unit_properties(
        self, object_path: str, timeout_s: float | None = None
    ) -> dict[str, Any]:
        return self._properties(object_path, SYSTEMD_UNIT_INTERFACE, timeout_s=timeout_s)

    def type_properties(
        self, object_path: str, unit_type: str, timeout_s: float | None = None
    ) -> dict[str, Any]:
        interface = _TYPE_INTERFACES[unit_type]
        return self._properties(object_path, interface, timeout_s=timeout_s)

    def _properties(
        self, object_path: str, interface: str, *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        address = DBusAddress(
            object_path, bus_name=SYSTEMD_DESTINATION, interface=interface
        )
        reply = self._proxy(Properties(address)).get_all(
            _timeout=self._timeout_s if timeout_s is None else timeout_s
        )
        raw = reply[0]
        if not isinstance(raw, dict):
            raise SystemdFailure(
                "unexpected_property_shape",
                {"interface": interface, "received": type(raw).__name__},
            )
        return {str(name): _unwrap_variant(value) for name, value in raw.items()}

    def _proxy(self, messages: Any) -> Proxy:
        if self._connection is None:
            raise RuntimeError("systemd D-Bus connection is not open")
        return Proxy(messages, self._connection, timeout=self._timeout_s)


class SystemdProvider:
    """Four closed, read-only questions for the system systemd manager."""

    provider = PROVIDER_SYSTEMD
    source = SOURCE_SYSTEMD_UNITS

    def __init__(
        self,
        *,
        runtime_path: str | Path | None = "/run/systemd/system",
        proc_cgroup: str | Path = "/proc/self/cgroup",
        timeout_s: float = DBUS_CALL_TIMEOUT_S,
        inventory_timeout_s: float = INVENTORY_TIMEOUT_S,
        max_units: int = MAX_UNITS,
        connection_factory: Callable[[], Any] | None = None,
        pidfd_connection_factory: Callable[[], Any] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runtime_path = Path(runtime_path) if runtime_path is not None else None
        self._proc_cgroup = Path(proc_cgroup)
        self._timeout_s = timeout_s
        self._inventory_timeout_s = inventory_timeout_s
        self._max_units = max_units
        self._connection_factory = connection_factory or (
            lambda: _JeepneySystemdConnection(
                min(self._timeout_s, self._inventory_timeout_s)
            )
        )
        if pidfd_connection_factory is not None:
            self._pidfd_connection_factory = pidfd_connection_factory
        elif connection_factory is not None:
            # An injected provider test seam may implement both closed reads.
            self._pidfd_connection_factory = connection_factory
        else:
            self._pidfd_connection_factory = lambda: _JeepneySystemdConnection(
                self._timeout_s, enable_fds=True
            )
        self._monotonic = monotonic

    # ---------------------------------------------------------------- manager/capability

    def read_manager(self) -> ManagerObservation:
        observed_at = _now()
        unavailable = self._environment_unavailable(observed_at)
        if unavailable is not None:
            return unavailable
        try:
            with self._connection_factory() as connection:
                properties = connection.manager_properties()
        except Exception as exc:  # Every transport failure is data at this boundary.
            return ManagerObservation(
                status="unavailable",
                observed_at=observed_at,
                reason="system_manager_unavailable",
                detail=_failure_detail(exc),
            )
        return _normalise_manager(properties, observed_at)

    def read_pidfd_unit_contract(self) -> PidfdUnitContractObservation:
        """Passively feature-detect the exact read-only ``GetUnitByPIDFD`` method."""
        observed_at = _now()
        unavailable = self._environment_unavailable(observed_at)
        if unavailable is not None:
            return PidfdUnitContractObservation(
                status="unavailable",
                observed_at=observed_at,
                reason=unavailable.reason,
                detail=unavailable.detail,
            )
        try:
            with self._connection_factory() as connection:
                root = ET.fromstring(connection.manager_introspection())
        except Exception as exc:
            return PidfdUnitContractObservation(
                status="unavailable",
                observed_at=observed_at,
                reason="systemd_pidfd_introspection_failed",
                detail=_failure_detail(exc),
            )
        manager = next(
            (
                node
                for node in root.findall("interface")
                if node.get("name") == SYSTEMD_MANAGER_INTERFACE
            ),
            None,
        )
        method = (
            next(
                (
                    node
                    for node in manager.findall("method")
                    if node.get("name") == "GetUnitByPIDFD"
                ),
                None,
            )
            if manager is not None
            else None
        )
        expected = (("h", "in"), ("o", "out"), ("s", "out"), ("ay", "out"))
        if method is None or _introspection_arguments(method, method=True) != expected:
            return PidfdUnitContractObservation(
                status="unavailable",
                observed_at=observed_at,
                reason="systemd_get_unit_by_pidfd_unsupported",
                detail={
                    "manager_scope": "system",
                    "method": "GetUnitByPIDFD",
                    "expected_signature": [list(item) for item in expected],
                    "mutation_invoked": False,
                },
            )
        return PidfdUnitContractObservation(
            status="available",
            observed_at=observed_at,
            detail={
                "manager_scope": "system",
                "method": "GetUnitByPIDFD",
                "signature": "h->osay",
                "unix_fd_transport_required": True,
                "mutation_invoked": False,
            },
        )

    def read_lifecycle_contract(self) -> LifecycleContractObservation:
        """Feature-detect Part B's loaded-Unit contract without invoking lifecycle.

        The probe lists the already-loaded estate, passively resolves one listed service
        with ``Manager.GetUnit``, and introspects that returned Unit object.  It never uses
        ``LoadUnit`` and never treats the similarly named Manager lifecycle methods as
        evidence for the Unit-object dispatch contract.
        """
        observed_at = _now()
        unavailable = self._environment_unavailable(observed_at)
        if unavailable is not None:
            return LifecycleContractObservation(
                status="unavailable",
                observed_at=observed_at,
                reason=unavailable.reason,
                detail=unavailable.detail,
            )
        try:
            with self._connection_factory() as connection:
                manager_xml = connection.manager_introspection()
                rows, inventory_method = connection.list_loaded_units()
                loaded_services = sorted(
                    str(row[0])
                    for row in rows
                    if len(row) > 2
                    and str(row[0]).endswith(".service")
                    and str(row[2]) == "loaded"
                )
                if not loaded_services:
                    return LifecycleContractObservation(
                        status="unavailable",
                        observed_at=observed_at,
                        reason="loaded_service_for_unit_introspection_missing",
                        detail={
                            "manager_scope": "system",
                            "transport": "system_bus_dbus",
                            "probe": "dbus_introspection",
                            "inventory_method": inventory_method,
                            "loaded_unit_lookup": "Manager.GetUnit",
                            "mutation_invoked": False,
                        },
                    )
                listed_unit = loaded_services[0]
                object_path = connection.get_unit_path(listed_unit)
                properties = connection.unit_properties(object_path)
                introspection_unit = _string_or_none(properties.get("Id"))
                if (
                    introspection_unit is None
                    or not introspection_unit.endswith(".service")
                    or properties.get("LoadState") != "loaded"
                ):
                    return LifecycleContractObservation(
                        status="unavailable",
                        observed_at=observed_at,
                        introspection_unit=introspection_unit,
                        reason="loaded_service_introspection_identity_invalid",
                        detail={
                            "listed_unit": listed_unit,
                            "load_state": properties.get("LoadState"),
                            "transport_handle_persisted": False,
                            "mutation_invoked": False,
                        },
                    )
                unit_xml = connection.unit_introspection(object_path)
            manager_root = ET.fromstring(manager_xml)
            unit_root = ET.fromstring(unit_xml)
        except Exception as exc:
            return LifecycleContractObservation(
                status="unavailable",
                observed_at=observed_at,
                reason="systemd_lifecycle_introspection_failed",
                detail=_failure_detail(exc),
            )
        manager = next(
            (
                node
                for node in manager_root.findall("interface")
                if node.get("name") == SYSTEMD_MANAGER_INTERFACE
            ),
            None,
        )
        if manager is None:
            return LifecycleContractObservation(
                status="unavailable",
                observed_at=observed_at,
                reason="systemd_manager_interface_missing",
            )
        unit_interface = next(
            (
                node
                for node in unit_root.findall("interface")
                if node.get("name") == SYSTEMD_UNIT_INTERFACE
            ),
            None,
        )
        if unit_interface is None:
            return LifecycleContractObservation(
                status="unavailable",
                observed_at=observed_at,
                introspection_unit=introspection_unit,
                reason="systemd_unit_interface_missing",
            )
        manager_method_nodes = {
            name: node
            for node in manager.findall("method")
            if isinstance((name := node.get("name")), str)
        }
        unit_method_nodes = {
            name: node
            for node in unit_interface.findall("method")
            if isinstance((name := node.get("name")), str)
        }
        manager_signal_nodes = {
            name: node
            for node in manager.findall("signal")
            if isinstance((name := node.get("name")), str)
        }
        manager_methods = tuple(
            sorted(_LIFECYCLE_MANAGER_METHODS.intersection(manager_method_nodes))
        )
        unit_methods = tuple(
            sorted(_LIFECYCLE_UNIT_METHODS.intersection(unit_method_nodes))
        )
        manager_signals = tuple(
            sorted(_LIFECYCLE_MANAGER_SIGNALS.intersection(manager_signal_nodes))
        )
        expected_manager_methods = {
            "Subscribe": (),
            "GetUnit": (("s", "in"), ("o", "out")),
        }
        expected_unit_methods = {
            "Start": (("s", "in"), ("o", "out")),
            "Stop": (("s", "in"), ("o", "out")),
            "Restart": (("s", "in"), ("o", "out")),
        }
        expected_signals = {
            "JobRemoved": (("u", None), ("o", None), ("s", None), ("s", None)),
        }
        missing_items = {
            f"manager_method:{name}"
            for name in _LIFECYCLE_MANAGER_METHODS - set(manager_method_nodes)
        } | {
            f"unit_method:{name}"
            for name in _LIFECYCLE_UNIT_METHODS - set(unit_method_nodes)
        } | {
            f"manager_signal:{name}"
            for name in _LIFECYCLE_MANAGER_SIGNALS - set(manager_signal_nodes)
        }
        for name, expected in expected_manager_methods.items():
            node = manager_method_nodes.get(name)
            if node is not None and _introspection_arguments(node, method=True) != expected:
                missing_items.add(f"signature:manager_method:{name}")
        for name, expected in expected_unit_methods.items():
            node = unit_method_nodes.get(name)
            if node is not None and _introspection_arguments(node, method=True) != expected:
                missing_items.add(f"signature:unit_method:{name}")
        for name, expected in expected_signals.items():
            node = manager_signal_nodes.get(name)
            if node is not None and _introspection_arguments(node, method=False) != expected:
                missing_items.add(f"signature:manager_signal:{name}")
        missing = tuple(sorted(missing_items))
        return LifecycleContractObservation(
            status="available" if not missing else "unavailable",
            observed_at=observed_at,
            manager_methods=manager_methods,
            unit_methods=unit_methods,
            manager_signals=manager_signals,
            introspection_unit=introspection_unit,
            missing=missing,
            reason=None if not missing else "required_systemd_lifecycle_contract_missing",
            detail={
                "manager_scope": "system",
                "transport": "system_bus_dbus",
                "probe": "dbus_introspection",
                "dispatch_interface": SYSTEMD_UNIT_INTERFACE,
                "loaded_unit_lookup": "Manager.GetUnit",
                "inventory_method": inventory_method,
                "unit_loaded_passively": True,
                "transport_handle_persisted": False,
                "mutation_invoked": False,
            },
        )

    # ----------------------------------------------------------------------- dispatch

    def dispatch_service_lifecycle(
        self, unit_id: str, action: str
    ) -> LifecycleDispatchResult:
        """Ask the manager to run one closed verb against one loaded service. Never raises.

        The provider's only mutating method, and the only one in this module that is not a
        question. Every failure it can have is one of the three outcomes, because a caller
        that had to interpret an exception would be deciding what it meant about the host —
        and that decision is exactly the one that must not be made by guessing.

        **The order is the safety argument.**

        1. the action and the unit name are checked against the closed vocabulary and the
           ``.service`` suffix, before a socket exists;
        2. the bus connection is opened under the provider's own transport budget;
        3. ``Manager.Subscribe`` and the ``JobRemoved`` match are established **first**, so
           the completion signal cannot be missed by a job that finishes quickly;
        4. ``Manager.GetUnit`` resolves the name to an object path — never ``LoadUnit``,
           which would pull an unloaded declaration into the manager's estate as a side
           effect of asking about it;
        5. that object's own ``Id`` and ``LoadState`` are read back and must be the
           canonical name that was asked for and ``loaded``. An alias resolving to some
           other unit stops here, with nothing dispatched;
        6. exactly one of ``Start``, ``Stop`` or ``Restart`` is called on that object, with
           the module's fixed mode;
        7. the wait is for **this** job — the path the manager just returned *and* the unit
           name that was asked for — bounded, and giving up is unknown rather than failure.

        **Authorization is systemd's decision and it is made here.** Nothing is preflighted:
        an unprivileged caller cannot reproduce the decision systemd makes with its own
        trusted unit and verb details, and a preflight that returned "probably allowed"
        would be a claim LocalPlane had not earned. A refusal arrives as an error reply to
        step 6 and becomes ``not_written``, which is a proof — no job was enqueued.
        """
        observed_at = _now()
        detail: dict[str, Any] = {
            "manager_scope": "system",
            "transport": "system_bus_dbus",
            "dispatch_interface": SYSTEMD_UNIT_INTERFACE,
            "dispatch_member": _LIFECYCLE_UNIT_METHOD_FOR.get(action),
            "mode": SYSTEMD_DISPATCH_MODE,
            "loaded_unit_lookup": "Manager.GetUnit",
            "transport_handle_persisted": False,
            "authorization_preflight": "not_preflighted",
            "observed_at": observed_at,
        }

        if action not in SYSTEMD_LIFECYCLE_ACTIONS:
            return _not_dispatched(
                action, unit_id, "unsupported_lifecycle_action",
                {**detail, "supported": list(SYSTEMD_LIFECYCLE_ACTIONS)})
        try:
            # The lifecycle vocabulary is a *service* vocabulary. What start, stop and
            # restart mean for a socket, a timer, a mount or a slice is a different question,
            # and the conservative applicability rules written about services do not answer
            # it. A caller cannot widen the surface by naming a unit of another type.
            validate_service_unit_name(unit_id)
        except SystemdInvalidUnitName as exc:
            return _not_dispatched(
                action, unit_id, "invalid_service_unit_name",
                {**detail, "required_unit_type": SYSTEMD_LIFECYCLE_UNIT_TYPE,
                 "error": str(exc)})

        unavailable = self._environment_unavailable(observed_at)
        if unavailable is not None:
            return _not_dispatched(
                action, unit_id, unavailable.reason or "systemd_unavailable",
                {**detail, **unavailable.detail})

        # **The phase boundary, and it is one variable.** While this is ``None`` the manager
        # has enqueued nothing and a failure is a proof that the host did not move. The
        # instant it holds a job path that proof is gone — permanently, and for every
        # failure after it, not only the ones raised by the dispatch call itself. Closing a
        # match, closing a connection and receiving a signal can all fail *after* a job is
        # running, and none of them says anything about what that job did.
        job_path: str | None = None
        invocation: str | None = None
        active_state: str | None = None
        completion: tuple[int, str] | None = None
        try:
            with self._connection_factory() as connection:
                connection.subscribe()
                with connection.job_watch() as watch:
                    try:
                        object_path = connection.get_unit_path(unit_id)
                    except _LoadedUnitAbsent:
                        return _not_dispatched(
                            action, unit_id, "unit_not_loaded", detail)
                    properties = connection.unit_properties(object_path)
                    identity = _string_or_none(properties.get("Id"))
                    load_state = _string_or_none(properties.get("LoadState"))
                    invocation = _invocation_id(properties.get("InvocationID"))
                    active_state = _string_or_none(properties.get("ActiveState"))
                    if identity != unit_id or load_state != "loaded":
                        return _not_dispatched(
                            action, unit_id, "dispatch_identity_not_canonical",
                            {**detail, "resolved_id": identity,
                             "load_state": load_state})
                    detail["invocation_id_at_dispatch"] = invocation
                    detail["active_state_at_dispatch"] = active_state

                    try:
                        job_path = connection.unit_lifecycle(object_path, action)
                    except DBusErrorResponse as exc:
                        # An error *reply* is an authoritative negative: the manager
                        # answered, and an answered call enqueued no job. This is the one
                        # place a refusal at the boundary is still a proof.
                        return _not_dispatched(
                            action, unit_id,
                            "authorization_denied"
                            if exc.name in _AUTHORIZATION_ERRORS
                            else "dispatch_refused",
                            {**detail, "dbus_error": str(exc.name),
                             "authorization_decision_point": "dispatch"},
                            invocation_id=invocation, active_state=active_state)
                    except Exception as exc:  # noqa: BLE001 - outcome, never exception
                        # The call left this process and nothing intelligible came back.
                        return _write_unknown(
                            action, unit_id, "dispatch_answer_untrustworthy",
                            {**detail, **_failure_detail(exc)},
                            invocation_id=invocation, active_state=active_state)

                    completion = watch.result_for(
                        job_path, unit_id, JOB_COMPLETION_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - outcome, never exception
            # Which side of the boundary this happened on is the whole of the decision, and
            # the exception's *type* is not consulted for it. An error reply arriving while
            # a job is already running is still an error reply, and it still says nothing
            # about that job — reading it as a pre-dispatch refusal would report a write
            # that may have happened as one that provably did not.
            if job_path is not None:
                return _write_unknown(
                    action, unit_id, "job_result_unobservable",
                    {**detail, **_failure_detail(exc)},
                    invocation_id=invocation, active_state=active_state)
            return _not_dispatched(
                action, unit_id,
                "systemd_refused_before_dispatch"
                if isinstance(exc, DBusErrorResponse)
                else "systemd_unreachable",
                {**detail, **_failure_detail(exc)},
                invocation_id=invocation, active_state=active_state)

        if completion is None:
            # The job was enqueued and this process stopped listening before it heard the
            # end of it. Nothing here can say whether it ran, and saying either would be
            # inventing the answer.
            return _write_unknown(
                action, unit_id, "job_result_not_observed",
                {**detail, "timeout_s": JOB_COMPLETION_TIMEOUT_S},
                invocation_id=invocation, active_state=active_state)

        job_id, job_result = completion
        # **Only `done` is written.** Every other result — including `skipped`, whose exact
        # guarantees this build has not established — means the manager accepted the
        # transaction and something other than completion happened to it. A start that
        # failed may have run half its execution; a stop that timed out may have killed
        # processes. `not_written` is a proof and none of those are proofs.
        written = job_result == JOB_RESULT_DONE
        return LifecycleDispatchResult(
            outcome="written" if written else "write_unknown",
            reason="job_completed" if written else "job_did_not_complete",
            action=action, unit_id=unit_id, job_id=job_id, job_result=job_result,
            invocation_id=invocation, active_state=active_state, detail=detail,
        )

    # ---------------------------------------------------------------------- inventory

    def observe_units(self) -> UnitBatch:
        started_at = _now()
        started_clock = self._monotonic()
        unavailable = self._environment_unavailable(started_at)
        if unavailable is not None:
            return _failed_batch(
                started_at,
                unavailable.reason or "system_manager_unavailable",
                unavailable.detail,
                inventory_limit=self._max_units,
            )

        units: dict[str, UnitObservation] = {}
        issues: list[dict[str, Any]] = []
        provider_version: str | None = None
        listed_count: int | None = None
        inventory_method: str | None = None
        truncated = False
        cap_reached = False
        timed_out = False
        deadline = started_clock + self._inventory_timeout_s

        def remaining_timeout() -> float:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise _InventoryDeadline("loaded-unit inventory deadline expired")
            return min(self._timeout_s, remaining)

        try:
            with self._connection_factory() as connection:
                manager = _normalise_manager(
                    connection.manager_properties(timeout_s=remaining_timeout()), _now()
                )
                provider_version = manager.version
                core_gaps = list(manager.detail.get("core_gaps") or [])
                optional_gaps = list(manager.detail.get("optional_gaps") or [])
                if core_gaps:
                    issues.append(
                        _issue(
                            "manager_core_properties_missing",
                            "the manager omitted conservative core provider evidence",
                            {"gaps": core_gaps},
                        )
                    )
                if optional_gaps:
                    issues.append(
                        _issue(
                            "manager_optional_metadata_missing",
                            "the manager omitted optional provider metadata",
                            {"gaps": optional_gaps},
                        )
                    )

                rows, inventory_method = connection.list_loaded_units(
                    timeout_s=remaining_timeout()
                )
                malformed_rows = sum(
                    1
                    for row in rows
                    if not isinstance(row, (list, tuple))
                    or len(row) < 7
                    or not isinstance(row[0], str)
                )
                if malformed_rows:
                    issues.append(
                        _issue(
                            "inventory_rows_unreadable",
                            "systemd returned loaded-unit rows this build could not identify",
                            {"count": malformed_rows},
                        )
                    )
                selected = sorted(
                    (row for row in rows if _row_is_supported(row)), key=lambda row: str(row[0])
                )
                listed_count = len(selected)
                cap_reached = listed_count >= self._max_units
                truncated = listed_count > self._max_units
                if cap_reached:
                    issues.append(
                        _issue(
                            "inventory_limit_reached",
                            "the selected loaded-unit inventory reached its observation limit",
                            {
                                "listed_count": listed_count,
                                "limit": self._max_units,
                                "omitted_count": max(listed_count - self._max_units, 0),
                                "truncated": truncated,
                            },
                        )
                    )
                selected = selected[: self._max_units]

                for row in selected:
                    listed_name = str(row[0])
                    object_path = str(row[6])
                    try:
                        observation = self._observe_path(
                            connection,
                            object_path,
                            listed_name=listed_name,
                            list_row=row,
                            timeout_factory=remaining_timeout,
                        )
                    except (OSError, TimeoutError, ConnectionError):
                        # A transport-level failure invalidates every object path obtained
                        # on this connection.  Abort and let the outer handler publish the
                        # coherent partial prefix; retrying hundreds of stale handles would
                        # add latency without adding evidence.
                        raise
                    except Exception as exc:
                        issues.append(
                            _issue(
                                "unit_read_failed",
                                "one loaded unit could not be observed",
                                {"unit": listed_name, **_failure_detail(exc)},
                            )
                        )
                        continue
                    prior = units.get(observation.canonical_id)
                    if prior is not None:
                        issues.append(
                            _issue(
                                "alias_duplicate_collapsed",
                                "more than one listed name resolved to one canonical unit",
                                {
                                    "canonical_id": observation.canonical_id,
                                    "listed_name": listed_name,
                                },
                            )
                        )
                        continue
                    units[observation.canonical_id] = observation
        except TimeoutError as exc:
            timed_out = True
            issues.append(
                _issue(
                    "inventory_timeout",
                    "the bounded inventory time budget expired",
                    {
                        "timeout_s": self._inventory_timeout_s,
                        "observed_count": len(units),
                        **_failure_detail(exc),
                    },
                )
            )
            # Before ListUnits answered there is no estate prefix whose completeness can
            # be described.  Once it answered, even a zero-unit observed prefix is a
            # truthful partial inventory rather than an empty healthy estate.
            if listed_count is None:
                return _failed_batch(
                    started_at,
                    "inventory_timeout",
                    {"timeout_s": self._inventory_timeout_s, **_failure_detail(exc)},
                    provider_version=provider_version,
                    inventory_limit=self._max_units,
                )
        except Exception as exc:
            if not units:
                return _failed_batch(
                    started_at,
                    "system_manager_read_failed",
                    _failure_detail(exc),
                    provider_version=provider_version,
                    inventory_limit=self._max_units,
                )
            issues.append(
                _issue(
                    "inventory_interrupted",
                    "the system manager connection failed during the inventory",
                    _failure_detail(exc),
                )
            )

        incomplete = (
            truncated
            or timed_out
            or any(
                issue["code"]
                in {
                    "unit_read_failed",
                    "inventory_interrupted",
                    "inventory_rows_unreadable",
                }
                for issue in issues
            )
        )
        return UnitBatch(
            status="partial" if incomplete else "ok",
            started_at=started_at,
            completed_at=_now(),
            provider_version=provider_version,
            units=tuple(units[name] for name in sorted(units)),
            listed_count=listed_count,
            selected_count=min(listed_count or 0, self._max_units),
            inventory_limit=self._max_units,
            inventory_complete=not incomplete,
            truncated=truncated,
            cap_reached=cap_reached,
            inventory_method=inventory_method,
            issues=tuple(issues),
            detail={
                "manager_scope": "system",
                "supported_unit_suffixes": list(SUPPORTED_UNIT_SUFFIXES),
                "duration_ms": round((self._monotonic() - started_clock) * 1000, 3),
                "connection_count": 1,
                "inventory_deadline_s": self._inventory_timeout_s,
                "per_call_timeout_s": self._timeout_s,
                "deadline_enforcement": "remaining_time_per_dbus_call",
            },
        )

    # ----------------------------------------------------------------------- targeted

    def observe_unit(self, unit_id: str) -> TargetedUnitObservation:
        _validate_unit_name(unit_id)
        started_at = _now()
        unavailable = self._environment_unavailable(started_at)
        if unavailable is not None:
            return TargetedUnitObservation(
                status="failed",
                requested_unit=unit_id,
                started_at=started_at,
                completed_at=_now(),
                reason=unavailable.reason,
                issues=(
                    _issue(
                        unavailable.reason or "system_manager_unavailable",
                        "the system manager could not be consulted",
                        unavailable.detail,
                    ),
                ),
            )

        provider_version: str | None = None
        try:
            with self._connection_factory() as connection:
                manager = _normalise_manager(connection.manager_properties(), _now())
                provider_version = manager.version
                try:
                    object_path = connection.get_unit_path(unit_id)
                except _LoadedUnitAbsent:
                    return TargetedUnitObservation(
                        status="absent",
                        requested_unit=unit_id,
                        started_at=started_at,
                        completed_at=_now(),
                        provider_version=provider_version,
                        reason="loaded_unit_absent",
                        issues=(
                            _issue(
                                "loaded_unit_absent",
                                "systemd authoritatively reports that this unit is not loaded",
                                {"unit": unit_id},
                            ),
                        ),
                    )
                observation = self._observe_path(
                    connection, object_path, listed_name=unit_id, list_row=None
                )
        except Exception as exc:
            return TargetedUnitObservation(
                status="failed",
                requested_unit=unit_id,
                started_at=started_at,
                completed_at=_now(),
                provider_version=provider_version,
                reason="targeted_unit_read_failed",
                issues=(
                    _issue(
                        "targeted_unit_read_failed",
                        "the targeted loaded unit could not be observed",
                        {"unit": unit_id, **_failure_detail(exc)},
                    ),
                ),
            )
        return TargetedUnitObservation(
            status="observed",
            requested_unit=unit_id,
            started_at=started_at,
            completed_at=_now(),
            provider_version=provider_version,
            unit=observation,
        )

    # ------------------------------------------------------------- lifecycle read context

    def observe_effect_graph(
        self,
        unit_id: str,
        action: str,
        *,
        max_nodes: int = EFFECT_GRAPH_MAX_NODES,
        max_depth: int = EFFECT_GRAPH_MAX_DEPTH,
        max_edges: int = EFFECT_GRAPH_MAX_EDGES,
        timeout_s: float = EFFECT_GRAPH_TIMEOUT_S,
    ) -> EffectGraphObservation:
        """Read a bounded potential-effect closure; never enqueue or load a unit."""
        _validate_unit_name(unit_id)
        if action not in SYSTEMD_LIFECYCLE_ACTIONS:
            raise ValueError("unsupported systemd lifecycle action")
        node_limit = max(1, min(max_nodes, EFFECT_GRAPH_MAX_NODES))
        depth_limit = max(0, min(max_depth, EFFECT_GRAPH_MAX_DEPTH))
        edge_limit = max(1, min(max_edges, EFFECT_GRAPH_MAX_EDGES))
        deadline_s = max(0.001, min(timeout_s, EFFECT_GRAPH_TIMEOUT_S))
        observed_at = _now()
        if not unit_id.endswith(".service"):
            return EffectGraphObservation(
                status="failed", observed_at=observed_at, requested_unit=unit_id,
                action=action, reason="target_not_service",
            )
        unavailable = self._environment_unavailable(observed_at)
        if unavailable is not None:
            return EffectGraphObservation(
                status="failed", observed_at=observed_at, requested_unit=unit_id,
                action=action, reason=unavailable.reason, gaps=("systemd.manager",),
                detail=unavailable.detail,
            )

        relations = (
            _START_EFFECT_RELATIONSHIPS
            if action == "start"
            else _STOP_EFFECT_RELATIONSHIPS
            if action == "stop"
            else _START_EFFECT_RELATIONSHIPS | _STOP_EFFECT_RELATIONSHIPS
        )
        deadline = self._monotonic() + deadline_s
        nodes: dict[str, dict[str, Any]] = {}
        edges: set[tuple[str, str, str]] = set()
        gaps: set[str] = set()
        active_sources: set[str] = set()
        active_upholding_sources: set[str] = set()
        target: UnitObservation | None = None
        canonical_target: str | None = None
        provider_version: str | None = None

        def remaining() -> float:
            value = deadline - self._monotonic()
            if value <= 0:
                raise _InventoryDeadline("effect graph deadline expired")
            return min(self._timeout_s, value)

        try:
            with self._connection_factory() as connection:
                manager = _normalise_manager(
                    connection.manager_properties(timeout_s=remaining()), _now()
                )
                provider_version = manager.version
                try:
                    target_path = connection.get_unit_path(unit_id, timeout_s=remaining())
                except _LoadedUnitAbsent:
                    return EffectGraphObservation(
                        status="failed", observed_at=observed_at,
                        requested_unit=unit_id, action=action,
                        provider_version=provider_version,
                        reason="loaded_unit_absent", gaps=("target.loaded_unit",),
                    )
                target = self._observe_path(
                    connection,
                    target_path,
                    listed_name=unit_id,
                    list_row=None,
                    timeout_factory=remaining,
                )
                canonical_target = target.canonical_id
                if canonical_target != unit_id:
                    return EffectGraphObservation(
                        status="failed", observed_at=observed_at,
                        requested_unit=unit_id, action=action,
                        provider_version=provider_version,
                        canonical_target=canonical_target, target=target,
                        reason="canonical_target_mismatch",
                        gaps=("target.canonical_id",),
                    )

                pending: list[tuple[str, int, str | None]] = [
                    (canonical_target, 0, target_path)
                ]
                queued = {canonical_target}
                aliases: dict[str, str] = {canonical_target: canonical_target}
                provisional_edges: set[tuple[str, str, str]] = set()
                edge_limit_reached = False
                while pending:
                    requested, depth, known_path = pending.pop(0)
                    if len(nodes) >= node_limit:
                        gaps.add("effect_graph.node_limit")
                        break
                    if depth > depth_limit:
                        gaps.add("effect_graph.depth_limit")
                        continue
                    try:
                        path = known_path or connection.get_unit_path(
                            requested, timeout_s=remaining()
                        )
                        properties = connection.unit_properties(path, timeout_s=remaining())
                    except _LoadedUnitAbsent:
                        gaps.add(f"effect_graph.unresolved:{requested}")
                        continue
                    canonical = properties.get("Id")
                    if not isinstance(canonical, str) or not canonical:
                        gaps.add(f"effect_graph.canonical_id:{requested}")
                        continue
                    if canonical in nodes:
                        aliases[requested] = canonical
                        continue
                    aliases[requested] = canonical
                    nodes[canonical] = properties
                    if requested != canonical:
                        gaps.add(f"effect_graph.alias_reference:{requested}")

                    if action in {"stop", "restart"}:
                        stop_when_unneeded = properties.get("StopWhenUnneeded")
                        if stop_when_unneeded is None:
                            gaps.add(f"effect_graph.unsupported:StopWhenUnneeded:{canonical}")
                        elif stop_when_unneeded is True:
                            gaps.add(f"effect_graph.stop_when_unneeded:{canonical}")

                    for relation in relations:
                        raw = properties.get(relation)
                        if raw is None:
                            gaps.add(f"effect_graph.unsupported:{relation}:{canonical}")
                            continue
                        if not isinstance(raw, (list, tuple)):
                            gaps.add(f"effect_graph.unreadable:{relation}:{canonical}")
                            continue
                        for referenced in raw:
                            if not isinstance(referenced, str) or not referenced:
                                gaps.add(f"effect_graph.unreadable:{relation}:{canonical}")
                                continue
                            if len(provisional_edges) >= edge_limit:
                                gaps.add("effect_graph.edge_limit")
                                edge_limit_reached = True
                                break
                            provisional_edges.add((canonical, relation, referenced))
                            if referenced not in queued:
                                if len(queued) >= node_limit:
                                    gaps.add("effect_graph.node_limit")
                                    continue
                                queued.add(referenced)
                                pending.append((referenced, depth + 1, None))
                        if edge_limit_reached:
                            break
                    if edge_limit_reached:
                        break

                for source, relation, referenced in provisional_edges:
                    resolved = aliases.get(referenced, referenced)
                    edges.add((source, relation, resolved))
                    referenced_properties = nodes.get(resolved)
                    if referenced_properties is None:
                        gaps.add(f"effect_graph.unresolved:{referenced}")
                        continue
                    if (
                        source == canonical_target
                        and relation in _REACTIVATION_RELATIONSHIPS
                    ):
                        active, evidence_gaps = _reactivation_source_evidence(
                            referenced_properties
                        )
                        gaps.update(
                            f"effect_graph.reactivation_source_{gap}:{relation}:{resolved}"
                            for gap in evidence_gaps
                        )
                        if active and relation == "TriggeredBy":
                            active_sources.add(resolved)
                        if active and relation == "UpheldBy":
                            active_upholding_sources.add(resolved)
        except Exception as exc:
            gaps.add("effect_graph.read_failed")
            return EffectGraphObservation(
                status="partial" if target else "failed",
                observed_at=observed_at,
                requested_unit=unit_id,
                action=action,
                provider_version=provider_version,
                canonical_target=canonical_target,
                target=target,
                units=tuple(sorted(nodes)),
                edges=tuple(
                    {"source": source, "relation": relation, "target": destination}
                    for source, relation, destination in sorted(edges)
                ),
                active_activation_sources=tuple(sorted(active_sources)),
                active_upholding_sources=tuple(sorted(active_upholding_sources)),
                gaps=tuple(sorted(gaps)),
                reason="effect_graph_read_failed",
                detail=_failure_detail(exc),
            )

        return EffectGraphObservation(
            status="complete" if not gaps else "partial",
            observed_at=observed_at,
            requested_unit=unit_id,
            action=action,
            provider_version=provider_version,
            canonical_target=canonical_target,
            target=target,
            units=tuple(sorted(nodes)),
            edges=tuple(
                {"source": source, "relation": relation, "target": destination}
                for source, relation, destination in sorted(edges)
            ),
            active_activation_sources=tuple(sorted(active_sources)),
            active_upholding_sources=tuple(sorted(active_upholding_sources)),
            gaps=tuple(sorted(gaps)),
            reason=None if not gaps else "effect_graph_incomplete",
            detail={
                "max_nodes": node_limit,
                "max_depth": depth_limit,
                "max_edges": edge_limit,
                "deadline_s": deadline_s,
                "ordering_relationships_excluded": ["Before", "After"],
                "transport_handles_persisted": False,
            },
        )

    # --------------------------------------------------------------- pidfd containment

    def resolve_process_unit_pidfd(self, pidfd: int) -> ProcessUnitResolution:
        """Resolve one caller-held pidfd with systemd's exact read-only method.

        There is intentionally no numeric-PID fallback.  The supplied integer is a local
        file descriptor passed over the provider-private D-Bus connection with Unix-FD
        negotiation enabled; it is never accepted from the agent protocol.
        """
        observed_at = _now()
        if isinstance(pidfd, bool) or not isinstance(pidfd, int) or pidfd < 0:
            return ProcessUnitResolution(
                status="failed",
                observed_at=observed_at,
                reason="invalid_pidfd",
            )
        unavailable = self._environment_unavailable(observed_at)
        if unavailable is not None:
            return ProcessUnitResolution(
                status="failed",
                observed_at=observed_at,
                reason=unavailable.reason,
                detail=unavailable.detail,
            )
        try:
            with self._pidfd_connection_factory() as connection:
                object_path, returned_id, returned_invocation = (
                    connection.get_unit_by_pidfd(pidfd)
                )
                unit = self._observe_path(
                    connection,
                    object_path,
                    listed_name=None,
                    list_row=None,
                    allow_out_of_scope=True,
                )
        except DBusErrorResponse as exc:
            return ProcessUnitResolution(
                status=("unsupported" if exc.name in _UNKNOWN_METHOD_ERRORS else "unresolved"),
                observed_at=observed_at,
                reason=(
                    "systemd_get_unit_by_pidfd_unsupported"
                    if exc.name in _UNKNOWN_METHOD_ERRORS
                    else "pidfd_unit_not_resolved"
                ),
                detail=_failure_detail(exc),
            )
        except Exception as exc:
            return ProcessUnitResolution(
                status="failed",
                observed_at=observed_at,
                reason="pidfd_unit_resolution_failed",
                detail=_failure_detail(exc),
            )
        invocation_id = _invocation_id(returned_invocation)
        unit_invocation = unit.facts.get("invocation_id")
        if (
            returned_id != unit.canonical_id
            or invocation_id is None
            or invocation_id != unit_invocation
        ):
            return ProcessUnitResolution(
                status="failed",
                observed_at=observed_at,
                canonical_id=unit.canonical_id,
                invocation_id=invocation_id,
                unit=unit,
                reason="pidfd_unit_identity_mismatch",
                detail={
                    "returned_unit_id": returned_id,
                    "canonical_unit_id": unit.canonical_id,
                    "returned_invocation_id": invocation_id,
                    "unit_invocation_id": unit_invocation,
                    "transport_handle_persisted": False,
                },
            )
        return ProcessUnitResolution(
            status="resolved",
            observed_at=observed_at,
            canonical_id=unit.canonical_id,
            invocation_id=invocation_id,
            unit=unit,
            detail={
                "manager_scope": "system",
                "unix_fd_transport": True,
                "transport_handle_persisted": False,
            },
        )

    # ---------------------------------------------------------------- self-unit evidence

    def resolve_current_process_unit(self) -> AgentUnitResolution:
        observed_at = _now()
        try:
            cgroup = _read_systemd_cgroup(self._proc_cgroup)
        except OSError as exc:
            return AgentUnitResolution(
                status="failed",
                observed_at=observed_at,
                reason="proc_cgroup_unreadable",
                detail={"path": str(self._proc_cgroup), **_failure_detail(exc)},
            )
        if cgroup is None:
            return AgentUnitResolution(
                status="unresolved",
                observed_at=observed_at,
                reason="systemd_cgroup_unavailable",
                gaps=("proc_self_cgroup.systemd",),
                detail={"path": str(self._proc_cgroup)},
            )

        unavailable = self._environment_unavailable(observed_at)
        if unavailable is not None:
            return AgentUnitResolution(
                status="failed",
                observed_at=observed_at,
                cgroup=cgroup,
                reason=unavailable.reason,
                detail=unavailable.detail,
            )
        try:
            with self._connection_factory() as connection:
                object_path = connection.get_unit_by_control_group(cgroup)
                unit = self._observe_path(
                    connection,
                    object_path,
                    listed_name=None,
                    list_row=None,
                    allow_out_of_scope=True,
                )
        except DBusErrorResponse as exc:
            return AgentUnitResolution(
                status="unresolved",
                observed_at=observed_at,
                cgroup=cgroup,
                reason="control_group_not_resolved",
                detail=_failure_detail(exc),
            )
        except Exception as exc:
            return AgentUnitResolution(
                status="failed",
                observed_at=observed_at,
                cgroup=cgroup,
                reason="control_group_resolution_failed",
                detail=_failure_detail(exc),
            )
        return AgentUnitResolution(
            status="resolved",
            observed_at=observed_at,
            cgroup=cgroup,
            canonical_id=unit.canonical_id,
            invocation_id=unit.facts.get("invocation_id"),
            unit=unit,
        )

    def resolve_control_group_unit(self, control_group: str) -> AgentUnitResolution:
        """Resolve one kernel-derived absolute cgroup path through systemd itself."""
        observed_at = _now()
        if (
            not isinstance(control_group, str)
            or not control_group.startswith("/")
            or "\x00" in control_group
            or len(control_group) > 4096
        ):
            return AgentUnitResolution(
                status="failed", observed_at=observed_at,
                method="manager_get_unit_by_control_group",
                reason="invalid_control_group_evidence",
            )
        try:
            with self._connection_factory() as connection:
                path = connection.get_unit_by_control_group(control_group)
                unit = self._observe_path(
                    connection, path, listed_name=None, list_row=None,
                    allow_out_of_scope=True,
                )
        except DBusErrorResponse as exc:
            return AgentUnitResolution(
                status="unresolved", observed_at=observed_at,
                method="manager_get_unit_by_control_group", cgroup=control_group,
                reason="control_group_not_resolved", detail=_failure_detail(exc),
            )
        except Exception as exc:
            return AgentUnitResolution(
                status="failed", observed_at=observed_at,
                method="manager_get_unit_by_control_group", cgroup=control_group,
                reason="control_group_resolution_failed", detail=_failure_detail(exc),
            )
        return AgentUnitResolution(
            status="resolved", observed_at=observed_at,
            method="manager_get_unit_by_control_group", cgroup=control_group,
            canonical_id=unit.canonical_id,
            invocation_id=unit.facts.get("invocation_id"), unit=unit,
        )

    def resolve_provider_unit(self, provider: str) -> AgentUnitResolution:
        """Resolve a closed provider owner without a service- or process-name heuristic."""
        observed_at = _now()
        bus_name = _PROVIDER_DBUS_NAMES.get(provider)
        if bus_name is None:
            return AgentUnitResolution(
                status="unresolved", observed_at=observed_at,
                method="provider_owner_to_systemd_unit",
                reason="provider_owner_resolution_unsupported",
                detail={"provider": provider},
            )
        try:
            with self._connection_factory() as connection:
                owner, pid = connection.dbus_name_owner_pid(bus_name)
                path = connection.get_unit_by_pid(pid)
                unit = self._observe_path(
                    connection, path, listed_name=None, list_row=None,
                    allow_out_of_scope=True,
                )
        except Exception as exc:
            return AgentUnitResolution(
                status="failed", observed_at=observed_at,
                method="dbus_owner_pid_to_systemd_unit",
                reason="provider_owner_resolution_failed",
                detail={"provider": provider, "bus_name": bus_name, **_failure_detail(exc)},
            )
        return AgentUnitResolution(
            status="resolved", observed_at=observed_at,
            method="dbus_owner_pid_to_systemd_unit",
            canonical_id=unit.canonical_id,
            invocation_id=unit.facts.get("invocation_id"), unit=unit,
            detail={"provider": provider, "bus_name": bus_name, "owner": owner, "pid": pid},
        )

    # ------------------------------------------------------------------------- internals

    def _environment_unavailable(self, observed_at: str) -> ManagerObservation | None:
        if self._runtime_path is None or self._runtime_path.exists():
            return None
        return ManagerObservation(
            status="unavailable",
            observed_at=observed_at,
            reason="systemd_system_manager_absent",
            detail={"runtime_path": str(self._runtime_path), "manager_scope": "system"},
        )

    def _observe_path(
        self,
        connection: Any,
        object_path: str,
        *,
        listed_name: str | None,
        list_row: tuple[Any, ...] | None,
        allow_out_of_scope: bool = False,
        timeout_factory: Callable[[], float] | None = None,
    ) -> UnitObservation:
        unit_properties = connection.unit_properties(
            object_path,
            timeout_s=timeout_factory() if timeout_factory is not None else None,
        )
        canonical_id = unit_properties.get("Id")
        if not isinstance(canonical_id, str) or not canonical_id:
            raise SystemdFailure(
                "canonical_id_unavailable",
                {"listed_name": listed_name, "interface": SYSTEMD_UNIT_INTERFACE},
            )
        unit_type = _unit_type(canonical_id)
        if unit_type is None and not allow_out_of_scope:
            raise SystemdFailure(
                "unsupported_unit_type", {"canonical_id": canonical_id}
            )
        if unit_type is None:
            unit_type = canonical_id.rsplit(".", 1)[-1] if "." in canonical_id else "unknown"

        type_properties: dict[str, Any] = {}
        type_gap: str | None = None
        if unit_type in _TYPE_INTERFACES:
            try:
                type_properties = connection.type_properties(
                    object_path,
                    unit_type,
                    timeout_s=timeout_factory() if timeout_factory is not None else None,
                )
            except DBusErrorResponse as exc:
                # A missing type interface on an older manager is a per-unit evidence gap,
                # not a reason to discard the authoritative Unit interface observation.
                if exc.name in _UNKNOWN_METHOD_ERRORS:
                    type_gap = f"{unit_type}.interface"
                else:
                    raise
            except SystemdFailure:
                raise
            except Exception:
                # The generic unit still exists, but a disconnect means this observation
                # is not trustworthy as a coherent two-interface read.
                raise

        observed_at = _now()
        facts, gaps = _normalise_unit(unit_properties, type_properties, unit_type)
        if type_gap:
            gaps.append(type_gap)
        return UnitObservation(
            canonical_id=canonical_id,
            facts=facts,
            observed_at=observed_at,
            gaps=tuple(sorted(set(gaps))),
            evidence={
                "source": SOURCE_SYSTEMD_UNITS,
                "transport": "system_bus_dbus",
                "destination": SYSTEMD_DESTINATION,
                # The returned object path was consumed on this connection and is
                # intentionally not persisted: manager re-exec and unit GC can invalidate
                # it.  Canonical Unit.Id is the durable evidence above this boundary.
                "transport_handle_persisted": False,
                "interfaces_read": [
                    SYSTEMD_UNIT_INTERFACE,
                    *([_TYPE_INTERFACES[unit_type]] if unit_type in _TYPE_INTERFACES else []),
                ],
                "listed_name": listed_name,
                "list_row": _safe_list_evidence(list_row),
                "manager_scope": "system",
            },
        )


def _normalise_manager(properties: dict[str, Any], observed_at: str) -> ManagerObservation:
    core_gaps = tuple(
        f"manager.{key}" for key in _MANAGER_CORE_PROPERTIES if key not in properties
    )
    optional_gaps = tuple(
        f"manager.{key}" for key in _MANAGER_OPTIONAL_PROPERTIES if key not in properties
    )
    gaps = core_gaps + optional_gaps
    facts = {
        "version": _string_or_none(properties.get("Version")),
        "system_state": _string_or_none(properties.get("SystemState")),
        "features": _string_or_none(properties.get("Features")),
        "virtualization": _string_or_none(properties.get("Virtualization")),
        "architecture": _string_or_none(properties.get("Architecture")),
        "tainted": _string_or_none(properties.get("Tainted")),
    }
    # Reaching and reading the Manager interface proves the capability.  Missing Version
    # or SystemState degrades the evidence without inventing a minimum version number.
    status = "ok" if not gaps else "degraded"
    return ManagerObservation(
        status=status,
        observed_at=observed_at,
        version=facts["version"],
        facts=facts,
        gaps=gaps,
        reason="manager_properties_incomplete" if gaps else None,
        detail={
            "manager_scope": "system",
            "transport": "system_bus_dbus",
            "core_gaps": list(core_gaps),
            "optional_gaps": list(optional_gaps),
        },
    )


def _normalise_unit(
    unit: dict[str, Any], typed: dict[str, Any], unit_type: str
) -> tuple[dict[str, Any], list[str]]:
    gaps: list[str] = []

    def optional(source: dict[str, Any], name: str, prefix: str) -> Any:
        if name not in source:
            gaps.append(f"{prefix}.{name}")
            return None
        return source[name]

    # Id, LoadState, ActiveState and SubState are the conservative unit-object core.  Id
    # was checked before this function; the others stay nullable and make the observation
    # partial if a manager omits them.
    for name in ("LoadState", "ActiveState", "SubState"):
        if name not in unit:
            gaps.append(f"unit.{name}")

    names_raw = optional(unit, "Names", "unit")
    names = (
        sorted({str(value) for value in names_raw if isinstance(value, str) and value})
        if isinstance(names_raw, (list, tuple))
        else None
    )
    drop_ins_raw = optional(unit, "DropInPaths", "unit")
    drop_ins = (
        [str(value) for value in drop_ins_raw if isinstance(value, str) and value]
        if isinstance(drop_ins_raw, (list, tuple))
        else None
    )

    relationships: list[dict[str, str]] = []
    for property_name, group in _RELATIONSHIP_GROUPS.items():
        # Slice 11A's generic observation fidelity must not depend on newer lifecycle-only
        # reverse properties.  The effect-graph read below assesses their absence for the
        # action that needs them; routine observation simply includes them when present.
        raw = (
            unit.get(property_name)
            if property_name in _LIFECYCLE_ONLY_RELATIONSHIPS
            else optional(unit, property_name, "unit")
        )
        if not isinstance(raw, (list, tuple)):
            continue
        for target in raw:
            if isinstance(target, str) and target:
                relationships.append(
                    {"kind": property_name, "group": group, "target_unit": target}
                )

    facts: dict[str, Any] = {
        "canonical_id": unit["Id"],
        "names": names,
        "description": _string_or_none(optional(unit, "Description", "unit")),
        "unit_type": unit_type,
        "load_state": _string_or_none(unit.get("LoadState")),
        "active_state": _string_or_none(unit.get("ActiveState")),
        "sub_state": _string_or_none(unit.get("SubState")),
        "unit_file_state": _string_or_none(optional(unit, "UnitFileState", "unit")),
        "unit_file_preset": _string_or_none(optional(unit, "UnitFilePreset", "unit")),
        "can_start": _bool_or_none(optional(unit, "CanStart", "unit")),
        "can_stop": _bool_or_none(optional(unit, "CanStop", "unit")),
        "can_reload": _bool_or_none(optional(unit, "CanReload", "unit")),
        "refuse_manual_start": _bool_or_none(
            optional(unit, "RefuseManualStart", "unit")
        ),
        "refuse_manual_stop": _bool_or_none(optional(unit, "RefuseManualStop", "unit")),
        "need_daemon_reload": _bool_or_none(optional(unit, "NeedDaemonReload", "unit")),
        "fragment_path": _path_or_none(optional(unit, "FragmentPath", "unit")),
        "source_path": _path_or_none(optional(unit, "SourcePath", "unit")),
        "drop_in_paths": drop_ins,
        "transient": _bool_or_none(optional(unit, "Transient", "unit")),
        "current_job": _job_or_none(optional(unit, "Job", "unit")),
        "invocation_id": _invocation_id(optional(unit, "InvocationID", "unit")),
        "timestamps": {
            _snake(name): _timestamp_or_none(optional(unit, name, "unit"))
            for name in (
                "StateChangeTimestamp",
                "StateChangeTimestampMonotonic",
                "InactiveExitTimestamp",
                "InactiveExitTimestampMonotonic",
                "ActiveEnterTimestamp",
                "ActiveEnterTimestampMonotonic",
                "ActiveExitTimestamp",
                "ActiveExitTimestampMonotonic",
                "InactiveEnterTimestamp",
                "InactiveEnterTimestampMonotonic",
            )
        },
        "template": _template_name(str(unit["Id"])),
        "relationships": relationships,
    }

    if unit_type == "service":
        facts["service"] = _normalise_service(typed, gaps, optional)
    elif unit_type == "socket":
        facts["socket"] = _normalise_socket(typed, gaps, optional)
    elif unit_type == "timer":
        timer = _normalise_timer(typed, gaps, optional)
        facts["timer"] = timer
        _append_activation_relationship(relationships, timer.get("unit"))
    elif unit_type == "path":
        path = _normalise_path(typed, gaps, optional)
        facts["path"] = path
        _append_activation_relationship(relationships, path.get("unit"))
    elif unit_type == "mount":
        facts["mount"] = _normalise_mount(typed, gaps, optional)

    return facts, gaps


def _normalise_service(
    typed: dict[str, Any], gaps: list[str], optional: Callable[..., Any]
) -> dict[str, Any]:
    get = lambda name: optional(typed, name, "service")
    return {
        "type": _string_or_none(get("Type")),
        "main_pid": _pid_or_none(get("MainPID")),
        "control_pid": _pid_or_none(get("ControlPID")),
        "exec_main_pid": _pid_or_none(get("ExecMainPID")),
        "result": _string_or_none(get("Result")),
        "exec_main_code": _int_or_none(get("ExecMainCode")),
        "exec_main_status": _int_or_none(get("ExecMainStatus")),
        "restart": _string_or_none(get("Restart")),
        "restart_usec": _duration_or_none(get("RestartUSec")),
        "n_restarts": _int_or_none(get("NRestarts")),
        "remain_after_exit": _bool_or_none(get("RemainAfterExit")),
        "guess_main_pid": _bool_or_none(get("GuessMainPID")),
        "exec_main_start_timestamp_monotonic": _timestamp_or_none(
            get("ExecMainStartTimestampMonotonic")
        ),
        "watchdog_usec": _duration_or_none(get("WatchdogUSec")),
        "watchdog_timestamp_monotonic": _timestamp_or_none(
            get("WatchdogTimestampMonotonic")
        ),
        "watchdog_signal": _int_or_none(get("WatchdogSignal")),
        "watchdog_pid": _pid_or_none(get("WatchdogPID")),
        "control_group": _path_or_none(get("ControlGroup")),
    }


def _normalise_socket(
    typed: dict[str, Any], gaps: list[str], optional: Callable[..., Any]
) -> dict[str, Any]:
    get = lambda name: optional(typed, name, "socket")
    listen_raw = get("Listen")
    listen: list[dict[str, str]] | None = None
    if isinstance(listen_raw, (list, tuple)):
        listen = []
        for entry in listen_raw:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                listen.append({"kind": str(entry[0]), "address": str(entry[1])})
    return {
        "listen": listen,
        "accept": _bool_or_none(get("Accept")),
        "accepted": _int_or_none(get("NAccepted")),
        "connections": _int_or_none(get("NConnections")),
        "refused": _int_or_none(get("NRefused")),
        "result": _string_or_none(get("Result")),
        "trigger_limit_interval_usec": _duration_or_none(get("TriggerLimitIntervalUSec")),
        "trigger_limit_burst": _int_or_none(get("TriggerLimitBurst")),
    }


def _normalise_timer(
    typed: dict[str, Any], gaps: list[str], optional: Callable[..., Any]
) -> dict[str, Any]:
    get = lambda name: optional(typed, name, "timer")
    return {
        "unit": _string_or_none(get("Unit")),
        "next_elapse_usec_realtime": _timestamp_or_none(get("NextElapseUSecRealtime")),
        "next_elapse_usec_monotonic": _timestamp_or_none(get("NextElapseUSecMonotonic")),
        "last_trigger_usec": _timestamp_or_none(get("LastTriggerUSec")),
        "last_trigger_usec_monotonic": _timestamp_or_none(
            get("LastTriggerUSecMonotonic")
        ),
        "persistent": _bool_or_none(get("Persistent")),
        "randomized_delay_usec": _duration_or_none(get("RandomizedDelayUSec")),
        "fixed_random_delay": _bool_or_none(get("FixedRandomDelay")),
        "accuracy_usec": _duration_or_none(get("AccuracyUSec")),
        "result": _string_or_none(get("Result")),
    }


def _normalise_path(
    typed: dict[str, Any], gaps: list[str], optional: Callable[..., Any]
) -> dict[str, Any]:
    get = lambda name: optional(typed, name, "path")
    paths_raw = get("Paths")
    paths: list[dict[str, str]] | None = None
    if isinstance(paths_raw, (list, tuple)):
        paths = []
        for entry in paths_raw:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                paths.append({"kind": str(entry[0]), "path": str(entry[1])})
    mode = get("DirectoryMode")
    return {
        "unit": _string_or_none(get("Unit")),
        "paths": paths,
        "make_directory": _bool_or_none(get("MakeDirectory")),
        "directory_mode": _mode_or_none(mode),
        "result": _string_or_none(get("Result")),
    }


def _normalise_mount(
    typed: dict[str, Any], gaps: list[str], optional: Callable[..., Any]
) -> dict[str, Any]:
    get = lambda name: optional(typed, name, "mount")
    return {
        "what": _safe_mount_source(_string_or_none(get("What"))),
        "where": _path_or_none(get("Where")),
        "type": _string_or_none(get("Type")),
        # Options is intentionally absent: mount options frequently carry credentials.
        "control_pid": _pid_or_none(get("ControlPID")),
        "directory_mode": _mode_or_none(get("DirectoryMode")),
        "result": _string_or_none(get("Result")),
        "sloppy_options": _bool_or_none(get("SloppyOptions")),
        "lazy_unmount": _bool_or_none(get("LazyUnmount")),
        "force_unmount": _bool_or_none(get("ForceUnmount")),
        "timeout_usec": _duration_or_none(get("TimeoutUSec")),
    }


def _append_activation_relationship(
    relationships: list[dict[str, str]], target: Any
) -> None:
    if not isinstance(target, str) or not target:
        return
    if any(r["kind"] == "Triggers" and r["target_unit"] == target for r in relationships):
        return
    relationships.append(
        {"kind": "Triggers", "group": "activation", "target_unit": target}
    )


def _read_systemd_cgroup(path: Path) -> str | None:
    """Read cgroup v2 or the v1 named systemd controller, without process heuristics."""
    text = path.read_text(encoding="utf-8")
    v1: str | None = None
    for line in text.splitlines():
        hierarchy, separator, rest = line.partition(":")
        if not separator:
            continue
        controllers, separator, cgroup = rest.partition(":")
        if not separator or not cgroup.startswith("/"):
            continue
        if hierarchy == "0" and controllers == "":
            return cgroup
        if "name=systemd" in controllers.split(",") or "systemd" in controllers.split(","):
            v1 = cgroup
    return v1


def _validate_unit_name(unit_id: str) -> None:
    if not isinstance(unit_id, str) or not _UNIT_NAME.fullmatch(unit_id):
        raise SystemdInvalidUnitName("unit_id must be one systemd unit name, not a path")
    if not unit_id.endswith(SUPPORTED_UNIT_SUFFIXES):
        raise SystemdInvalidUnitName(
            "unit_id must name a supported service, socket, timer, target, path or mount unit"
        )
    if "*" in unit_id or "?" in unit_id or "[" in unit_id:
        raise SystemdInvalidUnitName("unit_id must not contain a glob expression")


def _row_is_supported(row: Any) -> bool:
    return (
        isinstance(row, (list, tuple))
        and len(row) >= 7
        and isinstance(row[0], str)
        and row[0].endswith(SUPPORTED_UNIT_SUFFIXES)
    )


def _unit_type(unit_id: str) -> str | None:
    for suffix in SUPPORTED_UNIT_SUFFIXES:
        if unit_id.endswith(suffix):
            return suffix[1:]
    return None


def _template_name(unit_id: str) -> str | None:
    stem, marker, suffix = unit_id.rpartition(".")
    if "@" not in stem:
        return None
    prefix, instance = stem.split("@", 1)
    if not instance:  # A template declaration itself is not a runtime instance.
        return unit_id
    return f"{prefix}@.{suffix}" if marker else None


def _unwrap_variant(value: Any) -> Any:
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[0], str)
    ):
        return value[1]
    return value


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _path_or_none(value: Any) -> str | None:
    value = _string_or_none(value)
    return None if value in (None, "/") else value


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _pid_or_none(value: Any) -> int | None:
    value = _int_or_none(value)
    return value if value and value > 0 else None


def _timestamp_or_none(value: Any) -> int | None:
    value = _int_or_none(value)
    return value if value and value > 0 else None


def _duration_or_none(value: Any) -> int | None:
    value = _int_or_none(value)
    return value if value is not None and 0 <= value < _UINT64_MAX else None


def _mode_or_none(value: Any) -> str | None:
    value = _int_or_none(value)
    return f"0o{value:o}" if value is not None else None


def _job_or_none(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    job_id = _int_or_none(value[0])
    path = _string_or_none(value[1])
    if not job_id or path in (None, "/"):
        return None
    # The object path is deliberately not copied into facts.  It has operation-local
    # transport lifetime; the numeric job id is useful correlation evidence.
    return {"id": job_id}


def _reactivation_source_evidence(
    properties: dict[str, Any],
) -> tuple[bool, tuple[str, ...]]:
    """Classify only evidence strong enough for a reactivation safety judgement.

    A current Unit.Job is authoritative evidence that the source is changing, regardless
    of its current ActiveState.  Malformed or absent job/state properties are gaps rather
    than fabricated no-job/inactive facts.  The caller keeps relationship and unit identity
    in the gap name so TriggeredBy and UpheldBy remain distinguishable.
    """
    active_state = properties.get("ActiveState")
    gaps: list[str] = []
    active = active_state == "active"
    if not active and active_state in _REACTIVATION_STABLE_NEGATIVE_STATES:
        pass
    elif not active and active_state in _REACTIVATION_CHANGING_STATES:
        gaps.append("state_changing")
    elif not active and isinstance(active_state, str) and active_state:
        gaps.append("state_unknown")
    elif not active:
        gaps.append("state_unreadable")

    job_state = _reactivation_job_state(properties.get("Job"))
    if job_state == "pending":
        gaps.append("job_pending")
    elif job_state == "unreadable":
        gaps.append("job_unreadable")
    return active, tuple(gaps)


def _reactivation_job_state(value: Any) -> str:
    """Return ``none``, ``pending`` or ``unreadable`` for Unit.Job's ``(uo)`` value."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return "unreadable"
    job_id = _int_or_none(value[0])
    path = _string_or_none(value[1])
    if job_id == 0 and path == "/":
        return "none"
    if job_id is not None and job_id > 0 and path not in (None, "/"):
        return "pending"
    return "unreadable"


def _invocation_id(value: Any) -> str | None:
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    elif isinstance(value, (list, tuple)) and all(
        isinstance(item, int) and 0 <= item <= 255 for item in value
    ):
        raw = bytes(value)
    else:
        return None
    return raw.hex() if raw and any(raw) else None


def _safe_mount_source(value: str | None) -> str | None:
    if value is None or "@" not in value:
        return value
    # Credentials occasionally appear as URI/CIFS userinfo.  Keep the authoritative
    # endpoint while refusing to make an observation a credential disclosure surface.
    prefix, at, host = value.rpartition("@")
    if ":" not in prefix:
        return value
    if "://" in prefix:
        scheme = prefix.split("://", 1)[0]
        return f"{scheme}://{host}"
    if prefix.startswith("//"):
        return f"//{host}"
    return "[redacted-userinfo]@" + host


def _safe_list_evidence(row: tuple[Any, ...] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    # ListUnits' object and job paths are deliberately omitted.  The other fields are
    # manager-returned corroboration, not a second normalised state model.
    return {
        "name": str(row[0]) if len(row) > 0 else None,
        "load_state": str(row[2]) if len(row) > 2 else None,
        "active_state": str(row[3]) if len(row) > 3 else None,
        "sub_state": str(row[4]) if len(row) > 4 else None,
        "following": _string_or_none(row[5]) if len(row) > 5 else None,
        "job_id": _int_or_none(row[7]) if len(row) > 7 else None,
        "job_type": _string_or_none(row[8]) if len(row) > 8 else None,
    }


def _introspection_arguments(
    node: ET.Element, *, method: bool
) -> tuple[tuple[str | None, str | None], ...]:
    """Return the D-Bus type/direction contract from one introspection member."""
    return tuple(
        (
            argument.get("type"),
            argument.get("direction", "in") if method else argument.get("direction"),
        )
        for argument in node.findall("arg")
    )


def validate_service_unit_name(unit_id: str) -> None:
    """The one rule about what may be dispatched against, usable on both sides of the agent.

    Public because the agent boundary re-checks every parameter before the provider sees it,
    and two hand-written copies of a name rule are two places for one of them to be fixed.
    It answers only "is this a syntactically valid canonical service unit name" — whether
    such a unit exists, is loaded, or is the one the manager resolves is settled later, by
    the manager itself.
    """
    _validate_unit_name(unit_id)
    if _unit_type(unit_id) != SYSTEMD_LIFECYCLE_UNIT_TYPE:
        raise SystemdInvalidUnitName(
            f"systemd lifecycle acts only on {SYSTEMD_LIFECYCLE_UNIT_TYPE} units: "
            f"{unit_id!r}"
        )


def _not_dispatched(
    action: str,
    unit_id: str,
    reason: str,
    detail: dict[str, Any],
    *,
    invocation_id: str | None = None,
    active_state: str | None = None,
) -> LifecycleDispatchResult:
    """A proof that no job was enqueued, and therefore that the host did not move.

    Reserved for the cases where that is demonstrable, and every one of them is on the near
    side of the dispatch: a request the vocabulary refused before a socket existed, a manager
    that could not be reached at all, a unit that is not loaded or is not the one that was
    named, and an error *reply* to the dispatch call — which systemd sends instead of a job
    path, never as well as one.

    **Once a job path exists this answer is gone**, and it does not come back because a later
    failure happens to look like an early one. :func:`_write_unknown` is the answer after the
    boundary, whatever went wrong.
    """
    return LifecycleDispatchResult(
        outcome="not_written",
        reason=reason,
        action=action,
        unit_id=unit_id,
        invocation_id=invocation_id,
        active_state=active_state,
        detail=detail,
    )


def _write_unknown(
    action: str,
    unit_id: str,
    reason: str,
    detail: dict[str, Any],
    *,
    invocation_id: str | None = None,
    active_state: str | None = None,
) -> LifecycleDispatchResult:
    """The manager may have carried something out and nothing here can say whether it did.

    The answer for everything on the far side of the dispatch: a call that left and came back
    unintelligible, a job whose completion was never heard, a job that completed as anything
    other than ``done``, and any failure at all — closing a match, closing a connection,
    receiving a signal — once a job path has been returned.

    It is not a failure and it is not softened into one. What it obliges is a fresh reading
    and, where that cannot settle it, a person; what it must never become is ``not_written``,
    which is a claim about the host that nothing here has earned.
    """
    return LifecycleDispatchResult(
        outcome="write_unknown",
        reason=reason,
        action=action,
        unit_id=unit_id,
        invocation_id=invocation_id,
        active_state=active_state,
        detail=detail,
    )


def _failure_detail(exc: Exception) -> dict[str, Any]:
    detail: dict[str, Any] = {"error_type": type(exc).__name__}
    if isinstance(exc, DBusErrorResponse):
        detail["dbus_error"] = exc.name
    elif isinstance(exc, OSError):
        detail["errno"] = exc.errno
    elif isinstance(exc, SystemdFailure):
        detail.update(exc.detail)
        detail["reason"] = exc.reason
    else:
        detail["error"] = str(exc)[:512]
    return detail


def _issue(code: str, message: str, detail: dict[str, Any]) -> dict[str, Any]:
    return {"source": SOURCE_SYSTEMD_UNITS, "code": code, "message": message, "detail": detail}


def _failed_batch(
    started_at: str,
    reason: str,
    detail: dict[str, Any],
    *,
    provider_version: str | None = None,
    inventory_limit: int = MAX_UNITS,
) -> UnitBatch:
    return UnitBatch(
        status="failed",
        started_at=started_at,
        completed_at=_now(),
        provider_version=provider_version,
        inventory_limit=inventory_limit,
        inventory_complete=False,
        reason=reason,
        issues=(
            _issue(reason, "the system manager could not provide a loaded-unit inventory", detail),
        ),
        detail={"manager_scope": "system", **detail},
    )


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
