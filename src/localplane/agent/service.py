"""Agent operation dispatch, with no transport in it.

Everything the agent can be asked to do is in the handler table below. The table is the
contract: a method that is not in it does not exist, and adding one is a visible change to
this file rather than a consequence of a caller sending something new.

**Exactly four are classified as mutating**, and ``mutating_methods`` names all four
because it is true.
``network.set_interface_mtu`` forwards a typed mutation to the privileged helper, which
sends one netlink message. ``docker.container_lifecycle`` asks the Docker daemon to run one
of three declared verbs against one container, over the daemon's own API.
``systemd.service_lifecycle`` asks the system manager to run one of the same three verbs
against one already-loaded service unit, over systemd's own D-Bus interface.
``network.arm_mtu_guard`` writes nothing and can cause the first one to happen later, with
no further request, if a deadline passes — which is why the protocol counts it among the
mutating methods and why it is here at all: the guard has to be held by something that
outlives the backend and the operator's connection, and on this host that is this process.

**The agent makes no product decision on any path.** Whether this object may be changed,
whether an operator confirmed it, whether it carries the management path, whether a
checkpoint exists, whether carrying it out might interrupt LocalPlane itself and what the
desired end state is were all settled by the backend before the request was built. What the
agent does is forward a typed, already-authorised request and report the typed outcome back
without softening it.

**The write paths hold different privilege and the agent says so.** The MTU path holds none
here: the helper is a separate process behind a peer-credential check, and this file's only
relationship to the kernel is that it does not have one. The Docker path is different and
is not dressed up as anything else — write access to the Docker socket is effectively root
on this host, this process has whatever access the socket gives it, and the capability
probe establishes what that is rather than assuming it. The systemd path holds no privilege
of its own at all: it is this unprivileged process's own bus connection, what it may do is
whatever the administrator's PolicyKit configuration allows at the moment it asks, and
nothing here preflights, parses or predicts that decision. There is no ``systemctl``, no
shell, no ``sudo`` and no subprocess on it, and the privileged helper does not speak systemd.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from localplane import __version__
from localplane.agent.capabilities import DiscoveredCapability, discover_capabilities
from localplane.agent.guard import GuardRefusal, GuardRegistry
from localplane.agent.identity import HostIdentity, HostIdentityUnavailable, identify_host
from localplane.agent.docker_direct_attestation import DockerDirectAttestationReader
from localplane.agent.docker_runtime_owner import DockerRuntimeOwnerCorrelator
from localplane.agent.providers.base import CommandRunner
from localplane.agent.providers.collector import NetworkProviderEvidenceCollector
from localplane.agent.providers.docker import (
    DEFAULT_DOCKER_SOCKET,
    DEFAULT_LOG_LINES,
    DockerFailure,
    DockerProvider,
    InvalidContainerId,
)
from localplane.agent.providers.linux_network import (
    DEFAULT_SYSFS_NET,
    InvalidInterfaceName,
    LinuxNetworkProvider,
)
from localplane.agent.providers.linux_route import (
    InvalidRouteDestination,
    LinuxRouteProvider,
    RouteQuery,
)
from localplane.agent.providers.linux_socket_diag import LinuxSocketDiag
from localplane.agent.providers.systemd import (
    SystemdInvalidUnitName,
    SystemdProvider,
    validate_service_unit_name,
)
from localplane.agent.systemd_lifecycle_context import SystemdLifecycleContextReader
from localplane.helper.client import HelperClient, HelperError
from localplane.helper.protocol import HelperErrorCode, MutationOutcome
from localplane.helper.server import default_helper_socket_path
from localplane.protocol.capabilities import (
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
from localplane.protocol.providers import PROVIDER_SYSTEMD
from localplane.protocol.wire import (
    DOCKER_LIFECYCLE_ACTIONS,
    METHODS,
    MUTATING_METHODS,
    SYSTEMD_SERVICE_LIFECYCLE_ACTIONS,
    SYSTEMD_SERVICE_LIFECYCLE_UNIT_SUFFIX,
    ErrorCode,
    Method,
    ProtocolError,
)

TRANSPORT_AF_UNIX = "af_unix"


class AgentService:
    """Answers protocol methods. Constructed once per agent process."""

    def __init__(
        self,
        root: str | Path = "/",
        sysfs_net: str | Path = DEFAULT_SYSFS_NET,
        runner: CommandRunner | None = None,
        transport: str = TRANSPORT_AF_UNIX,
        docker_socket: str | Path = DEFAULT_DOCKER_SOCKET,
        route_query: RouteQuery | None = None,
        helper_socket: str | Path | None = None,
        helper_client: Any | None = None,
        docker_provider: Any | None = None,
        systemd_provider: Any | None = None,
        socket_diag_provider: Any | None = None,
        runtime_attestation_reader: Any | None = None,
        runtime_owner_correlator: Any | None = None,
        guard_timer: Callable[[float, Callable[[], None]], Any] | None = None,
        guard_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._root = Path(root)
        self._sysfs_net = Path(sysfs_net)
        self._runner = runner
        self._transport = transport
        self._docker_socket = Path(docker_socket)
        self._route_query = route_query
        self._helper_socket = (
            Path(helper_socket) if helper_socket is not None else default_helper_socket_path()
        )
        # A seam of the same shape as the command runner and the netlink query: the client
        # is injectable so that a helper that refuses, dies mid-request or answers with an
        # unknown outcome is reachable in a test, while the framing, the dispatch and the
        # outcome handling above it stay real.
        self._helper = (
            helper_client if helper_client is not None else HelperClient(self._helper_socket)
        )
        # One Docker boundary for the whole agent: the same object answers the ownership
        # evidence read, the container observation, logs, stats and the lifecycle verb.
        # Injectable for the same reason the command runner and the netlink query are — a
        # daemon that refuses, disappears mid-request or answers with something unreadable
        # has to be reachable in a test while everything above it stays real.
        self._docker = (
            docker_provider
            if docker_provider is not None
            else DockerProvider(socket_path=self._docker_socket)
        )
        # The official system-manager D-Bus boundary.  It opens a fresh connection for
        # each provider operation and never carries an object path across them.  Pointing
        # the runtime marker at the selected root also keeps alternate-root observations
        # from claiming that the host manager belongs to that filesystem.
        self._systemd = (
            systemd_provider
            if systemd_provider is not None
            else SystemdProvider(runtime_path=self._root / "run/systemd/system")
        )
        self._socket_diag = socket_diag_provider or LinuxSocketDiag()
        self._runtime_attestation = (
            runtime_attestation_reader
            if runtime_attestation_reader is not None
            # Runtime-owner authority is deliberately outside LOCALPLANE_ROOT and every
            # other environment-selected observation root.  Production always reads the
            # one fixed host deployment path; tests may inject a closed reader explicitly.
            else DockerDirectAttestationReader()
        )
        self._runtime_owner = (
            runtime_owner_correlator
            if runtime_owner_correlator is not None
            else DockerRuntimeOwnerCorrelator(
                docker=self._docker,
                systemd=self._systemd,
                attestation_reader=self._runtime_attestation,
            )
        )
        self._systemd_lifecycle_context = SystemdLifecycleContextReader(
            self._systemd, self._socket_diag, self._runtime_owner
        )
        self._instance_id = f"agent_{uuid.uuid4().hex}"
        self._started_at = _now()
        self._provider = LinuxNetworkProvider(sysfs_net=self._sysfs_net, runner=runner)
        self._providers = NetworkProviderEvidenceCollector(
            runner=runner, docker_socket=self._docker_socket
        )
        self._routes = LinuxRouteProvider(query=route_query)
        self._capabilities: tuple[DiscoveredCapability, ...] = self._discover()
        # The host side of the connection guard. It is given this service's own typed
        # MTU write and nothing else, so a guard can dispatch exactly the mutation the
        # agent could already be asked for and there is no second path to the helper.
        # The timer is a seam of the same shape as the command runner, the netlink
        # datagram exchange and the helper client: a test can fire a guard's deadline
        # deterministically while everything above it — the registry, the reversal's
        # parameters, the privileged path it goes through and the outcome it reports — stays
        # real. What is scripted is the passage of time and nothing else.
        self._guards = GuardRegistry(
            self._instance_id,
            self._set_interface_mtu,
            clock=guard_clock,
            timer_factory=guard_timer,
        )

        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            Method.AGENT_HELLO: self._agent_hello,
            Method.HOST_IDENTIFY: self._host_identify,
            Method.CAPABILITIES_LIST: self._capabilities_list,
            Method.NETWORK_OBSERVE_INTERFACES: self._network_observe_interfaces,
            Method.NETWORK_OBSERVE_PROVIDERS: self._network_observe_providers,
            Method.NETWORK_OBSERVE_ROUTE: self._network_observe_route,
            Method.NETWORK_SET_INTERFACE_MTU: self._network_set_interface_mtu,
            Method.NETWORK_ARM_MTU_GUARD: self._network_arm_mtu_guard,
            Method.NETWORK_DISARM_MTU_GUARD: self._network_disarm_mtu_guard,
            Method.NETWORK_MTU_GUARD_STATUS: self._network_mtu_guard_status,
            Method.DOCKER_OBSERVE_CONTAINERS: self._docker_observe_containers,
            Method.DOCKER_CONTAINER_LOGS: self._docker_container_logs,
            Method.DOCKER_CONTAINER_STATS: self._docker_container_stats,
            Method.DOCKER_CONTAINER_LIFECYCLE: self._docker_container_lifecycle,
            Method.SYSTEMD_OBSERVE_UNITS: self._systemd_observe_units,
            Method.SYSTEMD_OBSERVE_UNIT: self._systemd_observe_unit,
            Method.SYSTEMD_RESOLVE_AGENT_UNIT: self._systemd_resolve_agent_unit,
            Method.SYSTEMD_OBSERVE_LIFECYCLE_CONTEXT: (
                self._systemd_observe_lifecycle_context
            ),
            Method.SYSTEMD_SERVICE_LIFECYCLE: self._systemd_service_lifecycle,
        }
        missing = METHODS - set(self._handlers)
        if missing:
            raise RuntimeError(f"protocol methods without a handler: {sorted(missing)}")

    # ------------------------------------------------------------------------ dispatch

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def capabilities(self) -> tuple[DiscoveredCapability, ...]:
        return self._capabilities

    def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        handler = self._handlers.get(method)
        if handler is None:
            raise ProtocolError(
                ErrorCode.UNSUPPORTED_METHOD,
                f"unsupported method: {method!r}",
                {"method": method, "supported": sorted(METHODS)},
            )
        return handler(params)

    # ------------------------------------------------------------------------- methods

    def _agent_hello(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_no_params(params, Method.AGENT_HELLO)
        return {
            "agent": self._agent_facts(),
            "host": self._identity().as_dict(),
            "capabilities": [c.as_dict() for c in self._capabilities],
        }

    def _host_identify(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_no_params(params, Method.HOST_IDENTIFY)
        self._require_capability(CAPABILITY_HOST_OBSERVE)
        return {"host": self._identity().as_dict()}

    def _capabilities_list(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_no_params(params, Method.CAPABILITIES_LIST)
        # Re-probed rather than replayed: a capability that became available since start
        # should be reported as available, and one that disappeared should not be claimed.
        self._capabilities = self._discover()
        return {"capabilities": [c.as_dict() for c in self._capabilities]}

    def _network_observe_interfaces(self, params: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown_params(params, {"names"}, Method.NETWORK_OBSERVE_INTERFACES)
        self._require_capability(CAPABILITY_NETWORK_OBSERVE)

        names = params.get("names")
        if names is not None and not isinstance(names, list):
            raise ProtocolError(ErrorCode.INVALID_PARAMS, "names must be a list of strings")
        try:
            batch = self._provider.observe_interfaces(names)
        except InvalidInterfaceName as exc:
            raise ProtocolError(ErrorCode.INVALID_PARAMS, str(exc)) from exc
        except OSError as exc:
            raise ProtocolError(
                ErrorCode.PROVIDER_ERROR,
                f"network provider failed: {exc.strerror or exc}",
                {"provider": self._provider.name, "errno": exc.errno},
            ) from exc

        return {
            "agent_instance_id": self._instance_id,
            "host_id": self._identity().host_id,
            "observation": batch.as_dict(),
        }

    def _network_observe_providers(self, params: dict[str, Any]) -> dict[str, Any]:
        """Ask every provider what it says it owns. Takes no parameters, and could not.

        Deliberately a separate operation from observing interfaces. The providers are
        third-party daemons that can be slow, unreachable or refuse a socket; keeping them
        off the interface path means none of that can cost LocalPlane its view of the
        host's links. What comes back is what each provider said, with no attempt to
        decide which interface any of it refers to — that correlation is a judgement, and
        judgements are the backend's.
        """
        _require_no_params(params, Method.NETWORK_OBSERVE_PROVIDERS)
        self._require_capability(CAPABILITY_NETWORK_PROVIDERS_OBSERVE)
        batch = self._providers.collect()
        return {
            "agent_instance_id": self._instance_id,
            "host_id": self._identity().host_id,
            "providers": batch.as_dict(),
        }

    def _network_observe_route(self, params: dict[str, Any]) -> dict[str, Any]:
        """Ask the kernel which route it would use to reach one address.

        The one parameter is a destination address, and it is the only place in this agent
        where a value chosen by the caller is used for anything at all. It reaches no
        command line: it is parsed by :mod:`ipaddress` and sent to the kernel in its packed
        binary form inside an ``RTM_GETROUTE`` message. There is no argv, no shell, no
        executable name and no second message type to ask for.

        What comes back is what the kernel said, including an interface *index*. This
        method does not resolve that index to a name, to an object or to a policy: whether
        the interface behind it carries the operator's management path is a judgement, and
        judgements belong to the backend.
        """
        _reject_unknown_params(params, {"destination"}, Method.NETWORK_OBSERVE_ROUTE)
        self._require_capability(CAPABILITY_NETWORK_ROUTE_OBSERVE)

        destination = params.get("destination")
        if not isinstance(destination, str):
            raise ProtocolError(
                ErrorCode.INVALID_PARAMS, "destination must be a string holding one IP address"
            )
        try:
            observation = self._routes.observe_route(destination)
        except InvalidRouteDestination as exc:
            raise ProtocolError(ErrorCode.INVALID_PARAMS, str(exc)) from exc

        return {
            "agent_instance_id": self._instance_id,
            "host_id": self._identity().host_id,
            "route": observation.as_dict(),
        }

    def _network_set_interface_mtu(self, params: dict[str, Any]) -> dict[str, Any]:
        """Forward one typed, already-authorised MTU mutation to the privileged helper.

        **The agent decides nothing here.** It does not read the intent, does not consult
        an observation, does not know what the management path is and does not know whether
        anybody confirmed anything. Those were decided by the backend, against records this
        process cannot see, before this request existed. What the agent contributes is the
        privilege boundary: it holds none itself, and it hands the request to the process
        that does over a socket that checks who is on the other end.

        **The outcome is typed and never softened.** Three answers come out —
        ``not_written``, ``written``, ``write_unknown`` — and the third is preserved
        exactly. If the helper transport disappears after the request was written to the
        socket, the write may already have reached the kernel, and this method says so
        rather than reporting a failure. Only a failure that provably happened *before* the
        request left is ``not_written``.

        Every parameter is a typed scalar named by the protocol. There is no command, no
        argv, no executable, no shell, no provider, no message type and no interface name
        used as an identity — the identity is the kernel's interface index, and the name is
        carried, when the backend has one, only as a second guard the helper checks against
        the kernel itself.
        """
        _reject_unknown_params(
            params,
            {
                "attempt_id",
                "ifindex",
                "expected_current_mtu",
                "desired_mtu",
                "expected_interface_name",
            },
            Method.NETWORK_SET_INTERFACE_MTU,
        )
        self._require_capability(CAPABILITY_NETWORK_INTERFACE_SET_MTU)

        attempt_id = _require_str(params.get("attempt_id"), "attempt_id")
        ifindex = _require_int(params.get("ifindex"), "ifindex")
        expected_current_mtu = _require_int(
            params.get("expected_current_mtu"), "expected_current_mtu"
        )
        desired_mtu = _require_int(params.get("desired_mtu"), "desired_mtu")
        expected_interface_name = params.get("expected_interface_name")
        if expected_interface_name is not None and not isinstance(expected_interface_name, str):
            raise ProtocolError(
                ErrorCode.INVALID_PARAMS, "expected_interface_name must be a string"
            )

        return {
            "agent_instance_id": self._instance_id,
            "host_id": self._identity().host_id,
            "mutation": self._set_interface_mtu(
                attempt_id=attempt_id,
                ifindex=ifindex,
                expected_current_mtu=expected_current_mtu,
                desired_mtu=desired_mtu,
                expected_interface_name=expected_interface_name,
            ),
        }

    def _set_interface_mtu(
        self,
        *,
        attempt_id: str,
        ifindex: int,
        expected_current_mtu: int,
        desired_mtu: int,
        expected_interface_name: str | None,
    ) -> dict[str, Any]:
        """The mutation itself, and the one place a helper failure becomes an outcome.

        Extracted from the handler above because a connection guard's reversal is *this*
        write, dispatched later. Two mappings from a transport failure to a three-valued
        outcome would eventually disagree about whether a guard that lost the helper mid-
        request had written, and that disagreement is the exact thing this vocabulary
        exists to prevent.
        """
        try:
            return self._helper.set_interface_mtu(
                attempt_id=attempt_id,
                ifindex=ifindex,
                expected_current_mtu=expected_current_mtu,
                desired_mtu=desired_mtu,
                expected_interface_name=expected_interface_name,
            )
        except HelperError as exc:
            # The one branch in this agent where a transport failure is not simply a
            # failure. `dispatched` says whether the request bytes had already been
            # written to the helper socket, and that is the whole difference between a
            # provable negative and an open question.
            outcome = (
                MutationOutcome.WRITE_UNKNOWN if exc.dispatched else MutationOutcome.NOT_WRITTEN
            )
            return {
                "outcome": str(outcome),
                "reason": exc.code,
                "attempt_id": attempt_id,
                "ifindex": ifindex,
                "expected_current_mtu": expected_current_mtu,
                "desired_mtu": desired_mtu,
                "provider": None,
                "provider_version": None,
                "method": str(Method.NETWORK_SET_INTERFACE_MTU),
                "observed_name": None,
                "observed_mtu": None,
                "kernel_errno": None,
                "kernel_error": None,
                "detail": {
                    "helper_error": exc.as_dict(),
                    "helper_socket": str(self._helper_socket),
                    "dispatched": exc.dispatched,
                },
            }

    # ------------------------------------------------------------------ connection guard

    def _network_arm_mtu_guard(self, params: dict[str, Any]) -> dict[str, Any]:
        """Hold a reversal for one interface until a deadline, and say so.

        **Arming writes nothing and can cause a write**, which is why the protocol counts
        this among the mutating methods. What the caller supplies is exactly the reversal it
        wants held: the link's kernel index, the kernel's own name for it as the same second
        identity guard the write uses, the value the change will leave behind, the value to
        put back, the attempt id that reversal will carry, and how long to hold it. There is
        no command, no argv, no script, no probe target, no address, no route and no
        expression — a guard can dispatch the typed MTU write this agent already performs
        and nothing else, and it cannot compute its own parameters for it.

        The agent decides nothing about whether the guard *should* exist. Whether the target
        carries the operator's path, whether an operator typed a confirmation, whether a
        checkpoint is on disk and what the value to restore is were all settled by the
        backend against records this process cannot see.
        """
        _reject_unknown_params(
            params,
            {
                "guard_id",
                "attempt_id",
                "ifindex",
                "expected_interface_name",
                "guarded_mtu",
                "restore_mtu",
                "window_s",
            },
            Method.NETWORK_ARM_MTU_GUARD,
        )
        self._require_capability(CAPABILITY_NETWORK_INTERFACE_MTU_GUARD)

        expected_interface_name = params.get("expected_interface_name")
        if expected_interface_name is not None and not isinstance(expected_interface_name, str):
            raise ProtocolError(
                ErrorCode.INVALID_PARAMS, "expected_interface_name must be a string"
            )
        try:
            armed = self._guards.arm(
                guard_id=_require_str(params.get("guard_id"), "guard_id"),
                attempt_id=_require_str(params.get("attempt_id"), "attempt_id"),
                ifindex=_require_int(params.get("ifindex"), "ifindex"),
                expected_interface_name=expected_interface_name,
                guarded_mtu=_require_int(params.get("guarded_mtu"), "guarded_mtu"),
                restore_mtu=_require_int(params.get("restore_mtu"), "restore_mtu"),
                window_s=_require_int(params.get("window_s"), "window_s"),
            )
        except GuardRefusal as refusal:
            raise ProtocolError(
                ErrorCode.INVALID_PARAMS, refusal.code, refusal.detail
            ) from None
        return {
            "agent_instance_id": self._instance_id,
            "host_id": self._identity().host_id,
            "guard": armed,
        }

    def _network_disarm_mtu_guard(self, params: dict[str, Any]) -> dict[str, Any]:
        """Release a guard that is still holding, and report what became of it.

        **This method can only prevent a write.** It is not among the mutating methods for
        that reason, and it needs no capability: a host whose guard capability has gone
        away since a guard was armed must still be able to be asked to release it, and
        refusing here would leave a reversal running that nobody could stop.

        Releasing a guard that has already fired is not an error. The answer is what it did.
        """
        _reject_unknown_params(params, {"guard_id"}, Method.NETWORK_DISARM_MTU_GUARD)
        guard_id = _require_str(params.get("guard_id"), "guard_id")
        return {
            "agent_instance_id": self._instance_id,
            "host_id": self._identity().host_id,
            "guard": self._guards.disarm(guard_id),
        }

    def _network_mtu_guard_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """What a guard is doing. Reads, and never releases anything.

        Separate from disarming on purpose: a backend coming back from a restart has to
        find out where a guard stands *without* cancelling protection that is still
        running, and one method that sometimes cancelled would be one call away from doing
        exactly that.
        """
        _reject_unknown_params(params, {"guard_id"}, Method.NETWORK_MTU_GUARD_STATUS)
        guard_id = _require_str(params.get("guard_id"), "guard_id")
        return {
            "agent_instance_id": self._instance_id,
            "host_id": self._identity().host_id,
            "guard": self._guards.status(guard_id),
        }

    # -------------------------------------------------------------------------- docker

    def _docker_observe_containers(self, params: dict[str, Any]) -> dict[str, Any]:
        """Ask the Docker daemon about every container on this host. Takes no parameters.

        A separate operation from observing interfaces, for the reason every provider read
        is separate: a daemon that is slow, unreachable or refusing its socket must not cost
        LocalPlane its view of the host's links. It reports what Docker said and decides
        nothing — whether a container is healthy, whether LocalPlane may act on it, and what
        its state amounts to are judgements, and judgements belong to the backend.
        """
        _require_no_params(params, Method.DOCKER_OBSERVE_CONTAINERS)
        self._require_capability(CAPABILITY_DOCKER_CONTAINERS_OBSERVE)
        batch = self._docker.observe_containers()
        return {
            "agent_instance_id": self._instance_id,
            "host_id": self._identity().host_id,
            "containers": batch.as_dict(),
        }

    def _docker_container_logs(self, params: dict[str, Any]) -> dict[str, Any]:
        """The most recent lines one container wrote. Bounded, and never followed.

        Two parameters and both are typed scalars: a container id, which is checked against
        Docker's own id syntax before it can reach a URL, and a line count, which is clamped
        to a module ceiling. There is no ``since``, no ``follow``, no stream and no way to
        ask for a different endpoint.
        """
        _reject_unknown_params(params, {"container_id", "tail"}, Method.DOCKER_CONTAINER_LOGS)
        self._require_capability(CAPABILITY_DOCKER_CONTAINERS_OBSERVE)
        container_id = _require_str(params.get("container_id"), "container_id")
        tail = params.get("tail", DEFAULT_LOG_LINES)
        tail = _require_int(tail, "tail")
        if tail < 1:
            raise ProtocolError(ErrorCode.INVALID_PARAMS, "tail must be a positive integer")
        try:
            logs = self._docker.container_logs(container_id, tail=tail)
        except InvalidContainerId as exc:
            raise ProtocolError(ErrorCode.INVALID_PARAMS, str(exc)) from exc
        except DockerFailure as failure:
            raise _docker_error(failure) from None
        return {
            "agent_instance_id": self._instance_id,
            "host_id": self._identity().host_id,
            "logs": logs,
        }

    def _docker_container_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """One current sample of what a container is using. A snapshot, never a series."""
        _reject_unknown_params(params, {"container_id"}, Method.DOCKER_CONTAINER_STATS)
        self._require_capability(CAPABILITY_DOCKER_CONTAINERS_OBSERVE)
        container_id = _require_str(params.get("container_id"), "container_id")
        try:
            stats = self._docker.container_stats(container_id)
        except InvalidContainerId as exc:
            raise ProtocolError(ErrorCode.INVALID_PARAMS, str(exc)) from exc
        except DockerFailure as failure:
            raise _docker_error(failure) from None
        return {
            "agent_instance_id": self._instance_id,
            "host_id": self._identity().host_id,
            "stats": stats,
        }

    def _docker_container_lifecycle(self, params: dict[str, Any]) -> dict[str, Any]:
        """Run one declared lifecycle verb against one container. The agent's second write.

        **The agent decides nothing here.** Whether this container may be acted on, whether
        an operator confirmed it, what the expected end state is and whether the plan was
        still current were all settled by the backend, against records this process cannot
        see, before the request existed.

        **The verb is a member of a closed set and the target is an id.** ``action`` is
        checked against the protocol's three-member tuple, and the container id is checked
        against Docker's own syntax before it can reach a URL. There is no path, no query,
        no body, no command, no argv, no shell and no timeout parameter — the daemon's grace
        period is a module constant, because a caller-supplied one would be a value
        travelling from an API request into a host mutation, and the Run carries no values.

        **The outcome is typed and never softened.** Three answers come out —
        ``not_written``, ``written``, ``write_unknown`` — and the third is preserved exactly.
        A daemon that reports it began the operation and failed has not proven that nothing
        happened, and this method says so rather than reporting a clean failure.
        """
        _reject_unknown_params(
            params,
            {"attempt_id", "container_id", "action"},
            Method.DOCKER_CONTAINER_LIFECYCLE,
        )
        self._require_capability(CAPABILITY_DOCKER_CONTAINER_LIFECYCLE)

        attempt_id = _require_str(params.get("attempt_id"), "attempt_id")
        container_id = _require_str(params.get("container_id"), "container_id")
        action = _require_str(params.get("action"), "action")
        if action not in DOCKER_LIFECYCLE_ACTIONS:
            raise ProtocolError(
                ErrorCode.INVALID_PARAMS,
                f"unsupported lifecycle action: {action!r}",
                {"action": action, "supported": list(DOCKER_LIFECYCLE_ACTIONS)},
            )

        result = self._docker.lifecycle(container_id, action)
        return {
            "agent_instance_id": self._instance_id,
            "host_id": self._identity().host_id,
            "mutation": {
                **result.as_dict(),
                "attempt_id": attempt_id,
                "provider_version": self._docker_engine_version(),
            },
        }

    def _docker_engine_version(self) -> str | None:
        """The engine version this agent's probe recorded, for the mutation's provenance.

        Read from the capability rather than by asking the daemon again: a second round trip
        on the write path would be one more thing that can fail between a confirmation and a
        dispatch, and what is wanted here is the version of the daemon this agent
        established it was talking to.
        """
        for capability in self._capabilities:
            if capability.capability == CAPABILITY_DOCKER_CONTAINER_LIFECYCLE:
                version = capability.detail.get("engine_version")
                return version if isinstance(version, str) else None
        return None

    # -------------------------------------------------------------------------- systemd

    def _systemd_observe_units(self, params: dict[str, Any]) -> dict[str, Any]:
        """Observe the bounded loaded estate.  No parameter can widen its scope."""
        _require_no_params(params, Method.SYSTEMD_OBSERVE_UNITS)
        self._require_capability(CAPABILITY_SYSTEMD_UNITS_OBSERVE)
        return {
            "agent_instance_id": self._instance_id,
            "host_id": self._identity().host_id,
            "units": self._systemd.observe_units().as_dict(),
        }

    def _systemd_observe_unit(self, params: dict[str, Any]) -> dict[str, Any]:
        """Observe one already-loaded unit with Manager.GetUnit, never LoadUnit."""
        _reject_unknown_params(params, {"unit_id"}, Method.SYSTEMD_OBSERVE_UNIT)
        self._require_capability(CAPABILITY_SYSTEMD_UNITS_OBSERVE)
        unit_id = _require_str(params.get("unit_id"), "unit_id")
        try:
            observation = self._systemd.observe_unit(unit_id)
        except SystemdInvalidUnitName as exc:
            raise ProtocolError(ErrorCode.INVALID_PARAMS, str(exc)) from exc
        return {
            "agent_instance_id": self._instance_id,
            "host_id": self._identity().host_id,
            "observation": observation.as_dict(),
        }

    def _systemd_resolve_agent_unit(self, params: dict[str, Any]) -> dict[str, Any]:
        """Resolve this process from /proc/self/cgroup through systemd itself."""
        _require_no_params(params, Method.SYSTEMD_RESOLVE_AGENT_UNIT)
        self._require_capability(CAPABILITY_SYSTEMD_UNITS_OBSERVE)
        return {
            "agent_instance_id": self._instance_id,
            "host_id": self._identity().host_id,
            "resolution": self._systemd.resolve_current_process_unit().as_dict(),
        }

    def _systemd_observe_lifecycle_context(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Read one closed service safety context; no lifecycle method is reachable."""
        _reject_unknown_params(
            params,
            {
                "target_unit_id",
                "action",
                "connection",
                "management_providers",
                "provider_evidence_complete",
            },
            Method.SYSTEMD_OBSERVE_LIFECYCLE_CONTEXT,
        )
        self._require_capability(CAPABILITY_SYSTEMD_SERVICE_LIFECYCLE_CONTEXT_OBSERVE)
        target = _require_str(params.get("target_unit_id"), "target_unit_id")
        action = _require_str(params.get("action"), "action")
        if action not in SYSTEMD_SERVICE_LIFECYCLE_ACTIONS:
            raise ProtocolError(
                ErrorCode.INVALID_PARAMS,
                "action is not a supported systemd service lifecycle action",
                {"supported": list(SYSTEMD_SERVICE_LIFECYCLE_ACTIONS)},
            )
        connection = params.get("connection")
        if not isinstance(connection, dict):
            raise ProtocolError(ErrorCode.INVALID_PARAMS, "connection must be an object")
        _reject_unknown_params(
            connection,
            {
                "family", "peer_ip", "peer_port", "local_ip", "local_port",
                "backend_netns_inode",
            },
            Method.SYSTEMD_OBSERVE_LIFECYCLE_CONTEXT,
        )
        required_connection = {
            "family", "peer_ip", "peer_port", "local_ip", "local_port",
            "backend_netns_inode",
        }
        missing = sorted(required_connection - set(connection))
        if missing:
            raise ProtocolError(
                ErrorCode.INVALID_PARAMS,
                "connection is missing required accepted-socket evidence",
                {"missing": missing},
            )
        providers = params.get("management_providers")
        if not isinstance(providers, list) or any(
            not isinstance(value, str) for value in providers
        ):
            raise ProtocolError(
                ErrorCode.INVALID_PARAMS, "management_providers must be a list of strings"
            )
        complete = params.get("provider_evidence_complete")
        if not isinstance(complete, bool):
            raise ProtocolError(
                ErrorCode.INVALID_PARAMS, "provider_evidence_complete must be a boolean"
            )
        try:
            observation = self._systemd_lifecycle_context.observe(
                target_unit=target,
                action=action,
                connection=connection,
                management_providers=tuple(providers),
                provider_evidence_complete=complete,
            )
        except (SystemdInvalidUnitName, ValueError) as exc:
            raise ProtocolError(ErrorCode.INVALID_PARAMS, str(exc)) from exc
        return {
            "agent_instance_id": self._instance_id,
            "host_id": self._identity().host_id,
            "context": observation.as_dict(),
        }

    def _systemd_service_lifecycle(self, params: dict[str, Any]) -> dict[str, Any]:
        """Run one declared verb against one loaded service unit.

        One of the four methods the protocol classifies as mutating, and the only one
        that speaks to the system manager.

        **The agent decides nothing here.** Whether this unit may be acted on, whether an
        operator confirmed it, whether the plan was still current and whether carrying it
        out might take LocalPlane itself away were all settled by the backend, against
        records this process cannot see, before the request existed.

        **The verb is a member of a closed set and the target is a canonical unit name.**
        ``action`` is checked against the protocol's three-member tuple and ``unit_id``
        against systemd's own name syntax and the ``.service`` suffix, both before a bus
        connection is opened. There is no parameter here for an object path, an interface, a
        member, a signature, a job id, a start mode, a timeout or a property — the mode is a
        provider constant, because a caller-supplied one would let a request cancel jobs
        somebody else queued.

        **No privilege is borrowed.** There is no shell, no ``systemctl``, no ``sudo``, no
        subprocess and no widening of the privileged helper, which does not speak systemd at
        all. This is the unprivileged agent's own D-Bus connection, and what it may do is
        whatever the administrator's PolicyKit configuration allows at the moment it asks.

        **The outcome is typed and never softened.** Three answers come out and the third is
        preserved exactly: a manager that accepted a job and then reported anything other
        than completion has not proven that nothing happened.
        """
        _reject_unknown_params(
            params,
            {"attempt_id", "unit_id", "action"},
            Method.SYSTEMD_SERVICE_LIFECYCLE,
        )
        self._require_capability(CAPABILITY_SYSTEMD_SERVICE_LIFECYCLE)

        attempt_id = _require_str(params.get("attempt_id"), "attempt_id")
        unit_id = _require_str(params.get("unit_id"), "unit_id")
        action = _require_str(params.get("action"), "action")
        if action not in SYSTEMD_SERVICE_LIFECYCLE_ACTIONS:
            raise ProtocolError(
                ErrorCode.INVALID_PARAMS,
                f"unsupported lifecycle action: {action!r}",
                {"action": action, "supported": list(SYSTEMD_SERVICE_LIFECYCLE_ACTIONS)},
            )
        try:
            validate_service_unit_name(unit_id)
        except SystemdInvalidUnitName as exc:
            raise ProtocolError(
                ErrorCode.INVALID_PARAMS,
                str(exc),
                {"unit_id": unit_id,
                 "required_suffix": SYSTEMD_SERVICE_LIFECYCLE_UNIT_SUFFIX},
            ) from exc

        result = self._systemd.dispatch_service_lifecycle(unit_id, action)
        return {
            "agent_instance_id": self._instance_id,
            "host_id": self._identity().host_id,
            "mutation": {
                **result.as_dict(),
                "attempt_id": attempt_id,
                "provider": PROVIDER_SYSTEMD,
                "provider_version": self._systemd_manager_version(),
            },
        }

    def _systemd_manager_version(self) -> str | None:
        """The manager version this agent last read, for the mutation's provenance.

        Best effort and never fatal: a mutation that happened is not made less true by a
        version string that could not be read afterwards.
        """
        try:
            return self._systemd.read_manager().facts.get("version")
        except Exception:  # noqa: BLE001 - provenance detail, never the outcome
            return None

    # ------------------------------------------------------------------------- helpers

    def _discover(self) -> tuple[DiscoveredCapability, ...]:
        return discover_capabilities(
            root=self._root,
            sysfs_net=self._sysfs_net,
            runner=self._runner,
            docker_socket=self._docker_socket,
            route_query=self._route_query,
            helper_client=self._helper,
            helper_socket=self._helper_socket,
            docker_provider=self._docker,
            systemd_provider=self._systemd,
            socket_diag_provider=self._socket_diag,
            runtime_attestation_reader=self._runtime_attestation,
        )

    def _identity(self) -> HostIdentity:
        try:
            return identify_host(self._root)
        except HostIdentityUnavailable as exc:
            raise ProtocolError(
                ErrorCode.PROVIDER_ERROR,
                f"host identity unavailable: {exc}",
                {"root": str(self._root)},
            ) from exc

    def _agent_facts(self) -> dict[str, Any]:
        """What this agent is, stated without flattery.

        ``privilege`` and ``process_isolated`` describe the process that is actually
        running. This build holds no privilege and reads only world-readable sources, and
        it says exactly that rather than implying a separation it has not made.
        """
        euid = os.geteuid()
        return {
            "agent_instance_id": self._instance_id,
            "agent_version": __version__,
            "started_at": self._started_at,
            "transport": self._transport,
            "process_isolated": True,
            "privilege": "root" if euid == 0 else "unprivileged",
            "effective_uid": euid,
            "pid": os.getpid(),
            "methods": sorted(METHODS),
            # One member, and it is a fact rather than a gesture: the agent has a method
            # that writes, so the list that says which methods write has one entry in it.
            # The privilege to perform that write is still not the agent's — it belongs to
            # a separate process behind a peer-credential check, and `privilege` above
            # reports what this process actually is.
            "mutating_methods": sorted(MUTATING_METHODS),
            "sysfs_net": str(self._sysfs_net),
            "root": str(self._root),
            "docker_socket": str(self._docker_socket),
            "helper_socket": str(self._helper_socket),
        }

    def _require_capability(self, capability: str) -> None:
        found = next((c for c in self._capabilities if c.capability == capability), None)
        if found is None or found.status is CapabilityStatus.UNAVAILABLE:
            raise ProtocolError(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                f"capability {capability} is not available on this host",
                {
                    "capability": capability,
                    "status": str(found.status) if found else "absent",
                    "reason": found.reason if found else None,
                    "detail": found.detail if found else {},
                },
            )


