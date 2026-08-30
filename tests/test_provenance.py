"""Deciding who owns an object, from evidence and nothing else.

Pure functions over facts and provider readings. No database, no agent, no host — which is
what makes it possible to exercise the cases a single machine could never produce all of at
once: a Docker daemon that is unreachable, two networks claiming one gateway, a tailnet
interface whose daemon is stopped, an object two systems both claim.

The load-bearing tests here are the negative ones. Anything can attribute ``docker0`` to
Docker; the question is whether the model still does it when the only thing pointing that
way is the name.
"""

from __future__ import annotations

from typing import Any

import pytest

from localplane.backend.domain.provenance import (
    EMPTY_EVIDENCE,
    NEVER_CONSULTED,
    AdoptionEligibility,
    OwnershipConfidence,
    OwnershipRelation,
    OwnershipState,
    ProviderEvidence,
    ProviderReadingView,
    derive_adoption_eligibility,
    derive_provenance,
    external_owner,
)
from localplane.backend.domain.states import ManagementState
from localplane.protocol.providers import (
    PROVIDER_DOCKER,
    PROVIDER_KERNEL,
    PROVIDER_NETWORKMANAGER,
    PROVIDER_TAILSCALE,
    SOURCE_DOCKER_NETWORKS,
    SOURCE_NETWORKMANAGER_DEVICES,
    SOURCE_TAILSCALE_STATUS,
    ProviderStatus,
)

# ------------------------------------------------------------------------------- builders


def facts(
    name: str = "eth0",
    kind: str = "ethernet",
    addresses: list[str] | None = None,
    is_physical: bool = False,
    device_path: str | None = None,
    arphrd_type: int | None = 1,
) -> dict[str, Any]:
    return {
        "name": name,
        "kind": kind,
        "arphrd_type": arphrd_type,
        "is_physical": is_physical,
        "device_path": device_path,
        "addresses": (
            None
            if addresses is None
            else [{"family": "inet", "address": a, "prefix_length": 24} for a in addresses]
        ),
    }


def reading(
    provider: str,
    source: str,
    *,
    status: ProviderStatus = ProviderStatus.OK,
    records: tuple[dict[str, Any], ...] = (),
    reason: str | None = None,
    version: str | None = None,
) -> ProviderReadingView:
    return ProviderReadingView(
        provider=provider,
        source=source,
        status=str(status),
        observed_at="2026-08-22T09:00:00+00:00",
        reason=reason,
        version=version,
        records=records,
        provider_observation_id=f"pobs_{provider}",
    )


def docker(*networks: dict[str, Any], **kwargs: Any) -> ProviderReadingView:
    return reading(PROVIDER_DOCKER, SOURCE_DOCKER_NETWORKS, records=networks, **kwargs)


def network(
    network_id: str = "n" * 64,
    name: str = "monitoring_default",
    gateway: str | None = "172.19.0.1",
    bridge_name: str | None = None,
    driver: str = "bridge",
) -> dict[str, Any]:
    return {
        "network_id": network_id,
        "name": name,
        "driver": driver,
        "bridge_name": bridge_name,
        "default_bridge": bridge_name is not None,
        "ipam": [{"subnet": "172.19.0.0/16", "gateway": gateway}] if gateway else [],
        "compose_project": "monitoring",
    }


def nm(*devices: dict[str, Any], **kwargs: Any) -> ProviderReadingView:
    return reading(
        PROVIDER_NETWORKMANAGER, SOURCE_NETWORKMANAGER_DEVICES, records=devices, **kwargs
    )


def device(
    name: str = "eth0",
    state: str = "connected",
    external: bool = False,
    connection: str | None = "service-lan",
    uuid: str | None = "uuid-1",
) -> dict[str, Any]:
    return {
        "device": name,
        "device_type": "ethernet",
        "state": state,
        "state_raw": f"{state} (externally)" if external else state,
        "external": external,
        "connection": connection,
        "connection_uuid": uuid,
    }


def tailscale(
    backend_state: str = "Running",
    tun: bool | None = True,
    addresses: tuple[str, ...] = ("100.64.0.10",),
    **kwargs: Any,
) -> ProviderReadingView:
    return reading(
        PROVIDER_TAILSCALE,
        SOURCE_TAILSCALE_STATUS,
        records=(
            {
                "backend_state": backend_state,
                "tun": tun,
                "tailscale_ips": list(addresses),
                "hostname": "fixture-host",
                "dns_name": "fixture-host.tail.ts.net.",
            },
        ),
        **kwargs,
    )


