"""The helper channel's vocabulary: two methods, one of which writes.

The framing lives in :mod:`localplane.ipc` and is shared with the agent channel. **The
vocabulary is not**, and that is the whole reason this file exists separately: a method the
agent gains must not appear at the root end for free. Its envelope key, its version and its
method set are its own, and widening any of them is a visible change here.

Two rules belong to this file rather than to the framing:

* **Per-method parameters are a closed set.** ``HELPER_PARAMS`` says exactly which names each
  method accepts, and the codec refuses anything else — so there is nowhere to smuggle a
  command, an executable, a provider or a message type.
* **A mutation reports a typed outcome, never a boolean.** :class:`MutationOutcome` has three
  members because there are three truths, and collapsing the third into either of the others
  is the single failure this protocol exists to prevent.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Final

from localplane import ipc

HELPER_PROTOCOL_VERSION: Final = "1"

MAX_MESSAGE_BYTES: Final = 64 * 1024
"""Two orders of magnitude below the agent's, because nothing here carries an observation.
The largest message on this channel is a mutation request with four scalar fields."""

UNCORRELATED_REQUEST_ID: Final = ipc.UNCORRELATED_REQUEST_ID

#: The bounded range of MTU values the helper will attempt at all. 68 is IPv4's minimum link
#: MTU; 65536 is what the kernel's own loopback carries. A value outside this is refused
#: before the mutating request is built, so it never reaches the kernel and the refusal is
#: provably `not_written`.
MIN_MTU: Final = 68
MAX_MTU: Final = 65536


class HelperMethod(StrEnum):
    """Every operation the helper will answer. Exactly one of them writes."""

    HELLO = "helper.hello"
    """What this helper is. Reads nothing about the host and changes nothing; it exists so a
    capability probe can establish that the privileged path is reachable *without performing
    a mutation to find out*."""

    SET_INTERFACE_MTU = "network.interface.set_mtu"
    """Set one interface's MTU. The only mutating operation in this protocol."""


HELPER_METHODS: Final[frozenset[str]] = frozenset(m.value for m in HelperMethod)

MUTATING_HELPER_METHODS: Final[frozenset[str]] = frozenset({HelperMethod.SET_INTERFACE_MTU.value})

HELPER_PARAMS: Final[dict[str, frozenset[str]]] = {
    HelperMethod.HELLO.value: frozenset(),
    HelperMethod.SET_INTERFACE_MTU.value: frozenset({
        "attempt_id", "ifindex", "expected_current_mtu", "desired_mtu", "expected_interface_name",
    }),
}


class MutationOutcome(StrEnum):
    """What became of a mutating request. Three truths, and they are not interchangeable.

    The distinction is not cosmetic and it is not derivable from the host's state afterwards.
    Reading the MTU back answers *what is the host now*; it does not answer *did our write
    occur*. A value that already matched, a second writer and a successful write are
    indistinguishable from the resulting number alone.
    """

    NOT_WRITTEN = "not_written"
    """Provably no effect: execution failed before the point at which the kernel could have
    accepted the mutation."""

    WRITTEN = "written"
    """An authoritative acknowledgement, correlated to this request, that the kernel accepted
    the write."""

    WRITE_UNKNOWN = "write_unknown"
    """The request may have reached the kernel and there is not enough evidence to say. Never
    converted into either of the others by looking at the resulting value."""


class HelperErrorCode(StrEnum):
    """Structured failure codes. Callers branch on these, never on message text."""

    PROTOCOL_VERSION_UNSUPPORTED = ipc.PROTOCOL_VERSION_UNSUPPORTED
    MALFORMED_MESSAGE = ipc.MALFORMED_MESSAGE
    UNKNOWN_FIELD = ipc.UNKNOWN_FIELD
    UNSUPPORTED_METHOD = ipc.UNSUPPORTED_METHOD
    INVALID_PARAMS = ipc.INVALID_PARAMS
    MESSAGE_TOO_LARGE = ipc.MESSAGE_TOO_LARGE
    UNAUTHORIZED_PEER = ipc.UNAUTHORIZED_PEER
    INTERNAL_ERROR = ipc.INTERNAL_ERROR
    HELPER_UNAVAILABLE = "helper_unavailable"
    TIMEOUT = "timeout"


class HelperProtocolError(ipc.LineProtocolError):
    """A message this channel could not accept. Distinct from the agent channel's."""


CODEC: Final = ipc.Codec(
    envelope_key="localplane_helper",
    version=HELPER_PROTOCOL_VERSION,
    max_message_bytes=MAX_MESSAGE_BYTES,
    methods=HELPER_METHODS,
    error_type=HelperProtocolError,
    codes=HelperErrorCode,
    params=HELPER_PARAMS,
)


def encode_request(request_id: str, method: str, params: dict[str, Any] | None = None) -> bytes:
    return CODEC.request(request_id, method, params)
