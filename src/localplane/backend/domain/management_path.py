"""Which object carries the connection an operator is reaching LocalPlane over.

**The most expensive question in the product, and the one a wrong answer is unrecoverable
from.** An operator whose management path is removed does not get an error message; they
get silence, and whatever LocalPlane does next it does unobserved. So the rule this module
exists to hold is that the answer is *proven or unknown*, and there is no third option
where a plausible guess stands in.

**Two pieces of evidence, and they must agree.**

1. **The transport the request actually arrived on.** Not a header — the server side of the
   accepted connection: the peer address the kernel reports and the local address the
   connection terminated on. If that local address is present on exactly one object
   LocalPlane currently observes, the connection terminated on that object.
2. **The kernel's own route to the peer.** Asked of rtnetlink through the agent, read-only.
   If it leaves by the same object, the two independent answers agree.

Agreement confirms. **Disagreement resolves nothing** — it is reported as a conflict, and
the path stays unknown. Picking one of two disagreeing sources is how a control plane
arrives at a confident wrong answer.

**What is never allowed to answer it.** An interface's name. Which link carries the default
route. "It looks like the uplink". ``X-Forwarded-For``, ``X-Real-IP``, ``Forwarded`` or any
other header, because a header is written by whoever is asking. A peer or interface named in
a request body, because that is a claim and not evidence. A request from loopback, because
localhost is not an operator — that substitution is precisely what produced the 2026-07-22
lockout, where an apply removed the forwarding a real session depended on while a local call
vouched for a path it had never used.

**What it cannot answer yet, and says so.** Linux picks a route from more than a
destination: policy rules, ``fwmark``, the socket's UID, VRF membership, a bound source and
the network namespace all participate, and this build's lookup carries none of them. On a
host where any of those select a different table than an unmarked query would — a
Tailscale-marked flow, for instance — the route evidence can legitimately disagree with the
transport evidence. The answer then is ``unknown``, which is correct, and the limitation is
documented rather than papered over. Accuracy before coverage.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

from localplane.backend.domain.protection import ManagementPathVerdict
from localplane.backend.domain.states import Freshness, derive_freshness

#: What would settle the question, named so a caller sees what is missing rather than only
#: that something is. Both sources exist in this build; either one being unusable for *this*
#: request is what leaves the answer unresolved.
MANAGEMENT_PATH_EVIDENCE: tuple[str, ...] = (
    "session.peer",
    "route.observe",
)


class ManagementPathReason(StrEnum):
    """Why the management path is what it is. Every value is a distinct condition.

    Collapsing these into one "could not determine" would be the cheap thing to do and
    would make every one of them unactionable: "you are calling from localhost", "the
    address you reached me on is on two objects at once" and "the kernel routes your peer
    out of a different object than the one you connected to" have nothing in common except
    that the answer is unknown.
    """

    # --- proven
    CONFIRMED = "management_path_confirmed"

    # --- the transport this request arrived on cannot establish anything
    TRANSPORT_PEER_UNAVAILABLE = "transport_peer_unavailable"
    TRANSPORT_PEER_UNPARSEABLE = "transport_peer_unparseable"
    TRANSPORT_PEER_LOCAL = "transport_peer_local"
    TRANSPORT_PEER_LINK_LOCAL = "transport_peer_link_local"
    TRANSPORT_PEER_UNSPECIFIED = "transport_peer_unspecified"
    LOCAL_ENDPOINT_UNAVAILABLE = "local_endpoint_unavailable"
    LOCAL_ENDPOINT_UNPARSEABLE = "local_endpoint_unparseable"
    LOCAL_ENDPOINT_LOCAL = "local_endpoint_local"
    LOCAL_ENDPOINT_LINK_LOCAL = "local_endpoint_link_local"
    LOCAL_ENDPOINT_UNSPECIFIED = "local_endpoint_unspecified"
    ADDRESS_FAMILY_MISMATCH = "address_family_mismatch"

    # --- there is no evidence to reason from
    UNOBSERVED = "management_path_unobserved"
    REQUEST_CONTEXT_MISMATCH = "request_context_mismatch"
    EVIDENCE_STALE = "evidence_stale"

    # --- the local endpoint could not be tied to an object
    LOCAL_ENDPOINT_UNMAPPED = "local_endpoint_unmapped"
    LOCAL_ENDPOINT_AMBIGUOUS = "local_endpoint_ambiguous"
    INTERFACE_OBSERVATION_STALE = "interface_observation_stale"

    # --- the kernel's answer could not corroborate it
    ROUTE_LOOKUP_UNAVAILABLE = "route_lookup_unavailable"
    ROUTE_LOOKUP_FAILED = "route_lookup_failed"
    ROUTE_UNREACHABLE = "route_unreachable"
    ROUTE_EGRESS_UNSPECIFIED = "route_egress_unspecified"
    ROUTE_INTERFACE_UNMAPPED = "route_interface_unmapped"
    ROUTE_INTERFACE_AMBIGUOUS = "route_interface_ambiguous"

    # --- the two sources disagree, and neither wins
    ROUTE_CONFLICTS_WITH_LOCAL_ENDPOINT = "route_conflicts_with_local_endpoint"


class RouteStatus(StrEnum):
    """The four outcomes of a route lookup, as the agent reports them."""

    RESOLVED = "resolved"
    UNREACHABLE = "unreachable"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


#: How a route lookup that did not resolve is reported to an operator.
_ROUTE_FAILURE_REASONS: dict[str, ManagementPathReason] = {
    RouteStatus.UNREACHABLE: ManagementPathReason.ROUTE_UNREACHABLE,
    RouteStatus.FAILED: ManagementPathReason.ROUTE_LOOKUP_FAILED,
    RouteStatus.UNAVAILABLE: ManagementPathReason.ROUTE_LOOKUP_UNAVAILABLE,
}


# ------------------------------------------------------------------- transport evidence


@dataclass(frozen=True)
class TransportEvidence:
    """What the *server side of this connection* says about how the request arrived.

    Deliberately not called an actor, a session or an identity. Authentication proves
    credential possession but identifies nobody on the other end of this socket, and naming
    the peer anything that implies otherwise would invite a later build to treat it as though
    somebody had. It is a transport fact, and its whole value is that a caller cannot choose it.
    """

    peer_address: str | None
    peer_family: str | None
    local_address: str | None
    local_family: str | None
    unusable_reason: str | None

    #: What was on the wire before normalisation, so an operator can see what was read.
    raw_peer: str | None = None
    raw_local: str | None = None

    @property
    def usable(self) -> bool:
        return self.unusable_reason is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "peer_address": self.peer_address,
            "peer_family": self.peer_family,
            "local_endpoint_address": self.local_address,
            "local_endpoint_family": self.local_family,
            "usable": self.usable,
            "reason": self.unusable_reason,
        }


def read_transport(peer: str | None, local: str | None) -> TransportEvidence:
    """Classify one connection's endpoints. Pure; nothing about HTTP reaches here.

    IPv4-mapped IPv6 addresses are normalised to IPv4. A server bound to ``[::]`` reports
    every IPv4 peer as ``::ffff:a.b.c.d``, and comparing that text against the addresses the
    kernel lists on an interface would fail for a completely ordinary deployment.

    Everything that is not a routable unicast address on both ends is refused, with the
    reason saying which end and why:

    * **loopback**, on either side. A localhost request has no management path to prove and
      must never inherit one that somebody else's request proved.
    * **link-local**, because the address is only meaningful together with a scope this
      build does not carry, and a route lookup for one returns an interface the kernel
      picked rather than the one the connection used.
    * **unspecified**, which is a wildcard rather than a place.
    * **mismatched families**, because a v4 peer and a v6 local endpoint cannot both
      describe the connection that is being reasoned about.
    """
    peer_address, peer_reason = _classify(
        peer,
        unavailable=ManagementPathReason.TRANSPORT_PEER_UNAVAILABLE,
        unparseable=ManagementPathReason.TRANSPORT_PEER_UNPARSEABLE,
        loopback=ManagementPathReason.TRANSPORT_PEER_LOCAL,
        link_local=ManagementPathReason.TRANSPORT_PEER_LINK_LOCAL,
        unspecified=ManagementPathReason.TRANSPORT_PEER_UNSPECIFIED,
    )
    local_address, local_reason = _classify(
        local,
        unavailable=ManagementPathReason.LOCAL_ENDPOINT_UNAVAILABLE,
        unparseable=ManagementPathReason.LOCAL_ENDPOINT_UNPARSEABLE,
        loopback=ManagementPathReason.LOCAL_ENDPOINT_LOCAL,
        link_local=ManagementPathReason.LOCAL_ENDPOINT_LINK_LOCAL,
        unspecified=ManagementPathReason.LOCAL_ENDPOINT_UNSPECIFIED,
    )

    reason = peer_reason or local_reason
    if reason is None:
        assert peer_address is not None and local_address is not None
        if peer_address.version != local_address.version:
            reason = ManagementPathReason.ADDRESS_FAMILY_MISMATCH

    return TransportEvidence(
        peer_address=None if peer_address is None else str(peer_address),
        peer_family=_family_of(peer_address),
        local_address=None if local_address is None else str(local_address),
        local_family=_family_of(local_address),
        unusable_reason=None if reason is None else str(reason),
        raw_peer=peer,
        raw_local=local,
    )


def _classify(
    value: str | None,
    *,
    unavailable: ManagementPathReason,
    unparseable: ManagementPathReason,
    loopback: ManagementPathReason,
    link_local: ManagementPathReason,
    unspecified: ManagementPathReason,
) -> tuple[Any, ManagementPathReason | None]:
    if value is None or value == "":
        return None, unavailable
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None, unparseable
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    if address.is_loopback:
        return address, loopback
    if address.is_unspecified:
        return address, unspecified
    if address.is_link_local:
        return address, link_local
    return address, None


def _family_of(address: Any) -> str | None:
    if address is None:
        return None
    return "inet" if address.version == 4 else "inet6"


# ------------------------------------------------------------------- persisted evidence


@dataclass(frozen=True)
class RouteEvidence:
    """The kernel's answer about the route to the peer, as it was recorded.

    Facts only, and every one of them optional because the kernel supplies what applies. No
    LocalPlane identity appears here: ``oif_index`` is the kernel's interface index, and
    turning it into an object is the correlation :func:`derive_management_path` does.
    """

    status: str
    reason: str | None = None
    family: str | None = None
    destination: str | None = None
    destination_prefix_length: int | None = None
    preferred_source: str | None = None
    gateway: str | None = None
    oif_index: int | None = None
    table: int | None = None
    route_type: str | None = None
    scope: str | None = None
    protocol: str | None = None
    priority: int | None = None
    error: dict[str, Any] | None = None


@dataclass(frozen=True)
class ManagementPathObservation:
    """One recorded observation of how a connection reached LocalPlane.

    Raw evidence, not a conclusion. It holds the two addresses and what the kernel said
    about the route between them, and it does not hold "the management path is eth0" —
    because that is a judgement, it depends on observations of objects that move
    independently of this record, and a stored conclusion would be wrong the moment either
    of them did.
    """

    observation_id: str
    host_id: str
    observed_at: str
    transport_peer_address: str
    transport_peer_family: str
    local_endpoint_address: str
    local_endpoint_family: str
    route: RouteEvidence
    capability: str
    provider: str
    provider_version: str
    method: str
    agent_instance_id: str | None = None

    @property
    def observed_at_dt(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.observed_at)
        except ValueError:
            return None

    def matches(self, transport: TransportEvidence, host_id: str) -> bool:
        """Whether this evidence was taken from the connection now being reasoned about.

        Every part has to agree. Evidence proving that a remote operator's session
        terminates on one object says nothing whatsoever about a second operator's session,
        about a call from automation, or about the same operator arriving on a different
        address — and letting one stand for another is the substitution this whole module
        exists to prevent.

        The source *port* is deliberately not compared. It changes on every connection a
        client makes, so matching on it would mean no evidence was ever reusable and every
        read would need its own write — which is precisely the impure ``GET`` the reads in
        this product are not allowed to be.
        """
        return (
            self.host_id == host_id
            and self.transport_peer_address == transport.peer_address
            and self.local_endpoint_address == transport.local_address
            and self.transport_peer_family == transport.peer_family
            and self.local_endpoint_family == transport.local_family
        )


@dataclass(frozen=True)
class ObservedAddresses:
    """What LocalPlane currently observes about which object carries what.

    Both maps are built from the newest observation of each object, and ``stale`` names the
    objects whose newest observation has aged past the freshness horizon — because an
    address recorded ten minutes ago is not evidence that it is on that object now.
    """

    by_address: Mapping[str, tuple[str, ...]]
    by_index: Mapping[int, tuple[str, ...]]
    stale: frozenset[str]


def derive_management_path(
    *,
    transport: TransportEvidence,
    host_id: str,
    observation: ManagementPathObservation | None,
    observed: ObservedAddresses,
    now: datetime,
    ttl_s: float,
) -> ManagementPathVerdict:
    """Decide which object carries the management path for *this* request, or why not.

    Pure, and derived at read time from records that already exist. It contacts nothing and
    writes nothing, which is what lets a ``GET`` answer the question honestly without
    becoming a write.

    The checks are ordered by what stops mattering first, the same discipline adoption and
    planning follow: a caller told that the route to their peer is unreachable when the real
    problem is that they are calling from localhost has been sent to fix the wrong thing.
    """
    if not transport.usable:
        assert transport.unusable_reason is not None
        return _unresolved(transport.unusable_reason)

    if observation is None:
        return _unresolved(ManagementPathReason.UNOBSERVED)
    if not observation.matches(transport, host_id):
        return _unresolved(
            ManagementPathReason.REQUEST_CONTEXT_MISMATCH,
            evidence_id=None,
        )

    freshness, _age = derive_freshness(observation.observed_at_dt, ttl_s, now=now)
    if freshness is not Freshness.CURRENT:
        return _unresolved(
            ManagementPathReason.EVIDENCE_STALE,
            evidence_id=observation.observation_id,
            observed_at=observation.observed_at,
        )

    def unresolved(reason: ManagementPathReason) -> ManagementPathVerdict:
        return _unresolved(
            reason,
            evidence_id=observation.observation_id,
            observed_at=observation.observed_at,
        )

    # 1 — the local endpoint. Where the connection actually terminated.
    carrying = observed.by_address.get(observation.local_endpoint_address, ())
    if not carrying:
        return unresolved(ManagementPathReason.LOCAL_ENDPOINT_UNMAPPED)
    if len(carrying) > 1:
        return unresolved(ManagementPathReason.LOCAL_ENDPOINT_AMBIGUOUS)
    endpoint_object = carrying[0]

    # 2 — the kernel's route to the peer. Corroboration, never identity on its own.
    route = observation.route
    if route.status != RouteStatus.RESOLVED:
        return unresolved(
            _ROUTE_FAILURE_REASONS.get(route.status, ManagementPathReason.ROUTE_LOOKUP_FAILED)
        )
    if route.oif_index is None:
        return unresolved(ManagementPathReason.ROUTE_EGRESS_UNSPECIFIED)
    egress = observed.by_index.get(route.oif_index, ())
    if not egress:
        return unresolved(ManagementPathReason.ROUTE_INTERFACE_UNMAPPED)
    if len(egress) > 1:
        return unresolved(ManagementPathReason.ROUTE_INTERFACE_AMBIGUOUS)

    # 3 — they must agree. Neither wins on its own.
    if egress[0] != endpoint_object:
        return unresolved(ManagementPathReason.ROUTE_CONFLICTS_WITH_LOCAL_ENDPOINT)

    # 4 — and the observation both correlations rest on has to be current.
    if endpoint_object in observed.stale:
        return unresolved(ManagementPathReason.INTERFACE_OBSERVATION_STALE)

    return ManagementPathVerdict(
        resource_id=endpoint_object,
        reason=str(ManagementPathReason.CONFIRMED),
        evidence_id=observation.observation_id,
        observed_at=observation.observed_at,
        missing_evidence=(),
    )


def _unresolved(
    reason: str, *, evidence_id: str | None = None, observed_at: str | None = None
) -> ManagementPathVerdict:
    return ManagementPathVerdict(
        resource_id=None,
        reason=str(reason),
        evidence_id=evidence_id,
        observed_at=observed_at,
        missing_evidence=MANAGEMENT_PATH_EVIDENCE,
    )
