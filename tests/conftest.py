"""Fixtures.

The provider tests run against a sysfs tree built here rather than against the machine, so
that link states this host does not happen to have — a link with carrier but no operstate,
a device whose fields cannot be read — are exercised every run instead of only when the
hardware cooperates. The live tests in ``test_live_host.py`` cover the real thing.

An unreadable sysfs field is modelled by creating a *directory* where a file belongs.
Reading it raises ``IsADirectoryError``, an ``OSError``, which is the same failure class
the kernel produces when it answers ``EINVAL`` for ``carrier`` on a down link — the path
the provider actually has to handle.
"""

from __future__ import annotations

import base64
import http.server
import json
import os
import socketserver
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

import pytest
from fastapi.testclient import TestClient

from localplane.agent.providers.base import CommandFailure, CommandResult
from localplane.backend.app import create_app
from localplane.backend.auth import Authentication
from localplane.backend.config import Settings
from localplane.backend.db.database import Database

TEST_MASTER_SECRET = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
TEST_AUTHORIZATION = {"Authorization": f"Bearer {TEST_MASTER_SECRET}"}


class AuthenticatedTestClient(TestClient):
    """Existing API regressions run as an authenticated automation caller."""

    def __init__(self, *args, headers=None, **kwargs):
        merged = dict(TEST_AUTHORIZATION)
        if headers is not None:
            merged.update(headers)
        super().__init__(*args, headers=merged, **kwargs)


def create_authenticated_app(settings: Settings, database: Database):
    return create_app(
        settings,
        database,
        authentication=Authentication(
            TEST_MASTER_SECRET,
            bind_host=settings.bind_host,
            development_origin=settings.development_origin,
        ),
    )


# --------------------------------------------------------------------------- sysfs tree


def write_interface(
    sysfs_net: Path,
    name: str,
    *,
    ifindex: int,
    address: str | None = "aa:bb:cc:dd:ee:ff",
    addr_assign_type: str | None = "0",
    mtu: str = "1500",
    operstate: str = "down",
    carrier: str | None = "0",
    flags: str = "0x1003",
    arphrd: str = "1",
    speed: str | None = "-1",
    duplex: str | None = "unknown",
    carrier_changes: str = "0",
    devtype: str | None = None,
    device: str | None = None,
    subsystem: str | None = None,
    bridge: bool = False,
    wireless: bool = False,
    tun: bool = False,
    master: str | None = None,
    statistics: dict[str, str] | None = None,
    unreadable: Sequence[str] = (),
) -> Path:
    """Create one interface directory under a fake ``/sys/class/net``."""
    base = sysfs_net / name
    base.mkdir(parents=True)

    scalars: dict[str, str | None] = {
        "ifindex": str(ifindex),
        "address": address,
        "addr_assign_type": addr_assign_type,
        "mtu": mtu,
        "operstate": operstate,
        "carrier": carrier,
        "flags": flags,
        "type": arphrd,
        "speed": speed,
        "duplex": duplex,
        "carrier_changes": carrier_changes,
    }
    for field, value in scalars.items():
        if field in unreadable:
            (base / field).mkdir()
            continue
        if value is None:
            continue
        (base / field).write_text(value + "\n")

    uevent = f"INTERFACE={name}\nIFINDEX={ifindex}\n"
    if devtype:
        uevent += f"DEVTYPE={devtype}\n"
    (base / "uevent").write_text(uevent)

    stats_dir = base / "statistics"
    stats_dir.mkdir()
    for field, value in (statistics or {}).items():
        (stats_dir / field).write_text(value + "\n")

    if device:
        target = sysfs_net.parent / "devices" / device
        target.mkdir(parents=True, exist_ok=True)
        os.symlink(os.path.relpath(target, base), base / "device")
        if subsystem:
            bus = sysfs_net.parent / "bus" / subsystem
            bus.mkdir(parents=True, exist_ok=True)
            os.symlink(os.path.relpath(bus, target), target / "subsystem")
    if bridge:
        (base / "bridge").mkdir()
    if wireless:
        (base / "wireless").mkdir()
    if tun:
        (base / "tun_flags").write_text("0x1002\n")
    if master:
        master_dir = sysfs_net / master
        master_dir.mkdir(parents=True, exist_ok=True)
        os.symlink(os.path.relpath(master_dir, base), base / "master")
    return base


