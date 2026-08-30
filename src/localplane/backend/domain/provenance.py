"""Who made this object, who configures it, and what evidence says so.

Ownership is a **separate axis from management**, and the distinction is the whole point of
this module. Management is LocalPlane's stance towards an object — three values, chosen by
an operator through adopt and release. Ownership is a fact about the host: something out
there created this bridge, something out there is applying configuration to that link, and
LocalPlane either has evidence for that or it does not. A Docker-created bridge does not
become a fourth management state; it stays ``observed`` — a thing LocalPlane watches — and
what changes is that LocalPlane now knows it must not offer to take responsibility for it.

**Two relations, deliberately not one.**

``created_by``
    What brought the object into existence and would bring it back. This governs
    *lifetime*: an intent retained for a bridge that dockerd recreates on every daemon
    start is an intent about something that is not stable.
``configured_by``
    What is applying configuration to the object *now*. This governs *writes*: two systems
    setting the same link's state is the oldest failure in this domain, and it is the
    reason a NetworkManager-controlled device is not a management candidate for a build
    that has no NetworkManager write path.

They are frequently different. A USB ethernet adapter is created by the kernel binding a
driver to hardware and configured by NetworkManager applying a profile to it. Collapsing
them into one "owner" field would force a choice between claiming NetworkManager made the
hardware and claiming nothing configures the link — both false.

**Every claim is evidence-backed, and no claim is made from a name.** Nothing here matches
``docker0``, ``br-``, ``tailscale0`` or any other convention. What it matches is what a
provider declared against what the kernel reported:

* Docker declaring ``com.docker.network.bridge.name`` for one of its networks, or
  declaring an IPAM gateway that is, uniquely, an address the kernel reports on that link;
* NetworkManager reporting an active, non-external connection bound to the device;
* tailscaled reporting the addresses it holds, found on a tun link.

Where the provider's declaration and the kernel disagree — Docker names a bridge that the
kernel says is not a bridge — no claim is made at all. The two have to agree.

**Absence of a claim is a first-class answer, and it comes in two kinds.** A source that
was consulted and settled the question ("NetworkManager manages this device and is applying
nothing to it", "no Docker network corresponds to this link") leaves no gap. A source that
could not be consulted, or that answered in a way that settles nothing, leaves a gap, and
the assessment says so. ``unknown`` with an explicit reason is the correct answer far more
often than any guess would be, and it is what this module returns whenever the evidence
runs out.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping

from localplane.backend.domain.network import KIND_BRIDGE, KIND_LOOPBACK, KIND_TUNNEL
from localplane.backend.domain.states import ManagementState
from localplane.protocol.providers import (
    PROVIDER_DOCKER,
    PROVIDER_KERNEL,
    PROVIDER_LOCALPLANE,
    PROVIDER_NETWORKMANAGER,
    PROVIDER_SOURCES,
    PROVIDER_TAILSCALE,
    SOURCE_DOCKER_CONTAINERS,
    SOURCE_KERNEL_INTERFACE,
    ProviderStatus,
)


class OwnershipRelation(StrEnum):
    """How a system relates to an object. Not interchangeable; see the module docstring."""

    CREATED_BY = "created_by"
    CONFIGURED_BY = "configured_by"


class OwnershipConfidence(StrEnum):
    """How well the evidence supports a claim.

    There is no third, weaker value. LocalPlane does not emit suspicions: a claim that
    would need one has no business being made, and is reported as ``unknown`` instead.
    """

    CONFIRMED = "confirmed"
    """The provider names this object itself, and the kernel agrees with the naming."""

    CORROBORATED = "corroborated"
    """A provider's declaration matches, uniquely, a fact the kernel reports about it."""


class OwnershipState(StrEnum):
    """Whether anything is claimed about this object at all."""

    ATTRIBUTED = "attributed"
    UNKNOWN = "unknown"


#: The systems whose ownership means LocalPlane is not the one to be answerable for an
#: object. The kernel is not among them — every link on a Linux host is the kernel's in
#: some sense, and it is not a competing writer. Neither is LocalPlane itself.
_NOT_EXTERNAL = frozenset({PROVIDER_KERNEL, PROVIDER_LOCALPLANE})

NEVER_CONSULTED = "never_consulted"
"""A source that reported nothing in the newest evidence. Distinct from refusing to answer."""


