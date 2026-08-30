"""The read-only systemd subsystem, from D-Bus normalisation to stored API reads."""

from __future__ import annotations

import array
import json
import inspect
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from localplane.agent.providers.systemd import (
    MAX_UNITS,
    SUPPORTED_UNIT_SUFFIXES,
    SystemdProvider,
    SYSTEMD_DESTINATION,
    SYSTEMD_MANAGER_PATH,
    _JeepneySystemdConnection,
    _LoadedUnitAbsent,
    _ManagerMessages,
)
from localplane.agent.capabilities import _probe_systemd
from localplane.agent.server import AgentServer
from localplane.agent.service import AgentService
from localplane.backend.app import create_app
from localplane.backend.config import Settings
from localplane.backend.db.database import open_database
from localplane.backend.domain.identity import (
    IdentityBasis,
    OBJECT_KIND_SYSTEMD_UNIT,
    identify_systemd_unit,
)
from localplane.backend.domain.states import HealthState, ManagementState
from localplane.backend.domain.systemd import (
    classify_systemd_management,
    derive_systemd_health,
)
from localplane.backend.ingest import Ingestor
from localplane.protocol.capabilities import (
    CAPABILITY_SYSTEMD_SERVICE_LIFECYCLE,
    CAPABILITY_SYSTEMD_SERVICE_LIFECYCLE_CONTEXT_OBSERVE,
    CAPABILITY_SYSTEMD_UNITS_OBSERVE,
    MUTATING_CAPABILITIES,
    CapabilityStatus,
)
from localplane.protocol.wire import METHODS, MUTATING_METHODS
from tests.conftest import FakeRunner


_MANAGER_LIFECYCLE_INTROSPECTION = """<node>
  <interface name="org.freedesktop.systemd1.Manager">
    <method name="Subscribe"/>
    <method name="GetUnit"><arg type="s" direction="in"/><arg type="o" direction="out"/></method>
    <signal name="JobRemoved"><arg type="u"/><arg type="o"/><arg type="s"/><arg type="s"/></signal>
  </interface>
</node>"""

_UNIT_LIFECYCLE_INTROSPECTION = """<node>
  <interface name="org.freedesktop.systemd1.Unit">
    <method name="Start"><arg type="s" direction="in"/><arg type="o" direction="out"/></method>
    <method name="Stop"><arg type="s" direction="in"/><arg type="o" direction="out"/></method>
    <method name="Restart"><arg type="s" direction="in"/><arg type="o" direction="out"/></method>
  </interface>
</node>"""

_MANAGER_PIDFD_INTROSPECTION = """<node>
  <interface name="org.freedesktop.systemd1.Manager">
    <method name="GetUnitByPIDFD">
      <arg type="h" direction="in"/>
      <arg type="o" direction="out"/>
      <arg type="s" direction="out"/>
      <arg type="ay" direction="out"/>
    </method>
  </interface>
</node>"""


