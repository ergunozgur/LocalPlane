"""The write boundary over HTTP: confirm, apply, and the Change history.

The same estate as ``test_write_boundary.py`` — a real agent socket, a real privileged
helper socket, a simulated kernel behind the helper's transport — driven through the actual
API instead of through the services, because the two questions this file answers are about
the surface: what a caller can name, and what a caller is told.

The claims:

* **`apply` takes no request body**, so there is no parameter through which an MTU, an
  interface, a command, an argv, a provider or a shell could arrive. Bodies naming all of
  them change nothing, byte for byte;
* **a confirmation names a Run and a preview**, is single-use, and yields no token;
* **the Change history is a read** — including for a change that ended in
  `recovery_required`, where "check whether it is fine now" is exactly the read that must
  not quietly become a write;
* **there is no other way in**: no `/execute`, no generic rollback, no helper passthrough,
  no terminal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from localplane.agent.server import AgentServer
from localplane.agent.service import AgentService
from localplane.backend.app import create_app
from localplane.backend.config import Settings
from localplane.backend.db.database import open_database
from localplane.helper.client import HelperClient
from localplane.helper.server import HelperServer
from localplane.helper.service import HelperService
from tests.conftest import (
    FakeKernelLinks,
    FakeRouteQuery,
    FakeRunner,
    json_result,
    netlink_route,
    nmcli_devices,
    tailscale_status,
    write_interface,
)

API = "/api/v1"

#: The operator's own address, and the address they reach LocalPlane on. The endpoint is
#: carried by ``eth1``; the write target is ``eth0``, so every apply in this file happens
#: against an object *proven not* to be the management path.
OPERATOR = "192.0.2.130"
ENDPOINT = "192.0.2.215"


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


def _runner() -> FakeRunner:
    from localplane.agent.providers.linux_network import ADDR_ARGV, LINK_ARGV

    return FakeRunner(
        {
            LINK_ARGV: json_result(
                LINK_ARGV,
                [{"ifindex": 2, "ifname": "eth0"}, {"ifindex": 3, "ifname": "eth1"}],
            ),
            ADDR_ARGV: json_result(
                ADDR_ARGV,
                [
                    {"ifindex": 1, "ifname": "lo", "addr_info": []},
                    {"ifindex": 2, "ifname": "eth0", "addr_info": []},
                    {
                        "ifindex": 3,
                        "ifname": "eth1",
                        "addr_info": [
                            {
                                "family": "inet",
                                "local": ENDPOINT,
                                "prefixlen": 24,
                                "scope": "global",
                            }
                        ],
                    },
                ],
            ),
            **nmcli_devices(
                [
                    ("eth0", "ethernet", "unavailable", "", ""),
                    ("eth1", "ethernet", "unavailable", "", ""),
                ]
            ),
            **tailscale_status(),
        }
    )


def _routes() -> FakeRouteQuery:
    """The kernel's own answer: the operator's packets leave by ``eth1``, not ``eth0``."""
    return FakeRouteQuery(
        replies={
            OPERATOR: netlink_route(
                destination=OPERATOR, oif_index=3, preferred_source=ENDPOINT
            ),
            "127.0.0.1": netlink_route(
                destination="127.0.0.1", oif_index=1, preferred_source="127.0.0.1",
                route_type=2,
            ),
        }
    )


@pytest.fixture
def wired(sysfs: Path, fake_root: Path, tmp_path: Path, absent_docker: Path) -> Iterator[Any]:
    """A backend, an agent and a privileged helper, all reachable over real sockets.

    The client is given a peer address and a local endpoint, so its requests carry the
    transport evidence a real operator's would — which is what lets an apply happen at all:
    a call that cannot prove where it is connected from cannot prove the target is not the
    path it is connected over.
    """
    kernel = FakeKernelLinks(
        links={1: ("lo", 65536), 2: ("eth0", 1500), 3: ("eth1", 1500)}, sysfs_net=sysfs
    )
    helper = HelperServer(tmp_path / "helper.sock", HelperService(transport=kernel))
    helper.serve_in_thread()
    service = AgentService(
        root=fake_root,
        sysfs_net=sysfs,
        runner=_runner(),
        docker_socket=absent_docker,
        route_query=_routes(),
        helper_client=HelperClient(tmp_path / "helper.sock"),
        helper_socket=tmp_path / "helper.sock",
    )
    agent = AgentServer(tmp_path / "run" / "agent.sock", service)
    agent.serve_in_thread()
    settings = Settings(
        database_path=tmp_path / "store" / "localplane.db",
        agent_socket=agent.socket_path,
        agent_timeout_s=10.0,
        freshness_ttl_s=60.0,
        log_level="WARNING",
        observe_on_startup=False,
    )
    database = open_database(settings.database_path)
    app = create_app(settings, database)
    with TestClient(
        app, base_url=f"http://{ENDPOINT}:8080", client=(OPERATOR, 44321)
    ) as client:
        yield _Wired(client=client, kernel=kernel, sysfs=sysfs, helper=helper, app=app)
    database.close()
    agent.shutdown()
    agent.server_close()
    helper.shutdown()
    helper.server_close()


class _Wired:
    def __init__(
        self, client: TestClient, kernel: FakeKernelLinks, sysfs: Path, helper, app
    ):
        self.client = client
        self.kernel = kernel
        self.sysfs = sysfs
        self.helper = helper
        self.app = app

    def observe(self) -> None:
        assert self.client.post(f"{API}/network/observations/refresh").status_code == 200
        # Prove where *this* connection terminates, the way an operator's session does.
        proof = self.client.post(f"{API}/management-path/observations/refresh").json()
        assert proof["recorded"] is True, proof
        assert proof["management_path"]["state"] == "confirmed", proof

    def object_id(self, name: str = "eth0") -> str:
        body = self.client.get(f"{API}/network/interfaces").json()
        return next(i["object_id"] for i in body["interfaces"] if i["name"] == name)

    def adopt(self, name: str = "eth0") -> None:
        assert self.client.post(
            f"{API}/network/interfaces/{self.object_id(name)}/adopt"
        ).status_code == 200

    def drift(self, mtu: str = "1400", name: str = "eth0") -> None:
        (self.sysfs / name / "mtu").write_text(mtu + "\n")
        self.kernel.links[2] = (name, int(mtu))
        self.observe()

    def plan(self, name: str = "eth0") -> dict:
        response = self.client.post(
            f"{API}/runs",
            json={
                "operation": {
                    "type": "network.interface.reconcile_mtu",
                    "object_id": self.object_id(name),
                }
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    def mtu(self, name: str = "eth0") -> int:
        return int((self.sysfs / name / "mtu").read_text().strip())

    def loopback_client(self) -> TestClient:
        """The same backend, reached by automation that can prove nothing about its path."""
        return TestClient(
            self.app, base_url="http://127.0.0.1:8080", client=("127.0.0.1", 51000)
        )


@pytest.fixture
def planned(wired) -> Any:
    wired.observe()
    wired.adopt("eth0")
    wired.drift("1400")
    return wired


# ------------------------------------------------------------------------- the surface


def test_apply_ignores_every_value_a_caller_could_try_to_smuggle(planned):
    """There is no request body, so a body naming anything changes nothing.

    Sent as bodies rather than merely asserted absent from a model, because the useful claim
    is behavioural: a caller who tries to name an MTU, an interface, a command, an argv, a
    shell or a provider gets exactly the response they would have got with no body at all.
    """
    run = planned.plan()
    run_id = run["run_id"]
    planned.client.post(
        f"{API}/runs/{run_id}/confirm",
        json={"preview_id": run["preview"]["preview_id"], "acknowledge": True},
    )
    hostile = {
        "mtu": 9000,
        "desired_value": 9000,
        "desired": 9000,
        "value": 9000,
        "interface": "eth1",
        "object_id": "obj_somebody_elses",
        "command": "ip link set eth0 mtu 9000",
        "argv": ["ip", "link", "set", "eth0", "mtu", "9000"],
        "executable": "/usr/sbin/ip",
        "shell": "/bin/sh",
        "provider": "nmcli",
        "patch": {"mtu": 9000},
        "fields": {"mtu": 9000},
        "force": True,
    }
    response = planned.client.post(f"{API}/runs/{run_id}/apply", json=hostile)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "succeeded"
    assert body["change"]["desired_value"] == 1500, "a caller changed the target"
    assert body["change"]["mutation"]["outcome"] == "written"
    assert planned.kernel.mutations == [(2, 1500)]
    assert planned.mtu() == 1500


def test_confirm_refuses_a_body_naming_anything_it_does_not_declare(planned):
    run = planned.plan()
    for extra in ("mtu", "command", "argv", "shell", "actor", "user", "force"):
        response = planned.client.post(
            f"{API}/runs/{run['run_id']}/confirm",
            json={
                "preview_id": run["preview"]["preview_id"],
                "acknowledge": True,
                extra: "anything",
            },
        )
        assert response.status_code == 422, extra


def test_a_confirmation_for_one_run_does_not_authorise_another(planned):
    first = planned.plan()
    second = planned.plan()
    assert first["preview"]["preview_digest"] == second["preview"]["preview_digest"]

    assert planned.client.post(
        f"{API}/runs/{first['run_id']}/confirm",
        json={"preview_id": first["preview"]["preview_id"], "acknowledge": True},
    ).status_code == 200
    # The digest is identical and it authorises nothing.
    refused = planned.client.post(
        f"{API}/runs/{second['run_id']}/confirm",
        json={
            "preview_id": first["preview"]["preview_id"],
            "acknowledge": True,
            "expected_preview_digest": first["preview"]["preview_digest"],
        },
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "confirmation_preview_mismatch"

    blocked = planned.client.post(f"{API}/runs/{second['run_id']}/apply")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "confirmation_required"
    assert planned.kernel.mutations == []


def test_confirming_yields_no_token_and_names_no_actor(planned):
    run = planned.plan()
    body = planned.client.post(
        f"{API}/runs/{run['run_id']}/confirm",
        json={"preview_id": run["preview"]["preview_id"], "acknowledge": True},
    ).json()
    confirmation = body["confirmation"]
    assert confirmation["source"] == "unauthenticated_request"
    assert confirmation["consumed"] is False
    assert body["preview"]["confirmation"]["token_issued"] is False
    assert body["preview"]["confirmation"]["satisfied"] is True
    rendered = json.dumps(body)
    for absent in ('"actor"', '"actor_id"', '"user"', '"user_id"', '"operator_id"', '"token"'):
        assert absent not in rendered, absent
    # And nothing about the host moved.
    assert body["change_created"] is False
    assert body["host_mutated"] is False
    assert body["host_effect"] == "none"
    assert planned.kernel.mutations == []


def test_an_unacknowledged_confirmation_is_refused(planned):
    run = planned.plan()
    response = planned.client.post(
        f"{API}/runs/{run['run_id']}/confirm",
        json={"preview_id": run["preview"]["preview_id"], "acknowledge": False},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "confirmation_not_acknowledged"


def test_applying_without_confirming_leaves_the_run_awaiting_one(planned):
    run = planned.plan()
    response = planned.client.post(f"{API}/runs/{run['run_id']}/apply")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "confirmation_required"
    body = planned.client.get(f"{API}/runs/{run['run_id']}").json()
    assert body["state"] == "awaiting_confirmation"
    assert body["change"] is None
    assert body["change_created"] is False
    events = [e["event"] for e in body["events"]]
    # The Run reached the point of needing a confirmation, and then the apply was refused.
    # Both are recorded, in that order, rather than the refusal erasing the transition.
    assert events[-2:] == ["confirmation_required", "apply_refused"]


# ------------------------------------------------------------------ the change history


def test_the_change_history_reads_filters_and_writes_nothing(planned):
    run = planned.plan()
    planned.client.post(
        f"{API}/runs/{run['run_id']}/confirm",
        json={"preview_id": run["preview"]["preview_id"], "acknowledge": True},
    )
    applied = planned.client.post(f"{API}/runs/{run['run_id']}/apply").json()
    change_id = applied["change"]["change_id"]

    listing = planned.client.get(f"{API}/changes").json()
    assert listing["count"] == 1
    assert listing["changes"][0]["change_id"] == change_id
    assert listing["changes"][0]["result"] == "succeeded"
    assert listing["changes"][0]["host_effect"] == "written"

    first = planned.client.get(f"{API}/changes/{change_id}").json()
    mutations = list(planned.kernel.mutations)
    for _ in range(3):
        again = planned.client.get(f"{API}/changes/{change_id}").json()
        assert again == first
    assert planned.kernel.mutations == mutations, "reading history contacted the host"

    assert first["mutation"]["outcome"] == "written"
    assert first["mutation"]["provider"] == "linux.link"
    assert first["mutation"]["method"] == "netlink_rtm_newlink_mtu"
    assert first["verification"]["outcome"] == "verified"
    assert first["verification"]["observed_value"] == 1500
    assert first["rollback"]["required"] is False
    assert first["recovery"]["required"] is False
    assert first["host_mutated"] is True
    # The transcript comes with it, in order and typed.
    assert [e["event"] for e in first["events"]][:3] == [
        "run_planned",
        "confirmation_satisfied",
        "confirmation_required",
    ]
    assert first["events"][-1]["event"] == "run_finished"

    # Filters are a read too, and an id that does not exist is a 404 rather than an invention.
    assert planned.client.get(f"{API}/changes?result=succeeded").json()["count"] == 1
    assert planned.client.get(f"{API}/changes?result=recovery_required").json()["count"] == 0
    object_id = planned.object_id("eth0")
    assert planned.client.get(f"{API}/changes?object_id={object_id}").json()["count"] == 1
    assert planned.client.get(f"{API}/changes?object_id=obj_other").json()["count"] == 0
    missing = planned.client.get(f"{API}/changes/chg_nope")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "change_not_found"


def test_there_is_no_generic_execute_rollback_helper_or_terminal_endpoint(planned):
    run = planned.plan()
    for path in (
        "/execute",
        "/run",
        "/commands",
        "/shell",
        "/terminal",
        "/helper",
        "/helper/set_mtu",
        "/operations/network.interface.reconcile_mtu/execute",
        f"/runs/{run['run_id']}/execute",
        f"/runs/{run['run_id']}/arm",
        f"/runs/{run['run_id']}/verify",
        f"/runs/{run['run_id']}/rollback",
        f"/runs/{run['run_id']}/recover",
        "/network/interfaces/x/mtu",
        "/network/interfaces/x/set-mtu",
    ):
        assert planned.client.post(f"{API}{path}", json={}).status_code == 404, path
        assert planned.client.put(f"{API}{path}", json={}).status_code == 404, path
        assert planned.client.get(f"{API}{path}").status_code in (404, 405), path
    # And a change id that looks like a verb is a change id, not a verb.
    assert planned.client.get(f"{API}/changes/rollback").status_code == 404
    assert planned.client.post(f"{API}/changes/rollback", json={}).status_code == 405


def test_every_read_endpoint_is_still_pure(planned):
    """A page refresh must not be able to change what LocalPlane has recorded."""
    run = planned.plan()
    planned.client.post(
        f"{API}/runs/{run['run_id']}/confirm",
        json={"preview_id": run["preview"]["preview_id"], "acknowledge": True},
    )
    applied = planned.client.post(f"{API}/runs/{run['run_id']}/apply").json()
    change_id = applied["change"]["change_id"]
    mutations = list(planned.kernel.mutations)
    sweeps = planned.client.get(f"{API}/observations/sweeps").json()["count"]

    for path in (
        "/status",
        "/host",
        "/network/interfaces",
        "/observations/sweeps",
        "/findings",
        "/runs",
        f"/runs/{run['run_id']}",
        f"/runs/{run['run_id']}/preview",
        "/changes",
        f"/changes/{change_id}",
        "/management-path",
    ):
        assert planned.client.get(f"{API}{path}").status_code == 200, path
    assert planned.kernel.mutations == mutations
    assert planned.client.get(f"{API}/observations/sweeps").json()["count"] == sweeps


def test_automation_that_cannot_prove_its_path_cannot_apply(planned):
    """The reason the plan was safe to look at is evidence the second caller does not have.

    An operator over a proven path plans and confirms; a loopback call — automation, a cron
    job, a script on the box — cannot apply it, because it cannot prove the target is not
    the path *it* is connected over. It does not inherit the operator's proof, and the
    refusal says the plan is stale rather than pretending the check passed.
    """
    run = planned.plan()
    planned.client.post(
        f"{API}/runs/{run['run_id']}/confirm",
        json={"preview_id": run["preview"]["preview_id"], "acknowledge": True},
    )
    with planned.loopback_client() as automation:
        refused = automation.post(f"{API}/runs/{run['run_id']}/apply")
        assert refused.status_code == 409
        error = refused.json()["error"]
        assert error["code"] == "preview_stale"
        assert "protection_changed" in {r["code"] for r in error["detail"]["reasons"]}
    assert planned.kernel.mutations == []
    assert planned.client.get(f"{API}/changes").json()["count"] == 0

    # The operator, over the path they proved, can.
    applied = planned.client.post(f"{API}/runs/{run['run_id']}/apply")
    assert applied.status_code == 200
    assert applied.json()["state"] == "succeeded"


def test_the_run_response_says_what_happened_to_the_host(planned):
    run = planned.plan()
    planned.client.post(
        f"{API}/runs/{run['run_id']}/confirm",
        json={"preview_id": run["preview"]["preview_id"], "acknowledge": True},
    )
    body = planned.client.post(f"{API}/runs/{run['run_id']}/apply").json()
    assert body["state"] == "succeeded"
    assert body["host_effect"] == "written"
    assert body["host_mutated"] is True
    assert body["change_created"] is True
    assert body["change_id"] == body["change"]["change_id"]
    assert body["confirmation"]["consumed"] is True
    assert body["checkpoint"]["restores_value"] == 1400
    assert body["checkpoint"]["management_path"] == "not_on_management_path"
    assert body["checkpoint"]["execution_correlation"]["ifindex"] == 2
    assert body["preview"]["confirmation"]["satisfied"] is True
    # The list view agrees with the detail view.
    listed = planned.client.get(f"{API}/runs").json()["runs"]
    assert [(r["state"], r["change_created"]) for r in listed] == [("succeeded", True)]


def test_a_recovery_required_change_publishes_what_is_known_and_what_is_not(planned):
    """The actionable evidence, without a fabricated promise to retry."""
    run = planned.plan()
    planned.client.post(
        f"{API}/runs/{run['run_id']}/confirm",
        json={"preview_id": run["preview"]["preview_id"], "acknowledge": True},
    )

    state = {"n": 0}
    original = planned.kernel.mutate

    def acknowledge_and_diverge(frame: bytes) -> bytes:
        state["n"] += 1
        if state["n"] == 1:
            reply = original(frame)
            planned.kernel.links[2] = ("eth0", 9000)
            (planned.sysfs / "eth0" / "mtu").write_text("9000\n")
            return reply
        from tests.conftest import netlink_ack

        from localplane.helper.mtu import _NLMSGHDR

        return netlink_ack(_NLMSGHDR.unpack_from(frame, 0)[3], errno=0)

    planned.kernel.mutate = acknowledge_and_diverge  # type: ignore[method-assign]
    try:
        body = planned.client.post(f"{API}/runs/{run['run_id']}/apply").json()
    finally:
        planned.kernel.mutate = original  # type: ignore[method-assign]

    assert body["state"] == "recovery_required"
    change = body["change"]
    assert change["result"] == "recovery_required"
    recovery = change["recovery"]
    assert recovery["required"] is True
    assert recovery["reason"] == "rollback_verification_failed"
    assert recovery["known"]["before_value"] == 1400
    assert recovery["known"]["desired_value"] == 1500
    assert recovery["known"]["mutation_outcome"] == "written"
    assert recovery["known"]["rollback_outcome"] == "written"
    assert recovery["known"]["last_read_value"] == 9000
    assert recovery["unknown"], "recovery must say what it does not know"
    assert recovery["object_write_locked"] is True
    # There is no retry endpoint promising to put it right.
    assert planned.client.post(
        f"{API}/changes/{change['change_id']}/retry", json={}
    ).status_code == 404
    # And a second change against the held object is refused rather than queued.
    planned.observe()
    second = planned.plan()
    planned.client.post(
        f"{API}/runs/{second['run_id']}/confirm",
        json={"preview_id": second["preview"]["preview_id"], "acknowledge": True},
    )
    blocked = planned.client.post(f"{API}/runs/{second['run_id']}/apply")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "object_write_locked"
