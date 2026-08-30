"""The backend's client for the agent socket.

One connection per call. The agent answers in milliseconds and the backend talks to it a
handful of times a minute; a pool would add a class of stale-connection bug in exchange
for nothing measurable.

Failure is structured and it fails closed. Every way this can go wrong — no socket, a
refused connection, a timeout, a malformed frame, a mismatched ``request_id``, an error
from the agent itself — raises :class:`AgentError` with a code the caller can branch on.
There is no path that returns a plausible empty result when the agent did not answer,
because a caller that cannot tell those apart will eventually report one as the other.

**And one failure carries a second fact.** On a read path, "the call did not work" is one
condition. On a path that mutates, it is two: the request may never have left this
process, or it may already have reached the kernel. :attr:`AgentError.dispatched` records
which, set the moment the request frame is written to the socket, and it is what lets the
Change engine tell ``not_written`` from ``write_unknown`` without guessing. It is set on
every call, not only the mutating one, because a fact about what happened should not depend
on who is asking.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from localplane import ipc
from localplane.protocol.wire import CODEC, PROTOCOL_VERSION, ErrorCode, Method

LOG = logging.getLogger("localplane.backend.agent_client")

# The provider bounds the inventory itself at 30 seconds.  Leave enough transport margin
# for framing the bounded response; ordinary agent calls keep the configured default.
SYSTEMD_INVENTORY_CLIENT_TIMEOUT_S = 35.0


class AgentError(RuntimeError):
    """The agent could not be reached, or refused.

    ``dispatched`` says whether the request had already been written to the socket when
    this failure happened. False means the agent was never given the request, which on the
    mutating path is a proof that nothing was written. True means it may have acted, and
    the honest answer becomes "we do not know" rather than "it failed".
    """

    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        detail: dict[str, Any] | None = None,
        dispatched: bool = False,
    ):
        super().__init__(message)
        self.code = str(code)
        self.message = message
        self.detail = detail or {}
        self.dispatched = dispatched

    def as_dict(self) -> dict[str, Any]:
        """The error as an API error body. Deliberately the same three fields it always was.

        ``dispatched`` is not in here. It is a fact about *this process's* attempt, not
        about the failure a caller is being told about, and it is read from the attribute by
        the one caller that needs it — the mutating executor, which records it explicitly
        beside the error rather than smuggling it into a shape every read path also renders.
        """
        return {"code": self.code, "message": self.message, "detail": self.detail}


class AgentClient:
    """Typed calls to the agent. Every method here maps to one protocol method."""

    def __init__(self, socket_path: str | Path, timeout_s: float = 10.0) -> None:
        self.socket_path = Path(socket_path)
        self.timeout_s = timeout_s

    # ------------------------------------------------------------------- typed methods

    def hello(self) -> dict[str, Any]:
        return self.call(Method.AGENT_HELLO)

    def identify_host(self) -> dict[str, Any]:
        return self.call(Method.HOST_IDENTIFY)

    def list_capabilities(self) -> list[dict[str, Any]]:
        return self.call(Method.CAPABILITIES_LIST)["capabilities"]

    def observe_interfaces(self, names: list[str] | None = None) -> dict[str, Any]:
        params = {"names": names} if names is not None else {}
        return self.call(Method.NETWORK_OBSERVE_INTERFACES, params)

    def observe_providers(self) -> dict[str, Any]:
        return self.call(Method.NETWORK_OBSERVE_PROVIDERS)

    def observe_route(self, destination: str) -> dict[str, Any]:
        """Ask the kernel which route it would use to reach one address.

        ``destination`` is not a caller's to choose. Every call on this path passes an
        address the *backend* read from the server side of a live connection, and there is
        no endpoint through which a request body, a query parameter or a header can reach
        this argument. It is a typed IP address, it is validated again by the agent, and it
        reaches no command line at either end.
        """
        return self.call(Method.NETWORK_OBSERVE_ROUTE, {"destination": destination})

    def set_interface_mtu(
        self,
        *,
        attempt_id: str,
        ifindex: int,
        expected_current_mtu: int,
        desired_mtu: int,
        expected_interface_name: str | None = None,
    ) -> dict[str, Any]:
        """The one call on this client that can change the host.

        Every argument is a typed scalar the backend derived from records it already holds:
        the index and name come from the observation the checkpoint was armed against, the
        expected value from that same reading, and the desired value from the active intent
        and from nowhere else. There is no parameter here through which a caller of the API
        could supply an MTU, an interface, a command or a provider, because there is no
        parameter for any of those at any layer between the two.
        """
        params: dict[str, Any] = {
            "attempt_id": attempt_id,
            "ifindex": ifindex,
            "expected_current_mtu": expected_current_mtu,
            "desired_mtu": desired_mtu,
        }
        if expected_interface_name is not None:
            params["expected_interface_name"] = expected_interface_name
        return self.call(Method.NETWORK_SET_INTERFACE_MTU, params)

    def arm_mtu_guard(
        self,
        *,
        guard_id: str,
        attempt_id: str,
        ifindex: int,
        guarded_mtu: int,
        restore_mtu: int,
        window_s: int,
        expected_interface_name: str | None = None,
    ) -> dict[str, Any]:
        """Ask the agent to hold a reversal for this interface until a deadline.

        The second call on this client that can change the host, and it is the more
        interesting of the two: it writes nothing, and what it establishes is a write that
        will happen with no further request from anybody if the deadline passes.

        Every argument comes from the durable checkpoint written a moment earlier — which
        took the value to restore from a verified observation and the value being written
        from the active intent — and ``window_s`` is a policy constant. There is no
        parameter here for a command, a script, a probe, a peer, a route or a second path,
        and nothing on the API surface can reach any of these.
        """
        params: dict[str, Any] = {
            "guard_id": guard_id,
            "attempt_id": attempt_id,
            "ifindex": ifindex,
            "guarded_mtu": guarded_mtu,
            "restore_mtu": restore_mtu,
            "window_s": window_s,
        }
        if expected_interface_name is not None:
            params["expected_interface_name"] = expected_interface_name
        return self.call(Method.NETWORK_ARM_MTU_GUARD, params)

    def disarm_mtu_guard(self, guard_id: str) -> dict[str, Any]:
        """Release a guard, and be told what became of it. Can only prevent a write."""
        return self.call(Method.NETWORK_DISARM_MTU_GUARD, {"guard_id": guard_id})

    def mtu_guard_status(self, guard_id: str) -> dict[str, Any]:
        """What a guard is doing. A read: it never releases anything."""
        return self.call(Method.NETWORK_MTU_GUARD_STATUS, {"guard_id": guard_id})

    def observe_containers(self) -> dict[str, Any]:
        return self.call(Method.DOCKER_OBSERVE_CONTAINERS)

    def observe_systemd_units(self) -> dict[str, Any]:
        """Read the bounded loaded-unit inventory.  Takes no selection language."""
        return self.call(
            Method.SYSTEMD_OBSERVE_UNITS,
            timeout_s=max(self.timeout_s, SYSTEMD_INVENTORY_CLIENT_TIMEOUT_S),
        )

    def observe_systemd_unit(self, unit_id: str) -> dict[str, Any]:
        """Read one already-loaded unit.  The provider uses GetUnit, never LoadUnit."""
        return self.call(Method.SYSTEMD_OBSERVE_UNIT, {"unit_id": unit_id})

    def resolve_agent_systemd_unit(self) -> dict[str, Any]:
        """Resolve the agent process's containing unit from official cgroup evidence."""
        return self.call(Method.SYSTEMD_RESOLVE_AGENT_UNIT)

    def observe_systemd_lifecycle_context(
        self,
        *,
        target_unit_id: str,
        action: str,
        connection: dict[str, Any],
        management_providers: list[str],
        provider_evidence_complete: bool,
    ) -> dict[str, Any]:
        """Read one request-scoped lifecycle safety context; never dispatch a job."""
        return self.call(
            Method.SYSTEMD_OBSERVE_LIFECYCLE_CONTEXT,
            {
                "target_unit_id": target_unit_id,
                "action": action,
                "connection": connection,
                "management_providers": management_providers,
                "provider_evidence_complete": provider_evidence_complete,
            },
        )

    def container_logs(self, container_id: str, tail: int) -> dict[str, Any]:
        """The most recent lines a container wrote. Bounded at both ends of the path."""
        return self.call(
            Method.DOCKER_CONTAINER_LOGS, {"container_id": container_id, "tail": tail}
        )

    def container_stats(self, container_id: str) -> dict[str, Any]:
        return self.call(Method.DOCKER_CONTAINER_STATS, {"container_id": container_id})

    def container_lifecycle(
        self, *, attempt_id: str, container_id: str, action: str
    ) -> dict[str, Any]:
        """The second call on this client that can change the host.

        Three arguments, all typed scalars the backend derived from records it already
        holds: the attempt id it minted at the boundary, the container id from the
        observation the plan was made against, and the verb the *operation* declares — not
        one a caller chose. There is no parameter here for a Docker path, a payload, a
        command, a signal or a timeout, because there is no parameter for any of those at
        any layer between the API and the daemon.
        """
        return self.call(
            Method.DOCKER_CONTAINER_LIFECYCLE,
            {"attempt_id": attempt_id, "container_id": container_id, "action": action},
        )

    def systemd_service_lifecycle(
        self, *, attempt_id: str, unit_id: str, action: str
    ) -> dict[str, Any]:
        """One of the calls on this client that can change the host. Transport only.

        The protocol classifies four methods as mutating and this is one of them; the
        ordinal is not worth counting, because they write in different places by
        different means and one of them writes nothing directly at all.

        Three arguments, all typed scalars: the attempt id the backend minted at the write
        boundary, the canonical service unit name from the observation the plan was made
        against, and the verb the *operation* declares — not one a caller chose. There is no
        parameter here for a D-Bus object path, an interface, a member, a signature, a job
        id, a start mode, a timeout or a property, because there is no parameter for any of
        those at any layer between the API and the bus.

        This is the wire :class:`~localplane.backend.systemd_operations.SystemdLifecycleExecutor`
        dispatches through: it is called on the apply of an eligible systemd plan, and on
        nothing else.
        """
        return self.call(
            Method.SYSTEMD_SERVICE_LIFECYCLE,
            {"attempt_id": attempt_id, "unit_id": unit_id, "action": action},
        )

    # ------------------------------------------------------------------------ transport

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        return ipc.call(
            self.socket_path,
            CODEC,
            self.timeout_s if timeout_s is None else timeout_s,
            method,
            params,
            AgentError,
            unavailable=ErrorCode.AGENT_UNAVAILABLE,
            timeout_code=ErrorCode.TIMEOUT,
        )

    def protocol_version(self) -> str:
        return PROTOCOL_VERSION