@pytest.fixture
def sysfs_net(tmp_path: Path) -> Path:
    """An empty fake ``/sys/class/net``."""
    path = tmp_path / "sys" / "class" / "net"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def populated_sysfs(sysfs_net: Path) -> Path:
    """A tree covering every classification and every unknown the provider must handle."""
    write_interface(
        sysfs_net,
        "lo",
        ifindex=1,
        address="00:00:00:00:00:00",
        arphrd="772",
        flags="0x9",
        operstate="unknown",
        carrier="1",
        mtu="65536",
        speed=None,
        duplex=None,
    )
    write_interface(
        sysfs_net,
        "eth0",
        ifindex=2,
        address="02:00:00:00:00:10",
        addr_assign_type="0",
        flags="0x1003",
        operstate="down",
        carrier="0",
        speed="-1",
        duplex="unknown",
        device="fd580000.ethernet",
        subsystem="platform",
        statistics={"rx_bytes": "0", "tx_bytes": "0", "rx_dropped": "12"},
    )
    write_interface(
        sysfs_net,
        "eth1",
        ifindex=3,
        address="02:00:00:00:00:12",
        addr_assign_type="0",
        flags="0x1003",
        operstate="up",
        carrier="1",
        speed="1000",
        duplex="full",
        device="1-1.4:1.0",
        subsystem="usb",
    )
    write_interface(
        sysfs_net,
        "wlan0",
        ifindex=4,
        address="02:00:00:00:00:11",
        addr_assign_type="0",
        flags="0x1002",
        operstate="down",
        devtype="wlan",
        wireless=True,
        device="mmc1:0001:1",
        subsystem="sdio",
        unreadable=("carrier", "speed", "duplex"),
    )
    write_interface(
        sysfs_net,
        "wwan0",
        ifindex=5,
        address=None,
        addr_assign_type="1",
        arphrd="65534",
        flags="0x1090",
        operstate="down",
        devtype="wwan",
        device="1-1.2:1.4",
        subsystem="usb",
        unreadable=("carrier", "speed", "duplex"),
    )
    write_interface(
        sysfs_net,
        "tun0",
        ifindex=6,
        address="00:00:00:00:00:00",
        arphrd="65534",
        flags="0x1091",
        operstate="unknown",
        carrier="1",
        tun=True,
        speed=None,
        duplex=None,
    )
    write_interface(
        sysfs_net,
        "docker0",
        ifindex=7,
        address="02:00:00:00:00:13",
        addr_assign_type="3",
        flags="0x1003",
        operstate="down",
        carrier="0",
        devtype="bridge",
        bridge=True,
        speed=None,
        duplex=None,
    )
    write_interface(
        sysfs_net,
        "veth0",
        ifindex=8,
        address="02:00:00:00:00:14",
        addr_assign_type="3",
        flags="0x1303",
        operstate="up",
        carrier="1",
        master="docker0",
        speed="10000",
        duplex="full",
    )
    # Carrier is up while the kernel still calls the link down: a real disagreement, and
    # the only branch of derive_health() that reports 'degraded'.
    write_interface(
        sysfs_net,
        "odd0",
        ifindex=9,
        address="02:00:00:00:00:01",
        addr_assign_type="3",
        flags="0x1003",
        operstate="down",
        carrier="1",
        speed=None,
        duplex=None,
    )
    return sysfs_net


# ------------------------------------------------------------------------- command seam


@dataclass
class FakeRunner:
    """A command runner with scripted answers, so provider failure paths are reachable."""

    responses: dict[tuple[str, ...], CommandResult]
    calls: list[tuple[str, ...]]

    def __init__(self, responses: dict[tuple[str, ...], CommandResult] | None = None) -> None:
        self.responses = responses or {}
        self.calls = []

    def run(self, argv: Sequence[str], timeout_s: float = 5.0) -> CommandResult:
        """An unscripted command is one whose binary is not installed on the fixture host.

        Reported as ``NOT_FOUND`` rather than a bare error, because the difference is
        load-bearing downstream: a provider whose binary is absent owns nothing here, while
        one that is present and would not answer leaves the ownership question open.
        """
        argv = tuple(argv)
        self.calls.append(argv)
        if argv in self.responses:
            return self.responses[argv]
        return CommandResult(
            argv, None, "", "", f"not found: {argv[0]}", CommandFailure.NOT_FOUND
        )


