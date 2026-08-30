"""The one kernel mutation LocalPlane can perform: set an interface's MTU.

**One fixed request, built in one place.** :func:`_set_mtu_request` is the only function here
that produces a mutating netlink frame, and it takes an interface index and an MTU — not a
message type, not an attribute id, not a flag set. The type it produces is ``RTM_NEWLINK``
and the attribute is ``IFLA_MTU``, and there is no argument through which either could become
something else. ``RTM_NEWADDR``, ``RTM_NEWROUTE``, ``RTM_NEWRULE``, ``RTM_DELLINK`` and
``RTM_SETLINK`` are not spelled anywhere in this file, and there is no ``send(type, payload)``
for a caller to reach for. That is what makes this module structurally incapable of arbitrary
link or route mutation rather than merely uninterested in it — and it is why the helper does
not use a general-purpose netlink library, which would make the same guarantee a matter of
code review instead.

**No command runs.** No ``ip link set``, no argv assembled from anything, no shell, no
subprocess. What crosses to the kernel is four bytes of unsigned integer inside a frame this
module packed.

**Preconditions are checked against the kernel, not against what the caller said.** Before any
mutating frame exists the helper reads the link back with ``RTM_GETLINK`` and requires that
the index exists, the name is the one expected, the current MTU is the one expected, and the
desired value is in a bounded range and is not what is already there. A failure is
:data:`MutationOutcome.NOT_WRITTEN` with a typed reason, produced *before the mutating request
is built* — which is what makes "not written" a proof rather than an assumption.

**The acknowledgement is required, correlated and validated.** The request asks for one
(``NLM_F_ACK``), the reply must come from the kernel's own netlink port, and the echoed header
must carry this request's sequence number and message type. An acknowledgement that cannot be
correlated is not an acknowledgement, and a mutation whose acknowledgement cannot be
correlated is :data:`MutationOutcome.WRITE_UNKNOWN` — never "failed", and never resolved by
reading the MTU back.

**The transport seam keeps two failures apart**, and that split is the point:
:class:`NetlinkFailure` carries ``dispatched``, so "the request never left this process" and
"it may have left and there is no trustworthy result" cannot be collapsed by anything above.
"""

from __future__ import annotations

import errno as errno_module
import os
import socket
import struct
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from localplane.helper.protocol import MAX_MTU, MIN_MTU, MutationOutcome

PROVIDER_NAME: Final = "linux.link"
PROVIDER_VERSION: Final = "1"
SOURCE: Final = "rtnetlink"
MUTATE_METHOD: Final = "netlink_rtm_newlink_mtu"

DEFAULT_TIMEOUT_S: Final = 2.0
RECEIVE_BYTES: Final = 32 * 1024

#: ``CAP_NET_ADMIN`` from ``linux/capability.h``. The honest thing to check for: it answers
#: "could this process configure a link", which ``euid == 0`` only approximates in both
#: directions — a process can hold it without being root and be root without holding it.
CAP_NET_ADMIN: Final = 12

# --- netlink, from linux/netlink.h, linux/rtnetlink.h and linux/if_link.h ---------------

NETLINK_ROUTE: Final = 0
NLM_F_REQUEST: Final = 0x01
NLM_F_ACK: Final = 0x04
NLMSG_ERROR: Final = 0x02
NLMSG_DONE: Final = 0x03

#: The only two message types this module knows. Neither is a parameter anywhere.
RTM_NEWLINK: Final = 16
RTM_GETLINK: Final = 18

_NLMSGHDR = struct.Struct("=IHHII")
_IFINFOMSG = struct.Struct("=BBHiII")
_RTATTR = struct.Struct("=HH")

IFLA_IFNAME: Final = 3
IFLA_MTU: Final = 4


