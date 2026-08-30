"""Host identity, including the cases where the host will not say who it is."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from localplane.agent.identity import (
    BASIS_DBUS_MACHINE_ID,
    BASIS_HOSTNAME,
    BASIS_MACHINE_ID,
    HostIdentityUnavailable,
    identify_host,
)


def test_identity_from_machine_id(fake_root: Path, fake_uname: os.uname_result):
    identity = identify_host(fake_root, fake_uname)
    assert identity.identity_basis == BASIS_MACHINE_ID
    assert identity.identity_confidence == "high"
    assert identity.host_id.startswith("host_")
    assert identity.gaps == ()
    assert identity.hostname == "fixture-host"
    assert identity.os_id == "debian"
    assert identity.os_version_id == "12"
    assert identity.os_pretty_name == "Debian GNU/Linux 12 (bookworm)"
    assert identity.kernel_release == "6.8.0-test"
    assert identity.architecture == "aarch64"
    assert identity.boot_id == "11111111-2222-3333-4444-555555555555"


def test_the_host_id_is_deterministic(fake_root: Path, fake_uname: os.uname_result):
    """A rebuilt database must key the same host to the same id."""
    assert identify_host(fake_root, fake_uname).host_id == identify_host(fake_root, fake_uname).host_id


def test_the_host_id_does_not_contain_the_machine_id(fake_root: Path, fake_uname: os.uname_result):
    """The machine id is confidential; the host id is an application-specific derivation."""
    machine_id = (fake_root / "etc" / "machine-id").read_text().strip()
    assert machine_id not in identify_host(fake_root, fake_uname).host_id


def test_a_different_machine_id_is_a_different_host(fake_root: Path, fake_uname: os.uname_result):
    first = identify_host(fake_root, fake_uname).host_id
    (fake_root / "etc" / "machine-id").write_text("ffffffffffffffffffffffffffffffff\n")
    assert identify_host(fake_root, fake_uname).host_id != first


def test_a_malformed_machine_id_is_not_used(fake_root: Path, fake_uname: os.uname_result):
    (fake_root / "etc" / "machine-id").write_text("not-a-machine-id\n")
    identity = identify_host(fake_root, fake_uname)
    assert identity.identity_basis == BASIS_HOSTNAME
    assert "machine_id" in identity.gaps


def test_the_dbus_machine_id_is_the_first_fallback(fake_root: Path, fake_uname: os.uname_result):
    (fake_root / "etc" / "machine-id").unlink()
    dbus = fake_root / "var" / "lib" / "dbus"
    dbus.mkdir(parents=True)
    (dbus / "machine-id").write_text("abcdefabcdefabcdefabcdefabcdefab\n")
    identity = identify_host(fake_root, fake_uname)
    assert identity.identity_basis == BASIS_DBUS_MACHINE_ID
    assert identity.identity_confidence == "high"


def test_without_a_machine_id_the_fallback_says_it_is_weaker(
    fake_root: Path, fake_uname: os.uname_result
):
    (fake_root / "etc" / "machine-id").unlink()
    identity = identify_host(fake_root, fake_uname)
    assert identity.identity_basis == BASIS_HOSTNAME
    assert identity.identity_confidence == "low"
    assert "machine_id" in identity.gaps
    assert identity.host_id.startswith("host_")


def test_missing_os_release_is_a_named_gap_not_a_guess(
    fake_root: Path, fake_uname: os.uname_result
):
    (fake_root / "etc" / "os-release").unlink()
    identity = identify_host(fake_root, fake_uname)
    assert "os_release" in identity.gaps
    assert identity.os_id is None
    assert identity.os_pretty_name is None
    assert identity.os_version_id is None


def test_missing_boot_id_is_a_named_gap(fake_root: Path, fake_uname: os.uname_result):
    (fake_root / "proc" / "sys" / "kernel" / "random" / "boot_id").unlink()
    identity = identify_host(fake_root, fake_uname)
    assert identity.boot_id is None
    assert "boot_id" in identity.gaps


def test_the_running_and_configured_hostnames_are_reported_separately(
    fake_root: Path, fake_uname: os.uname_result
):
    """They can disagree, and collapsing them would hide a real condition."""
    (fake_root / "etc" / "hostname").write_text("renamed-in-config\n")
    identity = identify_host(fake_root, fake_uname)
    assert identity.hostname == "fixture-host"
    assert identity.configured_hostname == "renamed-in-config"


def test_identification_fails_rather_than_inventing_an_id(tmp_path: Path):
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    nameless = os.uname_result(("Linux", "", "6.8.0", "#1", "aarch64"))
    with pytest.raises(HostIdentityUnavailable):
        identify_host(empty_root, nameless)


def test_the_real_host_identifies_itself():
    identity = identify_host("/")
    assert identity.host_id.startswith("host_")
    assert identity.kernel_name == "Linux"
    assert identity.architecture == os.uname().machine