def _unit(
    unit_id: str,
    *,
    active: str = "active",
    sub: str = "running",
    names: list[str] | None = None,
    relationships: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    relationships = relationships or {}
    properties: dict[str, Any] = {
        "Id": unit_id,
        "Names": names or [unit_id],
        "Description": f"Fixture {unit_id}",
        "LoadState": "loaded",
        "ActiveState": active,
        "SubState": sub,
        "UnitFileState": "enabled",
        "UnitFilePreset": "enabled",
        "CanStart": True,
        "CanStop": True,
        "CanReload": False,
        "RefuseManualStart": False,
        "RefuseManualStop": False,
        "NeedDaemonReload": False,
        "FragmentPath": f"/usr/lib/systemd/system/{unit_id}",
        "SourcePath": "",
        "DropInPaths": [],
        "Transient": False,
        "Job": (0, "/"),
        "InvocationID": bytes.fromhex("01" * 16),
    }
    for name in (
        "StateChangeTimestamp", "StateChangeTimestampMonotonic",
        "InactiveExitTimestamp", "InactiveExitTimestampMonotonic",
        "ActiveEnterTimestamp", "ActiveEnterTimestampMonotonic",
        "ActiveExitTimestamp", "ActiveExitTimestampMonotonic",
        "InactiveEnterTimestamp", "InactiveEnterTimestampMonotonic",
    ):
        properties[name] = 100
    for name in (
        "Requires", "Wants", "Requisite", "BindsTo", "PartOf", "Before", "After",
        "Conflicts", "Triggers", "TriggeredBy", "OnFailure", "OnSuccess", "OnFailureOf",
        "OnSuccessOf",
        "Upholds", "UpheldBy", "RequiredBy", "RequisiteOf", "BoundBy", "ConsistsOf",
        "PropagatesStopTo", "StopPropagatedFrom", "ConflictedBy",
    ):
        properties[name] = relationships.get(name, [])
    properties["StopWhenUnneeded"] = False
    return properties


def _service() -> dict[str, Any]:
    return {
        "Type": "simple",
        "MainPID": 42,
        "ControlPID": 0,
        "ExecMainPID": 42,
        "Result": "success",
        "ExecMainCode": 1,
        "ExecMainStatus": 0,
        "Restart": "on-failure",
        "RestartUSec": 1_000_000,
        "NRestarts": 2,
        "RemainAfterExit": False,
        "GuessMainPID": True,
        "ExecMainStartTimestampMonotonic": 500,
        "WatchdogUSec": (1 << 64) - 1,
        "WatchdogTimestampMonotonic": 0,
        "WatchdogSignal": 6,
        "WatchdogPID": 0,
        "ControlGroup": "/system.slice/alpha.service",
        # These are realistic sensitive values returned by GetAll.  The provider's
        # explicit allowlist must make them unreachable outside this fixture.
        "Environment": ["PASSWORD=hunter2"],
        "EnvironmentFiles": [("/run/credentials/secret", False)],
        "ExecStart": [("/bin/sh", ["/bin/sh", "-c", "steal-secret"], False, 0, 0, 0, 0)],
        "ExecStartPre": [("/usr/bin/read-secret", [], False, 0, 0, 0, 0)],
    }


def _typed(unit_type: str) -> dict[str, Any]:
    if unit_type == "service":
        return _service()
    if unit_type == "socket":
        return {
            "Listen": [("Stream", "/run/alpha.sock")], "Accept": False,
            "NAccepted": 3, "NConnections": 1, "NRefused": 0, "Result": "success",
            "TriggerLimitIntervalUSec": 2_000_000, "TriggerLimitBurst": 20,
        }
    if unit_type == "timer":
        return {
            "Unit": "alpha.service", "NextElapseUSecRealtime": 1_000,
            "NextElapseUSecMonotonic": 900, "LastTriggerUSec": 800,
            "LastTriggerUSecMonotonic": 700, "Persistent": True,
            "RandomizedDelayUSec": 50, "FixedRandomDelay": False,
            "AccuracyUSec": 1_000_000, "Result": "success",
        }
    if unit_type == "path":
        return {
            "Unit": "alpha.service", "Paths": [("PathChanged", "/srv/input")],
            "MakeDirectory": False, "DirectoryMode": 0o755, "Result": "success",
        }
    if unit_type == "mount":
        return {
            "What": "smb://user:password@server/share", "Where": "/srv/share",
            "Type": "cifs", "Options": "username=user,password=secret", "ControlPID": 0,
            "DirectoryMode": 0o755, "Result": "success", "SloppyOptions": False,
            "LazyUnmount": False, "ForceUnmount": False, "TimeoutUSec": 5_000_000,
        }
    return {}


@dataclass
class FakeSystemdState:
    units: dict[str, dict[str, Any]]
    typed: dict[str, dict[str, Any]]
    aliases: dict[str, str] = field(default_factory=dict)
    manager: dict[str, Any] = field(
        default_factory=lambda: {
            "Version": "249.11-fixture", "SystemState": "running", "Features": "+PAM",
            "Virtualization": "kvm", "Architecture": "x86-64", "Tainted": "",
        }
    )
    calls: list[tuple[Any, ...]] = field(default_factory=list)
    timeouts: list[float | None] = field(default_factory=list)
    fail_units: set[str] = field(default_factory=set)
    manager_error: Exception | None = None
    manager_lifecycle_introspection: str = _MANAGER_LIFECYCLE_INTROSPECTION
    unit_lifecycle_introspection: str = _UNIT_LIFECYCLE_INTROSPECTION
    self_path: str | None = None
    connection_count: int = 0

    def path_for(self, name: str) -> str:
        canonical = self.aliases.get(name, name)
        return f"/org/freedesktop/systemd1/unit/{canonical.replace('.', '_2e').replace('@', '_40')}"


class FakeSystemdConnection:
    def __init__(self, state: FakeSystemdState) -> None:
        self.state = state

    def __enter__(self):
        self.state.connection_count += 1
        self.state.calls.append(("connect",))
        return self

    def __exit__(self, *args):
        self.state.calls.append(("close",))

    def manager_properties(self, timeout_s: float | None = None):
        self.state.calls.append(("manager_properties",))
        self.state.timeouts.append(timeout_s)
        if self.state.manager_error:
            raise self.state.manager_error
        return dict(self.state.manager)

    def manager_introspection(self, timeout_s: float | None = None):
        self.state.calls.append(("manager_introspect",))
        self.state.timeouts.append(timeout_s)
        return self.state.manager_lifecycle_introspection

    def unit_introspection(self, object_path: str, timeout_s: float | None = None):
        self.state.calls.append(("unit_introspect", object_path))
        self.state.timeouts.append(timeout_s)
        return self.state.unit_lifecycle_introspection

    def list_loaded_units(self, timeout_s: float | None = None):
        self.state.calls.append(("list_units_by_patterns", tuple(f"*{s}" for s in SUPPORTED_UNIT_SUFFIXES)))
        self.state.timeouts.append(timeout_s)
        rows = []
        for unit_id, properties in self.state.units.items():
            rows.append(
                (
                    unit_id, properties.get("Description", ""), properties.get("LoadState", ""),
                    properties.get("ActiveState", ""), properties.get("SubState", ""), "",
                    self.state.path_for(unit_id), 0, "", "/",
                )
            )
        return rows, "manager_list_units_by_patterns"

    def get_unit_path(self, unit_id: str, timeout_s: float | None = None):
        self.state.calls.append(("get_unit", unit_id))
        canonical = self.state.aliases.get(unit_id, unit_id)
        if canonical not in self.state.units:
            raise _LoadedUnitAbsent(unit_id)
        return self.state.path_for(canonical)

    def get_unit_by_control_group(self, cgroup: str, timeout_s: float | None = None):
        self.state.calls.append(("get_unit_by_control_group", cgroup))
        if self.state.self_path is None:
            raise OSError("not resolved")
        return self.state.self_path

    def unit_properties(self, object_path: str, timeout_s: float | None = None):
        self.state.calls.append(("unit_get_all", object_path))
        self.state.timeouts.append(timeout_s)
        canonical = self._canonical(object_path)
        if canonical in self.state.fail_units:
            raise RuntimeError("unit read failed")
        return dict(self.state.units[canonical])

    def type_properties(
        self, object_path: str, unit_type: str, timeout_s: float | None = None
    ):
        self.state.calls.append(("type_get_all", object_path, unit_type))
        self.state.timeouts.append(timeout_s)
        return dict(self.state.typed.get(self._canonical(object_path), {}))

    def _canonical(self, object_path: str) -> str:
        for canonical in self.state.units:
            if self.state.path_for(canonical) == object_path:
                return canonical
        raise KeyError(object_path)


def _estate() -> FakeSystemdState:
    units = {
        "alpha.service": _unit(
            "alpha.service", names=["alpha.service", "alpha-alias.service"],
            relationships={"After": ["network.target"], "Requires": ["network.target"]},
        ),
        "alpha.socket": _unit(
            "alpha.socket", sub="listening", relationships={"Triggers": ["alpha-alias.service"]}
        ),
        "alpha.timer": _unit("alpha.timer", sub="waiting"),
        "basic.target": _unit("basic.target", sub="active"),
        "alpha.path": _unit("alpha.path", sub="waiting"),
        "srv-share.mount": _unit("srv-share.mount", sub="mounted"),
        # Out-of-scope types are returned by the fallback-shaped fixture and filtered.
        "dev-sda.device": _unit("dev-sda.device"),
    }
    typed = {
        name: _typed(name.rsplit(".", 1)[1])
        for name in units
    }
    state = FakeSystemdState(units=units, typed=typed, aliases={"alpha-alias.service": "alpha.service"})
    state.self_path = state.path_for("alpha.service")
    return state


def _provider(state: FakeSystemdState, **kwargs: Any) -> SystemdProvider:
    return SystemdProvider(
        runtime_path=None,
        connection_factory=lambda: FakeSystemdConnection(state),
        **kwargs,
    )


def test_manager_and_inventory_use_one_closed_shared_connection():
    state = _estate()
    batch = _provider(state).observe_units()
    assert batch.status == "ok"
    assert batch.provider_version == "249.11-fixture"
    assert batch.listed_count == batch.selected_count == len(batch.units) == 6
    assert batch.inventory_complete is True and batch.truncated is False
    assert state.connection_count == 1
    assert sum(call[0] == "list_units_by_patterns" for call in state.calls) == 1
    assert all(timeout is not None and 0 < timeout <= 5 for timeout in state.timeouts)
    assert {unit.facts["unit_type"] for unit in batch.units} == {
        "service", "socket", "timer", "target", "path", "mount"
    }


def test_service_normalisation_and_sentinels_are_localplane_owned():
    unit = _provider(_estate()).observe_unit("alpha.service").unit
    assert unit is not None
    facts = unit.facts
    assert facts["canonical_id"] == "alpha.service"
    assert facts["current_job"] is None
    assert facts["source_path"] is None
    assert facts["invocation_id"] == "01" * 16
    assert facts["service"]["main_pid"] == 42
    assert facts["service"]["control_pid"] is None
    assert facts["service"]["watchdog_usec"] is None
    assert facts["service"]["watchdog_timestamp_monotonic"] is None


def test_each_type_has_only_its_own_small_type_specific_model():
    units = {u.canonical_id: u.facts for u in _provider(_estate()).observe_units().units}
    assert units["alpha.socket"]["socket"]["listen"] == [
        {"kind": "Stream", "address": "/run/alpha.sock"}
    ]
    assert units["alpha.timer"]["timer"]["unit"] == "alpha.service"
    assert units["alpha.path"]["path"]["paths"][0]["path"] == "/srv/input"
    assert units["srv-share.mount"]["mount"]["where"] == "/srv/share"
    assert units["srv-share.mount"]["mount"]["what"] == "smb://server/share"
    assert "service" not in units["alpha.socket"]
    assert not any(key in units["basic.target"] for key in ("service", "socket", "timer", "path", "mount"))


def test_sensitive_execution_and_mount_configuration_never_survives_normalisation():
    rendered = json.dumps(_provider(_estate()).observe_units().as_dict())
    for secret in ("hunter2", "PASSWORD", "steal-secret", "read-secret", "password=secret"):
        assert secret not in rendered
    for raw_field in ("EnvironmentFiles", "ExecStartPre", "ExecStart", "Options"):
        assert f'"{raw_field}"' not in rendered
    assert "/org/freedesktop/systemd1/unit/" not in rendered


def test_relationship_kinds_and_groups_are_not_flattened():
    units = {u.canonical_id: u.facts for u in _provider(_estate()).observe_units().units}
    service = units["alpha.service"]["relationships"]
    assert {r["kind"]: r["group"] for r in service} == {
        "Requires": "requirement", "After": "ordering"
    }
    socket = units["alpha.socket"]["relationships"]
    assert socket == [{"kind": "Triggers", "group": "activation", "target_unit": "alpha-alias.service"}]
    assert not any(r["kind"] == "After" and r["group"] == "requirement" for r in service)


def test_an_older_reduced_property_set_is_partial_not_a_failed_unit():
    state = _estate()
    state.manager = {"Version": "239", "SystemState": "running"}
    state.units["alpha.service"] = {
        "Id": "alpha.service", "LoadState": "loaded",
        "ActiveState": "future-active-state", "SubState": "future-sub-state",
    }
    state.typed["alpha.service"] = {"Type": "simple", "Result": "future-result"}
    target = _provider(state).observe_unit("alpha.service")
    assert target.status == "observed"
    assert target.unit is not None and target.unit.fidelity == "partial"
    assert target.unit.facts["active_state"] == "future-active-state"
    assert target.unit.facts["service"]["result"] == "future-result"
    assert "unit.UnitFilePreset" in target.unit.gaps
    assert "service.MainPID" in target.unit.gaps
    assert derive_systemd_health(target.unit.facts).state is HealthState.UNKNOWN


def test_missing_optional_manager_metadata_does_not_make_inventory_partial():
    state = _estate()
    state.manager = {"Version": "239", "SystemState": "running"}
    batch = _provider(state).observe_units()
    assert batch.status == "ok"
    assert batch.inventory_complete is True
    assert len(batch.units) == batch.selected_count == 6
    optional = next(
        issue for issue in batch.issues
        if issue["code"] == "manager_optional_metadata_missing"
    )
    assert set(optional["detail"]["gaps"]) == {
        "manager.Features", "manager.Virtualization", "manager.Architecture", "manager.Tainted"
    }


def test_inventory_passes_each_dbus_call_only_the_remaining_deadline_budget():
    state = _estate()
    clock = [0.0]
    received_timeouts: list[float] = []

    class DeadlineConnection(FakeSystemdConnection):
        def _spend(self, timeout_s: float | None, cost: float) -> None:
            assert timeout_s is not None and timeout_s > 0
            received_timeouts.append(timeout_s)
            if cost > timeout_s:
                clock[0] += timeout_s
                raise TimeoutError("simulated D-Bus call reached its supplied timeout")
            clock[0] += cost

        def manager_properties(self, timeout_s: float | None = None):
            self._spend(timeout_s, 0.2)
            return dict(self.state.manager)

        def list_loaded_units(self, timeout_s: float | None = None):
            self._spend(timeout_s, 0.2)
            return super().list_loaded_units(timeout_s)

        def unit_properties(self, object_path: str, timeout_s: float | None = None):
            self._spend(timeout_s, 0.6)
            return super().unit_properties(object_path, timeout_s)

        def type_properties(
            self, object_path: str, unit_type: str, timeout_s: float | None = None
        ):
            self._spend(timeout_s, 0.6)
            return super().type_properties(object_path, unit_type, timeout_s)

    batch = SystemdProvider(
        runtime_path=None,
        timeout_s=5,
        inventory_timeout_s=1.5,
        monotonic=lambda: clock[0],
        connection_factory=lambda: DeadlineConnection(state),
    ).observe_units()
    assert batch.status == "partial"
    assert batch.inventory_complete is False
    assert {issue["code"] for issue in batch.issues} >= {"inventory_timeout"}
    assert clock[0] == pytest.approx(1.5)
    assert received_timeouts == sorted(received_timeouts, reverse=True)
    assert batch.detail["deadline_enforcement"] == "remaining_time_per_dbus_call"


def test_inventory_limit_and_per_unit_failure_are_truthful_partial_results(
    fake_root: Path,
    populated_sysfs: Path,
    working_runner: FakeRunner,
    absent_docker: Path,
):
    state = _estate()
    state.fail_units.add("alpha.socket")
    batch = _provider(state, max_units=4).observe_units()
    assert batch.status == "partial"
    assert batch.truncated is True and batch.inventory_complete is False
    assert batch.listed_count == 6 and batch.selected_count == 4
    assert batch.inventory_limit == 4
    assert batch.as_dict()["inventory_limit"] == 4
    assert {issue["code"] for issue in batch.issues} >= {
        "inventory_limit_reached", "unit_read_failed"
    }

    service = AgentService(
        root=fake_root,
        sysfs_net=populated_sysfs,
        runner=working_runner,
        docker_socket=absent_docker,
        systemd_provider=_provider(state, max_units=4),
    )
    wire_batch = service.handle("systemd.observe_units", {})["units"]
    assert wire_batch["inventory_limit"] == 4
    assert wire_batch["listed_count"] == 6
    assert wire_batch["selected_count"] == 4
    assert wire_batch["truncated"] is True


def test_provider_unavailable_is_not_an_empty_success(tmp_path: Path):
    result = SystemdProvider(
        runtime_path=tmp_path / "not-systemd", max_units=4
    ).observe_units()
    assert result.status == "failed"
    assert result.inventory_complete is False
    assert result.reason == "systemd_system_manager_absent"
    assert result.units == ()
    assert result.inventory_limit == 4
    assert result.as_dict()["inventory_limit"] == 4


def test_capability_is_three_valued_from_the_manager_probe_not_the_distro():
    available = _probe_systemd(_provider(_estate()))
    assert available.status is CapabilityStatus.AVAILABLE
    assert available.mutating is False

    reduced = _estate()
    reduced.manager = {"Version": "239"}
    degraded = _probe_systemd(_provider(reduced))
    assert degraded.status is CapabilityStatus.DEGRADED
    assert "manager.SystemState" in degraded.detail["gaps"]

    unavailable_state = _estate()
    unavailable_state.manager_error = OSError("system bus absent")
    unavailable = _probe_systemd(_provider(unavailable_state))
    assert unavailable.status is CapabilityStatus.UNAVAILABLE
    assert unavailable.reason == "system_manager_unavailable"


def test_targeted_observation_is_get_unit_only_and_preserves_alias_identity():
    state = _estate()
    target = _provider(state).observe_unit("alpha-alias.service")
    assert target.status == "observed"
    assert target.unit is not None and target.unit.canonical_id == "alpha.service"
    assert ("get_unit", "alpha-alias.service") in state.calls
    assert not any(call[0] == "list_units_by_patterns" for call in state.calls)
    assert not any("load" in call[0].lower() for call in state.calls)


def test_targeted_absence_and_read_failure_are_distinct():
    absent = _provider(_estate()).observe_unit("not-loaded.service")
    assert (absent.status, absent.reason) == ("absent", "loaded_unit_absent")

    state = _estate()
    state.fail_units.add("alpha.service")
    failed = _provider(state).observe_unit("alpha.service")
    assert (failed.status, failed.reason) == ("failed", "targeted_unit_read_failed")
    assert failed.status != absent.status


def test_agent_unit_resolution_uses_proc_cgroup_and_get_unit_by_control_group(tmp_path: Path):
    cgroup = tmp_path / "cgroup"
    cgroup.write_text("0::/system.slice/alpha.service\n")
    state = _estate()
    result = SystemdProvider(
        runtime_path=None,
        proc_cgroup=cgroup,
        connection_factory=lambda: FakeSystemdConnection(state),
    ).resolve_current_process_unit()
    assert result.status == "resolved"
    assert result.canonical_id == "alpha.service"
    assert result.cgroup == "/system.slice/alpha.service"
    assert ("get_unit_by_control_group", "/system.slice/alpha.service") in state.calls
    assert not any(call[0] in {"process_name", "guess_unit"} for call in state.calls)


def test_scope_may_be_reported_as_self_evidence_without_entering_the_unit_inventory(tmp_path: Path):
    cgroup = tmp_path / "cgroup"
    cgroup.write_text("0::/user.slice/session-7.scope\n")
    state = _estate()
    state.units["session-7.scope"] = _unit("session-7.scope", sub="running")
    state.self_path = state.path_for("session-7.scope")
    provider = SystemdProvider(
        runtime_path=None, proc_cgroup=cgroup,
        connection_factory=lambda: FakeSystemdConnection(state),
    )
    assert provider.resolve_current_process_unit().canonical_id == "session-7.scope"
    assert "session-7.scope" not in {unit.canonical_id for unit in provider.observe_units().units}


class _PidfdSystemdConnection(FakeSystemdConnection):
    def __init__(
        self,
        state: FakeSystemdState,
        *,
        returned_id: str = "alpha.service",
        returned_invocation: bytes = bytes.fromhex("01" * 16),
        failure: Exception | None = None,
    ) -> None:
        super().__init__(state)
        self.returned_id = returned_id
        self.returned_invocation = returned_invocation
        self.failure = failure

    def get_unit_by_pidfd(self, pidfd: int):
        self.state.calls.append(("get_unit_by_pidfd", pidfd))
        if self.failure is not None:
            raise self.failure
        return (
            self.state.path_for("alpha.service"),
            self.returned_id,
            self.returned_invocation,
        )


def test_get_unit_by_pidfd_contract_is_exactly_feature_detected():
    state = _estate()
    state.manager_lifecycle_introspection = _MANAGER_PIDFD_INTROSPECTION
    available = _provider(state).read_pidfd_unit_contract()
    assert available.status == "available"
    assert available.detail == {
        "manager_scope": "system",
        "method": "GetUnitByPIDFD",
        "signature": "h->osay",
        "unix_fd_transport_required": True,
        "mutation_invoked": False,
    }

    state.manager_lifecycle_introspection = _MANAGER_PIDFD_INTROSPECTION.replace(
        '<arg type="ay" direction="out"/>',
        '<arg type="u" direction="out"/>',
    )
    unsupported = _provider(state).read_pidfd_unit_contract()
    assert (unsupported.status, unsupported.reason) == (
        "unavailable",
        "systemd_get_unit_by_pidfd_unsupported",
    )


def test_pidfd_resolution_uses_all_three_results_and_canonical_unit_properties():
    state = _estate()
    provider = SystemdProvider(
        runtime_path=None,
        connection_factory=lambda: FakeSystemdConnection(state),
        pidfd_connection_factory=lambda: _PidfdSystemdConnection(state),
    )
    result = provider.resolve_process_unit_pidfd(77)
    assert result.status == "resolved"
    assert result.canonical_id == "alpha.service"
    assert result.invocation_id == "01" * 16
    assert result.unit is not None
    assert result.unit.facts["canonical_id"] == "alpha.service"
    assert result.unit.facts["invocation_id"] == "01" * 16
    assert ("get_unit_by_pidfd", 77) in state.calls
    assert result.detail["unix_fd_transport"] is True
    assert result.detail["transport_handle_persisted"] is False


@pytest.mark.parametrize(
    ("returned_id", "returned_invocation"),
    [
        ("alpha-alias.service", bytes.fromhex("01" * 16)),
        ("alpha.service", bytes.fromhex("02" * 16)),
        ("alpha.service", bytes(16)),
    ],
)
def test_pidfd_resolution_rejects_alias_or_invocation_disagreement(
    returned_id: str, returned_invocation: bytes
):
    state = _estate()
    provider = SystemdProvider(
        runtime_path=None,
        connection_factory=lambda: FakeSystemdConnection(state),
        pidfd_connection_factory=lambda: _PidfdSystemdConnection(
            state,
            returned_id=returned_id,
            returned_invocation=returned_invocation,
        ),
    )
    result = provider.resolve_process_unit_pidfd(77)
    assert (result.status, result.reason) == ("failed", "pidfd_unit_identity_mismatch")


@pytest.mark.parametrize("failure", [TimeoutError("timeout"), PermissionError("denied")])
def test_pidfd_resolution_timeout_or_permission_failure_is_not_authority(
    failure: Exception,
):
    state = _estate()
    provider = SystemdProvider(
        runtime_path=None,
        connection_factory=lambda: FakeSystemdConnection(state),
        pidfd_connection_factory=lambda: _PidfdSystemdConnection(
            state, failure=failure
        ),
    )
    result = provider.resolve_process_unit_pidfd(77)
    assert (result.status, result.reason) == ("failed", "pidfd_unit_resolution_failed")


def test_jeepney_090_serializes_h_as_index_and_transmits_real_scm_rights_fd():
    from jeepney.io.blocking import DBusConnectionBase
    from jeepney.low_level import HeaderFields, Message

    pidfd = os.pidfd_open(os.getpid())
    sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    connection = DBusConnectionBase(sender, enable_fds=True)
    received_fd: int | None = None
    try:
        message = _ManagerMessages(
            SYSTEMD_MANAGER_PATH,
            SYSTEMD_DESTINATION,
        ).get_unit_by_pidfd(pidfd)
        encoded, descriptors = connection._serialise(message, serial=7)
        assert descriptors is not None and list(descriptors) == [pidfd]
        marker = object()
        parsed = Message.from_buffer(encoded, fds=(marker,))
        assert parsed.body == (marker,)
        assert parsed.header.fields[HeaderFields.unix_fds] == 1

        connection._send_with_fds(encoded, descriptors)
        _data, ancillary, flags, _address = receiver.recvmsg(
            len(encoded) + 64,
            socket.CMSG_SPACE(array.array("i").itemsize),
        )
        assert not flags & getattr(socket, "MSG_CTRUNC", 0)
        transferred = array.array("i")
        for level, kind, payload in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                transferred.frombytes(payload[: len(payload) - len(payload) % transferred.itemsize])
        assert len(transferred) == 1
        received_fd = transferred[0]
        assert os.readlink(f"/proc/self/fd/{received_fd}") == "anon_inode:[pidfd]"
        assert f"Pid:\t{os.getpid()}" in Path(
            f"/proc/self/fdinfo/{received_fd}"
        ).read_text(encoding="utf-8")
    finally:
        if received_fd is not None:
            os.close(received_fd)
        connection.close()
        receiver.close()
        os.close(pidfd)


def test_systemd_pidfd_adapter_enables_unix_fd_negotiation_only_on_that_connection(
    monkeypatch: pytest.MonkeyPatch,
):
    import localplane.agent.providers.systemd as systemd_module

    observed: list[bool] = []

    class RawConnection:
        def close(self):
            pass

    monkeypatch.setattr(
        systemd_module,
        "_open_bounded_system_bus",
        lambda _timeout, *, enable_fds=False: (
            observed.append(enable_fds) or RawConnection()
        ),
    )
    with _JeepneySystemdConnection(1, enable_fds=True):
        pass
    with _JeepneySystemdConnection(1, enable_fds=False):
        pass
    assert observed == [True, False]


def test_canonical_identity_ignores_alias_pid_invocation_and_object_path():
    identity = identify_systemd_unit("host-a", "alpha.service")
    assert identity.basis is IdentityBasis.PROVIDER_ID
    assert identity.value == "alpha.service"
    assert identity == identify_systemd_unit("host-a", "alpha.service")
    assert identity.object_id != identify_systemd_unit("host-a", "alpha-alias.service").object_id
    assert identity.object_id != identify_systemd_unit("host-b", "alpha.service").object_id


@pytest.mark.parametrize(
    "facts,state,reason",
    [
        ({"unit_type": "service", "load_state": "loaded", "active_state": "active", "sub_state": "running", "service": {"result": "success"}}, HealthState.HEALTHY, "service_running"),
        ({"unit_type": "service", "load_state": "loaded", "active_state": "active", "sub_state": "running", "service": {"result": "future-result"}}, HealthState.UNKNOWN, "unrecognised_service_result:future-result"),
        ({"unit_type": "service", "load_state": "loaded", "active_state": "active", "sub_state": "exited", "service": {"type": "oneshot", "remain_after_exit": True, "result": "success"}}, HealthState.HEALTHY, "oneshot_completed_and_retained"),
        ({"unit_type": "service", "load_state": "loaded", "active_state": "inactive", "sub_state": "dead", "service": {}}, HealthState.INACTIVE, "inactive:dead"),
        ({"unit_type": "service", "load_state": "loaded", "active_state": "failed", "sub_state": "failed", "service": {}}, HealthState.FAILED, "failed:failed"),
        ({"unit_type": "socket", "load_state": "loaded", "active_state": "active", "sub_state": "listening"}, HealthState.HEALTHY, "socket_listening"),
        ({"unit_type": "timer", "load_state": "loaded", "active_state": "active", "sub_state": "waiting"}, HealthState.HEALTHY, "timer_waiting"),
        ({"unit_type": "path", "load_state": "loaded", "active_state": "active", "sub_state": "waiting"}, HealthState.HEALTHY, "path_waiting"),
        ({"unit_type": "mount", "load_state": "loaded", "active_state": "active", "sub_state": "mounted"}, HealthState.HEALTHY, "mount_mounted"),
        ({"unit_type": "service", "load_state": "loaded", "active_state": "activating", "sub_state": "start", "service": {}}, HealthState.DEGRADED, "transition:activating:start"),
    ],
)
def test_health_is_unit_type_aware(facts, state, reason):
    verdict = derive_systemd_health(facts)
    assert (verdict.state, verdict.reason) == (state, reason)


def test_socket_activation_context_keeps_an_inactive_service_neutral():
    facts = {"unit_type": "service", "load_state": "loaded", "active_state": "inactive", "sub_state": "dead", "service": {}}
    verdict = derive_systemd_health(facts, active_socket_triggers=["alpha.socket"])
    assert (verdict.state, verdict.reason) == (HealthState.INACTIVE, "socket_activated_waiting")
    assert classify_systemd_management(facts).state is ManagementState.OBSERVE_ONLY


def test_systemd_has_four_read_methods_and_exactly_one_that_can_write():
    """The whole systemd surface of the protocol, named rather than counted.

    Four questions and one instruction. The instruction arrived with the systemd transport and
    it is the only one there will be: a second systemd method that could change a host would
    be a visible change here as well as in the protocol.

    Observation stays non-mutating and says so; asking about the lifecycle *context* stays
    non-mutating too, because reading what an action would affect is not performing it.
    """
    assert {
        "systemd.observe_units", "systemd.observe_unit", "systemd.resolve_agent_unit",
        "systemd.observe_lifecycle_context",
    } <= METHODS
    assert {
        method for method in METHODS if method.startswith("systemd.")
    } & MUTATING_METHODS == {"systemd.service_lifecycle"}
    assert CAPABILITY_SYSTEMD_UNITS_OBSERVE not in MUTATING_CAPABILITIES
    assert CAPABILITY_SYSTEMD_SERVICE_LIFECYCLE in MUTATING_CAPABILITIES
    assert CAPABILITY_SYSTEMD_SERVICE_LIFECYCLE_CONTEXT_OBSERVE not in MUTATING_CAPABILITIES
    assert "systemd.unit.lifecycle" not in MUTATING_CAPABILITIES
    # Still absent, and each for its own reason: LocalPlane does not preflight PolicyKit,
    # does not reload the manager, and does not edit or mask unit files.
    for absent in (
        "systemd.check_unit_authorization", "systemd.daemon_reload", "systemd.enable_unit",
        "systemd.disable_unit", "systemd.mask_unit", "systemd.reset_failed",
        "systemd.kill_unit", "systemd.set_unit_properties", "systemd.call",
    ):
        assert absent not in METHODS


def test_provider_has_no_generic_dbus_or_execution_surface():
    public = {
        name: tuple(inspect.signature(getattr(SystemdProvider, name)).parameters)
        for name in ("read_manager", "observe_units", "observe_unit", "resolve_current_process_unit")
    }
    assert public == {
        "read_manager": ("self",),
        "observe_units": ("self",),
        "observe_unit": ("self", "unit_id"),
        "resolve_current_process_unit": ("self",),
    }
    source = inspect.getsource(SystemdProvider)
    assert "subprocess" not in source
    assert "systemctl" not in source
    assert "busctl" not in source
    for caller_field in ("destination", "object_path", "interface", "member", "signature", "argv", "command"):
        assert caller_field not in public["observe_unit"]


def test_ingestion_collapses_aliases_and_resolves_relationships_without_placeholder_objects(
    database, fake_root: Path, populated_sysfs: Path, working_runner: FakeRunner, absent_docker: Path
):
    state = _estate()
    service = AgentService(
        root=fake_root, sysfs_net=populated_sysfs, runner=working_runner,
        docker_socket=absent_docker, systemd_provider=_provider(state),
    )
    ingestor = Ingestor(database)
    ingestor.ingest_handshake(service.handle("agent.hello", {}))
    payload = service.handle("systemd.observe_units", {})
    payload["agent_unit_resolution"] = service.handle("systemd.resolve_agent_unit", {})["resolution"]
    result = ingestor.ingest_systemd_sweep(payload)
    records = ingestor.objects.list_by_kind(result.host_id, OBJECT_KIND_SYSTEMD_UNIT)
    assert len(records) == 6
    alpha = next(record for record in records if record.identity_value == "alpha.service")
    socket = next(record for record in records if record.identity_value == "alpha.socket")
    trigger = next(r for r in socket.observation.facts["relationships"] if r["kind"] == "Triggers")
    assert trigger["canonical_target"] == "alpha.service"
    assert trigger["target_object_id"] == alpha.object_id
    assert trigger["resolution"] == "resolved"
    assert trigger["estate_state"] == "current"
    assert alpha.observation.facts["agent_process_containment"]["method"] == "manager_get_unit_by_control_group"
    assert not any(record.identity_value == "alpha-alias.service" for record in records)


def test_historical_object_existence_does_not_resolve_a_current_relationship(
    database, fake_root: Path, populated_sysfs: Path,
    working_runner: FakeRunner, absent_docker: Path,
):
    state = _estate()
    service = AgentService(
        root=fake_root, sysfs_net=populated_sysfs, runner=working_runner,
        docker_socket=absent_docker, systemd_provider=_provider(state),
    )
    ingestor = Ingestor(database)
    ingestor.ingest_handshake(service.handle("agent.hello", {}))
    first = ingestor.ingest_systemd_sweep(service.handle("systemd.observe_units", {}))
    first_socket = next(
        record for record in ingestor.objects.list_by_kind(first.host_id, OBJECT_KIND_SYSTEMD_UNIT)
        if record.identity_value == "alpha.socket"
    )
    initially = next(
        edge for edge in first_socket.observation.facts["relationships"]
        if edge["kind"] == "Triggers"
    )
    historical_service_id = initially["target_object_id"]
    assert initially["resolution"] == "resolved"

    del state.units["alpha.service"]
    del state.typed["alpha.service"]
    state.units["alpha.socket"]["Triggers"] = ["alpha.service"]
    second = ingestor.ingest_systemd_sweep(service.handle("systemd.observe_units", {}))
    current = ingestor.sweeps.latest(
        second.host_id, CAPABILITY_SYSTEMD_UNITS_OBSERVE, scope="inventory"
    )
    assert current is not None and current.sweep_id == second.sweep_id
    second_socket = next(
        record for record in ingestor.objects.list_by_kind(second.host_id, OBJECT_KIND_SYSTEMD_UNIT)
        if record.identity_value == "alpha.socket"
    )
    edge = next(
        relationship for relationship in second_socket.observation.facts["relationships"]
        if relationship["kind"] == "Triggers"
    )
    assert edge["resolution"] == "referenced"
    assert edge["estate_state"] == "not_observed"
    assert edge["target_object_id"] == historical_service_id
    assert historical_service_id not in ingestor.sweeps.object_ids(second.sweep_id)


@pytest.fixture
def systemd_api(
    tmp_path: Path, fake_root: Path, populated_sysfs: Path,
    working_runner: FakeRunner, absent_docker: Path,
) -> Iterator[tuple[TestClient, FakeSystemdState]]:
    state = _estate()
    proc = tmp_path / "proc-cgroup"
    proc.write_text("0::/system.slice/alpha.service\n")
    provider = SystemdProvider(
        runtime_path=None, proc_cgroup=proc,
        connection_factory=lambda: FakeSystemdConnection(state),
    )
    service = AgentService(
        root=fake_root, sysfs_net=populated_sysfs, runner=working_runner,
        docker_socket=absent_docker, systemd_provider=provider,
    )
    server = AgentServer(tmp_path / "agent" / "agent.sock", service)
    thread = server.serve_in_thread()
    settings = Settings(
        database_path=tmp_path / "systemd.db", agent_socket=server.socket_path,
        agent_timeout_s=20, freshness_ttl_s=60, log_level="WARNING", observe_on_startup=False,
    )
    database = open_database(settings.database_path)
    with TestClient(create_app(settings, database)) as client:
        yield client, state
    database.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_systemd_api_refreshes_explicitly_and_gets_are_store_only(systemd_api):
    client, state = systemd_api
    refresh = client.post("/api/v1/systemd/observations/refresh")
    assert refresh.status_code == 200
    assert refresh.json()["unit_count"] == 6
    calls_after_refresh = list(state.calls)

    listing = client.get("/api/v1/systemd/units")
    assert listing.status_code == 200
    body = listing.json()
    assert body["count"] == 6
    assert body["capability"]["capability"] == "systemd.units.observe"
    alpha = next(unit for unit in body["units"] if unit["canonical_id"] == "alpha.service")
    assert alpha["identity"]["value"] == "alpha.service"
    assert alpha["health"]["state"] == "healthy"
    assert client.get(f"/api/v1/systemd/units/{alpha['object_id']}").status_code == 200
    assert client.get("/api/v1/systemd/units/alpha.service").status_code == 404
    assert state.calls == calls_after_refresh, "a GET contacted the host"


def test_targeted_systemd_read_updates_one_object_without_replacing_estate_sweep(systemd_api):
    client, _ = systemd_api
    full = client.post("/api/v1/systemd/observations/refresh").json()
    before = client.get("/api/v1/systemd/units").json()
    assert before["last_sweep"]["sweep_id"] == full["sweep_id"]
    assert before["last_sweep"]["scope"] == "inventory"

    targeted = client.app.state.context.coordinator.refresh_systemd_unit("alpha.service")
    assert targeted.detail["scope"] == "targeted"
    after = client.get("/api/v1/systemd/units").json()
    assert after["last_sweep"]["sweep_id"] == full["sweep_id"]
    alpha = next(unit for unit in after["units"] if unit["canonical_id"] == "alpha.service")
    other = next(unit for unit in after["units"] if unit["canonical_id"] == "alpha.socket")
    assert alpha["observation"]["sweep_id"] == targeted.sweep_id
    assert alpha["observed_in_latest_sweep"] is True
    assert other["observed_in_latest_sweep"] is True
    latest_any = client.app.state.context.ingestor.sweeps.latest(
        after["host_id"], CAPABILITY_SYSTEMD_UNITS_OBSERVE
    )
    assert latest_any is not None and latest_any.sweep_id == targeted.sweep_id
    assert latest_any.scope == "targeted"

    targeted_socket = client.app.state.context.coordinator.refresh_systemd_unit("alpha.socket")
    stored_socket = client.get(f"/api/v1/systemd/units/{other['object_id']}").json()
    trigger = next(
        edge for edge in stored_socket["relationships"] if edge["kind"] == "Triggers"
    )
    assert stored_socket["observation"]["sweep_id"] == targeted_socket.sweep_id
    assert trigger["canonical_target"] == "alpha.service"
    assert trigger["resolution"] == "resolved"
    assert trigger["estate_state"] == "current"


def test_self_unit_gap_does_not_downgrade_a_complete_inventory(systemd_api):
    client, state = systemd_api
    state.self_path = None
    response = client.post("/api/v1/systemd/observations/refresh")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["inventory_complete"] is True
    assert body["agent_unit_resolution"]["status"] == "failed"
    assert {issue["source"] for issue in body["issues"]} >= {"systemd.agent_unit"}
    listing = client.get("/api/v1/systemd/units").json()
    assert listing["last_sweep"]["status"] == "ok"
    assert listing["last_sweep"]["scope"] == "inventory"


def test_socket_activated_health_uses_only_current_estate_context(systemd_api):
    client, state = systemd_api
    state.units["alpha.service"]["ActiveState"] = "inactive"
    state.units["alpha.service"]["SubState"] = "dead"
    assert client.post("/api/v1/systemd/observations/refresh").status_code == 200
    listing = client.get("/api/v1/systemd/units").json()
    alpha = next(unit for unit in listing["units"] if unit["canonical_id"] == "alpha.service")
    assert alpha["health"] == {
        "state": "inactive",
        "reason": "socket_activated_waiting",
    }


def test_a_systemd_lifecycle_run_persists_its_proof_and_stays_blocked_over_loopback(
    systemd_api,
):
    client, state = systemd_api
    assert client.post("/api/v1/systemd/observations/refresh").status_code == 200
    listing = client.get("/api/v1/systemd/units").json()
    target = next(
        unit for unit in listing["units"] if unit["canonical_id"] == "alpha.service"
    )
    calls_before = list(state.calls)
    response = client.post(
        "/api/v1/runs",
        json={
            "operation": {
                "type": "systemd.service.stop",
                "object_id": target["object_id"],
            }
        },
        headers={
            "Forwarded": "for=203.0.113.8",
            "X-Forwarded-For": "203.0.113.9",
            "X-Real-IP": "203.0.113.10",
        },
    )
    assert response.status_code == 201, response.json()
    body = response.json()
    preview = body["preview"]
    assert body["operation"] == "systemd.service.stop"
    assert body["host_mutated"] is False and body["change_created"] is False
    assert preview["authorization"] == {
        "state": "not_preflighted",
        "exact": False,
        "authority": "systemd",
        "decision_point": "dispatch",
        "action_id": "org.freedesktop.systemd1.manage-units",
        "canonical_target": "alpha.service",
        "verb": "stop",
        "reason": "unprivileged_caller_cannot_supply_trusted_polkit_unit_verb_details",
    }
    assert preview["systemd_lifecycle_context"]["status"] == "partial"
    assert preview["protection"]["status"] == "unknown"
    assert {entry["reason"] for entry in preview["protection"]["assessed"]} == {
        "management_path", "localplane_agent"
    }
    # Execution exists; this plan is still blocked, and by protection rather than by a
    # missing executor. The loopback transport establishes nothing about the management
    # path, so both typed reasons are unresolved and `unknown` never becomes `clear`.
    assert preview["how"]["availability"] == "available"
    assert preview["how"]["eligibility"] == "blocked"
    assert set(preview["how"]["blockers"]) == {
        "protection_unresolved:management_path",
        "protection_unresolved:localplane_agent",
    }
    assert preview["confirmation"]["satisfiable"] is True
    assert preview["recovery"]["mode"] == "none"
    assert preview["digest_version"] == 6

    # Self-impact is derived and published, and it is honest about knowing nothing here:
    # this request arrives over loopback, so nothing established where the backend runs.
    # `unresolved` is the answer, and it is never `not_detected` by default.
    assert preview["self_impact"]["subject"] == "localplane_backend_runtime"
    assert preview["self_impact"]["status"] == "unresolved"
    assert preview["self_impact"]["override_eligible"] is False
    assert preview["self_impact"]["envelope"] is None

    row = client.app.state.context.database.query_one(
        "SELECT authorization_assessment, lifecycle_context, protection_assessments, "
        "self_impact FROM run_previews WHERE preview_id = ?",
        (preview["preview_id"],),
    )
    assert json.loads(row["authorization_assessment"])["state"] == "not_preflighted"
    assert json.loads(row["lifecycle_context"])["target_unit"] == "alpha.service"
    assert len(json.loads(row["protection_assessments"])) == 2
    assert json.loads(row["self_impact"])["override_eligible"] is False
    assert client.app.state.context.database.query("SELECT * FROM changes") == []

    confirmation = client.post(
        f"/api/v1/runs/{body['run_id']}/confirm",
        json={
            "preview_id": preview["preview_id"],
            "acknowledge": True,
            "acknowledge_object": "alpha.service",
        },
    )
    assert confirmation.status_code == 409
    apply = client.post(f"/api/v1/runs/{body['run_id']}/apply")
    assert apply.status_code == 409
    assert client.app.state.context.database.query("SELECT * FROM changes") == []
    assert state.calls == calls_before, "Part A planning contacted a systemd write/read seam"


def test_systemd_lifecycle_api_accepts_only_object_identity(systemd_api):
    client, _ = systemd_api
    assert client.post("/api/v1/systemd/observations/refresh").status_code == 200
    target = next(
        unit for unit in client.get("/api/v1/systemd/units").json()["units"]
        if unit["canonical_id"] == "alpha.service"
    )
    for extra in (
        {"unit_id": "alpha.service"},
        {"verb": "stop"},
        {"member": "StopUnit"},
        {"object_path": "/org/freedesktop/systemd1/unit/alpha_2eservice"},
        {"command": "systemctl"},
    ):
        response = client.post(
            "/api/v1/runs",
            json={
                "operation": {
                    "type": "systemd.service.stop",
                    "object_id": target["object_id"],
                    **extra,
                }
            },
        )
        assert response.status_code == 422
    alias = client.post(
        "/api/v1/runs",
        json={
            "operation": {
                "type": "systemd.service.stop",
                "object_id": "alpha-alias.service",
            }
        },
    )
    assert alias.status_code == 404
    paths = client.get("/openapi.json").json()["paths"]
    assert not {
        "/api/v1/systemd/start",
        "/api/v1/systemd/stop",
        "/api/v1/systemd/restart",
        "/api/v1/systemd/lifecycle",
    } & set(paths)


def test_no_runtime_systemd_mirror_table_exists(database):
    tables = {
        row["name"]
        for row in database.query("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert not {"systemd_units", "systemd_state", "systemd_relationships"} & tables
    assert max(row["version"] for row in database.query("SELECT version FROM schema_migrations")) == 15


def test_inventory_default_bound_is_not_accidentally_unbounded():
    assert MAX_UNITS == 512