@dataclass(frozen=True)
class OwnerIdentity:
    """Which system, and which thing within it.

    ``instance`` is what makes a claim checkable: not "Docker" but "the Docker network
    ``dd9ca0…``"; not "NetworkManager" but "the connection ``0f6ad2ef…``". An operator can
    take that identifier to the provider and see the same record.
    """

    provider: str
    instance: str | None = None
    label: str | None = None
    version: str | None = None

    @property
    def external(self) -> bool:
        return self.provider not in _NOT_EXTERNAL


@dataclass(frozen=True)
class OwnershipEvidence:
    """One machine-readable fact supporting a claim."""

    source: str
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)
    observed_at: str | None = None


@dataclass(frozen=True)
class OwnershipClaim:
    """One relation, one owner, and everything that argues for it."""

    relation: OwnershipRelation
    owner: OwnerIdentity
    confidence: OwnershipConfidence
    reason: str
    evidence: tuple[OwnershipEvidence, ...] = ()


@dataclass(frozen=True)
class ConsultedSource:
    """What one source contributed to this object's assessment.

    Present for every source LocalPlane knows how to consult, including the ones that had
    nothing to say — "NetworkManager was asked about this device and disclaims it" is a
    result, and a reader who cannot see it cannot tell a settled question from an
    unexamined one.
    """

    source: str
    provider: str
    status: str
    outcome: str
    gap: bool
    """True when this source left the question open.

    Either it could not be consulted, or it answered in a way that settles nothing while
    leaving something to settle. A source that answered definitively — "no Docker network
    corresponds to this link" — is not a gap, even though it produced no claim.
    """

    observed_at: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Provenance:
    """The whole ownership answer for one object."""

    state: OwnershipState
    reason: str
    claims: tuple[OwnershipClaim, ...] = ()
    sources: tuple[ConsultedSource, ...] = ()

    @property
    def gaps(self) -> tuple[str, ...]:
        return tuple(s.source for s in self.sources if s.gap)

    def claims_for(self, relation: OwnershipRelation) -> tuple[OwnershipClaim, ...]:
        return tuple(c for c in self.claims if c.relation is relation)

    def owner_for(self, relation: OwnershipRelation) -> OwnershipClaim | None:
        """The single claim for a relation, or ``None`` when there is not exactly one.

        Disagreement is not resolved by picking a winner. Two systems claiming to
        configure the same object is a condition to report, not to average.
        """
        claims = self.claims_for(relation)
        return claims[0] if len(claims) == 1 else None

    @property
    def conflicting_relations(self) -> tuple[OwnershipRelation, ...]:
        return tuple(
            relation
            for relation in OwnershipRelation
            if len({c.owner.provider for c in self.claims_for(relation)}) > 1
        )


@dataclass(frozen=True)
class ProviderReadingView:
    """One provider's newest reading, as the derivation needs to see it."""

    provider: str
    source: str
    status: str
    observed_at: str
    reason: str | None = None
    version: str | None = None
    records: tuple[dict[str, Any], ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)
    provider_observation_id: str | None = None
    sweep_id: str | None = None


@dataclass(frozen=True)
class ProviderEvidence:
    """The newest reading from each provider LocalPlane can consult."""

    readings: Mapping[str, ProviderReadingView] = field(default_factory=dict)

    @classmethod
    def of(cls, readings: Iterable[ProviderReadingView]) -> "ProviderEvidence":
        return cls({r.provider: r for r in readings})

    def get(self, provider: str) -> ProviderReadingView | None:
        return self.readings.get(provider)


EMPTY_EVIDENCE = ProviderEvidence()


# --------------------------------------------------------------------------------------
# derivation
# --------------------------------------------------------------------------------------


