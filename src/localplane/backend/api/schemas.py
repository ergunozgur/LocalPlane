"""Response models.

Optionality is meaningful throughout. ``None`` means "not known", and it is never
substituted with a zero, an empty string or a cheerful default. The one place this
matters most is :attr:`NetworkInterface.addresses`: ``[]`` means the interface has no
addresses, ``null`` means the source that would have listed them could not be consulted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from localplane.backend.domain.states import (
    Freshness,
    HealthState,
    ManagementState,
    ReconciliationState,
)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ErrorBody(Model):
    code: str = Field(description="Stable machine-readable code. Branch on this, not the text.")
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(Model):
    error: ErrorBody


# ----------------------------------------------------------------------- authentication


class SessionStatus(Model):
    authenticated: Literal[True] = True
    mechanism: Literal["bearer", "session"]
    expires_at: datetime | None = Field(
        description="Absolute browser-session expiry; null for a master Bearer request."
    )


# ------------------------------------------------------------------------------- status


class DatabaseStatus(Model):
    path: str
    schema_versions: list[int] = Field(description="Migration versions applied to this store.")


class BackendStatus(Model):
    """Backend liveness. Deliberately independent of the agent.

    This answers "is the backend up", which is a different question from "can LocalPlane
    see the host". The second question is what ``/agent`` is for, and answering both here
    would make a reachable backend look like a working control plane.
    """

    status: str = "ok"
    version: str
    database: DatabaseStatus


# --------------------------------------------------------------------------------- host


class Host(Model):
    host_id: str
    identity_basis: str = Field(description="The evidence host_id was derived from.")
    identity_confidence: str
    hostname: str | None
    configured_hostname: str | None = Field(
        description="/etc/hostname. May differ from the running hostname; both are reported."
    )
    boot_id: str | None
    os_id: str | None
    os_version_id: str | None
    os_pretty_name: str | None
    kernel_name: str | None
    kernel_release: str | None
    architecture: str | None
    identity_gaps: list[str] = Field(
        description="Evidence sources that were not readable on this host."
    )
    first_seen_at: datetime
    last_seen_at: datetime
    freshness: Freshness
    age_seconds: float | None


# -------------------------------------------------------------------------------- agent


class Capability(Model):
    capability: str
    version: int
    status: str = Field(description="available | degraded | unavailable")
    mutating: bool
    summary: str
    reason: str | None
    detail: dict[str, Any]
    discovered_at: datetime


class AgentIdentity(Model):
    agent_instance_id: str = Field(
        description="Identifies an agent *process*. A restart produces a new one."
    )
    agent_version: str
    protocol_version: str
    transport: str
    process_isolated: bool
    privilege: str = Field(description="The privilege the agent process actually holds.")
    effective_uid: int | None
    pid: int | None
    started_at: datetime
    last_contact_at: datetime


class AgentStatus(Model):
    """Whether the agent is answering *now*, and what was last recorded about it."""

    reachable: bool
    source: str = Field(description="live — probed for this request; recorded — last known.")
    as_of: datetime
    error: ErrorBody | None = Field(
        default=None, description="Why the agent could not be reached, when it could not."
    )
    agent: AgentIdentity | None
    socket: str


class Capabilities(Model):
    reachable: bool
    source: str
    as_of: datetime
    agent_instance_id: str | None
    error: ErrorBody | None = None
    capabilities: list[Capability]


# --------------------------------------------------------------------------- network


class Identity(Model):
    basis: str
    value: str
    confidence: str


class Management(Model):
    state: ManagementState
    reason: str


class Health(Model):
    state: HealthState
    reason: str


class Observation(Model):
    observation_id: str
    sweep_id: str
    observed_at: datetime
    received_at: datetime
    freshness: Freshness
    age_seconds: float | None
    provider: str
    provider_version: str
    method: str
    capability: str
    fidelity: str = Field(description="complete | partial | degraded — how whole the evidence is.")
    gaps: list[str] = Field(
        description="Fields the source did not supply a value for. They are null, not zero."
    )


class Address(Model):
    family: str
    address: str
    prefix_length: int
    scope: str | None
    dynamic: bool | None
    valid_lifetime_s: int | None
    preferred_lifetime_s: int | None


class Link(Model):
    ifindex: int | None
    mtu: int | None
    mac_address: str | None
    mac_is_permanent: bool | None
    admin_up: bool | None
    operstate: str | None
    carrier: bool | None = Field(
        description="null while the link is administratively down — the kernel refuses the read."
    )
    speed_mbps: int | None = Field(description="null when the kernel does not know it.")
    duplex: str | None
    link_kind: str | None = Field(description="IFLA_INFO_KIND, e.g. bridge, veth, tun.")
    arphrd_type: int | None
    is_physical: bool | None
    device_path: str | None
    master: str | None
    carrier_changes: int | None


class Statistics(Model):
    rx_bytes: int | None
    tx_bytes: int | None
    rx_packets: int | None
    tx_packets: int | None
    rx_errors: int | None
    tx_errors: int | None
    rx_dropped: int | None
    tx_dropped: int | None


class IntentField(Model):
    """One field LocalPlane controls, and the value it intends for it."""

    field: str
    value_type: str
    value: bool | int


class IntentSummary(Model):
    """The active intent, reduced to what a list view needs."""

    intent_id: str
    version: int
    created_at: datetime
    controlled_fields: list[str]


# ---------------------------------------------------------------------------- ownership


class Owner(Model):
    """A system, and the specific thing within it that the claim is about."""

    provider: str = Field(description="docker | networkmanager | tailscale | kernel")
    instance: str | None = Field(
        description=(
            "The identifier inside that system — a Docker network id, a NetworkManager "
            "connection uuid, a device path. What makes the claim checkable against the "
            "provider itself."
        )
    )
    label: str | None = Field(description="That thing's human name, when it has one.")
    version: str | None = Field(description="The provider's version, when it reports one.")


class OwnershipClaimSummary(Model):
    """One relation and its owner, without the evidence payload.

    The evidence itself is at ``/network/interfaces/{id}/provenance``; what is here is the
    reason code, which names the *kind* of evidence the claim rests on without carrying it.
    """

    relation: str = Field(
        description=(
            "created_by — what brought the object into existence and would recreate it. "
            "configured_by — what is applying configuration to it now. They are different "
            "questions and are frequently answered by different systems."
        )
    )
    owner: Owner
    confidence: str = Field(
        description=(
            "confirmed — the provider names this object itself and the kernel agrees. "
            "corroborated — the provider's declaration matches, uniquely, a fact the "
            "kernel reports. There is no weaker value: LocalPlane makes no guesses."
        )
    )
    reason: str = Field(description="Stable machine-readable code. Branch on this.")
    evidence_sources: list[str] = Field(
        description="Which sources argued for this claim. Their content is on the detail resource."
    )


class AdoptionEligibility(Model):
    """Whether LocalPlane would take responsibility for this object, and why not if not.

    A separate axis from both management state and ownership. Management is where the
    object is; ownership is what is true about it; this is the policy that reads both.
    """

    eligible: bool
    reason: str = Field(
        description=(
            "object_observe_only | already_managed | externally_configured | "
            "externally_created | conflicting_ownership_claims | no_external_owner_proven"
        )
    )
    blocked_by: Owner | None = Field(
        default=None, description="The system whose ownership makes this ineligible."
    )
    evidence_gaps: list[str] = Field(
        default_factory=list,
        description=(
            "Sources that left the question open. Never makes an object eligible, and "
            "does not by itself make one ineligible — adoption records values that are "
            "already true and writes nothing. Reported so that an unexamined source is "
            "not mistaken for a clean one."
        ),
    )


class Ownership(Model):
    """What LocalPlane knows about who owns this object, and what follows from it.

    ``state`` is ``attributed`` when there is at least one evidence-backed claim and
    ``unknown`` otherwise — and ``unknown`` is a real answer with two distinct causes that
    ``reason`` separates: every source was consulted and none claimed the object
    (``no_provider_claim``), or something could not be consulted (``evidence_incomplete``).

    Nothing here is a management state. An object Docker owns is still ``observed``; what
    ownership changes is ``adoption``.
    """

    state: str = Field(description="attributed | unknown")
    reason: str = Field(
        description=(
            "externally_configured | externally_created | conflicting_claims | "
            "host_kernel_only | evidence_incomplete | no_provider_claim | no_observation"
        )
    )
    created_by: OwnershipClaimSummary | None = None
    configured_by: OwnershipClaimSummary | None = None
    evidence_gaps: list[str] = Field(
        default_factory=list, description="Sources that left the question open."
    )
    adoption: AdoptionEligibility


class OwnershipEvidenceItem(Model):
    """One machine-readable fact behind a claim, with the values that were compared."""

    source: str
    kind: str = Field(
        description="What kind of fact this is, e.g. declared_bridge_name, ipam_gateway_address."
    )
    detail: dict[str, Any] = Field(
        description="Exactly what was compared, so the claim can be checked rather than trusted."
    )
    observed_at: datetime | None


class OwnershipClaim(Model):
    relation: str
    owner: Owner
    confidence: str
    reason: str
    evidence: list[OwnershipEvidenceItem]


class ConsultedSource(Model):
    """What one source contributed, including the ones that had nothing to say.

    Present for every source LocalPlane knows how to consult. "NetworkManager was asked
    about this device and disclaims it" is a result, and without it a settled question
    would be indistinguishable from an unexamined one.
    """

    source: str
    provider: str
    status: str = Field(
        description=(
            "ok | absent | unavailable | error | never_consulted. `absent` means the "
            "provider is not installed on this host, which is a conclusion — it owns "
            "nothing here. `unavailable` means it is installed and would not answer, "
            "which is a gap."
        )
    )
    outcome: str = Field(
        description="What this source said about *this* object. Stable machine-readable code."
    )
    gap: bool = Field(
        description=(
            "True when this source left the question open. A source that answered "
            "definitively while producing no claim — 'no Docker network corresponds to "
            "this link' — is not a gap."
        )
    )
    observed_at: datetime | None
    freshness: Freshness | None = Field(
        default=None, description="How old this reading is, derived at read time."
    )
    age_seconds: float | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class Provenance(Model):
    """The whole ownership answer for one object, with every piece of evidence.

    A separate resource from the interface because the evidence is heavy and rarely wanted
    — but it is available, because a claim about an operator's host that cannot be checked
    is worth less than one that can.
    """

    object_id: str
    name: str
    management: Management = Field(
        description="Repeated here to make the independence of the two axes visible."
    )
    state: str
    reason: str
    claims: list[OwnershipClaim]
    sources: list[ConsultedSource]
    adoption: AdoptionEligibility
    observation: ObservationRef | None = Field(
        description="The interface observation the provider evidence was correlated against."
    )
    as_of: datetime = Field(description="When this assessment was computed. It is never stored.")


class ProviderReadingSummary(Model):
    """What one provider did during a sweep."""

    provider: str
    source: str
    status: str
    reason: str | None
    version: str | None


class NetworkInterface(Model):
    object_id: str
    kind: str
    name: str
    interface_kind: str = Field(
        description="What the kernel says this is: ethernet, wireless, bridge, tunnel, …"
    )
    identity: Identity
    management: Management
    reconciliation: ReconciliationState | None = Field(
        default=None,
        description=(
            "null unless management.state is 'managed'. An observed object has no retained "
            "intent, so it cannot drift — which is not the same as being in sync."
        ),
    )
    intent: IntentSummary | None = Field(
        default=None,
        description=(
            "The intent in force, when there is one. Its controlled_fields are the whole "
            "of what LocalPlane is answerable for on this object; everything else in this "
            "response is observation, not intention."
        ),
    )
    ownership: Ownership = Field(
        description=(
            "Who made this object and who configures it, on the evidence. A separate axis "
            "from management: an object another system owns is still `observed`, and what "
            "changes is that `ownership.adoption` refuses it."
        )
    )
    health: Health | None = Field(description="null when the object has never been observed.")
    observation: Observation | None
    observed_in_latest_sweep: bool | None = Field(
        description=(
            "Membership in the latest inventory sweep for this capability. A newer "
            "targeted observation can update this object's facts without replacing that "
            "estate-completeness boundary."
        )
    )
    link: Link | None
    addresses: list[Address] | None = Field(
        description="[] means no addresses. null means no address source was available."
    )
    statistics: Statistics | None
    first_seen_at: datetime
    last_seen_at: datetime


class ProviderIssue(Model):
    """Something a provider could not do during a sweep, named precisely enough to act on."""

    source: str = Field(description="Which source failed, e.g. rtnetlink.addr.")
    code: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class Sweep(Model):
    sweep_id: str
    capability: str
    scope: str = Field(description="inventory | targeted")
    provider: str
    provider_version: str
    status: str = Field(description="ok | partial | failed")
    started_at: datetime
    completed_at: datetime
    received_at: datetime
    object_count: int
    missing: list[str] = Field(
        description=(
            "Requested resource identities the provider authoritatively reported absent; "
            "read failures and unobserved inventory members are issues instead."
        )
    )
    issues: list[ProviderIssue]
    agent_instance_id: str | None


class NetworkInterfaceList(Model):
    host_id: str
    last_sweep: Sweep | None = Field(
        description=(
            "The sweep these objects were last re-read by. An empty list with a failed "
            "sweep means nobody could look; an empty list with no sweep means nobody has."
        )
    )
    count: int
    interfaces: list[NetworkInterface]


# ---------------------------------------------------------------------- docker containers


class ContainerImage(Model):
    """What this container was made from. Reported, never resolved or pulled."""

    reference: str | None = Field(description="The image reference the container was run from.")
    image_id: str | None = Field(description="The digest the daemon actually resolved it to.")


class ContainerPort(Model):
    """One port the container exposes, and the host binding for it if there is one."""

    container_port: int | None
    protocol: str
    host_ip: str | None = Field(description="null when the port is exposed but not published.")
    host_port: int | None
    published: bool


class ContainerMount(Model):
    """Where this container keeps state. Named volumes and bind mounts alike."""

    type: str | None = Field(description="volume | bind | tmpfs, as Docker reports it.")
    name: str | None = Field(description="The volume name, for a named volume.")
    source: str | None = Field(description="The host path or the volume's data directory.")
    destination: str | None
    driver: str | None
    mode: str | None
    read_write: bool | None
    propagation: str | None


class ContainerNetwork(Model):
    """One Docker network this container is attached to, as the daemon reports it.

    A relationship Docker states, not one LocalPlane inferred. A container in the host's
    network namespace has no entry here at all and says so through ``network_mode``.
    """

    name: str
    network_id: str | None
    ip_address: str | None
    ipv6_address: str | None
    gateway: str | None
    mac_address: str | None
    aliases: list[str]


class ContainerRestartPolicy(Model):
    name: str | None = Field(description="no | always | unless-stopped | on-failure")
    maximum_retry_count: int | None


class ContainerHealth(Model):
    """The container's own health check, when its image declares one.

    ``checked`` is false when the image declares no health check at all — which is not the
    same as a check that has not run, and the two must not be confused by a reader deciding
    whether ``status: null`` is a gap.
    """

    checked: bool
    status: str | None = Field(description="healthy | unhealthy | starting, or null.")
    failing_streak: int | None


class ContainerRuntime(Model):
    """What the container is doing now, in Docker's own words."""

    state: str | None = Field(
        description="created | running | paused | restarting | removing | exited | dead"
    )
    running: bool | None
    paused: bool | None
    restarting: bool | None
    exit_code: int | None
    error: str | None
    oom_killed: bool | None
    pid: int | None
    started_at: datetime | None = Field(
        description=(
            "When Docker records this container as having last started. The evidence a "
            "restart is proven from: a container running now that was running before has "
            "not been shown to have restarted."
        )
    )
    finished_at: datetime | None
    restart_count: int | None = Field(
        description="Restarts the daemon performed under the restart policy. Not operator ones."
    )


