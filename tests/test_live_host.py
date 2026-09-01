"""The whole management path against this actual machine, read-only.

The agent runs as a real operating-system process started through its own entry point,
reaching the real ``/sys/class/net`` and the real ``ip``. The backend talks to it over a
real socket. What is asserted is only what the host genuinely says — this file contains no
expected interface list, because a test that requires a particular NIC to exist is a test
about the test host rather than about LocalPlane.

Nothing here mutates anything. :func:`test_nothing_about_the_host_changed` compares the
link configuration before and after the whole run and fails if a single field moved, and
:func:`test_nothing_about_the_providers_changed` does the same for the Docker daemon,
NetworkManager and tailscaled — the three systems this build consults.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from localplane.backend.agent_client import AgentClient, AgentError
from tests.conftest import AuthenticatedTestClient, create_authenticated_app
from localplane.backend.config import Settings
from localplane.backend.db.database import open_database

pytestmark = pytest.mark.live

SRC = str(Path(__file__).resolve().parents[1] / "src")


def link_configuration() -> list[dict]:
    """The mutable parts of every link, as the kernel reports them right now."""
    raw = subprocess.run(
        ["ip", "--json", "link", "show"],
        capture_output=True,
        text=True,
        check=True,
        env={"LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
    ).stdout
    return sorted(
        (
            {
                "ifname": entry.get("ifname"),
                "ifindex": entry.get("ifindex"),
                "flags": sorted(entry.get("flags", [])),
                "mtu": entry.get("mtu"),
                "operstate": entry.get("operstate"),
                "address": entry.get("address"),
                "master": entry.get("master"),
            }
            for entry in json.loads(raw)
        ),
        key=lambda e: e["ifindex"],
    )


def read_only(argv: list[str]) -> str | None:
    """Run one fixed read-only command, or return ``None`` if it is not on this host."""
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=20,
            env={"LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return completed.stdout if completed.returncode == 0 else None


def routing_state() -> dict:
    """Routes, rules and the resolver, as this machine has them right now.

    Captured with the kernel's own tools rather than through LocalPlane, so a change
    LocalPlane made would show up here even if LocalPlane's own view of it did not. The
    route lookup this build performs is a query; if it were anything else, this is where
    it would be visible.
    """
    state: dict = {}
    for key, argv in (
        ("routes_v4", ["ip", "--json", "route", "show", "table", "all"]),
        ("routes_v6", ["ip", "--json", "-6", "route", "show", "table", "all"]),
        ("rules_v4", ["ip", "--json", "rule", "show"]),
        ("rules_v6", ["ip", "--json", "-6", "rule", "show"]),
        ("addresses", ["ip", "--json", "addr", "show"]),
    ):
        raw = read_only(argv)
        state[key] = json.loads(raw) if raw else None
    try:
        state["resolv_conf"] = Path("/etc/resolv.conf").read_text()
    except OSError:
        state["resolv_conf"] = None
    for sysctl in ("net.ipv4.ip_forward", "net.ipv6.conf.all.forwarding"):
        path = Path("/proc/sys", *sysctl.split("."))
        try:
            state[sysctl] = path.read_text().strip()
        except OSError:
            state[sysctl] = None
    return state


def provider_state() -> dict:
    """What the three providers say about themselves, for a before-and-after comparison.

    Read with the providers' own clients rather than through LocalPlane, so that a change
    LocalPlane made would show up here even if LocalPlane's own view of it did not.
    """
    state: dict = {}

    networks = read_only(["docker", "network", "ls", "--format", "{{.ID}} {{.Name}} {{.Driver}}"])
    state["docker_networks"] = sorted(networks.splitlines()) if networks else None

    devices = read_only(
        ["nmcli", "--terse", "--fields", "DEVICE,STATE,CONNECTION", "device", "status"]
    )
    state["nm_devices"] = sorted(devices.splitlines()) if devices else None

    status = read_only(["tailscale", "status", "--json"])
    if status:
        parsed = json.loads(status)
        state["tailscale"] = {
            "BackendState": parsed.get("BackendState"),
            "TailscaleIPs": parsed.get("TailscaleIPs"),
            "TUN": parsed.get("TUN"),
        }
    else:
        state["tailscale"] = None
    return state


@pytest.fixture(scope="module")
def host_before() -> list[dict]:
    return link_configuration()


@pytest.fixture(scope="module")
def providers_before(host_before) -> dict:
    return provider_state()


@pytest.fixture(scope="module")
def routing_before(host_before) -> dict:
    return routing_state()


@pytest.fixture(scope="module")
def agent_process(tmp_path_factory, host_before) -> Iterator[Path]:
    """The agent, started the way it would be started for real."""
    run_dir = tmp_path_factory.mktemp("live-agent")
    socket_path = run_dir / "agent.sock"
    environment = {
        **os.environ,
        "PYTHONPATH": SRC,
        "LOCALPLANE_AGENT_SOCKET": str(socket_path),
        # Deliberately a path that is not there. The agent on *this* machine must not find
        # a privileged helper, because a helper reachable from a read-only live suite is a
        # helper that could write to this machine's interfaces. The real write happens in a
        # disposable namespace, in `test_live_write.py`, and nowhere else.
        "LOCALPLANE_HELPER_SOCKET": str(run_dir / "helper-absent.sock"),
        "LOCALPLANE_LOG_LEVEL": "WARNING",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "localplane.agent"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if socket_path.exists():
            break
        if process.poll() is not None:
            raise RuntimeError(f"agent exited early: {process.communicate()[1].decode()}")
        time.sleep(0.05)
    else:
        process.kill()
        raise RuntimeError("agent did not create its socket")

    yield socket_path

    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


@pytest.fixture(scope="module")
def live_client(tmp_path_factory, agent_process: Path) -> Iterator[TestClient]:
    settings = Settings(
        database_path=tmp_path_factory.mktemp("live-store") / "localplane.db",
        agent_socket=agent_process,
        agent_timeout_s=30,
        freshness_ttl_s=60,
        log_level="WARNING",
        observe_on_startup=True,
    )
    database = open_database(settings.database_path)
    with AuthenticatedTestClient(create_authenticated_app(settings, database)) as client:
        yield client
    database.close()


# ------------------------------------------------------------------------- the real path


def test_the_agent_runs_as_its_own_process(agent_process: Path):
    hello = AgentClient(agent_process, timeout_s=30).hello()
    assert hello["agent"]["pid"] != os.getpid()
    assert hello["agent"]["transport"] == "af_unix"
    assert hello["agent"]["mutating_methods"] == [
        "docker.container_lifecycle",
        "network.arm_mtu_guard",
        "network.set_interface_mtu",
    ]
    # And it holds no privilege of its own: the write lives behind a socket, in a process
    # this suite deliberately does not start on this machine.
    assert hello["agent"]["privilege"] == ("root" if os.geteuid() == 0 else "unprivileged")


def test_the_agent_identifies_this_host(agent_process: Path):
    host = AgentClient(agent_process, timeout_s=30).identify_host()["host"]
    assert host["host_id"].startswith("host_")
    assert host["hostname"] == os.uname().nodename
    assert host["kernel_release"] == os.uname().release
    assert host["architecture"] == os.uname().machine


def test_the_agent_reports_capabilities_it_actually_probed(agent_process: Path):
    capabilities = {
        c["capability"]: c for c in AgentClient(agent_process, timeout_s=30).list_capabilities()
    }
    network = capabilities["network.observe"]
    assert network["status"] in {"available", "degraded"}
    assert network["detail"]["methods"]["sysfs"] == "ok"
    assert network["detail"]["interfaces_visible"] == len(os.listdir("/sys/class/net"))


def test_the_backend_owns_the_real_observation(live_client: TestClient):
    body = live_client.get("/api/v1/network/interfaces").json()
    assert body["last_sweep"]["status"] in {"ok", "partial"}
    assert body["count"] == body["last_sweep"]["object_count"]
    assert body["count"] > 0


def test_every_interface_the_kernel_lists_is_represented(live_client: TestClient):
    from_kernel = {
        entry
        for entry in os.listdir("/sys/class/net")
        if Path("/sys/class/net", entry, "ifindex").exists()
    }
    from_api = {i["name"] for i in live_client.get("/api/v1/network/interfaces").json()["interfaces"]}
    assert from_api == from_kernel


def test_the_observation_matches_what_the_kernel_says_right_now(live_client: TestClient):
    """Assert against sysfs directly, not against another LocalPlane read of it."""
    for interface in live_client.get("/api/v1/network/interfaces").json()["interfaces"]:
        base = Path("/sys/class/net", interface["name"])
        if not base.exists():
            continue  # a veth can legitimately disappear mid-test
        assert interface["link"]["ifindex"] == int((base / "ifindex").read_text())
        assert interface["link"]["mtu"] == int((base / "mtu").read_text())
        expected_admin_up = bool(int((base / "flags").read_text().strip(), 16) & 0x1)
        assert interface["link"]["admin_up"] is expected_admin_up


def test_unknown_values_from_the_real_kernel_stay_unknown(live_client: TestClient):
    """``speed`` reads -1 and ``duplex`` reads 'unknown' on a link with no carrier."""
    checked = 0
    for interface in live_client.get("/api/v1/network/interfaces").json()["interfaces"]:
        base = Path("/sys/class/net", interface["name"])
        try:
            raw_speed = (base / "speed").read_text().strip()
        except OSError:
            assert interface["link"]["speed_mbps"] is None
            checked += 1
            continue
        if raw_speed == "-1":
            assert interface["link"]["speed_mbps"] is None
            checked += 1
    assert checked > 0, "this host offered no unknown speed to check"


def test_an_interface_that_does_not_exist_is_reported_missing_not_invented(
    live_client: TestClient,
):
    body = live_client.post(
        "/api/v1/network/observations/refresh",
        params={"names": ["lpghost0"]},
    ).json()
    assert body["missing"] == ["lpghost0"]
    assert body["object_count"] == 0
    names = {i["name"] for i in live_client.get("/api/v1/network/interfaces").json()["interfaces"]}
    assert "lpghost0" not in names


def test_ethernet_interfaces_are_recognised_and_identified_by_hardware(live_client: TestClient):
    """eth0 if this host has one; whatever ethernet it does have otherwise."""
    interfaces = live_client.get("/api/v1/network/interfaces").json()["interfaces"]
    ethernet = [i for i in interfaces if i["interface_kind"] == "ethernet"]
    if not ethernet:
        pytest.skip("this host has no ethernet interface")
    for interface in ethernet:
        assert interface["identity"]["basis"] in {"permanent_mac", "device_path", "kernel_name"}
        assert interface["health"]["state"] in {"healthy", "degraded", "inactive", "unknown"}
        assert interface["health"]["reason"]
    preferred = next((i for i in ethernet if i["name"] == "eth0"), None)
    if preferred is not None:
        assert preferred["identity"]["basis"] == "permanent_mac"
        assert preferred["link"]["is_physical"] is True


def test_evidence_from_the_real_host_is_retrievable(live_client: TestClient):
    interfaces = live_client.get("/api/v1/network/interfaces").json()["interfaces"]
    body = live_client.get(
        f"/api/v1/network/interfaces/{interfaces[0]['object_id']}/evidence"
    ).json()
    assert body["evidence"]["sysfs_path"].startswith("/sys/class/net/")
    assert body["evidence"]["commands"][0]["argv"][0] == "ip"


def test_observing_twice_keeps_the_same_objects(live_client: TestClient):
    first = live_client.post("/api/v1/network/observations/refresh").json()
    before = {i["object_id"] for i in live_client.get("/api/v1/network/interfaces").json()["interfaces"]}
    second = live_client.post("/api/v1/network/observations/refresh").json()
    after = {i["object_id"] for i in live_client.get("/api/v1/network/interfaces").json()["interfaces"]}
    assert first["sweep_id"] != second["sweep_id"]
    assert before == after


# ------------------------------------------------------- management, still read-only


def default_route_device() -> str | None:
    """The interface the default route leaves by, if there is one."""
    raw = subprocess.run(
        ["ip", "--json", "route", "show", "default"],
        capture_output=True,
        text=True,
        check=True,
        env={"LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
    ).stdout
    routes = json.loads(raw or "[]")
    return routes[0].get("dev") if routes else None


def adoptable_interface(live_client: TestClient) -> dict | None:
    """An interface on this host that LocalPlane would offer to adopt.

    Chosen through the eligibility LocalPlane publishes, not by guessing: anything another
    system on this machine is demonstrably running is excluded because LocalPlane says it
    is excluded, which makes this helper an exercise of the contract rather than a
    duplicate of it.

    The device carrying the default route is passed over on top of that. Nothing here
    writes to the host, so adopting it would be harmless — but the management path is the
    one thing on this machine that must never become a fixture out of convenience, and the
    habit is worth keeping even where the risk is zero.
    """
    live_client.post("/api/v1/network/observations/refresh")
    uplink = default_route_device()
    candidates = [
        interface
        for interface in live_client.get("/api/v1/network/interfaces").json()["interfaces"]
        if interface["ownership"]["adoption"]["eligible"]
        and interface["name"] != uplink
        and interface["link"]["admin_up"] is not None
        and interface["link"]["mtu"] is not None
    ]
    return candidates[0] if candidates else None


def test_the_real_host_offers_something_to_adopt_and_something_it_will_not(
    live_client: TestClient,
):
    """Loopback exists on every Linux host, and it is never a management candidate."""
    interfaces = live_client.get("/api/v1/network/interfaces").json()["interfaces"]
    by_name = {i["name"]: i for i in interfaces}
    assert by_name["lo"]["management"] == {"state": "observe_only", "reason": "loopback"}

    response = live_client.post(
        f"/api/v1/network/interfaces/{by_name['lo']['object_id']}/adopt"
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "object_observe_only"


def test_adopting_a_real_interface_records_what_the_kernel_says_right_now(
    live_client: TestClient,
):
    """Read the intent LocalPlane wrote, then read sysfs. They have to be the same values."""
    interface = adoptable_interface(live_client)
    if interface is None:
        pytest.skip("this host offers no adoptable interface outside the management path")

    body = live_client.post(
        f"/api/v1/network/interfaces/{interface['object_id']}/adopt"
    ).json()
    assert body["host_mutated"] is False
    assert body["host_effect"] == "none"
    assert body["to_state"] == "managed"

    base = Path("/sys/class/net", interface["name"])
    intended = {f["field"]: f["value"] for f in body["intent"]["controlled_fields"]}
    assert set(intended) == {"admin_up", "mtu"}
    assert intended["mtu"] == int((base / "mtu").read_text())
    assert intended["admin_up"] is bool(int((base / "flags").read_text().strip(), 16) & 0x1)

    # Nothing about the machine moved, so it agrees with what was just recorded.
    assert body["reconciliation"]["state"] == "in_sync"
    live_client.post("/api/v1/network/observations/refresh")
    again = live_client.get(
        f"/api/v1/network/interfaces/{interface['object_id']}/reconciliation"
    ).json()
    assert again["reconciliation"]["state"] == "in_sync"
    assert live_client.get("/api/v1/findings").json()["count"] == 0


def test_releasing_a_real_interface_leaves_it_exactly_where_it_was(live_client: TestClient):
    interface = adoptable_interface(live_client)
    if interface is None:
        pytest.skip("this host offers no adoptable interface outside the management path")

    object_id = interface["object_id"]
    if live_client.get(f"/api/v1/network/interfaces/{object_id}/intent").status_code == 404:
        live_client.post(f"/api/v1/network/interfaces/{object_id}/adopt")

    before = subprocess.run(
        ["ip", "--json", "link", "show", interface["name"]],
        capture_output=True, text=True, check=True,
        env={"LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
    ).stdout

    body = live_client.post(f"/api/v1/network/interfaces/{object_id}/release").json()
    assert body["to_state"] == "observed"
    assert body["host_mutated"] is False
    assert body["reconciliation"] is None

    after = subprocess.run(
        ["ip", "--json", "link", "show", interface["name"]],
        capture_output=True, text=True, check=True,
        env={"LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
    ).stdout
    assert json.loads(after) == json.loads(before)

    current = live_client.get(f"/api/v1/network/interfaces/{object_id}").json()
    assert current["management"]["state"] == "observed"
    assert current["reconciliation"] is None
    assert current["intent"] is None

    # The intent is gone from the object and kept in the record.
    history = live_client.get(
        f"/api/v1/network/interfaces/{object_id}/intent/history"
    ).json()
    assert history["active_intent_id"] is None
    assert history["count"] >= 1
    assert "release" in {t["transition"] for t in history["transitions"]}


def link_of(name: str) -> dict:
    raw = subprocess.run(
        ["ip", "--json", "link", "show", name],
        capture_output=True, text=True, check=True,
        env={"LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
    ).stdout
    return json.loads(raw)[0]


@pytest.fixture(scope="module")
def revisable_interface(live_client: TestClient) -> dict | None:
    """One interface on this host, adopted once, for the revision tests to share.

    Chosen through the eligibility LocalPlane publishes, exactly as ``adoptable_interface``
    does, and then held for the rest of the module: adopting it makes it ineligible, so a
    per-test lookup would silently move to a different interface after the first revision
    and the version chain being asserted would belong to nobody in particular.
    """
    interface = adoptable_interface(live_client)
    if interface is None:
        return None
    live_client.post(f"/api/v1/network/interfaces/{interface['object_id']}/adopt")
    return interface


def test_revising_a_real_interface_moves_the_intent_and_not_the_machine(
    live_client: TestClient, revisable_interface: dict | None
):
    """The whole point of intent revision, against this actual machine.

    The intent is deliberately revised to an MTU the interface does not have, which is the
    case where a bug would show: if anything in this path could write, this is where it
    would write. The kernel is read directly before and after — not through LocalPlane —
    so a change LocalPlane made would show up here even if LocalPlane's own view of it did
    not.
    """
    interface = revisable_interface
    if interface is None:
        pytest.skip("this host offers no adoptable interface outside the management path")

    object_id = interface["object_id"]
    active = live_client.get(f"/api/v1/network/interfaces/{object_id}/intent").json()
    intended_mtu = {f["field"]: f["value"] for f in active["controlled_fields"]}["mtu"]

    before_link = link_of(interface["name"])
    before_mtu = (Path("/sys/class/net", interface["name"]) / "mtu").read_text()

    body = live_client.post(
        f"/api/v1/network/interfaces/{object_id}/intent/revise",
        json={
            "expected_intent_id": active["intent_id"],
            "expected_version": active["version"],
            # A value the link does not have, so agreement cannot be reached by accident.
            "fields": {"mtu": intended_mtu + 100},
        },
    ).json()

    assert body["host_mutated"] is False
    assert body["host_effect"] == "none"
    assert body["kind"] == "revise"
    assert body["management"]["state"] == "managed"
    assert body["intent"]["version"] == active["version"] + 1
    assert body["intent"]["supersedes"] == active["intent_id"]
    assert body["reconciliation"]["state"] == "drifted"

    # The machine is exactly as it was, read from the kernel rather than from LocalPlane.
    assert link_of(interface["name"]) == before_link
    assert (Path("/sys/class/net", interface["name"]) / "mtu").read_text() == before_mtu
    assert link_of(interface["name"])["mtu"] == intended_mtu


def test_adopting_a_real_runtime_as_intent_settles_the_drift_it_did_not_cause(
    live_client: TestClient, revisable_interface: dict | None
):
    """The second truthful answer to drift, and it applies nothing.

    The drift this closes was manufactured by the revision above, so nothing about the host
    ever needed putting right — and the finding says so. `intent_revised` is the resolution,
    and no observation is named as having proved it, because none did.
    """
    interface = revisable_interface
    if interface is None:
        pytest.skip("this host offers no adoptable interface outside the management path")

    object_id = interface["object_id"]
    active = live_client.get(f"/api/v1/network/interfaces/{object_id}/intent").json()
    reconciliation = live_client.get(
        f"/api/v1/network/interfaces/{object_id}/reconciliation"
    ).json()["reconciliation"]
    assert reconciliation["state"] == "drifted"

    open_drift = [
        f
        for f in live_client.get(f"/api/v1/findings?object_id={object_id}").json()["findings"]
        if f["finding_type"] == "network.interface.drift"
    ]
    before_link = link_of(interface["name"])

    body = live_client.post(
        f"/api/v1/network/interfaces/{object_id}/intent/adopt-runtime",
        json={"expected_intent_id": active["intent_id"]},
    ).json()

    assert body["host_mutated"] is False
    assert body["kind"] == "adopt_runtime"
    assert body["reconciliation"]["state"] == "in_sync"
    assert body["carried_forward"] == []
    # Every value it retained is one the kernel already had.
    base = Path("/sys/class/net", interface["name"])
    intended = {f["field"]: f["value"] for f in body["intent"]["controlled_fields"]}
    assert intended["mtu"] == int((base / "mtu").read_text())
    assert intended["admin_up"] is bool(int((base / "flags").read_text().strip(), 16) & 0x1)

    for finding in open_drift:
        resolved = live_client.get(f"/api/v1/findings/{finding['finding_id']}").json()
        assert resolved["status"] == "resolved"
        assert resolved["resolution"] == "intent_revised"
        assert resolved["resolved_by_observation_id"] is None

    assert link_of(interface["name"]) == before_link


def test_the_real_version_chain_reads_end_to_end(
    live_client: TestClient, revisable_interface: dict | None
):
    interface = revisable_interface
    if interface is None:
        pytest.skip("this host offers no adoptable interface outside the management path")

    history = live_client.get(
        f"/api/v1/network/interfaces/{interface['object_id']}/intent/history"
    ).json()
    versions = [i["version"] for i in history["intents"]]
    assert versions == sorted(versions, reverse=True)
    assert history["intents"][-1]["supersedes"] is None
    for newer, older in zip(history["intents"], history["intents"][1:]):
        assert newer["supersedes"] == older["intent_id"]
    assert {i["origin"] for i in history["intents"]} <= {"adopt", "revise", "adopt_runtime"}
    # Exactly one version is in force, and it is the one the object points at.
    active = [i for i in history["intents"] if i["active"]]
    assert [i["intent_id"] for i in active] == [history["active_intent_id"]]
    # Every revision states, durably, that it did nothing to the host.
    assert all(
        i["revision"]["host_effect"] == "none"
        for i in history["intents"]
        if i["revision"] is not None
    )


def test_a_revised_interface_can_still_be_released(
    live_client: TestClient, revisable_interface: dict | None
):
    interface = revisable_interface
    if interface is None:
        pytest.skip("this host offers no adoptable interface outside the management path")

    object_id = interface["object_id"]
    before_link = link_of(interface["name"])
    body = live_client.post(f"/api/v1/network/interfaces/{object_id}/release").json()

    assert body["to_state"] == "observed"
    assert body["host_mutated"] is False
    assert link_of(interface["name"]) == before_link
    history = live_client.get(
        f"/api/v1/network/interfaces/{object_id}/intent/history"
    ).json()
    assert history["active_intent_id"] is None
    assert history["count"] >= 2  # every version this file wrote is still there


# ---------------------------------------------------------------------- ownership


def ownership_by_name(live_client: TestClient) -> dict:
    return {
        i["name"]: i["ownership"]
        for i in live_client.get("/api/v1/network/interfaces").json()["interfaces"]
    }


def test_the_agent_probed_the_providers_on_this_host(agent_process: Path):
    capability = {
        c["capability"]: c for c in AgentClient(agent_process, timeout_s=30).list_capabilities()
    }["network.providers.observe"]
    assert capability["mutating"] is False
    assert capability["status"] in {"available", "degraded", "unavailable"}
    assert set(capability["detail"]["providers"]) == {"docker", "networkmanager", "tailscale"}
    for status in capability["detail"]["providers"].values():
        assert status in {"ok", "absent", "unavailable", "error"}


def test_every_interface_gets_an_ownership_answer(live_client: TestClient):
    """Including the ones that are unknown, which say which source left them that way."""
    for name, ownership in ownership_by_name(live_client).items():
        assert ownership["state"] in {"attributed", "unknown"}
        assert ownership["reason"]
        assert isinstance(ownership["adoption"]["eligible"], bool)
        if ownership["state"] == "unknown":
            assert ownership["created_by"] is None and ownership["configured_by"] is None
            assert ownership["reason"] in {"evidence_incomplete", "no_provider_claim"}
            if ownership["reason"] == "evidence_incomplete":
                assert ownership["evidence_gaps"], f"{name} is unknown for no stated reason"


def test_a_real_docker_bridge_is_attributed_to_a_real_docker_network(live_client: TestClient):
    """Matched against the daemon's own records, and checked against them here."""
    owned = {
        name: ownership
        for name, ownership in ownership_by_name(live_client).items()
        if (ownership["configured_by"] or {}).get("owner", {}).get("provider") == "docker"
    }
    if not owned:
        pytest.skip("this host has no Docker-owned bridge")

    declared = read_only(["docker", "network", "ls", "--format", "{{.ID}} {{.Name}}"])
    assert declared is not None, "a bridge was attributed to Docker with no Docker to ask"
    networks = {line.split()[0]: line.split()[1] for line in declared.splitlines()}

    for name, ownership in owned.items():
        owner = ownership["configured_by"]["owner"]
        # Docker's `ls` prints the short id; the claim carries the full one.
        assert owner["instance"][:12] in networks, f"{name} names a network Docker does not have"
        assert networks[owner["instance"][:12]] == owner["label"]
        assert ownership["configured_by"]["confidence"] in {"confirmed", "corroborated"}
        assert ownership["adoption"]["eligible"] is False
        assert ownership["adoption"]["reason"] == "externally_configured"


