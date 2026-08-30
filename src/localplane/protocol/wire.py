"""The agent channel's vocabulary. The framing itself lives in :mod:`localplane.ipc`.

Transport is AF_UNIX SOCK_STREAM; framing is one JSON object per line, UTF-8, request and
response strictly correlated by ``request_id``. That is written once, in ``ipc``, and shared
with the privileged helper's channel — because two hand-written copies of it would be two
places for a fix to land in one of.

**What is not shared is this file: the method set.** The agent's vocabulary and the helper's
are declared separately and deliberately, so a method the agent gains cannot appear at the
root end for free.

Every operation here is read-only except four, and they write in different places by
different means. ``network.set_interface_mtu`` forwards an already-authorised typed mutation
to the privileged helper, which sends one netlink message to the kernel.
``docker.container_lifecycle`` asks the Docker daemon to run one of three declared lifecycle
verbs against one container, over the daemon's own API. ``systemd.service_lifecycle`` asks
the system manager to run one of the same three verbs against one already-loaded service
unit, over systemd's own D-Bus interface and under whatever PolicyKit decides at the moment
it is asked. None is a product decision the agent makes, and none takes a path, a payload, a
command or a verb the caller invented: each takes an action from a closed set of three and
one identifier, and there is no method here through which an arbitrary Docker request or an
arbitrary D-Bus call could be issued.

**The systemd one names no D-Bus anything.** Its parameters are an attempt id, a canonical
``.service`` unit name and one of three actions. The object path is resolved by the provider
from that name and never leaves it; the interface, member, signature, start mode and job
identifier are the provider's; and there is no member of this protocol that would accept any
of them. Authorization is not asked here and is not preflighted anywhere — systemd decides
when it processes the call, and a refusal comes back as a typed outcome rather than as
something LocalPlane predicted.

The three connection-guard methods hold, release and report *one* deferred MTU write whose
every parameter was fixed by the caller when the guard was armed. There is no command, no
argv, no script, no probe target and no rollback expression among them: what a guard can do
is the same typed mutation ``network.set_interface_mtu`` already performs, and nothing else.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Final

from localplane import ipc

PROTOCOL_VERSION: Final = "1"

MAX_MESSAGE_BYTES: Final = 16 * 1024 * 1024
"""Hard ceiling on a single framed message.

An observation sweep of a host with many interfaces, carrying raw evidence, is the largest
message this protocol moves. The ceiling exists so a malformed or hostile peer cannot make
either side allocate without bound.
"""

UNCORRELATED_REQUEST_ID: Final = ipc.UNCORRELATED_REQUEST_ID


class Method(StrEnum):
    """Every operation the agent will answer. Exactly three of them write."""

    AGENT_HELLO = "agent.hello"
    HOST_IDENTIFY = "host.identify"
    CAPABILITIES_LIST = "capabilities.list"
    NETWORK_OBSERVE_INTERFACES = "network.observe_interfaces"
    NETWORK_OBSERVE_PROVIDERS = "network.observe_providers"
    NETWORK_OBSERVE_ROUTE = "network.observe_route"
    NETWORK_SET_INTERFACE_MTU = "network.set_interface_mtu"
    NETWORK_ARM_MTU_GUARD = "network.arm_mtu_guard"
    NETWORK_DISARM_MTU_GUARD = "network.disarm_mtu_guard"
    NETWORK_MTU_GUARD_STATUS = "network.mtu_guard_status"
    DOCKER_OBSERVE_CONTAINERS = "docker.observe_containers"
    DOCKER_CONTAINER_LOGS = "docker.container_logs"
    DOCKER_CONTAINER_STATS = "docker.container_stats"
    DOCKER_CONTAINER_LIFECYCLE = "docker.container_lifecycle"
    SYSTEMD_OBSERVE_UNITS = "systemd.observe_units"
    SYSTEMD_OBSERVE_UNIT = "systemd.observe_unit"
    SYSTEMD_RESOLVE_AGENT_UNIT = "systemd.resolve_agent_unit"
    SYSTEMD_OBSERVE_LIFECYCLE_CONTEXT = "systemd.observe_lifecycle_context"
    SYSTEMD_SERVICE_LIFECYCLE = "systemd.service_lifecycle"


METHODS: Final[frozenset[str]] = frozenset(m.value for m in Method)

MUTATING_METHODS: Final[frozenset[str]] = frozenset(
    {
        Method.NETWORK_SET_INTERFACE_MTU.value,
        Method.NETWORK_ARM_MTU_GUARD.value,
        Method.DOCKER_CONTAINER_LIFECYCLE.value,
        Method.SYSTEMD_SERVICE_LIFECYCLE.value,
    }
)
"""The subset of :data:`METHODS` that can change a host. Four members.

Declared here rather than derived at the agent, so "which of these writes" is a fact about
the protocol both sides agree on rather than an opinion the agent forms about itself.

The four are not the same kind of write and the vocabulary does not pretend they are. One
reaches the kernel through a privileged helper; one reaches a daemon that is already
running the thing being changed, through that daemon's own interface; one asks the system
manager to run a job against a unit it already has loaded. What they share is that a caller
cannot choose what any of them does.