def _docker_error(failure: DockerFailure) -> ProtocolError:
    """A Docker read that did not work, as a typed protocol error.

    ``provider_error`` rather than a bespoke code: it is the one this channel already has
    for "the thing behind this method would not answer", and the daemon's own reason travels
    in the detail where a caller can branch on it.
    """
    return ProtocolError(
        ErrorCode.PROVIDER_ERROR,
        f"docker could not be read: {failure.reason}",
        {
            "provider": "docker",
            "reason": failure.reason,
            "status": str(failure.status),
            "http_status": failure.http_status,
            **failure.detail,
        },
    )


def _require_no_params(params: dict[str, Any], method: str) -> None:
    _reject_unknown_params(params, set(), method)


def _reject_unknown_params(params: dict[str, Any], allowed: set[str], method: str) -> None:
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise ProtocolError(
            ErrorCode.UNKNOWN_FIELD,
            f"unknown parameter(s) for {method}: {', '.join(unknown)}",
            {"method": str(method), "unknown": unknown, "allowed": sorted(allowed)},
        )


def _require_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, f"{name} must be a non-empty string")
    return value


def _require_int(value: Any, name: str) -> int:
    # `bool` is a subclass of `int` and `True` where an MTU belongs is a caller bug.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(ErrorCode.INVALID_PARAMS, f"{name} must be an integer")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