class DockerContainer(Model):
    """One container, as a LocalPlane resource.

    The same shape every observed object has — identity, management, ownership, health,
    observation — with the Docker-specific parts kept as Docker's rather than flattened into
    the network-interface model. Docker remains authoritative for all of it: nothing here is
    a copy LocalPlane maintains, it is the newest observation of what the daemon said.
    """

    object_id: str
    kind: str
    name: str
    container_id: str = Field(description="Docker's own id. LocalPlane's identity for it.")
    short_id: str
    identity: Identity
    management: Management
    ownership: Ownership = Field(
        description=(
            "Docker created this container and Docker configures it, on the daemon's own "
            "evidence. That refuses adoption and does *not* block the lifecycle operations, "
            "because those execute through Docker's own API rather than behind its back."
        )
    )
    health: Health | None = Field(description="null when the object has never been observed.")
    observation: Observation | None
    observed_in_latest_sweep: bool | None = Field(
        description="Membership in the latest Docker inventory sweep."
    )
    image: ContainerImage
    created_at: datetime | None
    runtime: ContainerRuntime
    container_health: ContainerHealth
    restart_policy: ContainerRestartPolicy
    network_mode: str | None
    networks: list[ContainerNetwork]
    ports: list[ContainerPort]
    mounts: list[ContainerMount]
    labels: dict[str, str] = Field(
        description=(
            "The labels worth operating on: compose project and service, OCI image "
            "metadata, io.localplane.*, maintainer and description. The rest are counted "
            "and dropped — a label set is unbounded and occasionally carries secrets."
        )
    )
    labels_dropped: int = Field(description="How many labels were not kept.")
    log_driver: str | None
    platform: str | None
    first_seen_at: datetime
    last_seen_at: datetime


class DockerContainerList(Model):
    host_id: str
    last_sweep: Sweep | None = Field(
        description=(
            "The sweep these containers were last re-read by. An empty list with a failed "
            "sweep means nobody could look; an empty list with no sweep means nobody has."
        )
    )
    count: int
    containers: list[DockerContainer]


class ContainerLogLine(Model):
    timestamp: datetime | None = Field(
        description="null when the line carried no parsable instant."
    )
    stream: str = Field(description="stdout | stderr | unknown")
    message: str


class ContainerLogs(Model):
    """Recent output from one container. Read live, stored nowhere.

    Bounded twice — by lines and by bytes — and neither bound is negotiable from the
    request beyond asking for fewer lines. There is no follow, no stream and no websocket:
    Docker keeps the logs and LocalPlane does not build a second copy of them.
    """

    object_id: str
    container_id: str
    read_at: datetime
    requested_lines: int
    line_count: int
    truncated: bool = Field(
        description="True when the byte ceiling cut the read short before the line count did."
    )
    line_limit: int
    byte_limit: int
    source: str
    lines: list[ContainerLogLine]


class ContainerStats(Model):
    """One live sample of what a container is using. A snapshot, never a series.

    Docker's own numbers, normalised. There is no history here and none is stored: a
    metrics database is a product, not a field on a container, and this build does not have
    one. ``gaps`` names anything the sample could not answer rather than reporting a zero
    somebody would act on.
    """

    object_id: str
    container_id: str
    read_at: datetime
    sampled_at: datetime | None = Field(description="When the daemon took the sample.")
    cpu_percent: float | None = Field(
        description=(
            "Docker's own formula. Null rather than zero when the sample had no previous "
            "reading to subtract, because zero is a number an operator would act on."
        )
    )
    online_cpus: int | None
    memory_usage_bytes: int | None = Field(
        description="Usage with reclaimable page cache subtracted, as `docker stats` reports."
    )
    memory_usage_raw_bytes: int | None
    memory_limit_bytes: int | None
    memory_percent: float | None
    network_rx_bytes: int | None
    network_tx_bytes: int | None
    block_read_bytes: int | None
    block_write_bytes: int | None
    pids: int | None
    pids_limit: int | None
    gaps: list[str]


class ContainerObservationResult(Model):
    """What one container observation refresh did."""

    host_id: str
    sweep_id: str
    status: str = Field(description="ok | partial")
    container_count: int
    issues: list[ProviderIssue]
    provider_version: str


# --------------------------------------------------------------------------- systemd


class SystemdRelationship(Model):
    kind: str = Field(
        description=(
            "The exact systemd relation: Requires, Wants, Requisite, BindsTo, PartOf, "
            "Before, After, Conflicts, Triggers, TriggeredBy, OnFailure or OnSuccess forms."
        )
    )
    group: str = Field(description="requirement | ordering | conflict | activation | outcome")
    target_unit: str
    canonical_target: str | None
    target_object_id: str | None
    resolution: str = Field(description="resolved | referenced | external")
    estate_state: str | None = Field(description="current | not_observed | null for external")
    source: str


class SystemdJob(Model):
    id: int


class SystemdServiceFacts(Model):
    type: str | None
    main_pid: int | None
    control_pid: int | None
    exec_main_pid: int | None
    result: str | None
    exec_main_code: int | None
    exec_main_status: int | None
    restart: str | None
    restart_usec: int | None
    n_restarts: int | None
    remain_after_exit: bool | None
    guess_main_pid: bool | None
    exec_main_start_timestamp_monotonic: int | None
    watchdog_usec: int | None
    watchdog_timestamp_monotonic: int | None
    watchdog_signal: int | None
    watchdog_pid: int | None
    control_group: str | None


class SystemdSocketListen(Model):
    kind: str
    address: str


class SystemdSocketFacts(Model):
    listen: list[SystemdSocketListen] | None
    accept: bool | None
    accepted: int | None
    connections: int | None
    refused: int | None
    result: str | None
    trigger_limit_interval_usec: int | None
    trigger_limit_burst: int | None


class SystemdTimerFacts(Model):
    unit: str | None
    next_elapse_usec_realtime: int | None
    next_elapse_usec_monotonic: int | None
    last_trigger_usec: int | None
    last_trigger_usec_monotonic: int | None
    persistent: bool | None
    randomized_delay_usec: int | None
    fixed_random_delay: bool | None
    accuracy_usec: int | None
    result: str | None


class SystemdWatchedPath(Model):
    kind: str
    path: str


