"""The judgements: health, management stance, identity and freshness.

Every branch of every rule, because these are the sentences LocalPlane says about somebody
else's machine.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from localplane.backend.domain.identity import (
    IdentityBasis,
    IdentityConfidence,
    identify_interface,
)
from localplane.backend.domain.network import InterfaceSignals, classify_management, derive_health
from localplane.backend.domain.states import (
    Freshness,
    HealthState,
    ManagementState,
    derive_freshness,
)


def signals(kind="ethernet", admin_up=True, operstate="up", carrier=True):
    return InterfaceSignals(kind=kind, admin_up=admin_up, operstate=operstate, carrier=carrier)


# --------------------------------------------------------------------------------- health


@pytest.mark.parametrize(
    ("kwargs", "state", "reason"),
    [
        ({"operstate": "up"}, HealthState.HEALTHY, "operstate_up"),
        ({"admin_up": False, "operstate": "down", "carrier": None}, HealthState.INACTIVE, "admin_down"),
        ({"operstate": "down", "carrier": False}, HealthState.INACTIVE, "no_carrier"),
        ({"operstate": "down", "carrier": True}, HealthState.DEGRADED, "carrier_up_operstate_down"),
        ({"operstate": "down", "carrier": None}, HealthState.INACTIVE, "operstate_down_carrier_unreadable"),
        ({"operstate": "unknown", "carrier": True}, HealthState.HEALTHY, "carrier_up_operstate_unknown"),
        ({"operstate": "unknown", "carrier": False}, HealthState.INACTIVE, "no_carrier_operstate_unknown"),
        ({"operstate": "unknown", "carrier": None}, HealthState.UNKNOWN, "operstate_unknown_carrier_unreadable"),
        ({"operstate": "notpresent"}, HealthState.FAILED, "device_not_present"),
        ({"operstate": "lowerlayerdown"}, HealthState.INACTIVE, "lower_layer_down"),
        ({"operstate": "dormant"}, HealthState.INACTIVE, "dormant"),
        ({"operstate": "testing"}, HealthState.DEGRADED, "link_under_test"),
        ({"admin_up": None}, HealthState.UNKNOWN, "admin_state_unreadable"),
        ({"operstate": None}, HealthState.UNKNOWN, "operstate_unreadable"),
        ({"operstate": "invented"}, HealthState.UNKNOWN, "unrecognised_operstate:invented"),
    ],
)
def test_health_rules(kwargs, state, reason):
    verdict = derive_health(signals(**kwargs))
    assert verdict.state == state
    assert verdict.reason == reason


def test_an_interface_that_is_down_is_not_called_failed():
    """Without retained intent, nothing has said this link was supposed to be up."""
    assert derive_health(signals(operstate="down", carrier=False)).state is HealthState.INACTIVE


def test_operstate_is_read_case_insensitively():
    assert derive_health(signals(operstate="UP")).state is HealthState.HEALTHY


def test_every_health_verdict_carries_a_reason():
    for operstate in ("up", "down", "unknown", "dormant", "testing", "notpresent", None):
        assert derive_health(signals(operstate=operstate)).reason


# ----------------------------------------------------------------------------- management


@pytest.mark.parametrize(
    ("kind", "state", "reason"),
    [
        ("loopback", ManagementState.OBSERVE_ONLY, "loopback"),
        ("virtual_ethernet", ManagementState.OBSERVE_ONLY, "ephemeral_virtual_pair"),
        ("tunnel", ManagementState.OBSERVE_ONLY, "userspace_owned_tunnel"),
        ("bridge", ManagementState.OBSERVED, "management_candidate"),
        ("ethernet", ManagementState.OBSERVED, "management_candidate"),
        ("wireless", ManagementState.OBSERVED, "management_candidate"),
        ("wwan", ManagementState.OBSERVED, "management_candidate"),
        ("unknown", ManagementState.OBSERVED, "management_candidate"),
    ],
)
def test_management_classification(kind, state, reason):
    verdict = classify_management(signals(kind=kind))
    assert verdict.state == state
    assert verdict.reason == reason


def test_nothing_is_classified_managed_without_retained_intent():
    """Managed means retained intent, and there is no way to retain any yet."""
    for kind in ("ethernet", "bridge", "loopback", "tunnel", "wireless", "veth", "wwan"):
        assert classify_management(signals(kind=kind)).state != ManagementState.MANAGED


def test_management_does_not_depend_on_health():
    """The axes are independent: a down link is still a management candidate."""
    down = signals(kind="ethernet", admin_up=False, operstate="down", carrier=None)
    up = signals(kind="ethernet")
    assert classify_management(down).state == classify_management(up).state


# ------------------------------------------------------------------------------- identity


def test_a_permanent_mac_is_the_preferred_identity():
    identity = identify_interface("host_x", "eth0", "02:00:00:00:00:10", True, "platform/eth")
    assert identity.basis is IdentityBasis.PERMANENT_MAC
    assert identity.confidence is IdentityConfidence.HIGH


def test_a_generated_mac_is_not_used_as_identity():
    """Virtual links get a random MAC at creation; keying to one mints a new object each time."""
    identity = identify_interface("host_x", "veth0", "02:00:00:00:00:14", False, None)
    assert identity.basis is IdentityBasis.KERNEL_NAME
    assert identity.confidence is IdentityConfidence.LOW


def test_the_device_path_is_the_second_choice():
    identity = identify_interface("host_x", "wwan0", None, None, "usb/1-1.2:1.4")
    assert identity.basis is IdentityBasis.DEVICE_PATH
    assert identity.value == "usb/1-1.2:1.4"


@pytest.mark.parametrize("mac", ["00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"])
def test_addresses_that_identify_nothing_are_rejected(mac):
    assert identify_interface("host_x", "lo", mac, True, None).basis is IdentityBasis.KERNEL_NAME


def test_identity_is_deterministic():
    first = identify_interface("host_x", "eth0", "aa:bb:cc:dd:ee:ff", True, None)
    second = identify_interface("host_x", "eth0", "aa:bb:cc:dd:ee:ff", True, None)
    assert first.object_id == second.object_id


def test_a_rename_does_not_change_a_hardware_backed_identity():
    """The whole point: eth0 becoming enp3s0 is the same object."""
    before = identify_interface("host_x", "eth0", "aa:bb:cc:dd:ee:ff", True, None)
    after = identify_interface("host_x", "enp3s0", "aa:bb:cc:dd:ee:ff", True, None)
    assert before.object_id == after.object_id


def test_the_same_interface_on_another_host_is_another_object():
    a = identify_interface("host_a", "eth0", "aa:bb:cc:dd:ee:ff", True, None)
    b = identify_interface("host_b", "eth0", "aa:bb:cc:dd:ee:ff", True, None)
    assert a.object_id != b.object_id


def test_a_name_based_identity_does_change_when_renamed():
    """Honest consequence of weak evidence, which is why the basis is reported."""
    before = identify_interface("host_x", "br-a", None, None, None)
    after = identify_interface("host_x", "br-b", None, None, None)
    assert before.object_id != after.object_id


# ------------------------------------------------------------------------------ freshness


def test_freshness_is_derived_from_the_observation_time():
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    fresh, age = derive_freshness(now - timedelta(seconds=10), ttl_s=60, now=now)
    assert fresh is Freshness.CURRENT
    assert age == pytest.approx(10.0)


def test_an_old_observation_is_stale():
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    stale, age = derive_freshness(now - timedelta(seconds=600), ttl_s=60, now=now)
    assert stale is Freshness.STALE
    assert age == pytest.approx(600.0)


def test_never_observed_is_its_own_answer():
    assert derive_freshness(None)[0] is Freshness.NEVER_OBSERVED
    assert derive_freshness(None)[1] is None


def test_clock_skew_does_not_make_an_observation_stale():
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    fresh, age = derive_freshness(now + timedelta(seconds=5), ttl_s=60, now=now)
    assert fresh is Freshness.CURRENT
    assert age < 0
