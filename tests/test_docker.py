"""Docker as a LocalPlane subsystem: observing it, and acting on it through it.

Three kinds of test, and they are separated on purpose.

* **Normalisation and error mapping** — pure, against the daemon's own JSON shapes,
  including the ones that carry credentials and must not survive into an observation.
* **The public contract** — over a real HTTP client, a real store and a real agent socket,
  because a resource model that is only correct inside the backend is not one an operator
  has.
* **The whole path** — against a Docker daemon that actually changes state when it is asked
  to, so that a verification is a verification rather than a comparison against a fixture
  that was already the answer. `test_live_docker.py` does the same against the real daemon.

What the fake daemon here contributes that a stub could not: it is a real ``AF_UNIX`` HTTP
server, it records every request, and it moves its own containers between states — so the
tests can assert what was sent as well as what came back, and a restart that did not restart
is a state this file can actually produce.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

from localplane.agent.providers.docker import (
    PROBE_CONTAINER_ID,
    DockerFailure,
    DockerProvider,
    InvalidContainerId,
    _demultiplex,
    _stats,
)
from localplane.agent.service import AgentService
from localplane.backend.agent_client import AgentClient, AgentError
from localplane.backend.changes import ChangeService
from localplane.backend.container_operations import ContainerLifecycleExecutor
from localplane.backend.db.repositories import ObjectRecord
from localplane.backend.domain.changes import (
    ChangeResult,
    Expectation,
    MutationOutcome,
    MutationRequest,
    RecoveryReason,
)
from localplane.backend.domain.docker import (
    OBJECT_KIND_DOCKER_CONTAINER,
    LifecycleAction,
    classify_container_management,
    derive_container_health,
    is_running,
)
from localplane.backend.domain.identity import IdentityBasis, identify_container
from localplane.backend.domain.protection import (
    CANNOT_AFFECT_MANAGEMENT_PATH,
    ManagementPathVerdict,
    ProtectionStatus,
)
from localplane.backend.domain.provenance import OwnershipRelation
from localplane.backend.domain.runs import (
    OperationType,
    PlannedAction,
    RecoveryMode,
    RunState,
)
from localplane.backend.domain.states import HealthState, ManagementState
from localplane.backend.ingest import Ingestor, ObservationCoordinator
from localplane.backend.management import ManagementService
from localplane.backend.operations import OPERATIONS, build_executors
from localplane.backend.provenance import ProvenanceService
from localplane.backend.runs import RunRefused, RunService
from localplane.protocol.capabilities import (
    CAPABILITY_DOCKER_CONTAINER_LIFECYCLE,
    CAPABILITY_DOCKER_CONTAINERS_OBSERVE,
    CapabilityStatus,
)
from localplane.protocol.wire import DOCKER_LIFECYCLE_ACTIONS
from tests.conftest import docker_container, serve_docker

_ID = "c" * 64
_OTHER = "d" * 64


# ============================================================================ a live daemon


@dataclass
class StatefulDockerDaemon:
    """A Docker daemon whose containers actually change when it is asked to change them.

    The counterpart to ``conftest.DockerDaemon``, which answers from a fixed map — and it is
    the *same* server, the same handler and the same request record, because a fake daemon
    that framed its answers differently from the one the read tests use would be a second
    thing to keep in step. All this adds is an ``answer`` that routes by path and an ``act``
    that moves state.

    The lifecycle verbs move the container's state and its ``StartedAt`` the way the real
    daemon does, which is what makes the verification tests mean anything: a restart is
    proven from the instant Docker records, so a fake that never moved it would let a
    verification pass without the change having happened.
    """

    path: Path
    containers: dict[str, dict[str, Any]]
    requests: list[tuple[str, str]] = field(default_factory=list)
    lifecycle_status: dict[str, int] = field(default_factory=dict)
    refuse_writes: bool = False
    freeze_started_at: bool = False
    clock: datetime = field(default_factory=lambda: datetime(2026, 8, 20, 10, tzinfo=timezone.utc))
    server: Any = None
    stats: dict[str, Any] = field(default_factory=dict)
    logs: bytes = b""

    # ------------------------------------------------------------------------ answering

    def answer(self, verb: str, raw_path: str) -> tuple[int, bytes]:
        path = raw_path.partition("?")[0]
        parts = path.strip("/").split("/")
        if verb == "POST":
            if len(parts) == 3 and parts[0] == "containers" and parts[2] in _VERBS:
                status = self.act(parts[1], parts[2])
                return status, _json({"message": f"{parts[2]}: {status}"})
            return 404, _json({"message": "No such path"})
        if path == "/version":
            return 200, _json({"Version": "29.1.3", "ApiVersion": "1.52"})
        if path == "/networks":
            return 200, _json([])
        if path == "/containers/json":
            return 200, _json([{"Id": i} for i in self.containers])
        if len(parts) == 3 and parts[0] == "containers":
            container = self.containers.get(parts[1])
            if container is None:
                return 404, _json({"message": "No such container"})
            if parts[2] == "json":
                return 200, _json(container)
            if parts[2] == "logs":
                return 200, self.logs
            if parts[2] == "stats":
                return 200, _json(self.stats)
        return 404, _json({"message": "No such path"})

    # --------------------------------------------------------------------------- acting

    def act(self, container_id: str, action: str) -> int:
        """Carry the verb out, exactly as far as the real daemon would."""
        if self.refuse_writes:
            return 403
        override = self.lifecycle_status.get(action)
        if override is not None:
            return override
        container = self.containers.get(container_id)
        if container is None:
            return 404
        state = container["State"]
        running = state["Status"] == "running"
        if action == "start" and running:
            return 304
        if action == "stop" and not running:
            return 304
        if action in ("start", "restart"):
            state["Status"] = "running"
            state["Running"] = True
            state["ExitCode"] = 0
            if not self.freeze_started_at:
                self.clock += timedelta(seconds=5)
                state["StartedAt"] = self.clock.isoformat().replace("+00:00", "Z")
        else:
            state["Status"] = "exited"
            state["Running"] = False
            state["ExitCode"] = 143
        return 204

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


_VERBS = ("start", "stop", "restart")


def _json(payload: Any) -> bytes:
    return json.dumps(payload).encode()


@pytest.fixture
def daemon(tmp_path: Path) -> Iterator[Any]:
    started: list[StatefulDockerDaemon] = []

    def start(containers: list[dict[str, Any]] | None = None, **kwargs: Any) -> Any:
        made = StatefulDockerDaemon(
            path=tmp_path / f"docker-{len(started)}.sock",
            containers={c["Id"]: c for c in (containers or [docker_container(_ID, "web")])},
            **kwargs,
        )
        started.append(serve_docker(made))
        return made

    yield start
    for made in started:
        made.stop()


# ==================================================================== normalisation


def test_a_container_becomes_the_facts_localplane_records(daemon):
    """One inspect answer, in LocalPlane's shape, with the identity Docker guarantees."""
    provider = DockerProvider(daemon().path)
    batch = provider.observe_containers()
    assert batch.status == "ok"
    assert batch.engine_version == "29.1.3"
    facts = batch.containers[0].facts

    assert facts["container_id"] == _ID
    assert facts["short_id"] == _ID[:12]
    # Docker prefixes every name with a slash; that is a wire detail, not part of the name.
    assert facts["name"] == "web"
    assert facts["image"] == "example/app:1.0"
    assert facts["image_id"].startswith("sha256:")
    assert facts["state"] == "running"
    assert facts["running"] is True
    assert facts["started_at"] == "2026-08-20T10:00:00.123456789Z"
    # The zero instant Docker writes for "never" is reported as absent rather than as a date.
    assert facts["finished_at"] is None
    assert facts["restart_policy"] == {"name": "unless-stopped", "maximum_retry_count": 0}
    assert facts["network_mode"] == "app_default"
    assert facts["networks"][0]["name"] == "app_default"
    assert facts["networks"][0]["ip_address"] == "172.18.0.5"
    assert {(p["container_port"], p["published"]) for p in facts["ports"]} == {
        (8080, True), (9000, False)}
    assert facts["mounts"][0]["name"] == "app_data"
    assert facts["mounts"][0]["destination"] == "/data"