class SystemdPathFacts(Model):
    unit: str | None
    paths: list[SystemdWatchedPath] | None
    make_directory: bool | None
    directory_mode: str | None
    result: str | None


class SystemdMountFacts(Model):
    what: str | None
    where: str | None
    type: str | None
    control_pid: int | None
    directory_mode: str | None
    result: str | None
    sloppy_options: bool | None
    lazy_unmount: bool | None
    force_unmount: bool | None
    timeout_usec: int | None


class SystemdAgentContainment(Model):
    status: str
    method: str | None
    cgroup: str | None
    canonical_id: str | None
    invocation_id: str | None
    observed_at: datetime | None
    gaps: list[str]
    reason: str | None
    detail: dict[str, Any]


class SystemdUnit(Model):
    """One loaded system-manager unit, identified by systemd's canonical Unit.Id."""

    object_id: str
    kind: str
    canonical_id: str
    names: list[str] | None
    description: str | None
    unit_type: str
    identity: Identity
    management: Management
    health: Health | None
    observation: Observation | None
    observed_in_latest_sweep: bool | None = Field(
        description=(
            "Membership in the latest systemd inventory sweep, independent of a newer "
            "targeted observation on this unit."
        )
    )
    load_state: str | None
    active_state: str | None
    sub_state: str | None
    unit_file_state: str | None
    unit_file_preset: str | None
    can_start: bool | None
    can_stop: bool | None
    can_reload: bool | None
    refuse_manual_start: bool | None
    refuse_manual_stop: bool | None
    need_daemon_reload: bool | None
    fragment_path: str | None
    source_path: str | None
    drop_in_paths: list[str] | None
    transient: bool | None
    template: str | None
    current_job: SystemdJob | None
    invocation_id: str | None = Field(
        description="Execution-instance evidence. Never part of object identity."
    )
    timestamps: dict[str, int | None]
    relationships: list[SystemdRelationship]
    service: SystemdServiceFacts | None = None
    socket: SystemdSocketFacts | None = None
    timer: SystemdTimerFacts | None = None
    path: SystemdPathFacts | None = None
    mount: SystemdMountFacts | None = None
    agent_process_containment: SystemdAgentContainment | None = None
    first_seen_at: datetime
    last_seen_at: datetime


class SystemdUnitList(Model):
    host_id: str
    capability: Capability | None = Field(
        description="Last recorded systemd observation capability, including unavailable."
    )
    last_sweep: Sweep | None
    count: int
    units: list[SystemdUnit]


class SystemdObservationResult(Model):
    host_id: str
    sweep_id: str
    status: str = Field(description="ok | partial | failed")
    unit_count: int
    issues: list[ProviderIssue]
    provider_version: str
    listed_count: int | None
    selected_count: int | None
    inventory_limit: int | None
    inventory_complete: bool | None
    truncated: bool | None
    cap_reached: bool | None
    inventory_method: str | None
    agent_unit_resolution: SystemdAgentContainment | None


class Evidence(Model):
    """The raw material an observation was derived from."""

    object_id: str
    observation_id: str
    observed_at: datetime
    evidence: dict[str, Any]


class SweepList(Model):
    host_id: str
    count: int
    sweeps: list[Sweep]


class RefreshResult(Model):
    sweep_id: str
    host_id: str
    agent_instance_id: str | None
    status: str = Field(
        description=(
            "The interface observation's own status. Provider trouble does not change it: "
            "a sweep that saw every link and could not reach Docker did the first job."
        )
    )
    object_count: int
    observation_count: int
    missing: list[str]
    issues: list[ProviderIssue]
    providers: list[ProviderReadingSummary] = Field(
        default_factory=list,
        description=(
            "One entry per provider consulted for ownership evidence. Empty when none "
            "was — an agent without the capability, for instance, which `issues` explains."
        ),
    )


# -------------------------------------------------------------------------- management


class TypedValue(Model):
    """A value with its type stated, so a caller never has to infer one from JSON.

    ``true`` and ``1`` are different answers, and a client that guesses will eventually
    compare an admin state against an MTU.
    """

    value_type: str = Field(description="boolean | integer")
    value: bool | int


class ObservationRef(Model):
    """Which reading a record was derived from, and what produced it."""

    observation_id: str
    sweep_id: str
    observed_at: datetime
    capability: str
    provider: str
    provider_version: str


class IntentRevision(Model):
    """The event that produced one intent version by revising the one before it."""

    revision_id: str
    kind: str = Field(
        description=(
            "revise — the operator supplied a new desired value. adopt_runtime — the "
            "operator declared that what was observed is what was wanted. Different acts, "
            "kept apart so a history can still tell them apart."
        )
    )
    host_effect: str = Field(description="Always 'none'. The store cannot record otherwise.")
    occurred_at: datetime


class Intent(Model):
    """One version of what LocalPlane intends for an object.

    Immutable. Adopting again, or revising, writes a new version and leaves this one in
    place, so what was intended, when, and on what evidence stays answerable after the fact.
    """

    intent_id: str
    object_id: str
    host_id: str
    version: int
    supersedes: str | None = Field(description="The version this replaced, if any.")
    schema_version: int = Field(
        description="The shape of the controlled field set this intent was written against."
    )
    origin: str = Field(
        description=(
            "How this version came to exist: adopt | revise | adopt_runtime. Derived from "
            "the event that produced it, never from a column that could disagree with one."
        )
    )
    revision: IntentRevision | None = Field(
        default=None,
        description="The revision that produced this version. null when it was adopted.",
    )
    active: bool = Field(
        description="Whether this version is the one currently in force for the object."
    )
    captured_from: ObservationRef = Field(
        description=(
            "The observation this version was written against. For adopt and adopt_runtime "
            "its verified values became the intent; for an explicit revision it is the "
            "reading the operator's decision was made against, and the contract this "
            "intent is comparable under."
        )
    )
    created_at: datetime
    controlled_fields: list[IntentField] = Field(
        description=(
            "Everything LocalPlane is answerable for on this object, and nothing else. "
            "Transient link state, counters and dynamically acquired addresses are "
            "deliberately absent: they were never intended by anybody."
        )
    )


class FieldComparison(Model):
    """One typed, field-scoped verdict with the evidence behind it."""

    field: str
    value_type: str
    intended: bool | int
    observed: bool | int | None = Field(
        description="null when the value could not be read. That is unknown, never drift."
    )
    comparison: str = Field(description="matches | differs | unknown")
    reason: str = Field(description="Stable machine-readable code. Branch on this.")


class Reconciliation(Model):
    """How a managed object compares with its retained intent, computed now.

    Never stored. A reconciliation column would be a second copy of a fact that moves every
    time an observation lands, which is exactly the duplicated truth this model avoids.
    """

    state: ReconciliationState
    reason: str
    fields: list[FieldComparison]
    observation: ObservationRef | None = Field(
        description="The observation compared against. null when there has never been one."
    )
    as_of: datetime = Field(description="When this comparison was computed.")


class ReconciliationResult(Model):
    object_id: str
    management: Management
    reconciliation: Reconciliation | None = Field(
        description=(
            "null unless the object is managed. An object with no retained intent cannot "
            "drift, which is not the same as being in sync."
        )
    )
    intent: IntentSummary | None


class FindingEvidence(Model):
    """The typed comparison a drift claim rests on."""

    intent_id: str
    field: str
    intended: TypedValue
    observed: TypedValue | None = Field(
        description="null when the last evaluation could not read the value."
    )
    comparison: str = Field(description="differs | unknown")
    reason: str
    observation: str | None = Field(description="The observation this evidence came from.")
    sweep: str | None


class OwnershipFindingEvidence(Model):
    """The claim an ownership conflict rests on.

    A different kind of evidence from drift's, which is why it is a different shape. Drift
    compares one typed scalar against one controlled field; this names a relation, an owner
    and the source that established it. Squeezing the second into the first's fields would
    mean an ``intended`` value that stands for nothing.
    """

    intent_id: str = Field(description="The intent this conflicts with.")
    relation: str = Field(description="created_by | configured_by")
    owner: Owner
    confidence: str = Field(description="confirmed | corroborated")
    evidence_source: str
    reason: str
    provider_observation: str | None = Field(
        description="The provider reading that established the claim."
    )
    observation: str | None
    sweep: str | None


class Finding(Model):
    """A claim LocalPlane is making, with the evidence for it and its lifecycle.

    A finding is not a state. ``reconciliation = drifted`` is recomputed from scratch every
    time it is asked for; this is the durable record that LocalPlane noticed, when it first
    noticed, and how it ended. There is no severity: ranking one disagreement against
    another needs a model of what the object is for, and LocalPlane does not have one.
    """

    finding_id: str = Field(description="This episode. A recurrence is a new one.")
    finding_key: str = Field(
        description="The stable logical identity. The same disagreement always maps here."
    )
    host_id: str
    object_id: str
    object_name: str
    finding_type: str
    subject: str = Field(description="What within the object this is about — the field.")
    status: str = Field(description="open | resolved")
    summary: str = Field(
        description="Derived from the typed evidence at read time, never stored."
    )
    evidence: FindingEvidence | OwnershipFindingEvidence = Field(
        description="Shaped by `finding_type`. Both carry the typed values the claim rests on."
    )
    first_seen_at: datetime = Field(description="When this episode opened.")
    last_seen_at: datetime = Field(
        description=(
            "The last observation that *proved* the claim. Older than updated_at means "
            "the finding is open and the most recent look could not confirm it."
        )
    )
    updated_at: datetime = Field(description="The last evaluation that touched this record.")
    resolved_at: datetime | None
    resolution: str | None = Field(
        description=(
            "observed_matches_intent | intent_released | owner_no_longer_claims. "
            "Never a deletion."
        )
    )
    resolved_by_observation_id: str | None = Field(
        default=None, description="For drift: the observation that proved the claim ended."
    )
    resolved_by_provider_observation_id: str | None = Field(
        default=None,
        description=(
            "For an ownership conflict: the provider reading that stopped claiming the "
            "object. A provider that could not be read never resolves anything."
        ),
    )


class FindingList(Model):
    host_id: str
    status: str = Field(description="The filter this list was produced with.")
    count: int
    findings: list[Finding]


class ManagementTransition(Model):
    """An adopt or a release. Neither writes to the host."""

    transition_id: str
    transition: str = Field(description="adopt | release")
    from_state: ManagementState
    to_state: ManagementState
    intent_id: str
    observation_id: str | None = Field(
        description="The evidence adopt captured. Release consults none."
    )
    host_effect: str = Field(
        description="Always 'none'. The store cannot record a host write on this path."
    )
    occurred_at: datetime


class IntentHistory(Model):
    object_id: str
    active_intent_id: str | None
    count: int
    intents: list[Intent] = Field(
        description=(
            "Every version ever retained, newest first, each carrying the event that "
            "produced it. Reading `version`, `supersedes` and `origin` down the list "
            "reconstructs the whole chain: what was first adopted, every revision, which "
            "version superseded which, and which one is in force now."
        )
    )
    transitions: list[ManagementTransition] = Field(
        description=(
            "Adopt and release, newest first. Release leaves no other trace — the intent "
            "it retired is unchanged — so this is where the moment it stopped applying is. "
            "Revisions are not here: they do not move the management state, and a "
            "managed → managed row would describe a movement that did not happen. They "
            "are on the versions they produced."
        )
    )


