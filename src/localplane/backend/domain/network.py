"""Judgements about network interfaces.

The provider said what the kernel says. These functions decide what LocalPlane makes of
it. They are pure, they take facts and return a value with the reason attached, and every
branch is reachable from a real interface on a real host — which is why they are the part
of the system with the densest tests.

Two rules run through all of it:

* A reason travels with every verdict. "inactive" on its own is an opinion; "inactive
  because the link is administratively down" is something an operator can act on, and
  something that can be shown to be wrong.
* Absent evidence produces ``unknown``, not a default. An interface whose operstate could
  not be read is not healthy, and it is not failed either.
"""

from __future__ import annotations

from dataclasses import dataclass

from localplane.backend.domain.states import HealthState, ManagementState

KIND_LOOPBACK = "loopback"
KIND_TUNNEL = "tunnel"
KIND_VIRTUAL_ETHERNET = "virtual_ethernet"
KIND_BRIDGE = "bridge"


@dataclass(frozen=True)
class Verdict:
    """A state plus the reason it was reached."""

    state: str
    reason: str


@dataclass(frozen=True)
class InterfaceSignals:
    """The subset of interface facts that the judgements depend on."""

    kind: str
    admin_up: bool | None
    operstate: str | None
    carrier: bool | None


def derive_health(signals: InterfaceSignals) -> Verdict:
    """Decide an interface's health from link-layer evidence alone.

    No intended state is part of these signals, so no intent is consulted. That constrains
    what can honestly be said: a link that is down is reported ``inactive`` with the reason it is
    down, not ``failed``, because nothing has told LocalPlane the link was supposed to be
    up. ``failed`` is reserved for the kernel reporting an actual fault.

    Carrier is read from ``/sys/class/net/*/carrier`` rather than from the interface
    flags, because the sysfs ``flags`` file omits ``IFF_LOWER_UP`` — a link with a cable
    and a link without one are indistinguishable there.
    """
    if signals.admin_up is None:
        return Verdict(HealthState.UNKNOWN, "admin_state_unreadable")
    if not signals.admin_up:
        return Verdict(HealthState.INACTIVE, "admin_down")

    operstate = (signals.operstate or "").lower()
    if not operstate:
        return Verdict(HealthState.UNKNOWN, "operstate_unreadable")

    if operstate == "up":
        return Verdict(HealthState.HEALTHY, "operstate_up")
    if operstate == "notpresent":
        return Verdict(HealthState.FAILED, "device_not_present")
    if operstate == "testing":
        return Verdict(HealthState.DEGRADED, "link_under_test")
    if operstate == "dormant":
        return Verdict(HealthState.INACTIVE, "dormant")
    if operstate == "lowerlayerdown":
        return Verdict(HealthState.INACTIVE, "lower_layer_down")

    if operstate == "unknown":
        # Virtual links — loopback, tun devices — frequently never set an operstate.
        # Carrier is the only evidence left, and if it is missing too, so is the verdict.
        if signals.carrier is True:
            return Verdict(HealthState.HEALTHY, "carrier_up_operstate_unknown")
        if signals.carrier is False:
            return Verdict(HealthState.INACTIVE, "no_carrier_operstate_unknown")
        return Verdict(HealthState.UNKNOWN, "operstate_unknown_carrier_unreadable")

    if operstate == "down":
        if signals.carrier is False:
            return Verdict(HealthState.INACTIVE, "no_carrier")
        if signals.carrier is True:
            # The physical layer is up while the kernel still calls the link down. That
            # is a real disagreement, and naming it is more useful than picking a side.
            return Verdict(HealthState.DEGRADED, "carrier_up_operstate_down")
        return Verdict(HealthState.INACTIVE, "operstate_down_carrier_unreadable")

    return Verdict(HealthState.UNKNOWN, f"unrecognised_operstate:{operstate}")


def classify_management(signals: InterfaceSignals) -> Verdict:
    """Decide LocalPlane's stance towards an interface.

    Only kernel evidence is consulted — ``ARPHRD`` type and ``IFLA_INFO_KIND`` — never the
    interface name. An object is ``observe_only`` when writing to it could not work:

    * a loopback device is not configurable in any useful sense;
    * a veth is half of a pair created and destroyed by whatever runtime owns the other
      half, so an intent retained for it would outlive the object;
    * a tun/tap device exists only while the userspace process holding its file
      descriptor is alive.

    A bridge is not one of them. A bridge created by an operator and a bridge created by a
    container runtime are the same kind of kernel object, and both are things LocalPlane
    could in principle be answerable for — what differs is who else is already running
    them, which is an *ownership* question and is answered on its own axis by
    :mod:`localplane.backend.domain.provenance`. Deciding it here would put a provider's
    say-so into the management state, so that a Docker daemon becoming unreadable would
    change LocalPlane's stance towards an object; and since the classification is stored
    with the object, it would have to be reconciled against retained intent. Ownership
    governs *eligibility* — whether adoption is offered — and leaves the stance alone.
    """
    if signals.kind == KIND_LOOPBACK:
        return Verdict(ManagementState.OBSERVE_ONLY, "loopback")
    if signals.kind == KIND_VIRTUAL_ETHERNET:
        return Verdict(ManagementState.OBSERVE_ONLY, "ephemeral_virtual_pair")
    if signals.kind == KIND_TUNNEL:
        return Verdict(ManagementState.OBSERVE_ONLY, "userspace_owned_tunnel")
    return Verdict(ManagementState.OBSERVED, "management_candidate")