def evidence(*readings: ProviderReadingView) -> ProviderEvidence:
    return ProviderEvidence.of(readings)


def source_named(provenance, name: str):
    return next(s for s in provenance.sources if s.source == name)


# ------------------------------------------------------- names establish nothing at all


@pytest.mark.parametrize(
    ("name", "kind"),
    [("docker0", "bridge"), ("br-c0ffee000002", "bridge"), ("tailscale0", "tunnel")],
)
def test_a_name_is_never_evidence_of_ownership(name: str, kind: str):
    """The three names LocalPlane would most plausibly guess from, and it does not."""
    provenance = derive_provenance(facts(name=name, kind=kind, addresses=[]), EMPTY_EVIDENCE)
    assert provenance.state is OwnershipState.UNKNOWN
    assert provenance.claims == ()


def test_a_bridge_called_something_else_is_still_attributed_on_evidence():
    """And the converse: attribution follows the evidence wherever the name goes."""
    provenance = derive_provenance(
        facts(name="lan-services", kind="bridge", addresses=["172.19.0.1"]),
        evidence(docker(network())),
    )
    claim = provenance.owner_for(OwnershipRelation.CONFIGURED_BY)
    assert claim.owner.provider == PROVIDER_DOCKER
    assert claim.owner.label == "monitoring_default"


def test_no_provider_evidence_at_all_is_an_incomplete_answer():
    provenance = derive_provenance(facts(name="docker0", kind="bridge"), EMPTY_EVIDENCE)
    assert provenance.reason == "evidence_incomplete"
    assert set(provenance.gaps) == {
        SOURCE_DOCKER_NETWORKS, SOURCE_NETWORKMANAGER_DEVICES, SOURCE_TAILSCALE_STATUS
    }
    assert source_named(provenance, SOURCE_DOCKER_NETWORKS).status == NEVER_CONSULTED


# --------------------------------------------------------------------------------- docker


def test_a_docker_declared_bridge_name_is_confirmed_ownership():
    provenance = derive_provenance(
        facts(name="docker0", kind="bridge", addresses=["172.17.0.1"]),
        evidence(docker(network(name="bridge", bridge_name="docker0", gateway="172.17.0.1"),
                        version="27.1.1")),
    )
    created = provenance.owner_for(OwnershipRelation.CREATED_BY)
    configured = provenance.owner_for(OwnershipRelation.CONFIGURED_BY)
    assert created.confidence is OwnershipConfidence.CONFIRMED
    assert created.reason == "docker_declared_bridge_name"
    assert configured.owner.provider == PROVIDER_DOCKER
    assert created.evidence[0].detail["declared_bridge_name"] == "docker0"
    assert provenance.reason == "externally_configured"


def test_a_docker_gateway_on_the_link_is_corroborated_ownership():
    """A bridge Docker never named, identified by the address Docker declared for it."""
    provenance = derive_provenance(
        facts(name="br-c0ffee000002", kind="bridge", addresses=["172.19.0.1"]),
        evidence(docker(network(gateway="172.19.0.1"))),
    )
    claim = provenance.owner_for(OwnershipRelation.CREATED_BY)
    assert claim.confidence is OwnershipConfidence.CORROBORATED
    assert claim.reason == "docker_ipam_gateway_on_link"
    assert claim.evidence[0].detail["gateway"] == "172.19.0.1"
    assert claim.evidence[0].detail["compose_project"] == "monitoring"


def test_a_bridge_docker_does_not_account_for_is_a_settled_no():
    provenance = derive_provenance(
        facts(name="br0", kind="bridge", addresses=["192.0.2.1"]),
        evidence(docker(network(gateway="172.19.0.1"))),
    )
    assert provenance.claims == ()
    docker_source = source_named(provenance, SOURCE_DOCKER_NETWORKS)
    assert docker_source.outcome == "no_matching_docker_network"
    assert docker_source.gap is False


def test_docker_naming_a_bridge_the_kernel_says_is_not_one_claims_nothing():
    """The provider and the kernel have to agree. A contradiction is not a conclusion."""
    provenance = derive_provenance(
        facts(name="eth0", kind="ethernet", addresses=["172.17.0.1"]),
        evidence(docker(network(name="bridge", bridge_name="eth0", gateway="172.17.0.1"))),
    )
    assert provenance.claims == ()
    assert source_named(provenance, SOURCE_DOCKER_NETWORKS).outcome == (
        "declared_bridge_is_not_a_kernel_bridge"
    )