def test_a_docker_owned_bridge_cannot_be_adopted_on_this_host(live_client: TestClient):
    interfaces = live_client.get("/api/v1/network/interfaces").json()["interfaces"]
    owned = [
        i for i in interfaces
        if (i["ownership"]["configured_by"] or {}).get("owner", {}).get("provider") == "docker"
    ]
    if not owned:
        pytest.skip("this host has no Docker-owned bridge")

    interface = owned[0]
    # Still an ordinary management candidate: ownership did not move it to observe_only.
    assert interface["management"]["state"] == "observed"
    response = live_client.post(f"/api/v1/network/interfaces/{interface['object_id']}/adopt")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "externally_configured"
    assert response.json()["error"]["detail"]["owner"]["provider"] == "docker"


def test_a_networkmanager_driven_interface_is_configured_not_created(live_client: TestClient):
    driven = {
        name: ownership
        for name, ownership in ownership_by_name(live_client).items()
        if (ownership["configured_by"] or {}).get("owner", {}).get("provider")
        == "networkmanager"
    }
    if not driven:
        pytest.skip("no interface on this host has an active NetworkManager profile")

    devices = read_only(
        ["nmcli", "--terse", "--fields", "DEVICE,STATE,CON-UUID", "device", "status"]
    )
    states = {
        line.split(":")[0]: line.split(":")[1:] for line in (devices or "").splitlines()
    }
    for name, ownership in driven.items():
        state, uuid = states[name][0], states[name][1]
        assert state == "connected", f"{name} is claimed by NetworkManager while {state}"
        assert ownership["configured_by"]["owner"]["instance"] == uuid
        # NetworkManager configures; it did not make the device.
        created = ownership["created_by"]
        assert created is None or created["owner"]["provider"] == "kernel"
        assert ownership["adoption"]["eligible"] is False


