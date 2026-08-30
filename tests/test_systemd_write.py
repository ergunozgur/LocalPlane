"""The systemd lifecycle write vertical, end to end through the Change engine.

Everything below runs the real path: a real Change service, a real agent socket, a real
agent, a real provider — and one fake D-Bus transport at the bottom that actually moves its
own estate when a job runs. A fake that never moved would let a verification pass against a
reading that was already the answer, which is the kind of verification that verifies nothing.

What these tests are mostly about is the difference between a job and a result:

* **`done` is not success.** The manager carried a transaction out; whether the service is
  now in the state the plan promised is a separate question that only a fresh reading answers;
* **a restart cannot be proven from state alone.** A service running now that was running at
  dispatch has shown nothing, so the execution generation read at dispatch is what a restart
  is judged against — and it is the *original* baseline, on the first attempt and on every
  recovery afterwards;
* **outcomes are never softened.** A refusal before a job existed is `not_written` and ends
  the Run `failed` with nothing claimed about the host; a job that was enqueued and did not
  report completion is `write_unknown` and ends it `recovery_required`, holding the object;
* **truth survives the backend disappearing**, which for an operation that may stop the
  runtime hosting LocalPlane is the ordinary case rather than the exotic one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

from localplane.agent.providers.systemd import SystemdProvider
from localplane.agent.server import AgentServer
from localplane.agent.service import AgentService
from localplane.backend.agent_client import AgentClient
from localplane.backend.changes import ChangeService
from localplane.backend.db.repositories import ObjectRecord
from localplane.backend.domain.changes import (
    ChangeResult,
    HostEffect,
    MutationOutcome,
    RecoveryReason,
    VerificationOutcome,
)
from localplane.backend.domain.management_path import ManagementPathVerdict
from localplane.backend.domain.protection import ProtectionStatus
from localplane.backend.domain.runs import (
    ExecutionAvailability,
    ExecutionEligibility,
    OperationType,
    RunState,
)
from localplane.backend.domain.systemd_lifecycle import (
    EffectEdge,
    SystemdLifecycleContext,
    SystemdServiceAction,
)
from localplane.backend.ingest import Ingestor, ObservationCoordinator
from localplane.backend.management import ManagementService
from localplane.backend.operations import OPERATIONS, build_executors
from localplane.backend.provenance import ProvenanceService
from localplane.backend.runs import RunRefused, RunService
from tests.test_systemd import _service, _unit
from tests.test_systemd_dispatch import DispatchConnection, DispatchState

TARGET = "alpha.service"
NEW_INVOCATION = bytes.fromhex("02" * 16)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass
class Estate:
    """A host whose systemd actually changes when the manager is asked to change it."""

    database: Any
    state: DispatchState
    client: AgentClient
    coordinator: ObservationCoordinator
    ingestor: Ingestor
    runs: RunService
    changes: ChangeService
    host_id: str = ""

    # ----------------------------------------------------------------------- shortcuts

    def observe(self) -> Any:
        result = self.coordinator.refresh_systemd_units()
        self.host_id = result.host_id
        return result

    def target(self) -> ObjectRecord:
        for record in self.ingestor.objects.list_by_kind(self.host_id, "systemd.unit"):
            if record.identity_value == TARGET:
                return record
        raise AssertionError(f"no object for {TARGET}")

    def path(self) -> ManagementPathVerdict:
        """Nothing about the operator's path is proven, and nothing pretends otherwise.

        Systemd protection is carried by typed per-reason findings from the lifecycle
        context, not by this verdict; it is passed because every planner takes one.
        """
        return ManagementPathVerdict(
            resource_id=None, reason="management_path_not_used_for_systemd_protection"
        )

    def context(
        self,
        action: SystemdServiceAction,
        *,
        clear: bool = True,
        observed_at: str | None = None,
        **overrides: Any,
    ) -> SystemdLifecycleContext:
        """Fresh lifecycle evidence for one action, built from what was actually observed.

        `clear` is the ordinary case: the effect closure is disjoint from the management
        path and from the agent's own unit, both correlations are complete, and no gap
        remains — which is what earns `clear` rather than being assumed into it.
        """
        record = self.target()
        assert record.observation is not None
        values: dict[str, Any] = {
            "status": "complete" if clear else "partial",
            "observed_at": observed_at or _now(),
            "target_unit": TARGET,
            "action": action,
            "target_facts": dict(record.observation.facts),
            "effect_units": (TARGET,),
            "effect_edges": (EffectEdge(TARGET, "Requires", "beta.service"),),
            "effect_complete": clear,
            "management_units": ("backend.service",),
            "management_complete": clear,
            "connection_unit": "backend.service",
            "connection_unit_type": "service",
            "agent_unit": "localplane-agent.service",
            "agent_complete": clear,
            "agent_unit_type": "service",
            "gaps": () if clear else ("systemd.effect_graph",),
            "observation_id": record.observation.observation_id,
        }
        values.update(overrides)
        return SystemdLifecycleContext(**values)

    def plan(self, action: SystemdServiceAction, **kwargs: Any) -> Any:
        operation = OperationType(f"systemd.service.{action}")
        return self.runs.create(
            operation, self.target(), self.path(),
            systemd_lifecycle_context=self.context(action, **kwargs),
        )

    def confirm(self, run: Any, action: SystemdServiceAction) -> Any:
        return self.changes.confirm(
            run,
            preview_id=run.preview.preview_id,
            acknowledge=True,
            acknowledge_object=TARGET,
            expected_preview_digest=None,
            management_path=self.path(),
            systemd_lifecycle_context=self.context(action),
        )

    def apply(self, run: Any, action: SystemdServiceAction) -> Any:
        return self.changes.apply(
            run, self.path(), systemd_lifecycle_context=self.context(action)
        )

    def run_through(self, action: SystemdServiceAction) -> Any:
        outcome = self.plan(action)
        self.confirm(outcome.run, action)
        return self.apply(outcome.run, action)

    def rows(self, table: str) -> list[Any]:
        return self.database.query(f"SELECT * FROM {table}")


def _stopped(state: DispatchState) -> None:
    state.units[TARGET].update({"ActiveState": "inactive", "SubState": "dead"})


def _started(state: DispatchState) -> None:
    state.units[TARGET].update({"ActiveState": "active", "SubState": "running"})


def _restarted(state: DispatchState) -> None:
    state.units[TARGET].update(
        {"ActiveState": "active", "SubState": "running", "InvocationID": NEW_INVOCATION}
    )


@pytest.fixture
def estate(database, fake_root: Path, populated_sysfs: Path, working_runner, absent_docker,
           tmp_path: Path) -> Iterator[Estate]:
    state = DispatchState(
        units={TARGET: _unit(TARGET), "beta.service": _unit("beta.service")},
        typed={TARGET: _service(), "beta.service": _service()},
    )
    proc = tmp_path / "proc-cgroup"
    proc.write_text("0::/system.slice/localplane-agent.service\n")
    provider = SystemdProvider(
        runtime_path=None, proc_cgroup=proc,
        connection_factory=lambda: DispatchConnection(state),
    )
    service = AgentService(
        root=fake_root, sysfs_net=populated_sysfs, runner=working_runner,
        docker_socket=absent_docker, systemd_provider=provider,
    )
    server = AgentServer(tmp_path / "agent.sock", service)
    thread = server.serve_in_thread()
    client = AgentClient(server.socket_path, timeout_s=20.0)
    provenance = ProvenanceService(database)
    management = ManagementService(database, freshness_ttl_s=60.0, provenance=provenance)
    ingestor = Ingestor(database, management)
    coordinator = ObservationCoordinator(client, ingestor)
    runs = RunService(database, 60.0, OPERATIONS, provenance)
    estate = Estate(
        database=database, state=state, client=client, coordinator=coordinator,
        ingestor=ingestor, runs=runs,
        changes=ChangeService(
            database, runs, build_executors(client, coordinator, ingestor.objects)
        ),
    )
    estate.observe()
    try:
        yield estate
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ------------------------------------------------------------------------ the whole path


def test_a_stop_is_dispatched_and_proven_by_a_fresh_reading(estate: Estate):
    """The happy path, and the point is the second half: a job is not a result."""
    estate.state.on_dispatch = _stopped
    outcome = estate.run_through(SystemdServiceAction.STOP)

    change = outcome.change
    assert change.operation == "systemd.service.stop"
    assert change.mutation_outcome == str(MutationOutcome.WRITTEN)
    assert change.mutation_detail["job_result"] == "done"
    assert change.verification_outcome == str(VerificationOutcome.VERIFIED)
    assert change.verification_observed_state == "inactive"
    assert change.result == str(ChangeResult.SUCCEEDED)
    assert change.host_effect == str(HostEffect.WRITTEN)
    assert outcome.run.state == str(RunState.SUCCEEDED)
    # The proof is a reading taken after the write, through the ordinary path.
    assert change.verification_observation_id is not None
    assert change.verification_observation_id != change.execution_correlation["observation_id"]
    # An action arms nothing and claims no rollback.
    assert change.checkpoint_id is None
    assert change.rollback_outcome is None
    assert estate.changes.lock_for(outcome.run.run_id) is None


def test_a_start_is_dispatched_and_proven_by_a_fresh_reading(estate: Estate):
    estate.state.units[TARGET].update({"ActiveState": "inactive", "SubState": "dead"})
    estate.observe()
    estate.state.on_dispatch = _started
    outcome = estate.run_through(SystemdServiceAction.START)

    assert outcome.change.mutation_outcome == str(MutationOutcome.WRITTEN)
    assert outcome.change.verification_outcome == str(VerificationOutcome.VERIFIED)
    assert outcome.change.verification_observed_state == "active"
    assert outcome.run.state == str(RunState.SUCCEEDED)


def test_a_restart_is_proven_only_by_a_new_execution_generation(estate: Estate):
    """The state is the same before and after. Only the InvocationID says anything."""
    estate.state.on_dispatch = _restarted
    outcome = estate.run_through(SystemdServiceAction.RESTART)

    assert outcome.change.execution_correlation["invocation_id"] == "01" * 16
    assert outcome.change.verification_outcome == str(VerificationOutcome.VERIFIED)
    assert outcome.change.verification_observed_state == "active"
    assert outcome.run.state == str(RunState.SUCCEEDED)


def test_a_restart_that_did_not_restart_is_not_verified(estate: Estate):
    """`done` from the manager, `active` afterwards, and still not a restart.

    This is the case the whole InvocationID comparison exists for: doing nothing at all to
    an already-running service produces exactly this reading.
    """
    estate.state.on_dispatch = None  # the job completes; the service never restarts
    outcome = estate.run_through(SystemdServiceAction.RESTART)

    assert outcome.change.mutation_outcome == str(MutationOutcome.WRITTEN)
    assert outcome.change.verification_outcome == str(VerificationOutcome.MISMATCH)
    assert outcome.change.verification_reason == "service_did_not_start_a_new_execution"
    assert outcome.change.result == str(ChangeResult.RECOVERY_REQUIRED)
    assert outcome.change.recovery_reason == str(RecoveryReason.ACTION_NOT_PROVEN)
    assert outcome.run.state == str(RunState.RECOVERY_REQUIRED)
    # The object stays held: what it holds is not what was asked for.
    assert estate.changes.lock_for(outcome.run.run_id) is not None


def test_a_restart_whose_generation_cannot_be_read_is_unproven_rather_than_wrong(
    estate: Estate,
):
    """Unreadable and mismatched are different findings and lead to different sentences."""
    def _unreadable(state: DispatchState) -> None:
        state.units[TARGET].update({"ActiveState": "active", "SubState": "running"})
        state.units[TARGET]["InvocationID"] = bytes(16)

    estate.state.on_dispatch = _unreadable
    outcome = estate.run_through(SystemdServiceAction.RESTART)

    assert outcome.change.verification_outcome == str(VerificationOutcome.VALUE_UNREADABLE)
    assert outcome.change.verification_reason == "invocation_id_unreadable"
    assert outcome.change.result == str(ChangeResult.RECOVERY_REQUIRED)


def test_a_job_that_completed_is_not_success_on_its_own(estate: Estate):
    """The manager said `done` and the service is in the wrong state. Not verified."""
    estate.state.on_dispatch = None  # `done`, but the service stays active
    outcome = estate.run_through(SystemdServiceAction.STOP)

    assert outcome.change.mutation_detail["job_result"] == "done"
    assert outcome.change.mutation_outcome == str(MutationOutcome.WRITTEN)
    assert outcome.change.verification_outcome == str(VerificationOutcome.MISMATCH)
    assert outcome.change.result != str(ChangeResult.SUCCEEDED)


# ------------------------------------------------------------------------ what refused


def test_an_authorization_denial_is_not_written_and_the_run_fails(estate: Estate):
    """systemd refused before a job existed. Nothing was written and the record says so."""
    from tests.test_systemd_dispatch import _error_reply

    estate.state.dispatch_error = _error_reply("org.freedesktop.DBus.Error.AccessDenied")
    outcome = estate.run_through(SystemdServiceAction.STOP)

    assert outcome.change.mutation_outcome == str(MutationOutcome.NOT_WRITTEN)
    assert outcome.change.mutation_reason == "authorization_denied"
    assert outcome.change.host_effect == str(HostEffect.NONE)
    assert outcome.change.result == str(ChangeResult.FAILED)
    assert outcome.run.state == str(RunState.FAILED)
    # Nothing to recover from, so the object is given back.
    assert estate.changes.lock_for(outcome.run.run_id) is None


@pytest.mark.parametrize("job_result", ["failed", "timeout", "dependency", "skipped"])
def test_a_job_that_did_not_complete_is_unknown_and_holds_the_object(
    estate: Estate, job_result: str
):
    """Enqueued and not completed. Neither `succeeded` nor `failed` is available."""
    estate.state.job_result = job_result
    outcome = estate.run_through(SystemdServiceAction.STOP)

    assert outcome.change.mutation_outcome == str(MutationOutcome.WRITE_UNKNOWN)
    assert outcome.change.mutation_detail["job_result"] == job_result
    assert outcome.change.host_effect == str(HostEffect.WRITE_UNKNOWN)
    assert outcome.change.result == str(ChangeResult.RECOVERY_REQUIRED)
    assert outcome.change.recovery_reason == str(RecoveryReason.APPLY_WRITE_UNKNOWN)
    # No verification is attempted: it would answer what the host holds, and the question
    # that decides what to do here is whether this write occurred.
    assert outcome.change.verification_outcome == str(VerificationOutcome.NOT_ATTEMPTED)
    assert estate.changes.lock_for(outcome.run.run_id) is not None


def test_a_job_whose_completion_is_never_heard_is_also_unknown(estate: Estate):
    estate.state.withhold_result = True
    outcome = estate.run_through(SystemdServiceAction.STOP)

    assert outcome.change.mutation_outcome == str(MutationOutcome.WRITE_UNKNOWN)
    assert outcome.change.mutation_reason == "job_result_not_observed"
    assert outcome.change.result == str(ChangeResult.RECOVERY_REQUIRED)


def test_a_unit_that_vanished_mid_change_cannot_be_verified_from_a_stale_reading(
    estate: Estate,
):
    """The targeted read ran and did not see it. Its previous observation proves nothing."""
    def _unloaded(state: DispatchState) -> None:
        state.units.pop(TARGET)

    estate.state.on_dispatch = _unloaded
    outcome = estate.run_through(SystemdServiceAction.STOP)

    assert outcome.change.mutation_outcome == str(MutationOutcome.WRITTEN)
    assert outcome.change.verification_outcome == str(VerificationOutcome.TARGET_ABSENT)
    assert outcome.change.result == str(ChangeResult.RECOVERY_REQUIRED)
    assert outcome.change.recovery_reason == str(
        RecoveryReason.TARGET_ABSENT_AFTER_MUTATION
    )


# ------------------------------------------------------------- the plan and its authority


def test_the_published_plan_is_executable_and_takes_no_value_from_a_caller(estate: Estate):
    plan = estate.plan(SystemdServiceAction.STOP).plan

    assert plan.execution.availability is ExecutionAvailability.AVAILABLE
    assert plan.execution.eligibility is ExecutionEligibility.ELIGIBLE
    assert plan.execution.provider == "systemd"
    assert plan.protection.status is ProtectionStatus.CLEAR
    assert plan.self_impact.override_eligible is False
    assert plan.recovery.armed is False
    assert plan.recovery.rollback_possible is False


def test_an_apply_without_a_confirmation_waits_rather_than_writing(estate: Estate):
    run = estate.plan(SystemdServiceAction.STOP).run
    with pytest.raises(RunRefused) as raised:
        estate.apply(run, SystemdServiceAction.STOP)

    assert raised.value.code == "confirmation_required"
    assert estate.rows("changes") == []
    assert estate.runs.get(run.run_id).state == str(RunState.AWAITING_CONFIRMATION)
    assert ("unit_lifecycle",) not in {(c[0],) for c in estate.state.calls}


def test_a_plan_whose_lifecycle_evidence_moved_is_stale_rather_than_executed(
    estate: Estate,
):
    """A gate re-derived from *this* request's evidence, not the one the plan was made with.

    The apply is handed a context whose effect graph is no longer complete. That is a
    different document with a different protection verdict, and the remedy for a stale plan
    is a new Run rather than executing the one nobody reviewed.
    """
    run = estate.plan(SystemdServiceAction.STOP).run
    estate.confirm(run, SystemdServiceAction.STOP)

    with pytest.raises(RunRefused) as raised:
        estate.changes.apply(
            run, estate.path(),
            systemd_lifecycle_context=estate.context(
                SystemdServiceAction.STOP, clear=False
            ),
        )
    assert raised.value.code == "preview_stale"
    assert estate.rows("changes") == []


def test_an_apply_with_no_lifecycle_evidence_at_all_refuses(estate: Estate):
    """Not planning it from an older request's context, and not planning it from nothing."""
    run = estate.plan(SystemdServiceAction.STOP).run
    estate.confirm(run, SystemdServiceAction.STOP)

    with pytest.raises(RunRefused) as raised:
        estate.changes.apply(run, estate.path())
    assert raised.value.code == "preview_stale"
    assert estate.rows("changes") == []