class IntentRevisionRequest(Model):
    """The common part of both revisions: which version this decision was made against.

    ``expected_intent_id`` is required, and it is the whole of the lost-update defence. Two
    operators looking at the same drift will both read the intent in force, and whichever
    writes second must not silently overwrite a decision they never saw. A caller that
    names a version which is no longer active is told so and can decide again against the
    current desired state, which is the only safe thing to do with somebody else's
    judgement.
    """

    expected_intent_id: str = Field(
        description="The intent_id the caller read. Refused with 409 if it is not in force."
    )
    expected_version: int | None = Field(
        default=None,
        description=(
            "Optional cross-check on the same version. Refused with the same 409 when it "
            "disagrees, so a client holding a stale version number is not left to find out "
            "by comparing ids."
        ),
    )


class ExplicitIntentRevisionRequest(IntentRevisionRequest):
    """New desired values, supplied by the operator."""

    fields: dict[str, StrictBool | StrictInt] = Field(
        description=(
            "The controlled fields to give a new desired value, by name — currently "
            "`admin_up` (boolean) and `mtu` (integer). At least one is required. A name "
            "LocalPlane does not control is refused, never ignored, and a value of the "
            "wrong type is refused rather than coerced: `true` and `1` are different "
            "answers. Fields left out keep the value they had."
        )
    )


class FieldRevision(Model):
    """One controlled value moving from what was intended to what is now intended."""

    field: str
    value_type: str
    was: bool | int
    now: bool | int


class IntentRevisionResult(Model):
    """The result of revising a managed object's retained intent.

    ``host_mutated`` is ``false`` and there is no code path on which it could be anything
    else. A revision replaces one record of what LocalPlane wants with another; no link was
    brought up or down, no MTU was set, and this revision path reaches none of the agent's
    mutating methods — those belong to the apply of a Run. If the runtime and the intent
    now agree, they agree because the intent moved.
    """

    kind: str = Field(
        description=(
            "revise | adopt_runtime — the same value the new intent carries as `origin`, "
            "stated here because it is what this response is about."
        )
    )
    object_id: str
    host_id: str
    management: Management = Field(
        description=(
            "Unchanged, and reported so a caller can see it is unchanged. Revision is not "
            "a transition: the object was managed before and is managed after."
        )
    )
    host_mutated: bool = Field(
        description="Always false. This revision changed LocalPlane's records only."
    )
    host_effect: str = Field(description="Always 'none'.")
    note: str
    revision_id: str
    occurred_at: datetime
    previous_intent: Intent = Field(
        description=(
            "The version that was in force, exactly as it still is. Nothing about it was "
            "rewritten; it stopped being active, which is a different thing."
        )
    )
    intent: Intent = Field(description="The version now in force.")
    changed_fields: list[FieldRevision] = Field(
        description="The controlled values whose intended value this revision moved."
    )
    carried_forward: list[str] = Field(
        description=(
            "Controlled fields the operator did not name, kept at their existing intended "
            "value. Empty for adopt_runtime, where every value was read from the "
            "observation — including the ones that already agreed."
        )
    )
    reconciliation: Reconciliation = Field(
        description=(
            "Recomputed against the new intent and the newest observation. It may well be "
            "in_sync, and if it is, nothing about the host produced that."
        )
    )
    findings_resolved: list[str] = Field(
        default_factory=list,
        description=(
            "Drift findings closed by this revision, by id. Their resolution is "
            "`intent_revised`, never `observed_matches_intent`: nothing was remediated."
        ),
    )
    findings_opened: list[str] = Field(
        default_factory=list,
        description=(
            "Drift findings this revision opened — a new desired value the runtime does "
            "not have is a new disagreement, and it is recorded as one."
        ),
    )
    ownership: Ownership | None = Field(
        default=None,
        description=(
            "What LocalPlane knew about who owns this object when it agreed to revise, "
            "including any source it could not consult."
        ),
    )


class TransitionResult(Model):
    """The result of an adopt or a release.

    ``host_mutated`` is ``false`` and there is no code path on which it could be anything
    else. Adopt records values that were already true on the host; release forgets them.
    Neither brings an interface up, changes an MTU, nor dispatches one of the agent's
    mutating methods — those belong to the apply of a Run, and no transition here is one.
    """

    transition: str = Field(description="adopt | release")
    object_id: str
    host_id: str
    from_state: ManagementState
    to_state: ManagementState
    host_mutated: bool = Field(
        description="Always false. This transition changed LocalPlane's records only."
    )
    host_effect: str = Field(description="Always 'none'.")
    note: str
    transition_id: str
    occurred_at: datetime
    intent: Intent
    reconciliation: Reconciliation | None = Field(
        description="null after a release: nothing is retained, so nothing can disagree."
    )
    ownership: Ownership | None = Field(
        default=None,
        description=(
            "What LocalPlane knew about who owns this object when it agreed to manage it, "
            "including any source it could not consult. Null on release."
        ),
    )
    findings_resolved: list[str] = Field(
        default_factory=list,
        description="Findings closed by this transition, by id. Closed, never deleted.",
    )


# --------------------------------------------------------------------------------- runs


class ReconcileMtuOperation(Model):
    """Reconcile a managed interface's runtime MTU to the value its active intent holds.

    The body is a type and an object id, and there is nothing else it will accept. There is
    no desired value: the target comes from the retained intent, and an operator who wants a
    different one revises the intent first — through the endpoint that exists for it, with
    the concurrency check, the ownership gate and the version chain that come with it. A Run
    that could carry its own target would be a second, weaker route to changing desired
    state.

    There is also no field, no command, no argv, no provider, no method and no path.
    ``extra`` is forbidden on every model here, so a request carrying one is refused at the
    edge rather than ignored — and there is nothing behind this that could execute one.
    """

    type: Literal["network.interface.reconcile_mtu"] = Field(
        description="The typed operation. The vocabulary is closed; nothing else is accepted."
    )
    object_id: str = Field(description="The managed network interface to plan against.")


class ContainerLifecycleOperation(Model):
    """Start, stop or restart one container, through Docker's own API.

    The body is a type and an object id, and there is nothing else it will accept. The verb
    is *part of the type* — one of three names in a closed vocabulary — rather than a field,
    so there is no parameter through which a fourth could be requested, and no signal, grace
    period, timeout, command or payload anywhere on the path to the daemon.

    ``extra`` is forbidden on every model here, so a request carrying one is refused at the
    edge rather than ignored.
    """

    type: Literal[
        "docker.container.start",
        "docker.container.stop",
        "docker.container.restart",
    ] = Field(
        description="The typed operation. The vocabulary is closed; nothing else is accepted."
    )
    object_id: str = Field(description="The container to plan against.")


class SystemdServiceLifecycleOperation(Model):
    """Plan one closed systemd service action against an existing LocalPlane object.

    The request carries only the generic object id and the operation name.  The canonical
    Unit.Id, exact accepted socket tuple, effect graph and authorization assessment are all
    derived internally; no unit name or D-Bus field exists in this schema.
    """

    type: Literal[
        "systemd.service.start",
        "systemd.service.stop",
        "systemd.service.restart",
    ]
    object_id: str = Field(description="The existing systemd.unit object to plan against.")


class CreateRunRequest(Model):
    """Plan one typed operation.

    ``operation`` is a union discriminated on ``type`` — the shape it was designed as when
    it had one member. A request naming anything outside the seven closed operations is
    refused at the edge,
    before a planner is reached, by the same closed vocabulary the store CHECKs.
    """

    operation: (
        ReconcileMtuOperation
        | ContainerLifecycleOperation
        | SystemdServiceLifecycleOperation
    ) = Field(
        discriminator="type"
    )


class SelfImpactOverrideRequest(Model):
    """Accept the one typed hazard a self-impact plan publishes. Nothing else.

    **Everything this authority is about is already in the preview.** Which unit is at
    risk, whether LocalPlane may disappear, and whether it may come back are all in the
    immutable document this request names — bound into `expected_preview_digest`, which is
    checked. So there is nothing to echo back: asking a client to retype a display name
    would turn copied text into authority, and the copy could only ever agree with the
    document until the day it did not.

    **There is no field for choosing what to bypass.** No `force`, no `ignore_safety`, no
    list of blockers. A caller cannot name a hazard, a status or a verdict; the backend
    derived whether this exact plan is eligible for this one authority, and the only thing
    supplied here is that a person said yes to it.
    """

    preview_id: str = Field(
        description="The preview being overridden. Must be the one this run published."
    )
    expected_preview_digest: str | None = Field(
        default=None,
        description=(
            "Optional cross-check that the plan you read is the plan you are authorising. "
            "Recorded on the grant either way."
        ),
    )
    acknowledge: bool = Field(
        description=(
            "Must be true. It records that an operator accepted that carrying this out may "
            "interrupt LocalPlane itself, and that no second path to this host has been "
            "verified."
        )
    )


class ValidityReason(Model):
    code: str = Field(description="Stable machine-readable code. Branch on this.")
    detail: dict[str, Any] = Field(default_factory=dict)


class PlanValidity(Model):
    """Whether the published plan still describes what would happen if it ran.

    Derived for this request from what LocalPlane has already recorded — never stored, and
    reading it changes nothing. A stale plan is not an error and it is not rewritten: the
    preview stays exactly as it was published, and the remedy is to plan again.
    """

    state: str = Field(description="current | stale")
    reasons: list[ValidityReason] = Field(
        description=(
            "Empty when current. Otherwise what moved — the plan can no longer be made at "
            "all, or it can and comes out different."
        )
    )
    as_of: datetime = Field(description="When this was derived. It is never stored.")


class RunOperation(Model):
    type: str
    summary: str
    target_kind: str


class PlannedChange(Model):
    """WHAT would change, in one of two shapes.

    ``kind`` says which, and it is the field to read first. A **field** change moves one
    controlled value to another and fills `field` / `value_type` / `current` / `desired`. An
    **action** asks for a declared verb to be carried out and fills `action` /
    `observed_state` / `expected_state`; it has no desired *value*, because there is no
    retained value for it to reconcile towards.

    The unused half is null rather than absent, so the shape of the document does not change
    between operations — but nothing outside the half `kind` names carries any meaning.
    """

    object_id: str
    object_name: str
    kind: str = Field(description="field | action")

    field: str | None = Field(default=None, description="Field changes only.")
    value_type: str | None = Field(default=None, description="boolean | integer. Field only.")
    current: bool | int | None = Field(
        default=None, description="What the newest observation reported. Field changes only."
    )
    desired: bool | int | None = Field(
        default=None,
        description="What the active intent holds. Never supplied. Field changes only.",
    )

    action: str | None = Field(
        default=None,
        description=(
            "The declared verb. Actions only, and it comes from the operation the caller "
            "named out of a closed vocabulary — never from a value in the request."
        ),
    )
    observed_state: str | None = Field(
        default=None,
        description=(
            "The lifecycle state the resource was observed in. Actions only. Not a value to "
            "be written over: it is what the operator was looking at when they decided, and "
            "for a restart it is part of what a verification is judged against."
        ),
    )
    expected_state: str | None = Field(
        default=None,
        description="The state that must hold afterwards for this to have worked. Actions only.",
    )

    expected_after: bool | int | str = Field(
        description=(
            "What the host would read back if this were applied and verified. A distinct "
            "question from `desired` — a provider that normalised a value would answer it "
            "differently — and equal to it for the operations this build has."
        )
    )