def json_result(argv: Sequence[str], payload: Any) -> CommandResult:
    import json

    return CommandResult(tuple(argv), 0, json.dumps(payload), "")


@pytest.fixture
def link_argv() -> tuple[str, ...]:
    from localplane.agent.providers.linux_network import LINK_ARGV

    return LINK_ARGV


@pytest.fixture
def addr_argv() -> tuple[str, ...]:
    from localplane.agent.providers.linux_network import ADDR_ARGV

    return ADDR_ARGV


@pytest.fixture
def working_runner(link_argv: tuple[str, ...], addr_argv: tuple[str, ...]) -> FakeRunner:
    """Netlink answers matching ``populated_sysfs``."""
    return FakeRunner(
        {
            link_argv: json_result(
                link_argv,
                [
                    {"ifindex": 6, "ifname": "tun0", "linkinfo": {"info_kind": "tun", "info_data": {"type": "tun"}}},
                    {"ifindex": 7, "ifname": "docker0", "linkinfo": {"info_kind": "bridge", "info_data": {"stp_state": 0}}},
                    {"ifindex": 8, "ifname": "veth0", "linkinfo": {"info_kind": "veth"}},
                    {"ifindex": 2, "ifname": "eth0", "parentbus": "platform"},
                ],
            ),
            addr_argv: json_result(
                addr_argv,
                [
                    {"ifindex": 1, "ifname": "lo", "addr_info": [
                        {"family": "inet", "local": "127.0.0.1", "prefixlen": 8, "scope": "host",
                         "valid_life_time": 4294967295, "preferred_life_time": 4294967295}]},
                    {"ifindex": 2, "ifname": "eth0", "addr_info": []},
                    {"ifindex": 3, "ifname": "eth1", "addr_info": [
                        {"family": "inet", "local": "192.0.2.215", "prefixlen": 24, "scope": "global",
                         "dynamic": True, "valid_life_time": 41039, "preferred_life_time": 41039}]},
                    {"ifindex": 4, "ifname": "wlan0", "addr_info": []},
                    {"ifindex": 5, "ifname": "wwan0", "addr_info": []},
                    {"ifindex": 6, "ifname": "tun0", "addr_info": []},
                    {"ifindex": 7, "ifname": "docker0", "addr_info": []},
                    {"ifindex": 8, "ifname": "veth0", "addr_info": []},
                    {"ifindex": 9, "ifname": "odd0", "addr_info": []},
                ],
            ),
        }
    )


# ------------------------------------------------------------------------------ storage


@pytest.fixture
def absent_docker(tmp_path: Path) -> Path:
    """A Docker socket path that is not there.

    Every agent a test constructs is pointed at this. The machine running the suite may
    well have a Docker daemon, and a test whose ownership assertions depended on which
    networks that daemon happens to hold would be a test about this laptop. Tests that want
    Docker evidence serve it themselves — see :func:`fake_docker`.
    """
    return tmp_path / "docker-absent.sock"


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Any]:
    from localplane.backend.db.database import open_database

    db = open_database(tmp_path / "store" / "localplane.db")
    yield db
    db.close()


@pytest.fixture
def fake_root(tmp_path: Path) -> Path:
    """A filesystem root carrying a complete host identity."""
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    (root / "proc" / "sys" / "kernel" / "random").mkdir(parents=True)
    (root / "etc" / "machine-id").write_text("0123456789abcdef0123456789abcdef\n")
    (root / "etc" / "hostname").write_text("fixture-host\n")
    (root / "etc" / "os-release").write_text(
        'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nNAME="Debian GNU/Linux"\n'
        'ID=debian\nVERSION_ID="12"\n'
    )
    (root / "proc" / "sys" / "kernel" / "random" / "boot_id").write_text(
        "11111111-2222-3333-4444-555555555555\n"
    )
    return root


@pytest.fixture
def fake_uname() -> os.uname_result:
    return os.uname_result(("Linux", "fixture-host", "6.8.0-test", "#1 SMP", "aarch64"))


@pytest.fixture(scope="session")
def scratch_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="localplane-tests-") as path:
        yield Path(path)