def test_a_tunnel_is_not_attributed_to_tailscale_by_its_name(live_client: TestClient):
    """On a host whose tailscaled is stopped, ``tailscale0`` must come back unknown."""
    ownership = ownership_by_name(live_client)
    tunnels = {
        i["name"]
        for i in live_client.get("/api/v1/network/interfaces").json()["interfaces"]
        if i["interface_kind"] == "tunnel"
    }
    if not tunnels:
        pytest.skip("this host has no tunnel interface")

    status = read_only(["tailscale", "status", "--json"])
    running = bool(status) and json.loads(status).get("BackendState") == "Running"
    for name in tunnels:
        claim = ownership[name]["configured_by"]
        if claim is None:
            continue
        assert running, f"{name} was attributed to {claim['owner']['provider']} with no daemon"
        assert claim["owner"]["provider"] == "tailscale"

    if not running:
        # The name says Tailscale and LocalPlane says it does not know. That is the point.
        named = [t for t in tunnels if t.startswith("tailscale")]
        for name in named:
            assert ownership[name]["state"] == "unknown"
            assert "tailscale.status" in ownership[name]["evidence_gaps"]


def test_the_provenance_of_a_real_interface_carries_its_evidence(live_client: TestClient):
    interfaces = live_client.get("/api/v1/network/interfaces").json()["interfaces"]
    attributed = [i for i in interfaces if i["ownership"]["state"] == "attributed"]
    assert attributed, "nothing on this host could be attributed at all"

    body = live_client.get(
        f"/api/v1/network/interfaces/{attributed[0]['object_id']}/provenance"
    ).json()
    assert body["claims"]
    assert all(claim["evidence"] for claim in body["claims"])
    assert {s["source"] for s in body["sources"]} == {
        "kernel.interface", "docker.networks", "networkmanager.devices", "tailscale.status"
    }
    assert body["observation"]["observation_id"].startswith("obs_")