def derive_container_provenance(
    facts: Mapping[str, Any], evidence: ProviderEvidence
) -> Provenance:
    """Who owns a Docker container. The one case where the answer needs no correlation.

    Every other ownership question in LocalPlane is a correlation problem: a provider
    declares something, the kernel reports something else, and the work is deciding whether
    the two are about the same object without ever joining on a name. A container is not
    that. It exists **because** the daemon created it, its whole configuration is the
    daemon's record of it, and LocalPlane learned of it by asking the daemon — so the claim
    and the evidence for it are the same fact, and the confidence is ``confirmed`` on the
    strongest possible grounds rather than as a courtesy.

    Both relations are claimed and they are still two relations. Docker **created** this
    container and Docker **configures** it — its restart policy, its mounts, its networks
    and its published ports are all held by the daemon and reapplied by the daemon. That is
    the ordinary case; conflating the two would lose the ability to say anything else if a
    container ever turns out to be created by one system and configured by another.

    **This does not stop LocalPlane operating the container**, and the reason it does not is
    argued in :func:`ownership_block`: the three lifecycle actions execute through Docker's
    own API, so Docker remains the single writer and there is no second one to conflict
    with. Adoption is a different act and is still refused, because adopting would mean
    LocalPlane retaining a desired state for something whose desired state is Docker's.
    """
    reading = evidence.get(PROVIDER_DOCKER)
    container_id = facts.get("container_id")
    name = facts.get("name")

    if not isinstance(container_id, str) or not container_id:
        return Provenance(
            OwnershipState.UNKNOWN,
            "container_id_unreadable",
            sources=(
                ConsultedSource(
                    source=SOURCE_DOCKER_CONTAINERS,
                    provider=PROVIDER_DOCKER,
                    status=reading.status if reading else NEVER_CONSULTED,
                    outcome="container_id_unreadable",
                    gap=True,
                    observed_at=reading.observed_at if reading else None,
                ),
            ),
        )

    owner = OwnerIdentity(
        provider=PROVIDER_DOCKER,
        instance=container_id,
        label=name if isinstance(name, str) else None,
        version=reading.version if reading else None,
    )
    proof = OwnershipEvidence(
        source=SOURCE_DOCKER_CONTAINERS,
        kind="daemon_declares_container",
        detail={"container_id": container_id, "name": name},
        observed_at=reading.observed_at if reading else facts.get("observed_at"),
    )
    claims = tuple(
        OwnershipClaim(
            relation=relation,
            owner=owner,
            confidence=OwnershipConfidence.CONFIRMED,
            reason=reason,
            evidence=(proof,),
        )
        for relation, reason in (
            (OwnershipRelation.CREATED_BY, "docker_created_this_container"),
            (OwnershipRelation.CONFIGURED_BY, "docker_holds_this_container_s_configuration"),
        )
    )
    return Provenance(
        OwnershipState.ATTRIBUTED,
        "docker_declares_this_container",
        claims=claims,
        sources=(
            ConsultedSource(
                source=SOURCE_DOCKER_CONTAINERS,
                provider=PROVIDER_DOCKER,
                # The observation itself is the reading. There is no separate provider
                # record to have failed independently: if the daemon had not answered there
                # would be no container object here to ask about.
                status=str(ProviderStatus.OK),
                outcome="container_declared_by_daemon",
                gap=False,
                observed_at=proof.observed_at,
                detail={"container_id": container_id},
            ),
        ),
    )


def derive_provenance(facts: Mapping[str, Any], evidence: ProviderEvidence) -> Provenance:
    """Decide what is known about one interface's ownership, and why.

    ``facts`` are the normalized facts of the newest observation. Every source is consulted
    for every object, because "this source has nothing to do with this link" is part of the
    answer and leaving it out would make an unexamined source indistinguishable from an
    irrelevant one.
    """
    claims: list[OwnershipClaim] = []
    sources: list[ConsultedSource] = []

    kernel_claim, kernel_source = _kernel(facts)
    if kernel_claim is not None:
        claims.append(kernel_claim)
    sources.append(kernel_source)

    for derive in (_docker, _networkmanager, _tailscale):
        found, source = derive(facts, evidence)
        claims.extend(found)
        sources.append(source)

    return _assemble(tuple(claims), tuple(sources))