def test_two_networks_claiming_one_gateway_attribute_neither():
    provenance = derive_provenance(
        facts(name="br0", kind="bridge", addresses=["172.19.0.1"]),
        evidence(docker(network(network_id="a" * 64), network(network_id="b" * 64))),
    )
    assert provenance.claims == ()
    docker_source = source_named(provenance, SOURCE_DOCKER_NETWORKS)
    assert docker_source.outcome == "ambiguous_gateway_match"
    assert docker_source.gap is True


def test_docker_that_is_not_installed_leaves_no_gap():
    provenance = derive_provenance(
        facts(name="br0", kind="bridge", addresses=[]),
        evidence(
            docker(status=ProviderStatus.ABSENT, reason="docker_socket_absent"),
            nm(status=ProviderStatus.ABSENT, reason="nmcli_absent"),
            tailscale(status=ProviderStatus.ABSENT, reason="tailscale_absent", tun=None),
        ),
    )
    assert provenance.state is OwnershipState.UNKNOWN
    assert provenance.reason == "no_provider_claim"
    assert provenance.gaps == ()


def test_docker_that_will_not_answer_is_a_gap_not_a_conclusion():
    provenance = derive_provenance(
        facts(name="br0", kind="bridge", addresses=["172.19.0.1"]),
        evidence(
            docker(status=ProviderStatus.UNAVAILABLE, reason="permission_denied"),
            nm(device(name="br0", state="unmanaged", connection=None, uuid=None)),
            tailscale(),
        ),
    )
    assert provenance.state is OwnershipState.UNKNOWN
    assert provenance.reason == "evidence_incomplete"
    assert provenance.gaps == (SOURCE_DOCKER_NETWORKS,)
    assert source_named(provenance, SOURCE_DOCKER_NETWORKS).outcome == "permission_denied"


def test_a_bridge_whose_addresses_could_not_be_read_cannot_be_matched():
    provenance = derive_provenance(
        facts(name="br0", kind="bridge", addresses=None),
        evidence(docker(network())),
    )
    assert provenance.claims == ()
    assert source_named(provenance, SOURCE_DOCKER_NETWORKS).outcome == (
        "interface_addresses_unavailable"
    )
    assert provenance.reason == "evidence_incomplete"


def test_a_non_bridge_network_driver_is_not_considered():
    provenance = derive_provenance(
        facts(name="br0", kind="bridge", addresses=["172.19.0.1"]),
        evidence(docker(network(driver="overlay"))),
    )
    assert provenance.claims == ()


# ------------------------------------------------------------------------- networkmanager


def test_an_active_networkmanager_profile_is_configuration_not_creation():
    """NetworkManager configures devices. It does not make them, and does not claim to."""
    provenance = derive_provenance(
        facts(name="eth0", is_physical=True, device_path="platform/fd580000.ethernet"),
        evidence(nm(device(name="eth0", connection="service-lan", uuid="uuid-lan"),
                    version="1.46.0")),
    )
    configured = provenance.owner_for(OwnershipRelation.CONFIGURED_BY)
    created = provenance.owner_for(OwnershipRelation.CREATED_BY)
    assert configured.owner.provider == PROVIDER_NETWORKMANAGER
    assert configured.owner.instance == "uuid-lan"
    assert configured.owner.label == "service-lan"
    assert configured.confidence is OwnershipConfidence.CONFIRMED
    # The kernel made the device; NetworkManager did not.
    assert created.owner.provider == PROVIDER_KERNEL
    assert created.owner.instance == "platform/fd580000.ethernet"


def test_a_device_networkmanager_calls_external_is_not_networkmanagers():
    """The posture every Docker bridge has on a NetworkManager host."""
    provenance = derive_provenance(
        facts(name="docker0", kind="bridge", addresses=[]),
        evidence(nm(device(name="docker0", state="connected", external=True,
                           connection="docker0", uuid="uuid-docker"))),
    )
    assert provenance.claims_for(OwnershipRelation.CONFIGURED_BY) == ()
    nm_source = source_named(provenance, SOURCE_NETWORKMANAGER_DEVICES)
    assert nm_source.outcome == "device_externally_configured"
    assert nm_source.gap is False


