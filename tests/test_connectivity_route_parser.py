import socket
import struct

import pytest

from localplane.agent.providers.connectivity_route import validate_reply, connectivity_route_provider
from localplane.agent.providers.linux_route import NetlinkFailed


def frame(kind, body, sequence=1):
    return struct.pack("=IHHII", 16 + len(body), kind, 0, sequence, 0) + body


@pytest.mark.parametrize("payload", [
    b"", b"\x00" * 5,
    frame(2, b"\x00"),
    frame(2, struct.pack("=i", 1)),
    frame(24, b"\x00" * 4),
    frame(24, struct.pack("=BBBBBBBBI", socket.AF_INET, 32, 0, 0, 254, 0, 0, 1, 0) + struct.pack("=HH", 9, 4)),
    frame(2, struct.pack("=i", 0), sequence=2),
])
def test_malformed_kernel_reply_is_rejected(payload):
    with pytest.raises(NetlinkFailed):
        validate_reply(payload)


def test_provider_reports_malformed_reply_as_failed_without_network():
    result = connectivity_route_provider(query=lambda _: frame(2, b"\x00")).observe_route("192.0.2.1")
    assert result.status == "failed" and result.reason == "malformed_netlink_response"