class Refused(Exception):
    """A check refused before any mutating frame was built. Nothing was sent.

    Covers both halves of the precondition: the link read that could not be made, and the
    comparison that did not hold. Both mean the same thing to a caller — the outcome is
    :data:`MutationOutcome.NOT_WRITTEN`, provably.
    """

    def __init__(self, reason: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


class NetlinkFailure(Exception):
    """The netlink exchange did not complete, and whether it had begun.

    ``dispatched`` is the whole reason this is one class rather than a bare error: false means
    the kernel was never given the opportunity to accept anything, true means it may already
    have. Everything after ``send`` returns carries true.
    """

    def __init__(
        self, reason: str, detail: dict[str, Any] | None = None, dispatched: bool = False
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}
        self.dispatched = dispatched


class LinkTransport(Protocol):
    """This module's whole I/O: one read, one write, and whether a write is possible."""

    @property
    def can_mutate(self) -> bool:
        """Whether this process could have a link mutation accepted at all.

        Asked of the *transport* because the transport is what reaches the kernel, so a seam
        that replaces the kernel replaces this answer with it.
        """
        ...

    def query(self, request: bytes) -> bytes: ...

    def mutate(self, request: bytes) -> bytes: ...


@dataclass(frozen=True)
class LinkFacts:
    """One link as the kernel described it. Read for preconditions, never for a verdict."""

    ifindex: int
    name: str | None
    mtu: int | None


@dataclass(frozen=True)
class MutationResult:
    """What became of one mutation attempt, and the evidence for saying so.

    A report, not a verdict. Whether the target now holds the wanted value is a separate
    question with a separate answer, and nothing here may be read as having answered it.
    """

    outcome: MutationOutcome
    reason: str
    attempt_id: str
    ifindex: int
    expected_current_mtu: int
    desired_mtu: int
    observed_name: str | None = None
    observed_mtu: int | None = None
    kernel_errno: int | None = None
    kernel_error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": str(self.outcome),
            "reason": self.reason,
            "attempt_id": self.attempt_id,
            "ifindex": self.ifindex,
            "expected_current_mtu": self.expected_current_mtu,
            "desired_mtu": self.desired_mtu,
            "provider": PROVIDER_NAME,
            "provider_version": PROVIDER_VERSION,
            "source": SOURCE,
            "method": MUTATE_METHOD,
            "observed_name": self.observed_name,
            "observed_mtu": self.observed_mtu,
            "kernel_errno": self.kernel_errno,
            "kernel_error": self.kernel_error,
            "detail": self.detail,
        }