class PlanRationale(Model):
    """WHY this plan exists.

    ``intent_id`` is null for an action, and structurally so: LocalPlane retains no desired
    state for something it only acts on, nothing has drifted, and there is no version chain.
    A reader should not take the null as "the intent could not be loaded".
    """

    intent_id: str | None = None
    intent_version: int | None = None
    reason: str = Field(
        description="Stable code for what motivates the plan."
    )
    drift_finding_id: str | None = Field(
        description=(
            "The durable claim this plan answers, when LocalPlane had one open. Evidence "
            "rather than a precondition: a disagreement is real whether or not a sweep has "
            "yet turned it into a finding."
        )
    )


class PlanExecution(Model):
    """HOW it would run, and everything that says it would not.

    Two different questions, answered separately on purpose. ``availability`` is about this
    build — whether LocalPlane has any code that would execute this. ``eligibility`` is
    about this plan — whether it would be allowed to run if it did. A plan can be perfectly
    describable and completely ineligible, and saying both is more useful than refusing to
    describe it.
    """

    availability: str = Field(
        description=(
            "available | unavailable | not_implemented. This describes whether the build "
            "has an executor for this operation; every operation this build plans has one, "
            "so `not_implemented` appears only on previews stored by an earlier build, or "
            "on an operation this build could no longer execute."
        )
    )
    eligibility: str = Field(
        description=(
            "eligible | guarded | blocked. `guarded` means ordinary execution is blocked "
            "and the connection-guarded path is the only write path that exists for this "
            "plan — which happens for exactly one situation: a target *proven* to carry the "
            "management path this request arrived over. A target whose relation cannot be "
            "established is `blocked` and stays there."
        )
    )
    blockers: list[str] = Field(
        description=(
            "Every reason, not the first. An operator who fixes one and returns to find "
            "another they were never told about has been given the plan in instalments."
        )
    )
    provider: str | None = Field(
        description=(
            "The provider that would perform the write, when one is truthfully known — "
            "every operation this build plans names its executor's provider. `null` means "
            "no provider truthfully owns this write, and naming a plausible command, "
            "binary or daemon would be inventing an execution path."
        )
    )
    required_capability: str = Field(
        description=(
            "The agent capability an execution would need. Deliberately not in the "
            "protocol's capability list — that describes what the agent can do."
        )
    )
    capability_declared_by_agent: bool = Field(
        description="Whether the agent probed and declared it. Checked, not assumed."
    )
    note: str


class PlanIntentRef(Model):
    intent_id: str
    version: int
    capability: str
    provider: str


class PlanObservationRef(Model):
    observation_id: str
    sweep_id: str
    observed_at: datetime


class PublishedOwnershipClaim(Model):
    relation: str = Field(description="created_by | configured_by")
    provider: str
    instance: str | None
    label: str | None
    confidence: str = Field(description="confirmed | corroborated")
    external: bool = Field(
        description="Whether this owner is a system LocalPlane would be competing with."
    )


class PlanOwnership(Model):
    """Ownership as it stood when the plan was published, not as it stands now.

    A plan is a decision made against the evidence that existed when it was made. A preview
    whose ownership section improved overnight would be a different plan wearing the same
    identity — so this is stored, and a change to it makes the preview stale instead.
    """

    state: str
    reason: str
    claims: list[PublishedOwnershipClaim]
    evidence_gaps: list[str] = Field(
        description=(
            "Sources that left the question open. Unlike adoption, these block execution: "
            "adopt records values the host already has, and a write would be LocalPlane "
            "acting."
        )
    )
    provider_readings: dict[str, Any] = Field(
        description="Which reading each source was assessed from, by provider."
    )


class PlanEvidence(Model):
    """Exactly which records this plan was derived from.

    ``intent`` is null for an action, structurally: LocalPlane retains no desired state for
    something it only acts on, so there is no version to cite and no contract to compare
    under. A reader should not take the null as "the intent could not be loaded".
    """

    intent: PlanIntentRef | None = None
    observation: PlanObservationRef
    ownership: PlanOwnership


class RiskFactor(Model):
    code: str
    floor: str = Field(description="The tier this factor sets as a floor.")
    detail: str


class PlanRisk(Model):
    """RISK, derived from evidence and never declared alone.

    An operation carries a base tier; ownership and protection can raise it and nothing
    lowers it. A factor that merely failed to lower the tier is still listed, because
    "LocalPlane could not rule this out" is the kind of evidence that otherwise disappears.
    """

    tier: str = Field(description="low | medium | high")
    factors: list[RiskFactor]


class PlanProtection(Model):
    """Whether the target is protected, why, and what the answer rested on.

    Frozen into the preview as it stood when the plan was published, not re-derived on
    every read. A plan is a decision made against the evidence that existed when it was
    made, and a preview whose protection section improved overnight would be a different
    plan wearing the same identity.

    ``unknown`` is a real answer, and it is never softened into ``clear``. The management
    path is proven only from the transport a request actually arrived on and the kernel's
    own route to its peer; it is never inferred from an interface's name, from which link
    carries the default route, from a proxy header, or from a claim in a request body.
    """

    status: str = Field(
        description=(
            "protected | clear | unknown. `clear` means every protection reason this build "
            "implements was evaluated and none applies — it is not a word for `safe`."
        )
    )
    reasons: list[str] = Field(
        description="Protection reasons proven to apply. Empty unless status is protected."
    )
    unresolved: list[str] = Field(
        description="Reasons whose evidence could not be settled. Empty unless unknown."
    )
    management_path: str = Field(
        description=(
            "on_management_path | not_on_management_path | unknown — this target's relation "
            "to the operator's path: the target, not the target, or unresolved."
        )
    )
    reason: str
    missing_evidence: list[str] = Field(
        description="The evidence that would settle it, named rather than worked around."
    )
    evidence_id: str | None = Field(
        default=None,
        description=(
            "The management-path observation this judgement was made from. Bound to the "
            "preview, and deliberately not part of its digest: a later observation proving "
            "the same path has confirmed the plan, not changed it."
        ),
    )
    evidence_observed_at: datetime | None = None
    assessed: list["PlanProtectionReason"] = Field(
        default_factory=list,
        description="Every independently evaluated reason and its immutable evidence.",
    )


class PlanProtectionReason(Model):
    reason: str
    status: str
    detail: str
    evidence_id: str | None = None
    observed_at: datetime | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    missing_evidence: list[str] = Field(default_factory=list)


class PlanAuthorization(Model):
    state: str = Field(description="not_preflighted")
    exact: bool
    authority: str
    decision_point: str
    action_id: str
    canonical_target: str
    verb: str
    reason: str


class PlanEffectEdge(Model):
    source: str
    relation: str
    target: str


class PlanRuntimeOwnerCorrelation(Model):
    """Digest-bound ``docker-direct-unix-v1`` semantics, never transient handles."""

    contract_version: str
    method_version: int
    provider: str
    status: str
    attestation_fingerprint: str | None
    endpoint: str | None
    container_id: str | None
    container_started_at: str | None
    engine_id: str | None
    direct_transport_verified: bool
    peer_service_main_verified: bool
    owner_unit_id: str | None
    owner_invocation_id: str | None
    execution_cgroup_relation: str | None
    gaps: list[str]


class PlanSystemdLifecycleContext(Model):
    status: str
    target_unit: str
    action: str
    effect_units: list[str]
    effect_edges: list[PlanEffectEdge]
    effect_complete: bool
    active_activation_sources: list[str]
    active_upholding_sources: list[str]
    management_units: list[str]
    management_complete: bool
    connection_unit: str | None = Field(
        default=None,
        description=(
            "The systemd unit containing the connection this request arrived over — the "
            "one hosting LocalPlane's backend. A member of `management_units`, named "
            "separately because that set does not say which member is LocalPlane."
        ),
    )
    connection_unit_type: str | None = Field(
        default=None,
        description="service | scope | slice — read from systemd, never from the name.",
    )
    agent_unit: str | None
    agent_complete: bool
    agent_unit_type: str | None = Field(
        default=None,
        description=(
            "The kind of containment the agent has. A `.scope` belongs to a container "
            "runtime, which systemd need not publish an effect edge for, so a closure "
            "that misses it has proven nothing."
        ),
    )
    gaps: list[str]
    restart_baseline_invocation_id: str | None
    runtime_owner: PlanRuntimeOwnerCorrelation | None = None


class PlanSelfImpact(Model):
    """Whether executing this plan could interrupt the LocalPlane backend publishing it.

    Derived from the protection assessment and the lifecycle context, and it moves neither.
    A plan carrying this section is exactly as protected as it would be without one; what
    it adds is *which* hazard inside that verdict is LocalPlane itself.

    **`override_eligible` is not permission and grants nothing.** No authority exists in
    this build — no grant, no endpoint, no execution path reads it. It says only whether
    this exact hazard is the shape a narrowly typed operator authority could later be
    issued against, published now so that what such an authority would name is a document
    you can already read.
    """

    subject: str = Field(description="localplane_backend_runtime — the only one assessed.")
    status: str = Field(
        description=(
            "not_detected | proven | possible | unresolved. `not_detected` is earned from "
            "evidence about where the backend runs, never from an absence of it."
        )
    )
    outage: str = Field(
        description=(
            "none | temporary_possible | indefinite_possible. A restart may interrupt "
            "LocalPlane and it may return once the runtime recovers; a stop may take it "
            "away with nothing scheduled to bring it back. Neither promises a return, and "
            "no second path to this host has been verified."
        )
    )
    override_eligible: bool = Field(
        description=(
            "Whether this hazard is the one shape a future typed authority could cover. "
            "Not permission, and nothing in this build acts on it."
        )
    )
    detail: str = Field(description="The typed code for the status. Branch on this.")
    owner_unit_id: str | None = Field(
        default=None, description="The unit whose interruption would take the backend with it."
    )
    envelope: str | None = Field(
        default=None, description="The closed contract the proof came under, when eligible."
    )
    reasons: list[str] = Field(
        default_factory=list,
        description="Typed codes for every reason this is not eligible. Empty when it is.",
    )
    required: bool = Field(
        default=False,
        description=(
            "Whether this plan's only write path is the override — `how.eligibility` is "
            "`self_impact_override_required`. A fact about the plan, not about the "
            "document: `override_eligible` says the hazard *could* be authorised, this "
            "says it is the one thing standing in the way."
        ),
    )
    granted: bool = Field(
        default=False,
        description="Whether an operator has granted the override for this run. Never a token.",
    )
    consumed: bool = Field(
        default=False,
        description="Whether that grant has been spent. It is single-use.",
    )


