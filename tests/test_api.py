"""The HTTP surface, over a real agent socket and a real store.

The agent behind these tests reads the fixture sysfs tree, so the assertions are about
exact values rather than "something plausible came back". ``test_live_host.py`` does the
same against the machine.
"""

from __future__ import annotations

import concurrent.futures
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from localplane.agent.server import AgentServer
from localplane.agent.service import AgentService
from tests.conftest import AuthenticatedTestClient, create_authenticated_app
from localplane.backend.config import Settings
from localplane.backend.db.database import Database, open_database
from localplane.backend.db.repositories import HostRepository
from tests.conftest import FakeRunner


class ContentionTrackingRLock:
    """An RLock that proves another request attempted to enter while it was held."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.contender_attempted = threading.Event()

    def acquire(self) -> bool:
        if self._lock.acquire(blocking=False):
            return True
        self.contender_attempted.set()
        self._lock.acquire()
        return True

    def release(self) -> None:
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_args) -> None:
        self.release()


@pytest.fixture
def running_agent(
    tmp_path: Path,
    fake_root: Path,
    populated_sysfs: Path,
    working_runner: FakeRunner,
    absent_docker: Path,
):
    service = AgentService(
        root=fake_root,
        sysfs_net=populated_sysfs,
        runner=working_runner,
        docker_socket=absent_docker,
    )
    server = AgentServer(tmp_path / "run" / "agent.sock", service)
    thread = server.serve_in_thread()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _settings(tmp_path: Path, socket_path: Path, observe: bool = True) -> Settings:
    return Settings(
        database_path=tmp_path / "store" / "localplane.db",
        agent_socket=socket_path,
        agent_timeout_s=10,
        freshness_ttl_s=60,
        log_level="WARNING",
        observe_on_startup=observe,
    )


@pytest.fixture
def client(tmp_path: Path, running_agent: AgentServer) -> Iterator[TestClient]:
    settings = _settings(tmp_path, running_agent.socket_path)
    database = open_database(settings.database_path)
    with AuthenticatedTestClient(create_authenticated_app(settings, database)) as test_client:
        yield test_client
    database.close()


@pytest.fixture
def agentless_client(tmp_path: Path) -> Iterator[TestClient]:
    """A backend whose agent is not running — a state it must survive and report."""
    settings = _settings(tmp_path, tmp_path / "absent" / "agent.sock")
    database = open_database(settings.database_path)
    with AuthenticatedTestClient(create_authenticated_app(settings, database)) as test_client:
        yield test_client
    database.close()


def interfaces_by_name(client: TestClient) -> dict:
    return {i["name"]: i for i in client.get("/api/v1/network/interfaces").json()["interfaces"]}


# --------------------------------------------------------------------------------- status


def test_status_is_backend_liveness_only(client: TestClient):
    body = client.get("/api/v1/status").json()
    assert body["status"] == "ok"
    assert body["database"]["schema_versions"] == [
        1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16
    ]


def test_status_works_without_an_agent(agentless_client: TestClient):
    """A backend that cannot answer while the agent is down cannot report that it is."""
    assert agentless_client.get("/api/v1/status").status_code == 200


def test_parallel_sync_requests_serialize_shared_connection_transactions(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """A second real request contends, then owns a separate complete transaction."""
    database = client.app.state.context.database
    tracked_lock = ContentionTrackingRLock()
    database._lock = tracked_lock
    original_transaction = Database.transaction
    first_transaction_open = threading.Event()
    sequence_lock = threading.Lock()
    call_count = 0

    @contextmanager
    def force_first_two_transactions_to_overlap(self: Database):
        nonlocal call_count
        assert self is database
        with sequence_lock:
            call_count += 1
            call_number = call_count

        if call_number == 1:
            with original_transaction(self) as connection:
                first_transaction_open.set()
                assert tracked_lock.contender_attempted.wait(timeout=5)
                yield connection
            return

        assert first_transaction_open.wait(timeout=5)
        with original_transaction(self) as connection:
            yield connection

    monkeypatch.setattr(Database, "transaction", force_first_two_transactions_to_overlap)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(client.post, "/api/v1/network/observations/refresh")
            for _ in range(2)
        ]
        responses = [future.result(timeout=10) for future in futures]

    assert tracked_lock.contender_attempted.is_set()
    assert [response.status_code for response in responses] == [200, 200]
    assert len({response.json()["sweep_id"] for response in responses}) == 2
    assert client.get("/api/v1/network/interfaces").json()["count"] == 9


def test_concurrent_request_cannot_read_state_that_another_request_rolls_back(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """A real read contends on the connection and resumes only after writer rollback."""
    database = client.app.state.context.database
    tracked_lock = ContentionTrackingRLock()
    database._lock = tracked_lock
    original_hostname = client.get("/api/v1/host").json()["configured_hostname"]
    uncommitted_hostname = "proof-uncommitted-hostname"
    update_visible = threading.Event()
    original_upsert = HostRepository.upsert

    class ProofRollback(RuntimeError):
        pass

    def update_then_rollback(self: HostRepository, identity: dict, now: str) -> str:
        host_id = original_upsert(self, identity, now)
        self._db.execute(
            "UPDATE hosts SET configured_hostname = ? WHERE host_id = ?",
            (uncommitted_hostname, host_id),
        )
        update_visible.set()
        assert tracked_lock.contender_attempted.wait(timeout=5)
        raise ProofRollback("force the request transaction to roll back")

    monkeypatch.setattr(HostRepository, "upsert", update_then_rollback)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(client.post, "/api/v1/network/observations/refresh")
        assert update_visible.wait(timeout=5)
        reader = pool.submit(client.get, "/api/v1/host")
        with pytest.raises(ProofRollback):
            writer.result(timeout=10)
        observed_after_rollback = reader.result(timeout=10)

    assert tracked_lock.contender_attempted.is_set()
    assert observed_after_rollback.status_code == 200
    assert observed_after_rollback.json()["configured_hostname"] == original_hostname
    assert client.get("/api/v1/host").json()["configured_hostname"] == original_hostname


# ----------------------------------------------------------------------------------- host


def test_host_reports_identity_and_freshness(client: TestClient):
    body = client.get("/api/v1/host").json()
    assert body["host_id"].startswith("host_")
    assert body["identity_basis"] == "machine_id"
    assert body["identity_confidence"] == "high"
    assert body["os_id"] == "debian"
    assert body["configured_hostname"] == "fixture-host"
    assert body["freshness"] == "current"
    assert body["identity_gaps"] == []


def test_an_unknown_host_is_a_structured_404(agentless_client: TestClient):
    response = agentless_client.get("/api/v1/host")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "host_unknown"


# ---------------------------------------------------------------------------------- agent


def test_agent_reports_reachable_and_unprivileged(client: TestClient):
    body = client.get("/api/v1/agent").json()
    assert body["reachable"] is True
    assert body["source"] == "live"
    assert body["error"] is None
    assert body["agent"]["transport"] == "af_unix"
    assert body["agent"]["process_isolated"] is True
    assert body["agent"]["privilege"] == "unprivileged"


def test_an_unreachable_agent_is_reported_as_such_not_as_a_server_error(
    agentless_client: TestClient,
):
    response = agentless_client.get("/api/v1/agent")
    assert response.status_code == 200
    body = response.json()
    assert body["reachable"] is False
    assert body["error"]["code"] == "agent_unavailable"
    assert body["agent"] is None


def test_capabilities_are_live_and_carry_their_status(client: TestClient):
    body = client.get("/api/v1/agent/capabilities").json()
    assert body["reachable"] is True
    assert body["source"] == "live"
    capabilities = {c["capability"]: c for c in body["capabilities"]}
    assert set(capabilities) == {
        "host.observe",
        "network.observe",
        "network.providers.observe",
        "network.route.observe",
        "network.interface.set_mtu",
        "network.interface.mtu_guard",
        "docker.containers.observe",
        "docker.container.lifecycle",
        "systemd.units.observe",
        "systemd.service.lifecycle",
        "systemd.service.lifecycle_context.observe",
    }
    assert capabilities["network.observe"]["status"] == "available"
    assert capabilities["network.observe"]["mutating"] is False
    assert capabilities["network.observe"]["detail"]["methods"]["sysfs"] == "ok"


def test_only_the_declared_capabilities_describe_mutating_mechanisms(client: TestClient):
    """Four, and they are named. Every other capability is still a read.

    The assertion was `all(mutating is False)` while that was true, then an equality against
    one name, then two, and it is three now — named rather than counted, because a count is
    the assertion that fails to notice a swap. The MTU setter reaches the kernel through a
    privileged helper; the Docker lifecycle capability reaches a daemon that already owns
    what it is changing; the connection guard writes nothing itself and can cause the first
    to happen with no further request. The systemd capability describes the closed typed
    mechanism Part B will dispatch; Part A exposes no mutating method for it.
    """
    body = client.get("/api/v1/agent/capabilities").json()
    mutating = {c["capability"] for c in body["capabilities"] if c["mutating"]}
    assert mutating == {
        "network.interface.set_mtu",
        "network.interface.mtu_guard",
        "docker.container.lifecycle",
        "systemd.service.lifecycle",
    }


def test_capabilities_fall_back_to_what_was_recorded_and_say_so(
    tmp_path: Path, running_agent: AgentServer
):
    settings = _settings(tmp_path, running_agent.socket_path)
    database = open_database(settings.database_path)
    with AuthenticatedTestClient(create_authenticated_app(settings, database)) as client:
        assert client.get("/api/v1/agent/capabilities").json()["source"] == "live"
        running_agent.shutdown()
        running_agent.server_close()
        body = client.get("/api/v1/agent/capabilities").json()
        assert body["reachable"] is False
        assert body["source"] == "recorded"
        assert body["error"]["code"] == "agent_unavailable"
        assert {c["capability"] for c in body["capabilities"]} == {
            "host.observe",
            "network.observe",
            "network.providers.observe",
            "network.route.observe",
            "network.interface.set_mtu",
            "network.interface.mtu_guard",
            "docker.containers.observe",
            "docker.container.lifecycle",
            "systemd.units.observe",
            "systemd.service.lifecycle",
            "systemd.service.lifecycle_context.observe",
        }
    database.close()


# -------------------------------------------------------------------------------- network


def test_interfaces_are_observed_at_startup(client: TestClient):
    body = client.get("/api/v1/network/interfaces").json()
    assert body["count"] == 9
    assert body["last_sweep"]["status"] == "ok"
    assert body["last_sweep"]["object_count"] == 9


def test_an_interface_carries_every_axis_separately(client: TestClient):
    eth0 = interfaces_by_name(client)["eth0"]
    assert eth0["management"] == {"state": "observed", "reason": "management_candidate"}
    assert eth0["health"] == {"state": "inactive", "reason": "no_carrier"}
    assert eth0["reconciliation"] is None
    assert eth0["observation"]["freshness"] == "current"
    assert eth0["identity"]["basis"] == "permanent_mac"


def test_an_observed_object_does_not_drift(client: TestClient):
    """Null reconciliation is not in_sync: there is no intent to agree with."""
    for interface in interfaces_by_name(client).values():
        assert interface["management"]["state"] in {"observed", "observe_only"}
        assert interface["reconciliation"] is None


def test_unknown_link_values_are_null_not_zero(client: TestClient):
    interfaces = interfaces_by_name(client)
    assert interfaces["eth0"]["link"]["speed_mbps"] is None
    assert interfaces["eth0"]["link"]["duplex"] is None
    assert interfaces["wlan0"]["link"]["carrier"] is None
    assert interfaces["eth1"]["link"]["speed_mbps"] == 1000


def test_no_addresses_and_no_address_source_are_distinguishable_over_http(client: TestClient):
    assert interfaces_by_name(client)["eth0"]["addresses"] == []


def test_addresses_are_returned_with_their_lifetimes(client: TestClient):
    address = interfaces_by_name(client)["eth1"]["addresses"][0]
    assert address["address"] == "192.0.2.215"
    assert address["prefix_length"] == 24
    assert address["dynamic"] is True


def test_management_stances_are_reported_with_reasons(client: TestClient):
    interfaces = interfaces_by_name(client)
    assert interfaces["lo"]["management"] == {"state": "observe_only", "reason": "loopback"}
    assert interfaces["veth0"]["management"]["reason"] == "ephemeral_virtual_pair"
    # A bridge is a management candidate like any other configurable link. Whether one
    # is *actually* somebody else's is an ownership question, and it is answered on the
    # ownership axis rather than by narrowing LocalPlane's stance towards it.
    assert interfaces["docker0"]["management"] == {
        "state": "observed",
        "reason": "management_candidate",
    }


def test_a_single_interface_can_be_fetched(client: TestClient):
    eth0 = interfaces_by_name(client)["eth0"]
    body = client.get(f"/api/v1/network/interfaces/{eth0['object_id']}").json()
    assert body["object_id"] == eth0["object_id"]
    assert body["name"] == "eth0"


def test_an_unknown_object_is_a_structured_404(client: TestClient):
    response = client.get("/api/v1/network/interfaces/obj_does_not_exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "object_not_found"


def test_evidence_is_available_for_an_observation(client: TestClient):
    eth0 = interfaces_by_name(client)["eth0"]
    body = client.get(f"/api/v1/network/interfaces/{eth0['object_id']}/evidence").json()
    assert body["evidence"]["sysfs"]["flags"] == "0x1003"
    assert body["evidence"]["sysfs"]["speed"] == "-1"
    assert body["evidence"]["commands"][0]["argv"][0] == "ip"


def test_the_interface_list_is_empty_and_explained_when_the_agent_never_answered(
    agentless_client: TestClient,
):
    response = agentless_client.get("/api/v1/network/interfaces")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "host_unknown"


# ------------------------------------------------------------------------------- refresh


def test_refresh_observes_and_records_a_sweep(client: TestClient):
    body = client.post("/api/v1/network/observations/refresh").json()
    assert body["status"] == "ok"
    assert body["object_count"] == 9
    assert body["observation_count"] == 9
    assert body["missing"] == []
    assert client.get("/api/v1/observations/sweeps").json()["count"] == 2


def test_refresh_can_ask_for_named_interfaces(client: TestClient):
    body = client.post(
        "/api/v1/network/observations/refresh", params={"names": ["eth0", "ghost0"]}
    ).json()
    assert body["object_count"] == 1
    assert body["missing"] == ["ghost0"]


def test_refresh_without_an_agent_is_a_503_with_a_code(agentless_client: TestClient):
    response = agentless_client.post("/api/v1/network/observations/refresh")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "agent_unavailable"


def test_reads_do_not_observe(client: TestClient):
    """A page refresh must not be able to change what LocalPlane has recorded."""
    before = client.get("/api/v1/observations/sweeps").json()["count"]
    for _ in range(3):
        client.get("/api/v1/network/interfaces")
        client.get("/api/v1/host")
    assert client.get("/api/v1/observations/sweeps").json()["count"] == before


def test_re_observing_does_not_duplicate_objects(client: TestClient):
    client.post("/api/v1/network/observations/refresh")
    client.post("/api/v1/network/observations/refresh")
    body = client.get("/api/v1/network/interfaces").json()
    assert body["count"] == 9
    assert len({i["object_id"] for i in body["interfaces"]}) == 9


def test_a_targeted_read_does_not_replace_the_latest_inventory_sweep(client: TestClient):
    before = client.get("/api/v1/network/interfaces").json()
    inventory_sweep_id = before["last_sweep"]["sweep_id"]
    eth0 = next(item for item in before["interfaces"] if item["name"] == "eth0")
    assert eth0["observed_in_latest_sweep"] is True
    targeted = client.post(
        "/api/v1/network/observations/refresh", params={"names": ["eth1"]}
    ).json()
    after = client.get("/api/v1/network/interfaces").json()
    assert after["last_sweep"]["sweep_id"] == inventory_sweep_id
    assert after["last_sweep"]["scope"] == "inventory"
    refreshed = {item["name"]: item for item in after["interfaces"]}
    assert refreshed["eth1"]["observation"]["sweep_id"] == targeted["sweep_id"]
    assert refreshed["eth1"]["observed_in_latest_sweep"] is True
    assert refreshed["eth0"]["observed_in_latest_sweep"] is True


# --------------------------------------------------------------------------------- schema


def test_the_openapi_document_describes_every_endpoint(client: TestClient):
    document = client.get("/openapi.json").json()
    description = document["info"]["description"]
    assert "Exactly two endpoints in this API can result in a host write" in description
    assert "POST /api/v1/runs/{id}/apply" in description
    assert "POST /api/v1/changes/{id}/recovery/retry" in description
    assert "systemd to start, stop or restart one validated service unit" in description
    assert "Exactly one endpoint" not in description
    paths = document["paths"]
    assert set(paths) == {
        "/api/v1/session",
        "/api/v1/status",
        "/api/v1/host",
        "/api/v1/agent",
        "/api/v1/agent/capabilities",
        "/api/v1/network/interfaces",
        "/api/v1/network/interfaces/{object_id}",
        "/api/v1/network/interfaces/{object_id}/evidence",
        "/api/v1/network/interfaces/{object_id}/provenance",
        "/api/v1/network/interfaces/{object_id}/protection",
        "/api/v1/network/interfaces/{object_id}/adopt",
        "/api/v1/network/interfaces/{object_id}/release",
        "/api/v1/network/interfaces/{object_id}/intent",
        "/api/v1/network/interfaces/{object_id}/intent/history",
        "/api/v1/network/interfaces/{object_id}/intent/revise",
        "/api/v1/network/interfaces/{object_id}/intent/adopt-runtime",
        "/api/v1/network/interfaces/{object_id}/reconciliation",
        "/api/v1/network/observations/refresh",
        "/api/v1/docker/containers",
        "/api/v1/docker/containers/{object_id}",
        "/api/v1/docker/containers/observations/refresh",
        "/api/v1/docker/containers/{object_id}/logs",
        "/api/v1/docker/containers/{object_id}/stats",
        "/api/v1/systemd/units",
        "/api/v1/systemd/units/{object_id}",
        "/api/v1/systemd/observations/refresh",
        "/api/v1/management-path",
        "/api/v1/management-path/observations/refresh",
        "/api/v1/observations/sweeps",
        "/api/v1/findings",
        "/api/v1/findings/{finding_id}",
        "/api/v1/runs",
        "/api/v1/runs/{run_id}",
        "/api/v1/runs/{run_id}/preview",
        "/api/v1/runs/{run_id}/cancel",
        "/api/v1/runs/{run_id}/confirm",
        "/api/v1/runs/{run_id}/self-impact-override",
        "/api/v1/runs/{run_id}/apply",
        "/api/v1/runs/{run_id}/guard/keep",
        "/api/v1/changes",
        "/api/v1/changes/{change_id}",
        "/api/v1/changes/{change_id}/recovery/confirm",
        "/api/v1/changes/{change_id}/recovery/retry",
        "/api/v1/changes/{change_id}/recovery/resolve",
    }


def test_every_endpoint_that_is_not_a_get_is_named(client: TestClient):
    """Twenty endpoints are not exclusively a `GET`, and exactly two can change the host.

    Most of them change nothing at all. **Four read the host and record or return what
    they saw** — the interface sweep, container sweep, systemd sweep, and management-path
    observation — and two more read the host and record *nothing*: container logs and
    container stats are `POST` for the reason every observation here is, that a `GET` in
    this API never contacts the host, and they store nothing because Docker keeps the logs
    and this build has no metrics database. Adopt and release move LocalPlane's stance
    towards an object; revise and adopt-runtime move which version of its own intent that
    object points at; creating a Run publishes a plan, confirming one records that somebody
    satisfied the confirmation it requires, and cancelling one ends it before any boundary.

    **Two of them can write, and the count did not go up with the connection guard.**
    `apply` executes a published plan; `recovery/retry` re-attempts the end state of a change
    that could not be settled, and it is a write path because completing a recovery may
    genuinely require one — but it looks first, and it dispatches nothing unless a fresh
    reading fails to prove the end state and an operator has granted authority that has not
    been used. `recovery/confirm` grants that authority and touches nothing;
    `recovery/resolve` releases a hold and is **structurally** incapable of a mutation,
    because the store refuses a resolution row that carries one.

    **`guard/keep` still cannot write.** It contacts the host — to
    release a guard, or to ask what one did — and releasing a guard can only *prevent* the
    reversal it was holding. There is no parameter on it through which a value, a command,
    an interface or a deadline could arrive; what authorises keeping a guarded change is
    not something a caller can send, it is the management path the request itself re-proves
    over the object that was changed.

    **The eighteenth is systemd observation and it cannot write either.** It takes no
    body, reads the fixed loaded-unit scope over the provider's closed D-Bus vocabulary,
    and records only generic LocalPlane observations.

    **The nineteenth is the self-impact override, and it cannot write either.** It records
    that an operator accepted one typed hazard the published plan already states — that
    carrying it out may interrupt LocalPlane itself. It contacts the host only to re-derive
    the plan's own safety evidence for this request, exactly as confirming does. It takes no
    value, no verb, no target and no blocker to bypass; it removes no blocker and moves no
    protection verdict; and it does not stand in for the confirmation the plan separately
    requires. It is not a second write path — it is a second authority on the one that
    already existed.

    **The twentieth is the session endpoint, and it cannot change the host.** Its `POST`
    exchanges the master Bearer credential for a derived browser session; its `DELETE`
    revokes only that browser session. Neither operation grants host-write authority.

    The document lists exactly these twenty so a twenty-first cannot appear unnoticed, and
    the assertion is an equality rather than a containment for that reason.

    There is still deliberately no `/execute`, no `/operations/{name}/execute`, no generic
    `/rollback`, no terminal, no passthrough to the privileged helper, and nothing that
    forwards a request to the Docker daemon. The recovery endpoints are scoped to one change
    and take no value: `retry` has no body at all.
    """
    paths = client.get("/openapi.json").json()["paths"]
    non_get = {
        path: sorted(methods)
        for path, methods in paths.items()
        if set(methods) - {"get"}
    }
    assert non_get == {
        "/api/v1/session": ["delete", "get", "post"],
        "/api/v1/network/observations/refresh": ["post"],
        "/api/v1/docker/containers/observations/refresh": ["post"],
        "/api/v1/systemd/observations/refresh": ["post"],
        "/api/v1/docker/containers/{object_id}/logs": ["post"],
        "/api/v1/docker/containers/{object_id}/stats": ["post"],
        "/api/v1/management-path/observations/refresh": ["post"],
        "/api/v1/network/interfaces/{object_id}/adopt": ["post"],
        "/api/v1/network/interfaces/{object_id}/release": ["post"],
        "/api/v1/network/interfaces/{object_id}/intent/revise": ["post"],
        "/api/v1/network/interfaces/{object_id}/intent/adopt-runtime": ["post"],
        "/api/v1/runs": ["get", "post"],
        "/api/v1/runs/{run_id}/cancel": ["post"],
        "/api/v1/runs/{run_id}/confirm": ["post"],
        "/api/v1/runs/{run_id}/apply": ["post"],
        "/api/v1/runs/{run_id}/guard/keep": ["post"],
        "/api/v1/runs/{run_id}/self-impact-override": ["post"],
        "/api/v1/changes/{change_id}/recovery/confirm": ["post"],
        "/api/v1/changes/{change_id}/recovery/retry": ["post"],
        "/api/v1/changes/{change_id}/recovery/resolve": ["post"],
    }


def test_the_schema_documents_nullable_link_fields(client: TestClient):
    link = client.get("/openapi.json").json()["components"]["schemas"]["Link"]
    for field in ("speed_mbps", "carrier", "duplex", "mac_address"):
        assert "anyOf" in link["properties"][field], f"{field} must be nullable in the schema"


def test_the_schema_states_the_enumerated_states(client: TestClient):
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    assert set(schemas["ManagementState"]["enum"]) == {"observe_only", "observed", "managed"}
    assert set(schemas["HealthState"]["enum"]) == {
        "healthy",
        "degraded",
        "failed",
        "inactive",
        "unknown",
    }
    assert set(schemas["Freshness"]["enum"]) == {"current", "stale", "never_observed"}
    assert set(schemas["ReconciliationState"]["enum"]) == {
        "in_sync",
        "drifted",
        "applying",
        "unknown",
    }


def test_an_invalid_interface_name_is_a_400_not_a_503(client: TestClient):
    """A caller's mistake and an unreachable agent are different conditions."""
    response = client.post(
        "/api/v1/network/observations/refresh", params={"names": ["eth0; id"]}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_params"


def test_a_name_longer_than_the_kernel_allows_is_a_400(client: TestClient):
    response = client.post(
        "/api/v1/network/observations/refresh", params={"names": ["a" * 16]}
    )
    assert response.status_code == 400


def test_an_unusable_capability_is_a_503_naming_the_capability(
    tmp_path: Path,
    fake_root: Path,
    absent_docker: Path,
):
    from localplane.agent.server import AgentServer
    from localplane.agent.service import AgentService

    service = AgentService(
        root=fake_root,
        sysfs_net=tmp_path / "absent",
        runner=FakeRunner({}),
        docker_socket=absent_docker,
    )
    server = AgentServer(tmp_path / "cap" / "agent.sock", service)
    server.serve_in_thread()
    settings = _settings(tmp_path, server.socket_path, observe=False)
    database = open_database(settings.database_path)
    try:
        with AuthenticatedTestClient(create_authenticated_app(settings, database)) as client:
            response = client.post("/api/v1/network/observations/refresh")
            assert response.status_code == 503
            body = response.json()["error"]
            assert body["code"] == "capability_unavailable"
            assert body["detail"]["capability"] == "network.observe"
    finally:
        database.close()
        server.shutdown()
        server.server_close()


def test_sweep_issues_are_typed_in_the_schema(client: TestClient):
    schemas_ = client.get("/openapi.json").json()["components"]["schemas"]
    assert set(schemas_["ProviderIssue"]["properties"]) == {"source", "code", "message", "detail"}


def test_a_degraded_sweep_reports_its_issues_over_http(
    tmp_path: Path,
    fake_root: Path,
    populated_sysfs: Path,
    absent_docker: Path,
):
    """No `ip` on the host: partial, addresses null, and the reason is in the sweep."""
    from localplane.agent.server import AgentServer
    from localplane.agent.service import AgentService

    service = AgentService(
        root=fake_root,
        sysfs_net=populated_sysfs,
        runner=FakeRunner({}),
        docker_socket=absent_docker,
    )
    server = AgentServer(tmp_path / "degraded" / "agent.sock", service)
    server.serve_in_thread()
    settings = _settings(tmp_path, server.socket_path)
    database = open_database(settings.database_path)
    try:
        with AuthenticatedTestClient(create_authenticated_app(settings, database)) as client:
            body = client.get("/api/v1/network/interfaces").json()
            assert body["last_sweep"]["status"] == "partial"
            assert {i["code"] for i in body["last_sweep"]["issues"]} == {"command_failed"}
            assert {i["source"] for i in body["last_sweep"]["issues"]} == {
                "rtnetlink.link",
                "rtnetlink.addr",
            }
            assert all(i["addresses"] is None for i in body["interfaces"])
            assert all(i["observation"]["fidelity"] == "partial" for i in body["interfaces"])
    finally:
        database.close()
        server.shutdown()
        server.server_close()


def test_the_whole_stack_runs_with_logging_configured(tmp_path: Path, running_agent: AgentServer):
    """Startup, handshake, sweep and a read, with logging actually on.

    Everything below INFO is unreachable while the root logger sits at WARNING, which is
    where a bad structured-log field hides until the first real start.
    """
    import io
    import logging as stdlib_logging

    from localplane.log import configure_logging

    stream = io.StringIO()
    configure_logging("DEBUG", stream)
    try:
        settings = _settings(tmp_path, running_agent.socket_path)
        database = open_database(settings.database_path)
        with AuthenticatedTestClient(create_authenticated_app(settings, database)) as client:
            assert client.get("/api/v1/network/interfaces").json()["count"] == 9
            assert client.post("/api/v1/network/observations/refresh").json()["status"] == "ok"
        database.close()
    finally:
        configure_logging("WARNING")
        stdlib_logging.getLogger().handlers.clear()

    lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    messages = {entry["message"] for entry in lines}
    assert "applying migration" in messages
    assert "network sweep ingested" in messages
