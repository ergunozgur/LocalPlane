"""The management half of the HTTP surface.

Same shape as ``test_api.py``: a real agent process-in-a-thread over a real socket, a real
store, and a fixture ``/sys/class/net`` that the tests rewrite in order to produce genuine
drift. Nothing constructs an observation by hand.

Two claims are asserted repeatedly and on purpose, because they are the point of
adoption: adopt and release change LocalPlane's records, and they change nothing about the
host. The second half of that is checked twice over — the response says so, and the
fixture tree is compared byte for byte before and after.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from localplane.agent.server import AgentServer
from localplane.agent.service import AgentService
from localplane.backend.app import create_app
from localplane.backend.config import Settings
from localplane.backend.db.database import open_database
from tests.conftest import FakeRunner, json_result, write_interface

API = "/api/v1"


@pytest.fixture
def sysfs(sysfs_net: Path) -> Path:
    write_interface(
        sysfs_net, "lo", ifindex=1, address="00:00:00:00:00:00", arphrd="772", flags="0x9",
        operstate="unknown", carrier="1", mtu="65536", speed=None, duplex=None,
    )
    write_interface(
        sysfs_net, "eth0", ifindex=2, address="02:00:00:00:00:10", flags="0x1003",
        operstate="down", carrier="0", mtu="1500", device="fd580000.ethernet",
        subsystem="platform",
    )
    write_interface(
        sysfs_net, "eth1", ifindex=3, address="02:00:00:00:00:12", flags="0x1003",
        operstate="up", carrier="1", mtu="1500", speed="1000", duplex="full",
        device="1-1.4:1.0", subsystem="usb",
    )
    return sysfs_net


@pytest.fixture
def runner() -> FakeRunner:
    from localplane.agent.providers.linux_network import ADDR_ARGV, LINK_ARGV

    return FakeRunner(
        {
            LINK_ARGV: json_result(
                LINK_ARGV, [{"ifindex": 2, "ifname": "eth0"}, {"ifindex": 3, "ifname": "eth1"}]
            ),
            ADDR_ARGV: json_result(
                ADDR_ARGV,
                [
                    {"ifindex": 1, "ifname": "lo", "addr_info": []},
                    {"ifindex": 2, "ifname": "eth0", "addr_info": []},
                    {"ifindex": 3, "ifname": "eth1", "addr_info": []},
                ],
            ),
        }
    )


@pytest.fixture
def client(
    tmp_path: Path, fake_root: Path, sysfs: Path, runner: FakeRunner
, absent_docker: Path) -> Iterator[TestClient]:
    service = AgentService(
        root=fake_root,
        sysfs_net=sysfs,
        runner=runner,
        docker_socket=absent_docker,
    )
    server = AgentServer(tmp_path / "run" / "agent.sock", service)
    thread = server.serve_in_thread()
    settings = Settings(
        database_path=tmp_path / "store" / "localplane.db",
        agent_socket=server.socket_path,
        agent_timeout_s=10,
        freshness_ttl_s=60,
        log_level="WARNING",
        observe_on_startup=True,
    )
    database = open_database(settings.database_path)
    with TestClient(create_app(settings, database)) as test_client:
        yield test_client
    database.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


# ------------------------------------------------------------------------------ helpers


def interfaces(client: TestClient) -> dict:
    return {i["name"]: i for i in client.get(f"{API}/network/interfaces").json()["interfaces"]}


def object_id(client: TestClient, name: str) -> str:
    return interfaces(client)[name]["object_id"]


def adopt(client: TestClient, name: str):
    return client.post(f"{API}/network/interfaces/{object_id(client, name)}/adopt")


def release(client: TestClient, name: str):
    return client.post(f"{API}/network/interfaces/{object_id(client, name)}/release")


def set_value(sysfs: Path, interface: str, field: str, value: str) -> None:
    (sysfs / interface / field).write_text(value + "\n")


def make_unreadable(sysfs: Path, interface: str, field: str) -> None:
    path = sysfs / interface / field
    path.unlink()
    path.mkdir()


def tree_snapshot(sysfs: Path) -> dict:
    return {
        entry.name: {f.name: f.read_text() for f in sorted(entry.iterdir()) if f.is_file()}
        for entry in sorted(sysfs.iterdir())
        if (entry / "ifindex").exists()
    }


# ------------------------------------------------------------------------------- adopt


def test_adopt_moves_an_observed_interface_to_managed(client: TestClient):
    response = adopt(client, "eth0")
    assert response.status_code == 200
    body = response.json()

    assert body["transition"] == "adopt"
    assert body["from_state"] == "observed"
    assert body["to_state"] == "managed"
    assert body["intent"]["version"] == 1
    assert body["intent"]["active"] is True
    assert body["reconciliation"]["state"] == "in_sync"

    eth0 = interfaces(client)["eth0"]
    assert eth0["management"] == {"state": "managed", "reason": "adopted"}
    assert eth0["reconciliation"] == "in_sync"
    assert eth0["intent"]["controlled_fields"] == ["admin_up", "mtu"]


def test_the_adopt_response_says_the_host_was_not_touched(client: TestClient):
    body = adopt(client, "eth0").json()
    assert body["host_mutated"] is False
    assert body["host_effect"] == "none"
    assert "Nothing was written to the host" in body["note"]


def test_adopting_does_not_change_a_single_value_on_the_host(client: TestClient, sysfs: Path):
    before = tree_snapshot(sysfs)
    adopt(client, "eth0")
    adopt(client, "eth1")
    assert tree_snapshot(sysfs) == before


def test_adopt_records_typed_values_for_the_controlled_fields_only(client: TestClient):
    fields = adopt(client, "eth0").json()["intent"]["controlled_fields"]
    assert fields == [
        {"field": "admin_up", "value_type": "boolean", "value": True},
        {"field": "mtu", "value_type": "integer", "value": 1500},
    ]


def test_the_intent_names_the_observation_it_was_taken_from(client: TestClient):
    eth0 = interfaces(client)["eth0"]
    captured = adopt(client, "eth0").json()["intent"]["captured_from"]
    assert captured["observation_id"] == eth0["observation"]["observation_id"]
    assert captured["sweep_id"] == eth0["observation"]["sweep_id"]
    assert captured["capability"] == "network.observe"
    assert captured["provider"] == "linux.network"


def test_adopting_a_watch_only_interface_is_a_structured_409(client: TestClient):
    response = adopt(client, "lo")
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "object_observe_only"
    assert error["detail"]["reason"] == "loopback"
    assert interfaces(client)["lo"]["management"]["state"] == "observe_only"
    assert interfaces(client)["lo"]["intent"] is None


def test_adopting_twice_is_a_structured_409(client: TestClient):
    first = adopt(client, "eth0").json()
    response = adopt(client, "eth0")
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "already_managed"
    assert error["detail"]["active_intent_id"] == first["intent"]["intent_id"]


def test_adopting_an_unknown_object_is_a_structured_404(client: TestClient):
    response = client.post(f"{API}/network/interfaces/obj_nothing/adopt")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "object_not_found"


def test_adopt_is_refused_when_a_controlled_value_cannot_be_read(
    client: TestClient, sysfs: Path
):
    make_unreadable(sysfs, "eth0", "mtu")
    client.post(f"{API}/network/observations/refresh")

    response = adopt(client, "eth0")
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "controlled_values_unverified"
    assert [f["field"] for f in error["detail"]["fields"]] == ["mtu"]
    assert "sysfs.mtu" in error["detail"]["gaps"]
    assert interfaces(client)["eth0"]["management"]["state"] == "observed"


# ------------------------------------------------------------------------------ intent


def test_the_active_intent_is_readable(client: TestClient):
    adopted = adopt(client, "eth0").json()
    body = client.get(f"{API}/network/interfaces/{object_id(client, 'eth0')}/intent").json()
    assert body["intent_id"] == adopted["intent"]["intent_id"]
    assert body["origin"] == "adopt"
    assert body["active"] is True
    assert body["schema_version"] == 1


def test_an_observed_interface_has_no_intent_to_read(client: TestClient):
    response = client.get(f"{API}/network/interfaces/{object_id(client, 'eth1')}/intent")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "no_active_intent"
    assert error["detail"]["management_state"] == "observed"


def test_intent_history_keeps_every_version_and_every_transition(
    client: TestClient, sysfs: Path
):
    first = adopt(client, "eth0").json()["intent"]["intent_id"]
    release(client, "eth0")
    set_value(sysfs, "eth0", "mtu", "9000")
    client.post(f"{API}/network/observations/refresh")
    second = adopt(client, "eth0").json()["intent"]["intent_id"]

    body = client.get(
        f"{API}/network/interfaces/{object_id(client, 'eth0')}/intent/history"
    ).json()
    assert body["count"] == 2
    assert body["active_intent_id"] == second
    assert [i["version"] for i in body["intents"]] == [2, 1]
    assert [i["active"] for i in body["intents"]] == [True, False]
    assert body["intents"][0]["supersedes"] == first
    assert [t["transition"] for t in body["transitions"]] == ["adopt", "release", "adopt"]
    assert all(t["host_effect"] == "none" for t in body["transitions"])


# ---------------------------------------------------------------------- reconciliation


def test_an_observed_interface_reports_no_reconciliation_with_a_200(client: TestClient):
    body = client.get(
        f"{API}/network/interfaces/{object_id(client, 'eth1')}/reconciliation"
    ).json()
    assert body["management"]["state"] == "observed"
    assert body["reconciliation"] is None
    assert body["intent"] is None


def test_a_controlled_value_that_moves_is_reported_as_drift(client: TestClient, sysfs: Path):
    adopt(client, "eth0")
    set_value(sysfs, "eth0", "mtu", "9000")
    client.post(f"{API}/network/observations/refresh")

    assert interfaces(client)["eth0"]["reconciliation"] == "drifted"
    body = client.get(
        f"{API}/network/interfaces/{object_id(client, 'eth0')}/reconciliation"
    ).json()
    assert body["reconciliation"]["state"] == "drifted"
    assert body["reconciliation"]["reason"] == "controlled_field_differs"
    mtu = [f for f in body["reconciliation"]["fields"] if f["field"] == "mtu"][0]
    assert mtu == {
        "field": "mtu",
        "value_type": "integer",
        "intended": 1500,
        "observed": 9000,
        "comparison": "differs",
        "reason": "observed_value_differs",
    }
    assert body["reconciliation"]["observation"]["observation_id"]


def test_an_unreadable_controlled_value_is_reported_as_unknown(
    client: TestClient, sysfs: Path
):
    adopt(client, "eth0")
    make_unreadable(sysfs, "eth0", "mtu")
    client.post(f"{API}/network/observations/refresh")

    assert interfaces(client)["eth0"]["reconciliation"] == "unknown"
    body = client.get(
        f"{API}/network/interfaces/{object_id(client, 'eth0')}/reconciliation"
    ).json()
    assert body["reconciliation"]["reason"] == "controlled_field_not_observable"
    mtu = [f for f in body["reconciliation"]["fields"] if f["field"] == "mtu"][0]
    assert mtu["observed"] is None
    assert mtu["comparison"] == "unknown"
    assert client.get(f"{API}/findings").json()["count"] == 0


def test_health_and_reconciliation_do_not_move_together(client: TestClient, sysfs: Path):
    adopt(client, "eth0")
    adopt(client, "eth1")
    set_value(sysfs, "eth1", "mtu", "9000")
    client.post(f"{API}/network/observations/refresh")

    found = interfaces(client)
    assert found["eth1"]["health"]["state"] == "healthy"
    assert found["eth1"]["reconciliation"] == "drifted"
    assert found["eth0"]["health"]["state"] == "inactive"
    assert found["eth0"]["reconciliation"] == "in_sync"


# ---------------------------------------------------------------------------- findings


def test_drift_produces_one_finding_with_evidence(client: TestClient, sysfs: Path):
    adopt(client, "eth0")
    set_value(sysfs, "eth0", "mtu", "9000")
    client.post(f"{API}/network/observations/refresh")

    body = client.get(f"{API}/findings").json()
    assert body["count"] == 1
    finding = body["findings"][0]
    assert finding["finding_type"] == "network.interface.drift"
    assert finding["subject"] == "mtu"
    assert finding["status"] == "open"
    assert finding["object_name"] == "eth0"
    assert finding["evidence"]["intended"] == {"value_type": "integer", "value": 1500}
    assert finding["evidence"]["observed"] == {"value_type": "integer", "value": 9000}
    assert finding["evidence"]["comparison"] == "differs"
    assert "1500" in finding["summary"] and "9000" in finding["summary"]
    assert finding["resolved_at"] is None


def test_re_observing_the_same_drift_does_not_add_findings(client: TestClient, sysfs: Path):
    adopt(client, "eth0")
    set_value(sysfs, "eth0", "mtu", "9000")
    for _ in range(5):
        client.post(f"{API}/network/observations/refresh")

    body = client.get(f"{API}/findings?status=all").json()
    assert body["count"] == 1


def test_a_resolved_finding_is_kept_and_filterable(client: TestClient, sysfs: Path):
    adopt(client, "eth0")
    set_value(sysfs, "eth0", "mtu", "9000")
    client.post(f"{API}/network/observations/refresh")
    set_value(sysfs, "eth0", "mtu", "1500")
    client.post(f"{API}/network/observations/refresh")

    assert client.get(f"{API}/findings").json()["count"] == 0
    resolved = client.get(f"{API}/findings?status=resolved").json()
    assert resolved["count"] == 1
    finding = resolved["findings"][0]
    assert finding["resolution"] == "observed_matches_intent"
    assert finding["resolved_by_observation_id"]
    assert finding["evidence"]["observed"]["value"] == 9000
    assert client.get(f"{API}/findings?status=all").json()["count"] == 1


def test_a_single_finding_is_addressable(client: TestClient, sysfs: Path):
    adopt(client, "eth0")
    set_value(sysfs, "eth0", "mtu", "9000")
    client.post(f"{API}/network/observations/refresh")
    listed = client.get(f"{API}/findings").json()["findings"][0]

    body = client.get(f"{API}/findings/{listed['finding_id']}").json()
    assert body["finding_id"] == listed["finding_id"]
    assert body["finding_key"] == listed["finding_key"]


def test_an_unknown_finding_is_a_structured_404(client: TestClient):
    response = client.get(f"{API}/findings/fnd_nothing")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "finding_not_found"


def test_findings_can_be_scoped_to_one_object(client: TestClient, sysfs: Path):
    adopt(client, "eth0")
    adopt(client, "eth1")
    set_value(sysfs, "eth0", "mtu", "9000")
    set_value(sysfs, "eth1", "flags", "0x1002")
    client.post(f"{API}/network/observations/refresh")

    assert client.get(f"{API}/findings").json()["count"] == 2
    scoped = client.get(f"{API}/findings", params={"object_id": object_id(client, "eth1")}).json()
    assert scoped["count"] == 1
    assert scoped["findings"][0]["subject"] == "admin_up"
    assert scoped["findings"][0]["object_name"] == "eth1"


def test_an_invalid_status_filter_is_refused(client: TestClient):
    assert client.get(f"{API}/findings?status=maybe").status_code == 422


# ----------------------------------------------------------------------------- release


def test_release_returns_the_interface_to_observed(client: TestClient):
    adopt(client, "eth0")
    response = release(client, "eth0")
    assert response.status_code == 200
    body = response.json()

    assert body["transition"] == "release"
    assert body["from_state"] == "managed"
    assert body["to_state"] == "observed"
    assert body["reconciliation"] is None
    assert body["host_mutated"] is False
    assert "not a rollback" in body["note"]

    eth0 = interfaces(client)["eth0"]
    assert eth0["management"] == {"state": "observed", "reason": "released"}
    assert eth0["reconciliation"] is None
    assert eth0["intent"] is None


def test_release_closes_the_drift_it_can_no_longer_claim(client: TestClient, sysfs: Path):
    adopt(client, "eth0")
    set_value(sysfs, "eth0", "mtu", "9000")
    client.post(f"{API}/network/observations/refresh")
    open_finding = client.get(f"{API}/findings").json()["findings"][0]

    body = release(client, "eth0").json()
    assert body["findings_resolved"] == [open_finding["finding_id"]]

    assert client.get(f"{API}/findings").json()["count"] == 0
    resolved = client.get(f"{API}/findings?status=resolved").json()["findings"][0]
    assert resolved["finding_id"] == open_finding["finding_id"]
    assert resolved["resolution"] == "intent_released"
    assert resolved["resolved_by_observation_id"] is None


def test_release_leaves_the_host_and_the_observation_exactly_as_they_were(
    client: TestClient, sysfs: Path
):
    adopt(client, "eth0")
    set_value(sysfs, "eth0", "mtu", "9000")
    client.post(f"{API}/network/observations/refresh")

    before_tree = tree_snapshot(sysfs)
    before = interfaces(client)["eth0"]

    release(client, "eth0")

    after = interfaces(client)["eth0"]
    assert tree_snapshot(sysfs) == before_tree
    assert after["link"] == before["link"]
    assert after["link"]["mtu"] == 9000
    assert after["health"] == before["health"]
    assert after["observation"]["observation_id"] == before["observation"]["observation_id"]


def test_releasing_something_that_is_not_managed_is_a_structured_409(client: TestClient):
    response = release(client, "eth1")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_managed"


def test_adopt_and_release_leave_the_fixture_host_byte_identical(
    client: TestClient, sysfs: Path
):
    before = tree_snapshot(sysfs)
    adopt(client, "eth0")
    client.post(f"{API}/network/observations/refresh")
    release(client, "eth0")
    client.post(f"{API}/network/observations/refresh")
    assert tree_snapshot(sysfs) == before


# ------------------------------------------------------------------------------ purity


def test_reads_do_not_change_management_or_findings(client: TestClient, sysfs: Path):
    adopt(client, "eth0")
    set_value(sysfs, "eth0", "mtu", "9000")
    client.post(f"{API}/network/observations/refresh")

    sweeps = client.get(f"{API}/observations/sweeps").json()["count"]
    finding = client.get(f"{API}/findings").json()["findings"][0]

    for _ in range(4):
        client.get(f"{API}/network/interfaces")
        client.get(f"{API}/network/interfaces/{object_id(client, 'eth0')}/reconciliation")
        client.get(f"{API}/findings")

    assert client.get(f"{API}/observations/sweeps").json()["count"] == sweeps
    after = client.get(f"{API}/findings").json()["findings"][0]
    assert after["finding_id"] == finding["finding_id"]
    assert after["updated_at"] == finding["updated_at"]
    assert after["last_seen_at"] == finding["last_seen_at"]


def test_adopting_and_releasing_gained_no_ability_to_mutate(client: TestClient):
    """Adopting, releasing and revising still write nothing to the host.

    None of the mutating mechanisms in the product is reachable from these endpoints: they
    move LocalPlane's own records. The systemd lifecycle capability describes a passively
    detected future mechanism; Part A adds no mutating protocol method or executor for it.
    """
    body = client.get(f"{API}/agent/capabilities").json()
    mutating = {c["capability"] for c in body["capabilities"] if c["mutating"]}
    assert mutating == {
        "network.interface.set_mtu",
        "network.interface.mtu_guard",
        "docker.container.lifecycle",
        "systemd.service.lifecycle",
    }


# ------------------------------------------------------------------------------ schema


def test_the_schema_documents_the_management_surface(client: TestClient):
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    for name in (
        "TransitionResult",
        "Intent",
        "IntentField",
        "IntentHistory",
        "Reconciliation",
        "FieldComparison",
        "Finding",
        "FindingEvidence",
        "FindingList",
        "ManagementTransition",
    ):
        assert name in schemas, f"{name} must be in the published schema"


def test_the_schema_states_that_a_transition_does_not_touch_the_host(client: TestClient):
    result = client.get("/openapi.json").json()["components"]["schemas"]["TransitionResult"]
    assert "false" in result["properties"]["host_mutated"]["description"]
    assert "none" in result["properties"]["host_effect"]["description"]


def test_the_schema_keeps_reconciliation_nullable_and_enumerated(client: TestClient):
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    assert set(schemas["ReconciliationState"]["enum"]) == {
        "in_sync",
        "drifted",
        "applying",
        "unknown",
    }
    assert "anyOf" in schemas["NetworkInterface"]["properties"]["reconciliation"]
    assert "anyOf" in schemas["NetworkInterface"]["properties"]["intent"]
    assert "anyOf" in schemas["FieldComparison"]["properties"]["observed"]