def test_the_parts_that_carry_credentials_do_not_survive_into_an_observation(daemon):
    """Env and command lines are dropped, and the omission is declared rather than implied."""
    observation = DockerProvider(daemon().path).observe_containers().containers[0]
    rendered = json.dumps(observation.as_dict())
    assert "hunter2" not in rendered
    assert "SECRET_TOKEN" not in rendered
    assert "run --forever" not in rendered
    assert "/var/lib/docker/containers" not in rendered
    assert any("Env" in note for note in observation.evidence["omitted"])


def test_only_the_labels_worth_operating_on_are_kept_and_the_rest_are_counted(daemon):
    facts = DockerProvider(daemon().path).observe_containers().containers[0].facts
    assert set(facts["labels"]) == {
        "com.docker.compose.project",
        "com.docker.compose.service",
        "org.opencontainers.image.version",
        "maintainer",
    }
    assert facts["labels_dropped"] == 2


@pytest.mark.parametrize(
    "container, expected_state, expected_reason",
    [
        (docker_container(_ID, "a"), HealthState.HEALTHY, "running_no_healthcheck"),
        (docker_container(_ID, "a", health="healthy"), HealthState.HEALTHY,
         "healthcheck_passing"),
        (docker_container(_ID, "a", health="unhealthy"), HealthState.FAILED,
         "healthcheck_failing"),
        (docker_container(_ID, "a", health="starting"), HealthState.UNKNOWN,
         "healthcheck_starting"),
        (docker_container(_ID, "a", state="exited", exit_code=0), HealthState.INACTIVE,
         "exited_cleanly"),
        (docker_container(_ID, "a", state="exited", exit_code=137), HealthState.FAILED,
         "exited_with_code_137"),
        (docker_container(_ID, "a", state="created"), HealthState.INACTIVE, "never_started"),
        (docker_container(_ID, "a", state="paused"), HealthState.INACTIVE, "paused"),
        (docker_container(_ID, "a", state="restarting"), HealthState.DEGRADED, "restarting"),
        (docker_container(_ID, "a", state="dead"), HealthState.FAILED, "dead"),
    ],
)
def test_health_is_derived_from_dockers_evidence_with_the_reason_attached(
    daemon, container, expected_state, expected_reason
):
    facts = DockerProvider(daemon([container]).path).observe_containers().containers[0].facts
    verdict = derive_container_health(facts)
    assert (verdict.state, verdict.reason) == (expected_state, expected_reason)


def test_an_unreadable_state_is_unknown_and_never_a_default():
    assert derive_container_health({}).state is HealthState.UNKNOWN
    assert derive_container_health({"state": ""}).reason == "state_unreadable"
    # And the daemon contradicting itself is named rather than resolved by picking a side.
    contradiction = derive_container_health({"state": "running", "running": False})
    assert contradiction.reason == "state_running_but_not_running"
    assert is_running({"state": "running", "running": False}) is None


def test_a_container_is_observed_and_never_observe_only():
    """LocalPlane can act on a container, so it is not observe_only; it retains no desired
    state for one, so it is not managed either."""
    verdict = classify_container_management({})
    assert verdict.state == ManagementState.OBSERVED
    assert verdict.reason == "container_lifecycle_candidate"


def test_identity_is_the_daemons_id_and_never_the_name():
    """A recreated container is a new object, because it is a different container."""
    identity = identify_container("host_a", _ID)
    assert identity.basis is IdentityBasis.PROVIDER_ID
    assert identity.value == _ID
    assert identity.object_id == identify_container("host_a", _ID).object_id
    assert identity.object_id != identify_container("host_a", _OTHER).object_id
    assert identity.object_id != identify_container("host_b", _ID).object_id


# ============================================================== the transport's own guards


@pytest.mark.parametrize(
    "value",
    ["../../version", "web", "", "ABCDEF123456", "abc", "e" * 65, "abcdef123456/json",
     "abcdef123456?all=1", "abcdef12345g"],
)
def test_no_argument_can_address_a_different_docker_endpoint(daemon, value):
    """The one caller-supplied value that reaches a URL, checked before a request exists."""
    provider = DockerProvider(daemon().path)
    for call in (provider.container_logs, provider.container_stats):
        with pytest.raises(InvalidContainerId):
            call(value)
    result = provider.lifecycle(value, "start")
    assert (result.outcome, result.reason) == ("not_written", "invalid_container_id")


def test_the_provider_will_not_carry_out_a_verb_outside_the_protocols_tuple(daemon):
    provider = DockerProvider(daemon().path)
    assert DOCKER_LIFECYCLE_ACTIONS == ("start", "stop", "restart")
    for verb in ("kill", "pause", "remove", "exec", "update", "rename"):
        with pytest.raises(ValueError):
            provider.lifecycle(_ID, verb)


def test_reading_containers_sends_only_gets(daemon):
    made = daemon()
    DockerProvider(made.path).observe_containers()
    DockerProvider(made.path).container_logs(_ID, tail=5)
    assert {verb for verb, _ in made.requests} == {"GET"}


# ============================================================= lifecycle outcome mapping