def test_the_real_sweep_reports_what_each_provider_did(live_client: TestClient):
    body = live_client.post("/api/v1/network/observations/refresh").json()
    reported = {p["provider"]: p for p in body["providers"]}
    assert set(reported) == {"docker", "networkmanager", "tailscale"}
    for provider in reported.values():
        assert provider["status"] in {"ok", "absent", "unavailable", "error"}
        if provider["status"] != "ok":
            assert provider["reason"]


# ---------------------------------------------------------------------------- runs


@pytest.fixture(scope="module")
def run_interface(live_client: TestClient) -> dict | None:
    """One real interface, managed and genuinely drifted, for the Run tests to share.

    The drift is manufactured the only way this build can manufacture any: by revising the
    retained intent to an MTU the link does not carry. Nothing is written to the machine —
    what moves is LocalPlane's own record of what it wants — and the result is exactly the
    condition a reconciliation Run exists to plan against.
    """
    live_client.post("/api/v1/network/observations/refresh")
    interface = adoptable_interface(live_client)
    if interface is None:
        return None
    object_id = interface["object_id"]
    live_client.post(f"/api/v1/network/interfaces/{object_id}/adopt")
    active = live_client.get(f"/api/v1/network/interfaces/{object_id}/intent").json()
    intended = {f["field"]: f["value"] for f in active["controlled_fields"]}["mtu"]
    live_client.post(
        f"/api/v1/network/interfaces/{object_id}/intent/revise",
        json={
            "expected_intent_id": active["intent_id"],
            "fields": {"mtu": intended + 100},
        },
    )
    return {**interface, "runtime_mtu": intended, "intended_mtu": intended + 100}


