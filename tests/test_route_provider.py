"""The agent's route provider: one kernel question, and no way to make it two.

The provider exists because the management path needs corroboration that LocalPlane did not
write itself, and it is netlink rather than ``ip route get`` because a route lookup needs a
target address — which would otherwise be the first place a value derived from outside the
process reached a command line.

So what is asserted hardest here is not that it parses routes. It is that it cannot do
anything else: one message type out, no argv, no shell, no executable, no second purpose,
and a destination that is an :class:`ipaddress` object before it is anything.

The netlink exchange is scripted through the same kind of seam the other providers use for
their commands, so a host with no route, a kernel that refuses and a truncated reply are all
reachable every run. The real kernel is exercised too — in :func:`test_the_real_kernel_answers_a_real_lookup`
below, read-only, and again in ``test_live_host.py``.
"""

from __future__ import annotations

import errno
import ipaddress
import socket
import struct
from pathlib import Path

import pytest

from localplane.agent.providers import linux_route
from localplane.agent.providers.linux_route import (
    PROBE_DESTINATION,
    RTM_GETROUTE,
    InvalidRouteDestination,
    LinuxRouteProvider,
    NetlinkFailed,
    NetlinkUnavailable,
    RouteLookupStatus,
    probe_route,
)
from tests.conftest import FakeRouteQuery, netlink_error, netlink_route

LOOPBACK_ROUTE = netlink_route(
    destination="127.0.0.1", oif_index=1, preferred_source="127.0.0.1", route_type=2
)


def provider(**replies) -> tuple[LinuxRouteProvider, FakeRouteQuery]:
    query = FakeRouteQuery(dict(replies))
    return LinuxRouteProvider(query=query), query


# ------------------------------------------------------------------ it reports kernel facts


def test_a_resolved_route_reports_what_the_kernel_supplied():
    p, _ = provider(
        **{
            "192.0.2.130": netlink_route(
                destination="192.0.2.130",
                oif_index=3,
                preferred_source="192.0.2.215",
                gateway="192.0.2.1",
                table=254,
                priority=100,
            )
        }
    )
    observation = p.observe_route("192.0.2.130")

    assert observation.status is RouteLookupStatus.RESOLVED
    assert observation.reason is None
    assert observation.error is None
    route = observation.route
    assert route is not None
    assert route.family == "inet"
    assert route.destination == "192.0.2.130"
    assert route.destination_prefix_length == 32
    assert route.oif_index == 3
    assert route.preferred_source == "192.0.2.215"
    assert route.gateway == "192.0.2.1"
    assert route.table == 254
    assert route.route_type == "unicast"
    assert route.scope == "universe"
    assert route.priority == 100


def test_absent_attributes_are_null_and_never_zero():
    """A directly connected route has no gateway. That is not a gateway of 0.0.0.0."""
    p, _ = provider(**{"192.0.2.130": netlink_route(destination="192.0.2.130", oif_index=3)})
    route = p.observe_route("192.0.2.130").route
    assert route is not None
    assert route.gateway is None
    assert route.preferred_source is None
    assert route.priority is None


def test_the_provider_reports_an_index_and_never_resolves_it_to_a_name():
    """Turning an index into an object is a judgement, and judgements are the backend's."""
    p, _ = provider(**{"192.0.2.130": netlink_route(destination="192.0.2.130", oif_index=3)})
    rendered = p.observe_route("192.0.2.130").as_dict()
    assert rendered["route"]["oif_index"] == 3
    assert "ifname" not in rendered["route"]
    assert "interface" not in rendered["route"]
    assert "object_id" not in rendered["route"]
    # And nothing anywhere in the answer claims anything is protected or owned.
    flat = str(rendered)
    for word in ("protected", "management_path", "owner", "eligible", "blocker"):
        assert word not in flat


def test_an_ipv6_lookup_round_trips():
    p, _ = provider(
        **{
            "2001:db8::1": netlink_route(
                destination="2001:db8::1", oif_index=7, preferred_source="2001:db8::215"
            )
        }
    )
    route = p.observe_route("2001:db8::1").route
    assert route is not None
    assert route.family == "inet6"
    assert route.destination == "2001:db8::1"
    assert route.destination_prefix_length == 128
    assert route.preferred_source == "2001:db8::215"


def test_a_multipath_route_with_no_single_egress_resolves_without_inventing_one():
    p, _ = provider(**{"192.0.2.130": netlink_route(destination="192.0.2.130")})
    observation = p.observe_route("192.0.2.130")
    assert observation.status is RouteLookupStatus.RESOLVED
    assert observation.route is not None
    assert observation.route.oif_index is None