def test_a_protected_plan_is_blocked_and_its_verdict_is_never_rewritten(estate: Estate):
    """The effect closure reaches the management path. `protected` stays `protected`."""
    outcome = estate.plan(
        SystemdServiceAction.STOP,
        effect_units=(TARGET, "backend.service"),
    )
    plan = outcome.plan

    assert plan.protection.status is ProtectionStatus.PROTECTED
    assert plan.execution.eligibility is ExecutionEligibility.BLOCKED
    assert "protected:management_path" in plan.execution.blockers
    with pytest.raises(RunRefused) as raised:
        estate.confirm(outcome.run, SystemdServiceAction.STOP)
    assert raised.value.code == "preview_not_executable"
    assert estate.rows("changes") == []


def test_an_unresolved_plan_is_blocked_and_never_becomes_clear(estate: Estate):
    outcome = estate.plan(SystemdServiceAction.STOP, clear=False)
    plan = outcome.plan

    assert plan.protection.status is ProtectionStatus.UNKNOWN
    assert plan.execution.eligibility is ExecutionEligibility.BLOCKED
    assert plan.self_impact.override_eligible is False
    assert estate.rows("changes") == []


# --------------------------------------------------------------- recovery and disruption


def test_an_interruption_after_dispatch_settles_unknown_and_keeps_the_hold(estate: Estate):
    """The backend went away between the dispatch marker and the result.

    For an operation that may stop the runtime hosting LocalPlane this is the ordinary case.
    What the record says afterwards is that a write may have happened — never that one did,
    and never that one did not.
    """
    estate.state.on_dispatch = _stopped
    outcome = estate.run_through(SystemdServiceAction.STOP)
    change = outcome.change

    # Rewind to the instant a death between the marker and the result leaves behind.
    with estate.database.transaction():
        estate.changes.changes.update(change.change_id, {
            "mutation_outcome": None, "mutation_reason": None, "settled_at": None,
            "host_effect": "none", "result": "in_flight", "finished_at": None,
            "verification_outcome": "not_attempted", "verification_observation_id": None,
            "verification_observed_state": None, "verification_reason": None})
        estate.runs.runs.set_state(
            run_id=outcome.run.run_id, state=str(RunState.APPLYING), host_effect="none")
        estate.database.connection.execute(
            "UPDATE runs SET finished_at = NULL WHERE run_id = ?", (outcome.run.run_id,))
        estate.changes.locks.acquire(
            host_id=outcome.run.host_id, object_id=outcome.run.object_id,
            field="lifecycle", run_id=outcome.run.run_id, now=_now())

    settled = estate.changes.settle_interrupted()

    assert [c.change_id for c in settled] == [change.change_id]
    after = estate.changes.get_change(change.change_id)
    assert after.mutation_outcome == str(MutationOutcome.WRITE_UNKNOWN)
    assert after.result == str(ChangeResult.RECOVERY_REQUIRED)
    assert estate.changes.lock_for(outcome.run.run_id) is not None