# ------------------------------------------------------------------------ provider seams


@dataclass
class DockerDaemon:
    """A real Docker daemon socket, answering from a fixed response map.

    A real AF_UNIX HTTP server rather than a stubbed provider, so the transport is exercised
    too: the connection, the request line, the headers, the framing. It also records every
    request it received, which is how the tests prove that nothing but ``GET`` is ever sent
    to a Docker socket.

    This one is stateless: it answers what it was given and nothing it answers changes.
    :class:`StatefulDockerDaemon` is the same server with containers that actually move,
    and both share the harness below — a fake daemon that framed responses differently from
    the one the lifecycle tests use would be a second thing to keep in step.
    """

    path: Path
    responses: dict[str, tuple[int, str]]
    requests: list[tuple[str, str]] = field(default_factory=list)
    server: Any = None

    def answer(self, verb: str, path: str) -> tuple[int, bytes]:
        """What this daemon says to one request.

        Looked up with the query string first and without it second, so a fixture can pin
        an exact request when that is the point and ignore the query when it is not. The
        recorded request always keeps the whole thing.
        """
        status, body = self.responses.get(
            path,
            self.responses.get(
                path.partition("?")[0], (404, '{"message":"No such container"}')
            ),
        )
        return status, body.encode("utf-8")

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


def docker_handler(daemon: Any) -> type:
    """The one fake-Docker request handler. ``daemon.answer`` is the only difference.

    Everything a test could get wrong about a fake daemon lives here and is written once:
    the AF_UNIX peer with no address to format, the silenced logging, the request record,
    and a response the real client can actually parse.
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def address_string(self) -> str:  # AF_UNIX has no peer address to format
            return "unix"

        def log_message(self, *args: Any) -> None:
            return

        def _record_and_answer(self) -> None:
            daemon.requests.append((self.command, self.path))
            status, encoded = daemon.answer(self.command, self.path)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        do_GET = _record_and_answer
        do_POST = _record_and_answer
        do_DELETE = _record_and_answer

    return Handler


def serve_docker(daemon: Any) -> Any:
    """Put one fake daemon on its own socket and start answering."""
    server = socketserver.ThreadingUnixStreamServer(str(daemon.path), docker_handler(daemon))
    server.daemon_threads = True
    daemon.server = server
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return daemon


@pytest.fixture
def docker_daemon(tmp_path: Path) -> Iterator[Callable[..., DockerDaemon]]:
    """Factory for a fake Docker daemon listening on its own socket."""
    started: list[DockerDaemon] = []

    def start(
        networks: list[dict[str, Any]] | None = None,
        version: str = "27.1.1",
        responses: dict[str, tuple[int, str]] | None = None,
        name: str = "docker.sock",
    ) -> DockerDaemon:
        daemon = DockerDaemon(
            path=tmp_path / name,
            responses=responses
            if responses is not None
            else {
                "/version": (200, json.dumps({"Version": version, "ApiVersion": "1.44"})),
                "/networks": (200, json.dumps(networks or [])),
            },
        )
        started.append(serve_docker(daemon))
        return daemon

    yield start
    for daemon in started:
        daemon.stop()


def docker_container(
    container_id: str,
    name: str,
    *,
    state: str = "running",
    running: bool | None = None,
    exit_code: int = 0,
    image: str = "example/app:1.0",
    started_at: str | None = "2026-08-20T10:00:00.123456789Z",
    finished_at: str | None = None,
    health: str | None = None,
    restart_policy: str = "unless-stopped",
    network_mode: str = "app_default",
    networks: dict[str, Any] | None = None,
    ports: dict[str, Any] | None = None,
    mounts: list[dict[str, Any]] | None = None,
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One container as ``GET /containers/{id}/json`` renders it.

    Deliberately includes the parts LocalPlane drops — ``Config.Env``, the command line,
    the graph driver, unlisted labels — so that the tests can assert they were dropped
    rather than assert against a fixture that never had them.
    """
    state_block: dict[str, Any] = {
        "Status": state,
        "Running": running if running is not None else state == "running",
        "Paused": state == "paused",
        "Restarting": state == "restarting",
        "OOMKilled": False,
        "Dead": state == "dead",
        "Pid": 4242 if state == "running" else 0,
        "ExitCode": exit_code,
        "Error": "",
        "StartedAt": started_at or "0001-01-01T00:00:00Z",
        "FinishedAt": finished_at or "0001-01-01T00:00:00Z",
    }
    if health is not None:
        state_block["Health"] = {"Status": health, "FailingStreak": 0, "Log": [{"Output": "x"}]}
    return {
        "Id": container_id,
        "Name": f"/{name}",
        "Created": "2026-08-19T09:00:00.000000000Z",
        "Image": "sha256:" + "a" * 64,
        "Platform": "linux",
        "RestartCount": 0,
        "State": state_block,
        "Config": {
            "Image": image,
            "Env": ["SECRET_TOKEN=hunter2", "PATH=/usr/bin"],
            "Cmd": ["/bin/sh", "-c", "run --forever"],
            "Labels": {
                "com.docker.compose.project": "app",
                "com.docker.compose.service": name,
                "org.opencontainers.image.version": "1.0",
                "maintainer": "Example",
                "com.example.irrelevant": "x" * 40,
                "com.example.other": "y",
                **(labels or {}),
            },
        },
        "HostConfig": {
            "NetworkMode": network_mode,
            "RestartPolicy": {"Name": restart_policy, "MaximumRetryCount": 0},
            "Privileged": True,
            "Binds": ["/etc/passwd:/etc/passwd"],
            "LogConfig": {"Type": "json-file", "Config": {}},
        },
        "GraphDriver": {"Name": "overlay2", "Data": {"UpperDir": "/var/lib/docker/x"}},
        "LogPath": "/var/lib/docker/containers/x/x-json.log",
        "Mounts": mounts
        if mounts is not None
        else [
            {
                "Type": "volume",
                "Name": "app_data",
                "Source": "/var/lib/docker/volumes/app_data/_data",
                "Destination": "/data",
                "Driver": "local",
                "Mode": "z",
                "RW": True,
                "Propagation": "",
            }
        ],
        "NetworkSettings": {
            "Ports": ports
            if ports is not None
            else {
                "8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}],
                "9000/tcp": None,
            },
            "Networks": networks
            if networks is not None
            else {
                "app_default": {
                    "NetworkID": "net" + "0" * 61,
                    "IPAddress": "172.18.0.5",
                    "GlobalIPv6Address": "",
                    "Gateway": "172.18.0.1",
                    "MacAddress": "02:42:ac:12:00:05",
                    "Aliases": [name],
                }
            },
        },
    }