class PlanConfirmation(Model):
    """What confirming this plan takes, and whether it has been confirmed.

    ``token_issued`` is ``false`` on every path and the store will accept nothing else. A
    confirmation is a row naming this Run and this plan, not a bearer value handed to a
    caller: there is nothing here that could be presented to authorise anything else.
    ``satisfied`` is a fact about the Run rather than about the immutable document, and it
    is rendered from whether a confirmation has actually been recorded for it.
    """

    required: bool
    method: str = Field(description="none | acknowledge | typed")
    source: str = Field(
        description=(
            "policy | operation. An operation may make the requirement stronger and never "
            "weaker, and which of the two decided is recorded."
        )
    )
    reasons: list[str]
    policy: str = Field(
        description=(
            "The policy sentence in force when this was published. Stored, so a plan "
            "reviewed under one policy is not silently re-read under a later one."
        )
    )
    token_issued: bool = Field(
        description="Always false. Nothing is issued that could be presented elsewhere."
    )
    satisfied: bool = Field(
        description="Whether a confirmation has been recorded for this run."
    )
    satisfiable: bool = Field(
        description="Whether execution is available at all for this operation."
    )
    unsatisfiable_reason: str | None


class PlanVerification(Model):
    """What a future verification would have to observe. Nothing observed it."""

    executed: bool = Field(description="Always false. No verification has run.")
    capability: str
    provider: str
    field: str | None = None
    expect: bool | int | str
    condition: str


class PlanGuard(Model):
    """What a connection guard would be for this plan. Nothing here is armed.

    A connection guard is a **reversal held on the host with a deadline**, dispatched with
    no further request from anybody if nothing proves within the window that this console
    can still be reached over the object being changed. It is what makes a change to that
    object survivable rather than merely refused: the outage a mistake can cause is bounded
    by the window, and the bound is enforced by a component that does not depend on this
    connection, on this request, or on the process serving it.

    ``armed`` is ``false`` on every published plan and the store will not accept anything
    else — the same rule ``recovery.armed`` follows, for the same reason.
    """

    availability: str = Field(description="available | unavailable")
    reason: str = Field(
        description=(
            "The typed code. `guarded_execution_available`, or why not: "
            "`guard_not_required` (the target is not the management path, so the ordinary "
            "path is open and a guard would be a mechanism against no hazard), "
            "`operation_has_no_unattended_reversal`, `management_path_unproven`, "
            "`guard_capability_undeclared`."
        )
    )
    window_s: int = Field(
        description=(
            "How long the guard would hold. A policy constant: there is no request field, "
            "query parameter or setting through which a caller could choose it, because a "
            "caller who could choose it could choose one that never usefully fires."
        )
    )
    prerequisites: list[str] = Field(
        description=(
            "Every condition that must be proven before a guard is armed, in the order "
            "they are evaluated. All of them are re-proved from fresh evidence at apply "
            "time; the plan states them so an operator can see what the guard rests on."
        )
    )
    unmet: list[str] = Field(
        description=(
            "The ones that could not be proven when this plan was published. "
            "`host_side_guard_accepted` is never among them here — it cannot be evaluated "
            "before the attempt, and a plan that claimed it would be claiming a guard."
        )
    )
    armed: bool = Field(description="Always false. A plan is published before anything is armed.")
    guarantee: str


class PlanRecovery(Model):
    """What recovery would look like, and the fact that none of it is armed.

    ``armed`` is ``false`` on every path and the store will not accept anything else. "A
    rollback exists in principle" and "recovery is established and will fire" are different
    claims, and that difference is the whole point of the write-boundary rules.
    """

    mode: str = Field(description="auto | operator | none")
    rollback_possible: bool = Field(
        description="Whether a rollback exists in principle for this operation."
    )
    restores_field: str | None = Field(
        description="The field a rollback would restore, or null where there is nothing to."
    )
    restores_value: bool | int | None = Field(
        description=(
            "The value that would be put back: the one the host carries now. Null for an "
            "operation with no inverse LocalPlane may perform, where an inverse is not "
            "'the opposite verb' but a restoration nobody has to be asked about."
        )
    )
    armed: bool = Field(description="Always false. Nothing is holding this value.")
    guarantee: str = Field(description="Always 'none' while nothing is armed.")
    reason: str


class RunPreview(Model):
    """One published plan, exactly as it was published. Immutable.

    A trigger in the store refuses every update to it, so this is a faithful copy of what
    an operator was shown rather than a rendering of what would be decided now. If the
    truth underneath it moves, `validity` says so and the remedy is a new Run — the plan is
    never rewritten in place under the same identity.
    """

    preview_id: str
    preview_digest: str = Field(
        description=(
            "`sha256:<hex>` over a canonical form of the plan. What makes 'the operator "
            "confirmed this plan' checkable against 'this is the plan about to run'. "
            "Deliberately excludes timestamps and reading identifiers, which move without "
            "the plan meaning anything different."
        )
    )
    digest_version: int
    published_at: datetime
    operation: RunOperation
    what: PlannedChange
    why: PlanRationale
    how: PlanExecution
    evidence: PlanEvidence
    risk: PlanRisk
    protection: PlanProtection
    authorization: PlanAuthorization | None = None
    systemd_lifecycle_context: PlanSystemdLifecycleContext | None = None
    self_impact: PlanSelfImpact | None = None
    confirmation: PlanConfirmation
    verification: PlanVerification
    guard: PlanGuard
    recovery: PlanRecovery
    validity: PlanValidity


class RunPreviewSummary(Model):
    """The published plan reduced to what a list needs. The whole of it is on the Run.

    Carries the same two shapes as :class:`PlannedChange` and the same ``kind`` to tell them
    apart, so a list and a detail view never disagree about what a plan would do.
    """

    preview_id: str
    preview_digest: str
    published_at: datetime
    kind: str = Field(description="field | action")
    field: str | None = None
    current: bool | int | None = None
    desired: bool | int | None = None
    action: str | None = None
    observed_state: str | None = None
    expected_state: str | None = None
    risk_tier: str
    confirmation_required: bool
    execution_availability: str
    execution_eligibility: str
    blockers: list[str]
    validity: PlanValidity


class Run(Model):
    """A Run, and the plan it published.

    **A Run is still not a Change.** A Run is somebody asking what it would take to
    reconcile an object and whether doing so would be safe; a Change is the record that
    LocalPlane entered the path on which a host write may occur. They remain separate rows
    with separate meanings: planning creates a Run, and only applying creates a Change —
    not a preview, not a confirmation and not an armed checkpoint, because none of those
    can have moved anything about the host.
    """

    run_id: str
    host_id: str
    object_id: str
    object_name: str
    operation: str = Field(description="The typed operation this Run plans.")
    state: str = Field(
        description=(
            "preview | awaiting_confirmation | arming | applying | verifying | guarded | "
            "succeeded | failed | rolling_back | rollback_verifying | rolled_back | "
            "recovery_required | cancelled. Thirteen of the run lifecycle's fourteen "
            "states, and the store's "
            "CHECK accepts exactly these thirteen. `draft` is unreachable because creating "
            "a run is planning one. `guarded` means the change was written and verified "
            "against the object carrying this operator's own connection and a host-side "
            "guard is holding a reversal: the one thing still outstanding is whether the "
            "operator can still reach LocalPlane, which only their next request can answer."
        )
    )
    created_at: datetime
    cancelled_at: datetime | None
    finished_at: datetime | None
    host_mutated: bool = Field(
        description=(
            "True only when the host was written and LocalPlane can prove it. A "
            "`write_unknown` change is *not* reported here as a maybe — read `host_effect`."
        )
    )
    host_effect: str = Field(
        description=(
            "none | written | write_unknown. `none` for every run that never crossed the "
            "write boundary, and the store refuses any other value for one that did not."
        )
    )
    change_created: bool = Field(
        description=(
            "Whether this run crossed the write boundary. Planning, confirming and arming "
            "all happen without a Change, because none of them can have moved anything."
        )
    )
    change_id: str | None
    confirmation: RunConfirmation | None = Field(
        description="The confirmation recorded for this run, if one has been satisfied."
    )
    checkpoint: RunCheckpoint | None = Field(
        description=(
            "The recovery material, if it was armed. Its presence is what "
            "\"recovery is armed\" means."
        )
    )
    guard: RunGuard | None = Field(
        description=(
            "The connection guard, if one was established for this run. Its presence is "
            "what \"a guard is armed\" means, and the store refuses a change to the object "
            "carrying the management path without one."
        )
    )
    change: Change | None
    events: list[RunEventView] = Field(
        description="The append-only transcript, in order. Typed, never a debug log."
    )
    note: str
    preview: RunPreview


class RunSummary(Model):
    """A Run as a list renders it."""

    run_id: str
    host_id: str
    object_id: str
    object_name: str
    operation: str
    state: str
    created_at: datetime
    cancelled_at: datetime | None
    finished_at: datetime | None
    host_effect: str
    change_created: bool
    change_id: str | None
    preview: RunPreviewSummary


class RunList(Model):
    host_id: str
    state: str = Field(description="The filter this list was produced with.")
    count: int
    runs: list[RunSummary]


# ----------------------------------------------------------------------- write boundary


class ConfirmRunRequest(Model):
    """Satisfy the confirmation a published plan requires.

    ``preview_id`` is **required** and must be the preview this Run published. A digest is
    not enough: two identical concurrent plans share one, so a confirmation keyed on content
    could not say which of them an operator looked at. ``expected_preview_digest`` is an
    optional cross-check on *what* was confirmed, not a substitute for saying *which*.

    Nothing else is accepted. There is no field here for an MTU, an interface, a command, a
    provider or an actor, and ``extra`` is forbidden on every model in this file, so a
    request carrying one is refused at the edge rather than ignored.
    """

    preview_id: str = Field(
        description="The preview being confirmed. Must be the one this run published."
    )
    acknowledge: bool = Field(
        description=(
            "Must be true. `acknowledge` is what this build's medium-risk operations "
            "require; a plan whose target is proven to carry this connection requires the "
            "`typed` method as well, and `acknowledge_object` is how that is given."
        )
    )
    acknowledge_object: str | None = Field(
        default=None,
        description=(
            "Required when, and only when, the plan's confirmation method is `typed`: the "
            "display name of the object this plan changes, written out. It is not a "
            "password and it does not authorise anything — what makes a guarded execution "
            "permissible is the guard, the checkpoint and the proven relation. What typing "
            "establishes is that a person looked at *which* object this is about, and the "
            "string is stored as their statement rather than compared and discarded. "
            "Supplying one for a plan that does not require it is refused, not ignored."
        ),
    )
    expected_preview_digest: str | None = Field(
        default=None,
        description=(
            "Optional concurrency check. When given it must equal the published digest, "
            "so a confirmation cannot be recorded against a plan the caller has not seen."
        ),
    )


