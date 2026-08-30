"""Helper operation dispatch. Two methods, one of which writes.

The handler table below is the contract. A method that is not in it does not exist, and
adding one is a visible change to this file — which for a process running as root is the
whole point of writing the dispatch out longhand rather than resolving a name.

**Parameters are validated, typed and bounded before anything happens.** ``ifindex``,
``expected_current_mtu`` and ``desired_mtu`` must be integers in range; ``attempt_id`` must
be a short opaque string. Booleans are not integers here, because ``True`` arriving where
an MTU belongs is a caller bug and coercing it would be this file's.

**An attempt is dispatched at most once.** ``attempt_id`` is remembered with the outcome it
produced, so a client that retries after losing its answer is told what already happened
instead of dispatching a second write. That is the narrowest form of the idempotency rule:
not a framework, just the one place where a retry would otherwise
turn one write into two and one history into an ambiguous one.
"""

from __future__ import annotations

import os
import re
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Callable, Final

from localplane import __version__
from localplane.helper.mtu import (
    MUTATE_METHOD,
    PROVIDER_NAME,
    PROVIDER_VERSION,
    InterfaceMtuSetter,
    LinkTransport,
    MutationResult,
)
from localplane.helper.protocol import (
    HELPER_METHODS,
    HELPER_PROTOCOL_VERSION,
    MAX_MTU,
    MIN_MTU,
    MUTATING_HELPER_METHODS,
    HelperErrorCode,
    HelperMethod,
    HelperProtocolError,
)

#: How many completed attempts are remembered. Small on purpose: this exists to catch a
#: client retrying the request it just made, not to be a durable ledger. The durable
#: record of what LocalPlane attempted lives in the backend's `changes` table.
ATTEMPT_MEMORY = 256

_ATTEMPT_ID_RE: Final = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

MAX_IFINDEX: Final = 2**31 - 1