def docker_network(
    network_id: str,
    name: str,
    *,
    gateway: str | None = None,
    subnet: str | None = None,
    bridge_name: str | None = None,
    driver: str = "bridge",
    compose_project: str | None = None,
) -> dict[str, Any]:
    """One network as the Docker API renders it, including the parts LocalPlane drops."""
    options: dict[str, str] = {}
    if bridge_name:
        options["com.docker.network.bridge.name"] = bridge_name
        options["com.docker.network.bridge.default_bridge"] = "true"
    labels = {}
    if compose_project:
        labels["com.docker.compose.project"] = compose_project
        labels["com.docker.compose.network"] = "default"
        labels["com.example.irrelevant"] = "x" * 64
    return {
        "Name": name,
        "Id": network_id,
        "Created": "2026-04-04T20:59:30.805724602+02:00",
        "Scope": "local",
        "Driver": driver,
        "Options": options,
        "Labels": labels,
        "IPAM": {
            "Driver": "default",
            "Config": [{"Subnet": subnet or "172.18.0.0/16", "Gateway": gateway}]
            if gateway
            else [],
        },
        # The heavy parts, present exactly as the daemon sends them.
        "Containers": {
            "a7f3c8d2e1b4" * 2: {
                "Name": "grafana",
                "EndpointID": "e" * 64,
                "MacAddress": "02:42:ac:12:00:04",
                "IPv4Address": "172.18.0.4/16",
            }
        },
        "ConfigFrom": {"Network": ""},
        "Peers": None,
    }


