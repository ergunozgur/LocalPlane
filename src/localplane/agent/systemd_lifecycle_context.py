"""Compose read-only systemd lifecycle safety evidence inside the host agent.

This is not an execution path.  It combines two narrow providers — exact Linux socket
diagnostics and official systemd reads — and returns facts.  The backend remains the place
that judges applicability and protection.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final

from localplane.agent.docker_runtime_owner import RuntimeOwnerCorrelation
from localplane.agent.providers.linux_socket_diag import (
    LinuxSocketDiag,
    TcpSocketTuple,
    resolve_cgroup_path,
)
from localplane.protocol.docker_runtime_owner import RUNTIME_OWNER_UMBRELLA_GAP


MANAGEMENT_PROVIDERS: Final = frozenset({"networkmanager", "docker", "tailscale"})


@dataclass(frozen=True)
class LifecycleContextObservation:
    status: str
    observed_at: str
    target_unit: str
    action: str
    target_facts: dict[str, Any] = field(default_factory=dict)
    effect_units: tuple[str, ...] = ()
    effect_edges: tuple[dict[str, str], ...] = ()
    effect_complete: bool = False
    active_activation_sources: tuple[str, ...] = ()
    active_upholding_sources: tuple[str, ...] = ()
    management_units: tuple[str, ...] = ()
    management_complete: bool = False
    connection_unit: str | None = None
    """The systemd unit containing the accepted operator connection — the backend itself.

    Already inside ``management_units``, and typed separately because the set does not say
    *which* of its members is the one hosting LocalPlane. "This operation reaches a unit on
    the management path" and "this operation reaches the unit LocalPlane is running in" are
    different sentences, and only the second one is about LocalPlane disappearing.
    """

    connection_unit_type: str | None = None
    """``service``, ``scope``, ``slice`` — never inferred from the unit's name."""

    agent_unit: str | None = None
    agent_complete: bool = False
    agent_unit_type: str | None = None
    """The type of the unit containing the agent, for the same reason as above.

    A ``.service`` agent is contained by systemd directly and the effect graph speaks about
    it. Any other kind of containment is a runtime's, which systemd need not publish an edge
    for — so the type is carried rather than assumed from the resolution succeeding.
    """

    gaps: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)
    runtime_owner: RuntimeOwnerCorrelation | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "observed_at": self.observed_at,
            "target_unit": self.target_unit,
            "action": self.action,
            "target_facts": self.target_facts,
            "effect_units": list(self.effect_units),
            "effect_edges": [dict(edge) for edge in self.effect_edges],
            "effect_complete": self.effect_complete,
            "active_activation_sources": list(self.active_activation_sources),
            "active_upholding_sources": list(self.active_upholding_sources),
            "management_units": list(self.management_units),
            "management_complete": self.management_complete,
            "connection_unit": self.connection_unit,
            "connection_unit_type": self.connection_unit_type,
            "agent_unit": self.agent_unit,
            "agent_complete": self.agent_complete,
            "agent_unit_type": self.agent_unit_type,
            "gaps": list(self.gaps),
            "evidence": self.evidence,
            "runtime_owner": (
                self.runtime_owner.as_dict() if self.runtime_owner is not None else None
            ),
        }


