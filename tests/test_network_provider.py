"""The Linux network provider.

These are the tests that decide whether LocalPlane can be believed about a host. Most of
them are about the difference between "no" and "I don't know".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from localplane.agent.providers.base import CommandResult, Fidelity, SweepStatus
from localplane.agent.providers.linux_network import (
    InvalidInterfaceName,
    LinuxNetworkProvider,
    classify_kind,
)
from tests.conftest import FakeRunner, json_result, write_interface


def observe(sysfs, runner, names=None):
    return LinuxNetworkProvider(sysfs_net=sysfs, runner=runner).observe_interfaces(names)


def facts_by_name(batch):
    return {i.facts.name: i.facts for i in batch.interfaces}


# ------------------------------------------------------------------------ classification


def test_every_interface_class_is_read_from_kernel_evidence(populated_sysfs, working_runner):
    kinds = {name: f.kind for name, f in facts_by_name(observe(populated_sysfs, working_runner)).items()}
    assert kinds == {
        "lo": "loopback",
        "eth0": "ethernet",
        "eth1": "ethernet",
        "wlan0": "wireless",
        "wwan0": "wwan",
        "tun0": "tunnel",
        "docker0": "bridge",
        "veth0": "virtual_ethernet",
        "odd0": "ethernet",
    }


def test_classification_never_consults_the_interface_name(populated_sysfs, working_runner, link_argv, addr_argv):
    """A bridge called ``eth9`` is still a bridge; an ethernet called ``br0`` is not."""
    write_interface(populated_sysfs, "eth9", ifindex=20, bridge=True, devtype="bridge")
    write_interface(populated_sysfs, "br0", ifindex=21, device="0000:01:00.0", subsystem="pci")
    runner = FakeRunner({
        link_argv: json_result(link_argv, [{"ifname": "eth9", "linkinfo": {"info_kind": "bridge"}}]),
        addr_argv: json_result(addr_argv, []),
    })
    kinds = {n: f.kind for n, f in facts_by_name(observe(populated_sysfs, runner)).items()}
    assert kinds["eth9"] == "bridge"
    assert kinds["br0"] == "ethernet"


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"arphrd_type": 772, "link_kind": None, "devtype": None}, "loopback"),
        ({"arphrd_type": 1, "link_kind": "bridge", "devtype": None}, "bridge"),
        ({"arphrd_type": 1, "link_kind": "veth", "devtype": None}, "virtual_ethernet"),
        ({"arphrd_type": 65534, "link_kind": "tun", "devtype": None}, "tunnel"),
        ({"arphrd_type": 1, "link_kind": None, "devtype": "wlan"}, "wireless"),
        ({"arphrd_type": 65534, "link_kind": None, "devtype": "wwan"}, "wwan"),
        ({"arphrd_type": 1, "link_kind": "vlan", "devtype": None}, "vlan"),
        ({"arphrd_type": 1, "link_kind": "bond", "devtype": None}, "bond"),
        ({"arphrd_type": 1, "link_kind": None, "devtype": None}, "ethernet"),
        ({"arphrd_type": None, "link_kind": None, "devtype": None}, "unknown"),
    ],
)
def test_classify_kind_branches(kwargs, expected):
    assert classify_kind(is_bridge=False, is_wireless=False, is_tun=False, **kwargs) == expected


# ----------------------------------------------------------------- unknowns stay unknown


def test_a_speed_of_minus_one_is_not_a_speed(populated_sysfs, working_runner):
    """The kernel writes -1 when it does not know. Reporting -1 Mb/s would be a lie."""
    eth0 = facts_by_name(observe(populated_sysfs, working_runner))["eth0"]
    assert eth0.speed_mbps is None
    assert eth0.duplex is None


def test_an_unreadable_carrier_is_unknown_not_false(populated_sysfs, working_runner):
    """``carrier`` answers EINVAL on an admin-down link. That is not 'no carrier'."""
    wlan0 = facts_by_name(observe(populated_sysfs, working_runner))["wlan0"]
    assert wlan0.carrier is None
    assert wlan0.admin_up is False


def test_unknown_fields_are_named_in_gaps(populated_sysfs, working_runner):
    batch = observe(populated_sysfs, working_runner)
    gaps = {i.facts.name: set(i.gaps) for i in batch.interfaces}
    assert {"speed_mbps", "duplex"} <= gaps["eth0"]
    assert "carrier" in gaps["wlan0"]
    assert gaps["eth1"] == set()


def test_a_real_speed_survives(populated_sysfs, working_runner):
    eth1 = facts_by_name(observe(populated_sysfs, working_runner))["eth1"]
    assert eth1.speed_mbps == 1000
    assert eth1.duplex == "full"
    assert eth1.carrier is True


def test_no_addresses_and_no_address_source_are_different(populated_sysfs, working_runner, link_argv):
    """The distinction the whole observation model rests on."""
    with_source = facts_by_name(observe(populated_sysfs, working_runner))
    assert with_source["eth0"].addresses == ()

    runner = FakeRunner({link_argv: json_result(link_argv, [])})
    without_source = facts_by_name(observe(populated_sysfs, runner))
    assert without_source["eth0"].addresses is None


# --------------------------------------------------------------------------- link facts


def test_link_facts_are_normalized_from_sysfs(populated_sysfs, working_runner):
    eth0 = facts_by_name(observe(populated_sysfs, working_runner))["eth0"]
    assert eth0.ifindex == 2
    assert eth0.mtu == 1500
    assert eth0.mac_address == "02:00:00:00:00:10"
    assert eth0.mac_is_permanent is True
    assert eth0.admin_up is True
    assert eth0.operstate == "down"
    assert eth0.carrier is False
    assert eth0.is_physical is True
    assert eth0.device_path == "platform/fd580000.ethernet"
    assert eth0.arphrd_type == 1


def test_a_generated_mac_is_reported_as_not_permanent(populated_sysfs, working_runner):
    assert facts_by_name(observe(populated_sysfs, working_runner))["docker0"].mac_is_permanent is False


def test_statistics_are_read_and_absent_ones_stay_none(populated_sysfs, working_runner):
    stats = facts_by_name(observe(populated_sysfs, working_runner))["eth0"].statistics
    assert stats.rx_dropped == 12
    assert stats.rx_bytes == 0
    assert stats.tx_packets is None


def test_an_interface_with_no_statistics_reports_none(populated_sysfs, working_runner):
    assert facts_by_name(observe(populated_sysfs, working_runner))["lo"].statistics is None


def test_addresses_carry_their_source_and_lifetimes(populated_sysfs, working_runner):
    addresses = facts_by_name(observe(populated_sysfs, working_runner))["eth1"].addresses
    assert len(addresses) == 1
    address = addresses[0]
    assert (address.family, address.address, address.prefix_length) == ("inet", "192.0.2.215", 24)
    assert address.dynamic is True
    assert address.valid_lifetime_s == 41039


def test_forever_lifetimes_are_reported_as_absent(populated_sysfs, working_runner):
    """0xffffffff is iproute2 for 'forever', which is no lifetime at all."""
    lo = facts_by_name(observe(populated_sysfs, working_runner))["lo"].addresses[0]
    assert lo.valid_lifetime_s is None
    assert lo.preferred_lifetime_s is None


def test_bridge_membership_is_recorded(populated_sysfs, working_runner):
    assert facts_by_name(observe(populated_sysfs, working_runner))["veth0"].master == "docker0"


# ------------------------------------------------------------------- requested interfaces


def test_a_requested_interface_that_does_not_exist_is_reported_missing(populated_sysfs, working_runner):
    batch = observe(populated_sysfs, working_runner, ["eth0", "eth404"])
    assert [i.facts.name for i in batch.interfaces] == ["eth0"]
    assert batch.missing == ("eth404",)


def test_a_missing_interface_is_never_synthesised(populated_sysfs, working_runner):
    batch = observe(populated_sysfs, working_runner, ["ghost0"])
    assert batch.interfaces == ()
    assert batch.missing == ("ghost0",)


@pytest.mark.parametrize(
    "name",
    ["eth0; rm -rf /", "../../etc/passwd", "a" * 16, "", "eth0 && id", "$(whoami)", "eth\n0"],
)
def test_invalid_interface_names_are_refused(populated_sysfs, working_runner, name):
    with pytest.raises(InvalidInterfaceName):
        observe(populated_sysfs, working_runner, [name])


def test_too_many_requested_names_are_refused(populated_sysfs, working_runner):
    with pytest.raises(InvalidInterfaceName):
        observe(populated_sysfs, working_runner, [f"eth{n}" for n in range(200)])


def test_requested_names_never_reach_a_command(populated_sysfs, working_runner):
    """The filter is applied in Python; the argv is fixed and carries no caller input."""
    observe(populated_sysfs, working_runner, ["eth0"])
    for argv in working_runner.calls:
        assert "eth0" not in argv
        assert argv[0] == "ip" and "show" in argv


# ---------------------------------------------------------------------- provider errors


def test_a_missing_ip_command_degrades_rather_than_fails(populated_sysfs):
    batch = observe(populated_sysfs, FakeRunner({}))
    assert batch.status is SweepStatus.PARTIAL
    assert len(batch.interfaces) == 9
    assert all(i.fidelity is Fidelity.PARTIAL for i in batch.interfaces)
    assert all(i.facts.addresses is None for i in batch.interfaces)
    codes = {issue.code for issue in batch.issues}
    assert codes == {"command_failed"}


def test_a_provider_error_names_its_source_and_the_argv(populated_sysfs, link_argv, addr_argv):
    batch = observe(populated_sysfs, FakeRunner({}))
    sources = {issue.source for issue in batch.issues}
    assert sources == {"rtnetlink.link", "rtnetlink.addr"}
    assert all(issue.detail["argv"][0] == "ip" for issue in batch.issues)


def test_unparseable_command_output_is_an_issue_not_a_crash(populated_sysfs, link_argv, addr_argv):
    runner = FakeRunner({
        link_argv: CommandResult(link_argv, 0, "not json at all", ""),
        addr_argv: json_result(addr_argv, []),
    })
    batch = observe(populated_sysfs, runner)
    assert {i.code for i in batch.issues} == {"unparseable_output"}
    assert len(batch.interfaces) == 9


def test_unexpected_command_output_shape_is_an_issue(populated_sysfs, link_argv, addr_argv):
    runner = FakeRunner({
        link_argv: json_result(link_argv, {"not": "a list"}),
        addr_argv: json_result(addr_argv, []),
    })
    assert {i.code for i in observe(populated_sysfs, runner).issues} == {"unexpected_shape"}


def test_a_nonzero_exit_carries_stderr(populated_sysfs, link_argv, addr_argv):
    runner = FakeRunner({
        link_argv: CommandResult(link_argv, 2, "", "Cannot open netlink socket"),
        addr_argv: json_result(addr_argv, []),
    })
    issue = next(i for i in observe(populated_sysfs, runner).issues if i.source == "rtnetlink.link")
    assert "netlink" in issue.message


def test_unreadable_sysfs_is_a_failed_sweep_not_an_empty_one(tmp_path: Path, working_runner):
    """An empty estate and an unreadable one must never look the same."""
    batch = observe(tmp_path / "does-not-exist", working_runner)
    assert batch.status is SweepStatus.FAILED
    assert batch.interfaces == ()
    assert [i.code for i in batch.issues] == ["sysfs_unreadable"]


def test_unreadable_core_fields_degrade_the_observation(sysfs_net, working_runner):
    write_interface(sysfs_net, "eth0", ifindex=2, unreadable=("operstate", "flags"))
    observed = observe(sysfs_net, working_runner).interfaces[0]
    assert observed.fidelity is Fidelity.DEGRADED
    assert observed.facts.admin_up is None
    assert observed.facts.operstate is None
    assert {"sysfs.flags", "sysfs.operstate"} <= set(observed.gaps)


def test_sweep_status_is_derived_not_asserted(populated_sysfs, working_runner):
    batch = observe(populated_sysfs, working_runner)
    assert batch.status is SweepStatus.OK
    assert batch.derive_status() is batch.status


# -------------------------------------------------------------------------- evidence


def test_evidence_carries_the_raw_sources_and_the_argv(populated_sysfs, working_runner):
    evidence = next(
        i for i in observe(populated_sysfs, working_runner).interfaces if i.facts.name == "eth0"
    ).evidence
    assert evidence["sysfs"]["flags"] == "0x1003"
    assert evidence["sysfs"]["speed"] == "-1"
    assert evidence["rtnetlink_addr"]["addr_info"] == []
    assert evidence["commands"][0]["argv"][0] == "ip"
    assert evidence["sysfs_path"].endswith("/eth0")


def test_unreadable_sysfs_fields_are_recorded_as_errors(populated_sysfs, working_runner):
    evidence = next(
        i for i in observe(populated_sysfs, working_runner).interfaces if i.facts.name == "wlan0"
    ).evidence
    assert "carrier" in evidence["sysfs_errors"]
    assert "carrier" not in evidence["sysfs"]


def test_verbose_netlink_subobjects_are_omitted_and_the_omission_is_declared(
    populated_sysfs, working_runner
):
    observed = next(
        i for i in observe(populated_sysfs, working_runner).interfaces if i.facts.name == "docker0"
    )
    assert observed.evidence["rtnetlink_link"]["linkinfo"] == {"info_kind": "bridge"}
    assert "rtnetlink_link.linkinfo.info_data" in observed.evidence["omitted"]


def test_each_interface_carries_its_own_observation_time(populated_sysfs, working_runner):
    batch = observe(populated_sysfs, working_runner)
    stamps = [i.observed_at for i in batch.interfaces]
    assert all(batch.started_at <= s <= batch.completed_at for s in stamps)
