"""Strictly read-only checks against the real systemd and socket evidence seams.

The suite uses only :class:`SystemdProvider`'s closed read methods.  Unit
names are selected from the inventory systemd itself returned; no distro, package or
service name is assumed.  There is no lifecycle call, subprocess, systemctl invocation,
unit-file operation or request capable of changing the manager.
"""

from __future__ import annotations

import inspect
import os
import socket
from pathlib import Path

import pytest

from localplane.agent.capabilities import _probe_systemd, _probe_systemd_lifecycle
from localplane.agent.providers.linux_socket_diag import (
    LinuxSocketDiag,
    TcpSocketTuple,
    resolve_cgroup_path,
)
from localplane.agent.providers.docker import probe_peer_pidfd_support
from localplane.agent.providers.systemd import (
    SUPPORTED_UNIT_SUFFIXES,
    SystemdProvider,
    UnitBatch,
)
from localplane.protocol.capabilities import CapabilityStatus

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def provider() -> SystemdProvider:
    if not Path("/run/systemd/system").exists():
        pytest.skip("this host has no system systemd manager")
    return SystemdProvider()


@pytest.fixture(scope="module")
def inventory(provider: SystemdProvider) -> UnitBatch:
    result = provider.observe_units()
    if result.status == "failed":
        pytest.fail(f"the system manager could not be read: {result.reason}: {result.detail}")
    return result


def test_real_manager_is_reachable_and_reports_version(provider: SystemdProvider):
    manager = provider.read_manager()
    assert manager.status in {"ok", "degraded"}
    assert isinstance(manager.version, str) and manager.version
    assert manager.facts["version"] == manager.version
    assert manager.detail["manager_scope"] == "system"
    assert manager.detail["transport"] == "system_bus_dbus"


def test_real_loaded_inventory_is_bounded_and_truthful(inventory: UnitBatch):
    assert inventory.units
    assert inventory.listed_count is not None
    assert inventory.selected_count <= inventory.listed_count
    assert len(inventory.units) <= inventory.selected_count <= 512
    assert inventory.inventory_method in {
        "manager_list_units_by_patterns",
        "manager_list_units_filtered",
    }
    assert inventory.detail["connection_count"] == 1
    assert inventory.detail["inventory_deadline_s"] == 30.0
    assert inventory.detail["deadline_enforcement"] == "remaining_time_per_dbus_call"
    if inventory.status == "ok":
        assert inventory.inventory_complete is True
        assert inventory.truncated is False
    else:
        assert inventory.inventory_complete is False
        assert inventory.issues


@pytest.mark.parametrize(
    "unit_type", ["service", "socket", "timer", "target", "path", "mount"]
)
def test_real_supported_unit_type_is_normalised_when_present(
    unit_type: str, inventory: UnitBatch
):
    units = [unit for unit in inventory.units if unit.facts["unit_type"] == unit_type]
    if not units:
        pytest.skip(f"this manager currently has no loaded {unit_type} unit")
    unit = units[0]
    assert unit.canonical_id.endswith(f".{unit_type}")
    assert unit.facts["canonical_id"] == unit.canonical_id
    assert unit.facts["unit_type"] == unit_type
    if unit_type == "target":
        assert not any(
            key in unit.facts for key in ("service", "socket", "timer", "path", "mount")
        )
    else:
        assert unit_type in unit.facts


def test_real_targeted_get_unit_reuses_canonical_truth(
    provider: SystemdProvider, inventory: UnitBatch
):
    selected = min(inventory.units, key=lambda unit: unit.canonical_id)
    targeted = provider.observe_unit(selected.canonical_id)
    assert targeted.status == "observed"
    assert targeted.unit is not None
    assert targeted.unit.canonical_id == selected.canonical_id
    assert targeted.unit.facts["canonical_id"] == selected.canonical_id


def test_real_agent_process_containment_is_truthful(provider: SystemdProvider):
    resolution = provider.resolve_current_process_unit()
    assert resolution.method == "manager_get_unit_by_control_group"
    assert resolution.observed_at
    assert resolution.status in {"resolved", "unresolved", "failed"}
    if resolution.status == "resolved":
        assert resolution.cgroup
        assert resolution.canonical_id
        assert resolution.unit is not None
        assert resolution.unit.canonical_id == resolution.canonical_id
    else:
        assert resolution.reason


def test_real_capability_is_derived_from_manager_read(provider: SystemdProvider):
    capability = _probe_systemd(provider)
    assert capability.status in {CapabilityStatus.AVAILABLE, CapabilityStatus.DEGRADED}
    assert capability.mutating is False
    assert capability.detail["manager_scope"] == "system"
    assert capability.detail["provider_version"]