class SystemdLifecycleContextReader:
    """One closed lifecycle-context read.  No method on this class mutates systemd."""

    def __init__(
        self,
        systemd: Any,
        socket_diag: LinuxSocketDiag | None = None,
        runtime_owner_correlator: Any | None = None,
    ) -> None:
        self._systemd = systemd
        self._socket_diag = socket_diag or LinuxSocketDiag()
        self._runtime_owner = runtime_owner_correlator

    def observe(
        self,
        *,
        target_unit: str,
        action: str,
        connection: dict[str, Any],
        management_providers: tuple[str, ...],
        provider_evidence_complete: bool,
    ) -> LifecycleContextObservation:
        observed_at = _now()
        gaps: set[str] = set()
        evidence: dict[str, Any] = {
            "transport_source": "accepted_asgi_tcp_socket",
            "caller_supplied_transport_fields": False,
            "forwarded_headers_authoritative": False,
        }
        runtime_owner: RuntimeOwnerCorrelation | None = None

        graph = self._systemd.observe_effect_graph(target_unit, action)
        target_facts = graph.target.facts if graph.target is not None else {}
        gaps.update(graph.gaps)
        if not graph.complete:
            gaps.add("systemd.effect_graph")
        evidence["effect_graph"] = {
            "status": graph.status,
            "reason": graph.reason,
            "detail": graph.detail,
            "provider_version": graph.provider_version,
            "target_observation": graph.target.as_dict() if graph.target else None,
        }

        management_units: set[str] = set()
        management_complete = True
        connection_unit: str | None = None
        connection_unit_type: str | None = None
        local_inode = self._socket_diag.network_namespace_inode()
        backend_inode = connection.get("backend_netns_inode")
        evidence["network_namespace"] = {
            "backend_inode": backend_inode,
            "agent_inode": local_inode,
        }
        if not isinstance(backend_inode, int) or local_inode is None:
            management_complete = False
            gaps.add("management_path.network_namespace")
        elif backend_inode != local_inode:
            management_complete = False
            gaps.add("management_path.network_namespace_mismatch")
        else:
            unit, cgroup_path, unit_type, socket_evidence, socket_gaps = (
                self._socket_context(connection)
            )
            evidence.update(socket_evidence)
            connection_unit, connection_unit_type = unit, unit_type
            if unit is not None:
                management_units.add(unit)
            if socket_gaps:
                management_complete = False
                gaps.update(socket_gaps)
            elif unit is not None and unit_type != "service":
                if self._runtime_owner is None or cgroup_path is None:
                    management_complete = False
                    gaps.add(RUNTIME_OWNER_UMBRELLA_GAP)
                    evidence["accepted_socket"]["runtime_dependency"] = {
                        "status": "unknown",
                        "reason": "accepted_connection_unit_runtime_owner_unproven",
                        "containing_unit": unit,
                        "containing_unit_type": unit_type,
                        "authoritative_owner_unit": None,
                    }
                else:
                    owner = self._runtime_owner.correlate(cgroup_path)
                    runtime_owner = owner.correlation
                    evidence["runtime_owner"] = owner.evidence
                    if runtime_owner.resolved and runtime_owner.owner_unit_id:
                        management_units.add(runtime_owner.owner_unit_id)
                        evidence["accepted_socket"]["runtime_dependency"] = {
                            "status": "resolved",
                            "reason": "docker_direct_unix_runtime_owner_verified",
                            "containing_unit": unit,
                            "containing_unit_type": unit_type,
                            "authoritative_owner_unit": runtime_owner.owner_unit_id,
                            "contract": runtime_owner.contract_version,
                        }
                    else:
                        management_complete = False
                        gaps.add(RUNTIME_OWNER_UMBRELLA_GAP)
                        gaps.update(runtime_owner.gaps)
                        evidence["accepted_socket"]["runtime_dependency"] = {
                            "status": "unknown",
                            "reason": "accepted_connection_unit_runtime_owner_unproven",
                            "containing_unit": unit,
                            "containing_unit_type": unit_type,
                            "authoritative_owner_unit": None,
                            "contract": runtime_owner.contract_version,
                        }

        if not provider_evidence_complete:
            management_complete = False
            gaps.add("management_path.provider_evidence_incomplete")
        for provider in management_providers:
            if provider not in MANAGEMENT_PROVIDERS:
                management_complete = False
                gaps.add("management_path.provider_vocabulary_unknown")
                continue
            resolution = self._systemd.resolve_provider_unit(provider)
            evidence.setdefault("provider_owners", {})[provider] = resolution.as_dict()
            if resolution.status == "resolved" and resolution.canonical_id:
                management_units.add(resolution.canonical_id)
            else:
                management_complete = False
                gaps.add(f"management_path.provider_owner:{provider}")

        self_unit = self._systemd.resolve_current_process_unit()
        evidence["agent_containment"] = self_unit.as_dict()
        agent_complete = self_unit.status == "resolved" and bool(self_unit.canonical_id)
        agent_unit_type = (
            self_unit.unit.facts.get("unit_type") if self_unit.unit is not None else None
        )
        if not agent_complete:
            gaps.add("localplane_agent.containing_unit")

        complete = graph.complete and management_complete and agent_complete and not gaps
        return LifecycleContextObservation(
            status="complete" if complete else "partial",
            observed_at=observed_at,
            target_unit=target_unit,
            action=action,
            target_facts=dict(target_facts),
            effect_units=tuple(graph.units),
            effect_edges=tuple(graph.edges),
            effect_complete=graph.complete,
            active_activation_sources=tuple(graph.active_activation_sources),
            active_upholding_sources=tuple(graph.active_upholding_sources),
            management_units=tuple(sorted(management_units)),
            management_complete=management_complete,
            connection_unit=connection_unit,
            connection_unit_type=connection_unit_type,
            agent_unit=self_unit.canonical_id,
            agent_complete=agent_complete,
            agent_unit_type=agent_unit_type,
            gaps=tuple(sorted(gaps)),
            evidence=evidence,
            runtime_owner=runtime_owner,
        )

    def _socket_context(
        self, connection: dict[str, Any]
    ) -> tuple[
        str | None,
        str | None,
        str | None,
        dict[str, Any],
        tuple[str, ...],
    ]:
        try:
            query = TcpSocketTuple(
                family=str(connection["family"]),
                peer_ip=str(connection["peer_ip"]),
                peer_port=int(connection["peer_port"]),
                local_ip=str(connection["local_ip"]),
                local_port=int(connection["local_port"]),
            )
        except (KeyError, TypeError, ValueError):
            return None, None, None, {}, ("management_path.accepted_tcp_tuple_invalid",)
        peer = ipaddress.ip_address(query.peer_ip)
        local = ipaddress.ip_address(query.local_ip)
        if peer.is_loopback or local.is_loopback:
            return (
                None, None, None,
                {"accepted_socket": {"status": "unsupported", "reason": "loopback_transport"}},
                ("management_path.loopback_transport",),
            )
        diag = self._socket_diag.lookup(query)
        details: dict[str, Any] = {
            "accepted_socket": {
                "tuple": {
                    "family": query.family,
                    "peer_ip": query.peer_ip,
                    "peer_port": query.peer_port,
                    "local_ip": query.local_ip,
                    "local_port": query.local_port,
                },
                "diag": diag.as_dict(),
            }
        }
        if diag.status != "matched" or diag.match is None:
            return None, None, None, details, (f"management_path.socket_diag:{diag.reason}",)
        cgroup = resolve_cgroup_path(diag.match.cgroup_id)
        details["accepted_socket"]["cgroup"] = cgroup.as_dict()
        if cgroup.status != "resolved" or cgroup.path is None:
            return None, None, None, details, (f"management_path.cgroup:{cgroup.reason}",)
        resolution = self._systemd.resolve_control_group_unit(cgroup.path)
        details["accepted_socket"]["systemd_unit"] = resolution.as_dict()
        if resolution.status != "resolved" or not resolution.canonical_id:
            return (
                None, cgroup.path, None, details,
                (f"management_path.systemd_unit:{resolution.reason}",),
            )

        # A containing systemd service is directly represented in the same typed effect
        # graph as the lifecycle target.  A scope/slice/other container context is not:
        # systemd need not publish an effect edge from a container runtime daemon to the
        # container scope it owns.  Until an authoritative cross-provider runtime-owner
        # correlation exists, disjoint systemd units are therefore insufficient proof.
        unit_type = (
            resolution.unit.facts.get("unit_type")
            if resolution.unit is not None
            else None
        )
        if unit_type == "service":
            details["accepted_socket"]["runtime_dependency"] = {
                "status": "not_applicable",
                "reason": "containing_unit_is_systemd_service",
                "containing_unit": resolution.canonical_id,
                "containing_unit_type": unit_type,
                "authoritative_owner_unit": None,
            }
            return resolution.canonical_id, cgroup.path, unit_type, details, ()
        details["accepted_socket"]["runtime_dependency"] = {
            "status": "pending",
            "reason": "non_service_runtime_owner_correlation_required",
            "containing_unit": resolution.canonical_id,
            "containing_unit_type": unit_type,
            "authoritative_owner_unit": None,
        }
        return resolution.canonical_id, cgroup.path, unit_type, details, ()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
