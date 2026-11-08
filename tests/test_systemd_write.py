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

from dataclasses import dataclass, replace
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

from localplane.agent.providers.systemd import SystemdProvider
from localplane.agent.server import AgentServer
from localplane.agent.service import AgentService
from localplane.backend.agent_client import AgentClient, AgentError
from localplane.backend.changes import ChangeService
from localplane.backend.db.repositories import ObjectRecord
from localplane.backend.domain.changes import (
    ChangeResult,
    Expectation,
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
from localplane.backend.systemd_operations import SystemdLifecycleExecutor
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
    agent_service: AgentService
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
        agent_service=service,
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


def test_lost_reply_while_handler_is_active_blocks_recovery_until_completion(estate: Estate, monkeypatch):
    entered, resume = threading.Event(), threading.Event()
    original_dispatch = DispatchConnection.unit_lifecycle
    original_call = estate.client.systemd_service_lifecycle
    threads = []

    def paused(connection, *args, **kwargs):
        entered.set()
        assert resume.wait(15)
        return original_dispatch(connection, *args, **kwargs)

    def lost_reply(**kwargs):
        thread = threading.Thread(target=lambda: original_call(**kwargs))
        threads.append(thread)
        thread.start()
        assert entered.wait(10)
        raise AgentError("timeout", "response lost", dispatched=True)

    monkeypatch.setattr(DispatchConnection, "unit_lifecycle", paused)
    monkeypatch.setattr(estate.client, "systemd_service_lifecycle", lost_reply)
    estate.state.on_dispatch = _restarted
    try:
        outcome = estate.run_through(SystemdServiceAction.RESTART)
        change = outcome.change
        assert change.mutation_outcome == "write_unknown"
        assess = estate.changes.recovery_assess(change, estate.path())
        assert assess.attempt.refusal_code == "recovery_executor_completion_unknown"
        with pytest.raises(RunRefused, match="prior lifecycle handler"):
            estate.changes.recovery_resolve(change, acknowledge=True, operator_statement=TARGET,
                                            object_name=TARGET, note=None, expected_recovery_reason=None)
        with pytest.raises(RunRefused, match="prior lifecycle handler"):
            estate.changes.recovery_retry(change, estate.path(),
                systemd_lifecycle_context=estate.context(SystemdServiceAction.RESTART))
        assert estate.changes.lock_for(change.run_id) is not None
    finally:
        resume.set()
        for thread in threads:
            thread.join(15)
    assert not threads[0].is_alive()
    previous_instance = estate.agent_service._instance_id
    estate.agent_service._instance_id = "agent_restarted"
    try:
        unknown = estate.changes.recovery_assess(change, estate.path())
        assert unknown.attempt.refusal_code == "recovery_executor_completion_unknown"
        assert unknown.hold.object_write_locked
    finally:
        estate.agent_service._instance_id = previous_instance
    assessed = estate.changes.recovery_assess(change, estate.path())
    assert assessed.attempt.outcome == "proven"
    assert assessed.change.mutation_outcome == "write_unknown"
    assert len([c for c in estate.state.calls if c[0] == "unit_lifecycle"]) == 1


@pytest.mark.parametrize("action", ["assess", "retry", "resolve"])
def test_prior_handler_completion_precedes_recovery_observation(estate: Estate, monkeypatch, action):
    """A delayed handler cannot turn an earlier idle reading into a release or retry."""
    entered, resume = threading.Event(), threading.Event()
    original_dispatch = DispatchConnection.unit_lifecycle
    original_call = estate.client.systemd_service_lifecycle
    threads = []
    estate.state.units[TARGET].update({"ActiveState": "inactive", "SubState": "dead"})
    estate.observe()

    def paused(connection, *args, **kwargs):
        entered.set()
        assert resume.wait(15)
        return original_dispatch(connection, *args, **kwargs)

    def lost_reply(**kwargs):
        thread = threading.Thread(target=lambda: original_call(**kwargs))
        threads.append(thread)
        thread.start()
        assert entered.wait(10)
        raise AgentError("timeout", "response lost", dispatched=True)

    monkeypatch.setattr(DispatchConnection, "unit_lifecycle", paused)
    monkeypatch.setattr(estate.client, "systemd_service_lifecycle", lost_reply)
    # The delayed start returns the service to a failed state after the tempting reading.
    estate.state.on_dispatch = _stopped
    try:
        change = estate.run_through(SystemdServiceAction.START).change
        grant = estate.changes.recovery_confirm(change, acknowledge=True,
            acknowledge_object=TARGET, expected_recovery_reason=None, management_path=estate.path())
        estate.state.units[TARGET].update({"ActiveState": "active", "SubState": "running"})
        executor = estate.changes._executor(estate.runs.get(change.run_id))
        original_observe = executor.observe
        observed = []

        def reading_then_dispatch(record):
            reading = original_observe(record)
            observed.append(True)
            resume.set()
            threads[0].join(15)
            assert not threads[0].is_alive()
            return reading

        monkeypatch.setattr(executor, "observe", reading_then_dispatch)
        if action == "assess":
            result = estate.changes.recovery_assess(change, estate.path())
            assert result.attempt.refusal_code == "recovery_executor_completion_unknown"
            assert result.attempt.mutation_attempt_id is None
        elif action == "retry":
            with pytest.raises(RunRefused) as raised:
                estate.changes.recovery_retry(change, estate.path(),
                    systemd_lifecycle_context=estate.context(SystemdServiceAction.START),
                    confirmation_id=grant.confirmation_id)
            assert raised.value.code == "recovery_executor_completion_unknown"
        else:
            with pytest.raises(RunRefused) as raised:
                estate.changes.recovery_resolve(change, acknowledge=True,
                    operator_statement=TARGET, object_name=TARGET, note=None,
                    expected_recovery_reason=None)
            assert raised.value.code == "recovery_executor_completion_unknown"
        assert observed == [], "an observation was taken while a prior handler could still dispatch"
        assert estate.changes.lock_for(change.run_id) is not None
        assert estate.changes.recovery_confirmation_for(change).confirmation_id == grant.confirmation_id
        assert len([c for c in estate.state.calls if c[0] == "unit_lifecycle"]) == 0
    finally:
        resume.set()
        for thread in threads:
            thread.join(15)
    assert not threads[0].is_alive()
    monkeypatch.setattr(executor, "observe", original_observe)
    fresh = estate.changes.recovery_assess(change, estate.path())
    assert fresh.attempt.outcome == "refused"
    assert fresh.hold.object_write_locked
    assert estate.changes.recovery_confirmation_for(change).confirmation_id == grant.confirmation_id
    assert len([c for c in estate.state.calls if c[0] == "unit_lifecycle"]) == 1


@pytest.mark.parametrize("unit", [None, "other.service"])
def test_mutation_reply_requires_exact_canonical_unit(estate: Estate, monkeypatch, unit):
    original_call = estate.client.systemd_service_lifecycle

    def altered_reply(**kwargs):
        answer = original_call(**kwargs)
        if unit is None:
            answer["mutation"].pop("unit_id", None)
        else:
            answer["mutation"]["unit_id"] = unit
        return answer

    monkeypatch.setattr(estate.client, "systemd_service_lifecycle", altered_reply)
    estate.state.on_dispatch = _stopped
    outcome = estate.run_through(SystemdServiceAction.STOP)
    change = estate.changes.get_change(outcome.change.change_id)
    assert change.mutation_outcome == "write_unknown"
    assert change.mutation_reason == "unreadable_mutation_response"
    assert change.mutation_detail.get("handler_reply_validated") is not True
    assert change.result == "recovery_required"
    assert estate.changes.lock_for(change.run_id) is not None


@pytest.mark.parametrize("action", ["assess", "retry", "resolve"])
def test_handler_completing_during_status_query_requires_later_job_read(estate: Estate, monkeypatch, action):
    entered, resume = threading.Event(), threading.Event()
    original_dispatch = DispatchConnection.unit_lifecycle
    original_call = estate.client.systemd_service_lifecycle
    original_status = estate.client.systemd_service_lifecycle_status
    threads = []
    estate.state.units[TARGET].update({"ActiveState": "inactive", "SubState": "dead"})
    estate.observe()

    def paused(connection, *args, **kwargs):
        entered.set()
        assert resume.wait(15)
        return original_dispatch(connection, *args, **kwargs)

    def lost_reply(**kwargs):
        thread = threading.Thread(target=lambda: original_call(**kwargs))
        threads.append(thread)
        thread.start()
        assert entered.wait(10)
        raise AgentError("timeout", "response lost", dispatched=True)

    monkeypatch.setattr(DispatchConnection, "unit_lifecycle", paused)
    monkeypatch.setattr(estate.client, "systemd_service_lifecycle", lost_reply)
    estate.state.on_dispatch = _stopped
    try:
        change = estate.run_through(SystemdServiceAction.START).change
        grant = estate.changes.recovery_confirm(change, acknowledge=True,
            acknowledge_object=TARGET, expected_recovery_reason=None, management_path=estate.path())
        estate.state.units[TARGET].update({"ActiveState": "active", "SubState": "running"})

        def complete_during_status(**kwargs):
            resume.set()
            threads[0].join(15)
            assert not threads[0].is_alive()
            estate.state.units[TARGET]["Job"] = (7, "/org/freedesktop/systemd1/job/7")
            return original_status(**kwargs)

        monkeypatch.setattr(estate.client, "systemd_service_lifecycle_status", complete_during_status)
        if action == "assess":
            result = estate.changes.recovery_assess(change, estate.path())
            assert result.attempt.refusal_code == "recovery_systemd_job_unsettled"
        elif action == "retry":
            with pytest.raises(RunRefused) as raised:
                estate.changes.recovery_retry(change, estate.path(),
                    systemd_lifecycle_context=estate.context(SystemdServiceAction.START),
                    confirmation_id=grant.confirmation_id)
            assert raised.value.code == "recovery_systemd_job_unsettled"
        else:
            with pytest.raises(RunRefused) as raised:
                estate.changes.recovery_resolve(change, acknowledge=True,
                    operator_statement=TARGET, object_name=TARGET, note=None,
                    expected_recovery_reason=None)
            assert raised.value.code == "recovery_systemd_job_unsettled"
        assert estate.changes.lock_for(change.run_id) is not None
        assert estate.changes.recovery_confirmation_for(change).confirmation_id == grant.confirmation_id
        assert len([c for c in estate.state.calls if c[0] == "unit_lifecycle"]) == 1
        monkeypatch.setattr(estate.client, "systemd_service_lifecycle_status", original_status)
        estate.state.units[TARGET].update({"Job": (0, "/"), "ActiveState": "active", "SubState": "running"})
        fresh = estate.changes.recovery_assess(change, estate.path())
        assert fresh.attempt.outcome == "proven"
        assert fresh.change.mutation_outcome == "write_unknown"
        assert estate.changes.recovery_confirmation_for(change).confirmation_id == grant.confirmation_id
        assert len([c for c in estate.state.calls if c[0] == "unit_lifecycle"]) == 1
    finally:
        resume.set()
        for thread in threads:
            thread.join(15)


def test_receipts_bind_attempt_and_refuse_duplicates_and_exhaustion(estate: Estate):
    instance = estate.agent_service.instance_id
    args = {"attempt_id": "att_test", "unit_id": TARGET, "action": "restart",
            "agent_instance_id": instance}
    estate.client.systemd_service_lifecycle(**args)
    assert estate.client.systemd_service_lifecycle_status(**args)["status"] == "completed"
    assert estate.client.systemd_service_lifecycle_status(**{**args, "agent_instance_id": "old"})["status"] == "unknown"
    assert estate.client.systemd_service_lifecycle_status(**{**args, "unit_id": "other.service"})["status"] == "unknown"
    with pytest.raises(AgentError):
        estate.client.systemd_service_lifecycle(**args)
    estate.agent_service._lifecycle_receipts.update(
        {f"filled-{i}": (TARGET, "restart", "completed") for i in range(4095)})
    refusal = estate.client.systemd_service_lifecycle(**{**args, "attempt_id": "overflow"})
    assert refusal["agent_instance_id"] == instance
    assert refusal["mutation"]["outcome"] == "not_written"
    assert refusal["mutation"]["reason"] == "lifecycle_receipt_capacity_exhausted"
    assert refusal["mutation"]["attempt_id"] == "overflow"
    assert refusal["mutation"]["unit_id"] == TARGET
    assert refusal["mutation"]["action"] == "restart"
    assert estate.client.systemd_service_lifecycle_status(
        **{**args, "attempt_id": "overflow"})["status"] == "unknown"
    assert len([c for c in estate.state.calls if c[0] == "unit_lifecycle"]) == 1


def _fill_lifecycle_receipts(estate: Estate) -> None:
    receipts = estate.agent_service._lifecycle_receipts
    for index in range(4096 - len(receipts)):
        receipts[f"filled-{index}"] = (TARGET, "restart", "completed")
    assert len(receipts) == 4096


def test_capacity_refusal_on_apply_is_not_written_and_does_not_hold(estate: Estate):
    _fill_lifecycle_receipts(estate)
    outcome = estate.run_through(SystemdServiceAction.RESTART)
    assert outcome.change.mutation_outcome == "not_written"
    assert outcome.change.mutation_reason == "lifecycle_receipt_capacity_exhausted"
    assert outcome.change.mutation_detail["handler_reply_validated"] is True
    assert outcome.change.result == "failed"
    assert estate.changes.lock_for(outcome.run.run_id) is None
    assert estate.changes.confirmation_for(outcome.run.run_id).consumed is True
    assert not [c for c in estate.state.calls if c[0] == "unit_lifecycle"]
    assert len(estate.agent_service._lifecycle_receipts) == 4096


@pytest.mark.parametrize("disposition", ["assess", "resolve"])
def test_capacity_refusal_on_recovery_retry_keeps_hold_recoverable(estate: Estate, disposition):
    estate.state.withhold_result = True
    change = estate.run_through(SystemdServiceAction.RESTART).change
    assert change.mutation_outcome == "write_unknown"
    original_detail = dict(change.mutation_detail)
    _fill_lifecycle_receipts(estate)
    grant = estate.changes.recovery_confirm(change, acknowledge=True, acknowledge_object=TARGET,
        expected_recovery_reason=None, management_path=estate.path())
    result = estate.changes.recovery_retry(change, estate.path(),
        systemd_lifecycle_context=estate.context(SystemdServiceAction.RESTART),
        confirmation_id=grant.confirmation_id)
    assert result.attempt.outcome == "not_written"
    assert result.attempt.mutation_reason == "lifecycle_receipt_capacity_exhausted"
    assert result.attempt.mutation_detail["handler_reply_validated"] is True
    assert result.attempt.confirmation_id == grant.confirmation_id
    assert estate.changes.recovery_confirmation_for(change) is None
    assert result.hold.object_write_locked
    assert result.change.mutation_outcome == "write_unknown"
    assert result.change.mutation_detail == original_detail
    assert len([c for c in estate.state.calls if c[0] == "unit_lifecycle"]) == 1

    if disposition == "assess":
        estate.state.units[TARGET]["InvocationID"] = NEW_INVOCATION
        completed = estate.changes.recovery_assess(change, estate.path())
        assert completed.attempt.outcome == "proven"
    else:
        completed = estate.changes.recovery_resolve(change, acknowledge=True,
            operator_statement=TARGET, object_name=TARGET, note=None, expected_recovery_reason=None)
        assert completed.attempt.outcome == "resolved"
    assert not completed.hold.object_write_locked
    assert completed.change.mutation_outcome == "write_unknown"
    assert len([c for c in estate.state.calls if c[0] == "unit_lifecycle"]) == 1
    assert len(estate.agent_service._lifecycle_receipts) == 4096


@pytest.mark.parametrize("damage", ["attempt", "unit", "action", "instance", "missing", "outcome", "lost", "error"])
def test_capacity_reply_requires_exact_correlation_and_delivery(estate: Estate, monkeypatch, damage):
    _fill_lifecycle_receipts(estate)
    original_call = estate.client.systemd_service_lifecycle

    def damaged_reply(**kwargs):
        answer = original_call(**kwargs)
        if damage == "lost":
            raise AgentError("timeout", "response lost", dispatched=True)
        if damage == "error":
            raise AgentError("capability_unavailable", "capacity", dispatched=True)
        if damage == "missing":
            return {"agent_instance_id": answer["agent_instance_id"]}
        if damage == "instance":
            answer["agent_instance_id"] = "agent_other"
        elif damage == "outcome":
            answer["mutation"]["outcome"] = "unreadable"
        else:
            answer["mutation"][{"attempt": "attempt_id", "unit": "unit_id", "action": "action"}[damage]] = "wrong"
        return answer

    monkeypatch.setattr(estate.client, "systemd_service_lifecycle", damaged_reply)
    outcome = estate.run_through(SystemdServiceAction.RESTART)
    assert outcome.change.mutation_outcome == "write_unknown"
    assert outcome.change.mutation_detail.get("handler_reply_validated") is not True
    assert outcome.change.result == "recovery_required"
    assert estate.changes.lock_for(outcome.run.run_id) is not None
    assessed = estate.changes.recovery_assess(outcome.change, estate.path())
    assert assessed.attempt.refusal_code == "recovery_executor_completion_unknown"
    assert assessed.hold.object_write_locked
    assert not [c for c in estate.state.calls if c[0] == "unit_lifecycle"]


@pytest.mark.parametrize("damage", ["lost", "unit"])
def test_ambiguous_capacity_retry_keeps_hold_without_trusting_missing_receipt(estate: Estate, monkeypatch, damage):
    estate.state.withhold_result = True
    change = estate.run_through(SystemdServiceAction.RESTART).change
    _fill_lifecycle_receipts(estate)
    grant = estate.changes.recovery_confirm(change, acknowledge=True, acknowledge_object=TARGET,
        expected_recovery_reason=None, management_path=estate.path())
    original_call = estate.client.systemd_service_lifecycle

    def damaged_reply(**kwargs):
        answer = original_call(**kwargs)
        if damage == "lost":
            raise AgentError("timeout", "response lost", dispatched=True)
        answer["mutation"]["unit_id"] = "other.service"
        return answer

    monkeypatch.setattr(estate.client, "systemd_service_lifecycle", damaged_reply)
    retry = estate.changes.recovery_retry(change, estate.path(),
        systemd_lifecycle_context=estate.context(SystemdServiceAction.RESTART),
        confirmation_id=grant.confirmation_id)
    assert retry.attempt.outcome == "write_unknown"
    assert retry.attempt.mutation_detail.get("handler_reply_validated") is not True
    assessed = estate.changes.recovery_assess(change, estate.path())
    assert assessed.attempt.refusal_code == "recovery_executor_completion_unknown"
    assert assessed.hold.object_write_locked
    assert len([c for c in estate.state.calls if c[0] == "unit_lifecycle"]) == 1


@pytest.mark.parametrize("baseline", [None, "", "garbage", "0" * 32])
def test_restart_proof_requires_a_valid_original_generation(baseline):
    executor = SystemdLifecycleExecutor(SystemdServiceAction.RESTART, None, None, None)
    proof = executor.prove(
        {"canonical_id": TARGET, "current_job_state": "none", "active_state": "active", "sub_state": "running", "invocation_id": "02" * 16},
        Expectation(kind="state", state="active", correlation={"unit_id": TARGET, "invocation_id": baseline}),
    )
    assert proof.outcome == VerificationOutcome.VALUE_UNREADABLE
    assert proof.reason == "restart_baseline_invocation_id_missing"


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


@pytest.mark.parametrize("code", ["timeout", "agent_unavailable"])
def test_lifecycle_transport_loss_keeps_durable_unknown_even_if_unit_is_active(
    estate: Estate, monkeypatch, code: str,
):
    from localplane.backend.agent_client import AgentError
    estate.state.units[TARGET].update({"ActiveState": "inactive", "SubState": "dead"})
    estate.observe()
    estate.state.on_dispatch = _restarted
    original = estate.client.systemd_service_lifecycle

    def lose_answer(**kwargs):
        original(**kwargs)  # The scripted agent acts, but the backend loses its answer.
        raise AgentError(code, "scripted lost lifecycle answer", dispatched=True)

    monkeypatch.setattr(estate.client, "systemd_service_lifecycle", lose_answer)
    outcome = estate.run_through(SystemdServiceAction.START)
    assert outcome.change.mutation_outcome == str(MutationOutcome.WRITE_UNKNOWN)
    assert outcome.change.mutation_reason == code
    assert outcome.change.verification_outcome == str(VerificationOutcome.NOT_ATTEMPTED)
    assert outcome.run.state == str(RunState.RECOVERY_REQUIRED)
    estate.observe()  # Current active/running evidence cannot resolve a past unknown write.
    assert estate.state.units[TARGET]["ActiveState"] == "active"
    assert estate.changes.get_change(outcome.change.change_id) == outcome.change
    assert estate.runs.get(outcome.run.run_id).state == str(RunState.RECOVERY_REQUIRED)
    assert estate.changes.lock_for(outcome.run.run_id) is not None
    with pytest.raises(RunRefused):
        estate.apply(estate.runs.get(outcome.run.run_id), SystemdServiceAction.START)
    assert sum(call[0] == "unit_lifecycle" for call in estate.state.calls) == 1


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


def test_failed_targeted_read_is_unavailable_not_absent(estate: Estate, monkeypatch):
    original = estate.client.observe_systemd_unit

    def failed_read(unit_id):
        payload = original(unit_id)
        payload["observation"].update({"status": "failed", "unit": None,
                                      "reason": "targeted_unit_read_failed"})
        return payload

    monkeypatch.setattr(estate.client, "observe_systemd_unit", failed_read)
    estate.state.on_dispatch = _stopped
    outcome = estate.run_through(SystemdServiceAction.STOP)
    assert outcome.change.mutation_outcome == "written"
    assert outcome.change.verification_outcome == "observation_unavailable"
    assert outcome.change.recovery_reason == "action_verification_unavailable"
    assert estate.changes.lock_for(outcome.run.run_id) is not None


def test_failed_recovery_read_keeps_hold_without_claiming_absence(estate: Estate, monkeypatch):
    estate.state.withhold_result = True
    change = estate.run_through(SystemdServiceAction.RESTART).change
    original = estate.client.observe_systemd_unit

    def failed_read(unit_id):
        payload = original(unit_id)
        payload["observation"].update({"status": "failed", "unit": None,
                                      "reason": "targeted_unit_read_failed"})
        return payload

    monkeypatch.setattr(estate.client, "observe_systemd_unit", failed_read)
    result = estate.changes.recovery_assess(change, estate.path())
    assert result.attempt.evidence_outcome == "observation_unavailable"
    assert result.attempt.refusal_code == "recovery_systemd_observation_unavailable"
    assert result.hold.object_write_locked
    assert result.change.mutation_outcome == "write_unknown"


@pytest.mark.parametrize("corruption", ["failed", "wrong_host", "wrong_scope", "wrong_target", "wrong_provider", "malformed_provider", "no_evidence"])
def test_incompatible_targeted_sweep_cannot_prove_or_claim_absence(estate: Estate, monkeypatch, corruption):
    original = estate.coordinator.refresh_systemd_unit
    executor = estate.changes._executor(estate.runs.get(estate.plan(SystemdServiceAction.STOP).run.run_id))

    def incompatible(unit_id):
        result = original(unit_id)
        if corruption == "failed":
            return replace(result, status="failed")
        if corruption == "wrong_host":
            return replace(result, host_id="other")
        if corruption == "wrong_scope":
            return replace(result, detail={**result.detail, "scope": "inventory"})
        if corruption == "wrong_target":
            return replace(result, detail={**result.detail, "requested_unit": "other.service"})
        if corruption == "wrong_provider":
            return replace(result, providers=[{"provider": "other", "status": "ok"}])
        if corruption == "malformed_provider":
            return replace(result, providers=[None])
        return replace(result, sweep_id="missing-sweep", missing=[])

    monkeypatch.setattr(estate.coordinator, "refresh_systemd_unit", incompatible)
    attempt = executor.observe(estate.target())
    assert attempt.record is None
    assert attempt.failure == "observation_unavailable"


def test_different_canonical_unit_cannot_replace_target_observation(estate: Estate, monkeypatch):
    original = estate.client.observe_systemd_unit
    run = estate.plan(SystemdServiceAction.STOP).run
    executor = estate.changes._executor(run)

    def other_unit(unit_id):
        payload = original(unit_id)
        payload["observation"]["unit"]["facts"]["canonical_id"] = "other.service"
        return payload

    monkeypatch.setattr(estate.client, "observe_systemd_unit", other_unit)
    result = executor.observe(estate.target())
    assert result.record is None
    assert result.failure == "observation_unavailable"


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
    this proof of the required end state rather than attribution to the original attempt.
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
    what a systemd lifecycle plan requires is the typed method. Without that separate grant
    the honest ending is a refusal rather than a second job nobody authorised.
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

    assert raised.value.code == "recovery_confirmation_required"
    # Nothing was dispatched a second time, and the hold is still held.
    assert len([c for c in estate.state.calls if c[0] == "unit_lifecycle"]) == dispatches
    assert estate.changes.lock_for(outcome.run.run_id) is not None


def test_typed_recovery_grant_authorizes_one_retry(estate: Estate):
    estate.state.withhold_result = True
    outcome = estate.run_through(SystemdServiceAction.RESTART)
    change = outcome.change
    with pytest.raises(RunRefused):
        estate.changes.recovery_confirm(change, acknowledge=True, acknowledge_object="wrong",
            expected_recovery_reason=None, management_path=estate.path())
    grant = estate.changes.recovery_confirm(change, acknowledge=True, acknowledge_object=TARGET,
        expected_recovery_reason=None, management_path=estate.path())
    assert grant.method == "typed"
    estate.state.withhold_result = False
    estate.state.on_dispatch = _restarted
    revision = estate.changes.recovery_revision(change)
    result = estate.changes.recovery_retry(change, estate.path(),
        systemd_lifecycle_context=estate.context(SystemdServiceAction.RESTART),
        recovery_revision=revision, confirmation_id=grant.confirmation_id)
    assert result.attempt.outcome == "verified"
    assert result.attempt.confirmation_id == grant.confirmation_id
    assert len([c for c in estate.state.calls if c[0] == "unit_lifecycle"]) == 2


@pytest.mark.parametrize("job", [(7, "/org/freedesktop/systemd1/job/7"), None, (0, "/bad")])
def test_recovery_cannot_release_while_job_is_pending_or_unreadable(estate: Estate, job):
    estate.state.withhold_result = True
    estate.state.on_dispatch = _restarted
    change = estate.run_through(SystemdServiceAction.RESTART).change
    if job is None:
        del estate.state.units[TARGET]["Job"]
    else:
        estate.state.units[TARGET]["Job"] = job
    result = estate.changes.recovery_assess(change, estate.path())
    assert result.attempt.refusal_code == "recovery_systemd_job_unsettled"
    with pytest.raises(RunRefused) as raised:
        estate.changes.recovery_resolve(change, acknowledge=True, operator_statement=TARGET,
            object_name=TARGET, note=None, expected_recovery_reason=None)
    assert raised.value.code == "recovery_systemd_job_unsettled"
    assert estate.changes.lock_for(change.run_id) is not None


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