def test_planning_a_real_reconciliation_writes_nothing_to_this_machine(
    live_client: TestClient, run_interface: dict | None
):
    """The whole management path against this actual machine, and the kernel is read directly.

    A plan is made for the one thing this build could conceivably want to do to a link, and
    the interface's own state is read from the kernel before and after — not through
    LocalPlane — so a write LocalPlane made would show up here even if LocalPlane's own view
    of it did not.
    """
    interface = run_interface
    if interface is None:
        pytest.skip("this host offers no adoptable interface outside the management path")

    before_link = link_of(interface["name"])
    before_mtu = (Path("/sys/class/net", interface["name"]) / "mtu").read_text()

    live_client.post("/api/v1/network/observations/refresh")
    response = live_client.post(
        "/api/v1/runs",
        json={
            "operation": {
                "type": "network.interface.reconcile_mtu",
                "object_id": interface["object_id"],
            }
        },
    )
    assert response.status_code == 201, response.json()
    body = response.json()

    assert body["state"] == "preview"
    assert body["host_mutated"] is False
    assert body["host_effect"] == "none"
    assert body["change_created"] is False
    assert body["preview"]["what"]["current"] == interface["runtime_mtu"]
    assert body["preview"]["what"]["desired"] == interface["intended_mtu"]

    # The machine is exactly as it was, read from the kernel rather than from LocalPlane.
    assert link_of(interface["name"]) == before_link
    assert (Path("/sys/class/net", interface["name"]) / "mtu").read_text() == before_mtu
    assert link_of(interface["name"])["mtu"] == interface["runtime_mtu"]


