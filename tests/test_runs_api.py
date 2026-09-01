"""The Change Engine's HTTP surface: planning, reading and cancelling Runs.

Same shape as the other API suites — a real agent process-in-a-thread over a real socket, a
real store, and a fixture ``/sys/class/net`` the tests rewrite in order to produce genuine
drift. Nothing constructs an observation, and nothing constructs a preview.

What is asserted repeatedly, because it is the point of this surface:

* **there is no way to execute anything through this API** — no apply, no execute, no
  confirm, and nothing in a request body that could become a command, an argv, a provider
  method or a desired value;
* **a Run is not a Change** — ``host_mutated`` and ``change_created`` are false on every
  response and the fixture tree is compared byte for byte before and after;
* **what is not known is reported as unknown**, most of all whether the interface being
  planned against carries the path the operator is reaching LocalPlane over.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from localplane.agent.server import AgentServer
from localplane.agent.service import AgentService
from tests.conftest import AuthenticatedTestClient, create_authenticated_app
from localplane.backend.config import Settings
from localplane.backend.db.database import open_database
from tests.conftest import FakeRunner, json_result, write_interface

API = "/api/v1"
RECONCILE_MTU = "network.interface.reconcile_mtu"


@pytest.fixture
def sysfs(sysfs_net: Path) -> Path:
    write_interface(
        sysfs_net, "lo", ifindex=1, address="00:00:00:00:00:00", arphrd="772", flags="0x9",
        operstate="unknown", carrier="1", mtu="65536", speed=None, duplex=None,
    )
    write_interface(
        sysfs_net, "eth0", ifindex=2, address="02:00:00:00:00:10", flags="0x1003",
        operstate="up", carrier="1", mtu="1500", device="fd580000.ethernet",
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
    tmp_path: Path, fake_root: Path, sysfs: Path, runner: FakeRunner, absent_docker: Path
) -> Iterator[TestClient]:
    service = AgentService(
        root=fake_root,
        sysfs_net=sysfs,
        runner=runner,
        docker_socket=absent_docker,
        # No privileged helper on this fixture host, deliberately: this file is about the
        # planning half, where a plan is honestly blocked on the capability being absent.
        helper_socket=tmp_path / "run" / "helper-absent.sock",
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
    with AuthenticatedTestClient(create_authenticated_app(settings, database)) as test_client:
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


def adopt(client: TestClient, name: str = "eth0"):
    return client.post(f"{API}/network/interfaces/{object_id(client, name)}/adopt")


def drift(client: TestClient, sysfs: Path, name: str = "eth0", mtu: str = "1400") -> None:
    (sysfs / name / "mtu").write_text(mtu + "\n")
    client.post(f"{API}/network/observations/refresh")


def create_run(client: TestClient, name: str = "eth0", **overrides: Any):
    body = {"operation": {"type": RECONCILE_MTU, "object_id": object_id(client, name)}}
    body["operation"].update(overrides)
    return client.post(f"{API}/runs", json=body)


def planned(client: TestClient, sysfs: Path, name: str = "eth0") -> dict:
    adopt(client, name)
    drift(client, sysfs, name)
    response = create_run(client, name)
    assert response.status_code == 201, response.json()
    return response.json()


def _stable(interface: dict) -> dict:
    """An interface response with the one field that moves on its own removed.

    ``age_seconds`` is derived from the clock at read time and is *meant* to change without
    anything happening. Everything else in the response must not.
    """
    observation = dict(interface["observation"])
    observation.pop("age_seconds")
    return {**interface, "observation": observation}


def tree_snapshot(sysfs: Path) -> dict:
    return {
        entry.name: {f.name: f.read_text() for f in sorted(entry.iterdir()) if f.is_file()}
        for entry in sorted(sysfs.iterdir())
        if (entry / "ifindex").exists()
    }


# ------------------------------------------------------------------------------ planning


def test_planning_a_run_publishes_a_preview(client: TestClient, sysfs: Path):
    body = planned(client, sysfs)
    assert body["state"] == "preview"
    assert body["operation"] == RECONCILE_MTU
    assert body["object_name"] == "eth0"
    assert body["cancelled_at"] is None
    assert body["preview"]["preview_digest"].startswith("sha256:")
    assert body["preview"]["validity"]["state"] == "current"


def test_the_response_says_no_host_was_touched_and_no_change_was_created(
    client: TestClient, sysfs: Path
):
    body = planned(client, sysfs)
    assert body["host_mutated"] is False
    assert body["host_effect"] == "none"
    assert body["change_created"] is False
    assert "not a Change" in body["note"]


def test_the_preview_answers_what_why_how_evidence_risk_verify_and_recover(
    client: TestClient, sysfs: Path
):
    preview = planned(client, sysfs)["preview"]

    assert preview["what"] == {
        "object_id": preview["what"]["object_id"],
        "object_name": "eth0",
        # The discriminator a reader branches on. The action half of the document is null
        # on a field change, and asserting that here is what stops the two halves quietly
        # both acquiring values.
        "kind": "field",
        "field": "mtu",
        "value_type": "integer",
        "current": 1400,
        "desired": 1500,
        "action": None,
        "observed_state": None,
        "expected_state": None,
        "expected_after": 1500,
    }
    assert preview["why"]["intent_version"] == 1
    assert preview["why"]["reason"] == "controlled_field_differs"
    assert preview["why"]["drift_finding_id"] is not None

    # Two different questions. The build has execution code; this host has no privileged
    # helper for the agent to reach, so the plan is blocked on the thing actually missing.
    assert preview["how"]["availability"] == "available"
    assert preview["how"]["eligibility"] == "blocked"
    assert preview["how"]["blockers"] == [
        "required_capability_undeclared",
        # This client is calling over loopback, which proves nothing about where the
        # operator is connected from — so the target's relation to the management path is
        # unresolved and executing stays blocked on that too.
        "management_path_unproven",
    ]
    assert preview["how"]["provider"] == "linux.link"
    assert preview["how"]["required_capability"] == "network.interface.set_mtu"
    assert preview["how"]["capability_declared_by_agent"] is False

    assert preview["evidence"]["observation"]["observation_id"]
    assert preview["evidence"]["ownership"]["evidence_gaps"] == []

    assert preview["risk"]["tier"] == "medium"
    assert {f["code"] for f in preview["risk"]["factors"]} == {
        "operation_base_risk",
        "management_path_unproven",
    }

    assert preview["verification"]["executed"] is False
    assert preview["verification"]["field"] == "mtu"
    assert preview["verification"]["expect"] == 1500

    assert preview["recovery"]["armed"] is False
    assert preview["recovery"]["rollback_possible"] is True
    assert preview["recovery"]["restores_field"] == "mtu"
    assert preview["recovery"]["restores_value"] == 1400
    assert preview["recovery"]["guarantee"] == "none"


def test_the_preview_says_the_management_path_is_unknown(client: TestClient, sysfs: Path):
    """Never inferred. Not from the name, not from the shape, not from who is asking.

    This client's transport carries no usable address at all — the test server's peer is a
    name, not an address — so nothing about the management path can be established, and the
    reason says which end failed rather than collapsing into a generic "could not tell".
    """
    protection = planned(client, sysfs)["preview"]["protection"]
    assert protection["status"] == "unknown"
    assert protection["management_path"] == "unknown"
    assert protection["reason"] == "transport_peer_unparseable"
    assert protection["reasons"] == []
    assert protection["unresolved"] == ["management_path"]
    assert protection["evidence_id"] is None
    assert sorted(protection["missing_evidence"]) == ["route.observe", "session.peer"]
    # And the answer does not depend on which interface it is, or what it is called.
    other = planned(client, sysfs, "eth1")["preview"]
    assert other["protection"] == protection
    assert "management_path_unproven" in other["how"]["blockers"]


def test_confirmation_is_described_and_still_never_issued(client: TestClient, sysfs: Path):
    """A confirmation became satisfiable. It did not become something a caller holds.

    ``token_issued`` is false on every path and the store CHECKs the column to zero: a
    confirmation here is a durable row naming one Run and one published plan, consumed once
    by an apply of that Run. Nothing comes back that could be presented anywhere else.
    """
    confirmation = planned(client, sysfs)["preview"]["confirmation"]
    assert confirmation["required"] is True
    assert confirmation["method"] == "typed"
    assert confirmation["source"] == "policy"
    assert confirmation["token_issued"] is False
    # Nobody has confirmed this plan.
    assert confirmation["satisfied"] is False
    # But one could: execution exists now, which is a different claim from being issued.
    assert confirmation["satisfiable"] is True
    assert confirmation["unsatisfiable_reason"] is None


def test_a_preview_exists_even_though_execution_is_blocked(client: TestClient, sysfs: Path):
    """The useful case: describing what would change is not the same as being allowed to."""
    body = planned(client, sysfs)
    assert body["preview"]["what"]["current"] != body["preview"]["what"]["desired"]
    assert body["preview"]["how"]["eligibility"] == "blocked"
    assert "required_capability_undeclared" in body["preview"]["how"]["blockers"]


# -------------------------------------------------------------------------- the refusals


def test_planning_against_an_unmanaged_object_is_refused(client: TestClient):
    response = create_run(client, "eth0")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_managed"


def test_planning_against_an_observe_only_object_is_refused(client: TestClient):
    response = create_run(client, "lo")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "object_observe_only"


def test_a_runtime_that_already_agrees_is_refused_with_its_own_code(client: TestClient):
    adopt(client, "eth0")
    response = create_run(client, "eth0")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "already_reconciled"
    assert response.json()["error"]["detail"]["value"] == 1500
    assert client.get(f"{API}/runs").json()["count"] == 0


def test_an_object_that_does_not_exist_is_a_404(client: TestClient):
    response = client.post(
        f"{API}/runs", json={"operation": {"type": RECONCILE_MTU, "object_id": "obj_nope"}}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "object_not_found"


def test_a_refused_plan_leaves_no_run_behind(client: TestClient):
    create_run(client, "eth0")
    create_run(client, "lo")
    assert client.get(f"{API}/runs?state=all").json()["count"] == 0


# ------------------------------------------------------------- the closed typed contract


def test_an_operation_type_localplane_does_not_implement_is_refused(client: TestClient):
    adopt(client, "eth0")
    target = object_id(client, "eth0")
    for name in (
        "network.interface.set_mtu",
        "network.interface.reconcile",
        "shell",
        "exec",
        "custom",
        "",
    ):
        response = client.post(
            f"{API}/runs", json={"operation": {"type": name, "object_id": target}}
        )
        assert response.status_code == 422, name


def test_no_command_argv_provider_or_method_can_enter_the_api(client: TestClient, sysfs: Path):
    """A hard invariant. There is no field for any of it, and extras are refused.

    Not ignored — refused. A body that carried a command and got a 201 back would leave the
    caller entitled to believe LocalPlane had accepted it.
    """
    adopt(client, "eth0")
    drift(client, sysfs)
    target = object_id(client, "eth0")
    for extra in (
        {"command": "ip link set eth0 mtu 1500"},
        {"argv": ["ip", "link", "set", "eth0", "mtu", "1500"]},
        {"shell": True},
        {"executable": "/sbin/ip"},
        {"provider": "networkmanager"},
        {"method": "device.modify"},
        {"patch": {"mtu": 1500}},
        {"script": "#!/bin/sh"},
    ):
        response = client.post(
            f"{API}/runs",
            json={"operation": {"type": RECONCILE_MTU, "object_id": target, **extra}},
        )
        assert response.status_code == 422, extra
    assert client.get(f"{API}/runs?state=all").json()["count"] == 0


def test_a_desired_value_cannot_be_supplied_through_a_run(client: TestClient, sysfs: Path):
    """The target comes from the retained intent. A Run is not a second way to move it."""
    adopt(client, "eth0")
    drift(client, sysfs)
    target = object_id(client, "eth0")
    for extra in ({"desired": 9000}, {"mtu": 9000}, {"value": 9000}, {"fields": {"mtu": 9000}}):
        response = client.post(
            f"{API}/runs",
            json={"operation": {"type": RECONCILE_MTU, "object_id": target, **extra}},
        )
        assert response.status_code == 422, extra

    # Moving it goes through the endpoint that exists for it, and the plan follows.
    active = client.get(f"{API}/network/interfaces/{target}/intent").json()
    client.post(
        f"{API}/network/interfaces/{target}/intent/revise",
        json={"expected_intent_id": active["intent_id"], "fields": {"mtu": 9000}},
    )
    body = create_run(client, "eth0").json()
    assert body["preview"]["what"]["desired"] == 9000


def test_there_is_still_no_generic_way_to_execute_anything(client: TestClient, sysfs: Path):
    """Fail closed. `apply` exists; the shortcuts around it do not.

    Executing goes through one endpoint, on one Run, against a plan that was published,
    confirmed and armed. There is no generic executor, no separate arm, verify or rollback
    entry point an operator or a script could reach for, and no passthrough to the
    privileged helper. Each of these is a 404 because it does not exist.
    """
    body = planned(client, sysfs)
    run_id = body["run_id"]
    for path in (
        f"{API}/runs/{run_id}/execute",
        f"{API}/runs/{run_id}/arm",
        f"{API}/runs/{run_id}/verify",
        f"{API}/runs/{run_id}/rollback",
        f"{API}/runs/{run_id}/confirm-and-run",
        f"{API}/execute",
        f"{API}/operations/network.interface.reconcile_mtu/execute",
        f"{API}/helper",
        f"{API}/terminal",
        f"{API}/network/interfaces/eth0/mtu",
    ):
        assert client.post(path, json={}).status_code == 404, path
        assert client.get(path).status_code == 404, path
    # And the two that do exist are not reachable by GET.
    for path in (f"{API}/runs/{run_id}/apply", f"{API}/runs/{run_id}/confirm"):
        assert client.get(path).status_code == 405, path


def test_a_run_cannot_be_edited_through_the_api(client: TestClient, sysfs: Path):
    run_id = planned(client, sysfs)["run_id"]
    for method in (client.put, client.patch, client.delete):
        assert method(f"{API}/runs/{run_id}").status_code in (404, 405)


# ------------------------------------------------------------------------------- reading


def test_a_run_can_be_read_back_whole(client: TestClient, sysfs: Path):
    created = planned(client, sysfs)
    fetched = client.get(f"{API}/runs/{created['run_id']}").json()
    assert fetched["preview"]["preview_digest"] == created["preview"]["preview_digest"]
    assert fetched["preview"]["what"] == created["preview"]["what"]
    assert fetched["state"] == "preview"


def test_the_preview_is_addressable_on_its_own(client: TestClient, sysfs: Path):
    created = planned(client, sysfs)
    preview = client.get(f"{API}/runs/{created['run_id']}/preview").json()
    assert preview["preview_id"] == created["preview"]["preview_id"]
    assert preview["preview_digest"] == created["preview"]["preview_digest"]
    assert preview["what"] == created["preview"]["what"]


def test_runs_are_listed_newest_first_and_can_be_filtered(client: TestClient, sysfs: Path):
    first = planned(client, sysfs)
    (sysfs / "eth0" / "mtu").write_text("1300\n")
    client.post(f"{API}/network/observations/refresh")
    second = create_run(client, "eth0").json()

    listed = client.get(f"{API}/runs").json()
    assert listed["count"] == 2
    assert [r["run_id"] for r in listed["runs"]] == [second["run_id"], first["run_id"]]
    assert listed["runs"][0]["preview"]["current"] == 1300
    assert listed["runs"][0]["change_created"] is False

    client.post(f"{API}/runs/{first['run_id']}/cancel")
    assert [r["run_id"] for r in client.get(f"{API}/runs?state=cancelled").json()["runs"]] == [
        first["run_id"]
    ]
    assert [r["run_id"] for r in client.get(f"{API}/runs?state=preview").json()["runs"]] == [
        second["run_id"]
    ]


def test_an_unknown_run_is_a_404_with_a_code(client: TestClient):
    response = client.get(f"{API}/runs/run_nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "run_not_found"


# ----------------------------------------------------------------------------- staleness


def test_a_plan_goes_stale_when_the_value_it_planned_from_moves(client: TestClient, sysfs: Path):
    created = planned(client, sysfs)
    assert created["preview"]["validity"]["state"] == "current"

    (sysfs / "eth0" / "mtu").write_text("1300\n")
    client.post(f"{API}/network/observations/refresh")

    fetched = client.get(f"{API}/runs/{created['run_id']}").json()
    validity = fetched["preview"]["validity"]
    assert validity["state"] == "stale"
    assert [r["code"] for r in validity["reasons"]] == ["planned_values_changed"]
    # And the plan itself is untouched: what was published is what is still published.
    assert fetched["preview"]["what"] == created["preview"]["what"]
    assert fetched["preview"]["preview_digest"] == created["preview"]["preview_digest"]


def test_a_plan_goes_stale_when_the_intent_it_targeted_is_revised(
    client: TestClient, sysfs: Path
):
    created = planned(client, sysfs)
    target = created["object_id"]
    active = client.get(f"{API}/network/interfaces/{target}/intent").json()
    client.post(
        f"{API}/network/interfaces/{target}/intent/revise",
        json={"expected_intent_id": active["intent_id"], "fields": {"mtu": 9000}},
    )
    validity = client.get(f"{API}/runs/{created['run_id']}").json()["preview"]["validity"]
    assert validity["state"] == "stale"
    assert set(r["code"] for r in validity["reasons"]) == {
        "planned_values_changed",
        "intent_replaced",
    }


def test_a_plan_goes_stale_when_the_object_is_released(client: TestClient, sysfs: Path):
    created = planned(client, sysfs)
    client.post(f"{API}/network/interfaces/{created['object_id']}/release")
    validity = client.get(f"{API}/runs/{created['run_id']}").json()["preview"]["validity"]
    assert validity["state"] == "stale"
    assert [r["code"] for r in validity["reasons"]] == ["not_managed"]


def test_a_refresh_that_changes_nothing_leaves_the_plan_current(
    client: TestClient, sysfs: Path
):
    created = planned(client, sysfs)
    for _ in range(3):
        client.post(f"{API}/network/observations/refresh")
    validity = client.get(f"{API}/runs/{created['run_id']}").json()["preview"]["validity"]
    assert validity["state"] == "current"


# -------------------------------------------------------------------------- cancellation


def test_cancelling_ends_the_run_and_creates_no_change(client: TestClient, sysfs: Path):
    created = planned(client, sysfs)
    response = client.post(f"{API}/runs/{created['run_id']}/cancel")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "cancelled"
    assert body["cancelled_at"] is not None
    assert body["change_created"] is False
    assert body["host_mutated"] is False
    assert "No Change was created" in body["note"]


def test_a_cancelled_run_keeps_its_preview_and_stays_readable(client: TestClient, sysfs: Path):
    created = planned(client, sysfs)
    client.post(f"{API}/runs/{created['run_id']}/cancel")
    preview = client.get(f"{API}/runs/{created['run_id']}/preview").json()
    assert preview == {
        **created["preview"],
        "validity": preview["validity"],
    }


def test_cancelling_changes_nothing_about_the_object_or_its_claims(
    client: TestClient, sysfs: Path
):
    created = planned(client, sysfs)
    target = created["object_id"]
    before_interface = _stable(client.get(f"{API}/network/interfaces/{target}").json())
    before_intent = client.get(f"{API}/network/interfaces/{target}/intent").json()
    before_findings = client.get(f"{API}/findings?status=all").json()
    before_tree = tree_snapshot(sysfs)

    client.post(f"{API}/runs/{created['run_id']}/cancel")

    assert _stable(client.get(f"{API}/network/interfaces/{target}").json()) == before_interface
    assert client.get(f"{API}/network/interfaces/{target}/intent").json() == before_intent
    assert client.get(f"{API}/findings?status=all").json() == before_findings
    assert tree_snapshot(sysfs) == before_tree


def test_a_cancelled_run_cannot_be_cancelled_twice(client: TestClient, sysfs: Path):
    created = planned(client, sysfs)
    client.post(f"{API}/runs/{created['run_id']}/cancel")
    response = client.post(f"{API}/runs/{created['run_id']}/cancel")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "run_not_cancellable"


def test_cancelling_a_run_that_does_not_exist_is_a_404(client: TestClient):
    response = client.post(f"{API}/runs/run_nope/cancel")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "run_not_found"


# --------------------------------------------------------------------------------- purity


def test_reads_do_not_plan_observe_or_write_anything(client: TestClient, sysfs: Path):
    created = planned(client, sysfs)
    before_tree = tree_snapshot(sysfs)
    before_sweeps = client.get(f"{API}/observations/sweeps").json()["count"]
    before_run = client.get(f"{API}/runs/{created['run_id']}").json()
    before_findings = client.get(f"{API}/findings?status=all").json()

    for _ in range(3):
        for path in (
            "/runs",
            f"/runs/{created['run_id']}",
            f"/runs/{created['run_id']}/preview",
            "/network/interfaces",
            "/findings?status=all",
        ):
            assert client.get(f"{API}{path}").status_code == 200

    after_run = client.get(f"{API}/runs/{created['run_id']}").json()
    assert {k: v for k, v in after_run.items() if k != "preview"} == {
        k: v for k, v in before_run.items() if k != "preview"
    }
    assert after_run["preview"]["preview_digest"] == before_run["preview"]["preview_digest"]
    assert client.get(f"{API}/observations/sweeps").json()["count"] == before_sweeps
    assert client.get(f"{API}/findings?status=all").json() == before_findings
    assert client.get(f"{API}/runs").json()["count"] == 1
    assert tree_snapshot(sysfs) == before_tree


def test_planning_does_not_resolve_the_drift_it_describes(client: TestClient, sysfs: Path):
    adopt(client, "eth0")
    drift(client, sysfs)
    before = client.get(f"{API}/findings").json()
    assert before["count"] == 1

    create_run(client, "eth0")

    after = client.get(f"{API}/findings").json()
    assert after == before
    target = object_id(client, "eth0")
    assert (
        client.get(f"{API}/network/interfaces/{target}/reconciliation").json()[
            "reconciliation"
        ]["state"]
        == "drifted"
    )


def test_the_whole_run_surface_leaves_the_fixture_host_byte_identical(
    client: TestClient, sysfs: Path
):
    # The drift is produced first, because writing it is a change *to the fixture*, made
    # by the test. Everything after this line is LocalPlane, and none of it may move a byte.
    adopt(client, "eth0")
    drift(client, sysfs)
    before = tree_snapshot(sysfs)

    first = create_run(client, "eth0").json()
    second = create_run(client, "eth0").json()
    client.get(f"{API}/runs")
    client.get(f"{API}/runs/{first['run_id']}")
    client.get(f"{API}/runs/{first['run_id']}/preview")
    client.post(f"{API}/runs/{second['run_id']}/cancel")
    client.post(f"{API}/runs/{first['run_id']}/cancel")
    assert tree_snapshot(sysfs) == before


def test_only_the_declared_agent_capabilities_describe_mutating_mechanisms(
    client: TestClient, sysfs: Path
):
    """The mutating capability vocabulary is exact even while Part A cannot dispatch.

    It is declared — the agent has the method — and probed ``unavailable``, because there is
    no privileged helper to reach. Declared and available are different facts and the
    preview reports the second, which is why every plan in this file is blocked.
    """
    planned(client, sysfs)
    body = client.get(f"{API}/agent/capabilities").json()
    assert body["capabilities"]
    by_name = {c["capability"]: c for c in body["capabilities"]}
    assert {n for n, c in by_name.items() if c["mutating"]} == {
        "network.interface.set_mtu",
        "network.interface.mtu_guard",
        "docker.container.lifecycle",
        "systemd.service.lifecycle",
    }
    assert by_name["network.interface.set_mtu"]["status"] == "unavailable"


# ---------------------------------------------------------------------------- the schema


def test_the_schema_documents_the_run_surface(client: TestClient):
    paths = client.get("/openapi.json").json()["paths"]
    assert set(paths["/api/v1/runs"]) == {"get", "post"}
    assert set(paths["/api/v1/runs/{run_id}"]) == {"get"}
    assert set(paths["/api/v1/runs/{run_id}/preview"]) == {"get"}
    assert set(paths["/api/v1/runs/{run_id}/cancel"]) == {"post"}
    assert set(paths["/api/v1/runs/{run_id}/confirm"]) == {"post"}
    assert set(paths["/api/v1/runs/{run_id}/apply"]) == {"post"}
    assert set(paths["/api/v1/changes"]) == {"get"}
    assert set(paths["/api/v1/changes/{change_id}"]) == {"get"}
    for absent in (
        "/api/v1/runs/{run_id}/execute",
        "/api/v1/runs/{run_id}/arm",
        "/api/v1/runs/{run_id}/rollback",
        "/api/v1/execute",
        "/api/v1/changes/{change_id}/rollback",
    ):
        assert absent not in paths


def test_apply_accepts_no_request_body_at_all(client: TestClient):
    """The strongest form of "a caller cannot supply a value": there is nothing to supply it to.

    The operation is not validated free of an MTU, an interface or a command — it has no
    body parameter, so the OpenAPI document carries no request schema for it and there is
    nothing on the path a value could arrive through.
    """
    spec = client.get("/openapi.json").json()
    apply_spec = spec["paths"]["/api/v1/runs/{run_id}/apply"]["post"]
    assert "requestBody" not in apply_spec
    assert [p["name"] for p in apply_spec.get("parameters", [])] == ["run_id"]
    # Confirming does take a body, and it names a preview, an acknowledgement and — only
    # where the plan's method is `typed` — the object it changes, written out. There is
    # still no value, no field, no verb and no command among them.
    confirm = spec["components"]["schemas"]["ConfirmRunRequest"]
    assert set(confirm["properties"]) == {
        "preview_id",
        "acknowledge",
        "acknowledge_object",
        "expected_preview_digest",
    }
    assert confirm.get("additionalProperties") is False


def test_the_schema_states_the_closed_operation_vocabulary(client: TestClient):
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    operation = schemas["ReconcileMtuOperation"]
    assert operation["properties"]["type"]["const"] == RECONCILE_MTU
    assert operation["additionalProperties"] is False
    assert sorted(operation["required"]) == ["object_id", "type"]


def test_the_schema_still_says_a_run_is_not_a_change(client: TestClient):
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    run = schemas["Run"]
    assert "Change" in run["description"]
    assert "not a Change" in run["description"]
    # `change_created` stopped being "always false" and became the fact it names: whether
    # this run crossed the boundary. Planning, confirming and arming still do not.
    assert "write boundary" in run["properties"]["change_created"]["description"]
    assert "confirming and arming" in run["properties"]["change_created"]["description"]
    # And `host_mutated` is true only for a proven write — never for `write_unknown`.
    assert "write_unknown" in run["properties"]["host_mutated"]["description"]
    assert "awaiting_confirmation" in run["properties"]["state"]["description"]
    assert "recovery_required" in run["properties"]["state"]["description"]


def test_the_schema_says_execution_recovery_and_verification_are_not_real(client: TestClient):
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    execution = schemas["PlanExecution"]["properties"]
    assert "not_implemented" in execution["availability"]["description"]
    assert "Always false" in schemas["PlanRecovery"]["properties"]["armed"]["description"]
    assert (
        "Always false" in schemas["PlanVerification"]["properties"]["executed"]["description"]
    )
    assert (
        "Always false" in schemas["PlanConfirmation"]["properties"]["token_issued"]["description"]
    )