def test_a_durable_provider_outcome_survives_a_restart_unchanged(estate: Estate):
    """The dispatch recorded what the manager said. A restart is not a second opinion."""
    estate.state.job_result = "failed"
    outcome = estate.run_through(SystemdServiceAction.STOP)
    before = outcome.change

    estate.changes.settle_interrupted()

    after = estate.changes.get_change(before.change_id)
    assert after.mutation_outcome == str(MutationOutcome.WRITE_UNKNOWN)
    assert after.mutation_detail["job_result"] == "failed"
    assert after.mutation_reason == before.mutation_reason
    assert after.result == str(ChangeResult.RECOVERY_REQUIRED)


def test_a_restart_that_did_happen_is_proven_by_recovery_without_writing_again(
    estate: Estate,
):
    """The backend returned. A fresh reading settles it, and no second job is dispatched.

    The comparison is against the **original** pre-dispatch generation, which is what makes
    this a proof that *this* restart happened rather than that some restart did.
    """
    estate.state.on_dispatch = _restarted
    estate.state.withhold_result = True
    outcome = estate.run_through(SystemdServiceAction.RESTART)
    assert outcome.change.result == str(ChangeResult.RECOVERY_REQUIRED)
    dispatches = len([c for c in estate.state.calls if c[0] == "unit_lifecycle"])

    recovery = estate.changes.recovery_retry(
        estate.changes.get_change(outcome.change.change_id),
        estate.path(),
        systemd_lifecycle_context=estate.context(SystemdServiceAction.RESTART),
    )

    assert str(recovery.attempt.outcome) == "proven"
    assert recovery.attempt.evidence_outcome == str(VerificationOutcome.VERIFIED)
    assert recovery.hold.unresolved is False
    # Nothing was dispatched again merely because the backend came back.
    assert len([c for c in estate.state.calls if c[0] == "unit_lifecycle"]) == dispatches
    assert recovery.change.mutation_outcome == str(MutationOutcome.WRITE_UNKNOWN)