class KeepGuardedRunRequest(Model):
    """Keep a guarded change. One field, and it names nothing.

    There is deliberately no object id, no value, no interface, no deadline and no guard id
    here: what authorises keeping a guarded change is not something a caller can send, it is
    the management path this request itself re-proves over the object that was changed. A
    body that could name anything would be a body that could name the wrong thing.
    """

    acknowledge: bool = Field(
        description=(
            "Must be true. Keeping a change to the object carrying your own connection is "
            "a deliberate act, and a request that did not say so would be one a stray "
            "client could make."
        )
    )


class RunConfirmation(Model):
    """A confirmation that was actually satisfied. Durable, bound, single-use.

    There is no actor and no token. ``source`` records only that the request crossed the
    accepted authentication boundary; it does not identify a person. Nothing
    was issued that a caller could present anywhere else — the confirmation is a row naming
    this Run and this plan, and the only thing that can use it is an apply of this Run.
    """

    confirmation_id: str
    preview_id: str
    preview_digest: str
    required_method: str = Field(description="What the published policy demanded.")
    method: str = Field(description="What was given.")
    typed_statement: str | None = Field(
        description=(
            "What the operator wrote, when the method was `typed`. Stored as their "
            "statement: a record that said only 'typed' would have kept the ceremony and "
            "thrown away the evidence."
        )
    )
    policy: str
    source: str = Field(
        description=(
            "`authenticated_request` for new records; historical unauthenticated records "
            "remain unchanged. Neither value identifies a person."
        )
    )
    satisfied_at: datetime
    consumed: bool
    consumed_at: datetime | None
    consumed_by_attempt_id: str | None


class RunGuard(Model):
    """The connection guard armed for this run: what is holding it, and what it did.

    Distinct from `preview.guard`, which says what a guard *would* be. This is the one that
    exists. Its `phase` comes from the component actually holding it — the agent on the
    host — and is recorded when that component is asked, never inferred from the clock.
    """

    guard_id: str
    phase: str = Field(
        description=(
            "arming | armed | disarmed | fired | lost | unreachable. `arming` means "
            "LocalPlane asked and has not heard back, which is a crash window rather than a "
            "state anything may be dispatched from. `lost` means the holder no longer knows "
            "this guard — the change is done and nothing is watching it. `unreachable` "
            "means the holder could not be asked, which is the absence of an answer and "
            "not an answer."
        )
    )
    holder_id: str | None = Field(
        description=(
            "Which instance of the host-side component undertook to hold it. An answer "
            "from a different instance is not a report about this guard."
        )
    )
    window_s: int
    armed_at: datetime | None
    expires_at: datetime | None = Field(
        description=(
            "The deadline the holder stated, on the holder's clock. Recorded rather than "
            "recomputed: that is the clock the reversal will actually run on."
        )
    )
    window_lapsed: bool | None = Field(
        description=(
            "Whether the deadline has passed, derived on read and never stored. `true` on "
            "an unsettled guard means the reversal has probably happened and nobody has "
            "collected its result yet — which is what `POST /runs/{id}/guard/keep` does, "
            "and it is why reading this can never move the run on its own."
        )
    )
    restores_value: bool | int | None = Field(
        description="What the reversal would put back: the value the checkpoint holds."
    )
    reversal_attempt_id: str
    kept_at: datetime | None
    kept_evidence_id: str | None = Field(
        description=(
            "The management-path observation that proved this console could still be "
            "reached over the changed object. Taken *after* the write, over the path the "
            "write could have destroyed — which is why it is a different record from the "
            "one the plan rests on."
        )
    )
    settled_at: datetime | None
    settled_reason: str | None
    fired_at: datetime | None
    reversal_outcome: str | None = Field(
        description=(
            "not_written | written | write_unknown, for a guard that fired. Never derived "
            "from what the target holds afterwards — that answers what the host is now, "
            "which is a different question from whether the reversal occurred."
        )
    )
    reversal_reason: str | None


class RunCheckpoint(Model):
    """The durable material recovery rests on, written before the write boundary.

    Its existence *is* what "recovery is armed" means. A previous value known to a running
    process is not arming: a process that dies holding it leaves nothing behind, which is
    the situation this row exists for.
    """

    checkpoint_id: str
    field: str
    restores_value: bool | int = Field(description="The verified value before the change.")
    desired_value: bool | int
    observation_id: str = Field(description="The reading the before value came from.")
    observed_at: datetime
    intent_id: str
    intent_version: int
    management_path: str = Field(
        description=(
            "The target's proven relation to the operator's path at arming time. The store "
            "accepts only `not_on_management_path` here: guarded mutation of the path an "
            "operator is reached over is a capability this build does not have, so a "
            "checkpoint for one cannot be armed at all."
        )
    )
    evidence_id: str | None
    execution_correlation: dict[str, Any] = Field(
        description=(
            "The stable material the executor needs to reach the target — opaque to the "
            "Change engine, which must not learn what kind of thing it is moving."
        )
    )
    armed_at: datetime


class RunEventView(Model):
    """One typed entry in a Run's transcript. Append-only; the store refuses edits."""

    sequence: int
    event: str = Field(description="Closed vocabulary. Branch on this, not on `detail`.")
    state_from: str | None
    state_to: str | None
    occurred_at: datetime
    change_id: str | None
    detail: dict[str, Any] = Field(
        description="Structured accompaniment, never the authoritative statement."
    )


class ChangeMutation(Model):
    """What became of one dispatched mutation. Three truths, never interchangeable."""

    outcome: str | None = Field(
        description=(
            "not_written | written | write_unknown, or null while a dispatch is unsettled. "
            "`write_unknown` is not a failure mode of the other two: it means the write may "
            "have taken effect and LocalPlane cannot prove whether it did."
        )
    )
    reason: str | None
    provider: str | None
    method: str | None
    attempt_id: str
    dispatch_began_at: datetime | None = Field(
        description=(
            "Written and committed before the request was sent. A change that says dispatch "
            "began and records no outcome is `write_unknown` on any later reading — that is "
            "the crash window, stated rather than hidden."
        )
    )
    settled_at: datetime | None
    detail: dict[str, Any]


class ChangeVerification(Model):
    """Whether a fresh reading proved the wanted value. Never the mutation's own word."""

    outcome: str = Field(
        description=(
            "not_attempted | verified | mismatch | value_unreadable | "
            "observation_unavailable | source_incompatible | target_absent"
        )
    )
    observation_id: str | None = Field(
        description="The reading that proved it. Required for `verified`."
    )
    observed_value: bool | int | None
    expected_value: bool | int | None = Field(
        description="The value a field change had to show. Null for an action, which has none."
    )
    observed_state: str | None = Field(
        default=None,
        description="What an action's target was observed in. Null for a field change.",
    )
    expected_state: str | None = Field(
        default=None, description="The state an action had to produce. Null for a field change."
    )
    reason: str | None


class ChangeRollback(Model):
    """The restoration attempt, through the same privileged path, and its own read-back."""

    required: bool
    attempt_id: str | None
    dispatch_began_at: datetime | None
    outcome: str | None = Field(
        description="not_written | written | write_unknown, or null if none was attempted."
    )
    reason: str | None
    restores_value: bool | int | None = Field(
        description=(
            "The value a restoration would put back. Null for an action, which has none — "
            "the inverse of `start` is not `stop`, and the store refuses a row claiming one."
        )
    )
    verification: ChangeVerification
    detail: dict[str, Any]


class RecoveryAttemptView(Model):
    """One recovery action against a Change: a retry, or a person releasing the hold.

    A **later, separate event**. Nothing here is a correction to the Change it belongs to —
    that record goes on saying what happened to it, and this says what somebody did about it
    afterwards.
    """

    attempt_id: str
    sequence: int
    kind: str = Field(description="retry | resolve.")
    started_at: datetime
    finished_at: datetime | None
    outcome: str = Field(
        description=(
            "in_flight | proven | verified | not_written | write_unknown | not_proven | "
            "refused | interrupted | resolved. `proven` is a fresh reading establishing the "
            "end state with no mutation at all; `refused` is provably no new host effect."
        )
    )
    refusal_code: str | None = Field(
        description="Why it stopped before dispatching anything. Typed, never prose."
    )
    releases_hold: bool
    management_path: str = Field(
        description=(
            "The relation this attempt's *own* request proved. Evidence from the original "
            "Run is never reused as authority for a new write."
        )
    )
    protection_evidence_id: str | None
    evidence: ChangeVerification = Field(
        description=(
            "The reading taken **before** anything was written, and what the operation made "
            "of it. `verified` here means the end state was already reached."
        )
    )
    mutation: ChangeMutation | None = Field(
        description="The new write attempt, where one happened. Null where none did."
    )
    host_effect: str = Field(
        description=(
            "What *this attempt* did to the host. The Change's own `host_effect` still "
            "answers what the original attempt did; they are separate facts."
        )
    )
    verification: ChangeVerification | None = Field(
        description="The reading taken **after** a new write. Null where nothing was written."
    )
    confirmation_id: str | None
    operator_statement: str | None = Field(
        description="What a person typed to release the hold deliberately. Resolutions only."
    )
    note: str | None


class RecoveryAuthority(Model):
    """An outstanding grant authorising one recovery retry to write again.

    Recorded in the same table, under the same single-use rule, as the confirmation that
    authorised the original apply — and never that one, which was consumed by it.
    """

    confirmation_id: str
    required_method: str
    method: str
    policy: str
    source: str = Field(description="Authentication-boundary source; nobody is identified.")
    satisfied_at: datetime


class ChangeRecovery(Model):
    """What LocalPlane knows and does not know when it could not prove a safe end state.

    It also records what has been done about the recovery requirement. `required` says
    this Change ended in recovery and goes on saying so for ever; `state` says whether the
    hold is still held.
    """

    required: bool
    state: str = Field(
        description=(
            "not_required | unresolved | resolved. `resolved` means the hold was released — "
            "by a retry that proved the end state, or by a person. It never means the change "
            "succeeded, and `result` is left saying exactly what it said."
        )
    )
    reason: str | None = Field(description="Typed code, never prose.")
    known: dict[str, Any] = Field(
        description="What is established: the value before, the value wanted, what was read."
    )
    unknown: list[str] = Field(
        description="What is not established, stated rather than glossed over."
    )
    object_write_locked: bool = Field(
        description=(
            "Whether this object's controlled field is still held. A recovery-required "
            "change keeps the hold: the value is unproven, and a second change against it "
            "would be building on a foundation nobody has checked."
        )
    )
    released_at: datetime | None
    released_by: str | None = Field(description="retry | resolve, or null while it is held.")
    released_by_attempt_id: str | None
    last_observed: dict[str, Any] = Field(
        description=(
            "The newest reading any recovery action took, and what the operation made of "
            "it. Empty until one has been taken."
        )
    )
    attempts: list[RecoveryAttemptView] = Field(
        description="Every recovery action, in order. Append-only and never rewritten."
    )
    authority: RecoveryAuthority | None = Field(
        description="An outstanding grant for one retry that must write again, if there is one."
    )
    available_actions: list[str] = Field(
        description=(
            "What the operator can do next: `retry` re-attempts the original end state and "
            "looks before it writes; `confirm_retry` grants the authority a retry needs if "
            "it must write again; `resolve` releases the hold and claims nothing."
        )
    )