def _assemble(
    claims: tuple[OwnershipClaim, ...], sources: tuple[ConsultedSource, ...]
) -> Provenance:
    provenance = Provenance(OwnershipState.UNKNOWN, "", claims, sources)
    if provenance.conflicting_relations:
        return Provenance(OwnershipState.ATTRIBUTED, "conflicting_claims", claims, sources)
    if any(c.owner.external for c in claims if c.relation is OwnershipRelation.CONFIGURED_BY):
        return Provenance(OwnershipState.ATTRIBUTED, "externally_configured", claims, sources)
    if any(c.owner.external for c in claims):
        return Provenance(OwnershipState.ATTRIBUTED, "externally_created", claims, sources)
    if claims:
        return Provenance(OwnershipState.ATTRIBUTED, "host_kernel_only", claims, sources)
    if provenance.gaps:
        # No claim, and at least one source could not settle its part. This is the honest
        # unknown: something might own this object and LocalPlane could not find out.
        return Provenance(OwnershipState.UNKNOWN, "evidence_incomplete", claims, sources)
    return Provenance(OwnershipState.UNKNOWN, "no_provider_claim", claims, sources)


# ------------------------------------------------------------------------------- kernel


def _kernel(facts: Mapping[str, Any]) -> tuple[OwnershipClaim | None, ConsultedSource]:
    """What the kernel's own account of the link establishes about its existence.

    Two cases, both evidenced and neither inferred from a name:

    * ``ARPHRD_LOOPBACK`` is the kernel declaring this is *the* loopback device. No
      userspace process creates one.
    * a link with a device node on a bus exists because the kernel bound a driver to
      hardware. ``device_path`` is that node.

    Everything else — bridges, veth pairs, tun devices — is created *through* the kernel by
    somebody, and which somebody is exactly the question the other sources answer. Claiming
    the kernel created them would be true in a sense that is useless and misleading.
    """
    source = SOURCE_KERNEL_INTERFACE
    kind = facts.get("kind")

    if kind == KIND_LOOPBACK:
        return (
            OwnershipClaim(
                relation=OwnershipRelation.CREATED_BY,
                owner=OwnerIdentity(provider=PROVIDER_KERNEL),
                confidence=OwnershipConfidence.CONFIRMED,
                reason="kernel_loopback_device",
                evidence=(
                    OwnershipEvidence(
                        source=source,
                        kind="arphrd_type",
                        detail={"arphrd_type": facts.get("arphrd_type"), "kind": kind},
                    ),
                ),
            ),
            ConsultedSource(
                source=source,
                provider=PROVIDER_KERNEL,
                status=str(ProviderStatus.OK),
                outcome="kernel_loopback_device",
                gap=False,
            ),
        )

    device_path = facts.get("device_path")
    if facts.get("is_physical") and isinstance(device_path, str) and device_path:
        return (
            OwnershipClaim(
                relation=OwnershipRelation.CREATED_BY,
                owner=OwnerIdentity(provider=PROVIDER_KERNEL, instance=device_path),
                confidence=OwnershipConfidence.CONFIRMED,
                reason="kernel_device_backed",
                evidence=(
                    OwnershipEvidence(
                        source=source,
                        kind="device_path",
                        detail={"device_path": device_path, "is_physical": True},
                    ),
                ),
            ),
            ConsultedSource(
                source=source,
                provider=PROVIDER_KERNEL,
                status=str(ProviderStatus.OK),
                outcome="kernel_device_backed",
                gap=False,
            ),
        )

    return None, ConsultedSource(
        source=source,
        provider=PROVIDER_KERNEL,
        status=str(ProviderStatus.OK),
        # A virtual link the kernel did not create on its own account. Who did is what the
        # provider sources are for; the kernel simply has no claim to make.
        outcome="virtual_link_no_kernel_claim",
        gap=False,
    )


# ------------------------------------------------------------------------------- docker


