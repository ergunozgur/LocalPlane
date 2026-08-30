"""Capability discovery.

A capability is reported because it was probed on this host and worked — not because
LocalPlane has a name for it. Discovery runs the same reads the capability would perform
in earnest, so ``available`` means "this was tried and it answered", ``degraded`` means
"it answered with less than the full evidence, and here is what is missing", and
``unavailable`` means "do not ask".

The backend is expected to treat an absent capability and an unavailable one identically,
and to treat a degraded one as usable but incomplete.

Mutating capabilities are probed without performing the mutation they describe. Asking the
helper what it is (``helper.hello``), making Docker's established non-mutating access probe,
and introspecting systemd's closed lifecycle contract establish mechanism availability
without changing a resource. A capability probe that had to write would be a write nobody
asked for. Per-request systemd authorization remains a separate dispatch-time assessment;
it is not capability degradation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from localplane.agent.guard import MAX_WINDOW_S as MAX_GUARD_WINDOW_S
from localplane.agent.guard import MIN_WINDOW_S as MIN_GUARD_WINDOW_S
from localplane.agent.identity import HostIdentityUnavailable, identify_host
from localplane.agent.docker_direct_attestation import DockerDirectAttestationReader
from localplane.agent.providers.base import CommandRunner, SubprocessRunner
from localplane.agent.providers.collector import NetworkProviderEvidenceCollector
from localplane.agent.providers.docker import (
    DEFAULT_DOCKER_SOCKET,
    DockerFailure,
    DockerProvider,
    probe_peer_pidfd_support,
)
from localplane.agent.providers.evidence import ProviderStatus
from localplane.agent.providers.linux_network import (
    ADDR_ARGV,
    DEFAULT_SYSFS_NET,
    LINK_ARGV,
    iter_interface_names,
)
from localplane.agent.providers.linux_route import (
    PROBE_DESTINATION,
    RouteLookupStatus,
    RouteQuery,
    probe_route,
)
from localplane.agent.providers.linux_socket_diag import LinuxSocketDiag
from localplane.agent.providers.systemd import SystemdProvider
from localplane.helper.client import HelperClient, HelperError
from localplane.helper.protocol import (
    HELPER_PROTOCOL_VERSION,
    HelperMethod,
)
from localplane.helper.server import default_helper_socket_path
from localplane.protocol.capabilities import (
    CAPABILITIES,
    CAPABILITY_DOCKER_CONTAINER_LIFECYCLE,
    CAPABILITY_DOCKER_CONTAINERS_OBSERVE,
    CAPABILITY_HOST_OBSERVE,
    CAPABILITY_NETWORK_INTERFACE_MTU_GUARD,
    CAPABILITY_NETWORK_INTERFACE_SET_MTU,
    CAPABILITY_NETWORK_OBSERVE,
    CAPABILITY_NETWORK_PROVIDERS_OBSERVE,
    CAPABILITY_NETWORK_ROUTE_OBSERVE,
    CAPABILITY_SYSTEMD_UNITS_OBSERVE,
    CAPABILITY_SYSTEMD_SERVICE_LIFECYCLE,
    CAPABILITY_SYSTEMD_SERVICE_LIFECYCLE_CONTEXT_OBSERVE,
    CapabilityStatus,
)


@dataclass(frozen=True)
class DiscoveredCapability:
    """A capability as it stands on this host, right now."""

    capability: str
    version: int
    status: CapabilityStatus
    mutating: bool
    summary: str
    discovered_at: str
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.status is not CapabilityStatus.UNAVAILABLE

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "version": self.version,
            "status": str(self.status),
            "mutating": self.mutating,
            "summary": self.summary,
            "reason": self.reason,
            "detail": self.detail,
            "discovered_at": self.discovered_at,
        }


def discover_capabilities(
    root: str | Path = "/",
    sysfs_net: str | Path = DEFAULT_SYSFS_NET,
    runner: CommandRunner | None = None,
    docker_socket: str | Path = DEFAULT_DOCKER_SOCKET,
    route_query: RouteQuery | None = None,
    helper_client: Any | None = None,
    helper_socket: str | Path | None = None,
    docker_provider: Any | None = None,
    systemd_provider: Any | None = None,
    socket_diag_provider: Any | None = None,
    runtime_attestation_reader: Any | None = None,
) -> tuple[DiscoveredCapability, ...]:
    """Probe every capability this agent implements. Returns them all, including failures."""
    runner = runner if runner is not None else SubprocessRunner()
    write = _probe_network_interface_set_mtu(helper_client, helper_socket)
    systemd = (
        systemd_provider
        if systemd_provider is not None
        else SystemdProvider(runtime_path=Path(root) / "run/systemd/system")
    )
    socket_diag = socket_diag_provider or LinuxSocketDiag()
    # The ordinary observation root is environment-selectable for fixtures.  Runtime-owner
    # authority is not: production reads the fixed host attestation unless a test injects
    # the closed reader seam directly.
    runtime_attestation = runtime_attestation_reader or DockerDirectAttestationReader()
    return (
        _probe_host_observe(Path(root)),
        _probe_network_observe(Path(sysfs_net), runner),
        _probe_network_providers_observe(runner, docker_socket),
        _probe_network_route_observe(route_query),
        write,
        _probe_network_interface_mtu_guard(write),
        *_probe_docker(docker_socket, docker_provider),
        _probe_systemd(systemd),
        *_probe_systemd_lifecycle(systemd, socket_diag, runtime_attestation),
    )


def _probe_host_observe(root: Path) -> DiscoveredCapability:
    definition = CAPABILITIES[CAPABILITY_HOST_OBSERVE]
    try:
        identity = identify_host(root)
    except HostIdentityUnavailable as exc:
        return _capability(
            definition,
            CapabilityStatus.UNAVAILABLE,
            reason="host_identity_unavailable",
            detail={"error": str(exc)},
        )
    if identity.gaps:
        return _capability(
            definition,
            CapabilityStatus.DEGRADED,
            reason="incomplete_host_evidence",
            detail={"gaps": list(identity.gaps), "identity_basis": identity.identity_basis},
        )
    return _capability(
        definition,
        CapabilityStatus.AVAILABLE,
        detail={"identity_basis": identity.identity_basis},
    )


def _probe_network_observe(sysfs_net: Path, runner: CommandRunner) -> DiscoveredCapability:
    definition = CAPABILITIES[CAPABILITY_NETWORK_OBSERVE]
    try:
        interface_count = len(list(iter_interface_names(sysfs_net)))
    except OSError as exc:
        return _capability(
            definition,
            CapabilityStatus.UNAVAILABLE,
            reason="sysfs_unreadable",
            detail={"path": str(sysfs_net), "error": exc.strerror or str(exc)},
        )

    methods: dict[str, str] = {"sysfs": "ok"}
    failures: list[str] = []
    for argv, key in ((LINK_ARGV, "rtnetlink_link"), (ADDR_ARGV, "rtnetlink_addr")):
        result = runner.run(argv, timeout_s=5.0)
        if result.ok:
            methods[key] = "ok"
        else:
            methods[key] = "unavailable"
            failures.append(
                f"{' '.join(argv)}: {result.error or result.stderr.strip() or result.returncode}"
            )

    detail: dict[str, Any] = {
        "methods": methods,
        "sysfs_path": str(sysfs_net),
        "interfaces_visible": interface_count,
    }
    if failures:
        detail["failures"] = failures
        return _capability(
            definition,
            CapabilityStatus.DEGRADED,
            reason="no_l3_address_source",
            detail=detail,
        )
    return _capability(definition, CapabilityStatus.AVAILABLE, detail=detail)


def _probe_network_providers_observe(
    runner: CommandRunner, docker_socket: str | Path
) -> DiscoveredCapability:
    """Consult the providers for real, and report what each one did.

    A host where none of these systems is installed is ``available``, not ``unavailable``:
    "Docker is not here" is a complete answer, and one LocalPlane can act on. What makes
    this capability degraded is a provider that *is* here and would not answer, because
    then part of the ownership question genuinely cannot be settled.
    """
    definition = CAPABILITIES[CAPABILITY_NETWORK_PROVIDERS_OBSERVE]
    batch = NetworkProviderEvidenceCollector(runner=runner, docker_socket=docker_socket).collect()
    by_provider = {r.provider: str(r.status) for r in batch.readings}
    unreadable = {
        r.provider: r.reason
        for r in batch.readings
        if r.status in (ProviderStatus.UNAVAILABLE, ProviderStatus.ERROR)
    }
    detail: dict[str, Any] = {
        "providers": by_provider,
        "sources": [r.source for r in batch.readings],
    }

    if not batch.readings:
        return _capability(
            definition,
            CapabilityStatus.UNAVAILABLE,
            reason="no_provider_sources",
            detail=detail,
        )
    if len(unreadable) == len(batch.readings):
        return _capability(
            definition,
            CapabilityStatus.UNAVAILABLE,
            reason="no_provider_source_readable",
            detail={**detail, "unreadable": unreadable},
        )
    if unreadable:
        return _capability(
            definition,
            CapabilityStatus.DEGRADED,
            reason="provider_sources_unreadable",
            detail={**detail, "unreadable": unreadable},
        )
    return _capability(definition, CapabilityStatus.AVAILABLE, detail=detail)


def _probe_network_route_observe(route_query: RouteQuery | None = None) -> DiscoveredCapability:
    """Ask the kernel for a route it certainly has, and report what happened.

    The probe is the capability's own read, run in earnest against a fixed destination —
    loopback, which every Linux host routes. It proves the whole path: that a netlink
    socket can be opened at all, that the kernel answers this process, and that the reply
    parses. A host where any of that is untrue must not have LocalPlane believing it can
    corroborate a management path.

    There is no ``degraded`` here. The capability answers one question and either the
    kernel can be asked it or it cannot.
    """
    definition = CAPABILITIES[CAPABILITY_NETWORK_ROUTE_OBSERVE]
    observation = probe_route(query=route_query)
    detail: dict[str, Any] = {
        "probe_destination": PROBE_DESTINATION,
        "source": "rtnetlink",
        "method": "netlink_rtm_getroute",
        "status": str(observation.status),
    }
    if observation.status is not RouteLookupStatus.RESOLVED:
        return _capability(
            definition,
            CapabilityStatus.UNAVAILABLE,
            reason=observation.reason or "route_lookup_unavailable",
            detail={**detail, "error": observation.error},
        )
    return _capability(definition, CapabilityStatus.AVAILABLE, detail=detail)


def _probe_network_interface_mtu_guard(write: DiscoveredCapability) -> DiscoveredCapability:
    """Whether this agent can hold a connection guard, derived from the write it reverses.

    **It is derived rather than separately probed, and that is a statement about the
    mechanism.** A guard is a deferred use of exactly the write above: this process holds a
    timer, and when it expires it sends the same typed request through the same privileged
    helper. So there is nothing else to ask. What would be dishonest is to report the guard
    available while the write it consists of is not — a plan published as guardable against
    a host whose helper is missing would arm nothing and then dispatch a change nothing was
    watching.

    ``degraded`` is carried through for the same reason it exists on the write: a helper
    that is speaking and cannot configure links would refuse the reversal too, and a
    capability that hid that would be a guard that reports itself armed and cannot act.

    That this capability exists *at all* is the fact the backend needs and cannot derive:
    an older agent that can set an MTU and knows nothing about guards simply does not list
    it, and the backend then publishes no guarded plan.
    """
    definition = CAPABILITIES[CAPABILITY_NETWORK_INTERFACE_MTU_GUARD]
    detail: dict[str, Any] = {
        "reverses_capability": CAPABILITY_NETWORK_INTERFACE_SET_MTU,
        "reversal_status": str(write.status),
        "max_window_s": MAX_GUARD_WINDOW_S,
        "min_window_s": MIN_GUARD_WINDOW_S,
    }
    if write.status is CapabilityStatus.UNAVAILABLE:
        return _capability(
            definition,
            CapabilityStatus.UNAVAILABLE,
            reason="reversal_capability_unavailable",
            detail={**detail, "reversal_reason": write.reason},
        )
    if write.status is CapabilityStatus.DEGRADED:
        return _capability(
            definition,
            CapabilityStatus.DEGRADED,
            reason="reversal_capability_degraded",
            detail={**detail, "reversal_reason": write.reason},
        )
    return _capability(definition, CapabilityStatus.AVAILABLE, detail=detail)


def _probe_network_interface_set_mtu(
    helper_client: Any | None = None, helper_socket: str | Path | None = None
) -> DiscoveredCapability:
    """Ask the privileged helper what it is. The one probe that must not do its own job.

    Four outcomes, and each says something different about what LocalPlane may claim:

    * the helper answers, runs as root and declares the mutating method — ``available``;
    * the helper answers and **cannot configure links** — ``degraded``. It is reachable and
      it is speaking, and the kernel would refuse its writes. Reporting that as available
      would let a plan be published as executable that cannot execute; reporting it as
      unavailable would lose the fact that the path exists and exactly one thing is wrong
      with it. The helper answers this from the capability it actually holds rather than
      from its uid, because a process can hold ``CAP_NET_ADMIN`` without being root and be
      root without holding it;
    * the helper answers and does **not** declare the mutating method, or speaks a protocol
      version this agent does not — ``unavailable``. A helper that does not agree about
      what the method is must not be sent one;
    * the helper cannot be reached at all — ``unavailable``, with the transport's own
      reason.

    Nothing is written on any of these paths, and there is no branch on which a write is
    attempted to find out whether writing works.
    """
    definition = CAPABILITIES[CAPABILITY_NETWORK_INTERFACE_SET_MTU]
    socket_path = (
        Path(helper_socket) if helper_socket is not None else default_helper_socket_path()
    )
    client = helper_client if helper_client is not None else HelperClient(socket_path)
    detail: dict[str, Any] = {
        "helper_socket": str(socket_path),
        "helper_protocol_version": HELPER_PROTOCOL_VERSION,
        "probe_method": str(HelperMethod.HELLO),
    }
    try:
        hello = client.hello()
    except HelperError as exc:
        return _capability(
            definition,
            CapabilityStatus.UNAVAILABLE,
            reason=exc.code,
            detail={**detail, "error": exc.message, "helper_detail": exc.detail},
        )

    detail = {
        **detail,
        "helper_instance_id": hello.get("helper_instance_id"),
        "helper_version": hello.get("helper_version"),
        "helper_privilege": hello.get("privilege"),
        "helper_effective_uid": hello.get("effective_uid"),
        "helper_can_configure_network": hello.get("can_configure_network"),
        "helper_methods": hello.get("methods"),
        "helper_mutating_methods": hello.get("mutating_methods"),
        "provider": hello.get("provider"),
        "provider_version": hello.get("provider_version"),
        "method": hello.get("mutate_method"),
        "mtu_range": hello.get("mtu_range"),
    }

    if hello.get("protocol_version") != HELPER_PROTOCOL_VERSION:
        return _capability(
            definition,
            CapabilityStatus.UNAVAILABLE,
            reason="helper_protocol_version_unsupported",
            detail={**detail, "helper_reported": hello.get("protocol_version")},
        )
    if str(HelperMethod.SET_INTERFACE_MTU) not in (hello.get("mutating_methods") or []):
        return _capability(
            definition,
            CapabilityStatus.UNAVAILABLE,
            reason="helper_does_not_declare_the_method",
            detail=detail,
        )
    if not hello.get("can_configure_network"):
        return _capability(
            definition,
            CapabilityStatus.DEGRADED,
            reason="helper_cannot_configure_network",
            detail=detail,
        )
    return _capability(definition, CapabilityStatus.AVAILABLE, detail=detail)


def _capability(
    definition: Any,
    status: CapabilityStatus,
    reason: str | None = None,
    detail: dict[str, Any] | None = None,
) -> DiscoveredCapability:
    return DiscoveredCapability(
        capability=definition.capability,
        version=definition.version,
        status=status,
        mutating=definition.mutating,
        summary=definition.summary,
        reason=reason,
        detail=detail or {},
        discovered_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
    )


def _probe_docker(
    docker_socket: str | Path, provider: Any | None = None
) -> tuple[DiscoveredCapability, ...]:
    """Establish what this agent's Docker access actually is — reading, acting, or neither.

    Two capabilities and two probes, because the two can differ. In the ordinary case they
    are the same socket and the answer is the same for both; a daemon published through a
    read-only proxy answers the read and refuses the lifecycle verb, and an agent that
    reported one capability for both would let LocalPlane publish executable plans it cannot
    execute.

    **A missing socket is ``unavailable``, not an error.** A host with no Docker has no
    containers, and the honest report is that this capability cannot be used here — the
    whole agent goes on working, which is the property the provider seam exists for.

    The lifecycle probe is the one place in LocalPlane that sends a mutating verb to find
    something out, and what it addresses is an id no container can have. See
    :meth:`~localplane.agent.providers.docker.DockerProvider.probe_lifecycle` for why the
    alternative — declaring a write capability nobody checked — is worse.
    """
    socket_path = Path(docker_socket)
    client = provider if provider is not None else DockerProvider(socket_path=socket_path)
    observe = CAPABILITIES[CAPABILITY_DOCKER_CONTAINERS_OBSERVE]
    lifecycle = CAPABILITIES[CAPABILITY_DOCKER_CONTAINER_LIFECYCLE]
    detail: dict[str, Any] = {"socket": str(socket_path), "method": "unix_socket_http"}

    if not socket_path.exists():
        return (
            _capability(observe, CapabilityStatus.UNAVAILABLE, "docker_socket_absent", detail),
            _capability(lifecycle, CapabilityStatus.UNAVAILABLE, "docker_socket_absent", detail),
        )

    try:
        version = client.probe_read()
    except DockerFailure as failure:
        unreadable = {**detail, **failure.detail, "status": str(failure.status)}
        return (
            _capability(observe, CapabilityStatus.UNAVAILABLE, failure.reason, unreadable),
            # Not "unknown": a daemon that will not answer a read will not be asked to act.
            _capability(
                lifecycle, CapabilityStatus.UNAVAILABLE, "docker_not_readable", unreadable
            ),
        )

    detail = {**detail, **version}
    read = _capability(observe, CapabilityStatus.AVAILABLE, detail=detail)

    try:
        probe = client.probe_lifecycle()
    except DockerFailure as failure:
        return read, _capability(
            lifecycle,
            CapabilityStatus.UNAVAILABLE,
            failure.reason,
            {**detail, **failure.detail, "http_status": failure.http_status},
        )

    status_code = probe.get("probe_http_status")
    probed = {**detail, **{k: v for k, v in probe.items() if k not in version}}
    if status_code == 404:
        # The daemon routed a lifecycle request from this agent and answered that the
        # container does not exist. That is the whole proof: authorised, and nothing done.
        return read, _capability(lifecycle, CapabilityStatus.AVAILABLE, detail=probed)
    if status_code in (401, 403, 405):
        return read, _capability(
            lifecycle, CapabilityStatus.UNAVAILABLE, "lifecycle_requests_refused", probed
        )
    return read, _capability(
        lifecycle, CapabilityStatus.UNAVAILABLE, "lifecycle_probe_inconclusive", probed
    )


def _probe_systemd(provider: Any) -> DiscoveredCapability:
    """Read the Manager interface; never infer availability from a distro or process name."""
    definition = CAPABILITIES[CAPABILITY_SYSTEMD_UNITS_OBSERVE]
    manager = provider.read_manager()
    detail = {
        "provider": "systemd",
        "provider_version": manager.version,
        "manager_scope": "system",
        "transport": "system_bus_dbus",
        "probe": "manager_properties",
        "facts": manager.facts,
        "gaps": list(manager.gaps),
        **manager.detail,
    }
    if manager.status == "unavailable":
        return _capability(
            definition,
            CapabilityStatus.UNAVAILABLE,
            reason=manager.reason or "system_manager_unavailable",
            detail=detail,
        )
    if manager.status == "degraded":
        return _capability(
            definition,
            CapabilityStatus.DEGRADED,
            reason=manager.reason or "manager_properties_incomplete",
            detail=detail,
        )
    return _capability(definition, CapabilityStatus.AVAILABLE, detail=detail)


def _probe_systemd_lifecycle(
    provider: Any,
    socket_diag: Any,
    runtime_attestation_reader: Any | None = None,
) -> tuple[DiscoveredCapability, DiscoveredCapability]:
    """Probe typed lifecycle mechanics and the independent read-only context seam.

    Introspection is passive.  In particular this does not call a lifecycle method with a
    fake unit as an authorization probe — Part A contains no such call at all.
    """
    lifecycle_definition = CAPABILITIES[CAPABILITY_SYSTEMD_SERVICE_LIFECYCLE]
    context_definition = CAPABILITIES[
        CAPABILITY_SYSTEMD_SERVICE_LIFECYCLE_CONTEXT_OBSERVE
    ]
    try:
        contract = provider.read_lifecycle_contract()
    except Exception as exc:  # Capability discovery must not take the agent down.
        lifecycle = _capability(
            lifecycle_definition,
            CapabilityStatus.UNAVAILABLE,
            reason="systemd_lifecycle_introspection_failed",
            detail={
                "provider": "systemd",
                "probe": "dbus_introspection",
                "mutation_invoked": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        contract = None
    if contract is None:
        lifecycle_detail: dict[str, Any] = {}
    else:
        lifecycle_detail = {
            "provider": "systemd",
            "manager_scope": "system",
            "transport": "system_bus_dbus",
            "probe": "dbus_introspection",
            "manager_methods": list(contract.manager_methods),
            "unit_methods": list(contract.unit_methods),
            "manager_signals": list(contract.manager_signals),
            "introspection_unit": contract.introspection_unit,
            "missing": list(contract.missing),
            "authorization_decision_point": "dispatch",
            "authorization_preflight": "not_preflighted",
            "mutation_invoked": False,
            **contract.detail,
        }
        lifecycle = _capability(
            lifecycle_definition,
            (
                CapabilityStatus.AVAILABLE
                if contract.status == "available"
                else CapabilityStatus.UNAVAILABLE
            ),
            reason=contract.reason,
            detail=lifecycle_detail,
        )

    diag = socket_diag.probe()
    manager = provider.read_manager()
    try:
        peer_pidfd = probe_peer_pidfd_support()
    except Exception as exc:
        peer_pidfd = {
            "status": "unavailable",
            "reason": "so_peerpidfd_probe_failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    try:
        pidfd_contract = provider.read_pidfd_unit_contract().as_dict()
    except Exception as exc:
        pidfd_contract = {
            "status": "unavailable",
            "reason": "systemd_pidfd_introspection_failed",
            "detail": {"error_type": type(exc).__name__, "error": str(exc)},
        }
    reader = runtime_attestation_reader or DockerDirectAttestationReader()
    try:
        attestation_read = reader.read()
        attestation = {
            "status": attestation_read.status,
            "reason": attestation_read.reason,
            "contract": (
                attestation_read.attestation.contract
                if attestation_read.attestation is not None
                else "docker-direct-unix-v1"
            ),
            "fingerprint": (
                attestation_read.attestation.fingerprint
                if attestation_read.attestation is not None
                else None
            ),
        }
    except Exception as exc:
        attestation = {
            "status": "unavailable",
            "reason": "attestation_probe_failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    context_detail = {
        "provider": "systemd+linux_socket_diag",
        "manager_scope": "system",
        "socket_diag": diag,
        "manager_status": manager.status,
        "runtime_owner": {
            "contract": "docker-direct-unix-v1",
            "implementation": "supported",
            "kernel_peer_pidfd": peer_pidfd,
            "systemd_pidfd_containment": pidfd_contract,
            "trusted_attestation": attestation,
            "observation_required_for_completeness": True,
        },
        "mutation_invoked": False,
    }
    if manager.status == "unavailable":
        context = _capability(
            context_definition,
            CapabilityStatus.UNAVAILABLE,
            reason=manager.reason or "system_manager_unavailable",
            detail=context_detail,
        )
    elif diag.get("status") != "available":
        context = _capability(
            context_definition,
            CapabilityStatus.UNAVAILABLE,
            reason=str(diag.get("reason") or "socket_diag_unavailable"),
            detail=context_detail,
        )
    else:
        context = _capability(
            context_definition, CapabilityStatus.AVAILABLE, detail=context_detail
        )
    return lifecycle, context