class Change(Model):
    """The durable record that LocalPlane entered a path on which a host write may occur.

    **Not a claim that one happened.** `mutation.outcome` answers that, and it has three
    values because there are three truths. A Change may exist with `not_written`: the
    boundary was crossed and the privileged path refused before the kernel could accept
    anything, which is a true and useful thing to have recorded.

    A Change is also none of the other things LocalPlane records. It is not a management
    transition, not an intent revision, not a Run and not a confirmation — those are
    decisions about LocalPlane's own records, and this is the moment it stopped deciding.
    """

    change_id: str
    run_id: str
    preview_id: str
    checkpoint_id: str | None = Field(
        description="Null for an action: there is no previous value, so nothing to restore."
    )
    host_id: str
    object_id: str
    object_name: str
    operation: str
    change_kind: str = Field(
        description=(
            "field | action. The discriminator, and the only honest way to read the two "
            "halves below: which of them carries anything is decided here, not by probing "
            "for nulls."
        )
    )
    field: str | None
    before_value: bool | int | None
    desired_value: bool | int | None
    action: str | None = Field(description="The declared verb. An action only.")
    observed_state: str | None = Field(
        description="What the resource was observed in when the plan was made. An action only."
    )
    expected_state: str | None = Field(
        description="What must hold afterwards for the action to have worked."
    )
    created_at: datetime = Field(description="The write-boundary time.")
    host_effect: str = Field(
        description=(
            "none | written | write_unknown. Widened from the single value every event "
            "table in LocalPlane carried in earlier schema versions."
        )
    )
    host_mutated: bool = Field(
        description="True only for `written`. `write_unknown` is not a maybe-true here."
    )
    mutation: ChangeMutation
    verification: ChangeVerification
    rollback: ChangeRollback
    recovery: ChangeRecovery
    result: str = Field(
        description=(
            "in_flight | succeeded | failed | rolled_back | recovery_required. `failed` is "
            "permitted only where nothing was written and that is provable; the store "
            "refuses anything else."
        )
    )
    finished_at: datetime | None
    events: list[RunEventView]


class ChangeSummary(Model):
    """A Change as a list renders it."""

    change_id: str
    run_id: str
    object_id: str
    object_name: str
    operation: str
    change_kind: str
    field: str | None
    before_value: bool | int | None
    desired_value: bool | int | None
    action: str | None
    expected_state: str | None
    created_at: datetime
    finished_at: datetime | None
    host_effect: str
    mutation_outcome: str | None
    verification_outcome: str
    rollback_outcome: str | None
    result: str
    recovery_required: bool
    recovery_reason: str | None
    recovery_state: str = Field(
        description=(
            "not_required | unresolved | resolved. `resolved` says the hold was released, "
            "never that the change succeeded — `result` goes on saying what happened."
        )
    )


class ChangeList(Model):
    host_id: str
    count: int
    changes: list[ChangeSummary]


class RecoveryConfirmRequest(Model):
    """Authorise one recovery retry to dispatch a *new* mutation.

    The confirmation the original apply consumed authorises nothing here. It authorised an
    attempt; that attempt happened. This is a second, separate grant — recorded in the same
    table, under the same single-use rule — and at most one may be outstanding at a time.

    **A retry does not need this to begin.** A retry first asks whether a fresh reading
    already proves the end state, and one that does completes without writing and without
    consuming anything. This exists for the case where it does not.

    Nothing else is accepted. There is no field here for a value, a verb, a target, a
    provider or a command, and ``extra`` is forbidden on every model in this file.
    """

    acknowledge: bool = Field(
        description="Must be true. The same acknowledge method the original plan required."
    )
    expected_recovery_reason: str | None = Field(
        default=None,
        description=(
            "Optional cross-check. When given it must equal the change's recovery reason, "
            "so authority cannot be granted against a situation the caller has not seen."
        ),
    )


class RecoveryResolveRequest(Model):
    """Release a recovery hold by hand. No host mutation, and the store refuses one.

    This says that a person has inspected or handled the situation and LocalPlane may give
    the object back. It does **not** say the change succeeded, that the mutation happened,
    that the host is safe, that anything was rolled back or that the intended state was
    reached; the original result and the original recovery reason are left saying exactly
    what they said, and whatever can currently be observed is recorded beside the decision
    as what it is.

    ``acknowledge_object`` is the object's name, typed out under the identity this build
    holds: an escape from a safety hold should not be one accidental click.
    """

    acknowledge: bool = Field(description="Must be true.")
    acknowledge_object: str = Field(
        description=(
            "The object's name, typed out. Recorded as the operator's statement; it must "
            "match, and it is the deliberation rather than a lookup key."
        )
    )
    note: str | None = Field(
        default=None,
        max_length=2000,
        description="What the operator wants recorded beside the release. Kept verbatim.",
    )
    expected_recovery_reason: str | None = Field(
        default=None, description="Optional cross-check against the change's recovery reason."
    )


# ----------------------------------------------------------------------- management path


class TransportEvidenceView(Model):
    """What the server side of *this* connection says about how the request arrived.

    Read from the accepted socket, never from a header. `X-Forwarded-For`, `X-Real-IP` and
    `Forwarded` are not consulted anywhere on this path, and there is no setting that turns
    them on: a management path proven from something the caller wrote is a request that has
    been believed rather than evidence that has been gathered.

    It is transport, not identity. Authentication proves credential possession but identifies
    nobody on the other end of this socket — the value of `peer_address` is precisely that a
    caller cannot choose it, not that it says who anyone is.
    """

    peer_address: str | None = Field(
        description="The remote end of this connection, when it is a usable unicast address."
    )
    peer_family: str | None
    local_endpoint_address: str | None = Field(
        description=(
            "The local address this connection terminated on — the stronger of the two "
            "facts, because it is what ties the connection to an object."
        )
    )
    local_endpoint_family: str | None
    usable: bool = Field(
        description="Whether this transport can establish a management path at all."
    )
    reason: str | None = Field(
        description="Why it cannot, when it cannot. Loopback is the common one."
    )


class RouteEvidenceView(Model):
    """What the kernel answered when asked for the route to the peer. Facts only.

    `oif_index` is the kernel's interface index. The agent does not turn it into a name or
    an object: correlating it with a LocalPlane object is a judgement, and judgements are
    the backend's.
    """

    status: str = Field(description="resolved | unreachable | failed | unavailable")
    reason: str | None
    family: str | None
    destination: str | None
    destination_prefix_length: int | None
    preferred_source: str | None
    gateway: str | None
    oif_index: int | None
    table: int | None
    route_type: str | None
    scope: str | None
    protocol: str | None
    priority: int | None
    error: dict[str, Any] | None


class ManagementPathEvidence(Model):
    """One recorded observation of how a connection reached LocalPlane.

    Raw evidence, kept so that a protection judgement stays answerable afterwards. It does
    not hold a conclusion: which object carries the path depends on facts that move
    independently of this record, so the verdict is derived at read time and this is what
    made it believable.
    """

    observation_id: str
    host_id: str
    observed_at: datetime
    agent_instance_id: str | None
    capability: str
    provider: str
    provider_version: str
    method: str
    transport_peer_address: str
    local_endpoint_address: str
    family: str
    route: RouteEvidenceView


class ManagementPath(Model):
    """Which object carries the connection this request arrived on, or why that is unknown.

    The answer depends on the connection asking, and deliberately so. Evidence proving that
    one operator's session terminates on one object says nothing about a second operator's
    session, about automation calling over loopback, or about the same operator arriving on
    a different address — and a judgement that an object is safe to change rests on evidence
    about the session it would be changed from.

    Confirming it requires two independent sources to agree: the local address this
    connection terminated on must be present on exactly one currently observed object, and
    the kernel's route to the peer must leave by that same object. Disagreement resolves
    nothing and is reported as a conflict.
    """

    host_id: str
    state: str = Field(description="confirmed | unresolved")
    object_id: str | None = Field(
        description="The object carrying the path. Null unless state is confirmed."
    )
    object_name: str | None
    reason: str = Field(
        description=(
            "The typed code for this answer — `management_path_confirmed`, or which piece "
            "of evidence was missing, unusable or in conflict."
        )
    )
    missing_evidence: list[str] = Field(
        description="What would settle it, named rather than worked around."
    )
    transport: TransportEvidenceView
    evidence: ManagementPathEvidence | None = Field(
        description="The observation this answer was derived from, when there is one."
    )
    evidence_ttl_seconds: float = Field(
        description=(
            "How long an observation vouches for a path. The same horizon every other "
            "observation in LocalPlane uses: an address can be removed and a route replaced "
            "at any time, so this evidence is not entitled to a longer life than the "
            "readings the rest of the product rests on."
        )
    )
    as_of: datetime


class ManagementPathObservationResult(Model):
    """The outcome of observing the management path for this request.

    Read-only with respect to the host. The kernel is asked which route it would use to
    reach the peer — an `RTM_GETROUTE` netlink query, a question — and nothing is created,
    modified, brought up or brought down.
    """

    host_id: str
    host_mutated: bool = Field(description="Always false. This observes; it does not change.")
    host_effect: str = Field(description="Always 'none'.")
    recorded: bool = Field(
        description=(
            "Whether evidence was written. False when the transport could not establish "
            "anything — a request from loopback leaves no row, because a record that looks "
            "like evidence and proves nothing is worse than no record."
        )
    )
    note: str
    management_path: ManagementPath


class ProtectionReasonView(Model):
    """One protection reason, evaluated against one object."""

    reason: str
    status: str = Field(description="protected | clear | unknown, for this reason alone.")
    detail: str = Field(description="The typed code for what was decided.")
    evidence_id: str | None
    observed_at: datetime | None


class ObjectProtection(Model):
    """Whether this object is protected, why, and on what evidence.

    A separate axis from ownership, and the two answer different questions. Ownership asks
    whose the object is; protection asks what changing it would put at risk. An object can
    be LocalPlane's own and protected, or externally configured and not protected at all,
    and a Run can truthfully publish `protection: clear` while remaining blocked because
    another system demonstrably configures the object.

    `clear` is scoped to the reasons assessed for this object and operation. It is not a
    word for `safe`.
    """

    object_id: str
    object_name: str
    status: str = Field(description="protected | clear | unknown")
    reasons: list[str] = Field(description="Reasons proven to apply.")
    unresolved: list[str] = Field(description="Reasons whose evidence could not be settled.")
    management_path: str = Field(
        description="on_management_path | not_on_management_path | unknown"
    )
    reason: str
    missing_evidence: list[str]
    assessed: list[ProtectionReasonView]
    implemented_reasons: list[str] = Field(
        description=(
            "Every protection reason evaluated for this object. `clear` means these were "
            "evaluated and none applied — which is why the list is published rather than "
            "left for a reader to assume."
        )
    )
    note: str
    as_of: datetime