class HelperService:
    """Answers helper protocol methods. Constructed once per helper process."""

    def __init__(
        self,
        transport: LinkTransport | None = None,
        instance_id: str | None = None,
    ) -> None:
        self._instance_id = instance_id or f"helper_{uuid.uuid4().hex}"
        self._started_at = _now()
        self._setter = InterfaceMtuSetter(transport=transport)
        self._attempts: "OrderedDict[str, MutationResult]" = OrderedDict()
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            HelperMethod.HELLO: self._hello,
            HelperMethod.SET_INTERFACE_MTU: self._set_interface_mtu,
        }
        missing = HELPER_METHODS - set(self._handlers)
        if missing:
            raise RuntimeError(f"helper methods without a handler: {sorted(missing)}")

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def methods(self) -> list[str]:
        return sorted(HELPER_METHODS)

    @property
    def mutating_methods(self) -> list[str]:
        return sorted(MUTATING_HELPER_METHODS)

    def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        handler = self._handlers.get(method)
        if handler is None:
            raise HelperProtocolError(
                HelperErrorCode.UNSUPPORTED_METHOD,
                f"unsupported helper method: {method!r}",
                {"method": method, "supported": sorted(HELPER_METHODS)},
            )
        return handler(params)

    # ------------------------------------------------------------------------- methods

    def _hello(self, _params: dict[str, Any]) -> dict[str, Any]:
        """What this helper is. Reads nothing about the host and writes nothing.

        ``can_configure_network`` is the load-bearing answer and it is a fact about the path
        to the kernel rather than about the uid: the transport reports whether a link
        mutation could be accepted at all. A helper that answers and cannot configure links
        is reported by the agent as *degraded* — reachable, speaking, and unable to do the
        thing — which is more useful than either "available" or "absent" and is the whole
        reason the probe asks instead of writing something to find out.

        ``privilege`` and ``effective_uid`` are reported beside it as description. They are
        not what decides.
        """
        euid = os.geteuid()
        return {
            "helper_instance_id": self._instance_id,
            "helper_version": __version__,
            "protocol_version": HELPER_PROTOCOL_VERSION,
            "started_at": self._started_at,
            "pid": os.getpid(),
            "effective_uid": euid,
            "privilege": "root" if euid == 0 else "unprivileged",
            "can_configure_network": self._setter.can_mutate,
            "methods": self.methods,
            "mutating_methods": self.mutating_methods,
            "provider": PROVIDER_NAME,
            "provider_version": PROVIDER_VERSION,
            "mutate_method": MUTATE_METHOD,
            "mtu_range": {"minimum": MIN_MTU, "maximum": MAX_MTU},
        }

    def _set_interface_mtu(self, params: dict[str, Any]) -> dict[str, Any]:
        """The one mutating operation. Typed scalars in, one typed outcome out.

        It makes no product decision. Whether this interface may be written to, whether an
        operator confirmed it, whether it carries the management path and whether a
        checkpoint exists are all questions the backend answered before this request was
        built. What is checked here is the kernel's own agreement about the interface, which
        is the one thing the backend cannot check without a race.
        """
        attempt_id = _require_attempt_id(params.get("attempt_id"))
        ifindex = _require_int(params.get("ifindex"), "ifindex", minimum=1, maximum=MAX_IFINDEX)
        expected_current_mtu = _require_int(
            params.get("expected_current_mtu"), "expected_current_mtu", minimum=0, maximum=MAX_MTU
        )
        desired_mtu = _require_int(
            params.get("desired_mtu"), "desired_mtu", minimum=0, maximum=MAX_MTU
        )
        expected_interface_name = _optional_name(params.get("expected_interface_name"))

        remembered = self._attempts.get(attempt_id)
        if remembered is not None:
            # The same attempt, asked again. Answering with what happened is the only safe
            # response: dispatching a second mutating request would turn one write into
            # two and one history into a history nobody can reconstruct.
            return {**remembered.as_dict(), "replayed": True}

        result = self._setter.set_mtu(
            attempt_id=attempt_id,
            ifindex=ifindex,
            expected_current_mtu=expected_current_mtu,
            desired_mtu=desired_mtu,
            expected_interface_name=expected_interface_name,
        )
        self._remember(attempt_id, result)
        return {**result.as_dict(), "replayed": False}

    def _remember(self, attempt_id: str, result: MutationResult) -> None:
        self._attempts[attempt_id] = result
        while len(self._attempts) > ATTEMPT_MEMORY:
            self._attempts.popitem(last=False)


def _require_attempt_id(value: Any) -> str:
    if not isinstance(value, str) or not _ATTEMPT_ID_RE.match(value):
        raise HelperProtocolError(
            HelperErrorCode.INVALID_PARAMS,
            "attempt_id must be a short opaque identifier",
            {"pattern": _ATTEMPT_ID_RE.pattern},
        )
    return value


def _require_int(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    # `bool` is a subclass of `int`, and `True` arriving where an MTU belongs is a caller
    # bug. Coercing it would make this file the place the bug became invisible.
    if isinstance(value, bool) or not isinstance(value, int):
        raise HelperProtocolError(
            HelperErrorCode.INVALID_PARAMS, f"{name} must be an integer", {"parameter": name}
        )
    if not minimum <= value <= maximum:
        raise HelperProtocolError(
            HelperErrorCode.INVALID_PARAMS,
            f"{name} is outside the range this helper accepts",
            {"parameter": name, "value": value, "minimum": minimum, "maximum": maximum},
        )
    return value


def _optional_name(value: Any) -> str | None:
    if value is None:
        return None
    # IFNAMSIZ is 16 including the terminator, so 15 characters is the kernel's ceiling.
    if not isinstance(value, str) or not 1 <= len(value) <= 15:
        raise HelperProtocolError(
            HelperErrorCode.INVALID_PARAMS,
            "expected_interface_name must be a kernel interface name",
            {"parameter": "expected_interface_name"},
        )
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
