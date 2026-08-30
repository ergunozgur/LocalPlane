"""Observing and assessing the management path. Read-only with respect to the host.

Two operations, and the difference between them is the difference between a read and a
write in this product:

* :meth:`ManagementPathService.observe` captures evidence. It reads the transport of the
  request that asked for it, queries the kernel through the agent, and records what came
  back. It writes to LocalPlane's own store and to nothing else — the netlink message it
  causes is ``RTM_GETROUTE``, a question.
* :meth:`ManagementPathService.assess` answers the question from evidence that already
  exists. It contacts nothing, refreshes nothing and writes nothing, which is what lets a
  ``GET`` say whether a plan is still safe without a page refresh changing the record.

**Every address on this path comes from the server side of a real connection.** There is no
parameter, body field, query string or header through which a caller can name a peer, a
local endpoint or an interface and have it treated as evidence. That is not a validation
rule that could be relaxed; it is the absence of an argument.

**Evidence proves one connection and no other.** A stored observation answers for a request
only when the host, both addresses and both families match what *this* request arrived on.
A remote operator's proof does not carry over to a second operator, to automation calling
over loopback, or to the same operator arriving on a different address. The 2026-07-22
lockout is the shape of what happens when it does.
"""

from __future__ import annotations

import ipaddress
import logging
import uuid
from datetime import datetime, timezone

from localplane.backend.agent_client import AgentClient, AgentError
from localplane.backend.db.database import Database, to_json
from localplane.backend.db.repositories import (
    AgentRepository,
    ManagementPathRepository,
    ObjectRepository,
)
from localplane.backend.domain.identity import OBJECT_KIND_NETWORK_INTERFACE
from localplane.backend.domain.management_path import (
    ManagementPathObservation,
    ObservedAddresses,
    RouteStatus,
    TransportEvidence,
    derive_management_path,
)
from localplane.backend.domain.protection import ManagementPathVerdict
from localplane.backend.domain.states import Freshness, derive_freshness

LOG = logging.getLogger("localplane.backend.management_path")

#: The evidence a management-path observation is made of, named the way an ordinary
#: observation names its source.
CAPABILITY = "network.route.observe"


