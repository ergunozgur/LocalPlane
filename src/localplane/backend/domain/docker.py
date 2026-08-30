"""Judgements about Docker containers.

The daemon said what it is running. These functions decide what LocalPlane makes of it.
They are pure, they take facts and return a value with the reason attached, and the two
rules that run through the interface judgements run through these unchanged:

* **A reason travels with every verdict.** "failed" on its own is an opinion; "failed
  because the container's own health check has been failing" is something an operator can
  act on and something that can be shown to be wrong.
* **Absent evidence produces ``unknown``, not a default.** A container whose state could
  not be read is not healthy, and it is not failed either.

**Docker's semantics are preserved, not flattened into the interface model.** A container
is not a link: it has a lifecycle state rather than an operstate, its health is a verdict
the workload's own check produced rather than something the kernel reports, and what
LocalPlane does to it are *actions* rather than a value written into a field. Where Docker
has a concept LocalPlane does not, the concept survives; where LocalPlane has a vocabulary
that already fits — health, management, protection — Docker's answer is expressed in it
rather than in a second one.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from localplane.backend.domain.network import Verdict
from localplane.backend.domain.states import HealthState, ManagementState

OBJECT_KIND_DOCKER_CONTAINER = "docker.container"


class LifecycleState(StrEnum):
    """Docker's own container states, as the daemon reports them.

    Not LocalPlane's vocabulary and not translated into one. These are the words the
    daemon uses, and an operator reading a LocalPlane container page should see the same
    word they would see in ``docker ps``.
    """

    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    RESTARTING = "restarting"
    REMOVING = "removing"
    EXITED = "exited"
    DEAD = "dead"


#: The states in which the container's main process is not running. Derived from Docker's
#: own reporting rather than guessed: ``running`` is a boolean the daemon publishes, and
#: this set exists for the cases where a verdict has to be reached from the state name.
NOT_RUNNING = frozenset(
    {
        LifecycleState.CREATED,
        LifecycleState.EXITED,
        LifecycleState.DEAD,
        LifecycleState.REMOVING,
    }
)


class LifecycleAction(StrEnum):
    """What LocalPlane may ask Docker to do to a container. Three verbs, and that is all.

    A closed set, and closed for the same reason the operation vocabulary is: an action is
    a *name with a meaning LocalPlane implements*, and there is no escape hatch through
    which a fourth could arrive. Creating, removing, killing, pausing, renaming, updating
    and executing are not here, and nothing in this build can construct them.
    """

    START = "start"
    STOP = "stop"
    RESTART = "restart"


#: The field a lifecycle Run holds the object's write lock on. Not a controlled field —
#: LocalPlane retains no desired state for a container — but the thing being serialised:
#: two Runs must not be starting and stopping the same container at once, and the lock
#: table already answers "one mutating run per object and named aspect".
LIFECYCLE_LOCK_FIELD = "lifecycle"


def derive_container_health(facts: dict[str, object]) -> Verdict:
    """What Docker's evidence about this container amounts to.

    The order is the order the evidence stops mattering. A container that is not running
    cannot be healthy whatever its last health check said, so the lifecycle state is read
    first; a running container's own health check is then the strongest evidence available,
    and running with no check declared is as much as can be claimed — which is exactly the
    claim an interface with ``operstate up`` and nothing probing it gets.

    ``exited`` splits on the exit code because the two are genuinely different events. A
    container that ran to completion and returned zero is ``inactive``: it did what it was
    for. One that returned non-zero failed, and reporting that as merely inactive would
    lose the only signal an operator has that something is wrong.
    """
    state = facts.get("state")
    running = facts.get("running")

    if not isinstance(state, str) or not state:
        return Verdict(HealthState.UNKNOWN, "state_unreadable")

    if state == LifecycleState.RESTARTING:
        return Verdict(HealthState.DEGRADED, "restarting")
    if state == LifecycleState.PAUSED:
        return Verdict(HealthState.INACTIVE, "paused")
    if state == LifecycleState.CREATED:
        return Verdict(HealthState.INACTIVE, "never_started")
    if state == LifecycleState.REMOVING:
        return Verdict(HealthState.INACTIVE, "removing")
    if state == LifecycleState.DEAD:
        return Verdict(HealthState.FAILED, "dead")
    if state == LifecycleState.EXITED:
        code = facts.get("exit_code")
        if not isinstance(code, int):
            return Verdict(HealthState.UNKNOWN, "exited_exit_code_unreadable")
        if code == 0:
            return Verdict(HealthState.INACTIVE, "exited_cleanly")
        return Verdict(HealthState.FAILED, f"exited_with_code_{code}")

    if state == LifecycleState.RUNNING:
        if running is False:
            # The daemon contradicted itself. Naming the disagreement is more useful than
            # picking a side, exactly as it is for a link whose carrier and operstate
            # disagree.
            return Verdict(HealthState.UNKNOWN, "state_running_but_not_running")
        health = facts.get("health")
        status = health.get("status") if isinstance(health, dict) else None
        checked = health.get("checked") if isinstance(health, dict) else False
        if not checked or status is None:
            return Verdict(HealthState.HEALTHY, "running_no_healthcheck")
        if status == "healthy":
            return Verdict(HealthState.HEALTHY, "healthcheck_passing")
        if status == "unhealthy":
            return Verdict(HealthState.FAILED, "healthcheck_failing")
        if status == "starting":
            # Running, and its own check has not reached a verdict yet. Reporting that as
            # healthy would be claiming a result nobody has; reporting it as failed would be
            # inventing one.
            return Verdict(HealthState.UNKNOWN, "healthcheck_starting")
        return Verdict(HealthState.UNKNOWN, f"unrecognised_health_status:{status}")

    return Verdict(HealthState.UNKNOWN, f"unrecognised_state:{state}")


def classify_container_management(facts: dict[str, object]) -> Verdict:
    """LocalPlane's stance towards a container.

    ``observed`` — a container is something LocalPlane can act on, so it is not
    ``observe_only``, and it is not ``managed`` either, because managed means LocalPlane
    retains a desired state for it and reconciles the runtime towards that. It does not:
    a container's declaration is Docker's, its configuration is Docker's, and the three
    lifecycle actions are *actions*, not reconciliation towards a value LocalPlane holds.

    Adoption is offered on the same terms as everywhere else and refused on the same
    evidence: a container is created and configured by Docker, which is an ownership
    conflict, so adopting one is refused with ``externally_configured``. That is the
    correct answer and it comes from the ownership axis rather than from a special case
    here — the same discipline that keeps a Docker-created bridge in ``observed``.
    """
    return Verdict(ManagementState.OBSERVED, "container_lifecycle_candidate")


def is_running(facts: dict[str, object]) -> bool | None:
    """Whether the container's main process is running, or ``None`` if that is unreadable.

    The daemon publishes a boolean and a state name, and they normally agree. When only
    one is readable that one is used; when they disagree the answer is ``None``, because a
    verification that resolved a contradiction by preferring one field would be deciding
    something the evidence does not say.
    """
    running = facts.get("running")
    state = facts.get("state")
    named = state == LifecycleState.RUNNING if isinstance(state, str) and state else None
    if isinstance(running, bool) and named is not None:
        return running if running == named else None
    if isinstance(running, bool):
        return running
    return named


def parse_docker_instant(raw: Any) -> datetime | None:
    """One RFC 3339 instant as Docker writes it, or ``None``.

    Two things make this its own function rather than a call to
    :meth:`datetime.fromisoformat`. Docker reports **nanoseconds** and ``fromisoformat``
    accepts at most microseconds, so the fraction is truncated — which can only make two
    instants compare *equal* that were nanoseconds apart, a stricter answer and not a looser
    one. And Docker writes ``0001-01-01T00:00:00Z`` for "never", which is a sentinel and not
    a date; it comes back absent, because a rendering that showed the year 1 as a start time
    would be worse than a field that says it does not know.

    Everything that reads one of these timestamps reads it through here: the executor that
    proves a restart from it and the API that renders it must agree about what it says.
    """
    if not isinstance(raw, str) or not raw or raw.startswith("0001-01-01"):
        return None
    text = raw.replace("Z", "+00:00")
    head, dot, tail = text.partition(".")
    if dot:
        fraction = tail[: len(tail) - len(tail.lstrip("0123456789"))]
        text = f"{head}.{fraction[:6]:0<6}{tail[len(fraction):]}"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
