"""The wire contract shared by the LocalPlane Agent and the LocalPlane backend.

This package is imported by the agent, so it is standard-library only. It carries no
behaviour beyond framing, validation and vocabulary: it must stay small enough to read
in one sitting, because it is the boundary that decides what the backend is allowed to
ask a privileged component to do.
"""

from localplane.protocol.capabilities import (
    CAPABILITIES,
    CAPABILITY_HOST_OBSERVE,
    CAPABILITY_NETWORK_OBSERVE,
    CapabilityStatus,
)
from localplane.protocol.wire import (
    MAX_MESSAGE_BYTES,
    METHODS,
    PROTOCOL_VERSION,
    ErrorCode,
    Method,
    ProtocolError,
    decode_request,
    decode_response,
    encode_error_response,
    encode_ok_response,
    encode_request,
    read_message,
    write_message,
)

__all__ = [
    "CAPABILITIES",
    "CAPABILITY_HOST_OBSERVE",
    "CAPABILITY_NETWORK_OBSERVE",
    "CapabilityStatus",
    "MAX_MESSAGE_BYTES",
    "METHODS",
    "PROTOCOL_VERSION",
    "ErrorCode",
    "Method",
    "ProtocolError",
    "decode_request",
    "decode_response",
    "encode_error_response",
    "encode_ok_response",
    "encode_request",
    "read_message",
    "write_message",
]
