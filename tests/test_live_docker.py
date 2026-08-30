"""The Docker subsystem against the real daemon on this machine, end to end.

**Exactly one container is touched and this file creates it.** It is made for the test,
carries a unique name and a LocalPlane label so that a stray one is identifiable
afterwards, gets ``--network=none`` so it cannot reach anything, and is removed in a
``finally``. Every other container on this host is captured before and after and asserted
byte-identical — including LocalPlane's own, which is exactly the container an operator
would least like a test to have restarted.

**The fixture is created with Docker directly, and that is not a gap in the product.**
Creating a container is not a LocalPlane capability in this build and must not become one
to make a test convenient; what the product does here is observe, read logs, sample stats,
and start, stop and restart — and every one of those goes through the real agent, the real
protocol and the real Run.

Skipped rather than failed where Docker is absent, unreadable or has no image this can run
without pulling one: this suite must be runnable on a machine that has no Docker at all.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

import pytest

from localplane.agent.server import AgentServer
from localplane.agent.service import AgentService
from localplane.backend.agent_client import AgentClient
from localplane.backend.changes import ChangeService
from localplane.backend.db.database import open_database
from localplane.backend.domain.changes import ChangeResult, MutationOutcome
from localplane.backend.domain.docker import OBJECT_KIND_DOCKER_CONTAINER
from localplane.backend.domain.protection import ManagementPathVerdict
from localplane.backend.domain.runs import OperationType, RunState
from localplane.backend.ingest import Ingestor
from localplane.backend.ingest import ObservationCoordinator
from localplane.backend.management import ManagementService
from localplane.backend.operations import OPERATIONS, build_executors
from localplane.backend.provenance import ProvenanceService
from localplane.backend.runs import RunService

pytestmark = pytest.mark.live

#: The label every container this file creates carries, so a leaked one is identifiable and
#: so nothing here can be mistaken for a container somebody meant to keep.
FIXTURE_LABEL = "io.localplane.test"

#: What the fixture runs: it prints one line so the log read has something to find, then
#: sleeps so that starting, stopping and restarting it are all meaningful.
PAYLOAD = (
    "import time,sys;print('localplane-live-fixture ready',flush=True);"
    "sys.stderr.write('stderr line\\n');sys.stderr.flush();time.sleep(3600)"
)


def _docker(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=120,
        env={"LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
    )
    if check and result.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _available_image() -> str | None:
    """A locally present image carrying a python3. Nothing is pulled, ever."""
    try:
        listed = _docker("images", "--format", "{{.Repository}}:{{.Tag}}").splitlines()
    except (AssertionError, FileNotFoundError, subprocess.TimeoutExpired):
        return None
    for candidate in ("localplane-backend:latest",):
        if candidate in listed:
            return candidate
    return None


def _estate_of_containers() -> dict[str, dict[str, Any]]:
    """Every container on this host as Docker describes it. The before/after capture."""
    ids = _docker("ps", "-aq").split()
    if not ids:
        return {}
    inspected = json.loads(_docker("inspect", *ids))
    # `State` moves on its own for a running container — uptime is not a change LocalPlane
    # made — so what is compared is identity, configuration and the lifecycle facts a
    # LocalPlane operation would move.
    return {
        c["Id"]: {
            "Name": c["Name"],
            "Image": c["Image"],
            "Created": c["Created"],
            "RestartCount": c["RestartCount"],
            "Config": c["Config"],
            "HostConfig": c["HostConfig"],
            # Sorted, because the daemon does not promise an order here and was observed
            # returning one container's mounts differently between two consecutive reads on
            # this machine. What matters is the set, and an order nobody promised is not a
            # change LocalPlane made.
            "Mounts": sorted(json.dumps(m, sort_keys=True) for m in c["Mounts"]),
            "State": {k: c["State"][k] for k in ("Status", "Running", "StartedAt",
                                                 "FinishedAt", "ExitCode", "Paused")},
        }
        for c in inspected
    }


@pytest.fixture
def fixture_container() -> Iterator[str]:
    """One disposable container, created here and removed here whatever happens."""
    image = _available_image()
    if image is None:
        pytest.skip("docker, or a locally available image to run without pulling, is absent")
    name = f"localplane-live-{uuid.uuid4().hex[:10]}"
    _docker(
        "run", "-d",
        "--name", name,
        "--label", f"{FIXTURE_LABEL}=disposable",
        # No network at all: this container cannot reach this machine or anything else.
        "--network=none",
        # Nothing mounted, no added capability, and it is not privileged.
        "--entrypoint", "python3",
        image, "-c", PAYLOAD,
    )
    container_id = _docker("inspect", "-f", "{{.Id}}", name).strip()
    try:
        yield container_id
    finally:
        _docker("rm", "-f", name, check=False)


@pytest.fixture
def estate(tmp_path: Path) -> Iterator[Any]:
    """A real agent over this machine's Docker socket, and a real backend behind it."""
    service = AgentService(helper_socket=tmp_path / "no-helper.sock")
    capabilities = {c.capability: c for c in service.capabilities}
    lifecycle = capabilities.get("docker.container.lifecycle")
    if lifecycle is None or str(lifecycle.status) != "available":
        pytest.skip(f"this agent has no Docker lifecycle capability: {lifecycle}")

    server = AgentServer(tmp_path / "agent.sock", service)
    server.serve_in_thread()
    database = open_database(tmp_path / "store" / "localplane.db")
    client = AgentClient(server.socket_path, timeout_s=30.0)
    provenance = ProvenanceService(database)
    management = ManagementService(database, freshness_ttl_s=60.0, provenance=provenance)
    ingestor = Ingestor(database, management)
    coordinator = ObservationCoordinator(client, ingestor)
    runs = RunService(database, 60.0, OPERATIONS, provenance)
    changes = ChangeService(database, runs, build_executors(client, coordinator, ingestor.objects))

    class Live:
        def __init__(self) -> None:
            self.database = database
            self.client = client
            self.ingestor = ingestor
            self.coordinator = coordinator
            self.provenance = provenance
            self.runs = runs
            self.changes = changes
            self.host_id = ""
            self.management_path = ManagementPathVerdict(
                resource_id=None, reason="management_path_unobserved"
            )

        def observe(self) -> Any:
            result = self.coordinator.refresh_containers()
            self.host_id = result.host_id
            return result

        def record(self, container_id: str) -> Any:
            for candidate in self.ingestor.objects.list_by_kind(
                self.host_id, OBJECT_KIND_DOCKER_CONTAINER
            ):
                if candidate.identity_value == container_id:
                    return candidate
            raise AssertionError(f"container {container_id[:12]} was not observed")

        def run(self, operation: OperationType, container_id: str) -> Any:
            outcome = self.runs.create(
                operation, self.record(container_id), self.management_path)
            self.changes.confirm(
                outcome.run,
                preview_id=outcome.run.preview.preview_id,
                acknowledge=True,
                expected_preview_digest=None,
                management_path=self.management_path,
            )
            return self.changes.apply(self.runs.get(outcome.run.run_id), self.management_path)

        def authorise(self, change: Any) -> Any:
            return self.changes.recovery_confirm(
                change, acknowledge=True, expected_recovery_reason=change.recovery_reason,
                management_path=self.management_path)

        def attempts(self, change: Any) -> list[Any]:
            return self.changes.recovery_history(change)

    live = Live()
    live.observe()
    try:
        yield live
    finally:
        database.close()
        server.shutdown()
        server.server_close()