class InterfaceMtuSetter:
    """Set one interface's MTU, having first proved the kernel agrees about the interface."""

    provider = PROVIDER_NAME
    version = PROVIDER_VERSION

    def __init__(
        self, transport: LinkTransport | None = None, timeout_s: float = DEFAULT_TIMEOUT_S
    ) -> None:
        self._transport = transport if transport is not None else KernelLinkTransport(timeout_s)
        self._sequence = 0

    @property
    def can_mutate(self) -> bool:
        return bool(self._transport.can_mutate)

    def read_link(self, ifindex: int) -> LinkFacts:
        """Ask the kernel about one link by index. Raises :class:`Refused`."""
        self._sequence += 1
        return _interpret_link(
            self._transport.query(_get_link_request(ifindex, self._sequence)),
            ifindex,
            self._sequence,
        )

    def set_mtu(
        self,
        *,
        attempt_id: str,
        ifindex: int,
        expected_current_mtu: int,
        desired_mtu: int,
        expected_interface_name: str | None = None,
    ) -> MutationResult:
        """Set ``ifindex``'s MTU, or say precisely why it did not happen.

        The order is the safety argument. Everything that can be refused is refused before a
        mutating frame exists, so a refusal is ``not_written`` by construction. After the
        frame is dispatched there are exactly two honest answers — the kernel acknowledged, or
        LocalPlane does not know — and which one is decided by whether an acknowledgement
        arrived that this request can be shown to own.
        """
        def result(outcome: MutationOutcome, reason: str, **extra: Any) -> MutationResult:
            return MutationResult(
                outcome=outcome, reason=reason, attempt_id=attempt_id, ifindex=ifindex,
                expected_current_mtu=expected_current_mtu, desired_mtu=desired_mtu, **extra,
            )

        def seen(link: LinkFacts) -> dict[str, Any]:
            return {"observed_name": link.name, "observed_mtu": link.mtu}

        try:
            link = self._check(ifindex, expected_current_mtu, desired_mtu,
                               expected_interface_name)
        except Refused as exc:
            return result(
                MutationOutcome.NOT_WRITTEN, exc.reason,
                observed_name=exc.detail.get("observed_name"),
                observed_mtu=exc.detail.get("observed_mtu"),
                kernel_errno=exc.detail.get("kernel_errno"),
                kernel_error=exc.detail.get("kernel_error"),
                detail=exc.detail,
            )

        self._sequence += 1
        sequence = self._sequence
        try:
            reply = self._transport.mutate(_set_mtu_request(ifindex, desired_mtu, sequence))
        except NetlinkFailure as exc:
            outcome = (
                MutationOutcome.WRITE_UNKNOWN if exc.dispatched else MutationOutcome.NOT_WRITTEN
            )
            return result(outcome, exc.reason, detail=exc.detail, **seen(link))

        try:
            code = _acknowledgement(reply, sequence)
        except NetlinkFailure as exc:
            return result(MutationOutcome.WRITE_UNKNOWN, exc.reason,
                          detail=exc.detail, **seen(link))
        if code == 0:
            return result(MutationOutcome.WRITTEN, "kernel_acknowledged", **seen(link))
        return result(MutationOutcome.NOT_WRITTEN, "kernel_rejected",
                      kernel_errno=code, kernel_error=os.strerror(code), **seen(link))

    def _check(
        self, ifindex: int, expected: int, desired: int, expected_name: str | None
    ) -> LinkFacts:
        """Everything that can refuse this, refused before a mutating frame can exist."""
        if not MIN_MTU <= desired <= MAX_MTU:
            raise Refused("mtu_out_of_range",
                          {"desired_mtu": desired, "minimum": MIN_MTU, "maximum": MAX_MTU})
        if not MIN_MTU <= expected <= MAX_MTU:
            raise Refused("expected_mtu_out_of_range",
                          {"expected_current_mtu": expected, "minimum": MIN_MTU,
                           "maximum": MAX_MTU})
        if desired == expected:
            raise Refused("desired_equals_current",
                          {"desired_mtu": desired, "expected_current_mtu": expected})

        link = self.read_link(ifindex)
        if link.mtu is None:
            raise Refused("current_mtu_unreadable",
                          {"ifindex": ifindex, "observed_name": link.name})
        if expected_name is not None and link.name != expected_name:
            # The index is the identity; the name is a second, independent guard against an
            # index recycled onto a different link between the plan and the write. It is
            # exactly as strong as LocalPlane's own object identity for a link with no
            # permanent address, which is what a disposable link is.
            raise Refused("interface_identity_mismatch",
                          {"ifindex": ifindex, "expected_interface_name": expected_name,
                           "observed_name": link.name, "observed_mtu": link.mtu})
        if link.mtu != expected:
            # The final race guard, and the one the kernel itself enforces for LocalPlane:
            # between the plan and this instant somebody may have set another value, and
            # writing over it would be acting on a world that has moved.
            raise Refused("mtu_precondition_failed",
                          {"ifindex": ifindex, "expected_current_mtu": expected,
                           "observed_mtu": link.mtu, "observed_name": link.name})
        if link.mtu == desired:
            raise Refused("desired_equals_current",
                          {"ifindex": ifindex, "observed_mtu": link.mtu, "desired_mtu": desired})
        return link


