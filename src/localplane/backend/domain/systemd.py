"""LocalPlane judgements over systemd's normalised unit facts.

systemd state strings are deliberately not enums here.  New values are safe provider data,
not parse errors; values this build does not understand produce an ``unknown`` health
verdict with the original value preserved in the facts.
"""

from __future__ import annotations

from typing import Any, Iterable

from localplane.backend.domain.network import Verdict
from localplane.backend.domain.states import HealthState, ManagementState


def classify_systemd_management(facts: dict[str, Any]) -> Verdict:
    """Units can be acted on through the lifecycle executor, but not adopted:
    observation stays observe-only on the intent axis."""
    return Verdict(ManagementState.OBSERVE_ONLY, "systemd_observation_only")


def derive_systemd_health(
    facts: dict[str, Any], *, active_socket_triggers: Iterable[str] = ()
) -> Verdict:
    """Map one unit with type-aware, deliberately conservative semantics.

    ``active_socket_triggers`` contains canonical socket ids that currently activate this
    unit.  It refines the reason for an inactive on-demand service without pretending that
    the service process itself is running.  Callers without coherent cross-object evidence
    simply omit it and receive the neutral ``inactive`` verdict.
    """
    unit_type = facts.get("unit_type")
    load = facts.get("load_state")
    active = facts.get("active_state")
    sub = facts.get("sub_state")

    if not isinstance(unit_type, str) or not isinstance(active, str) or not active:
        return Verdict(HealthState.UNKNOWN, "systemd_state_unreadable")
    if isinstance(load, str) and load not in {"loaded", "stub", "merged"}:
        if load in {"error", "not-found", "bad-setting"}:
            return Verdict(HealthState.FAILED, f"load_state:{load}")
        return Verdict(HealthState.UNKNOWN, f"unrecognised_load_state:{load}")
    if active == "failed":
        return Verdict(HealthState.FAILED, f"failed:{sub or 'unknown'}")
    if active in {"activating", "deactivating", "reloading", "refreshing"}:
        return Verdict(HealthState.DEGRADED, f"transition:{active}:{sub or 'unknown'}")
    if active == "inactive":
        if unit_type == "service" and tuple(active_socket_triggers):
            return Verdict(HealthState.INACTIVE, "socket_activated_waiting")
        return Verdict(HealthState.INACTIVE, f"inactive:{sub or 'unknown'}")
    if active == "maintenance":
        return Verdict(HealthState.DEGRADED, f"maintenance:{sub or 'unknown'}")
    if active != "active":
        return Verdict(HealthState.UNKNOWN, f"unrecognised_active_state:{active}")

    if unit_type == "service":
        service = facts.get("service") if isinstance(facts.get("service"), dict) else {}
        result = service.get("result")
        if isinstance(result, str) and result not in {"success", "done"}:
            known_failures = {
                "resources", "timeout", "exit-code", "signal", "core-dump", "watchdog",
                "start-limit-hit", "protocol", "oom-kill", "failure", "exec-condition",
            }
            if result in known_failures:
                return Verdict(HealthState.FAILED, f"service_result:{result}")
            return Verdict(HealthState.UNKNOWN, f"unrecognised_service_result:{result}")
        if sub == "running":
            return Verdict(HealthState.HEALTHY, "service_running")
        if sub == "exited":
            if service.get("type") == "oneshot" or service.get("remain_after_exit") is True:
                return Verdict(HealthState.HEALTHY, "oneshot_completed_and_retained")
            return Verdict(HealthState.UNKNOWN, "active_service_exited_without_oneshot_evidence")
        return Verdict(HealthState.UNKNOWN, f"unrecognised_active_service_sub_state:{sub}")

    expected_substates = {
        "socket": {"listening", "running"},
        "timer": {"waiting", "elapsed"},
        "path": {"waiting", "running"},
        "mount": {"mounted"},
        "target": {"active"},
    }
    if unit_type in expected_substates and sub in expected_substates[unit_type]:
        return Verdict(HealthState.HEALTHY, f"{unit_type}_{sub}")
    if unit_type in expected_substates:
        return Verdict(HealthState.UNKNOWN, f"unrecognised_active_{unit_type}_sub_state:{sub}")
    return Verdict(HealthState.UNKNOWN, f"unrecognised_unit_type:{unit_type}")