def test_the_real_daemon_is_observed_read_and_operated_end_to_end(estate, fixture_container):
    """One container, created for this test, taken through everything this build can do
    with a container.

    Discovery, detail, logs, stats, then stop, start and restart — each applied through a
    real Run and each verified by a fresh observation rather than by the daemon's own
    acknowledgement. Every other container on this host is captured before and after.
    """
    before = _estate_of_containers()
    assert fixture_container in before

    # ------------------------------------------------------------------ discovery + detail
    estate.observe()
    record = estate.record(fixture_container)
    facts = record.observation.facts
    assert record.identity_basis == "provider_id"
    assert facts["state"] == "running"
    assert facts["running"] is True
    # `io.localplane.*` is a kept label prefix, so the marker this fixture carries survives
    # into the observation — which is what makes a leaked fixture identifiable afterwards.
    assert facts["labels"][FIXTURE_LABEL] == "disposable"
    assert facts["labels_dropped"] >= 0
    assert facts["network_mode"] == "none"
    assert facts["started_at"]
    assert record.observation.health_state == "healthy"
    assert record.observation.health_reason == "running_no_healthcheck"

    provenance = estate.provenance.for_object(record)
    assert {str(c.relation) for c in provenance.claims} == {"created_by", "configured_by"}
    assert estate.provenance.eligibility(record, provenance).reason == "externally_configured"

    # ------------------------------------------------------------------------------- logs
    logs = estate.client.container_logs(fixture_container, tail=50)["logs"]
    messages = [line["message"] for line in logs["lines"]]
    assert "localplane-live-fixture ready" in messages
    assert {line["stream"] for line in logs["lines"]} <= {"stdout", "stderr"}
    assert logs["truncated"] is False

    # ------------------------------------------------------------------------------ stats
    stats = estate.client.container_stats(fixture_container)["stats"]
    assert stats["memory_usage_bytes"] and stats["memory_usage_bytes"] > 0
    assert stats["memory_limit_bytes"] and stats["memory_limit_bytes"] > 0
    assert stats["pids"] and stats["pids"] >= 1
    assert stats["cpu_percent"] is not None

    # -------------------------------------------------------------------------------- stop
    stopped = estate.run(OperationType.DOCKER_CONTAINER_STOP, fixture_container)
    assert stopped.run.state == RunState.SUCCEEDED
    assert stopped.change.mutation_outcome == MutationOutcome.WRITTEN
    assert stopped.change.result == ChangeResult.SUCCEEDED
    assert stopped.change.verification_observed_state == "exited"
    assert stopped.change.checkpoint_id is None
    # Proven against the daemon independently of LocalPlane's own reading.
    assert _docker("inspect", "-f", "{{.State.Running}}", fixture_container).strip() == "false"

    # ------------------------------------------------------------------------------- start
    estate.observe()
    started = estate.run(OperationType.DOCKER_CONTAINER_START, fixture_container)
    assert started.run.state == RunState.SUCCEEDED
    assert started.change.verification_observed_state == "running"
    assert _docker("inspect", "-f", "{{.State.Running}}", fixture_container).strip() == "true"

    started_at_before = _docker(
        "inspect", "-f", "{{.State.StartedAt}}", fixture_container).strip()

    # ----------------------------------------------------------------------------- restart
    # A second apart, so that the instant Docker records has somewhere to move to.
    time.sleep(1)
    estate.observe()
    restarted = estate.run(OperationType.DOCKER_CONTAINER_RESTART, fixture_container)
    assert restarted.run.state == RunState.SUCCEEDED
    assert restarted.change.verification_observed_state == "running"
    started_at_after = _docker(
        "inspect", "-f", "{{.State.StartedAt}}", fixture_container).strip()
    # The proof the verification rests on, checked here against the daemon directly.
    assert started_at_after != started_at_before

    # ------------------------------------------------------------- three Changes, no more
    changes = estate.changes.list_changes(estate.host_id)
    assert len(changes) == 3
    assert all(c.change_kind == "action" for c in changes)
    assert all(c.checkpoint_id is None and c.rollback_required is False for c in changes)
    assert {c.object_id for c in changes} == {estate.record(fixture_container).object_id}
    assert estate.database.query("SELECT * FROM run_checkpoints") == []

    # ---------------------------------------------------- and nothing else on this host moved
    after = _estate_of_containers()
    assert set(after) == set(before)
    for container_id in before:
        if container_id == fixture_container:
            continue
        assert after[container_id] == before[container_id], (
            f"container {container_id[:12]} changed during the test"
        )