def nmcli_devices(rows: Sequence[Sequence[str]], version: str = "1.46.0") -> dict:
    """Scripted ``nmcli`` answers for a :class:`FakeRunner`.

    ``rows`` are ``(device, type, state, connection, uuid)`` exactly as terse mode prints
    them, so a test writes ``connected (externally)`` rather than a parsed flag.
    """
    from localplane.agent.providers.network_manager import DEVICE_ARGV, GENERAL_ARGV

    escaped = "\n".join(
        ":".join(field.replace("\\", "\\\\").replace(":", "\\:") for field in row)
        for row in rows
    )
    return {
        GENERAL_ARGV: CommandResult(GENERAL_ARGV, 0, f"running:{version}:connected\n", ""),
        DEVICE_ARGV: CommandResult(DEVICE_ARGV, 0, escaped + "\n", ""),
    }


def tailscale_status(
    backend_state: str = "Running",
    tun: bool = True,
    addresses: Sequence[str] = ("100.64.0.10", "2001:db8::64:10"),
    version: str = "1.102.3",
) -> dict:
    """A scripted ``tailscale status --json``, peers and all."""
    from localplane.agent.providers.tailscale import STATUS_ARGV

    payload = {
        "Version": version,
        "TUN": tun,
        "BackendState": backend_state,
        "TailscaleIPs": list(addresses),
        "Self": {
            "HostName": "fixture-host",
            "DNSName": "fixture-host.tail0000aa.ts.net.",
            "TailscaleIPs": list(addresses),
        },
        # Dropped by the provider; present here because the daemon sends it.
        "Peer": {
            "nodekey:" + "a" * 64: {"HostName": "laptop", "TailscaleIPs": ["100.64.0.11"]}
        },
        "Health": [],
    }
    return {STATUS_ARGV: CommandResult(STATUS_ARGV, 0, json.dumps(payload), "")}


# ------------------------------------------------------------------------- netlink seam


@dataclass
class FakeRouteQuery:
    """A scripted kernel, so every branch of the route parser is reachable every run.

    The seam is deliberately as narrow as the command runner's: it replaces the datagram
    exchange and nothing else. The request frame is still built by the provider, the reply
    is still a real netlink message, and every byte of the parsing, the attribute walk and
    the judgement above it runs for real. What a machine cannot be asked to provide on
    demand — a host with no route, a kernel that refuses, a truncated reply, no netlink at
    all — is what this exists to supply.

    ``replies`` is keyed by the destination address the request carries, which is read back
    out of the frame rather than passed alongside it: a test that scripted an answer for one
    address and got it for another would be proving nothing.
    """

    replies: dict[str, Any] = field(default_factory=dict)
    default: Any = None
    requests: list[str] = field(default_factory=list)
    frames: list[bytes] = field(default_factory=list)

    def __call__(self, request: bytes) -> bytes:
        destination = destination_of(request)
        self.requests.append(destination)
        self.frames.append(request)
        answer = self.replies.get(destination, self.default)
        if answer is None:
            from localplane.agent.providers.linux_route import NetlinkFailed

            raise NetlinkFailed("no_scripted_reply", {"destination": destination})
        if isinstance(answer, BaseException):
            raise answer
        return answer


def destination_of(request: bytes) -> str:
    """The address a built ``RTM_GETROUTE`` frame asks about, read back off the wire."""
    import socket as _socket
    import struct as _struct

    from localplane.agent.providers.linux_route import RTA_DST, _NLMSGHDR, _RTATTR, _RTMSG

    length, kind, _flags, _seq, _pid = _NLMSGHDR.unpack_from(request, 0)
    body = request[_NLMSGHDR.size : length]
    family = body[0]
    offset = _RTMSG.size
    while offset + _RTATTR.size <= len(body):
        attr_len, attr_type = _RTATTR.unpack_from(body, offset)
        payload = body[offset + _RTATTR.size : offset + attr_len]
        if attr_type == RTA_DST:
            return _socket.inet_ntop(family, payload)
        offset += (attr_len + 3) & ~3
    raise AssertionError("the request carried no destination")