def _docker(
    facts: Mapping[str, Any], evidence: ProviderEvidence
) -> tuple[tuple[OwnershipClaim, ...], ConsultedSource]:
    reading = evidence.get(PROVIDER_DOCKER)
    unusable = _unusable_source(PROVIDER_DOCKER, reading)
    if unusable is not None:
        return (), unusable
    assert reading is not None

    networks = [r for r in reading.records if r.get("driver") == "bridge"]
    name = facts.get("name")
    kind = facts.get("kind")
    addresses = _addresses(facts)

    def source(outcome: str, gap: bool = False, **detail: Any) -> ConsultedSource:
        return ConsultedSource(
            source=reading.source,
            provider=PROVIDER_DOCKER,
            status=reading.status,
            outcome=outcome,
            gap=gap,
            observed_at=reading.observed_at,
            detail=detail,
        )

    # Docker naming the host bridge itself is the strongest evidence there is — but only
    # if the kernel agrees the link is a bridge. A declaration that contradicts the kernel
    # is a reason to claim nothing, not a reason to believe the declaration.
    named = [n for n in networks if n.get("bridge_name") and n.get("bridge_name") == name]
    if len(named) > 1:
        return (), source("ambiguous_declared_bridge_name", gap=True, networks=len(named))
    if named:
        if kind != KIND_BRIDGE:
            return (), source(
                "declared_bridge_is_not_a_kernel_bridge", gap=True, observed_kind=kind
            )
        network = named[0]
        return (
            _docker_claims(
                reading,
                network,
                OwnershipConfidence.CONFIRMED,
                "docker_declared_bridge_name",
                OwnershipEvidence(
                    source=reading.source,
                    kind="declared_bridge_name",
                    detail={
                        "network_id": network.get("network_id"),
                        "network_name": network.get("name"),
                        "declared_bridge_name": network.get("bridge_name"),
                        "interface_name": name,
                        "default_bridge": network.get("default_bridge"),
                    },
                    observed_at=reading.observed_at,
                ),
            ),
            source(
                "matched_declared_bridge_name",
                network_id=network.get("network_id"),
                network_name=network.get("name"),
            ),
        )

    if kind != KIND_BRIDGE:
        # Docker's networks are bridges here. A link that is not one is not a Docker
        # network's bridge, and that is a settled answer rather than a gap.
        return (), source("not_a_bridge", observed_kind=kind)
    if addresses is None:
        return (), source("interface_addresses_unavailable", gap=True)

    # The remaining evidence is Docker's own IPAM: it declares a gateway address for each
    # network, and the kernel reports the addresses on each link. A gateway that is on
    # this link, and that only one Docker network declares, identifies the network. This
    # is not name matching — the bridge could be called anything.
    declared = Counter(
        gateway for n in networks for gateway in _gateways(n)
    )
    matched = [
        (n, gateway)
        for n in networks
        for gateway in _gateways(n)
        if gateway in addresses and declared[gateway] == 1
    ]
    unique_networks = {n.get("network_id") for n, _ in matched}
    if len(unique_networks) > 1:
        return (), source(
            "ambiguous_gateway_match", gap=True, networks=sorted(str(i) for i in unique_networks)
        )
    if not matched:
        ambiguous = [g for n in networks for g in _gateways(n) if g in addresses]
        if ambiguous:
            return (), source("ambiguous_gateway_match", gap=True, gateways=sorted(set(ambiguous)))
        return (), source("no_matching_docker_network", networks=len(networks))

    network, gateway = matched[0]
    return (
        _docker_claims(
            reading,
            network,
            OwnershipConfidence.CORROBORATED,
            "docker_ipam_gateway_on_link",
            OwnershipEvidence(
                source=reading.source,
                kind="ipam_gateway_address",
                detail={
                    "network_id": network.get("network_id"),
                    "network_name": network.get("name"),
                    "gateway": gateway,
                    "subnets": [c.get("subnet") for c in network.get("ipam") or []],
                    "interface_addresses": sorted(addresses),
                    "compose_project": network.get("compose_project"),
                },
                observed_at=reading.observed_at,
            ),
        ),
        source(
            "matched_ipam_gateway",
            network_id=network.get("network_id"),
            network_name=network.get("name"),
            gateway=gateway,
        ),
    )


def _docker_claims(
    reading: ProviderReadingView,
    network: Mapping[str, Any],
    confidence: OwnershipConfidence,
    reason: str,
    evidence: OwnershipEvidence,
) -> tuple[OwnershipClaim, ...]:
    """Both relations, from one piece of evidence, because both are what it shows.

    A Docker network's bridge exists because dockerd created it — it is recreated on the
    next daemon start if it is removed — and its address is the gateway dockerd declared
    for that network. The same record establishes creation and configuration; splitting it
    into two differently-evidenced claims would suggest LocalPlane knows something about
    one that it does not know about the other.
    """
    owner = OwnerIdentity(
        provider=PROVIDER_DOCKER,
        instance=network.get("network_id"),
        label=network.get("name"),
        version=reading.version,
    )
    return tuple(
        OwnershipClaim(
            relation=relation,
            owner=owner,
            confidence=confidence,
            reason=reason,
            evidence=(evidence,),
        )
        for relation in (OwnershipRelation.CREATED_BY, OwnershipRelation.CONFIGURED_BY)
    )