**``network.arm_mtu_guard`` is the third, and it is here because of what it can cause
rather than what it does.** Arming writes nothing. What it establishes is a reversal that
will be dispatched with no further request if a deadline passes — so a caller who can arm a
guard can cause a kernel write, and a method that could do that while being classified as
read-only would make this inventory a lie in exactly the direction nobody checks.
``network.disarm_mtu_guard`` and ``network.mtu_guard_status`` are not here: one can only
*prevent* the reversal and the other only reports it.
"""

DOCKER_LIFECYCLE_ACTIONS: Final[tuple[str, ...]] = ("start", "stop", "restart")
"""The whole of what ``docker.container_lifecycle`` will accept in its ``action``.

Three verbs, declared in the protocol so that both sides agree on the closed set rather than
the agent deciding for itself what it is willing to run. Creating, removing, killing,
pausing, renaming, committing, executing and updating a container are not here, and the
agent refuses anything not on this tuple before it reaches the Docker socket.
"""

SYSTEMD_SERVICE_LIFECYCLE_ACTIONS: Final[tuple[str, ...]] = (
    "start",
    "stop",
    "restart",
)
"""The closed service action vocabulary, shared by the read context and the dispatch.

Three verbs, and the same three on both sides of the channel. ``systemd.service_lifecycle``
accepts an ``action`` from this tuple and nothing else, and the agent refuses anything not on
it before a bus connection is opened. Enabling, disabling, masking, unmasking, reloading,
reloading the daemon, resetting a failed unit, killing one, freezing one and editing a unit
file are not here, and there is no member of this protocol through which any of them could
be reached.

**What the caller does not choose.** The action selects which method the provider calls on
the unit object it resolved itself; the caller supplies no D-Bus object path, interface,
member name, signature, job identifier, start mode, timeout or property name, because there
is no parameter for any of those at any layer between the API and the bus. The start mode is
a provider constant, not an argument.
"""

SYSTEMD_SERVICE_LIFECYCLE_UNIT_SUFFIX: Final = ".service"
"""The only unit type the mutating method will accept, checked on both sides.

The lifecycle vocabulary is a *service* vocabulary: what start, stop and restart mean for a
socket, a timer, a path, a mount or a slice is not the same question, and the conservative
applicability rules the backend publishes were written about services. A caller cannot widen
this by naming a unit of another type, and the agent refuses one before it reaches the bus.
"""


DOCKER_LOG_LINES_DEFAULT: Final = 200
DOCKER_LOG_LINES_MAX: Final = 2000
"""How many recent log lines ``docker.container_logs`` returns, and the most it will.

Declared in the protocol rather than at either end because both ends need them: the agent
clamps to the ceiling, and the API documents it. A caller may ask for fewer and cannot ask
for more, and there is no ``follow`` or ``since`` to ask for at all — this method is a
bounded read, and streaming is a different thing that this build does not have.
"""


class ErrorCode(StrEnum):
    """Structured failure codes. Callers branch on these, not on message text."""

    PROTOCOL_VERSION_UNSUPPORTED = ipc.PROTOCOL_VERSION_UNSUPPORTED
    MALFORMED_MESSAGE = ipc.MALFORMED_MESSAGE
    UNKNOWN_FIELD = ipc.UNKNOWN_FIELD
    UNSUPPORTED_METHOD = ipc.UNSUPPORTED_METHOD
    INVALID_PARAMS = ipc.INVALID_PARAMS
    MESSAGE_TOO_LARGE = ipc.MESSAGE_TOO_LARGE
    UNAUTHORIZED_PEER = ipc.UNAUTHORIZED_PEER
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    PROVIDER_ERROR = "provider_error"
    INTERNAL_ERROR = ipc.INTERNAL_ERROR
    AGENT_UNAVAILABLE = "agent_unavailable"
    TIMEOUT = "timeout"


class ProtocolError(ipc.LineProtocolError):
    """A message this channel could not accept. Distinct from the helper channel's."""


#: Per-method parameters are checked in the agent's handlers rather than here, because one
#: of them takes a list and the checks that matter are about types, not names.
CODEC: Final = ipc.Codec(
    envelope_key="localplane_protocol",
    version=PROTOCOL_VERSION,
    max_message_bytes=MAX_MESSAGE_BYTES,
    methods=METHODS,
    error_type=ProtocolError,
    codes=ErrorCode,
)


def encode_request(request_id: str, method: str, params: dict[str, Any] | None = None) -> bytes:
    return CODEC.request(request_id, method, params)


def encode_ok_response(request_id: str, result: dict[str, Any]) -> bytes:
    return CODEC.ok(request_id, result)


def encode_error_response(
    request_id: str, code: ErrorCode, message: str, detail: dict[str, Any] | None = None
) -> bytes:
    return CODEC.failure(request_id, code, message, detail)


def decode_request(raw: bytes) -> tuple[str, str, dict[str, Any]]:
    return CODEC.decode_request(raw)


def decode_response(raw: bytes) -> tuple[str, bool, dict[str, Any] | None, dict[str, Any] | None]:
    return CODEC.decode_response(raw)


def read_message(stream: Any, limit: int = MAX_MESSAGE_BYTES) -> bytes | None:
    return CODEC.read_message(stream, limit)


def write_message(stream: Any, frame: bytes) -> None:
    CODEC.write_message(stream, frame)