@pytest.mark.parametrize(
    "status, outcome, reason",
    [
        (204, "written", "daemon_acknowledged"),
        (304, "not_written", "already_in_that_state"),
        (404, "not_written", "container_not_found"),
        (409, "not_written", "conflict"),
        (400, "not_written", "bad_request"),
        # The daemon began the operation and reports that it failed. Whether anything took
        # effect before it failed is not something the response answers.
        (500, "write_unknown", "daemon_error"),
    ],
)
def test_what_the_daemon_answered_decides_the_outcome_and_nothing_else(
    daemon, status, outcome, reason
):
    made = daemon(lifecycle_status={"start": status})
    result = DockerProvider(made.path).lifecycle(_ID, "start")
    assert (result.outcome, result.reason, result.http_status) == (outcome, reason, status)


def test_a_daemon_that_was_never_reached_is_a_proof_that_nothing_happened(tmp_path: Path):
    """`not_written` is a proof: the socket does not exist, so nothing was dispatched."""
    result = DockerProvider(tmp_path / "absent.sock").lifecycle(_ID, "stop")
    assert result.outcome == "not_written"
    assert result.detail["dispatched"] is False


def test_a_read_that_cannot_reach_the_daemon_says_which_kind_of_silence(tmp_path: Path):
    provider = DockerProvider(tmp_path / "absent.sock")
    batch = provider.observe_containers()
    assert (batch.status, batch.reason) == ("failed", "docker_socket_absent")
    with pytest.raises(DockerFailure):
        provider.probe_read()


# ============================================================================ logs and stats


def test_log_frames_are_demultiplexed_in_both_of_dockers_shapes():
    line = b"2026-08-20T10:00:00.000000000Z hello\n"
    framed = b"\x01\x00\x00\x00" + len(line).to_bytes(4, "big") + line
    framed += b"\x02\x00\x00\x00" + len(line).to_bytes(4, "big") + b"2026-08-20T10:00:01.0Z bad\n"
    entries = _demultiplex(framed)
    assert [e["stream"] for e in entries] == ["stdout", "stderr"]
    assert entries[0]["message"] == "hello"
    assert entries[0]["timestamp"] == "2026-08-20T10:00:00.000000000Z"
    # A TTY container's output arrives unframed and is still read.
    raw = _demultiplex(b"2026-08-20T10:00:00.000000000Z plain\n")
    assert raw == [{"timestamp": "2026-08-20T10:00:00.000000000Z", "stream": "unknown",
                    "message": "plain"}]


def test_logs_are_bounded_and_say_when_a_bound_bit(daemon):
    made = daemon()
    made.logs = b"\x01\x00\x00\x00" + (2_000_000).to_bytes(4, "big") + b"x" * 2_000_000
    logs = DockerProvider(made.path).container_logs(_ID, tail=5000)
    assert logs["truncated"] is True
    # Asking for more lines than the ceiling is clamped rather than refused.
    assert logs["requested_lines"] == logs["line_limit"] == 2000


def test_cpu_is_null_rather_than_zero_when_the_sample_cannot_answer():
    """Zero is a number an operator would act on. Absent evidence is not zero."""
    without = _stats(_ID, {"cpu_stats": {"cpu_usage": {"total_usage": 10}}, "precpu_stats": {}})
    assert without["cpu_percent"] is None
    assert "cpu_percent" in without["gaps"]

    with_previous = _stats(_ID, {
        "read": "2026-08-20T10:00:00Z",
        "cpu_stats": {"cpu_usage": {"total_usage": 200}, "system_cpu_usage": 2000,
                      "online_cpus": 4},
        "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 1000},
        "memory_stats": {"usage": 200, "limit": 1000, "stats": {"inactive_file": 50}},
        "networks": {"eth0": {"rx_bytes": 7, "tx_bytes": 9}},
        "blkio_stats": {"io_service_bytes_recursive": [{"op": "read", "value": 4},
                                                       {"op": "write", "value": 6}]},
        "pids_stats": {"current": 3, "limit": 100},
    })
    assert with_previous["cpu_percent"] == pytest.approx(40.0)
    # `docker stats` subtracts reclaimable page cache; the raw number is kept beside it.
    assert with_previous["memory_usage_bytes"] == 150
    assert with_previous["memory_usage_raw_bytes"] == 200
    assert with_previous["network_rx_bytes"] == 7
    assert with_previous["block_write_bytes"] == 6
    assert with_previous["pids"] == 3
    assert with_previous["gaps"] == []


# ========================================================================= capability probes


def test_read_and_lifecycle_are_probed_separately_and_reported_separately(daemon, tmp_path):
    from localplane.agent.capabilities import discover_capabilities

    def probe(socket_path: Path) -> dict[str, Any]:
        return {c.capability: c for c in discover_capabilities(
            root=tmp_path / "nothing", docker_socket=socket_path)}

    made = daemon()
    available = probe(made.path)
    assert available[CAPABILITY_DOCKER_CONTAINERS_OBSERVE].status is CapabilityStatus.AVAILABLE
    assert available[CAPABILITY_DOCKER_CONTAINER_LIFECYCLE].status is CapabilityStatus.AVAILABLE
    # The lifecycle probe addresses an id no container can have, and touches nothing.
    assert ("POST", f"/containers/{PROBE_CONTAINER_ID}/start") in made.requests
    assert made.containers[_ID]["State"]["Status"] == "running"

    # A socket that answers reads and refuses write verbs: one capability, not both.
    refusing = probe(daemon(refuse_writes=True).path)
    assert refusing[CAPABILITY_DOCKER_CONTAINERS_OBSERVE].status is CapabilityStatus.AVAILABLE
    lifecycle = refusing[CAPABILITY_DOCKER_CONTAINER_LIFECYCLE]
    assert lifecycle.status is CapabilityStatus.UNAVAILABLE
    assert lifecycle.reason == "lifecycle_requests_refused"

    absent = probe(tmp_path / "absent.sock")
    assert all(
        absent[name].status is CapabilityStatus.UNAVAILABLE
        and absent[name].reason == "docker_socket_absent"
        for name in (CAPABILITY_DOCKER_CONTAINERS_OBSERVE, CAPABILITY_DOCKER_CONTAINER_LIFECYCLE)
    )


# ============================================================ the resource model and the run