# ------------------------------------------------------------------------- it fails honestly


@pytest.mark.parametrize(
    "code,reason",
    [
        (errno.ENETUNREACH, "network_unreachable"),
        (errno.EHOSTUNREACH, "host_unreachable"),
        (errno.ENETDOWN, "network_down"),
    ],
)
def test_no_route_is_unreachable_not_a_failure(code: int, reason: str):
    """The kernel answered. Its answer was that it cannot get there."""
    p, _ = provider(**{"8.8.8.8": netlink_error(code)})
    observation = p.observe_route("8.8.8.8")
    assert observation.status is RouteLookupStatus.UNREACHABLE
    assert observation.reason == reason
    assert observation.route is None
    assert observation.error == {"errno": code, "message": errno_message(code)}


def test_an_unexpected_kernel_error_is_a_failure_with_the_errno():
    p, _ = provider(**{"8.8.8.8": netlink_error(errno.ENOMEM)})
    observation = p.observe_route("8.8.8.8")
    assert observation.status is RouteLookupStatus.FAILED
    assert observation.reason == "netlink_error"
    assert observation.error is not None and observation.error["errno"] == errno.ENOMEM


def test_no_netlink_socket_is_unavailable_not_failed():
    """"The query could not be made" and "the query did not work" are different states."""
    query = FakeRouteQuery(default=NetlinkUnavailable("netlink_unavailable", {"error": "nope"}))
    observation = LinuxRouteProvider(query=query).observe_route("192.0.2.130")
    assert observation.status is RouteLookupStatus.UNAVAILABLE
    assert observation.reason == "netlink_unavailable"


def test_a_transport_failure_is_reported_with_its_reason():
    query = FakeRouteQuery(default=NetlinkFailed("netlink_timeout", {"timeout_s": 2.0}))
    observation = LinuxRouteProvider(query=query).observe_route("192.0.2.130")
    assert observation.status is RouteLookupStatus.FAILED
    assert observation.reason == "netlink_timeout"
    assert observation.error == {"timeout_s": 2.0}


def test_a_truncated_reply_is_refused_rather_than_half_parsed():
    whole = netlink_route(destination="192.0.2.130", oif_index=3)
    p, _ = provider(**{"192.0.2.130": whole[: len(whole) - 8]})
    observation = p.observe_route("192.0.2.130")
    assert observation.status is RouteLookupStatus.FAILED
    assert observation.reason == "malformed_netlink_response"
    assert observation.route is None


def test_a_reply_carrying_no_route_says_so():
    from localplane.agent.providers.linux_route import NLMSG_DONE, _NLMSGHDR

    p, _ = provider(**{"192.0.2.130": _NLMSGHDR.pack(_NLMSGHDR.size, NLMSG_DONE, 0, 1, 0)})
    observation = p.observe_route("192.0.2.130")
    assert observation.status is RouteLookupStatus.FAILED
    assert observation.reason == "no_route_in_reply"


def test_an_empty_reply_is_a_failure_and_not_an_empty_route():
    p, _ = provider(**{"192.0.2.130": b""})
    assert p.observe_route("192.0.2.130").status is RouteLookupStatus.FAILED


# ---------------------------------------------------------------- it takes an address only


@pytest.mark.parametrize(
    "value",
    [
        "not-an-ip",
        "192.0.2.130/24",
        "192.0.2.130 || rm -rf /",
        "$(id)",
        "`id`",
        "; ip link set eth0 down",
        "eth0",
        "default",
        "192.0.2.999",
        "",
        "  192.0.2.1  ",
    ],
)
def test_anything_that_is_not_an_address_is_refused_before_the_kernel_is_touched(value: str):
    p, query = provider()
    with pytest.raises(InvalidRouteDestination):
        p.observe_route(value)
    assert query.requests == [], "a refused destination must not reach the kernel"


def test_a_scoped_address_is_refused_because_the_query_cannot_carry_a_scope():
    p, query = provider()
    with pytest.raises(InvalidRouteDestination):
        p.observe_route("fe80::1%eth0")
    assert query.requests == []


def test_a_non_string_destination_is_refused():
    p, _ = provider()
    for value in (None, 3232235777, ["192.0.2.1"], {"address": "192.0.2.1"}):
        with pytest.raises(InvalidRouteDestination):
            p.observe_route(value)  # type: ignore[arg-type]


