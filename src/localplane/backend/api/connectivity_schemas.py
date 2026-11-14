"""Closed connectivity evidence and deliberate-action API vocabulary."""
from __future__ import annotations

import ipaddress
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictInt, StrictStr, field_validator

from localplane.backend.api.schemas import Model
from localplane.protocol.diagnostic_policy import _name


class ConnectivityEvidence(Model):
    source: str | None = None
    provider_version: str | None = None
    agent_instance_id: str | None = None
    host_id: str | None = None
    vantage: str | None = None
    observed_at: datetime | None = None
    freshness: Literal["current", "stale", "unknown"] = "unknown"
    completeness: Literal["complete", "partial", "unknown"] = "unknown"
    status: Literal["observed", "not_observed", "unavailable", "unsupported", "changed"] = "not_observed"
    gaps: list[str] = Field(default_factory=list)


class ConnectivityAddress(Model):
    address: str
    prefix: int | None = None


class ConnectivityIPSettings(Model):
    family: Literal["ipv4", "ipv6"]
    method: str | None = None
    ignore_auto_dns: bool | None = None
    dns_priority: int | None = None
    route_metric: int | None = None
    gateway: str | None = None
    dns_servers: list[str] | None = None


class ConnectivityProfile(Model):
    provider: str
    uuid: str | None = None
    name: str | None = None
    version: str | None = None
    device_identity: str | None = None
    device_ifindex: int | None = None
    active_connection_identity: str | None = None
    state: Literal["saved", "unsaved", "generated", "external", "unknown"] = "unknown"
    unsaved: bool | None = None
    generated: bool | None = None
    external: bool | None = None
    addresses: list[ConnectivityAddress] | None = None
    routes: list[ConnectivityRoute] | None = None
    dns_servers: list[str] | None = None
    search_domains: list[str] | None = None
    routing_domains: list[ConnectivityResolverDomain] | None = None
    ip_settings: list[ConnectivityIPSettings] | None = None


class ConnectivityApplied(Model):
    provider: str
    version: str | None = None
    addresses: list[ConnectivityAddress] | None = None
    routes: list[ConnectivityRoute] | None = None
    dns_servers: list[str] | None = None
    search_domains: list[str] | None = None
    routing_domains: list[ConnectivityResolverDomain] | None = None
    ip_settings: list[ConnectivityIPSettings] | None = None


class ConnectivityNexthop(Model):
    gateway: str | None = None
    interface: str | None = None
    weight: int | None = None
    ifindex: int | None = None
    object_id: str | None = None


class ConnectivityRoute(Model):
    family: Literal["ipv4", "ipv6"]
    destination: str
    table: str | None = None
    route_type: str | None = None
    scope: str | None = None
    protocol: str | None = None
    metric: int | None = None
    nexthops: list[ConnectivityNexthop] | None = None


class ConnectivityRule(Model):
    family: Literal["ipv4", "ipv6"]
    priority: int | None = None
    source: str | None = None
    destination: str | None = None
    table: str | None = None
    action: str | None = None
    fwmark: str | None = None
    fwmask: str | None = None
    incoming_interface: str | None = None
    outgoing_interface: str | None = None
    uid_range: str | None = None
    suppress_prefix_length: int | None = None


class ConnectivityKernel(Model):
    addresses: list[ConnectivityAddress] | None = None


class ConnectivityResolverDomain(Model):
    name: str
    route_only: bool | None = None


class ConnectivityResolverLink(Model):
    interface: str | None = None
    servers: list[str] | None = None
    domains: list[str] | None = None
    routing_domains: list[ConnectivityResolverDomain] | None = None
    current_server: str | None = None
    dns_default_route: bool | None = None


class ConnectivityResolver(Model):
    global_servers: list[str] | None = None
    global_domains: list[str] | None = None
    global_routing_domains: list[ConnectivityResolverDomain] | None = None
    current_server: str | None = None
    current_server_ifindex: int | None = None
    links: list[ConnectivityResolverLink] | None = None


class ConnectivityClientResolver(Model):
    client_nameservers: list[str] | None = None
    client_mode: Literal["stub", "direct", "unknown"] = "unknown"


class ProfileSection(Model):
    evidence: ConnectivityEvidence
    value: ConnectivityProfile | None = None


class AppliedSection(Model):
    evidence: ConnectivityEvidence
    value: ConnectivityApplied | None = None


class KernelSection(Model):
    evidence: ConnectivityEvidence
    value: ConnectivityKernel | None = None


class ResolverSection(Model):
    evidence: ConnectivityEvidence
    value: ConnectivityResolver | None = None


class ClientResolverSection(Model):
    evidence: ConnectivityEvidence
    value: ConnectivityClientResolver | None = None


class RoutesSection(Model):
    evidence: ConnectivityEvidence
    value: list[ConnectivityRoute] | None = None


class RulesSection(Model):
    evidence: ConnectivityEvidence
    value: list[ConnectivityRule] | None = None


class TcpPolicyTarget(Model):
    address: str
    port: int