def netlink_route(
    *,
    destination: str,
    oif_index: int | None = None,
    preferred_source: str | None = None,
    gateway: str | None = None,
    table: int | None = 254,
    route_type: int = 1,
    scope: int = 0,
    protocol: int = 0,
    priority: int | None = None,
) -> bytes:
    """One ``RTM_NEWROUTE`` reply, packed the way the kernel packs it."""
    import ipaddress as _ipaddress
    import struct as _struct

    from localplane.agent.providers.linux_route import (
        RTA_DST,
        RTA_GATEWAY,
        RTA_OIF,
        RTA_PREFSRC,
        RTA_PRIORITY,
        RTA_TABLE,
        RTM_NEWROUTE,
        _NLMSGHDR,
        _RTATTR,
        _RTMSG,
    )

    address = _ipaddress.ip_address(destination)
    family = 2 if address.version == 4 else 10
    bits = 32 if address.version == 4 else 128

    def attribute(kind: int, payload: bytes) -> bytes:
        raw = _RTATTR.pack(_RTATTR.size + len(payload), kind) + payload
        return raw + b"\x00" * (-len(raw) % 4)

    body = _RTMSG.pack(family, bits, 0, 0, table or 0, protocol, scope, route_type, 0)
    body += attribute(RTA_DST, address.packed)
    if table is not None:
        body += attribute(RTA_TABLE, _struct.pack("=I", table))
    if oif_index is not None:
        body += attribute(RTA_OIF, _struct.pack("=I", oif_index))
    if preferred_source is not None:
        body += attribute(RTA_PREFSRC, _ipaddress.ip_address(preferred_source).packed)
    if gateway is not None:
        body += attribute(RTA_GATEWAY, _ipaddress.ip_address(gateway).packed)
    if priority is not None:
        body += attribute(RTA_PRIORITY, _struct.pack("=I", priority))
    return _NLMSGHDR.pack(_NLMSGHDR.size + len(body), RTM_NEWROUTE, 0, 1, 0) + body


def netlink_error(errno: int) -> bytes:
    """One ``NLMSG_ERROR`` reply. ``errno`` is positive; netlink sends it negated."""
    import struct as _struct

    from localplane.agent.providers.linux_route import NLMSG_ERROR, _NLMSGHDR

    body = _struct.pack("=i", -errno) + b"\x00" * 16
    return _NLMSGHDR.pack(_NLMSGHDR.size + len(body), NLMSG_ERROR, 0, 1, 0) + body


# ------------------------------------------------------------------- privileged helper seam


@dataclass
class FakeKernelLinks:
    """A simulated kernel behind the privileged helper's transport seam.

    The seam is exactly as narrow as the route provider's: it replaces the netlink datagram
    exchange and nothing else. The request frames are still built by
    :mod:`localplane.helper.mtu`, the replies are still real netlink messages, and every
    byte of the parsing, the attribute walk, the sequence correlation and the outcome
    decision runs for real. What this supplies is what a machine cannot be asked for on
    demand: an interface that vanishes mid-write, a kernel that refuses, an acknowledgement
    that never arrives, and one that arrives belonging to somebody else.

    ``links`` maps interface index to ``(name, mtu)`` and is the whole of the simulated
    state. A successful mutation moves it *and* the sysfs tree the provider reads, so the
    ordinary observation path sees what the write did — which is what makes an end-to-end
    verification a real verification rather than a rehearsal.
    """

    links: dict[int, tuple[str, int]] = field(default_factory=dict)
    sysfs_net: Path | None = None
    #: Whether this transport could have a mutation accepted. True by default: the
    #: simulated kernel behind it does accept them, which is the point of it existing. A
    #: test that wants a helper reachable but unable to configure links sets it False and
    #: asserts the capability comes back degraded.
    can_mutate: bool = True
    #: Scripted answers for the mutating half, by ifindex or by ``"*"``. A value may be an
    #: exception to raise (before or after dispatch, and the helper tells them apart), or a
    #: callable returning raw reply bytes.
    on_mutate: dict[Any, Any] = field(default_factory=dict)
    on_query: dict[Any, Any] = field(default_factory=dict)
    queries: list[int] = field(default_factory=list)
    mutations: list[tuple[int, int]] = field(default_factory=list)

    # ------------------------------------------------------------------------ transport

    def query(self, request: bytes) -> bytes:

        ifindex, mtu = _read_link_frame(request)
        self.queries.append(ifindex)
        scripted = self.on_query.get(ifindex, self.on_query.get("*"))
        if isinstance(scripted, BaseException):
            raise scripted
        if callable(scripted):
            return scripted(request)
        if ifindex not in self.links:
            return netlink_ack(_sequence_of(request), errno=19)  # ENODEV
        name, current = self.links[ifindex]
        return netlink_link(
            ifindex=ifindex, name=name, mtu=current, sequence=_sequence_of(request)
        )

    def mutate(self, request: bytes) -> bytes:
        ifindex, mtu = _read_link_frame(request)
        assert mtu is not None, "a mutating frame must carry an MTU attribute"
        self.mutations.append((ifindex, mtu))
        scripted = self.on_mutate.get(ifindex, self.on_mutate.get("*"))
        if isinstance(scripted, BaseException):
            raise scripted
        if callable(scripted):
            return scripted(request)
        if ifindex not in self.links:
            return netlink_ack(_sequence_of(request), errno=19)  # ENODEV
        name, _current = self.links[ifindex]
        self.links[ifindex] = (name, mtu)
        if self.sysfs_net is not None:
            path = Path(self.sysfs_net) / name / "mtu"
            if path.exists() or path.parent.exists():
                path.write_text(f"{mtu}\n")
        return netlink_ack(_sequence_of(request), errno=0)


