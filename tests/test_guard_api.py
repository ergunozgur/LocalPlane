"""The connection guard over HTTP: what a caller can name, and what a caller is told.

The same estate as ``test_changes_api.py``, with one thing moved: the address the operator
reaches LocalPlane on is carried by ``eth0``, and the kernel's own route to them leaves by
``eth0`` — so the object every run in this file targets is *proven* to be the path the
request is arriving over. That is the situation every earlier build refused
outright, and it is the only situation a guard exists for.

The claims:

* **`guard/keep` carries no value, no object and no path**, and a body naming any of them
  is refused at the edge rather than ignored;
* **the proof is the transport of the request**, so the same call over loopback — which can
  prove nothing about where it is connected from — cannot keep a guarded change no matter
  what it sends;
* **reading is still reading**: a guarded run can be inspected without settling, releasing
  or extending anything, and two consecutive reads are byte-identical;
* **the surface did not grow an escape hatch**: no arm, no disarm, no extend, no probe.
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
from tests.test_connection_guard import ManualTimers

API = "/api/v1"

#: The operator, and the address they reach LocalPlane on — carried by ``eth0``, which is
#: also what every run here changes. That is the whole subject of the file.
OPERATOR = "192.0.2.130"
ENDPOINT = "192.0.2.215"


@pytest.fixture
def sysfs(sysfs_net: Path) -> Path:
    write_interface(sysfs_net, "lo", ifindex=1, address="00:00:00:00:00:00", arphrd="772",
                    flags="0x9", operstate="unknown", carrier="1", mtu="65536",
                    speed=None, duplex=None)
    write_interface(sysfs_net, "eth0", ifindex=2, address="02:00:00:00:00:10", flags="0x1003",
                    operstate="up", carrier="1", mtu="1500", device="fd580000.ethernet",
                    subsystem="platform")
    write_interface(sysfs_net, "eth1", ifindex=3, address="02:00:00:00:00:12", flags="0x1003",
                    operstate="up", carrier="1", mtu="1500", speed="1000", duplex="full",
                    device="1-1.4:1.0", subsystem="usb")
    return sysfs_net


def _runner() -> FakeRunner:
    from localplane.agent.providers.linux_network import ADDR_ARGV, LINK_ARGV

    return FakeRunner({
        LINK_ARGV: json_result(LINK_ARGV, [{"ifindex": 2, "ifname": "eth0"},
                                           {"ifindex": 3, "ifname": "eth1"}]),
        ADDR_ARGV: json_result(ADDR_ARGV, [
            {"ifindex": 1, "ifname": "lo", "addr_info": []},
            {"ifindex": 2, "ifname": "eth0", "addr_info": [
                {"family": "inet", "local": ENDPOINT, "prefixlen": 24, "scope": "global"}]},
            {"ifindex": 3, "ifname": "eth1", "addr_info": []},
        ]),
        **nmcli_devices([("eth0", "ethernet", "unavailable", "", ""),
                         ("eth1", "ethernet", "unavailable", "", "")]),
        **tailscale_status(),
    })


def _routes() -> FakeRouteQuery:
    """The kernel's own answer: the operator's packets leave by ``eth0``."""
    return FakeRouteQuery(replies={
        OPERATOR: netlink_route(destination=OPERATOR, oif_index=2, preferred_source=ENDPOINT),
        "127.0.0.1": netlink_route(destination="127.0.0.1", oif_index=1,
                                   preferred_source="127.0.0.1", route_type=2),
    })


class _Wired:
    def __init__(self, client, kernel, sysfs, timers, app):
        self.client = client
        self.kernel = kernel
        self.sysfs = sysfs
        self.timers = timers
        self.app = app

    def observe(self) -> None:
        assert self.client.post(f"{API}/network/observations/refresh").status_code == 200
        proof = self.client.post(f"{API}/management-path/observations/refresh").json()
        assert proof["recorded"] is True, proof
        assert proof["management_path"]["state"] == "confirmed", proof

    def object_id(self, name: str = "eth0") -> str:
        body = self.client.get(f"{API}/network/interfaces").json()
        return next(i["object_id"] for i in body["interfaces"] if i["name"] == name)

    def adopt(self, name: str = "eth0") -> None:
        assert self.client.post(
            f"{API}/network/interfaces/{self.object_id(name)}/adopt").status_code == 200

    def drift(self, mtu: str = "1400", name: str = "eth0") -> None:
        (self.sysfs / name / "mtu").write_text(mtu + "\n")
        self.kernel.links[2] = (name, int(mtu))
        self.observe()

    def plan(self, name: str = "eth0") -> dict:
        response = self.client.post(f"{API}/runs", json={"operation": {
            "type": "network.interface.reconcile_mtu", "object_id": self.object_id(name)}})
        assert response.status_code == 201, response.text
        return response.json()

    def confirm(self, run: dict, **overrides: Any) -> Any:
        body = {"preview_id": run["preview"]["preview_id"], "acknowledge": True,
                "acknowledge_object": "eth0"}
        body.update(overrides)
        return self.client.post(f"{API}/runs/{run['run_id']}/confirm", json=body)

    def mtu(self, name: str = "eth0") -> int:
        return int((self.sysfs / name / "mtu").read_text().strip())

    def loopback_client(self) -> TestClient:
        return AuthenticatedTestClient(self.app, base_url="http://127.0.0.1:8080",
                          client=("127.0.0.1", 51000))


@pytest.fixture
def wired(sysfs: Path, fake_root: Path, tmp_path: Path, absent_docker: Path) -> Iterator[Any]:
    kernel = FakeKernelLinks(links={1: ("lo", 65536), 2: ("eth0", 1500), 3: ("eth1", 1500)},
                             sysfs_net=sysfs)
    helper = HelperServer(tmp_path / "helper.sock", HelperService(transport=kernel))
    helper.serve_in_thread()
    timers = ManualTimers()
    service = AgentService(root=fake_root, sysfs_net=sysfs, runner=_runner(),
                           docker_socket=absent_docker, route_query=_routes(),
                           helper_client=HelperClient(tmp_path / "helper.sock"),
                           helper_socket=tmp_path / "helper.sock",
                           guard_timer=timers)
    agent = AgentServer(tmp_path / "run" / "agent.sock", service)
    agent.serve_in_thread()
    settings = Settings(database_path=tmp_path / "store" / "localplane.db",
                        agent_socket=agent.socket_path, agent_timeout_s=10.0,
                        freshness_ttl_s=60.0, log_level="WARNING", observe_on_startup=False)
    database = open_database(settings.database_path)
    app = create_authenticated_app(settings, database)
    with AuthenticatedTestClient(app, base_url=f"http://{ENDPOINT}:8080",
                    client=(OPERATOR, 44321)) as client:
        yield _Wired(client, kernel, sysfs, timers, app)
    database.close()
    agent.shutdown()
    agent.server_close()
    helper.shutdown()
    helper.server_close()


@pytest.fixture
def held(wired) -> Any:
    """A guarded change, written, verified, and waiting on the connection."""
    wired.observe()
    wired.adopt("eth0")
    wired.drift("1400")
    run = wired.plan("eth0")
    assert wired.confirm(run).status_code == 200
    applied = wired.client.post(f"{API}/runs/{run['run_id']}/apply")
    assert applied.status_code == 200, applied.text
    assert applied.json()["state"] == "guarded", applied.json()
    wired.run_id = run["run_id"]
    return wired


# ---------------------------------------------------------------------------- the preview


def test_the_preview_says_the_guarded_path_is_the_only_write_path(wired):
    wired.observe()
    wired.adopt("eth0")
    wired.drift("1400")
    preview = wired.plan("eth0")["preview"]

    assert preview["how"]["eligibility"] == "guarded"
    assert preview["how"]["blockers"] == ["target_is_management_path"]
    assert preview["protection"]["management_path"] == "on_management_path"
    assert preview["guard"]["availability"] == "available"
    assert preview["guard"]["armed"] is False
    assert preview["guard"]["unmet"] == []
    assert preview["guard"]["window_s"] > 0
    assert preview["confirmation"]["method"] == "typed"
    assert preview["confirmation"]["token_issued"] is False


def test_the_openapi_document_offers_no_way_to_name_a_path_a_command_or_a_deadline(wired):
    """Read out of the published contract, because that is what a caller reads.

    The guard's request model has exactly one field. There is nowhere on this surface to
    put a backup interface, a second channel, a probe target, a route, a command, an argv or
    a window — which is what makes "the caller cannot choose the alternate path" a property
    of the API rather than a promise about the code behind it.
    """
    spec = wired.client.get("/openapi.json").json()
    keep = spec["components"]["schemas"]["KeepGuardedRunRequest"]
    assert set(keep["properties"]) == {"acknowledge"}
    assert keep.get("additionalProperties") is False

    confirm = spec["components"]["schemas"]["ConfirmRunRequest"]
    assert set(confirm["properties"]) == {
        "preview_id", "acknowledge", "acknowledge_object", "expected_preview_digest"}
    assert confirm.get("additionalProperties") is False

    # Read from what a caller may *send*, which is the request bodies and nothing else.
    # The prose on this API names an argv in order to say there is nowhere to put one, and
    # a response may perfectly well report a route it observed; what must not exist is a
    # property a request could carry.
    accepted: set[str] = set()
    for methods in spec["paths"].values():
        for operation in methods.values():
            body = (operation.get("requestBody") or {}).get("content") or {}
            for media in body.values():
                ref = (media.get("schema") or {}).get("$ref")
                if not ref:
                    continue
                model = spec["components"]["schemas"][ref.rsplit("/", 1)[-1]]
                accepted |= set(model.get("properties") or {})

    assert accepted == {
        # adopt / release / revise / adopt-runtime / create-run
        "expected_intent_id", "expected_version", "fields", "operation",
        # confirm / apply / keep / recovery
        "preview_id", "acknowledge", "acknowledge_object", "expected_preview_digest",
        "note", "expected_recovery_reason",
    }
    for forbidden in ("backup_interface", "second_path", "probe_target", "watchdog_command",
                      "rollback_command", "timeout_action", "argv", "shell", "command",
                      "script", "route", "gateway", "peer_address", "deadline", "window_s",
                      "guard_id", "force", "extend", "mtu", "interface", "object_id"):
        assert forbidden not in accepted, forbidden


# ------------------------------------------------------------------------- confirming


def test_confirming_a_guarded_plan_needs_the_object_name_and_records_what_was_typed(wired):
    wired.observe()
    wired.adopt("eth0")
    wired.drift("1400")
    run = wired.plan("eth0")

    missing = wired.confirm(run, acknowledge_object=None)
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "confirmation_object_required"

    wrong = wired.confirm(run, acknowledge_object="eth1")
    assert wrong.status_code == 409
    assert wrong.json()["error"]["code"] == "confirmation_object_mismatch"

    ok = wired.confirm(run)
    assert ok.status_code == 200
    confirmation = ok.json()["confirmation"]
    assert confirmation["method"] == "typed"
    assert confirmation["typed_statement"] == "eth0"
    assert "token" not in str(confirmation)


def test_confirm_still_refuses_a_body_naming_anything_it_does_not_declare(wired):
    wired.observe()
    wired.adopt("eth0")
    wired.drift("1400")
    run = wired.plan("eth0")
    for extra in ({"mtu": 9000}, {"desired": 9000}, {"command": "ip"}, {"argv": ["ip"]},
                  {"object_id": "obj"}, {"interface": "eth1"}, {"window_s": 3600},
                  {"backup_interface": "eth1"}, {"second_path": "tailscale0"}):
        body = {"preview_id": run["preview"]["preview_id"], "acknowledge": True,
                "acknowledge_object": "eth0", **extra}
        response = wired.client.post(f"{API}/runs/{run['run_id']}/confirm", json=body)
        assert response.status_code == 422, (extra, response.text)


# ------------------------------------------------------------------------------ applying


def test_the_apply_response_publishes_the_guard_that_is_holding(held):
    body = held.client.get(f"{API}/runs/{held.run_id}").json()

    assert body["state"] == "guarded"
    assert body["host_effect"] == "written"
    assert body["change"]["mutation"]["outcome"] == "written"
    guard = body["guard"]
    assert guard["phase"] == "armed"
    assert guard["holder_id"]
    assert guard["expires_at"]
    assert guard["restores_value"] == 1400
    assert guard["window_lapsed"] is False
    assert guard["kept_at"] is None and guard["settled_at"] is None
    assert held.mtu() == 1500


def _without_clock(body: Any) -> Any:
    """The same document with the "as of now" stamps removed.

    Every read in this API carries when it was answered, so two reads differ in that one
    field by construction. What must not differ is anything else — and in particular
    nothing about the guard, which is the read that "is it fine now?" would otherwise turn
    into a write.
    """
    if isinstance(body, dict):
        return {k: _without_clock(v) for k, v in body.items() if k != "as_of"}
    if isinstance(body, list):
        return [_without_clock(v) for v in body]
    return body


def test_reading_a_guarded_run_settles_nothing_and_is_byte_identical(held):
    """"Is it fine now?" is exactly the read that must not quietly become a write."""
    first = held.client.get(f"{API}/runs/{held.run_id}").json()
    second = held.client.get(f"{API}/runs/{held.run_id}").json()
    assert _without_clock(first) == _without_clock(second)
    assert first["guard"] == second["guard"]
    assert held.client.get(f"{API}/runs/{held.run_id}/preview").json()["guard"][
        "availability"] == "available"
    assert len(held.timers.live) == 1
    assert held.client.get(f"{API}/runs/{held.run_id}").json()["state"] == "guarded"


# ------------------------------------------------------------------------------- keeping


def test_keeping_over_the_proven_path_succeeds_and_releases_the_guard(held):
    response = held.client.post(f"{API}/runs/{held.run_id}/guard/keep",
                                json={"acknowledge": True})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "succeeded"
    assert body["guard"]["phase"] == "disarmed"
    assert body["guard"]["kept_at"] is not None
    assert body["guard"]["kept_evidence_id"] is not None
    assert body["guard"]["reversal_outcome"] is None
    assert held.timers.live == []
    assert held.mtu() == 1500


def test_automation_that_cannot_prove_its_path_cannot_keep_a_guarded_change(held):
    """The proof is the transport, so a loopback call cannot inherit a remote operator's.

    This is the same property that stops such a call applying a plan, at the one moment it
    matters most: an automation that "keeps" a change which has just cut the operator off
    would be the 2026-07-22 lockout with an audit trail.
    """
    local = held.loopback_client()
    response = local.post(f"{API}/runs/{held.run_id}/guard/keep", json={"acknowledge": True})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "guard_connection_not_proved"
    assert held.client.get(f"{API}/runs/{held.run_id}").json()["state"] == "guarded"
    assert len(held.timers.live) == 1


def test_keep_refuses_a_body_naming_anything_it_does_not_declare(held):
    for extra in ({"mtu": 9000}, {"object_id": "obj"}, {"guard_id": "grd_x"},
                  {"window_s": 3600}, {"extend": True}, {"command": "ip"},
                  {"argv": ["ip"]}, {"interface": "eth1"}, {"force": True},
                  {"rollback": False}, {"second_path": "tailscale0"}):
        response = held.client.post(f"{API}/runs/{held.run_id}/guard/keep",
                                    json={"acknowledge": True, **extra})
        assert response.status_code == 422, (extra, response.text)
    # And nothing moved.
    assert held.client.get(f"{API}/runs/{held.run_id}").json()["state"] == "guarded"


def test_an_unacknowledged_keep_is_refused_and_leaves_the_guard_holding(held):
    response = held.client.post(f"{API}/runs/{held.run_id}/guard/keep",
                                json={"acknowledge": False})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "keep_not_acknowledged"
    assert len(held.timers.live) == 1


def test_a_lapsed_window_is_reported_as_lapsed_before_anybody_collects_it(held):
    """Derived on read, and deliberately not allowed to change the phase.

    A deadline passing means the reversal has probably happened, and "probably" is not
    something this API states as a fact about a host. What turns it into an answer is asking
    the holder, which is a `POST`.
    """
    held.timers.fire()
    body = held.client.get(f"{API}/runs/{held.run_id}").json()
    assert body["state"] == "guarded"
    assert body["guard"]["phase"] == "armed"  # nobody has asked yet
    assert held.mtu() == 1400  # and yet the host is already back

    settled = held.client.post(f"{API}/runs/{held.run_id}/guard/keep",
                               json={"acknowledge": True})
    assert settled.status_code == 200
    body = settled.json()
    assert body["state"] == "rolled_back"
    assert body["guard"]["phase"] == "fired"
    assert body["guard"]["reversal_outcome"] == "written"
    assert body["change"]["result"] == "rolled_back"


def test_keeping_a_run_that_is_not_guarded_is_refused(held):
    held.client.post(f"{API}/runs/{held.run_id}/guard/keep", json={"acknowledge": True})
    again = held.client.post(f"{API}/runs/{held.run_id}/guard/keep", json={"acknowledge": True})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "run_not_guarded"


def test_a_guarded_run_cannot_be_cancelled_over_http(held):
    response = held.client.post(f"{API}/runs/{held.run_id}/cancel")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "run_not_cancellable"


def test_there_is_no_endpoint_that_arms_extends_or_disarms_a_guard(held):
    for path in (f"{API}/runs/{held.run_id}/guard",
                 f"{API}/runs/{held.run_id}/guard/arm",
                 f"{API}/runs/{held.run_id}/guard/disarm",
                 f"{API}/runs/{held.run_id}/guard/extend",
                 f"{API}/runs/{held.run_id}/guard/rollback",
                 f"{API}/guards", f"{API}/guards/{held.run_id}", f"{API}/watchdog"):
        assert held.client.post(path, json={}).status_code == 404, path
        assert held.client.get(path).status_code in (404, 405), path
