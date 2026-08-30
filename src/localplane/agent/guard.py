"""The host side of the connection guard: an armed reversal with a deadline.

**This is the component the guard exists to be.** LocalPlane's backend is expected to run in
a container; the agent is expected to run on the host it manages. A guard held in the
backend would be a guard that disappears with the thing whose mistake it is supposed to
survive, and a guard held in the operator's connection would be a guard that dies with
exactly the connection it is protecting. So it is held here, in a long-lived host-side
process, and it needs nobody's permission and nobody's answer to act.

**What it holds is not a decision.** Everything about the reversal — which link, which
value, which identity guard, which attempt id, how long the window is — is fixed by the
backend at arming time from a durable checkpoint, and this module cannot change any of it,
cannot compute a different one and has no parameter through which a caller could supply
one. What it contributes is the passage of time and a call to the privileged helper that
the agent could already make.

**It is not a scheduler and not a watchdog framework.** A guard is armed by one request,
holds one reversal, and settles exactly once. There is no queue, no recurring work, no
policy, no retry and nothing that reads the state of the host to decide anything.

**Its failure mode is a no-op, structurally.** The reversal is dispatched with the value the
change was supposed to leave behind as its compare-and-set precondition, so a guard that
fires when the change never landed — or after somebody else has moved the value — is
refused by the privileged helper before a mutating frame exists. A guard can only undo the
change it was armed for.

Standard library only, like the rest of the agent.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

LOG = logging.getLogger("localplane.agent.guard")

#: The longest window this component will hold, whatever it is asked for. A guard is a
#: bounded outage, and a bound nobody can exceed is what makes that sentence true even if
#: the backend asks for something absurd. It is not a tunable and there is no setting.
MAX_WINDOW_S = 600

#: The shortest window worth holding. Below this the reversal would fire while the change
#: it guards is still being dispatched, which is a mechanism that reverts its own work.
MIN_WINDOW_S = 5

#: How many settled guards are remembered so their outcome can still be collected. A guard
#: settles in minutes and is collected by the next request that asks; this exists so a
#: backend that was restarting while one fired can still find out what it did.
SETTLED_MEMORY = 64

#: What a caller is told about a guard this process has never held or no longer remembers.
LOST = "lost"

ARMING_REFUSED_WINDOW = "guard_window_out_of_range"
ARMING_REFUSED_TARGET_HELD = "guard_already_armed_for_target"
ARMING_REFUSED_DUPLICATE = "guard_already_armed"
ARMING_REFUSED_NOTHING_TO_REVERT = "guard_reversal_would_change_nothing"


@dataclass
class _Guard:
    """One armed reversal. Everything in it was decided by the backend."""

    guard_id: str
    attempt_id: str
    ifindex: int
    expected_interface_name: str | None
    guarded_mtu: int
    restore_mtu: int
    window_s: int
    armed_at: datetime
    expires_at: datetime
    phase: str = "armed"
    fired_at: str | None = None
    mutation: dict[str, Any] | None = None
    timer: threading.Timer | None = field(default=None, repr=False)

    def report(self, holder_id: str) -> dict[str, Any]:
        return {
            "guard_id": self.guard_id,
            "phase": self.phase,
            "holder_id": holder_id,
            "attempt_id": self.attempt_id,
            "ifindex": self.ifindex,
            "guarded_mtu": self.guarded_mtu,
            "restore_mtu": self.restore_mtu,
            "window_s": self.window_s,
            "armed_at": _stamp(self.armed_at),
            "expires_at": _stamp(self.expires_at),
            "fired_at": self.fired_at,
            "mutation": self.mutation,
        }


class GuardRegistry:
    """Every guard this agent instance is holding, and the ones it has settled.

    One lock covers the whole registry and is held for the duration of a firing. That is
    deliberate: a disarm arriving while the reversal is in flight blocks until the reversal
    has an outcome, so the answer a caller gets is always a settled one. A third phase
    meaning "acting, ask again" would be a state the backend would have to poll, and a
    guard that has to be polled to be understood is a guard whose result can be missed.
    """

    def __init__(
        self,
        holder_id: str,
        mutate: Callable[..., dict[str, Any]],
        clock: Callable[[], datetime] | None = None,
        timer_factory: Callable[[float, Callable[[], None]], Any] | None = None,
    ) -> None:
        self._holder_id = holder_id
        #: The one thing this registry can do to the host, injected rather than reached for:
        #: the agent's own typed MTU write, with its own three-valued outcome. There is no
        #: other callable here and no way to supply one from a request.
        self._mutate = mutate
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._timer_factory = timer_factory or _real_timer
        self._lock = threading.RLock()
        self._armed: dict[str, _Guard] = {}
        self._settled: OrderedDict[str, _Guard] = OrderedDict()

    # ---------------------------------------------------------------------------- arm

    def arm(
        self,
        *,
        guard_id: str,
        attempt_id: str,
        ifindex: int,
        expected_interface_name: str | None,
        guarded_mtu: int,
        restore_mtu: int,
        window_s: int,
    ) -> dict[str, Any]:
        """Hold a reversal for ``window_s`` seconds, and say so.

        Refuses rather than adjusts. A window outside the bounds this module will hold, a
        second guard for a target already guarded, a guard id already known, and a reversal
        that would write the value it expects to find are each a typed refusal — because a
        guard silently narrowed, widened or deduplicated is a guard whose behaviour the
        backend's durable record no longer describes.
        """
        if not MIN_WINDOW_S <= window_s <= MAX_WINDOW_S:
            raise GuardRefusal(
                ARMING_REFUSED_WINDOW,
                {"window_s": window_s, "min_s": MIN_WINDOW_S, "max_s": MAX_WINDOW_S},
            )
        if guarded_mtu == restore_mtu:
            raise GuardRefusal(
                ARMING_REFUSED_NOTHING_TO_REVERT,
                {"guarded_mtu": guarded_mtu, "restore_mtu": restore_mtu},
            )
        with self._lock:
            if guard_id in self._armed or guard_id in self._settled:
                raise GuardRefusal(ARMING_REFUSED_DUPLICATE, {"guard_id": guard_id})
            held = next((g for g in self._armed.values() if g.ifindex == ifindex), None)
            if held is not None:
                raise GuardRefusal(
                    ARMING_REFUSED_TARGET_HELD,
                    {"ifindex": ifindex, "held_by_guard_id": held.guard_id},
                )
            now = self._clock()
            guard = _Guard(
                guard_id=guard_id,
                attempt_id=attempt_id,
                ifindex=ifindex,
                expected_interface_name=expected_interface_name,
                guarded_mtu=guarded_mtu,
                restore_mtu=restore_mtu,
                window_s=window_s,
                armed_at=now,
                expires_at=now + timedelta(seconds=window_s),
            )
            guard.timer = self._timer_factory(float(window_s), lambda: self._fire(guard_id))
            self._armed[guard_id] = guard
            LOG.info(
                "connection guard armed",
                extra={
                    "guard_id": guard_id,
                    "ifindex": ifindex,
                    "restore_mtu": restore_mtu,
                    "window_s": window_s,
                    "expires_at": _stamp(guard.expires_at),
                },
            )
            return guard.report(self._holder_id)

    # ------------------------------------------------------------------------- disarm

    def disarm(self, guard_id: str) -> dict[str, Any]:
        """Release a guard that is still holding, and report what became of it.

        Releasing a guard that has already fired is not an error and does not undo
        anything: the answer is the report of what it did. A guard this process does not
        know is ``lost``, which is a different answer from "there is no such guard" — the
        change it was armed for may well have happened.
        """
        with self._lock:
            guard = self._armed.get(guard_id)
            if guard is not None:
                if guard.timer is not None:
                    guard.timer.cancel()
                    guard.timer = None
                guard.phase = "disarmed"
                self._settle(guard)
                LOG.info("connection guard disarmed", extra={"guard_id": guard_id})
                return guard.report(self._holder_id)
            settled = self._settled.get(guard_id)
            if settled is not None:
                return settled.report(self._holder_id)
            return _lost(guard_id, self._holder_id)

    # ------------------------------------------------------------------------- status

    def status(self, guard_id: str) -> dict[str, Any]:
        """What a guard is doing, without changing it. Never releases anything.

        Separate from :meth:`disarm` on purpose. A backend coming back from a restart has to
        find out where a guard stands *without* cancelling protection that is still running,
        and one method that sometimes cancelled would be one call away from doing exactly
        that.
        """
        with self._lock:
            guard = self._armed.get(guard_id) or self._settled.get(guard_id)
            if guard is None:
                return _lost(guard_id, self._holder_id)
            return guard.report(self._holder_id)

    # --------------------------------------------------------------------------- fire

    def _fire(self, guard_id: str) -> None:
        """The deadline passed and nothing proved the connection. Put the value back.

        Nothing is decided here. The reversal's parameters were fixed when the guard was
        armed, and the compare-and-set precondition is the value the change was supposed to
        leave behind — so a reversal that would write over anything else is refused by the
        privileged helper before a mutating frame exists.
        """
        with self._lock:
            guard = self._armed.get(guard_id)
            if guard is None:
                return
            guard.timer = None
            guard.phase = "fired"
            guard.fired_at = _stamp(self._clock())
            LOG.warning(
                "connection guard fired; reverting",
                extra={
                    "guard_id": guard_id,
                    "ifindex": guard.ifindex,
                    "restore_mtu": guard.restore_mtu,
                    "window_s": guard.window_s,
                },
            )
            guard.mutation = self._mutate(
                attempt_id=guard.attempt_id,
                ifindex=guard.ifindex,
                expected_current_mtu=guard.guarded_mtu,
                desired_mtu=guard.restore_mtu,
                expected_interface_name=guard.expected_interface_name,
            )
            self._settle(guard)
            LOG.warning(
                "connection guard reversal recorded",
                extra={
                    "guard_id": guard_id,
                    "outcome": (guard.mutation or {}).get("outcome"),
                    "reason": (guard.mutation or {}).get("reason"),
                },
            )

    # ---------------------------------------------------------------------- internals

    def _settle(self, guard: _Guard) -> None:
        self._armed.pop(guard.guard_id, None)
        self._settled[guard.guard_id] = guard
        while len(self._settled) > SETTLED_MEMORY:
            self._settled.popitem(last=False)

    def shutdown(self) -> None:
        """Cancel every timer. Used when a process is being torn down deliberately.

        It performs no reversal and claims none: a guard whose process is stopped has
        stopped protecting, and pretending otherwise on the way out would be the one lie
        this component must not tell.
        """
        with self._lock:
            for guard in self._armed.values():
                if guard.timer is not None:
                    guard.timer.cancel()
                    guard.timer = None


class GuardRefusal(RuntimeError):
    """The registry would not arm a guard, and nothing has been held."""

    def __init__(self, code: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail or {}


def _lost(guard_id: str, holder_id: str) -> dict[str, Any]:
    return {
        "guard_id": guard_id,
        "phase": LOST,
        "holder_id": holder_id,
        "attempt_id": None,
        "ifindex": None,
        "guarded_mtu": None,
        "restore_mtu": None,
        "window_s": None,
        "armed_at": None,
        "expires_at": None,
        "fired_at": None,
        "mutation": None,
    }


def _real_timer(delay_s: float, action: Callable[[], None]) -> threading.Timer:
    timer = threading.Timer(delay_s, action)
    timer.daemon = True
    timer.start()
    return timer


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")