# ------------------------------------------------------------------------------- recovery


def _hold_by_interfering_with_a_stop(estate, container_id: str) -> Any:
    """Produce a *real* recovery hold against the real daemon, and return the Change.

    A third party starts the container again the instant LocalPlane's ``stop`` lands, so the
    fresh reading cannot show the state the action promised. That is exactly the hazard
    verification exists for, and it is the same technique — and the same justification — the
    live MTU write uses for its rollback scenario: the interference happens **outside** the
    product, through the tool an administrator would use, and everything LocalPlane does is
    entirely real and entirely unmodified.

    Deliberately not produced by a container that exits on startup. That would be a race with
    the daemon's own reaping, decided inside a few milliseconds, and a live test whose subject
    depends on winning it is a live test that reports nothing on a fast machine.
    """
    estate.observe()
    executor = estate.changes._executors[OperationType.DOCKER_CONTAINER_STOP]
    original = executor.mutate
    interfered: list[str] = []

    def stop_then_somebody_starts_it_again(request: Any) -> Any:
        report = original(request)
        _docker("start", container_id)
        interfered.append(report.reason)
        return report

    executor.mutate = stop_then_somebody_starts_it_again  # type: ignore[method-assign]
    try:
        outcome = estate.run(OperationType.DOCKER_CONTAINER_STOP, container_id)
    finally:
        executor.mutate = original  # type: ignore[method-assign]

    change = outcome.change
    assert interfered, "the interference did not run, so this is not the situation under test"
    assert change.change_kind == "action"
    assert change.action == "stop"
    assert change.mutation_outcome == MutationOutcome.WRITTEN
    assert change.result == ChangeResult.RECOVERY_REQUIRED
    assert change.recovery_reason == "action_not_proven"
    assert change.verification_observed_state == "running"
    assert change.checkpoint_id is None
    assert estate.changes.lock_for(change.run_id) is not None
    return change