def test_a_real_preview_claims_nothing_it_did_not_establish(
    live_client: TestClient, run_interface: dict | None
):
    """Every stage after the write boundary is reported as what it is: not reached."""
    if run_interface is None:
        pytest.skip("this host offers no adoptable interface outside the management path")

    runs = live_client.get("/api/v1/runs").json()["runs"]
    assert runs, "the planning test should have left a run behind"
    preview = live_client.get(f"/api/v1/runs/{runs[0]['run_id']}/preview").json()

    # The build can execute this operation; this machine cannot, and the plan says which.
    assert preview["how"]["availability"] == "available"
    assert preview["how"]["eligibility"] == "blocked"
    assert preview["how"]["provider"] == "linux.link"
    assert preview["how"]["capability_declared_by_agent"] is False
    assert "required_capability_undeclared" in preview["how"]["blockers"]
    assert preview["confirmation"]["token_issued"] is False
    assert preview["confirmation"]["satisfied"] is False
    assert preview["recovery"]["armed"] is False
    assert preview["recovery"]["guarantee"] == "none"
    assert preview["verification"]["executed"] is False
    # And on a machine with real routes, real daemons and a real operator session, whether
    # this interface carries the management path is still unknown — because nothing
    # LocalPlane observes answers it, and the alternative is a guess.
    assert preview["protection"]["management_path"] == "unknown"
    assert "management_path_unproven" in preview["how"]["blockers"]


def test_a_real_run_can_be_cancelled_and_leaves_the_machine_alone(
    live_client: TestClient, run_interface: dict | None
):
    interface = run_interface
    if interface is None:
        pytest.skip("this host offers no adoptable interface outside the management path")

    before_link = link_of(interface["name"])
    runs = live_client.get("/api/v1/runs?state=preview").json()["runs"]
    assert runs
    body = live_client.post(f"/api/v1/runs/{runs[0]['run_id']}/cancel").json()

    assert body["state"] == "cancelled"
    assert body["change_created"] is False
    assert body["host_mutated"] is False
    assert link_of(interface["name"]) == before_link
    # The plan it published is still there, exactly as it was published.
    preview = live_client.get(f"/api/v1/runs/{runs[0]['run_id']}/preview").json()
    assert preview["what"]["current"] == interface["runtime_mtu"]