def _gateways(network: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(config.get("gateway"))
        for config in network.get("ipam") or []
        if isinstance(config, Mapping) and config.get("gateway")
    )


# ----------------------------------------------------------------------- networkmanager


def _networkmanager(
    facts: Mapping[str, Any], evidence: ProviderEvidence
) -> tuple[tuple[OwnershipClaim, ...], ConsultedSource]:
    """NetworkManager's posture towards this device, taken exactly as far as it goes.

    Being *present* in NetworkManager's device table is not ownership: NetworkManager
    enumerates every link on the host, including the ones it has been told to leave alone
    and the ones whose configuration it merely found. Only an active, non-external
    connection is NetworkManager applying something to a device, and only that is claimed.
    """
    reading = evidence.get(PROVIDER_NETWORKMANAGER)
    unusable = _unusable_source(PROVIDER_NETWORKMANAGER, reading)
    if unusable is not None:
        return (), unusable
    assert reading is not None

    name = facts.get("name")
    # Joined on the kernel interface name, which is the identifier both the kernel and
    # NetworkManager use for the same device. That is a join on a shared identity, not a
    # guess from what the interface is called.
    device = next((r for r in reading.records if r.get("device") == name), None)

    def source(outcome: str, gap: bool = False, **detail: Any) -> ConsultedSource:
        return ConsultedSource(
            source=reading.source,
            provider=PROVIDER_NETWORKMANAGER,
            status=reading.status,
            outcome=outcome,
            gap=gap,
            observed_at=reading.observed_at,
            detail=detail,
        )

    if device is None:
        return (), source("device_not_present", devices=len(reading.records))
    state = device.get("state")
    if state == "unmanaged":
        return (), source("device_unmanaged", state=device.get("state_raw"))
    if device.get("external"):
        # NetworkManager is describing a configuration it did not make. This is the
        # posture every Docker bridge has on a NetworkManager host, and reading it as
        # ownership would attribute all of them to the wrong system.
        return (), source(
            "device_externally_configured",
            state=device.get("state_raw"),
            connection=device.get("connection"),
        )
    if state != "connected" or not device.get("connection_uuid"):
        return (), source(
            "device_managed_without_active_profile", state=device.get("state_raw")
        )

    owner = OwnerIdentity(
        provider=PROVIDER_NETWORKMANAGER,
        instance=device.get("connection_uuid"),
        label=device.get("connection"),
        version=reading.version,
    )
    claim = OwnershipClaim(
        relation=OwnershipRelation.CONFIGURED_BY,
        owner=owner,
        confidence=OwnershipConfidence.CONFIRMED,
        reason="networkmanager_active_profile",
        evidence=(
            OwnershipEvidence(
                source=reading.source,
                kind="active_connection",
                detail={
                    "device": device.get("device"),
                    "device_type": device.get("device_type"),
                    "state": device.get("state_raw"),
                    "connection": device.get("connection"),
                    "connection_uuid": device.get("connection_uuid"),
                },
                observed_at=reading.observed_at,
            ),
        ),
    )
    # No `created_by`: NetworkManager configures devices, it does not make them. Claiming
    # otherwise would say NetworkManager produced the hardware.
    return (claim,), source(
        "device_actively_configured",
        connection=device.get("connection"),
        connection_uuid=device.get("connection_uuid"),
    )


# ---------------------------------------------------------------------------- tailscale