@dataclass
class Estate:
    """A backend with a real agent socket in front of a Docker daemon that really moves."""

    database: Any
    daemon: StatefulDockerDaemon
    client: AgentClient
    ingestor: Ingestor
    coordinator: ObservationCoordinator
    provenance: ProvenanceService
    runs: RunService
    changes: ChangeService
    host_id: str = ""
    management_path: ManagementPathVerdict = field(
        default_factory=lambda: ManagementPathVerdict(
            resource_id=None, reason="management_path_unobserved"
        )
    )

    def observe(self) -> Any:
        result = self.coordinator.refresh_containers()
        self.host_id = result.host_id
        return result

    def container(self, name: str = "web") -> ObjectRecord:
        for record in self.ingestor.objects.list_by_kind(
            self.host_id, OBJECT_KIND_DOCKER_CONTAINER
        ):
            if record.display_name == name:
                return record
        raise AssertionError(f"no container named {name}")

    def plan(self, operation: OperationType, name: str = "web") -> Any:
        return self.runs.create(operation, self.container(name), self.management_path)

    def apply(self, operation: OperationType, name: str = "web") -> Any:
        outcome = self.plan(operation, name)
        self.changes.confirm(
            outcome.run,
            preview_id=outcome.run.preview.preview_id,
            acknowledge=True,
            expected_preview_digest=None,
            management_path=self.management_path,
        )
        return self.changes.apply(self.runs.get(outcome.run.run_id), self.management_path)

    def events(self, run_id: str) -> list[str]:
        return [e.event for e in self.changes.transcript(run_id)]


@pytest.fixture
def estate(database, fake_root: Path, sysfs_net: Path, tmp_path: Path, daemon) -> Iterator[Estate]:
    from localplane.agent.server import AgentServer

    made = daemon([docker_container(_ID, "web", state="exited", exit_code=0),
                   docker_container(_OTHER, "sidecar", state="running")])
    service = AgentService(root=fake_root, sysfs_net=sysfs_net, docker_socket=made.path,
                           helper_socket=tmp_path / "no-helper.sock")
    server = AgentServer(tmp_path / "agent.sock", service)
    server.serve_in_thread()

    client = AgentClient(server.socket_path, timeout_s=10.0)
    provenance = ProvenanceService(database)
    management = ManagementService(database, freshness_ttl_s=60.0, provenance=provenance)
    ingestor = Ingestor(database, management)
    coordinator = ObservationCoordinator(client, ingestor)
    runs = RunService(database, 60.0, OPERATIONS, provenance)
    estate = Estate(
        database=database, daemon=made, client=client, ingestor=ingestor,
        coordinator=coordinator, provenance=provenance, runs=runs,
        changes=ChangeService(database, runs,
                              build_executors(client, coordinator, ingestor.objects)),
    )
    estate.observe()
    yield estate
    server.shutdown()
    server.server_close()


def test_containers_are_localplane_objects_with_their_own_observations(estate: Estate):
    """The same pipeline every other object goes through: one sweep, one object each."""
    records = estate.ingestor.objects.list_by_kind(estate.host_id, OBJECT_KIND_DOCKER_CONTAINER)
    assert {r.display_name for r in records} == {"web", "sidecar"}
    web = estate.container()
    assert web.identity_basis == str(IdentityBasis.PROVIDER_ID)
    assert web.identity_value == _ID
    assert web.management_state == ManagementState.OBSERVED
    assert web.observation is not None
    assert web.observation.capability == CAPABILITY_DOCKER_CONTAINERS_OBSERVE
    assert web.observation.provider == "docker"
    assert web.observation.health_state == HealthState.INACTIVE
    # One sweep row, of its own capability, beside the interface ones rather than inside them.
    sweep = estate.ingestor.sweeps.latest(estate.host_id, CAPABILITY_DOCKER_CONTAINERS_OBSERVE)
    assert sweep is not None and sweep.object_count == 2


def test_docker_owns_every_container_and_that_refuses_adoption_without_blocking_execution(
    estate: Estate,
):
    """Both relations, on the daemon's own evidence, and the two consequences kept apart."""
    record = estate.container()
    provenance = estate.provenance.for_object(record)
    assert {c.relation for c in provenance.claims} == {
        OwnershipRelation.CREATED_BY, OwnershipRelation.CONFIGURED_BY}
    assert all(c.owner.provider == "docker" and c.owner.instance == _ID
               for c in provenance.claims)
    assert provenance.gaps == ()

    # Adoption is refused: adopting means retaining a desired state Docker already holds.
    eligibility = estate.provenance.eligibility(record, provenance)
    assert eligibility.eligible is False
    assert eligibility.reason == "externally_configured"

    # And the very same ownership does *not* block the lifecycle plan, because that acts
    # through Docker rather than behind it.
    plan = estate.plan(OperationType.DOCKER_CONTAINER_START).plan
    assert plan.execution.blockers == ()
    assert plan.ownership.state == "attributed"


def test_a_lifecycle_plan_is_an_action_and_says_so_everywhere(estate: Estate):
    outcome = estate.plan(OperationType.DOCKER_CONTAINER_START)
    plan = outcome.plan
    assert isinstance(plan.change, PlannedAction)
    assert (plan.change.action, plan.change.observed_state, plan.change.expected_state) == (
        "start", "exited", "running")
    assert plan.change.expected_after == "running"

    # No intent, no version, no drift: there is nothing for an action to reconcile.
    assert plan.evidence.intent_id is None
    assert plan.evidence.intent_version is None
    assert plan.evidence.drift_finding_id is None

    # No inverse, and the plan says why rather than leaving a reader to infer it.
    assert plan.recovery.mode is RecoveryMode.NONE
    assert plan.recovery.rollback_possible is False
    assert plan.recovery.armed is False
    assert "no inverse" in plan.recovery.reason

    # The store agrees about all of it.
    row = estate.database.query_one(
        "SELECT * FROM run_previews WHERE preview_id = ?", (outcome.run.preview.preview_id,))
    assert row["change_kind"] == "action"
    assert row["action"] == "start"
    assert (row["field"], row["value_type"], row["current_value"], row["desired_value"]) == (
        None, None, None, None)
    assert row["intent_id"] is None


def test_a_container_is_never_the_management_path_and_the_plan_says_which(estate: Estate):
    """Clear for the reason, and the relation still reports what the evidence says.

    The management-path model answers which *network interface* carries the operator's
    connection, and a container is not one — so the protection reason comes out
    ``operation_cannot_affect_management_path`` rather than a proven negative LocalPlane
    has not earned. The relation stays ``unknown``, because the path itself is.
    """
    plan = estate.plan(OperationType.DOCKER_CONTAINER_START).plan
    assert plan.protection.status is ProtectionStatus.CLEAR
    assert plan.protection.reason == CANNOT_AFFECT_MANAGEMENT_PATH
    assert str(plan.protection.management_path) == "unknown"
    assert "management_path_unproven" not in plan.execution.blockers


@pytest.mark.parametrize(
    "operation, target, code",
    [
        (OperationType.DOCKER_CONTAINER_START, "sidecar", "container_already_running"),
        (OperationType.DOCKER_CONTAINER_STOP, "web", "container_already_stopped"),
    ],
)
def test_a_verb_with_nothing_to_do_is_refused_rather_than_planned(
    estate: Estate, operation, target, code
):
    """The same refusal ``already_reconciled`` is: work reported as done without any done."""
    with pytest.raises(RunRefused) as raised:
        estate.plan(operation, target)
    assert raised.value.code == code
    assert estate.database.query("SELECT * FROM runs") == []