def test_an_unmanaged_device_is_a_disclaimer():
    provenance = derive_provenance(
        facts(name="tailscale0", kind="tunnel", addresses=[]),
        evidence(nm(device(name="tailscale0", state="unmanaged", connection=None, uuid=None))),
    )
    assert provenance.claims == ()
    assert source_named(provenance, SOURCE_NETWORKMANAGER_DEVICES).outcome == "device_unmanaged"


def test_a_managed_device_with_nothing_active_is_not_being_configured():
    """NetworkManager could configure this device. It is not doing so, and present tense
    is what `configured_by` means."""
    provenance = derive_provenance(
        facts(name="eth0", is_physical=True, device_path="platform/eth"),
        evidence(nm(device(name="eth0", state="unavailable", connection=None, uuid=None))),
    )
    assert provenance.claims_for(OwnershipRelation.CONFIGURED_BY) == ()
    nm_source = source_named(provenance, SOURCE_NETWORKMANAGER_DEVICES)
    assert nm_source.outcome == "device_managed_without_active_profile"
    assert nm_source.gap is False


def test_a_device_networkmanager_has_never_heard_of_claims_nothing():
    provenance = derive_provenance(
        facts(name="wwan0", is_physical=True, device_path="usb/1-1.2"),
        evidence(nm(device(name="eth0"))),
    )
    assert source_named(provenance, SOURCE_NETWORKMANAGER_DEVICES).outcome == "device_not_present"


# ------------------------------------------------------------------------------ tailscale


def test_a_tunnel_carrying_the_daemons_addresses_is_the_daemons():
    provenance = derive_provenance(
        facts(name="ts0", kind="tunnel", addresses=["100.64.0.10"]),
        evidence(tailscale(version="1.102.3")),
    )
    claim = provenance.owner_for(OwnershipRelation.CONFIGURED_BY)
    assert claim.owner.provider == PROVIDER_TAILSCALE
    assert claim.confidence is OwnershipConfidence.CORROBORATED
    assert claim.evidence[0].detail["matched_addresses"] == ["100.64.0.10"]


def test_a_stopped_daemon_leaves_the_question_open():
    """The interface outlives the daemon, so a stopped one settles nothing about it."""
    provenance = derive_provenance(
        facts(name="tailscale0", kind="tunnel", addresses=[]),
        evidence(tailscale(backend_state="Stopped")),
    )
    assert provenance.state is OwnershipState.UNKNOWN
    assert provenance.reason == "evidence_incomplete"
    source = source_named(provenance, SOURCE_TAILSCALE_STATUS)
    assert source.outcome == "backend_not_running"
    assert source.gap is True


def test_a_running_daemon_whose_addresses_are_elsewhere_settles_the_question():
    provenance = derive_provenance(
        facts(name="wg0", kind="tunnel", addresses=["10.9.0.2"]),
        evidence(tailscale()),
    )
    source = source_named(provenance, SOURCE_TAILSCALE_STATUS)
    assert source.outcome == "daemon_addresses_not_on_link"
    assert source.gap is False


def test_a_daemon_doing_userspace_networking_holds_no_interface():
    provenance = derive_provenance(
        facts(name="tailscale0", kind="tunnel", addresses=["100.64.0.10"]),
        evidence(tailscale(tun=False)),
    )
    assert provenance.claims == ()
    assert source_named(provenance, SOURCE_TAILSCALE_STATUS).outcome == "userspace_networking"


def test_a_bridge_is_never_a_tailnet_interface_whatever_the_daemon_is_doing():
    """And so a stopped daemon is not a gap for objects it could not own anyway."""
    provenance = derive_provenance(
        facts(name="docker0", kind="bridge", addresses=[]),
        evidence(tailscale(backend_state="Stopped")),
    )
    source = source_named(provenance, SOURCE_TAILSCALE_STATUS)
    assert source.outcome == "not_a_tunnel_link"
    assert source.gap is False


# --------------------------------------------------------------------------------- kernel


def test_the_loopback_device_is_the_kernels():
    provenance = derive_provenance(
        facts(name="lo", kind="loopback", arphrd_type=772, addresses=[]), EMPTY_EVIDENCE
    )
    claim = provenance.owner_for(OwnershipRelation.CREATED_BY)
    assert claim.owner.provider == PROVIDER_KERNEL
    assert claim.evidence[0].detail["arphrd_type"] == 772


def test_a_link_backed_by_hardware_was_made_by_the_kernel():
    provenance = derive_provenance(
        facts(name="eth0", is_physical=True, device_path="platform/fd580000.ethernet",
              addresses=[]),
        EMPTY_EVIDENCE,
    )
    claim = provenance.owner_for(OwnershipRelation.CREATED_BY)
    assert claim.owner.instance == "platform/fd580000.ethernet"
    assert claim.owner.external is False


