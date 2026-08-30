"""The management path over HTTP: what a caller can say, and what only the socket can.

The whole surface is two reads and one observation, and the property that matters most is
what is *absent* from all three: there is no peer address, no local address, no interface,
no route target, no command and no argv a caller can supply. The evidence comes from the
server side of the connection the request arrived on, and a request body claiming otherwise
is not refused so much as unreachable — there is nothing for it to reach.

The client is given a real address on both ends, so these exercise the path a real operator
takes. `test_runs_api.py` covers the opposite case by default: a client whose transport
carries no address at all, where the honest answer is unknown.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from localplane.agent.server import AgentServer
from localplane.agent.service import AgentService
from localplane.backend.app import create_app
from localplane.backend.config import Settings
from localplane.backend.db.database import open_database
from tests.conftest import FakeRouteQuery, FakeRunner, json_result, netlink_route, write_interface

API = "/api/v1"
RECONCILE_MTU = "network.interface.reconcile_mtu"

OPERATOR = "192.0.2.130"
ENDPOINT = "192.0.2.215"
IMPOSTOR = "192.0.2.131"


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
        }
    )


@pytest.fixture
def routes() -> FakeRouteQuery:
    """A kernel that routes the operator out of index 3 — the link carrying the endpoint."""
    return FakeRouteQuery(
        {
            OPERATOR: netlink_route(
                destination=OPERATOR, oif_index=3, preferred_source=ENDPOINT
            ),
            IMPOSTOR: netlink_route(
                destination=IMPOSTOR, oif_index=3, preferred_source=ENDPOINT
            ),
            "127.0.0.1": netlink_route(
                destination="127.0.0.1", oif_index=1, preferred_source="127.0.0.1",
                route_type=2,
            ),
        }
    )


def _client(
    tmp_path: Path,
    fake_root: Path,
    sysfs: Path,
    runner: FakeRunner,
    routes: FakeRouteQuery,
    absent_docker: Path,
    *,
    peer: str,
    endpoint: str,
    name: str = "agent.sock",
) -> Iterator[TestClient]:
    service = AgentService(
        root=fake_root,
        sysfs_net=sysfs,
        runner=runner,
        docker_socket=absent_docker,
        route_query=routes,
        helper_socket=tmp_path / "run" / "helper-absent.sock",
    )
    server = AgentServer(tmp_path / "run" / name, service)
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
    with TestClient(
        create_app(settings, database),
        base_url=f"http://{endpoint}:8080",
        client=(peer, 44321),
    ) as test_client:
        yield test_client
    database.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture
def client(
    tmp_path: Path, fake_root: Path, sysfs: Path, runner: FakeRunner,
    routes: FakeRouteQuery, absent_docker: Path,
) -> Iterator[TestClient]:
    """An operator at 192.0.2.130 reaching LocalPlane on 192.0.2.215."""
    yield from _client(
        tmp_path, fake_root, sysfs, runner, routes, absent_docker,
        peer=OPERATOR, endpoint=ENDPOINT,
    )


# ------------------------------------------------------------------------------ helpers


def interfaces(client: TestClient) -> dict:
    return {i["name"]: i for i in client.get(f"{API}/network/interfaces").json()["interfaces"]}


def object_id(client: TestClient, name: str) -> str:
    return interfaces(client)[name]["object_id"]


def observe(client: TestClient) -> dict:
    return client.post(f"{API}/management-path/observations/refresh").json()


def drift(client: TestClient, name: str) -> str:
    """Adopt an interface and revise its intent away from the runtime. Writes no host."""
    oid = object_id(client, name)
    client.post(f"{API}/network/interfaces/{oid}/adopt")
    intent = client.get(f"{API}/network/interfaces/{oid}/intent").json()
    client.post(
        f"{API}/network/interfaces/{oid}/intent/revise",
        json={"expected_intent_id": intent["intent_id"], "fields": {"mtu": 9000}},
    )
    client.post(f"{API}/network/observations/refresh")
    return oid


def plan(client: TestClient, object_id: str) -> dict:
    response = client.post(
        f"{API}/runs",
        json={"operation": {"type": RECONCILE_MTU, "object_id": object_id}},
    )
    assert response.status_code == 201, response.json()
    return response.json()


# --------------------------------------------------------------------------- observing


def test_observing_confirms_the_path_from_the_connection_it_arrived_on(client: TestClient):
    body = observe(client)
    assert body["host_mutated"] is False
    assert body["host_effect"] == "none"
    assert body["recorded"] is True

    path = body["management_path"]
    assert path["state"] == "confirmed"
    assert path["object_id"] == object_id(client, "eth1")
    assert path["object_name"] == "eth1"
    assert path["reason"] == "management_path_confirmed"
    assert path["missing_evidence"] == []


def test_the_transport_it_reports_is_the_one_the_socket_carried(client: TestClient):
    transport = observe(client)["management_path"]["transport"]
    assert transport == {
        "peer_address": OPERATOR,
        "peer_family": "inet",
        "local_endpoint_address": ENDPOINT,
        "local_endpoint_family": "inet",
        "usable": True,
        "reason": None,
    }


def test_the_evidence_it_publishes_is_the_evidence_it_stored(client: TestClient):
    evidence = observe(client)["management_path"]["evidence"]
    assert evidence["observation_id"].startswith("mpo_")
    assert evidence["transport_peer_address"] == OPERATOR
    assert evidence["local_endpoint_address"] == ENDPOINT
    assert evidence["capability"] == "network.route.observe"
    assert evidence["provider"] == "linux.route"
    assert evidence["method"] == "netlink_rtm_getroute"
    assert evidence["route"]["status"] == "resolved"
    assert evidence["route"]["oif_index"] == 3
    assert evidence["route"]["preferred_source"] == ENDPOINT


def test_the_evidence_names_an_index_and_the_backend_does_the_correlating(client: TestClient):
    """The agent reported 3. That it means eth1 is LocalPlane's judgement, not the agent's."""
    body = observe(client)["management_path"]
    assert body["evidence"]["route"]["oif_index"] == 3
    assert body["object_id"] == object_id(client, "eth1")
    assert "eth1" not in str(body["evidence"])


# ------------------------------------------------------- a caller cannot supply evidence


def test_a_body_naming_a_peer_changes_nothing(client: TestClient):
    """There is no parameter for it to be, so the claim is simply not consulted."""
    honest = observe(client)["management_path"]
    response = client.post(
        f"{API}/management-path/observations/refresh",
        json={
            "peer_address": "203.0.113.9",
            "local_endpoint_address": "203.0.113.10",
            "transport_peer": "203.0.113.9",
            "interface": "eth0",
            "object_id": object_id(client, "eth0"),
            "route_to": "203.0.113.9",
            "target_address": "203.0.113.9",
            "command": "ip route get 203.0.113.9",
            "argv": ["ip", "route", "get", "203.0.113.9"],
            "shell": True,
        },
    )
    assert response.status_code == 200
    claimed = response.json()["management_path"]
    assert claimed["transport"] == honest["transport"]
    assert claimed["object_id"] == honest["object_id"]
    assert claimed["evidence"]["transport_peer_address"] == OPERATOR
    assert "203.0.113" not in str(response.json())


def test_query_parameters_naming_a_target_change_nothing(client: TestClient):
    honest = observe(client)["management_path"]
    body = client.post(
        f"{API}/management-path/observations/refresh",
        params={
            "peer_address": "203.0.113.9",
            "destination": "203.0.113.9",
            "interface": "eth0",
            "names": ["eth0"],
        },
    ).json()["management_path"]
    assert body["transport"] == honest["transport"]
    assert body["object_id"] == honest["object_id"]


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Forwarded-For": "203.0.113.9"},
        {"X-Forwarded-For": "203.0.113.9, 192.0.2.130"},
        {"X-Real-IP": "203.0.113.9"},
        {"Forwarded": 'for=203.0.113.9;host=example;proto=https'},
        {"Host": "203.0.113.10:8080"},
        {"X-Forwarded-Host": "203.0.113.10"},
        {"X-Forwarded-Proto": "https"},
    ],
)
def test_proxy_headers_are_read_by_nothing(client: TestClient, headers: dict):
    """A header is written by whoever is asking. There is no setting that trusts one."""
    honest = observe(client)["management_path"]
    body = client.post(
        f"{API}/management-path/observations/refresh", headers=headers
    ).json()["management_path"]
    assert body["transport"]["peer_address"] == OPERATOR
    assert body["transport"]["local_endpoint_address"] == ENDPOINT
    assert body["object_id"] == honest["object_id"]
    assert "203.0.113" not in str(body)


def test_a_header_cannot_make_an_unusable_transport_usable(
    tmp_path: Path, fake_root: Path, sysfs: Path, runner: FakeRunner,
    routes: FakeRouteQuery, absent_docker: Path,
):
    """The reverse-proxy case, stated plainly: the direct peer is local, so it is unknown."""
    for local_client in _client(
        tmp_path, fake_root, sysfs, runner, routes, absent_docker,
        peer="127.0.0.1", endpoint="127.0.0.1",
    ):
        body = local_client.post(
            f"{API}/management-path/observations/refresh",
            headers={"X-Forwarded-For": OPERATOR, "X-Real-IP": OPERATOR},
        ).json()
        assert body["recorded"] is False
        path = body["management_path"]
        assert path["state"] == "unresolved"
        assert path["object_id"] is None
        assert path["reason"] == "transport_peer_local"
        assert path["transport"]["usable"] is False


def test_a_local_request_records_nothing_at_all(
    tmp_path: Path, fake_root: Path, sysfs: Path, runner: FakeRunner,
    routes: FakeRouteQuery, absent_docker: Path,
):
    for local_client in _client(
        tmp_path, fake_root, sysfs, runner, routes, absent_docker,
        peer="127.0.0.1", endpoint=ENDPOINT,
    ):
        # Constructing the agent probed the route capability against its own fixed
        # loopback destination. Nothing after that is allowed to reach the kernel.
        asked = list(routes.requests)
        local_client.post(f"{API}/management-path/observations/refresh")
        assert local_client.get(f"{API}/management-path").json()["state"] == "unresolved"
        assert routes.requests == asked


# ------------------------------------------------------------------------ GET is pure


def test_reading_the_management_path_contacts_nothing(client: TestClient, routes: FakeRouteQuery):
    observe(client)
    asked = list(routes.requests)
    for _ in range(3):
        assert client.get(f"{API}/management-path").json()["state"] == "confirmed"
    assert routes.requests == asked


def test_reading_the_management_path_writes_nothing(client: TestClient):
    observe(client)
    before = client.get(f"{API}/management-path").json()
    for _ in range(3):
        client.get(f"{API}/management-path")
        client.get(f"{API}/network/interfaces/{object_id(client, 'eth1')}/protection")
    after = client.get(f"{API}/management-path").json()
    assert after["evidence"]["observation_id"] == before["evidence"]["observation_id"]
    assert after["evidence"]["observed_at"] == before["evidence"]["observed_at"]


def test_reading_before_observing_says_unobserved_rather_than_guessing(client: TestClient):
    body = client.get(f"{API}/management-path").json()
    assert body["state"] == "unresolved"
    assert body["object_id"] is None
    assert body["reason"] == "management_path_unobserved"
    assert sorted(body["missing_evidence"]) == ["route.observe", "session.peer"]
    assert body["evidence"] is None
    # And the transport is still reported, because that part *is* known.
    assert body["transport"]["peer_address"] == OPERATOR


def test_the_ttl_is_published_so_a_reader_knows_how_long_a_proof_lasts(client: TestClient):
    assert client.get(f"{API}/management-path").json()["evidence_ttl_seconds"] == 60.0


# ----------------------------------------------------------------------- object protection


def test_the_confirmed_interface_is_protected_by_the_management_path(client: TestClient):
    observe(client)
    body = client.get(f"{API}/network/interfaces/{object_id(client, 'eth1')}/protection").json()
    assert body["status"] == "protected"
    assert body["reasons"] == ["management_path"]
    assert body["unresolved"] == []
    assert body["management_path"] == "on_management_path"
    assert body["reason"] == "management_path_confirmed"
    assert body["assessed"][0]["evidence_id"].startswith("mpo_")
    assert body["implemented_reasons"] == ["management_path"]


def test_another_interface_is_clear_and_says_which_reasons_were_evaluated(client: TestClient):
    observe(client)
    body = client.get(f"{API}/network/interfaces/{object_id(client, 'eth0')}/protection").json()
    assert body["status"] == "clear"
    assert body["management_path"] == "not_on_management_path"
    assert body["reason"] == "not_the_management_path"
    assert body["implemented_reasons"] == ["management_path"]
    assert "not a word for `safe`" in body["note"]


def test_every_interface_is_unknown_while_the_path_itself_is(client: TestClient):
    for name in ("lo", "eth0", "eth1"):
        body = client.get(
            f"{API}/network/interfaces/{object_id(client, name)}/protection"
        ).json()
        assert body["status"] == "unknown"
        assert body["management_path"] == "unknown"
        assert body["unresolved"] == ["management_path"]


def test_protection_and_ownership_are_answered_separately(client: TestClient):
    observe(client)
    oid = object_id(client, "eth0")
    protection = client.get(f"{API}/network/interfaces/{oid}/protection").json()
    ownership = client.get(f"{API}/network/interfaces/{oid}/provenance").json()
    assert protection["status"] == "clear"
    # Two documents about one object, sharing nothing but its identity.
    assert protection["object_id"] == ownership["object_id"]
    for word in ("created_by", "configured_by", "claims", "adoption", "sources", "state"):
        assert word not in protection
    for word in ("protected", "management_path", "unresolved", "implemented_reasons"):
        assert word not in ownership


def test_protection_for_an_unknown_object_is_a_structured_404(client: TestClient):
    response = client.get(f"{API}/network/interfaces/obj_nope/protection")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "object_not_found"


# ------------------------------------------------------------- evidence is not transferable


def test_another_peer_gets_no_benefit_from_this_ones_proof(
    tmp_path: Path, fake_root: Path, sysfs: Path, runner: FakeRunner,
    routes: FakeRouteQuery, absent_docker: Path,
):
    """Two operators, one store. The second has proven nothing until it observes."""
    for first in _client(
        tmp_path, fake_root, sysfs, runner, routes, absent_docker,
        peer=OPERATOR, endpoint=ENDPOINT,
    ):
        assert observe(first)["management_path"]["state"] == "confirmed"
        with TestClient(
            first.app, base_url=f"http://{ENDPOINT}:8080", client=(IMPOSTOR, 51000)
        ) as second:
            body = second.get(f"{API}/management-path").json()
            assert body["state"] == "unresolved"
            assert body["reason"] == "management_path_unobserved"
            assert body["object_id"] is None
            # And it can observe for itself, which is a different row of evidence.
            mine = second.post(
                f"{API}/management-path/observations/refresh"
            ).json()["management_path"]
            assert mine["state"] == "confirmed"
            assert (
                mine["evidence"]["observation_id"]
                != first.get(f"{API}/management-path").json()["evidence"]["observation_id"]
            )


# ----------------------------------------------------------------- the Change Engine


def test_a_plan_against_the_management_path_is_protected_and_high_risk(client: TestClient):
    observe(client)
    body = plan(client, drift(client, "eth1"))
    preview = body["preview"]

    protection = preview["protection"]
    assert protection["status"] == "protected"
    assert protection["reasons"] == ["management_path"]
    assert protection["management_path"] == "on_management_path"
    assert protection["evidence_id"].startswith("mpo_")

    assert preview["risk"]["tier"] == "high"
    assert "target_is_management_path" in {f["code"] for f in preview["risk"]["factors"]}
    assert preview["confirmation"]["required"] is True
    assert preview["confirmation"]["method"] == "typed"
    assert "target_is_management_path" in preview["how"]["blockers"]
    assert "management_path_unproven" not in preview["how"]["blockers"]

    # And none of it moves the write boundary an inch. This is the plan the build can now
    # execute in general and refuses to execute here, which is the whole point: what blocks
    # it is what the target *is*, proven, not a feature LocalPlane has not written.
    assert body["host_mutated"] is False
    assert body["change_created"] is False
    assert body["change"] is None
    assert preview["how"]["availability"] == "available"
    assert preview["how"]["eligibility"] == "blocked"
    assert preview["confirmation"]["token_issued"] is False
    assert preview["confirmation"]["satisfied"] is False
    assert preview["recovery"]["armed"] is False
    assert preview["verification"]["executed"] is False


def test_a_plan_against_a_proven_non_target_drops_the_unproven_blocker(client: TestClient):
    observe(client)
    preview = plan(client, drift(client, "eth0"))["preview"]

    assert preview["protection"]["status"] == "clear"
    assert preview["protection"]["management_path"] == "not_on_management_path"
    assert "management_path_unproven" not in preview["how"]["blockers"]
    assert "target_is_management_path" not in preview["how"]["blockers"]
    # Still blocked, and for the reason that is genuinely there: this fixture host has no
    # privileged helper, so the agent does not declare the capability an execution needs.
    assert preview["how"]["eligibility"] == "blocked"
    assert preview["how"]["blockers"] == ["required_capability_undeclared"]
    # Risk falls back to the operation's own tier, and confirmation with it.
    assert preview["risk"]["tier"] == "medium"
    assert preview["confirmation"]["method"] == "acknowledge"


def test_the_preview_freezes_the_judgement_it_showed(client: TestClient):
    """Published, not re-derived. What an operator was shown stays what they were shown."""
    observe(client)
    body = plan(client, drift(client, "eth1"))
    preview = client.get(f"{API}/runs/{body['run_id']}/preview").json()
    assert preview["protection"] == body["preview"]["protection"]
    assert preview["risk"] == body["preview"]["risk"]
    assert preview["preview_digest"] == body["preview"]["preview_digest"]
    assert preview["digest_version"] == 6


def test_a_preview_read_over_a_transport_that_proves_nothing_becomes_stale(
    tmp_path: Path, fake_root: Path, sysfs: Path, runner: FakeRunner,
    routes: FakeRouteQuery, absent_docker: Path,
):
    """The safety judgement rests on the session it was made from, and says so.

    A plan reviewed by an operator whose connection proved the management path does not go
    on being current when the next reader is a local automation call that proved nothing.
    That is not pedantry: the reason the plan was safe to look at is evidence the second
    caller does not have.
    """
    for operator in _client(
        tmp_path, fake_root, sysfs, runner, routes, absent_docker,
        peer=OPERATOR, endpoint=ENDPOINT,
    ):
        observe(operator)
        body = plan(operator, drift(operator, "eth0"))
        assert body["preview"]["validity"]["state"] == "current"

        with TestClient(
            operator.app, base_url="http://127.0.0.1:8080", client=("127.0.0.1", 51000)
        ) as automation:
            stale = automation.get(f"{API}/runs/{body['run_id']}/preview").json()
            assert stale["validity"]["state"] == "stale"
            assert "protection_changed" in {r["code"] for r in stale["validity"]["reasons"]}
            # The published document is unchanged; only the verdict about it moved.
            assert stale["protection"] == body["preview"]["protection"]
            assert stale["preview_digest"] == body["preview"]["preview_digest"]


def test_planning_still_writes_nothing_to_the_host(client: TestClient, sysfs: Path):
    def snapshot() -> dict:
        return {
            entry.name: {f.name: f.read_text() for f in sorted(entry.iterdir()) if f.is_file()}
            for entry in sorted(sysfs.iterdir())
            if (entry / "ifindex").exists()
        }

    before = snapshot()
    observe(client)
    plan(client, drift(client, "eth1"))
    client.get(f"{API}/management-path")
    assert snapshot() == before


def test_there_is_still_no_way_to_apply_to_the_management_path(client: TestClient):
    """The write boundary exists now, and this target is on the wrong side of it.

    A plan whose target is *proven* to carry the operator's connection cannot be applied:
    not with a confirmation, not with a stronger one, and not through any side entrance.
    The refusal is a typed 409 naming the target rather than a 404 pretending the feature
    is missing — and the endpoints that never existed still do not.
    """
    observe(client)
    run_id = plan(client, drift(client, "eth1"))["run_id"]

    refused = client.post(f"{API}/runs/{run_id}/apply")
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "preview_not_executable"

    confirmed = client.post(
        f"{API}/runs/{run_id}/confirm",
        json={"preview_id": plan_preview_id(client, run_id), "acknowledge": True},
    )
    assert confirmed.status_code == 409
    assert confirmed.json()["error"]["code"] in {
        "preview_not_executable",
        "confirmation_method_unsupported",
    }

    for path in (
        f"/runs/{run_id}/execute",
        f"/runs/{run_id}/arm",
        f"/runs/{run_id}/verify",
        f"/runs/{run_id}/rollback",
        "/management-path/apply",
        "/management-path/declare",
        "/network/interfaces/{object_id}/protection/override",
        "/execute",
    ):
        assert client.post(f"{API}{path}", json={}).status_code == 404, path

    # Nothing was armed, no change exists, and the object is not locked.
    assert client.get(f"{API}/changes").json()["changes"] == []
    assert client.get(f"{API}/runs/{run_id}").json()["checkpoint"] is None


def plan_preview_id(client: TestClient, run_id: str) -> str:
    return client.get(f"{API}/runs/{run_id}").json()["preview"]["preview_id"]


def test_the_management_path_needs_a_host_before_it_can_answer(
    tmp_path: Path, fake_root: Path, sysfs: Path, runner: FakeRunner,
    routes: FakeRouteQuery, absent_docker: Path,
):
    """No host identified means no estate to place a path in, and the 404 says so."""
    settings = Settings(
        database_path=tmp_path / "empty" / "localplane.db",
        agent_socket=tmp_path / "nowhere.sock",
        agent_timeout_s=1,
        freshness_ttl_s=60,
        log_level="WARNING",
        observe_on_startup=False,
    )
    database = open_database(settings.database_path)
    with TestClient(
        create_app(settings, database),
        base_url=f"http://{ENDPOINT}:8080",
        client=(OPERATOR, 44321),
    ) as blank:
        for method, path in (
            ("get", f"{API}/management-path"),
            ("post", f"{API}/management-path/observations/refresh"),
        ):
            response = getattr(blank, method)(path)
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "host_unknown"
    database.close()


def test_an_agent_that_is_gone_leaves_the_path_unresolved_rather_than_erroring(
    tmp_path: Path, fake_root: Path, sysfs: Path, runner: FakeRunner,
    routes: FakeRouteQuery, absent_docker: Path,
):
    """One of two sources being unavailable is an answer about evidence, not a 503."""
    service = AgentService(
        root=fake_root, sysfs_net=sysfs, runner=runner,
        docker_socket=absent_docker, route_query=routes,
        helper_socket=tmp_path / "run" / "helper-absent.sock",
    )
    server = AgentServer(tmp_path / "run" / "agent.sock", service)
    thread = server.serve_in_thread()
    settings = Settings(
        database_path=tmp_path / "store" / "localplane.db",
        agent_socket=server.socket_path,
        agent_timeout_s=5,
        freshness_ttl_s=60,
        log_level="WARNING",
        observe_on_startup=True,
    )
    database = open_database(settings.database_path)
    with TestClient(
        create_app(settings, database),
        base_url=f"http://{ENDPOINT}:8080",
        client=(OPERATOR, 44321),
    ) as client:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        response = client.post(f"{API}/management-path/observations/refresh")
        assert response.status_code == 200
        body = response.json()
        assert body["recorded"] is True
        path = body["management_path"]
        assert path["state"] == "unresolved"
        assert path["reason"] == "route_lookup_unavailable"
        assert path["evidence"]["route"]["status"] == "unavailable"
        assert path["evidence"]["route"]["reason"] == "agent_unavailable"
        assert path["evidence"]["route"]["oif_index"] is None
    database.close()


def test_no_endpoint_accepts_a_declared_management_interface(client: TestClient):
    """A declaration is a claim, not evidence, and this build takes neither."""
    oid = object_id(client, "eth0")
    for path, body in (
        (f"/network/interfaces/{oid}/protection", {"management_path": True}),
        ("/management-path", {"object_id": oid}),
    ):
        assert client.post(f"{API}{path}", json=body).status_code == 405