def test_restart_plans_from_either_state_because_it_is_not_a_disagreement(estate: Estate):
    """The case the field model cannot express: expected state equals observed state."""
    plan = estate.plan(OperationType.DOCKER_CONTAINER_RESTART, "sidecar").plan
    assert (plan.change.observed_state, plan.change.expected_state) == ("running", "running")


# ================================================================== the whole path, verified


def test_starting_a_container_writes_verifies_and_succeeds(estate: Estate):
    outcome = estate.apply(OperationType.DOCKER_CONTAINER_START)
    assert outcome.run.state == RunState.SUCCEEDED
    change = outcome.change
    assert change.result == ChangeResult.SUCCEEDED
    assert change.mutation_outcome == MutationOutcome.WRITTEN
    assert change.host_effect == "written"
    assert change.verification_outcome == "verified"
    assert change.verification_observed_state == "running"
    assert change.verification_observation_id is not None

    # The daemon really moved, and the proof is a fresh observation rather than the answer.
    assert estate.daemon.containers[_ID]["State"]["Status"] == "running"
    assert estate.container().observation.facts["state"] == "running"

    # An action carries no checkpoint and no rollback, and the store refuses one that claims
    # otherwise, so the absence is structural rather than a code path nobody took.
    assert change.checkpoint_id is None
    assert change.change_kind == "action"
    assert change.rollback_required is False
    assert change.rollback_attempt_id is None
    assert estate.changes.checkpoint_for(outcome.run.run_id) is None
    assert estate.database.query("SELECT * FROM run_checkpoints") == []


def test_the_transcript_of_an_action_names_what_actually_happened(estate: Estate):
    outcome = estate.apply(OperationType.DOCKER_CONTAINER_START)
    assert estate.events(outcome.run.run_id) == [
        "run_planned",
        "confirmation_satisfied",
        "confirmation_required",
        "confirmation_consumed",
        # Not `arming_started`: there is no recovery to arm, and a transcript that borrowed
        # arming's name would read like an arming that never completed.
        "target_correlated",
        "write_boundary_crossed",
        "mutation_dispatched",
        "mutation_result",
        "verification_started",
        "verification_result",
        "run_finished",
    ]


def test_a_restart_is_proven_from_dockers_own_record_of_when_it_started(estate: Estate):
    before = estate.daemon.containers[_OTHER]["State"]["StartedAt"]
    outcome = estate.apply(OperationType.DOCKER_CONTAINER_RESTART, "sidecar")
    assert outcome.run.state == RunState.SUCCEEDED
    after = estate.daemon.containers[_OTHER]["State"]["StartedAt"]
    assert after != before


def test_a_restart_that_left_the_container_running_without_restarting_it_is_not_success(
    estate: Estate,
):
    """The case a state comparison alone cannot catch, and the reason the instant is carried.

    The daemon acknowledges the request and the container is running afterwards — and it was
    running before. Docker's own record of when it last started did not move, so nothing
    was proven, and a verification that reported success here would be verifying nothing.
    """
    estate.daemon.freeze_started_at = True
    outcome = estate.apply(OperationType.DOCKER_CONTAINER_RESTART, "sidecar")

    assert outcome.run.state == RunState.RECOVERY_REQUIRED
    change = outcome.change
    assert change.mutation_outcome == MutationOutcome.WRITTEN
    assert change.verification_outcome == "mismatch"
    assert change.verification_reason == "container_did_not_start_again"
    assert change.result == ChangeResult.RECOVERY_REQUIRED
    assert change.recovery_reason == RecoveryReason.ACTION_NOT_PROVEN

    # No opposite verb was issued: the daemon saw the restart and nothing else. The only
    # other POST on the socket is the capability probe, which addresses an id no container
    # can have — so this also asserts that nothing but the probe and the change was sent.
    posts = [p for verb, p in estate.daemon.requests if verb == "POST"]
    assert [p for p in posts if PROBE_CONTAINER_ID not in p] == [
        f"/containers/{_OTHER}/restart?t=10"]
    # And the object is held, because its state is not what LocalPlane asked for.
    assert estate.changes.lock_for(outcome.run.run_id) is not None


def test_a_daemon_error_is_write_unknown_and_never_a_clean_failure(estate: Estate):
    """The daemon began the operation and reports it failed; that is not proof of nothing."""
    estate.daemon.lifecycle_status = {"start": 500}
    outcome = estate.apply(OperationType.DOCKER_CONTAINER_START)
    assert outcome.change.mutation_outcome == MutationOutcome.WRITE_UNKNOWN
    assert outcome.change.host_effect == "write_unknown"
    assert outcome.run.state == RunState.RECOVERY_REQUIRED
    assert outcome.change.recovery_reason == RecoveryReason.APPLY_WRITE_UNKNOWN
    # Verification is not attempted after write_unknown: it answers a different question.
    assert outcome.change.verification_outcome == "not_attempted"


def test_a_container_that_disappeared_is_not_written_and_ends_failed(estate: Estate):
    estate.daemon.lifecycle_status = {"start": 404}
    outcome = estate.apply(OperationType.DOCKER_CONTAINER_START)
    assert outcome.change.mutation_outcome == MutationOutcome.NOT_WRITTEN
    assert outcome.change.mutation_reason == "container_not_found"
    assert outcome.change.host_effect == "none"
    assert outcome.run.state == RunState.FAILED
    assert outcome.change.result == ChangeResult.FAILED


def test_stopping_a_container_proves_it_exited(estate: Estate):
    outcome = estate.apply(OperationType.DOCKER_CONTAINER_STOP, "sidecar")
    assert outcome.run.state == RunState.SUCCEEDED
    assert outcome.change.verification_observed_state == "exited"
    assert estate.daemon.containers[_OTHER]["State"]["Running"] is False


def test_an_unrelated_container_is_untouched_by_a_lifecycle_run(estate: Estate):
    before = json.dumps(estate.daemon.containers[_OTHER], sort_keys=True)
    estate.apply(OperationType.DOCKER_CONTAINER_START)
    assert json.dumps(estate.daemon.containers[_OTHER], sort_keys=True) == before


def test_the_executor_refuses_to_dispatch_a_verb_that_is_not_its_own():
    """A Change built for one operation cannot be carried out by another's executor."""
    executor = ContainerLifecycleExecutor(LifecycleAction.STOP, None, None, None)  # type: ignore[arg-type]
    report = executor.mutate(MutationRequest(
        kind="action", attempt_id="att_x", correlation={"container_id": _ID}, action="start"))
    assert report.outcome is MutationOutcome.NOT_WRITTEN
    assert report.reason == "action_mismatch"