def test_a_virtual_link_gets_no_kernel_claim():
    """Somebody made this veth *through* the kernel, and which somebody is the question."""
    provenance = derive_provenance(
        facts(name="veth0", kind="virtual_ethernet", addresses=[]), EMPTY_EVIDENCE
    )
    assert provenance.claims_for(OwnershipRelation.CREATED_BY) == ()


# ------------------------------------------------------- ownership is not management state


def test_derivation_never_sees_a_management_state():
    """Two objects in different management states, identical facts, identical ownership."""
    same = facts(name="br0", kind="bridge", addresses=["172.19.0.1"])
    provenance = derive_provenance(same, evidence(docker(network())))
    for state in ManagementState:
        eligibility = derive_adoption_eligibility(state, provenance)
        assert provenance.state is OwnershipState.ATTRIBUTED
        assert eligibility.eligible is False


def test_an_owned_object_is_refused_adoption_and_named():
    provenance = derive_provenance(
        facts(name="br0", kind="bridge", addresses=["172.19.0.1"]),
        evidence(
            docker(network()),
            nm(device(name="br0", state="connected", external=True, connection="br0")),
            tailscale(),
        ),
    )
    eligibility = derive_adoption_eligibility(ManagementState.OBSERVED, provenance)
    assert eligibility == AdoptionEligibility(
        eligible=False,
        reason="externally_configured",
        blocked_by=external_owner(provenance).owner,
        evidence_gaps=(),
    )


def test_an_ordinary_physical_interface_stays_eligible():
    provenance = derive_provenance(
        facts(name="eth0", is_physical=True, device_path="platform/eth", addresses=[]),
        evidence(
            docker(),
            nm(device(name="eth0", state="unavailable", connection=None, uuid=None)),
            tailscale(),
        ),
    )
    eligibility = derive_adoption_eligibility(ManagementState.OBSERVED, provenance)
    assert eligibility.eligible is True
    assert eligibility.reason == "no_external_owner_proven"
    assert eligibility.evidence_gaps == ()


def test_unknown_ownership_does_not_become_external_ownership():
    """The whole point of the unknown/attributed split: a gap is not an owner."""
    provenance = derive_provenance(
        facts(name="eth0", is_physical=True, device_path="platform/eth", addresses=[]),
        evidence(docker(status=ProviderStatus.UNAVAILABLE, reason="permission_denied")),
    )
    eligibility = derive_adoption_eligibility(ManagementState.OBSERVED, provenance)
    assert external_owner(provenance) is None
    assert eligibility.eligible is True
    # Eligible, and honest about what it could not check.
    assert SOURCE_DOCKER_NETWORKS in eligibility.evidence_gaps


def test_observe_only_is_never_adoptable_however_clean_the_ownership():
    provenance = derive_provenance(
        facts(name="lo", kind="loopback", arphrd_type=772, addresses=[]),
        evidence(docker(), nm(), tailscale()),
    )
    eligibility = derive_adoption_eligibility(ManagementState.OBSERVE_ONLY, provenance)
    assert eligibility.eligible is False
    assert eligibility.reason == "object_observe_only"


def test_an_already_managed_object_is_not_adoptable_again():
    provenance = derive_provenance(facts(), evidence(docker(), nm(), tailscale()))
    eligibility = derive_adoption_eligibility(ManagementState.MANAGED, provenance)
    assert eligibility.reason == "already_managed"


def test_two_systems_claiming_one_object_is_reported_not_resolved():
    """A bridge Docker made and NetworkManager is also actively driving."""
    provenance = derive_provenance(
        facts(name="br0", kind="bridge", addresses=["172.19.0.1"]),
        evidence(
            docker(network()),
            nm(device(name="br0", state="connected", connection="bridge-br0", uuid="uuid-b")),
        ),
    )
    assert OwnershipRelation.CONFIGURED_BY in provenance.conflicting_relations
    assert provenance.reason == "conflicting_claims"
    # Neither is picked as the winner.
    assert provenance.owner_for(OwnershipRelation.CONFIGURED_BY) is None
    eligibility = derive_adoption_eligibility(ManagementState.OBSERVED, provenance)
    assert eligibility.reason == "conflicting_ownership_claims"
    assert eligibility.blocked_by is None