def test_real_lifecycle_contract_is_feature_detected_without_dispatch(
    provider: SystemdProvider,
):
    contract = provider.read_lifecycle_contract()
    assert contract.status == "available"
    assert {"GetUnit", "Subscribe"} <= set(contract.manager_methods)
    assert {"Start", "Stop", "Restart"} <= set(contract.unit_methods)
    assert "JobRemoved" in contract.manager_signals
    assert contract.introspection_unit
    assert contract.introspection_unit.endswith(".service")
    assert contract.missing == ()
    assert contract.detail["probe"] == "dbus_introspection"
    assert contract.detail["dispatch_interface"] == "org.freedesktop.systemd1.Unit"
    assert contract.detail["loaded_unit_lookup"] == "Manager.GetUnit"
    assert contract.detail["unit_loaded_passively"] is True
    assert contract.detail["transport_handle_persisted"] is False
    assert contract.detail["mutation_invoked"] is False

    lifecycle, context = _probe_systemd_lifecycle(provider, LinuxSocketDiag())
    assert lifecycle.status is CapabilityStatus.AVAILABLE
    assert lifecycle.detail["authorization_preflight"] == "not_preflighted"
    assert lifecycle.detail["mutation_invoked"] is False
    assert context.status is CapabilityStatus.AVAILABLE
    assert context.mutating is False
    runtime_owner = context.detail["runtime_owner"]
    assert runtime_owner["contract"] == "docker-direct-unix-v1"
    assert runtime_owner["implementation"] == "supported"
    assert runtime_owner["observation_required_for_completeness"] is True


def test_real_peer_pidfd_and_systemd_pidfd_containment_are_read_only(
    provider: SystemdProvider,
):
    kernel = probe_peer_pidfd_support()
    if kernel["status"] != "available":
        pytest.skip(f"SO_PEERPIDFD is unsupported on this host: {kernel}")
    assert kernel["numeric_pid_reconstruction_used"] is False

    contract = provider.read_pidfd_unit_contract()
    if contract.status != "available":
        pytest.skip(f"GetUnitByPIDFD is unsupported on this manager: {contract.as_dict()}")
    assert contract.detail["signature"] == "h->osay"
    assert contract.detail["mutation_invoked"] is False

    pidfd = os.pidfd_open(os.getpid())
    try:
        resolution = provider.resolve_process_unit_pidfd(pidfd)
    finally:
        os.close(pidfd)
    if resolution.status != "resolved":
        pytest.skip(f"this test process has no resolvable system unit: {resolution.as_dict()}")
    assert resolution.canonical_id
    assert resolution.invocation_id
    assert resolution.unit is not None
    assert resolution.unit.canonical_id == resolution.canonical_id
    assert resolution.unit.facts["invocation_id"] == resolution.invocation_id
    assert resolution.detail["unix_fd_transport"] is True
    assert resolution.detail["manager_scope"] == "system"


def test_real_effect_graph_read_is_bounded_and_uses_inventory_selected_identity(
    provider: SystemdProvider, inventory: UnitBatch
):
    services = sorted(
        unit.canonical_id
        for unit in inventory.units
        if unit.facts["unit_type"] == "service"
    )
    if not services:
        pytest.skip("this manager currently has no loaded service unit")
    # Stop context exercises reverse/effect properties, including feature-detected
    # UpheldBy, without invoking Unit.Stop or enqueueing any job.
    graph = provider.observe_effect_graph(services[0], "stop", timeout_s=3.0)
    assert graph.canonical_target == services[0]
    assert graph.target is not None
    assert graph.status in {"complete", "partial"}
    assert len(graph.units) <= 128
    assert len(graph.edges) <= 2048
    assert not any(edge["relation"] in {"Before", "After"} for edge in graph.edges)
    if graph.status == "partial":
        assert graph.gaps


def test_real_exact_tcp_socket_cgroup_and_containment_read(provider: SystemdProvider):
    diag = LinuxSocketDiag()
    assert diag.network_namespace_inode() == Path("/proc/self/ns/net").stat().st_ino
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.connect(listener.getsockname())
            accepted, _ = listener.accept()
            with accepted:
                local_ip, local_port = accepted.getsockname()
                peer_ip, peer_port = accepted.getpeername()
                result = diag.lookup(
                    TcpSocketTuple(
                        family="ipv4",
                        peer_ip=peer_ip,
                        peer_port=peer_port,
                        local_ip=local_ip,
                        local_port=local_port,
                    )
                )
    assert result.status == "matched"
    assert result.match is not None and result.match.cgroup_id > 0
    cgroup = resolve_cgroup_path(result.match.cgroup_id)
    assert cgroup.status == "resolved"
    assert cgroup.path is not None
    resolution = provider.resolve_control_group_unit(cgroup.path)
    assert resolution.status in {"resolved", "unresolved", "failed"}
    if resolution.status == "resolved":
        assert resolution.canonical_id
    else:
        assert resolution.reason


def test_live_suite_can_only_reach_the_closed_read_provider_surface():
    public = {
        name
        for name, member in inspect.getmembers(SystemdProvider, inspect.isfunction)
        if not name.startswith("_")
    }
    assert public == {
        "read_manager",
        "read_pidfd_unit_contract",
        "read_lifecycle_contract",
        "observe_units",
        "observe_unit",
        "observe_effect_graph",
        "resolve_current_process_unit",
        "resolve_control_group_unit",
        "resolve_process_unit_pidfd",
        "resolve_provider_unit",
    }
    assert SUPPORTED_UNIT_SUFFIXES == (
        ".service", ".socket", ".timer", ".target", ".path", ".mount"
    )
