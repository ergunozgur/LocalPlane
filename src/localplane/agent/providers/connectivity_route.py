"""Validate a connectivity-only netlink reply before the existing route parser sees it."""
from __future__ import annotations

import socket
import struct

from localplane.agent.providers.linux_route import (
    KernelRouteQuery, LinuxRouteProvider, NetlinkFailed, NLMSG_DONE, NLMSG_ERROR,
    RTM_NEWROUTE, RTA_DST, RTA_SRC, RTA_GATEWAY, RTA_PREFSRC, RTA_OIF,
    RTA_PRIORITY, RTA_TABLE,
)

_HEADER = struct.Struct("=IHHII")
_ROUTE = struct.Struct("=BBBBBBBBI")
_ATTR = struct.Struct("=HH")


def validate_reply(payload: bytes) -> bytes:
    if not isinstance(payload, bytes) or not payload:
        raise NetlinkFailed("malformed_netlink_response")
    offset = 0
    while offset < len(payload):
        if len(payload) - offset < _HEADER.size:
            raise NetlinkFailed("malformed_netlink_response")
        length, kind, _, sequence, _ = _HEADER.unpack_from(payload, offset)
        if length < _HEADER.size or offset + length > len(payload) or sequence != 1:
            raise NetlinkFailed("malformed_netlink_response")
        body = payload[offset + _HEADER.size:offset + length]
        if kind == NLMSG_ERROR and (len(body) < 4 or struct.unpack_from("=i", body)[0] > 0):
            raise NetlinkFailed("malformed_netlink_response")
        if kind == RTM_NEWROUTE:
            if len(body) < _ROUTE.size:
                raise NetlinkFailed("malformed_netlink_response")
            family = body[0]
            if family not in (socket.AF_INET, socket.AF_INET6):
                raise NetlinkFailed("malformed_netlink_response")
            pos = _ROUTE.size
            seen = set()
            while pos < len(body):
                if len(body) - pos < _ATTR.size:
                    raise NetlinkFailed("malformed_netlink_response")
                size, attr = _ATTR.unpack_from(body, pos)
                if size < _ATTR.size or pos + size > len(body):
                    raise NetlinkFailed("malformed_netlink_response")
                data_len = size - _ATTR.size
                if attr in seen or (attr in (RTA_DST, RTA_SRC, RTA_GATEWAY, RTA_PREFSRC) and data_len != (4 if family == socket.AF_INET else 16)) or (attr in (RTA_OIF, RTA_PRIORITY, RTA_TABLE) and data_len != 4):
                    raise NetlinkFailed("malformed_netlink_response")
                seen.add(attr)
                pos += (size + 3) & ~3
            if pos > len(body):
                raise NetlinkFailed("malformed_netlink_response")
        offset += (length + 3) & ~3
        if offset > len(payload):
            raise NetlinkFailed("malformed_netlink_response")
        if kind == NLMSG_DONE and offset != len(payload):
            raise NetlinkFailed("malformed_netlink_response")
    return payload


def connectivity_route_provider(query=None) -> LinuxRouteProvider:
    inner = query if query is not None else KernelRouteQuery()
    return LinuxRouteProvider(query=lambda request: validate_reply(inner(request)))