class KernelLinkTransport:
    """The real netlink socket. One datagram out, one back, per call.

    Opened, used and closed per exchange: a mutation happens a handful of times a day at most,
    and a held descriptor would add a class of stale-socket bug for nothing measurable.

    The reply must come from netlink source port zero. Any process on this host may open a
    netlink socket, and an acknowledgement from one of them is not evidence the kernel
    accepted anything.
    """

    def __init__(self, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout_s = timeout_s

    @property
    def can_mutate(self) -> bool:
        return has_net_admin()

    def query(self, request: bytes) -> bytes:
        try:
            return self._exchange(request, dispatched_after_send=False)
        except NetlinkFailure as exc:
            raise Refused(exc.reason, exc.detail) from exc

    def mutate(self, request: bytes) -> bytes:
        """Send the mutating frame. Everything after ``send`` returns is ``dispatched``."""
        return self._exchange(request, dispatched_after_send=True)

    def _exchange(self, request: bytes, *, dispatched_after_send: bool) -> bytes:
        try:
            sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_ROUTE)
        except (AttributeError, OSError) as exc:
            raise NetlinkFailure("netlink_unavailable", {"error": _describe(exc)}) from exc
        try:
            sock.settimeout(self._timeout_s)
            try:
                sock.bind((0, 0))
                sock.send(request)
            except OSError as exc:
                raise NetlinkFailure("netlink_send_failed", {"error": _describe(exc)}) from exc
            # Past this line the kernel may have accepted the write.
            dispatched = dispatched_after_send
            try:
                payload, sender = sock.recvfrom(RECEIVE_BYTES)
            except socket.timeout as exc:
                raise NetlinkFailure("acknowledgement_timeout", {"timeout_s": self._timeout_s},
                                     dispatched) from exc
            except OSError as exc:
                raise NetlinkFailure("acknowledgement_not_received",
                                     {"error": _describe(exc)}, dispatched) from exc
        finally:
            sock.close()

        if not (isinstance(sender, tuple) and sender and sender[0] == 0):
            raise NetlinkFailure("acknowledgement_not_from_kernel",
                                 {"sender_port_id": sender[0] if sender else None}, dispatched)
        return payload


# ------------------------------------------------------------------------ frame builders


def _get_link_request(ifindex: int, sequence: int) -> bytes:
    """Build the read: ``RTM_GETLINK`` for one index. Takes no message type."""
    body = _IFINFOMSG.pack(0, 0, 0, ifindex, 0, 0)
    return _NLMSGHDR.pack(
        _NLMSGHDR.size + len(body), RTM_GETLINK, NLM_F_REQUEST, sequence, 0
    ) + body


def _set_mtu_request(ifindex: int, mtu: int, sequence: int) -> bytes:
    """Build the one mutating message this package can send.

    The message type is ``RTM_NEWLINK`` and the attribute is ``IFLA_MTU``, both constants.
    There is no parameter for either, so there is no call site that could turn this into a
    link deletion, an address change or a route write.

    ``ifi_change`` is left zero: this request changes an attribute, not a flag, and a non-zero
    change mask is how a link is brought up or down. Not asking is stronger than asking for
    nothing.
    """
    body = _IFINFOMSG.pack(0, 0, 0, ifindex, 0, 0) + _attribute(
        IFLA_MTU, struct.pack("=I", mtu)
    )
    return _NLMSGHDR.pack(
        _NLMSGHDR.size + len(body), RTM_NEWLINK, NLM_F_REQUEST | NLM_F_ACK, sequence, 0
    ) + body


def _attribute(kind: int, payload: bytes) -> bytes:
    raw = _RTATTR.pack(_RTATTR.size + len(payload), kind) + payload
    return raw + b"\0" * (-len(raw) % 4)


# ----------------------------------------------------------------------------- parsing


def _interpret_link(reply: bytes, ifindex: int, sequence: int) -> LinkFacts:
    """The kernel's answer to a link query, or the reason there is none."""
    for kind, header_sequence, body in _messages(reply):
        if kind == NLMSG_ERROR:
            (code,) = struct.unpack_from("=i", body, 0)
            if code == 0:
                continue
            number = -code
            raise Refused(
                "interface_not_found" if number == errno_module.ENODEV else "link_query_rejected",
                {"kernel_errno": number, "kernel_error": os.strerror(number), "ifindex": ifindex},
            )
        if kind == RTM_NEWLINK:
            if header_sequence != sequence:
                raise Refused("link_reply_uncorrelated",
                              {"expected_sequence": sequence, "received_sequence": header_sequence})
            facts = _link_facts(body)
            if facts is None:
                break
            if facts.ifindex != ifindex:
                raise Refused("link_reply_wrong_interface",
                              {"requested_ifindex": ifindex, "reply_ifindex": facts.ifindex})
            return facts
    raise Refused("no_link_in_reply", {"ifindex": ifindex})