class ConnectivityPolicy(Model):
    status: Literal["valid", "missing", "invalid", "unsafe", "unreadable"]
    digest: str | None
    dns: list[str] | None
    tcp: list[TcpPolicyTarget] | None
    version: int | None = None
    control_plane_complete: bool | None = None
    address_count: int | None = None
    tcp_status: Literal["ready", "disabled", "missing_declaration", "incomplete_declaration", "policy_unavailable"] = "policy_unavailable"


class ConnectivitySnapshot(Model):
    object_id: str
    profile: ProfileSection
    applied: AppliedSection
    kernel: KernelSection
    routes: RoutesSection
    rules: RulesSection
    resolver: ResolverSection
    client_resolver: ClientResolverSection
    policy: ConnectivityPolicy


class ConnectivitySections(Model):
    profile: ProfileSection
    applied: AppliedSection
    kernel: KernelSection
    routes: RoutesSection
    rules: RulesSection
    resolver: ResolverSection
    client_resolver: ClientResolverSection


class AgentConnectivityIdentity(Model):
    ifindex: StrictInt
    name: StrictStr
    mac_address: StrictStr | None
    mac_is_permanent: StrictBool | None
    device_path: StrictStr | None
    kernel_device: StrictStr
    sysfs_device: StrictStr
    boot_id: StrictStr
    network_namespace: StrictStr


class AgentConnectivityObservation(Model):
    status: Literal["observed", "unavailable", "changed"]
    identity: AgentConnectivityIdentity | None
    started_at: datetime
    completed_at: datetime
    sections: ConnectivitySections | None
    gaps: list[str]


class AgentConnectivityResponse(Model):
    host_id: StrictStr
    agent_instance_id: StrictStr
    observation: AgentConnectivityObservation


class RouteRequest(Model):
    destination: StrictStr

    @field_validator("destination")
    @classmethod
    def numeric_destination(cls, value: str) -> str:
        if "%" in value:
            raise ValueError("scoped addresses are not accepted")
        return str(ipaddress.ip_address(value))


class AgentRouteFacts(Model):
    family: Literal["inet", "inet6"]
    destination: str | None
    destination_prefix_length: StrictInt | None
    preferred_source: str | None
    gateway: str | None
    oif_index: StrictInt | None
    table: StrictInt | None
    route_type: str | None
    scope: str | None
    protocol: str | None
    priority: StrictInt | None


class AgentRouteObservation(Model):
    provider: Literal["linux.route"]
    provider_version: str
    capability: Literal["network.route.observe"]
    source: Literal["rtnetlink"]
    method: Literal["netlink_rtm_getroute"]
    destination: str
    family: Literal["inet", "inet6"]
    status: Literal["resolved", "unreachable", "failed", "unavailable"]
    reason: str | None
    route: AgentRouteFacts | None
    error: dict | None
    observed_at: datetime


class AgentRouteAnswer(Model):
    agent_instance_id: str
    host_id: str
    route: AgentRouteObservation
    egress_identity: AgentConnectivityIdentity | None = None


class RouteResult(Model):
    object_id: str
    destination: str
    status: Literal["selected", "no_route", "prohibited", "unavailable", "error"]
    source: str | None = None
    gateway: str | None = None
    egress_interface: str | None = None
    egress_ifindex: int | None = None
    egress_object_id: str | None = None
    agent_instance_id: str | None = None
    host_id: str | None = None
    provider_version: str | None = None
    query_context: str = "Unmarked lookup in the agent network namespace; no source or interface binding."
    table: str | None = None
    route_type: str | None = None
    metric: int | None = None
    observed_at: datetime | None = None
    detail: str


class DnsRequest(Model):
    kind: Literal["dns"]
    name: StrictStr
    record_type: Literal["A", "AAAA"]
    policy_digest: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]

    @field_validator("name")
    @classmethod
    def dns_name(cls, value: str) -> str:
        return _name(value)


class TcpRequest(Model):
    kind: Literal["tcp"]
    address: StrictStr
    port: Annotated[StrictInt, Field(ge=1, le=65535)]
    policy_digest: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]

    @field_validator("address")
    @classmethod
    def numeric_address(cls, value: str) -> str:
        if "%" in value:
            raise ValueError("scoped addresses are not accepted")
        return str(ipaddress.ip_address(value))


DiagnosticRequest = Annotated[DnsRequest | TcpRequest, Field(discriminator="kind")]


class SocketEndpoint(Model):
    address: str
    port: int


class DnsResult(Model):
    kind: Literal["dns"] = "dns"
    name: str
    record_type: Literal["A", "AAAA"]
    outcome: Literal["resolved", "no_data", "not_found", "server_failure", "refused", "timeout", "unavailable", "denied", "policy_mismatch", "busy", "rate_limited", "error"]
    vantage: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    answers: list[str] | None = None
    truncated: bool | None = None
    detail: str


class TcpResult(Model):
    kind: Literal["tcp"] = "tcp"
    address: str
    port: int
    outcome: Literal["connected", "refused", "timeout", "no_route", "permission_denied", "unavailable", "denied", "policy_mismatch", "busy", "rate_limited", "error"]
    vantage: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    local: SocketEndpoint | None = None
    remote: SocketEndpoint | None = None
    detail: str


DiagnosticResult = Annotated[DnsResult | TcpResult, Field(discriminator="kind")]