def _started_at(container_id: str) -> str:
    return _docker("inspect", "-f", "{{.State.StartedAt}}", container_id).strip()


def test_a_real_hold_refuses_a_retry_with_no_authority_and_is_released_by_a_person(
    estate, fixture_container
):
    """The two steps that send nothing at all, against the real daemon.

    A retry looks first, finds the end state still not reached, and is then **refused**: the
    confirmation the original apply consumed authorises nothing, and no other authority
    exists. A resolution then ends the hold, and it sends nothing either. Both are checked
    against the daemon's own record of when the container last started rather than against
    LocalPlane's account of itself.
    """
    before = _estate_of_containers()
    change = _hold_by_interfering_with_a_stop(estate, fixture_container)
    at_hold = _started_at(fixture_container)

    # ------------------------------------------------------- a retry with no authority
    with pytest.raises(Exception) as raised:
        estate.changes.recovery_retry(change, estate.management_path)
    assert getattr(raised.value, "code", None) == "recovery_confirmation_required"
    assert _started_at(fixture_container) == at_hold, "a refused retry reached the daemon"
    assert _docker(
        "inspect", "-f", "{{.State.Running}}", fixture_container).strip() == "true"
    assert estate.changes.lock_for(change.run_id) is not None
    refused = estate.attempts(change)[-1]
    assert (refused.outcome, refused.host_effect) == ("refused", "none")
    assert refused.mutation_outcome is None
    assert refused.evidence_outcome == "mismatch"
    assert refused.evidence_observed_state == "running"

    # --------------------------------------------------------------- released by a person
    name = estate.record(fixture_container).display_name
    resolved = estate.changes.recovery_resolve(
        change, acknowledge=True, operator_statement=name, object_name=name,
        note="somebody else is running this; handled out of band",
        expected_recovery_reason=change.recovery_reason)
    assert resolved.attempt.outcome == "resolved"
    assert resolved.attempt.mutation_outcome is None
    assert resolved.attempt.host_effect == "none"
    assert resolved.attempt.operator_statement == name
    # Recorded beside the decision, and it does not prove the state the change wanted.
    assert resolved.attempt.evidence_observed_state == "running"
    assert str(resolved.hold.state) == "resolved"
    assert estate.changes.lock_for(change.run_id) is None
    assert _started_at(fixture_container) == at_hold, "a resolution reached the daemon"

    # A released hold cannot be released again, by either route.
    for act in (
        lambda: estate.changes.recovery_resolve(
            change, acknowledge=True, operator_statement=name, object_name=name,
            note=None, expected_recovery_reason=None),
        lambda: estate.changes.recovery_retry(change, estate.management_path),
    ):
        with pytest.raises(Exception) as raised:
            act()
        assert getattr(raised.value, "code", None) == "recovery_already_resolved"

    _assert_the_record_still_says_what_happened(estate, change)
    _assert_only_the_fixture_moved(before, fixture_container)


def test_a_real_retry_settles_a_hold_by_looking_when_the_end_state_is_already_there(
    estate, fixture_container
):
    """The best ending there is, proved against the daemon rather than against ourselves.

    Somebody stops the container by hand — the state the change asked for — and the retry
    establishes it through the ordinary observation path. **No verb reaches the daemon**, and
    the daemon's own record of when the container last started and last finished is unmoved.
    """
    before = _estate_of_containers()
    change = _hold_by_interfering_with_a_stop(estate, fixture_container)

    # An administrator does what LocalPlane could not prove it had done.
    _docker("stop", fixture_container)
    at_retry = (_started_at(fixture_container),
                _docker("inspect", "-f", "{{.State.FinishedAt}}", fixture_container).strip())

    result = estate.changes.recovery_retry(change, estate.management_path)
    attempt = result.attempt

    assert attempt.outcome == "proven"
    assert attempt.evidence_outcome == "verified"
    assert attempt.evidence_observed_state == "exited"
    assert attempt.mutation_attempt_id is None
    assert attempt.confirmation_id is None
    assert attempt.host_effect == "none"
    assert attempt.protection_management_path == "unknown"
    assert attempt.releases_hold is True
    assert str(result.hold.state) == "resolved"
    assert estate.changes.lock_for(change.run_id) is None
    # Nothing was sent: the daemon neither started nor stopped it again.
    assert (_started_at(fixture_container),
            _docker("inspect", "-f", "{{.State.FinishedAt}}",
                    fixture_container).strip()) == at_retry

    _assert_the_record_still_says_what_happened(estate, change)
    _assert_only_the_fixture_moved(before, fixture_container)