def _acknowledgement(reply: bytes, sequence: int) -> int:
    """The kernel's errno for this request, or raise because it cannot be correlated.

    Zero means accepted. A positive value means the kernel parsed the request, named it, and
    refused before applying anything — a definite negative. Anything that is not a correlated
    acknowledgement leaves the question open, and an open question after dispatch is
    ``write_unknown``, which is what raising here becomes.
    """
    messages = list(_messages(reply, dispatched=True))
    for kind, header_sequence, body in messages:
        if kind != NLMSG_ERROR:
            continue
        if header_sequence != sequence:
            raise NetlinkFailure(
                "acknowledgement_uncorrelated",
                {"expected_sequence": sequence, "received_sequence": header_sequence}, True)
        echoed = _echoed_request(body)
        if echoed is not None and echoed != (RTM_NEWLINK, sequence):
            raise NetlinkFailure(
                "acknowledgement_echoes_another_request",
                {"expected": [RTM_NEWLINK, sequence], "echoed_type": echoed[0],
                 "echoed_sequence": echoed[1]}, True)
        (code,) = struct.unpack_from("=i", body, 0)
        return -code
    raise NetlinkFailure("no_acknowledgement_in_reply",
                         {"message_types": sorted({k for k, _s, _b in messages})}, True)


def _messages(reply: bytes, *, dispatched: bool = False):
    """Split a netlink datagram into ``(type, sequence, body)``, refusing a malformed one.

    A malformation means different things on the two halves: an unreadable *query* reply is a
    failed read (:class:`Refused`), and an unreadable *mutation* reply is a write whose
    outcome is unknown.
    """
    offset = 0
    while offset + _NLMSGHDR.size <= len(reply):
        length, kind, _flags, sequence, _pid = _NLMSGHDR.unpack_from(reply, offset)
        if length < _NLMSGHDR.size or offset + length > len(reply):
            detail = {"declared_length": length, "remaining": len(reply) - offset}
            raise (
                NetlinkFailure("malformed_netlink_response", detail, True)
                if dispatched
                else Refused("malformed_netlink_response", detail)
            )
        if kind == NLMSG_DONE:
            return
        yield kind, sequence, reply[offset + _NLMSGHDR.size : offset + length]
        offset += (length + 3) & ~3


def _echoed_request(body: bytes) -> tuple[int, int] | None:
    """The header ``nlmsgerr`` echoes back, as ``(type, sequence)``, when it carries one."""
    if len(body) < 4 + _NLMSGHDR.size:
        return None
    _length, kind, _flags, sequence, _pid = _NLMSGHDR.unpack_from(body, 4)
    return kind, sequence


def _link_facts(body: bytes) -> LinkFacts | None:
    if len(body) < _IFINFOMSG.size:
        return None
    _family, _pad, _type, ifindex, _flags, _change = _IFINFOMSG.unpack_from(body, 0)
    attributes = _attributes(body, _IFINFOMSG.size)
    return LinkFacts(
        ifindex=ifindex,
        name=_string(attributes.get(IFLA_IFNAME)),
        mtu=_uint32(attributes.get(IFLA_MTU)),
    )


def _attributes(body: bytes, offset: int) -> dict[int, bytes]:
    found: dict[int, bytes] = {}
    while offset + _RTATTR.size <= len(body):
        length, kind = _RTATTR.unpack_from(body, offset)
        if length < _RTATTR.size or offset + length > len(body):
            break
        found[kind] = body[offset + _RTATTR.size : offset + length]
        offset += (length + 3) & ~3
    return found


def _string(payload: bytes | None) -> str | None:
    if payload is None:
        return None
    try:
        return payload.split(b"\0", 1)[0].decode("utf-8")
    except UnicodeDecodeError:
        return None


def _uint32(payload: bytes | None) -> int | None:
    if payload is None or len(payload) < 4:
        return None
    return int(struct.unpack_from("=I", payload, 0)[0])


def has_net_admin() -> bool:
    """Whether this process holds ``CAP_NET_ADMIN`` in its effective set.

    Read from ``/proc/self/status``, which is the kernel's own answer, rather than inferred
    from the effective uid. Anything unreadable is ``False``: not being able to establish a
    privilege is not the same as having it.
    """
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("CapEff:"):
                    return bool(int(line.split()[1], 16) & (1 << CAP_NET_ADMIN))
    except (OSError, ValueError, IndexError):
        return False
    return False


def _describe(exc: BaseException) -> str:
    return exc.strerror or str(exc) if isinstance(exc, OSError) else str(exc)