def _sequence_of(frame: bytes) -> int:
    from localplane.helper.mtu import _NLMSGHDR

    _length, _kind, _flags, sequence, _pid = _NLMSGHDR.unpack_from(frame, 0)
    return sequence


def _read_link_frame(frame: bytes) -> tuple[int, int | None]:
    """The ifindex and (for a mutation) the MTU a built frame carries, read off the wire."""
    import struct as _struct

    from localplane.helper.mtu import IFLA_MTU, _IFINFOMSG, _NLMSGHDR, _RTATTR

    length, _kind, _flags, _sequence, _pid = _NLMSGHDR.unpack_from(frame, 0)
    body = frame[_NLMSGHDR.size : length]
    _family, _pad, _type, ifindex, _flags2, _change = _IFINFOMSG.unpack_from(body, 0)
    offset = _IFINFOMSG.size
    mtu: int | None = None
    while offset + _RTATTR.size <= len(body):
        attr_len, attr_type = _RTATTR.unpack_from(body, offset)
        if attr_len < _RTATTR.size:
            break
        payload = body[offset + _RTATTR.size : offset + attr_len]
        if attr_type == IFLA_MTU and len(payload) >= 4:
            mtu = int(_struct.unpack_from("=I", payload, 0)[0])
        offset += (attr_len + 3) & ~3
    return ifindex, mtu


def netlink_link(*, ifindex: int, name: str, mtu: int, sequence: int = 1) -> bytes:
    """One ``RTM_NEWLINK`` reply, packed the way the kernel packs one."""
    import struct as _struct

    from localplane.helper.mtu import (
        IFLA_IFNAME,
        IFLA_MTU,
        RTM_NEWLINK,
        _IFINFOMSG,
        _NLMSGHDR,
        _RTATTR,
    )

    def attribute(kind: int, payload: bytes) -> bytes:
        raw = _RTATTR.pack(_RTATTR.size + len(payload), kind) + payload
        return raw + b"\x00" * (-len(raw) % 4)

    body = _IFINFOMSG.pack(0, 0, 1, ifindex, 0, 0)
    body += attribute(IFLA_IFNAME, name.encode() + b"\x00")
    body += attribute(IFLA_MTU, _struct.pack("=I", mtu))
    return _NLMSGHDR.pack(_NLMSGHDR.size + len(body), RTM_NEWLINK, 0, sequence, 0) + body


def netlink_ack(sequence: int, *, errno: int = 0, echo_type: int | None = None,
                echo_sequence: int | None = None) -> bytes:
    """One ``NLMSG_ERROR`` reply. ``errno`` is positive; netlink sends it negated.

    ``echo_type`` and ``echo_sequence`` override the header the kernel echoes back inside
    the error payload, which is what a correlation check reads.
    """
    from localplane.helper.mtu import RTM_NEWLINK, _NLMSGHDR

    import struct as _struct

    echoed = _NLMSGHDR.pack(
        32,
        RTM_NEWLINK if echo_type is None else echo_type,
        0x05,
        sequence if echo_sequence is None else echo_sequence,
        0,
    )
    body = _struct.pack("=i", -errno) + echoed
    return _NLMSGHDR.pack(_NLMSGHDR.size + len(body), 0x02, 0, sequence, 0) + body