def test_a_verification_reads_this_sweep_and_never_a_memory_of_the_target(estate: Estate):
    """A reading that is not from *this* read is not a read-back."""
    executor = ContainerLifecycleExecutor(
        LifecycleAction.START, estate.client, estate.coordinator, estate.ingestor.objects)
    record = estate.container()
    del estate.daemon.containers[_ID]
    attempt = executor.observe(record)
    assert attempt.taken is False
    assert attempt.failure == "target_absent"


def test_proving_a_start_needs_the_state_and_the_daemons_two_answers_to_agree():
    executor = ContainerLifecycleExecutor(LifecycleAction.START, None, None, None)  # type: ignore[arg-type]
    expectation = Expectation(kind="action", correlation={}, state="running")
    assert executor.prove({"state": "running", "running": True}, expectation).outcome.proved
    assert executor.prove({"state": "exited", "running": False}, expectation).reason == (
        "observed_state_differs")
    assert executor.prove({}, expectation).reason == "container_state_unreadable"
    assert executor.prove({"state": "running", "running": False}, expectation).reason == (
        "daemon_state_and_running_flag_disagree")


# ============================================================================ the API surface


@pytest.fixture
def api(tmp_path: Path, fake_root: Path, sysfs_net: Path, daemon) -> Iterator[Any]:
    """The public contract, over a real store, a real agent socket and a real daemon."""
    from fastapi.testclient import TestClient

    from localplane.agent.server import AgentServer
    from localplane.backend.app import create_app
    from localplane.backend.config import Settings
    from localplane.backend.db.database import open_database

    made = daemon([docker_container(_ID, "web", state="exited", exit_code=0)])
    made.logs = (b"\x01\x00\x00\x00"
                 + len(b"2026-08-20T10:00:00.000000000Z ready\n").to_bytes(4, "big")
                 + b"2026-08-20T10:00:00.000000000Z ready\n")
    made.stats = {
        "read": "2026-08-20T10:00:00Z",
        "cpu_stats": {"cpu_usage": {"total_usage": 200}, "system_cpu_usage": 2000,
                      "online_cpus": 2},
        "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 1000},
        "memory_stats": {"usage": 1000, "limit": 4000, "stats": {"inactive_file": 0}},
        "pids_stats": {"current": 5},
    }
    service = AgentService(root=fake_root, sysfs_net=sysfs_net, docker_socket=made.path,
                           helper_socket=tmp_path / "no-helper.sock")
    server = AgentServer(tmp_path / "agent.sock", service)
    server.serve_in_thread()

    settings = Settings(
        database_path=tmp_path / "store" / "localplane.db",
        agent_socket=server.socket_path, agent_timeout_s=10, freshness_ttl_s=60,
        log_level="WARNING", observe_on_startup=True,
    )
    database = open_database(settings.database_path)
    with TestClient(create_app(settings, database)) as client:
        client.post("/api/v1/docker/containers/observations/refresh")
        yield client, made
    database.close()
    server.shutdown()
    server.server_close()


def test_the_container_collection_and_detail_answer_what_docker_ps_and_inspect_would(api):
    client, _ = api
    listed = client.get("/api/v1/docker/containers").json()
    assert listed["count"] == 1
    assert listed["last_sweep"]["capability"] == CAPABILITY_DOCKER_CONTAINERS_OBSERVE

    summary = listed["containers"][0]
    detail = client.get(f"/api/v1/docker/containers/{summary['object_id']}").json()
    # The same document from the collection and from the resource. `age_seconds` is derived
    # per request and is the one field that legitimately moves between two reads.
    assert {k: v for k, v in detail.items() if k != "observation"} == {
        k: v for k, v in summary.items() if k != "observation"}
    assert {k: v for k, v in detail["observation"].items() if k != "age_seconds"} == {
        k: v for k, v in summary["observation"].items() if k != "age_seconds"}

    assert detail["name"] == "web"
    assert detail["container_id"] == _ID
    assert detail["identity"]["basis"] == "provider_id"
    assert detail["image"]["reference"] == "example/app:1.0"
    assert detail["runtime"]["state"] == "exited"
    assert detail["runtime"]["exit_code"] == 0
    assert detail["restart_policy"]["name"] == "unless-stopped"
    assert detail["container_health"]["checked"] is False
    assert detail["ports"][0]["host_port"] == 8080
    assert detail["mounts"][0]["destination"] == "/data"
    assert detail["networks"][0]["ip_address"] == "172.18.0.5"
    assert detail["labels"]["com.docker.compose.project"] == "app"
    assert detail["labels_dropped"] == 2
    assert detail["log_driver"] == "json-file"
    # Ownership travels with it, and it refuses adoption without blocking the lifecycle.
    assert detail["ownership"]["adoption"]["reason"] == "externally_configured"
    assert detail["health"]["state"] == "inactive"
    assert detail["observation"]["freshness"] == "current"