def _tailscale(
    facts: Mapping[str, Any], evidence: ProviderEvidence
) -> tuple[tuple[OwnershipClaim, ...], ConsultedSource]:
    """Attribute a tun link to tailscaled only when the daemon's own addresses are on it.

    The interface being called ``tailscale0`` establishes nothing. What establishes
    something is the daemon reporting that it is running, that it holds a kernel interface
    rather than doing userspace networking, and that its addresses are the addresses the
    kernel reports on this link.

    A daemon that is installed but stopped leaves a gap rather than a conclusion: the
    interface it created outlives it, so a stopped daemon is not evidence that this link
    is not Tailscale's.
    """
    reading = evidence.get(PROVIDER_TAILSCALE)
    unusable = _unusable_source(PROVIDER_TAILSCALE, reading)
    if unusable is not None:
        return (), unusable
    assert reading is not None

    def source(outcome: str, gap: bool = False, **detail: Any) -> ConsultedSource:
        return ConsultedSource(
            source=reading.source,
            provider=PROVIDER_TAILSCALE,
            status=reading.status,
            outcome=outcome,
            gap=gap,
            observed_at=reading.observed_at,
            detail=detail,
        )

    daemon = reading.records[0] if reading.records else None
    if daemon is None:
        return (), source("daemon_state_unreadable", gap=True)

    backend_state = daemon.get("backend_state")
    # The kind check comes first, and it is what keeps the gap below narrow. A tailnet
    # interface is a tun device; a bridge or a physical NIC is not one whatever the daemon
    # is doing, so for those the question is settled without consulting its state at all.
    if facts.get("kind") != KIND_TUNNEL:
        return (), source("not_a_tunnel_link", observed_kind=facts.get("kind"))
    if daemon.get("tun") is False:
        # Userspace networking: the daemon holds no kernel interface at all, so no link on
        # this host is its. A settled answer.
        return (), source("userspace_networking", backend_state=backend_state)
    if backend_state != "Running":
        # A tun link and a stopped daemon. The interface outlives the daemon that made it,
        # so a stopped tailscaled is not evidence that this link is not Tailscale's — and
        # the name it happens to have is not evidence that it is.
        return (), source("backend_not_running", gap=True, backend_state=backend_state)

    addresses = _addresses(facts)
    if addresses is None:
        return (), source("interface_addresses_unavailable", gap=True)

    daemon_addresses = {a for a in daemon.get("tailscale_ips") or [] if isinstance(a, str)}
    matched = sorted(daemon_addresses & addresses)
    if not matched:
        return (), source(
            "daemon_addresses_not_on_link",
            daemon_addresses=sorted(daemon_addresses),
        )

    owner = OwnerIdentity(
        provider=PROVIDER_TAILSCALE,
        instance=daemon.get("dns_name") or daemon.get("hostname"),
        label=daemon.get("hostname"),
        version=reading.version,
    )
    evidence_item = OwnershipEvidence(
        source=reading.source,
        kind="daemon_addresses_on_link",
        detail={
            "matched_addresses": matched,
            "daemon_addresses": sorted(daemon_addresses),
            "backend_state": backend_state,
            "tun": daemon.get("tun"),
        },
        observed_at=reading.observed_at,
    )
    claims = tuple(
        OwnershipClaim(
            relation=relation,
            owner=owner,
            confidence=OwnershipConfidence.CORROBORATED,
            reason="tailscale_daemon_addresses_on_link",
            evidence=(evidence_item,),
        )
        for relation in (OwnershipRelation.CREATED_BY, OwnershipRelation.CONFIGURED_BY)
    )
    return claims, source("matched_daemon_addresses", matched=matched)


# ------------------------------------------------------------------------------ helpers


def _unusable_source(provider: str, reading: ProviderReadingView | None) -> ConsultedSource | None:
    """The source outcome when a provider produced nothing usable, or ``None`` if it did.

    ``absent`` is the one non-``ok`` status that settles the question: a daemon that is not
    installed owns nothing here.
    """
    if reading is None:
        return ConsultedSource(
            source=PROVIDER_SOURCES.get(provider, provider),
            provider=provider,
            status=NEVER_CONSULTED,
            outcome="no_provider_evidence",
            gap=True,
        )
    if reading.status == ProviderStatus.ABSENT:
        return ConsultedSource(
            source=reading.source,
            provider=provider,
            status=reading.status,
            outcome=reading.reason or "provider_absent",
            gap=False,
            observed_at=reading.observed_at,
        )
    if reading.status != ProviderStatus.OK:
        return ConsultedSource(
            source=reading.source,
            provider=provider,
            status=reading.status,
            outcome=reading.reason or "provider_unreadable",
            gap=True,
            observed_at=reading.observed_at,
            detail=reading.detail,
        )
    return None