def test_a_restart_that_did_not_happen_cannot_be_retried_into_a_second_write(
    estate: Estate,
):
    """The generation is the one from before, so nothing is proven — and nothing is retried.

    A retry that would dispatch a *new* mutation needs authority this build can record, and
    what a systemd lifecycle plan requires is the typed method. Recovery accepts only an
    acknowledgement, so the honest ending is a refusal that wrote nothing rather than a
    second job nobody authorised. The hold stays, and a person settles it — by looking, or
    by resolving it.
    """
    estate.state.withhold_result = True
    outcome = estate.run_through(SystemdServiceAction.RESTART)
    dispatches = len([c for c in estate.state.calls if c[0] == "unit_lifecycle"])

    with pytest.raises(RunRefused) as raised:
        estate.changes.recovery_retry(
            estate.changes.get_change(outcome.change.change_id),
            estate.path(),
            systemd_lifecycle_context=estate.context(SystemdServiceAction.RESTART),
        )

    assert raised.value.code == "confirmation_method_unsupported"
    # Nothing was dispatched a second time, and the hold is still held.
    assert len([c for c in estate.state.calls if c[0] == "unit_lifecycle"]) == dispatches
    assert estate.changes.lock_for(outcome.run.run_id) is not None


# ------------------------------------------------------------------- the closed surface