def test_a_real_retry_with_authority_acts_again_and_proves_the_result(
    estate, fixture_container
):
    """One granted authority, one further `stop` on the real daemon, and an independent proof.

    The confirmation the original apply consumed authorises nothing here: it authorised an
    attempt, and the attempt happened. The retry is refused until a separate grant exists,
    then sends the verb again and proves the result with a fresh observation — checked here
    against `docker inspect` as well.
    """
    before = _estate_of_containers()
    change = _hold_by_interfering_with_a_stop(estate, fixture_container)
    assert _docker(
        "inspect", "-f", "{{.State.Running}}", fixture_container).strip() == "true"

    grant = estate.authorise(change)
    assert grant.purpose == "recovery_retry"
    assert grant.consumed is False
    assert grant.confirmation_id != estate.changes.confirmation_for(
        change.run_id).confirmation_id

    result = estate.changes.recovery_retry(change, estate.management_path)
    attempt = result.attempt

    assert attempt.outcome == "verified"
    assert attempt.evidence_outcome == "mismatch"
    assert attempt.evidence_observed_state == "running"
    assert attempt.mutation_outcome == MutationOutcome.WRITTEN
    assert attempt.mutation_provider == "docker"
    assert attempt.host_effect == "written"
    assert attempt.verification_outcome == "verified"
    assert attempt.verification_observed_state == "exited"
    assert attempt.confirmation_id == grant.confirmation_id
    assert attempt.releases_hold is True
    assert str(result.hold.state) == "resolved"
    assert estate.changes.lock_for(change.run_id) is None
    # Proven against the daemon independently of LocalPlane's own reading.
    assert _docker(
        "inspect", "-f", "{{.State.Running}}", fixture_container).strip() == "false"

    # Exactly one attempt dispatched anything, and the grant is spent.
    assert len([a for a in estate.attempts(change) if a.mutation_attempt_id]) == 1
    spent = estate.database.query_one(
        "SELECT * FROM run_confirmations WHERE confirmation_id = ?",
        (grant.confirmation_id,))
    assert spent["consumed_at"] is not None
    assert spent["consumed_by_attempt_id"] == attempt.mutation_attempt_id

    _assert_the_record_still_says_what_happened(estate, change)
    _assert_only_the_fixture_moved(before, fixture_container)


def _assert_the_record_still_says_what_happened(estate, change: Any) -> None:
    """A later act by an operator does not rewrite the Change it was about."""
    settled = estate.changes.get_change(change.change_id)
    assert settled.result == ChangeResult.RECOVERY_REQUIRED
    assert settled.recovery_reason == "action_not_proven"
    assert settled.mutation_outcome == MutationOutcome.WRITTEN
    assert settled.verification_observed_state == "running"
    assert estate.runs.get(change.run_id).state == RunState.RECOVERY_REQUIRED
    assert estate.database.query("SELECT * FROM object_write_locks") == []


def _assert_only_the_fixture_moved(before: dict, fixture_id: str) -> None:
    """Every other container on this host, byte-identical. Including LocalPlane's own."""
    after = _estate_of_containers()
    assert set(after) == set(before)
    for container_id in before:
        if container_id == fixture_id:
            continue
        assert after[container_id] == before[container_id], (
            f"container {container_id[:12]} changed during the test"
        )


def test_observing_and_reading_the_real_daemon_changes_nothing(estate):
    """The read path, against every container this machine actually has."""
    before = _estate_of_containers()
    result = estate.observe()
    assert result.status in ("ok", "partial")
    records = estate.ingestor.objects.list_by_kind(
        estate.host_id, OBJECT_KIND_DOCKER_CONTAINER)
    assert len(records) == len(before)

    for record in records:
        assert record.management_state == "observed"
        assert record.observation is not None
        # Every container is Docker's, on the daemon's own evidence, and adoption is refused.
        eligibility = estate.provenance.eligibility(record)
        assert eligibility.eligible is False

    # A second sweep is a second observation of the same objects, not a second set of them.
    estate.observe()
    assert len(estate.ingestor.objects.list_by_kind(
        estate.host_id, OBJECT_KIND_DOCKER_CONTAINER)) == len(before)

    assert _estate_of_containers() == before