def _addresses(facts: Mapping[str, Any]) -> set[str] | None:
    """The addresses on the link, or ``None`` when the source that lists them failed.

    ``None`` and ``set()`` are different answers and both are used: an empty set means the
    link genuinely has no addresses, which settles a gateway comparison in the negative.
    """
    addresses = facts.get("addresses")
    if addresses is None:
        return None
    return {
        a["address"]
        for a in addresses
        if isinstance(a, Mapping) and isinstance(a.get("address"), str)
    }


# --------------------------------------------------------------------------------------
# what ownership means for management
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AdoptionEligibility:
    """Whether LocalPlane may take responsibility for an object, and why not if not.

    Separate from both management state and provenance on purpose. Management is where the
    object *is*; provenance is what is *true about* it; eligibility is the policy decision
    that reads both — and it is a decision this build makes conservatively, because it has
    no write path for anything.
    """

    eligible: bool
    reason: str
    blocked_by: OwnerIdentity | None = None
    evidence_gaps: tuple[str, ...] = ()
    """Sources that left the question open. Present whether or not the object is eligible.

    A gap never makes an object eligible that would otherwise not be, and it does not by
    itself make one ineligible: adoption records values that are already true and writes
    nothing. It is reported so that neither an operator nor a later write path mistakes an
    unexamined source for a clean one.
    """


def external_owner(
    provenance: Provenance, *, ignoring: str | None = None
) -> OwnershipClaim | None:
    """The claim, if any, that puts this object in somebody else's hands.

    ``configured_by`` outranks ``created_by``: something applying configuration now is the
    more immediate reason LocalPlane must not.

    ``ignoring`` names one provider whose claim is not being asked about — see
    :func:`ownership_block`. It never widens the answer: a second external owner is still
    found, and this is the only place the exemption is applied so there is one reading of
    what it means.
    """
    for relation in (OwnershipRelation.CONFIGURED_BY, OwnershipRelation.CREATED_BY):
        for claim in provenance.claims_for(relation):
            if claim.owner.external and claim.owner.provider != ignoring:
                return claim
    return None


def ownership_block(
    provenance: Provenance, *, acting_through: str | None = None
) -> tuple[str, OwnerIdentity | None] | None:
    """Why ownership forbids LocalPlane taking new responsibility, or ``None``.

    **``acting_through`` is the difference between acting against an owner and acting
    through one**, and it is the reason a Docker container can be operated at all.

    The block exists because a write LocalPlane performs behind another system's back makes
    it a second writer: setting an MTU with netlink on a link NetworkManager is configuring
    means two systems disagreeing about one value, and the one that reapplies its profile
    last wins. That argument is about *how* the write is made. It does not apply to an
    operation whose execution path is the owner's own interface — asking the Docker daemon
    to start a container it created and configures is not competing with Docker, it is
    using it, and the daemon remains the single writer throughout.

    So a claim naming the provider an operation acts through is not a block. A claim naming
    any *other* provider still is, and conflicting claims still are, because both of those
    are cases where somebody else would be surprised. An operation that names no provider —
    every operation before this one — is unaffected, and the argument for it is unchanged.
    """
    if provenance.conflicting_relations:
        return "conflicting_ownership_claims", None
    claim = external_owner(provenance, ignoring=acting_through)
    if claim is None:
        return None
    reason = (
        "externally_configured"
        if claim.relation is OwnershipRelation.CONFIGURED_BY
        else "externally_created"
    )
    return reason, claim.owner


def derive_adoption_eligibility(
    management_state: str, provenance: Provenance
) -> AdoptionEligibility:
    """Whether this object could be adopted right now.

    The order matters and is not arbitrary. Structural impossibility first — an object
    LocalPlane will never write is never a candidate, whoever owns it. Then the management
    state, because adopt is a transition out of ``observed`` and nothing else. Then
    ownership: LocalPlane refuses to become answerable for
    an object another system is demonstrably running, because it has no write model that
    could coexist with one.
    """
    gaps = provenance.gaps
    if management_state == ManagementState.OBSERVE_ONLY:
        return AdoptionEligibility(False, "object_observe_only", None, gaps)
    if management_state == ManagementState.MANAGED:
        return AdoptionEligibility(False, "already_managed", None, gaps)
    block = ownership_block(provenance)
    if block is not None:
        reason, owner = block
        return AdoptionEligibility(False, reason, owner, gaps)
    return AdoptionEligibility(True, "no_external_owner_proven", None, gaps)