def test_logs_and_stats_are_read_live_and_stored_nowhere(api):
    client, _ = api
    object_id = client.get("/api/v1/docker/containers").json()["containers"][0]["object_id"]

    logs = client.post(f"/api/v1/docker/containers/{object_id}/logs?tail=5").json()
    assert logs["line_count"] == 1
    assert logs["lines"][0] == {
        "timestamp": "2026-08-20T10:00:00Z", "stream": "stdout", "message": "ready"}
    assert logs["requested_lines"] == 5

    stats = client.post(f"/api/v1/docker/containers/{object_id}/stats").json()
    assert stats["cpu_percent"] == pytest.approx(20.0)
    assert stats["memory_usage_bytes"] == 1000
    assert stats["memory_limit_bytes"] == 4000
    assert stats["pids"] == 5
    assert stats["gaps"] == []

    # Nothing about either is kept: Docker holds the logs and there is no metrics database.
    tables = {r["name"] for r in client.app.state.context.database.query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert not any("log" in name or "stat" in name or "metric" in name for name in tables)


def test_a_container_run_is_created_through_the_ordinary_run_surface(api):
    client, made = api
    object_id = client.get("/api/v1/docker/containers").json()["containers"][0]["object_id"]

    created = client.post("/api/v1/runs", json={
        "operation": {"type": "docker.container.start", "object_id": object_id}})
    assert created.status_code == 201
    body = created.json()
    assert body["operation"] == "docker.container.start"
    assert body["preview"]["what"]["kind"] == "action"
    assert body["preview"]["what"]["action"] == "start"
    assert body["preview"]["what"]["expected_state"] == "running"
    assert body["preview"]["what"]["field"] is None
    assert body["host_effect"] == "none"
    assert body["change_created"] is False
    # Nothing was asked of the daemon by planning.
    assert not any(verb == "POST" and PROBE_CONTAINER_ID not in path
                   for verb, path in made.requests)

    preview = client.get(f"/api/v1/runs/{body['run_id']}/preview").json()
    assert preview["what"]["kind"] == "action"
    assert preview["what"]["expected_after"] == "running"
    assert preview["why"]["intent_id"] is None
    assert preview["why"]["reason"] == "operator_requested_action"
    assert preview["recovery"]["mode"] == "none"
    assert preview["recovery"]["rollback_possible"] is False
    assert preview["recovery"]["restores_value"] is None
    assert preview["verification"]["expect"] == "running"


@pytest.mark.parametrize(
    "body",
    [
        {"type": "docker.container.kill", "object_id": "obj_x"},
        {"type": "docker.container.remove", "object_id": "obj_x"},
        {"type": "docker.container.exec", "object_id": "obj_x"},
        {"type": "docker.container.start", "object_id": "obj_x", "command": "sh"},
        {"type": "docker.container.start", "object_id": "obj_x", "timeout": 1},
        {"type": "docker.container.start", "object_id": "obj_x", "signal": "SIGKILL"},
    ],
)
def test_the_request_carries_a_type_and_a_target_and_will_accept_nothing_else(api, body):
    client, _ = api
    assert client.post("/api/v1/runs", json={"operation": body}).status_code == 422


def test_there_is_no_passthrough_to_the_docker_daemon(api):
    """Named absences, asserted. A control plane is not a proxy."""
    client, _ = api
    object_id = client.get("/api/v1/docker/containers").json()["containers"][0]["object_id"]
    for path in (
        "/api/v1/docker",
        "/api/v1/docker/version",
        "/api/v1/docker/images",
        "/api/v1/docker/volumes",
        "/api/v1/docker/networks",
        "/api/v1/docker/exec",
        f"/api/v1/docker/containers/{object_id}/exec",
        f"/api/v1/docker/containers/{object_id}/start",
        f"/api/v1/docker/containers/{object_id}/kill",
        f"/api/v1/docker/containers/{object_id}/terminal",
        f"/api/v1/docker/containers/{object_id}/inspect",
    ):
        assert client.get(path).status_code in (404, 405), path
        assert client.post(path).status_code in (404, 405), path


# ================================================================== the migration, staged


def _seed_a_pre_docker_store(database: Any) -> None:
    """A 0001–0007 store holding one of everything the rebuilds touch.

    Not an empty database: the rebuilds copy rows, restate one column from another table and
    widen four CHECKs, and none of that is exercised by a store with nothing in it.
    """
    connection = database.connection
    now = "2026-08-01T00:00:00.000000+00:00"
    preview: dict[str, Any] = {
        "preview_id": "prv_old", "preview_digest": "sha256:old", "digest_version": 2,
        "operation": "network.interface.reconcile_mtu", "field": "mtu",
        "value_type": "integer", "current_value": 1400, "desired_value": 1500,
        "intent_id": "i", "intent_version": 1, "intent_capability": "network.observe",
        "intent_provider": "linux", "observation_id": "ob", "sweep_id": "s",
        "observed_at": now, "ownership_state": "unattributed",
        "ownership_reason": "no_provider_claim", "protection_status": "unknown",
        "protection_unresolved": '["management_path"]',
        "protection_management_path": "unknown", "protection_reason": "r",
        "risk_tier": "medium", "confirmation_required": 1, "confirmation_method": "typed",
        "confirmation_source": "policy", "confirmation_policy": "p",
        "confirmation_token_issued": 0, "execution_availability": "available",
        "execution_eligibility": "eligible", "execution_provider": "linux.link",
        "required_capability": "network.interface.set_mtu", "capability_declared": 1,
        "recovery_mode": "auto", "recovery_rollback_possible": 1, "recovery_armed": 0,
        "recovery_guarantee": "none", "recovery_reason": "r",
        "verification_capability": "network.observe", "verification_provider": "linux",
        "verification_condition": "c", "verification_executed": 0, "published_at": now,
    }
    with database.transaction():
        connection.execute(
            "INSERT INTO hosts (host_id,identity_basis,identity_confidence,first_seen_at,"
            "last_seen_at) VALUES ('h','machine_id','high',?,?)", (now, now))
        connection.execute(
            "INSERT INTO objects (object_id,host_id,kind,identity_basis,identity_value,"
            "identity_confidence,display_name,management_state,management_reason,"
            "first_seen_at,last_seen_at) VALUES "
            "('o','h','network.interface','kernel_name','eth0','low','eth0','observed','r',?,?)",
            (now, now))
        connection.execute(
            "INSERT INTO observation_sweeps (sweep_id,host_id,capability,provider,"
            "provider_version,status,started_at,completed_at,received_at,object_count) VALUES "
            "('s','h','network.observe','linux','1','ok',?,?,?,1)", (now, now, now))
        connection.execute(
            "INSERT INTO observations (observation_id,sweep_id,host_id,object_id,capability,"
            "provider,provider_version,method,fidelity,observed_at,received_at,health_state,"
            "health_reason,facts) VALUES ('ob','s','h','o','network.observe','linux','1','m',"
            "'complete',?,?,'healthy','ok','{}')", (now, now))
        connection.execute(
            "INSERT INTO intents (intent_id,object_id,host_id,version,supersedes,"
            "schema_version,origin,capability,provider,provider_version,observation_id,"
            "sweep_id,observed_at,created_at) VALUES ('i','o','h',1,NULL,1,'adopt',"
            "'network.observe','linux','1','ob','s',?,?)", (now, now))
        connection.execute(
            "UPDATE objects SET management_state='managed', active_intent_id='i'")
        columns = [r[1] for r in connection.execute("PRAGMA table_info(run_previews)")]
        present = [c for c in columns if c in preview]
        connection.execute(
            f"INSERT INTO run_previews ({','.join(present)}) "
            f"VALUES ({','.join('?' for _ in present)})",
            tuple(preview[c] for c in present))
        connection.execute(
            "INSERT INTO runs (run_id,host_id,object_id,operation,state,preview_id,"
            "host_effect,created_at,finished_at) VALUES ('run','h','o',"
            "'network.interface.reconcile_mtu','succeeded','prv_old','written',?,?)", (now, now))
        connection.execute(
            "INSERT INTO run_confirmations (confirmation_id,run_id,preview_id,preview_digest,"
            "digest_version,required_method,method,policy,source,satisfied_at,consumed_at,"
            "consumed_by_attempt_id) VALUES ('cnf','run','prv_old','sha256:old',2,"
            "'acknowledge','acknowledge','p','unauthenticated_request',?,?,'att')", (now, now))
        connection.execute(
            "INSERT INTO run_checkpoints (checkpoint_id,run_id,preview_id,host_id,object_id,"
            "intent_id,intent_version,field,value_type,before_value,desired_value,"
            "observation_id,observed_at,protection_management_path,execution_correlation,"
            "armed_at) VALUES ('ckp','run','prv_old','h','o','i',1,'mtu','integer',1400,1500,"
            "'ob',?,'not_on_management_path','{\"ifindex\": 2}',?)", (now, now))
        connection.execute(
            "INSERT INTO changes (change_id,run_id,preview_id,checkpoint_id,host_id,object_id,"
            "operation,field,value_type,before_value,desired_value,created_at,"
            "apply_attempt_id,dispatch_began_at,mutation_outcome,host_effect,"
            "verification_outcome,verification_observation_id,verification_observed_value,"
            "result,finished_at) VALUES ('chg','run','prv_old','ckp','h','o',"
            "'network.interface.reconcile_mtu','mtu','integer',1400,1500,?,'apl',?,'written',"
            "'written','verified','ob',1500,'succeeded',?)", (now, now, now))
        connection.execute(
            "INSERT INTO run_events (event_id,run_id,change_id,sequence,event,occurred_at,"
            "detail) VALUES ('evt','run','chg',1,'run_planned',?,'{}')", (now,))


def test_upgrading_a_0007_store_keeps_its_data_its_checksums_and_its_schema(tmp_path: Path):
    """Five tables rebuilt with foreign keys suspended, and nothing lost or invented.

    The escape this migration declares is the thing under test as much as the schema is: the
    engine turns enforcement off *around* the transaction, not inside it, and runs
    ``PRAGMA foreign_key_check`` before committing — so a rebuild that orphaned a row could
    not have got this far.
    """
    import shutil

    from localplane.backend.db.database import MIGRATIONS_DIR, open_database

    staged = tmp_path / "at_0007"
    staged.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(path.stem.split("_")[0]) <= 7:
            shutil.copy(path, staged / path.name)

    old = open_database(tmp_path / "staged.db", staged)
    _seed_a_pre_docker_store(old)
    before = {r["version"]: r["checksum"] for r in old.query("SELECT * FROM schema_migrations")}
    assert sorted(before) == [1, 2, 3, 4, 5, 6, 7]
    old.close()

    upgraded = open_database(tmp_path / "staged.db")
    fresh = open_database(tmp_path / "fresh.db")
    try:
        after = {r["version"]: r["checksum"]
                 for r in upgraded.query("SELECT * FROM schema_migrations")}
        assert sorted(after) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
        assert {v: before[v] for v in before} == {v: after[v] for v in before}

        # Every pre-Docker row is a field change and says so, and none of them acquired an
        # action, a verb or a state.
        preview = dict(upgraded.query_one("SELECT * FROM run_previews WHERE preview_id='prv_old'"))
        assert preview["change_kind"] == "field"
        assert (preview["current_value"], preview["desired_value"]) == (1400, 1500)
        assert preview["intent_id"] == "i"
        assert (preview["action"], preview["observed_state"], preview["expected_state"]) == (
            None, None, None)

        change = dict(upgraded.query_one("SELECT * FROM changes WHERE change_id='chg'"))
        assert change["change_kind"] == "field"
        assert change["checkpoint_id"] == "ckp"
        assert change["result"] == "succeeded"
        assert change["verification_observed_value"] == 1500
        assert change["verification_observed_state"] is None
        # Restated from the checkpoint rather than invented: the correlation a pre-0008
        # change used is where its executor read it, and now it is on the row too.
        assert change["execution_correlation"] == '{"ifindex": 2}'

        # The confirmation this store already held is an *apply* confirmation, restated by
        # 0009 rather than reinterpreted: it is the only thing one could have authorised.
        assert upgraded.query_one(
            "SELECT purpose FROM run_confirmations")["purpose"] == "apply"
        assert upgraded.query("SELECT * FROM change_recovery_attempts") == []

        for table, expected in (("runs", 1), ("run_confirmations", 1), ("run_checkpoints", 1),
                                ("run_events", 1), ("objects", 1), ("intents", 1),
                                ("observations", 1)):
            assert upgraded.query_one(f"SELECT count(*) c FROM {table}")["c"] == expected

        assert upgraded.query("PRAGMA foreign_key_check") == []
        assert upgraded.query_one("PRAGMA integrity_check")[0] == "ok"
        # Foreign keys are enforced again after the migration, not left off.
        assert upgraded.query_one("PRAGMA foreign_keys")[0] == 1

        def schema(db: Any) -> list[Any]:
            return sorted(
                (r["type"], r["name"], r["sql"])
                for r in db.query(
                    "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
            )

        assert schema(upgraded) == schema(fresh)
    finally:
        upgraded.close()
        fresh.close()


def test_only_migrations_that_declared_it_suspend_foreign_keys():
    """The escape is a declaration inside the checksummed file, and it is greppable.

    Five migrations declare it and all are named: 0008 rebuilt `objects`; 0010 rebuilt
    four referenced tables; 0012 rebuilds the referenced Run/preview pair to add the
    closed systemd planning vocabulary; 0013 rebuilds `run_previews` again to add the
    backend self-impact derivation; and 0014 rebuilds it once more together with
    `run_confirmations` to add the override authority. Another appearing without a change
    that argued for it fails here.
    """
    from localplane.backend.db.database import (
        FOREIGN_KEYS_OFF_DIRECTIVE,
        MIGRATIONS_DIR,
        load_migrations,
    )

    declared = {m.name for m in load_migrations(MIGRATIONS_DIR) if m.suspends_foreign_keys}
    assert declared == {
        "docker", "connection_guard", "systemd_lifecycle", "self_impact",
        "self_impact_override", "systemd_lifecycle_changes",
    }
    # And it cannot be changed without the checksum noticing, because it travels in the file.
    for name in (
        "0008_docker.sql",
        "0010_connection_guard.sql",
        "0012_systemd_lifecycle.sql",
        "0013_self_impact.sql",
        "0014_self_impact_override.sql",
        "0015_systemd_lifecycle_changes.sql",
    ):
        assert FOREIGN_KEYS_OFF_DIRECTIVE in (MIGRATIONS_DIR / name).read_text()


def test_the_store_accepts_no_operation_or_change_kind_outside_the_vocabulary(estate: Estate):
    import sqlite3

    outcome = estate.plan(OperationType.DOCKER_CONTAINER_START)
    for name in ("docker.container.kill", "docker.container.remove", "docker.exec", ""):
        with pytest.raises(sqlite3.IntegrityError):
            estate.database.connection.execute(
                "UPDATE runs SET operation = ? WHERE run_id = ?", (name, outcome.run.run_id))
    for kind in ("mutation", "command", ""):
        with pytest.raises(sqlite3.IntegrityError):
            estate.database.connection.execute(
                "UPDATE run_previews SET change_kind = ? WHERE preview_id = ?",
                (kind, outcome.run.preview.preview_id))