class ManagementPathService:
    """Captures and interprets evidence about how LocalPlane is being reached."""

    def __init__(
        self,
        database: Database,
        client: AgentClient,
        freshness_ttl_s: float,
    ) -> None:
        self._db = database
        self._client = client
        # Deliberately the *same* horizon ordinary observations use, not a second knob.
        # Management-path evidence proves where one connection terminated at one moment;
        # an address can be removed and a route replaced at any time, so it is not entitled
        # to a longer life than the readings every other judgement in the product rests on.
        # One setting also means the two cannot quietly drift apart into a state where a
        # plan is current and the proof of its safety is not.
        self._ttl_s = freshness_ttl_s
        self.observations = ManagementPathRepository(database)
        self.objects = ObjectRepository(database)
        self.agents = AgentRepository(database)

    @property
    def freshness_ttl_s(self) -> float:
        return self._ttl_s

    # ------------------------------------------------------------------------- observe

    def observe(self, host_id: str, transport: TransportEvidence) -> ManagementPathVerdict:
        """Capture evidence for the connection this request arrived on, then assess it.

        Nothing is written when the transport cannot establish anything. A request from
        loopback, from a reverse proxy on this host, or over a transport whose endpoints
        cannot be read leaves no row behind — a record that looks like evidence, sorts as
        the newest evidence and proves nothing is worse than no record at all.

        A usable transport whose route lookup *failed* is recorded, because "LocalPlane
        asked and this is what happened" is evidence of a different kind. The row says why
        and carries no route facts; the schema enforces both.

        The agent being unreachable is not an error here. It is one of the two sources
        being unavailable, which leaves the answer unresolved with a reason that says so —
        the same shape every other unknown on this path takes.
        """
        if not transport.usable:
            return self._assess(host_id, transport, observation=None)

        assert transport.peer_address is not None
        assert transport.local_address is not None

        route, agent_instance_id = self._lookup(transport.peer_address)
        observation_id = f"mpo_{uuid.uuid4().hex}"
        observed_at = _now()

        with self._db.transaction():
            self.observations.insert(
                {
                    "observation_id": observation_id,
                    "host_id": host_id,
                    "observed_at": observed_at,
                    "agent_instance_id": agent_instance_id,
                    "transport_peer_address": transport.peer_address,
                    "transport_peer_family": transport.peer_family,
                    "local_endpoint_address": transport.local_address,
                    "local_endpoint_family": transport.local_family,
                    "capability": CAPABILITY,
                    "provider": route["provider"],
                    "provider_version": route["provider_version"],
                    "method": route["method"],
                    "route_status": route["status"],
                    "route_reason": route["reason"],
                    "route_family": route["family"],
                    "route_destination": route["destination"],
                    "route_destination_prefix_length": route["destination_prefix_length"],
                    "route_preferred_source": route["preferred_source"],
                    "route_gateway": route["gateway"],
                    "route_oif_index": route["oif_index"],
                    "route_table": route["table"],
                    "route_type": route["route_type"],
                    "route_scope": route["scope"],
                    "route_protocol": route["protocol"],
                    "route_priority": route["priority"],
                    "route_error": None if route["error"] is None else to_json(route["error"]),
                }
            )

        verdict = self.assess(host_id, transport)
        LOG.info(
            "management path observed",
            extra={
                "observation_id": observation_id,
                "host_id": host_id,
                "transport_peer": transport.peer_address,
                "local_endpoint": transport.local_address,
                "route_status": route["status"],
                "route_oif_index": route["oif_index"],
                "management_path_object_id": verdict.resource_id,
                "management_path_reason": verdict.reason,
                "host_effect": "none",
            },
        )
        return verdict

    def _lookup(self, destination: str) -> tuple[dict, str | None]:
        """Ask the agent for the kernel's route to ``destination``.

        ``destination`` is an :class:`ipaddress`-validated address the backend read from a
        live socket. It is re-canonicalised here before it leaves the process, so what
        crosses the agent protocol is the same text that will be compared against an
        object's addresses, and nothing else could be.

        Every way this can fail becomes a route status rather than an exception, because
        every one of them is a fact about the evidence: the agent not running, the agent
        declaring no route capability, the kernel refusing, a route that does not exist.
        None of them is a reason to refuse a caller who only asked where they are connected
        from.
        """
        address = str(ipaddress.ip_address(destination))
        try:
            answer = self._client.observe_route(address)
        except AgentError as exc:
            return (
                _unresolved_route(
                    status=RouteStatus.UNAVAILABLE,
                    reason=exc.code,
                    error={"code": exc.code, "message": exc.message, "detail": exc.detail},
                ),
                None,
            )

        route = answer.get("route")
        if not isinstance(route, dict):
            return (
                _unresolved_route(
                    status=RouteStatus.FAILED,
                    reason="malformed_route_response",
                    error={"received": sorted(answer)},
                ),
                answer.get("agent_instance_id"),
            )
        facts = route.get("route") or {}
        return (
            {
                "provider": route.get("provider", "unknown"),
                "provider_version": route.get("provider_version", "unknown"),
                "method": route.get("method", "unknown"),
                "status": str(route.get("status", RouteStatus.FAILED)),
                "reason": route.get("reason"),
                "family": facts.get("family"),
                "destination": facts.get("destination"),
                "destination_prefix_length": facts.get("destination_prefix_length"),
                "preferred_source": facts.get("preferred_source"),
                "gateway": facts.get("gateway"),
                "oif_index": facts.get("oif_index"),
                "table": facts.get("table"),
                "route_type": facts.get("route_type"),
                "scope": facts.get("scope"),
                "protocol": facts.get("protocol"),
                "priority": facts.get("priority"),
                "error": route.get("error"),
            },
            answer.get("agent_instance_id"),
        )

    # -------------------------------------------------------------------------- assess

    def assess(self, host_id: str, transport: TransportEvidence) -> ManagementPathVerdict:
        """Which object carries the management path for this request. Pure.

        Reads only records that already exist, and the answer depends on the connection
        asking: that is deliberate rather than incidental. A judgement that a particular
        object is safe to change rests on evidence about the session it is being changed
        from, and a call arriving over a transport that proves nothing gets ``unknown`` —
        including when a different, remote session proved the path a moment earlier.
        """
        observation = None
        if transport.usable:
            assert transport.peer_address is not None
            assert transport.local_address is not None
            observation = self.observations.newest_matching(
                host_id=host_id,
                peer_address=transport.peer_address,
                local_endpoint_address=transport.local_address,
            )
        return self._assess(host_id, transport, observation)

    def _assess(
        self,
        host_id: str,
        transport: TransportEvidence,
        observation: ManagementPathObservation | None,
    ) -> ManagementPathVerdict:
        return derive_management_path(
            transport=transport,
            host_id=host_id,
            observation=observation,
            observed=self.observed_addresses(host_id),
            now=datetime.now(timezone.utc),
            ttl_s=self._ttl_s,
        )

    def evidence(self, verdict: ManagementPathVerdict) -> ManagementPathObservation | None:
        """The observation a verdict was reached from, when there is one to show."""
        if verdict.evidence_id is None:
            return None
        return self.observations.get(verdict.evidence_id)

    def observed_addresses(self, host_id: str) -> ObservedAddresses:
        """Which object currently carries which address, and which kernel index.

        Built from the newest observation of each object, with the addresses canonicalised
        the same way the transport ones are so that ``::ffff:192.0.2.1`` and ``192.0.2.1``
        cannot be two different keys for one address.

        Objects whose newest observation has aged past the freshness horizon are listed as
        stale rather than dropped. An address recorded ten minutes ago is not evidence that
        it is on that object now — but knowing *which* object it was on is what turns the
        answer into "your evidence is too old" instead of "that address is on nothing here".

        A single address appearing on two objects makes both candidates and the derivation
        refuses to choose. That is a real state on a real host — a duplicated address, a
        bridge and its member — and picking the first would be a coin toss dressed as proof.
        """
        by_address: dict[str, list[str]] = {}
        by_index: dict[int, list[str]] = {}
        stale: set[str] = set()
        now = datetime.now(timezone.utc)

        for record in self.objects.list_by_kind(host_id, OBJECT_KIND_NETWORK_INTERFACE):
            observation = record.observation
            if observation is None:
                continue
            freshness, _age = derive_freshness(observation.observed_at_dt, self._ttl_s, now=now)
            if freshness is not Freshness.CURRENT:
                stale.add(record.object_id)

            index = observation.facts.get("ifindex")
            if isinstance(index, int):
                by_index.setdefault(index, []).append(record.object_id)

            for address in observation.facts.get("addresses") or []:
                if not isinstance(address, dict):
                    continue
                canonical = _canonical(address.get("address"))
                if canonical is None:
                    continue
                by_address.setdefault(canonical, []).append(record.object_id)

        return ObservedAddresses(
            by_address={a: tuple(sorted(set(o))) for a, o in by_address.items()},
            by_index={i: tuple(sorted(set(o))) for i, o in by_index.items()},
            stale=frozenset(stale),
        )


def _canonical(address: object) -> str | None:
    if not isinstance(address, str):
        return None
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return None
    mapped = getattr(parsed, "ipv4_mapped", None)
    return str(mapped if mapped is not None else parsed)


def _unresolved_route(*, status: str, reason: str, error: dict) -> dict:
    """A route lookup that produced no route, shaped like one that did.

    Every fact column is ``None``, which the schema requires of a lookup that did not
    resolve: a failure carrying data invites the data to be read as an answer.
    """
    return {
        "provider": "linux.route",
        "provider_version": "1",
        "method": "netlink_rtm_getroute",
        "status": str(status),
        "reason": reason,
        "family": None,
        "destination": None,
        "destination_prefix_length": None,
        "preferred_source": None,
        "gateway": None,
        "oif_index": None,
        "table": None,
        "route_type": None,
        "scope": None,
        "protocol": None,
        "priority": None,
        "error": error,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