def test_the_store_accepts_exactly_the_three_systemd_operations(estate: Estate):
    import sqlite3

    sql = estate.database.query_one(
        "SELECT sql FROM sqlite_master WHERE name = 'changes'"
    )["sql"]
    for verb in ("systemd.service.start", "systemd.service.stop", "systemd.service.restart"):
        assert verb in sql
    for absent in (
        "systemd.service.reload", "systemd.service.enable", "systemd.service.mask",
        "systemd.daemon.reload", "systemd.unit.lifecycle",
    ):
        assert absent not in sql

    estate.state.on_dispatch = _stopped
    outcome = estate.run_through(SystemdServiceAction.STOP)
    with pytest.raises(sqlite3.IntegrityError):
        with estate.database.transaction():
            estate.database.connection.execute(
                "UPDATE changes SET operation = 'systemd.service.reload' WHERE change_id = ?",
                (outcome.change.change_id,))


def test_nothing_on_the_path_names_a_shell_a_helper_or_a_root_broker(estate: Estate):
    """The whole write path, read as source: no shell, no systemctl, no helper widening."""
    from localplane.backend import systemd_operations
    from localplane.helper.protocol import HELPER_METHODS

    source = Path(systemd_operations.__file__).read_text()
    for forbidden in ("subprocess", "systemctl", "sudo", "os.system", "shell=True",
                      "dbus-send", "busctl"):
        assert forbidden not in source
    # The privileged component is exactly where it was; systemd does not go through it.
    assert HELPER_METHODS == {"helper.hello", "network.interface.set_mtu"}
