"""The capability vocabulary.

A capability names something the agent can actually do on this host. It is discovered by
probing, never assumed. The backend must not conclude that a capability exists because
LocalPlane knows the concept: an entry absent from an agent's reported list is not
available, and an entry present with status ``unavailable`` is not available either.

Only capabilities that the agent genuinely implements are declared here. The list grows
when the agent grows, not before.

``mutating`` is part of the contract rather than a comment. Every capability was
``mutating: False`` until one of them wrote to a host, and four carry the flag now:
:data:`CAPABILITY_NETWORK_INTERFACE_SET_MTU`, behind a privileged helper that sends one
fixed netlink message; :data:`CAPABILITY_DOCKER_CONTAINER_LIFECYCLE`, behind three declared
verbs on the Docker daemon's own API; and
:data:`CAPABILITY_NETWORK_INTERFACE_MTU_GUARD`, which writes nothing itself and can cause
the first one to happen with no further request when a guard's deadline passes; plus
:data:`CAPABILITY_SYSTEMD_SERVICE_LIFECYCLE`, which is detected through passive
introspection and whose mutating protocol method is dispatched on the apply of an eligible
plan. A caller
that needs to know whether asking for something could change the host reads this flag and
does not have to know what any of them is.

**The guard is a capability of its own rather than a property of the write.** An agent that
can set an MTU is not necessarily an agent that knows how to hold a guard — the two sides
of this product are versioned independently, and a backend that assumed otherwise would
publish guarded plans against an agent that would refuse to arm one. It is probed for what
it is, and it is ``unavailable`` wherever the write it would reverse is.

**Docker's two capabilities are separate because the access can be.** Reading the daemon
and asking it to act are the same socket in the ordinary case and are not the same thing in
general: a socket published through a read-only proxy answers ``GET /containers/json`` and
refuses a lifecycle ``POST``. Reporting one capability for both would make LocalPlane
publish executable plans it cannot execute, so each is probed for what it actually is.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, NamedTuple


class CapabilityStatus(StrEnum):
    """Whether a capability can be used, and how well."""

    AVAILABLE = "available"
    """Every method behind the capability probed successfully."""

    DEGRADED = "degraded"
    """The capability works, but with less evidence than its full definition promises."""

    UNAVAILABLE = "unavailable"
    """The capability cannot be used on this host. ``reason`` says why."""


class CapabilityDefinition(NamedTuple):
    """The static definition of a capability — its contract, not its availability."""

    capability: str
    version: int
    summary: str
    mutating: bool


CAPABILITY_HOST_OBSERVE: Final = "host.observe"
CAPABILITY_NETWORK_OBSERVE: Final = "network.observe"
CAPABILITY_NETWORK_PROVIDERS_OBSERVE: Final = "network.providers.observe"
CAPABILITY_NETWORK_ROUTE_OBSERVE: Final = "network.route.observe"
CAPABILITY_NETWORK_INTERFACE_SET_MTU: Final = "network.interface.set_mtu"
CAPABILITY_NETWORK_INTERFACE_MTU_GUARD: Final = "network.interface.mtu_guard"
CAPABILITY_DOCKER_CONTAINERS_OBSERVE: Final = "docker.containers.observe"
CAPABILITY_DOCKER_CONTAINER_LIFECYCLE: Final = "docker.container.lifecycle"
CAPABILITY_SYSTEMD_UNITS_OBSERVE: Final = "systemd.units.observe"
CAPABILITY_SYSTEMD_SERVICE_LIFECYCLE: Final = "systemd.service.lifecycle"
CAPABILITY_SYSTEMD_SERVICE_LIFECYCLE_CONTEXT_OBSERVE: Final = (
    "systemd.service.lifecycle_context.observe"
)

CAPABILITIES: Final[dict[str, CapabilityDefinition]] = {
    CAPABILITY_HOST_OBSERVE: CapabilityDefinition(
        capability=CAPABILITY_HOST_OBSERVE,
        version=1,
        summary="Read this host's identity, operating system and kernel facts.",
        mutating=False,
    ),
    CAPABILITY_NETWORK_OBSERVE: CapabilityDefinition(
        capability=CAPABILITY_NETWORK_OBSERVE,
        version=1,
        summary="Read network interface link state and addresses. Never writes.",
        mutating=False,
    ),
    CAPABILITY_NETWORK_PROVIDERS_OBSERVE: CapabilityDefinition(
        capability=CAPABILITY_NETWORK_PROVIDERS_OBSERVE,
        version=1,
        summary=(
            "Ask the systems that configure networking on this host — Docker, "
            "NetworkManager, Tailscale — what they say they own. Never writes."
        ),
        mutating=False,
    ),
    CAPABILITY_NETWORK_ROUTE_OBSERVE: CapabilityDefinition(
        capability=CAPABILITY_NETWORK_ROUTE_OBSERVE,
        version=1,
        summary=(
            "Ask the kernel which route it would use to reach one address, and report "
            "what it answered. A query, never a change: the netlink message it sends is "
            "RTM_GETROUTE and there is no code here that can send another."
        ),
        mutating=False,
    ),
    CAPABILITY_NETWORK_INTERFACE_SET_MTU: CapabilityDefinition(
        capability=CAPABILITY_NETWORK_INTERFACE_SET_MTU,
        version=1,
        summary=(
            "Set one network interface's MTU, through the privileged helper, as one fixed "
            "RTM_NEWLINK message carrying IFLA_MTU. The only capability in LocalPlane that "
            "reaches the kernel."
        ),
        mutating=True,
    ),
    CAPABILITY_NETWORK_INTERFACE_MTU_GUARD: CapabilityDefinition(
        capability=CAPABILITY_NETWORK_INTERFACE_MTU_GUARD,
        version=1,
        summary=(
            "Hold a connection guard for one interface: a reversal to the value the "
            "interface carried before a change, dispatched by this agent if a deadline "
            "passes without the change being kept. It performs no write of its own and "
            "can dispatch nothing but the reversal it was armed with."
        ),
        mutating=True,
    ),
    CAPABILITY_DOCKER_CONTAINERS_OBSERVE: CapabilityDefinition(
        capability=CAPABILITY_DOCKER_CONTAINERS_OBSERVE,
        version=1,
        summary=(
            "Read what the Docker daemon says about the containers on this host — what "
            "they are, what state they are in, what they are attached to, what they have "
            "mounted, and their logs and current resource usage on request. Never writes."
        ),
        mutating=False,
    ),
    CAPABILITY_DOCKER_CONTAINER_LIFECYCLE: CapabilityDefinition(
        capability=CAPABILITY_DOCKER_CONTAINER_LIFECYCLE,
        version=1,
        summary=(
            "Start, stop or restart one existing container through the Docker daemon's "
            "own API. Three declared verbs and nothing else: this capability cannot "
            "create, remove, kill, pause, rename or execute anything in a container."
        ),
        mutating=True,
    ),
    CAPABILITY_SYSTEMD_UNITS_OBSERVE: CapabilityDefinition(
        capability=CAPABILITY_SYSTEMD_UNITS_OBSERVE,
        version=1,
        summary=(
            "Read the system systemd manager's bounded loaded-unit estate and one loaded "
            "unit on demand through the official D-Bus API. Never writes or loads a unit."
        ),
        mutating=False,
    ),
    CAPABILITY_SYSTEMD_SERVICE_LIFECYCLE: CapabilityDefinition(
        capability=CAPABILITY_SYSTEMD_SERVICE_LIFECYCLE,
        version=1,
        summary=(
            "Declare whether the system manager exposes LocalPlane's closed service "
            "start, stop and restart job contract. Dispatch-time systemd authorization "
            "is assessed separately; the methods run only on the apply of an eligible "
            "plan."
        ),
        # The capability describes a mechanism whose use changes the host: the mutating
        # protocol method exists and is dispatched on the apply of an eligible plan.
        mutating=True,
    ),
    CAPABILITY_SYSTEMD_SERVICE_LIFECYCLE_CONTEXT_OBSERVE: CapabilityDefinition(
        capability=CAPABILITY_SYSTEMD_SERVICE_LIFECYCLE_CONTEXT_OBSERVE,
        version=2,
        summary=(
            "Read a bounded systemd service effect graph and correlate the accepted TCP "
            "socket and LocalPlane agent to containing units, including verified "
            "docker-direct-unix-v1 runtime ownership where its exact prerequisites hold. "
            "Never dispatches a job."
        ),
        mutating=False,
    ),
}

MUTATING_CAPABILITIES: Final[frozenset[str]] = frozenset(
    name for name, definition in CAPABILITIES.items() if definition.mutating
)
"""The capabilities whose mechanisms can change a host. Four members.

Published as a set rather than left to be derived so that "how many ways can LocalPlane
change a host" is one lookup, and so that a test can assert what the answer is.
"""