def test_this_build_refuses_to_apply_a_real_run_on_this_machine(live_client: TestClient):
    """Fail closed on the real host: the write path exists and every gate here shuts it.

    The refusal is a typed 409 naming what is missing, not a 404 pretending the feature is
    absent — and the endpoints that never existed still do not exist.
    """
    runs = live_client.get("/api/v1/runs?state=all").json()["runs"]
    if runs:
        run_id = runs[0]["run_id"]
        refused = live_client.post(f"/api/v1/runs/{run_id}/apply")
        assert refused.status_code == 409
        # Which gate answers first depends on what the module did to this Run before now —
        # it may have been cancelled. Every member of this set is a refusal to write.
        assert refused.json()["error"]["code"] in {
            "preview_not_executable",
            "run_not_appliable",
            "preview_stale",
            "execution_blocked",
        }
        confirmed = live_client.post(
            f"/api/v1/runs/{run_id}/confirm",
            json={
                "preview_id": runs[0]["preview"]["preview_id"],
                "acknowledge": True,
            },
        )
        assert confirmed.status_code == 409
        assert confirmed.json()["error"]["code"] in {
            "preview_not_executable",
            "run_not_confirmable",
            "confirmation_method_unsupported",
            "preview_stale",
        }
    run_id = runs[0]["run_id"] if runs else "run_none"
    for path in (
        f"/api/v1/runs/{run_id}/execute",
        f"/api/v1/runs/{run_id}/arm",
        f"/api/v1/runs/{run_id}/verify",
        f"/api/v1/runs/{run_id}/rollback",
        "/api/v1/execute",
        "/api/v1/terminal",
        "/api/v1/helper",
    ):
        assert live_client.post(path, json={}).status_code == 404, path
    # And nothing was recorded as having been written to this machine.
    assert live_client.get("/api/v1/changes").json()["changes"] == []


# ------------------------------------------------------------------------------ safety


def test_the_agent_on_this_machine_cannot_reach_a_privileged_helper(agent_process: Path):
    """The kernel write and the guard that would reverse it both have nowhere to go here.

    The methods exist — the agent would forward a typed mutation to a privileged helper, and
    would hold a reversal to send through the same one — and there is no helper on this
    machine to forward either to. So the write capability probes ``unavailable``, **and the
    guard capability probes unavailable with it**, which is the load-bearing half: a host
    that cannot perform the write cannot undo it either, and a guard reported available
    there would be a plan published as guardable that would arm nothing.

    That is the state this whole live suite runs in, and it is what makes the suite
    read-only by construction rather than by care.
    """
    hello = AgentClient(agent_process, timeout_s=30).hello()
    assert hello["agent"]["mutating_methods"] == [
        "docker.container_lifecycle",
        "network.arm_mtu_guard",
        "network.set_interface_mtu",
    ]
    capabilities = {c["capability"]: c for c in hello["capabilities"]}
    mutating = {
        "network.interface.set_mtu",
        "network.interface.mtu_guard",
        "docker.container.lifecycle",
        "systemd.service.lifecycle",
    }
    assert {n for n, c in capabilities.items() if c["mutating"]} == mutating
    # The kernel write has nowhere to go on this machine: there is no privileged helper.
    assert capabilities["network.interface.set_mtu"]["status"] == "unavailable"
    assert capabilities["network.interface.set_mtu"]["reason"] == "helper_unavailable"
    # And the guard is unavailable *because* it is: it is that write, deferred.
    assert capabilities["network.interface.mtu_guard"]["status"] == "unavailable"
    assert capabilities["network.interface.mtu_guard"]["reason"] == (
        "reversal_capability_unavailable")
    for name, capability in capabilities.items():
        if name not in mutating:
            assert capability["mutating"] is False
    assert set(hello["agent"]["methods"]) == {
        "agent.hello",
        "capabilities.list",
        "host.identify",
        "network.observe_interfaces",
        "network.observe_providers",
        "network.observe_route",
        "network.set_interface_mtu",
        "network.arm_mtu_guard",
        "network.disarm_mtu_guard",
        "network.mtu_guard_status",
        "docker.observe_containers",
        "docker.container_logs",
        "docker.container_stats",
        "docker.container_lifecycle",
        "systemd.observe_units",
        "systemd.observe_unit",
        "systemd.resolve_agent_unit",
        "systemd.observe_lifecycle_context",
    }


def test_no_plan_against_this_machine_can_be_applied(live_client: TestClient):
    """Every gate that would have to pass, and on this machine none of them does.

    There is no helper, so the capability is undeclared; the suite calls over loopback, so
    the management path is unprovable and no object is proven off it. Both are reported as
    blockers, and an apply is refused rather than attempted.
    """
    assert live_client.get("/api/v1/changes").json()["changes"] == []
    assert live_client.get("/api/v1/management-path").json()["state"] == "unresolved"
    # And the endpoints that could write refuse an id that does not exist rather than
    # inventing one to write to.
    assert live_client.post("/api/v1/runs/run_nope/apply").status_code == 404
    assert live_client.post(
        "/api/v1/runs/run_nope/confirm", json={"preview_id": "prv_nope", "acknowledge": True}
    ).status_code == 404


def test_nothing_about_this_machines_routing_changed(
    live_client: TestClient, routing_before: dict
):
    """The one thing the route lookup touches, compared before and after.

    Routes, policy rules, addresses, the resolver and the forwarding sysctls, read with the
    kernel's own tools. A netlink route *lookup* leaves every one of them exactly as it
    found them, and this is what would notice if it did not.
    """
    live_client.post("/api/v1/management-path/observations/refresh")
    live_client.get("/api/v1/management-path")
    after, before = routing_state(), dict(routing_before)
    # The uplink's DHCP lease counters tick down while the suite runs — the host doing its
    # own job, not a change LocalPlane made. They are excluded here and nowhere else: every
    # other field of every address, and all of the routes, rules, resolver and forwarding
    # state, are compared exactly.
    after["addresses"] = _addresses_without_lease_counters(after["addresses"])
    before["addresses"] = _addresses_without_lease_counters(before["addresses"])
    assert after == before