def test_the_destination_reaches_the_kernel_as_packed_bytes_not_as_text():
    """The whole reason this is netlink and not a command: there is no text on the wire."""
    p, query = provider(**{"192.0.2.130": netlink_route(destination="192.0.2.130", oif_index=3)})
    p.observe_route("192.0.2.130")
    frame = query.frames[0]
    assert b"192.0.2.130" not in frame
    assert ipaddress.ip_address("192.0.2.130").packed in frame


def test_the_only_message_the_provider_sends_is_a_route_query():
    p, query = provider(**{"192.0.2.130": netlink_route(destination="192.0.2.130", oif_index=3)})
    p.observe_route("192.0.2.130")
    _length, kind, flags, _seq, _pid = linux_route._NLMSGHDR.unpack_from(query.frames[0], 0)
    assert kind == RTM_GETROUTE
    assert flags == linux_route.NLM_F_REQUEST
    # No create, no replace, no append, no excl, no ack-and-do.
    for forbidden in ("NLM_F_CREATE", "NLM_F_REPLACE", "NLM_F_APPEND", "NLM_F_EXCL"):
        assert not hasattr(linux_route, forbidden)


def test_the_module_names_no_mutating_netlink_message():
    """RTM_NEWROUTE is received. Nothing that changes anything is even spelled here."""
    source = Path(linux_route.__file__).read_text()
    code = "".join(part for i, part in enumerate(source.split('"""')) if i % 2 == 0)
    for forbidden in (
        "RTM_DELROUTE",
        "RTM_NEWLINK",
        "RTM_SETLINK",
        "RTM_DELLINK",
        "RTM_NEWADDR",
        "RTM_DELADDR",
        "RTM_NEWRULE",
        "RTM_DELRULE",
    ):
        assert forbidden not in code, f"linux_route names {forbidden}"


def test_the_provider_has_no_execution_path_at_all():
    """The docstrings say what it does not do; the code must not do it."""
    source = Path(linux_route.__file__).read_text()
    code = "".join(part for i, part in enumerate(source.split('"""')) if i % 2 == 0)
    code = "\n".join(
        line for line in code.splitlines() if not line.strip().startswith("#")
    ).lower()
    for forbidden in ("subprocess", "os.system", "popen", "shell=", "eval(", "exec(", "argv"):
        assert forbidden not in code, f"linux_route mentions {forbidden}"


def test_there_is_no_generic_netlink_entry_point():
    """One purpose, one signature. No ``call(type, payload)`` to grow into."""
    public = {name for name in dir(LinuxRouteProvider) if not name.startswith("_")}
    assert public == {"name", "version", "observe_route"}


def test_building_a_frame_takes_no_message_type():
    """The only function that produces a netlink frame cannot be asked for another kind."""
    import inspect

    assert set(inspect.signature(linux_route._request).parameters) == {"address", "family"}


# ------------------------------------------------------------------------------- probing


def test_the_capability_probe_asks_about_a_fixed_address():
    query = FakeRouteQuery({PROBE_DESTINATION: LOOPBACK_ROUTE})
    observation = probe_route(query=query)
    assert observation.status is RouteLookupStatus.RESOLVED
    assert query.requests == [PROBE_DESTINATION]
    assert PROBE_DESTINATION == "127.0.0.1"


def test_the_probe_destination_is_a_constant_and_not_a_parameter():
    import inspect

    assert "destination" not in inspect.signature(probe_route).parameters


# ------------------------------------------------------------------------ the real kernel


@pytest.mark.live
def test_the_real_kernel_answers_a_real_lookup():
    """Against this machine, read-only. Loopback is the one route every Linux host has."""
    observation = LinuxRouteProvider().observe_route("127.0.0.1")
    assert observation.status is RouteLookupStatus.RESOLVED
    assert observation.route is not None
    assert observation.route.oif_index == socket.if_nametoindex("lo")
    assert observation.route.route_type == "local"


def errno_message(code: int) -> str:
    import os

    return os.strerror(code)


def test_the_reply_must_come_from_the_kernel():
    """A netlink message from another process on this host is not evidence about routes."""
    import inspect

    source = inspect.getsource(linux_route.KernelRouteQuery)
    assert "sender[0] != 0" in source
    assert "netlink_reply_not_from_kernel" in source


def test_the_request_declares_its_own_length_correctly():
    p, query = provider(**{"192.0.2.130": netlink_route(destination="192.0.2.130", oif_index=3)})
    p.observe_route("192.0.2.130")
    frame = query.frames[0]
    (declared,) = struct.unpack_from("=I", frame, 0)
    assert declared == len(frame)
