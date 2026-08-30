"""Capability discovery.

The rule these enforce: a capability is reported because it was probed, and a capability
that cannot be used says so with a reason.
"""

from __future__ import annotations

from pathlib import Path

from localplane.agent.capabilities import discover_capabilities
from localplane.protocol.capabilities import (
    CAPABILITIES,
    CAPABILITY_DOCKER_CONTAINER_LIFECYCLE,
    CAPABILITY_DOCKER_CONTAINERS_OBSERVE,
    CAPABILITY_HOST_OBSERVE,
    CAPABILITY_NETWORK_INTERFACE_MTU_GUARD,
    CAPABILITY_NETWORK_INTERFACE_SET_MTU,
    CAPABILITY_NETWORK_OBSERVE,
    CAPABILITY_NETWORK_PROVIDERS_OBSERVE,
    CAPABILITY_NETWORK_ROUTE_OBSERVE,
    CAPABILITY_SYSTEMD_SERVICE_LIFECYCLE,
    CAPABILITY_SYSTEMD_SERVICE_LIFECYCLE_CONTEXT_OBSERVE,
    CAPABILITY_SYSTEMD_UNITS_OBSERVE,
    MUTATING_CAPABILITIES,
    CapabilityStatus,
)
from tests.conftest import FakeRunner


def by_name(capabilities):
    return {c.capability: c for c in capabilities}


def test_only_capabilities_this_agent_implements_are_declared():
    """No fictional future catalogue: the vocabulary is what the code supports today."""
    assert set(CAPABILITIES) == {
        CAPABILITY_HOST_OBSERVE,
        CAPABILITY_NETWORK_OBSERVE,
        CAPABILITY_NETWORK_PROVIDERS_OBSERVE,
        CAPABILITY_NETWORK_ROUTE_OBSERVE,
        CAPABILITY_NETWORK_INTERFACE_SET_MTU,
        CAPABILITY_NETWORK_INTERFACE_MTU_GUARD,
        CAPABILITY_DOCKER_CONTAINERS_OBSERVE,
        CAPABILITY_DOCKER_CONTAINER_LIFECYCLE,
        CAPABILITY_SYSTEMD_UNITS_OBSERVE,
        CAPABILITY_SYSTEMD_SERVICE_LIFECYCLE,
        CAPABILITY_SYSTEMD_SERVICE_LIFECYCLE_CONTEXT_OBSERVE,
    }


def test_only_the_four_declared_capabilities_describe_mutating_mechanisms():
    """Four, and all are named. Every other capability is still a read.

    Asserted as an equality rather than as a count, because a count is the assertion that
    fails to notice a swap. The three are not the same kind of write and the vocabulary
    does not pretend they are: one reaches the kernel through a privileged helper, one
    reaches a daemon that already owns what it is changing, and the third —
    `network.interface.mtu_guard` — writes nothing itself and can cause the first to happen
    with no further request when a guard's deadline passes. The fourth describes the typed
    systemd service lifecycle mechanism Part B will use; Part A has no mutating protocol
    method or executor, but classifying that mechanism as a read would still be a lie.
    """
    mutating = {name for name, d in CAPABILITIES.items() if d.mutating}
    assert mutating == {
        CAPABILITY_NETWORK_INTERFACE_SET_MTU,
        CAPABILITY_NETWORK_INTERFACE_MTU_GUARD,
        CAPABILITY_DOCKER_CONTAINER_LIFECYCLE,
        CAPABILITY_SYSTEMD_SERVICE_LIFECYCLE,
    }
    assert MUTATING_CAPABILITIES == mutating


def test_every_capability_outside_that_set_is_a_read():
    for name, definition in CAPABILITIES.items():
        if name in MUTATING_CAPABILITIES:
            continue
        assert definition.mutating is False, f"{name} claims to mutate"


def test_a_working_host_reports_both_capabilities_available(
    fake_root: Path, populated_sysfs: Path, working_runner: FakeRunner
):
    capabilities = by_name(discover_capabilities(fake_root, populated_sysfs, working_runner))
    assert capabilities[CAPABILITY_HOST_OBSERVE].status is CapabilityStatus.AVAILABLE
    network = capabilities[CAPABILITY_NETWORK_OBSERVE]
    assert network.status is CapabilityStatus.AVAILABLE
    assert network.detail["methods"] == {
        "sysfs": "ok",
        "rtnetlink_link": "ok",
        "rtnetlink_addr": "ok",
    }
    assert network.detail["interfaces_visible"] == 9


def test_discovery_actually_probes_rather_than_declaring(
    fake_root: Path, populated_sysfs: Path, working_runner: FakeRunner
):
    """Every probe runs the read it would perform in earnest, including the providers."""
    discover_capabilities(fake_root, populated_sysfs, working_runner)
    assert working_runner.calls == [
        ("ip", "--json", "--details", "link", "show"),
        ("ip", "--json", "addr", "show"),
        ("nmcli", "--terse", "--escape", "yes", "--fields", "RUNNING,VERSION,STATE",
         "general", "status"),
        ("tailscale", "status", "--json"),
    ]


def test_without_iproute2_network_observe_is_degraded_with_a_reason(
    fake_root: Path, populated_sysfs: Path
):
    network = by_name(discover_capabilities(fake_root, populated_sysfs, FakeRunner({})))[
        CAPABILITY_NETWORK_OBSERVE
    ]
    assert network.status is CapabilityStatus.DEGRADED
    assert network.reason == "no_l3_address_source"
    assert network.detail["methods"]["rtnetlink_addr"] == "unavailable"
    assert network.detail["failures"]
    assert network.usable is True


def test_without_sysfs_network_observe_is_unavailable(
    fake_root: Path, tmp_path: Path, working_runner: FakeRunner
):
    network = by_name(
        discover_capabilities(fake_root, tmp_path / "absent", working_runner)
    )[CAPABILITY_NETWORK_OBSERVE]
    assert network.status is CapabilityStatus.UNAVAILABLE
    assert network.reason == "sysfs_unreadable"
    assert network.usable is False


def test_incomplete_host_evidence_degrades_host_observe(
    fake_root: Path, populated_sysfs: Path, working_runner: FakeRunner
):
    (fake_root / "etc" / "os-release").unlink()
    host = by_name(discover_capabilities(fake_root, populated_sysfs, working_runner))[
        CAPABILITY_HOST_OBSERVE
    ]
    assert host.status is CapabilityStatus.DEGRADED
    assert host.reason == "incomplete_host_evidence"
    assert "os_release" in host.detail["gaps"]


def test_an_unidentifiable_host_makes_host_observe_unavailable(
    tmp_path: Path, populated_sysfs: Path, working_runner: FakeRunner, monkeypatch
):
    import os

    empty = tmp_path / "nothing"
    empty.mkdir()
    monkeypatch.setattr(
        os, "uname", lambda: os.uname_result(("Linux", "", "6.8.0", "#1", "aarch64"))
    )
    host = by_name(discover_capabilities(empty, populated_sysfs, working_runner))[
        CAPABILITY_HOST_OBSERVE
    ]
    assert host.status is CapabilityStatus.UNAVAILABLE
    assert host.reason == "host_identity_unavailable"


def test_the_real_host_reports_network_observe_usable():
    network = by_name(discover_capabilities())[CAPABILITY_NETWORK_OBSERVE]
    assert network.status in {CapabilityStatus.AVAILABLE, CapabilityStatus.DEGRADED}
    assert network.detail["methods"]["sysfs"] == "ok"
    assert network.detail["interfaces_visible"] > 0