def _addresses_without_lease_counters(entries):
    """Addresses with the two fields a running DHCP lease moves on its own removed."""
    if entries is None:
        return None
    stripped = []
    for entry in entries:
        copy = dict(entry)
        copy["addr_info"] = [
            {
                key: value
                for key, value in info.items()
                if key not in ("valid_life_time", "preferred_life_time")
            }
            for info in entry.get("addr_info", [])
        ]
        stripped.append(copy)
    return stripped


def test_nothing_about_the_host_changed(live_client: TestClient, host_before: list[dict]):
    """Runs after the whole module has exercised the real host. Nothing may have moved."""
    live_client.post("/api/v1/network/observations/refresh")
    assert link_configuration() == host_before


def test_nothing_about_the_providers_changed(live_client: TestClient, providers_before: dict):
    """The three systems LocalPlane now consults are exactly as they were.

    Consulting a daemon is not a neutral act by default — a client can create, connect,
    activate or restart things by accident. This asserts that a whole module's worth of
    ownership derivation left Docker's networks, NetworkManager's device states and
    tailscaled's backend state untouched.
    """
    live_client.post("/api/v1/network/observations/refresh")
    assert provider_state() == providers_before


# ------------------------------------------------------------------- the management path


def test_the_real_agent_answers_a_real_route_lookup(agent_process: Path):
    """rtnetlink, against this kernel, through the real agent over the real socket.

    Loopback is the one destination every Linux host routes, so this asserts against a fact
    rather than against this machine's uplink — and it checks the index the kernel gave
    against the index the kernel gives, not against a name.
    """
    answer = AgentClient(agent_process, timeout_s=30).observe_route("127.0.0.1")
    route = answer["route"]
    assert route["provider"] == "linux.route"
    assert route["method"] == "netlink_rtm_getroute"
    assert route["capability"] == "network.route.observe"
    assert route["status"] == "resolved"
    assert route["route"]["oif_index"] == socket.if_nametoindex("lo")
    assert route["route"]["family"] == "inet"
    # An index, and nothing resolved from it. The correlation is the backend's.
    assert "ifname" not in route["route"]


def test_the_real_route_lookup_refuses_anything_that_is_not_an_address(agent_process: Path):
    """No argv, no shell, no interface name, no route expression. One typed address."""
    client = AgentClient(agent_process, timeout_s=30)
    for value in ("eth0", "default", "192.0.2.1/24", "; ip link set eth0 down", "$(id)", ""):
        with pytest.raises(AgentError) as raised:
            client.observe_route(value)
        assert raised.value.code == "invalid_params", value


def test_the_real_route_lookup_takes_no_other_parameter(agent_process: Path):
    client = AgentClient(agent_process, timeout_s=30)
    for params in (
        {"destination": "127.0.0.1", "command": "ip"},
        {"destination": "127.0.0.1", "argv": ["ip", "route", "get", "127.0.0.1"]},
        {"destination": "127.0.0.1", "table": 254},
        {"destination": "127.0.0.1", "oif": "eth0"},
        {"destination": "127.0.0.1", "shell": True},
    ):
        with pytest.raises(AgentError) as raised:
            client.call("network.observe_route", params)
        assert raised.value.code == "unknown_field", params


def test_the_real_agent_declares_the_route_capability_as_read_only(agent_process: Path):
    capabilities = {
        c["capability"]: c for c in AgentClient(agent_process, timeout_s=30).list_capabilities()
    }
    route = capabilities["network.route.observe"]
    assert route["mutating"] is False
    assert route["status"] == "available"
    assert route["detail"]["method"] == "netlink_rtm_getroute"
    assert route["detail"]["probe_destination"] == "127.0.0.1"


def test_the_management_path_over_a_loopback_client_stays_unknown(live_client: TestClient):
    """This suite's own client is local, and local is never an operator.

    The strongest statement this file can make about the management path: on a real
    machine, with real routes, a real uplink and a real agent, LocalPlane still refuses to
    name a management path for a request that arrived over a transport that cannot prove
    one.
    """
    body = live_client.post("/api/v1/management-path/observations/refresh").json()
    assert body["host_mutated"] is False
    assert body["host_effect"] == "none"
    assert body["recorded"] is False
    path = body["management_path"]
    assert path["state"] == "unresolved"
    assert path["object_id"] is None
    assert path["evidence"] is None
    assert path["transport"]["usable"] is False
    assert sorted(path["missing_evidence"]) == ["route.observe", "session.peer"]


def test_no_real_interface_is_marked_clear_while_the_path_is_unresolved(
    live_client: TestClient,
):
    live_client.post("/api/v1/network/observations/refresh")
    interfaces = live_client.get("/api/v1/network/interfaces").json()["interfaces"]
    assert interfaces
    for interface in interfaces:
        body = live_client.get(
            f"/api/v1/network/interfaces/{interface['object_id']}/protection"
        ).json()
        assert body["status"] == "unknown", interface["name"]
        assert body["management_path"] == "unknown"
        assert body["unresolved"] == ["management_path"]
        assert body["implemented_reasons"] == ["management_path"]


def test_reading_the_real_management_path_writes_nothing(live_client: TestClient):
    def evidence_rows() -> int:
        return len(
            live_client.app.state.context.database.query(
                "SELECT * FROM management_path_observations"
            )
        )

    before = evidence_rows()
    for _ in range(3):
        assert live_client.get("/api/v1/management-path").json()["state"] == "unresolved"
    assert evidence_rows() == before == 0
