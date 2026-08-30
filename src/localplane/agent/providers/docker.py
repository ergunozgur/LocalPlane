"""The one place LocalPlane talks to Docker.

Docker already implements container lifecycle, inspection, logs, statistics, networks,
mounts, health and restart policy, and it is authoritative for every one of them. This
module is therefore deliberately *thin*: it is a transport, a closed set of typed calls,
and the normalisation that turns a daemon's JSON into the shape the rest of LocalPlane
already speaks. It reimplements nothing Docker does.

**One boundary, not four.** Reading networks for ownership evidence, observing containers,
fetching logs, sampling statistics and running a lifecycle verb are five things a caller
can ask for and one place they are asked from. Splitting them into an evidence client, a
runtime client and a lifecycle client would be three wrappers over one socket, three places
for a header, a timeout or an error mapping to drift apart, and three surfaces to audit
instead of one.

**Why the Engine socket and not the SDK**, in the order the reasons actually decide it.

*The outcome vocabulary.* ``docker`` 7.2.0's ``APIClient.start``/``stop``/``restart`` return
``None`` and call ``requests``' ``raise_for_status()``, which does not raise on ``304``. So a
``stop`` that stopped a container and a ``stop`` the daemon declined because it was already
stopped are the **same return value** — and that is exactly the ``written`` / ``not_written``
line this module exists to draw. Recovering it means calling the private ``_post`` and
reading the status back, at which point the SDK's container API is bypassed entirely.
``dispatched`` is the same shape of problem one layer down: ``requests`` reports a connect
failure and a mid-read reset as one ``ConnectionError``, and telling them apart means
matching on the ``urllib3`` exception it wrapped.

*The size of what it would replace.* Not the HTTP — ``http.client`` already does that, and
there is no framing or parsing code in this file. What a library would replace is
``_UnixHTTPConnection``: thirteen lines, and the SDK hand-writes the same nine-line
``socket.socket(AF_UNIX); settimeout; connect`` in ``docker/transport/unixconn.py``, because
neither ``urllib3`` nor ``http.client`` ships Unix-socket support. Sixty to ninety lines
saved, against 149 modules and ~59,000 lines across six packages, and the agent's
stdlib-only guarantee.

*The surface it exposes is the weakest of the three reasons and is listed last on purpose.*
A dependency may expose ``containers.run`` and ``exec_create`` internally while this adapter
exposes six typed calls — the boundary is this module, not the package. What it does cost is
auditability: "no verb but ``GET`` and one closed ``POST`` tuple" is currently read off this
file rather than taken on trust about what the adapter calls.

The trade would be different for a build that needed broad Docker coverage — compose, image
lifecycle, registries, events, exec. This one needs six calls.

**What a caller may ask for is a closed set.** There is no method here that takes a path, a
verb, a query string or a body. The only value that ever reaches a URL is a container id,
and :func:`_container_path` requires it to be lowercase hexadecimal — so there is no
argument, on any method, through which a request for some other Docker endpoint could be
constructed.

**Privilege is not pretended away.** Write access to this socket is effectively root on the
host: a daemon that will start a container will start one with the host's filesystem
mounted. LocalPlane does not proxy the socket, does not narrow it and does not claim to —
it probes what its access actually is, publishes read and lifecycle as separate
capabilities, and refuses to report a lifecycle capability it has not established.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import select
import socket
import stat
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlencode

from localplane.agent.docker_direct_attestation import DockerDirectEngineAttestation
from localplane.agent.providers.evidence import ProviderReading
from localplane.protocol.docker_runtime_owner import (
    DOCKER_DIRECT_UNIX_CONTRACT,
    DOCKER_RUNTIME_OWNER_METHOD_VERSION,
)
from localplane.protocol.providers import (
    PROVIDER_DOCKER,
    SOURCE_DOCKER_CONTAINERS,
    SOURCE_DOCKER_NETWORKS,
    ProviderStatus,
)
from localplane.protocol.wire import (
    DOCKER_LIFECYCLE_ACTIONS,
    DOCKER_LOG_LINES_DEFAULT,
    DOCKER_LOG_LINES_MAX,
)

DEFAULT_DOCKER_SOCKET = "/var/run/docker.sock"

METHOD = "unix_socket_http"

VERSION_PATH = "/version"
NETWORKS_PATH = "/networks"
CONTAINERS_PATH = "/containers/json"
INFO_PATH = "/info"

# Linux added this stable UAPI number in 6.5.  Python builds predating the constant may
# run on newer kernels, so availability is established by getsockopt(2), not by whether
# the Python ``socket`` module happened to publish the name when it was built.
SO_PEERPIDFD = getattr(socket, "SO_PEERPIDFD", 77)
_UCRED = struct.Struct("=3i")

RUNTIME_OWNER_API_MIN = (1, 41)
RUNTIME_OWNER_API_MAX = (1, 47)
RUNTIME_OWNER_METHOD_VERSION = DOCKER_RUNTIME_OWNER_METHOD_VERSION
RUNTIME_OWNER_SNAPSHOT_TIMEOUT_S = 20.0
_FULL_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")

#: Read ceiling for one response body. A daemon with a great many containers still answers
#: in kilobytes; this exists so a hostile or broken peer cannot make the agent allocate
#: without bound. Logs have their own, smaller, ceiling.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

#: Ceiling on one log fetch, independent of how many lines were asked for. A container that
#: writes megabyte lines must not be able to turn a bounded request into an unbounded read.
MAX_LOG_BYTES = 1024 * 1024

#: The most containers one observation will inspect. Listing is one request; inspecting is
#: one per container, and a host with thousands of them should get a truthful partial
#: observation rather than a sweep that takes minutes. What was left out is reported.
MAX_CONTAINERS = 200

#: Ceiling on log lines per request, and the default when none is asked for. Declared in
#: the protocol, because the API documents the same two numbers the agent enforces.
MAX_LOG_LINES = DOCKER_LOG_LINES_MAX
DEFAULT_LOG_LINES = DOCKER_LOG_LINES_DEFAULT

#: How long the daemon is given to stop a container before it kills it. Docker's own
#: default is 10 seconds and this is not a parameter: an operator-supplied grace period is
#: a value that would have to travel from the API through the Run, and the Run carries no
#: values.
STOP_TIMEOUT_S = 10

#: A container id as Docker writes one. The only caller-supplied value that reaches a URL
#: in this module, and it is checked against this before it does. Docker accepts a name
#: here too; LocalPlane never sends one, because a name is not an identity and a value that
#: may contain a slash has no business in a path.
_CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")

#: Labels worth keeping, by exact name and by prefix. Everything else is counted and
#: dropped: a container's label set is unbounded, frequently carries build metadata nobody
#: operates on, and occasionally carries secrets. What was dropped is reported as a count
#: rather than left implicit.
_LABEL_NAMES = frozenset(
    {
        "com.docker.compose.project",
        "com.docker.compose.service",
        "com.docker.compose.config-hash",
        "com.docker.compose.project.working_dir",
        "com.docker.compose.project.config_files",
        "com.docker.compose.container-number",
        "maintainer",
        "description",
    }
)
_LABEL_PREFIXES = ("org.opencontainers.image.", "io.localplane.")

#: The longest label value kept. A longer one is truncated and marked.
_MAX_LABEL_VALUE = 512

_OMITTED_NETWORKS = (
    "Containers",
    "Options (except the bridge name and default-bridge flags)",
    "Labels (except com.docker.compose.project and .network)",
    "ConfigFrom",
    "Peers",
)

_OMITTED_CONTAINERS = (
    "Config.Env (may carry credentials)",
    "Config.Cmd, Entrypoint, Healthcheck.Test (command lines)",
    "GraphDriver, ResolvConfPath, HostnamePath, LogPath",
    "HostConfig (except NetworkMode and RestartPolicy)",
    "Labels not on the kept list",
    "State.Health.Log (only the current status and failing streak are kept)",
)

LABEL_COMPOSE_PROJECT = "com.docker.compose.project"
LABEL_COMPOSE_NETWORK = "com.docker.compose.network"

OPTION_BRIDGE_NAME = "com.docker.network.bridge.name"
OPTION_DEFAULT_BRIDGE = "com.docker.network.bridge.default_bridge"


class InvalidContainerId(ValueError):
    """A container id that is not one. Raised before anything reaches a URL."""


@dataclass(frozen=True)
class DockerFailure(Exception):
    """A request that did not produce a usable answer, and what may be concluded from it.

    ``dispatched`` is the load-bearing field on the lifecycle path and it is set the moment
    the request is handed to a connected socket. False means the daemon was never given the
    request, which is a proof that nothing happened. True means it may already have acted,
    and the honest answer stops being "it failed".
    """

    status: ProviderStatus
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)
    dispatched: bool = False
    http_status: int | None = None


@dataclass(frozen=True)
class DockerRuntimeOwnerFailure(Exception):
    """A closed runtime-owner read that cannot contribute authority."""

    gap: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DockerPeerProcess:
    """Ephemeral identity of the process recorded on one connected AF_UNIX socket."""

    pidfd: int
    pid: int
    uid: int
    gid: int


@dataclass(frozen=True)
class DockerRuntimeVersion:
    engine_version: str
    api_version: str
    minimum_api_version: str
    os: str
    arch: str

    def as_dict(self) -> dict[str, str]:
        return {
            "engine_version": self.engine_version,
            "api_version": self.api_version,
            "minimum_api_version": self.minimum_api_version,
            "os": self.os,
            "arch": self.arch,
        }


@dataclass(frozen=True)
class DockerRuntimeInfo:
    engine_id: str
    server_version: str
    os_type: str
    cgroup_driver: str
    cgroup_version: str
    live_restore_enabled: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "engine_id": self.engine_id,
            "server_version": self.server_version,
            "os_type": self.os_type,
            "cgroup_driver": self.cgroup_driver,
            "cgroup_version": self.cgroup_version,
            "live_restore_enabled": self.live_restore_enabled,
        }


@dataclass(frozen=True)
class ContainerObservation:
    """One container as LocalPlane records it, with the evidence it was read from."""

    container_id: str
    facts: dict[str, Any]
    gaps: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def fidelity(self) -> str:
        return "complete" if not self.gaps else "partial"

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": METHOD,
            "fidelity": self.fidelity,
            "observed_at": self.facts.get("observed_at"),
            "gaps": list(self.gaps),
            "facts": self.facts,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class ContainerBatch:
    """Every container the daemon reported, or the typed reason there are none."""

    status: str
    started_at: str
    completed_at: str
    api_version: str | None = None
    engine_version: str | None = None
    containers: tuple[ContainerObservation, ...] = ()
    reason: str | None = None
    issues: tuple[dict[str, Any], ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability": "docker.containers.observe",
            "provider": PROVIDER_DOCKER,
            "provider_version": self.engine_version or "unknown",
            "api_version": self.api_version,
            "source": SOURCE_DOCKER_CONTAINERS,
            "method": METHOD,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "reason": self.reason,
            "containers": [c.as_dict() for c in self.containers],
            "issues": [dict(i) for i in self.issues],
            "detail": self.detail,
        }


@dataclass(frozen=True)
class LifecycleResult:
    """What the daemon said about one lifecycle request. A report, never a verdict."""

    outcome: str
    """``written`` · ``not_written`` · ``write_unknown``. The vocabulary is the Change
    engine's and the mapping is argued for in :meth:`DockerProvider.lifecycle`."""

    reason: str
    action: str
    container_id: str
    http_status: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "action": self.action,
            "container_id": self.container_id,
            "http_status": self.http_status,
            "provider": PROVIDER_DOCKER,
            "method": METHOD,
            "detail": self.detail,
        }


class _UnixHTTPConnection(http.client.HTTPConnection):
    """``http.client`` over an AF_UNIX stream. The only transport this module has."""

    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._socket_path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


class _RuntimeOwnerReconnect(RuntimeError):
    pass


class _RuntimeOwnerHTTPConnection(_UnixHTTPConnection):
    """AF_UNIX HTTP connection that structurally refuses a second connect attempt."""

    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__(socket_path, timeout)
        self.connect_count = 0

    def connect(self) -> None:
        if self.connect_count:
            raise _RuntimeOwnerReconnect(
                "runtime-owner HTTP transport attempted to reconnect"
            )
        self.connect_count += 1
        super().connect()


class DockerRuntimeOwnerSession:
    """One non-reconnecting, fixed-operation Engine snapshot transport.

    Instances are created only from :meth:`DockerProvider.open_runtime_owner_snapshot`
    with a trusted typed attestation.  There is no public verb/path/query primitive.
    """

    def __init__(
        self,
        attestation: DockerDirectEngineAttestation,
        *,
        timeout_s: float,
        snapshot_timeout_s: float = RUNTIME_OWNER_SNAPSHOT_TIMEOUT_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._attestation = attestation
        self._timeout_s = timeout_s
        self._connection: _RuntimeOwnerHTTPConnection | None = None
        self._socket: socket.socket | None = None
        self._peer: DockerPeerProcess | None = None
        self._selected_api: str | None = None
        self._listed_ids: frozenset[str] | None = None
        self._snapshot_timeout_s = snapshot_timeout_s
        self._monotonic = monotonic
        self._deadline: float | None = None

    def __enter__(self) -> "DockerRuntimeOwnerSession":
        endpoint = self._attestation.engine_endpoint
        self._deadline = self._monotonic() + self._snapshot_timeout_s
        try:
            endpoint_stat = os.lstat(endpoint)
            if not stat.S_ISSOCK(endpoint_stat.st_mode):
                raise DockerRuntimeOwnerFailure(
                    "management_path.runtime_owner.direct_transport_unproven",
                    "engine_endpoint_is_not_unix_socket",
                    {"endpoint": endpoint},
                )
            connection = _RuntimeOwnerHTTPConnection(endpoint, self._timeout_s)
            # ``http.client`` otherwise reconnects automatically when ``sock`` becomes
            # None.  Runtime-owner evidence is tied to this one listener peer, so even an
            # attempted implicit reconnect must be structurally impossible.
            connection.auto_open = 0
            connection.connect()
            if connection.sock is None:
                raise DockerRuntimeOwnerFailure(
                    "management_path.runtime_owner.direct_transport_unproven",
                    "engine_socket_not_connected",
                )
            self._connection = connection
            self._socket = connection.sock
            self._apply_transport_deadline(self._socket)
            self._peer = self._read_peer(self._socket)
            self.require_peer_live()
            return self
        except DockerRuntimeOwnerFailure:
            self.close()
            raise
        except FileNotFoundError as exc:
            self.close()
            raise DockerRuntimeOwnerFailure(
                "management_path.runtime_owner.direct_transport_unproven",
                "docker_socket_absent",
                {"endpoint": endpoint, "error": str(exc)},
            ) from exc
        except PermissionError as exc:
            self.close()
            raise DockerRuntimeOwnerFailure(
                "management_path.runtime_owner.direct_transport_unproven",
                "permission_denied",
                {"endpoint": endpoint, "error": str(exc)},
            ) from exc
        except OSError as exc:
            self.close()
            raise DockerRuntimeOwnerFailure(
                "management_path.runtime_owner.direct_transport_unproven",
                "docker_socket_unavailable",
                {"endpoint": endpoint, "errno": exc.errno, "error": str(exc)},
            ) from exc
        except Exception:
            # Keep an unexpected validation failure from leaking the connected socket.
            # Known evidence failures above retain their closed typed mapping.
            self.close()
            raise

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    @property
    def peer(self) -> DockerPeerProcess:
        if self._peer is None:
            raise RuntimeError("runtime-owner session is not connected")
        return self._peer

    @property
    def selected_api_version(self) -> str | None:
        return self._selected_api

    def close(self) -> None:
        peer = self._peer
        self._peer = None
        if peer is not None:
            try:
                os.close(peer.pidfd)
            except OSError:
                pass
        connection = self._connection
        self._connection = None
        self._socket = None
        if connection is not None:
            connection.close()

    def require_peer_live(self) -> None:
        self._require_snapshot_deadline()
        peer = self.peer
        try:
            poller = select.poll()
            poller.register(
                peer.pidfd,
                select.POLLIN
                | select.POLLHUP
                | select.POLLERR
                | getattr(select, "POLLNVAL", 0),
            )
            exited = bool(poller.poll(0))
        except OSError:
            exited = True
        if exited:
            raise DockerRuntimeOwnerFailure(
                "management_path.runtime_owner.peer_process_exited",
                "docker_peer_process_exited",
            )

    def read_version(self) -> DockerRuntimeVersion:
        payload = self._get_json(VERSION_PATH, versioned=False)
        if not isinstance(payload, dict):
            raise self._snapshot_shape("version_not_object")
        values = {
            "engine_version": payload.get("Version"),
            "api_version": payload.get("ApiVersion"),
            "minimum_api_version": payload.get("MinAPIVersion"),
            "os": payload.get("Os"),
            "arch": payload.get("Arch"),
        }
        if any(not isinstance(value, str) or not value for value in values.values()):
            raise self._snapshot_shape("version_fields_invalid")
        if values["os"] != "linux":
            raise DockerRuntimeOwnerFailure(
                "management_path.runtime_owner.direct_transport_unproven",
                "docker_engine_not_linux",
                {"os": values["os"]},
            )
        selected = _select_runtime_api(
            values["api_version"], values["minimum_api_version"]
        )
        if self._selected_api is None:
            self._selected_api = selected
        elif self._selected_api != selected:
            raise DockerRuntimeOwnerFailure(
                "management_path.runtime_owner.engine_snapshot_changed",
                "docker_api_negotiation_changed",
            )
        return DockerRuntimeVersion(**values)

    def read_info(self) -> DockerRuntimeInfo:
        payload = self._get_json(INFO_PATH, versioned=True)
        if not isinstance(payload, dict):
            raise self._snapshot_shape("info_not_object")
        engine_id = payload.get("ID")
        server_version = payload.get("ServerVersion")
        os_type = payload.get("OSType")
        cgroup_driver = payload.get("CgroupDriver")
        cgroup_version = payload.get("CgroupVersion")
        live_restore = payload.get("LiveRestoreEnabled")
        if (
            not isinstance(engine_id, str)
            or not engine_id
            or not isinstance(server_version, str)
            or not server_version
            or os_type != "linux"
            or not isinstance(cgroup_driver, str)
            or not cgroup_driver
            or str(cgroup_version) != "2"
            or not isinstance(live_restore, bool)
        ):
            raise self._snapshot_shape("info_fields_invalid")
        return DockerRuntimeInfo(
            engine_id=engine_id,
            server_version=server_version,
            os_type=os_type,
            cgroup_driver=cgroup_driver,
            cgroup_version="2",
            live_restore_enabled=live_restore,
        )

    def list_container_ids(self) -> tuple[str, ...]:
        payload = self._get_json(CONTAINERS_PATH, query={"all": "1"}, versioned=True)
        if not isinstance(payload, list):
            raise self._snapshot_shape("container_list_not_array")
        if len(payload) > MAX_CONTAINERS:
            raise DockerRuntimeOwnerFailure(
                "management_path.runtime_owner.container_enumeration_incomplete",
                "container_limit_reached",
                {"listed": len(payload), "limit": MAX_CONTAINERS},
            )
        ids: list[str] = []
        for entry in payload:
            container_id = entry.get("Id") if isinstance(entry, dict) else None
            if not isinstance(container_id, str) or not _FULL_CONTAINER_ID.fullmatch(
                container_id
            ):
                raise self._snapshot_shape("daemon_listed_container_id_invalid")
            ids.append(container_id)
        if len(ids) != len(set(ids)):
            raise self._snapshot_shape("daemon_listed_container_id_duplicate")
        self._listed_ids = frozenset(ids)
        return tuple(ids)

    def inspect_listed_container(self, container_id: str) -> dict[str, Any]:
        if self._listed_ids is None or container_id not in self._listed_ids:
            raise DockerRuntimeOwnerFailure(
                "management_path.runtime_owner.daemon_process_unattested",
                "container_id_not_from_daemon_enumeration",
            )
        try:
            payload = self._get_json(
                _container_path(container_id, "/json"), versioned=True
            )
        except DockerRuntimeOwnerFailure as exc:
            if (
                exc.reason == "docker_runtime_http_status"
                and exc.detail.get("http_status") == 404
            ):
                raise DockerRuntimeOwnerFailure(
                    "management_path.runtime_owner.container_changed",
                    "daemon_listed_container_disappeared",
                    {"container_id": container_id},
                ) from exc
            raise
        if not isinstance(payload, dict):
            raise self._snapshot_shape("container_inspect_not_object")
        return payload

    def _read_peer(self, connected: socket.socket) -> DockerPeerProcess:
        """Pin the AF_UNIX listener creator recorded on the connected client socket.

        Linux reports the listener creator, which is exactly the direct-listener contract
        here and deliberately makes socket activation resolve to the activator instead of
        being rescued as Docker.
        """
        try:
            credentials = connected.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size
            )
            pid, uid, gid = _UCRED.unpack(credentials)
        except (OSError, struct.error) as exc:
            raise DockerRuntimeOwnerFailure(
                "management_path.runtime_owner.daemon_process_unattested",
                "docker_peer_credentials_unavailable",
                {"error": str(exc)},
            ) from exc
        if pid <= 0:
            raise DockerRuntimeOwnerFailure(
                "management_path.runtime_owner.pid_namespace_mismatch",
                "docker_peer_credentials_pid_invalid",
                {"peercred_pid": pid},
            )
        try:
            pidfd = connected.getsockopt(socket.SOL_SOCKET, SO_PEERPIDFD)
        except OSError as exc:
            raise DockerRuntimeOwnerFailure(
                "management_path.runtime_owner.peer_pidfd_unavailable",
                "so_peerpidfd_unavailable",
                {"errno": exc.errno, "error": str(exc)},
            ) from exc
        if isinstance(pidfd, bool) or not isinstance(pidfd, int) or pidfd < 0:
            raise DockerRuntimeOwnerFailure(
                "management_path.runtime_owner.peer_pidfd_unavailable",
                "so_peerpidfd_invalid",
            )
        validated_pidfd = False
        try:
            try:
                _validate_pidfd_handle(pidfd)
            except (OSError, ValueError) as exc:
                raise DockerRuntimeOwnerFailure(
                    "management_path.runtime_owner.peer_pidfd_unavailable",
                    "so_peerpidfd_result_is_not_pidfd",
                    {"error_type": type(exc).__name__, "error": str(exc)},
                ) from exc
            validated_pidfd = True
            try:
                pidfd_pid = _pidfd_visible_pid(pidfd, validate_handle=False)
            except (OSError, ValueError) as exc:
                raise DockerRuntimeOwnerFailure(
                    "management_path.runtime_owner.pid_namespace_mismatch",
                    "peer_pidfd_process_not_visible",
                    {"error_type": type(exc).__name__, "error": str(exc)},
                ) from exc
            if pidfd_pid <= 0 or pidfd_pid != pid:
                raise DockerRuntimeOwnerFailure(
                    "management_path.runtime_owner.pid_namespace_mismatch",
                    "peer_pidfd_and_peercred_disagree",
                    {"peercred_pid": pid, "pidfd_visible_pid": pidfd_pid},
                )
            if uid != 0:
                raise DockerRuntimeOwnerFailure(
                    "management_path.runtime_owner.daemon_process_unattested",
                    "docker_peer_not_rootful",
                    {"peer_uid": uid},
                )
            return DockerPeerProcess(pidfd=pidfd, pid=pid, uid=uid, gid=gid)
        except Exception:
            # A wrong or unsupported option number could theoretically yield an integer
            # that names some unrelated descriptor already owned by this process.  Never
            # close it unless procfs first proved that it is an actual pidfd.
            if validated_pidfd:
                os.close(pidfd)
            raise

    def _get_json(
        self,
        path: str,
        *,
        query: dict[str, str] | None = None,
        versioned: bool,
    ) -> Any:
        if versioned:
            if self._selected_api is None:
                raise RuntimeError("Docker API version has not been selected")
            path = f"/v{self._selected_api}{path}"
        raw = self._get_bytes(path, query)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DockerRuntimeOwnerFailure(
                "management_path.runtime_owner.engine_snapshot_changed",
                "docker_runtime_response_unparseable",
                {"error": str(exc)},
            ) from exc

    def _get_bytes(self, path: str, query: dict[str, str] | None) -> bytes:
        connection = self._connection
        original_socket = self._socket
        if connection is None or original_socket is None:
            raise RuntimeError("runtime-owner session is not connected")
        if (
            connection.sock is not original_socket
            or connection.connect_count != 1
            or connection.auto_open != 0
        ):
            raise self._reconnected()
        self._apply_transport_deadline(original_socket)
        self.require_peer_live()
        url = f"{path}?{urlencode(query)}" if query else path
        try:
            connection.request(
                "GET",
                url,
                headers={
                    "Host": "localhost",
                    "Accept": "application/json",
                    "Connection": "keep-alive",
                },
            )
            response = connection.getresponse()
            body = response.read(MAX_RESPONSE_BYTES + 1)
        except _RuntimeOwnerReconnect as exc:
            raise self._reconnected(str(exc)) from exc
        except (
            ConnectionError,
            TimeoutError,
            socket.timeout,
            http.client.HTTPException,
        ) as exc:
            raise DockerRuntimeOwnerFailure(
                "management_path.runtime_owner.transport_reconnected",
                "docker_runtime_transport_failed",
                {"error_type": type(exc).__name__, "error": str(exc)},
            ) from exc
        except OSError as exc:
            raise DockerRuntimeOwnerFailure(
                "management_path.runtime_owner.transport_reconnected",
                "docker_runtime_socket_failed",
                {"errno": exc.errno, "error": str(exc)},
            ) from exc
        if len(body) > MAX_RESPONSE_BYTES:
            raise self._snapshot_shape("runtime_response_limit_exceeded")
        if hasattr(response, "isclosed") and not response.isclosed():
            raise self._snapshot_shape("runtime_response_not_fully_consumed")
        if response.status != 200:
            raise DockerRuntimeOwnerFailure(
                "management_path.runtime_owner.engine_snapshot_changed",
                "docker_runtime_http_status",
                {
                    "path": path,
                    "http_status": response.status,
                    "daemon_message": _message(body),
                },
            )
        connection_tokens = {
            token.strip().lower()
            for token in response.getheader("Connection", "").split(",")
            if token.strip()
        }
        if response.will_close or "close" in connection_tokens:
            raise self._reconnected("Docker response requires connection close")
        if (
            connection.sock is not original_socket
            or connection.connect_count != 1
            or connection.auto_open != 0
        ):
            raise self._reconnected()
        self.require_peer_live()
        return body

    def _apply_transport_deadline(self, connected: socket.socket) -> None:
        remaining = self._require_snapshot_deadline()
        connected.settimeout(min(self._timeout_s, remaining))

    def _require_snapshot_deadline(self) -> float:
        if self._deadline is None:
            raise RuntimeError("runtime-owner snapshot deadline is not initialized")
        remaining = self._deadline - self._monotonic()
        if remaining <= 0:
            raise DockerRuntimeOwnerFailure(
                "management_path.runtime_owner.engine_snapshot_changed",
                "docker_runtime_snapshot_deadline_exceeded",
            )
        return remaining

    @staticmethod
    def _snapshot_shape(reason: str) -> DockerRuntimeOwnerFailure:
        return DockerRuntimeOwnerFailure(
            "management_path.runtime_owner.engine_snapshot_changed", reason
        )

    @staticmethod
    def _reconnected(
        reason: str = "Docker HTTP connection identity changed",
    ) -> DockerRuntimeOwnerFailure:
        return DockerRuntimeOwnerFailure(
            "management_path.runtime_owner.transport_reconnected", reason
        )


def _container_path(container_id: str, suffix: str) -> str:
    """``/containers/<id><suffix>`` — the only place a value reaches a Docker URL.

    ``suffix`` is always a module constant supplied by a method in this file, and the id is
    required to be lowercase hexadecimal of Docker's own length. A name, an empty string, a
    path segment, a query string or anything with a ``/``, a ``.`` or a ``%`` in it is
    refused here, before a request exists — so no argument to any public method on this
    provider can be made to address a different Docker endpoint.
    """
    if not isinstance(container_id, str) or not _CONTAINER_ID.match(container_id):
        raise InvalidContainerId(
            "a container id must be 12 to 64 lowercase hexadecimal characters"
        )
    return f"/containers/{container_id}{suffix}"


class DockerProvider:
    """LocalPlane's whole relationship with the Docker daemon.

    Six typed calls, each of which either answers or says why it could not. Five read and
    one writes, and the one that writes takes its verb from a closed set of three declared
    in the protocol rather than from anything a caller composed.
    """

    provider = PROVIDER_DOCKER
    source = SOURCE_DOCKER_NETWORKS

    def __init__(
        self,
        socket_path: str | Path = DEFAULT_DOCKER_SOCKET,
        timeout_s: float = 5.0,
        lifecycle_timeout_s: float = 60.0,
    ) -> None:
        self._socket_path = Path(socket_path)
        self._timeout_s = timeout_s
        # Lifecycle gets its own budget: stopping a container that ignores SIGTERM takes
        # the daemon's full grace period plus a kill, and a read timeout applied to that
        # would turn an ordinary slow stop into `write_unknown` every time.
        self._lifecycle_timeout_s = lifecycle_timeout_s

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    def open_runtime_owner_snapshot(
        self, attestation: DockerDirectEngineAttestation
    ) -> DockerRuntimeOwnerSession:
        """Open the one closed same-transport runtime-owner read.

        The endpoint is accepted only inside the trusted typed attestation.  This method
        has no string/path/verb/query/container/PID/cgroup/unit parameter and deliberately
        ignores the ordinary Docker provider socket setting, which is operational access
        rather than runtime-owner authority.
        """
        if (
            not isinstance(attestation, DockerDirectEngineAttestation)
            or attestation.contract != DOCKER_DIRECT_UNIX_CONTRACT
            or attestation.runtime != "docker"
            or attestation.engine_transport != "direct_unix"
            or attestation.engine_process_role != "service_main"
            or attestation.engine_scope != "host_system_manager"
            or attestation.engine_privilege != "rootful"
        ):
            raise DockerRuntimeOwnerFailure(
                "management_path.runtime_owner.attestation_untrusted",
                "docker_direct_attestation_type_invalid",
            )
        return DockerRuntimeOwnerSession(attestation, timeout_s=self._timeout_s)

    # --------------------------------------------------------------------- ownership

    def read(self) -> ProviderReading:
        """What the daemon declares about its own networks. The ownership evidence path.

        It uses two fixed reads: the
        records kept are the ones that can support an ownership claim about a kernel link,
        and everything else is dropped with the omission declared rather than implied.
        """
        observed_at = _now()
        detail: dict[str, Any] = {
            "socket": str(self._socket_path),
            "endpoints": [VERSION_PATH, NETWORKS_PATH],
            "omitted": list(_OMITTED_NETWORKS),
        }

        if not self._socket_path.exists():
            # Not a gap: no daemon, therefore nothing on this host is Docker's.
            return self._unreadable(
                ProviderStatus.ABSENT, "docker_socket_absent", observed_at, detail
            )
        try:
            version = self._version()
            networks = self._get(NETWORKS_PATH)
        except DockerFailure as failure:
            return self._unreadable(
                failure.status, failure.reason, observed_at, {**detail, **failure.detail}
            )
        if not isinstance(networks, list):
            return self._unreadable(
                ProviderStatus.ERROR,
                "unexpected_shape",
                observed_at,
                {**detail, "received": type(networks).__name__},
            )

        records = tuple(
            record for record in (_network_record(entry) for entry in networks) if record
        )
        return ProviderReading(
            provider=self.provider,
            source=self.source,
            status=ProviderStatus.OK,
            method=METHOD,
            observed_at=observed_at,
            version=version.get("Version"),
            records=records,
            detail={**detail, "networks": len(records)},
        )

    # -------------------------------------------------------------------- containers

    def observe_containers(self) -> ContainerBatch:
        """Every container on this host, as the daemon describes it.

        One list request and one inspect per container. The list alone would be cheaper and
        it does not carry what an operator needs: no health verdict, no restart policy, no
        exit code, no start time — and the start time is what tells a restart that actually
        happened from a container that was already running.

        Never raises. A daemon that is absent, unreachable or unreadable produces a batch
        that says so, because a Docker that cannot be read must not cost LocalPlane the
        rest of its view of the host.
        """
        started_at = _now()

        def failed(status: str, reason: str, detail: dict[str, Any]) -> ContainerBatch:
            return ContainerBatch(
                status=status,
                started_at=started_at,
                completed_at=_now(),
                reason=reason,
                detail={"socket": str(self._socket_path), **detail},
            )

        if not self._socket_path.exists():
            return failed("failed", "docker_socket_absent", {})
        try:
            version = self._version()
            listed = self._get(CONTAINERS_PATH, {"all": "1"})
        except DockerFailure as failure:
            return failed("failed", failure.reason, failure.detail)
        if not isinstance(listed, list):
            return failed("failed", "unexpected_shape", {"received": type(listed).__name__})

        ids = [
            entry["Id"]
            for entry in listed
            if isinstance(entry, dict) and isinstance(entry.get("Id"), str) and entry["Id"]
        ]
        issues: list[dict[str, Any]] = []
        omitted = ids[MAX_CONTAINERS:]
        if omitted:
            issues.append(
                {
                    "source": SOURCE_DOCKER_CONTAINERS,
                    "code": "container_limit_reached",
                    "message": (
                        f"this host reports {len(ids)} containers and this observation "
                        f"inspects at most {MAX_CONTAINERS}"
                    ),
                    "detail": {"omitted": len(omitted), "limit": MAX_CONTAINERS},
                }
            )

        observations: list[ContainerObservation] = []
        for container_id in ids[:MAX_CONTAINERS]:
            try:
                inspected = self._get(_container_path(container_id, "/json"))
            except (DockerFailure, InvalidContainerId) as exc:
                issues.append(
                    {
                        "source": SOURCE_DOCKER_CONTAINERS,
                        "code": getattr(exc, "reason", "invalid_container_id"),
                        "message": str(exc),
                        "detail": {"container_id": container_id},
                    }
                )
                continue
            if not isinstance(inspected, dict):
                issues.append(
                    {
                        "source": SOURCE_DOCKER_CONTAINERS,
                        "code": "unexpected_shape",
                        "message": "the daemon's inspect answer was not an object",
                        "detail": {"container_id": container_id},
                    }
                )
                continue
            observations.append(_container_observation(inspected))

        return ContainerBatch(
            # `partial` is the honest word for a sweep that saw the estate and could not
            # read all of it. It is not `failed`: the containers that were read are real.
            status="ok" if not issues else "partial",
            started_at=started_at,
            completed_at=_now(),
            api_version=version.get("ApiVersion"),
            engine_version=version.get("Version"),
            containers=tuple(observations),
            issues=tuple(issues),
            detail={
                "socket": str(self._socket_path),
                "endpoints": [VERSION_PATH, CONTAINERS_PATH, "/containers/{id}/json"],
                "listed": len(ids),
                "inspected": len(observations),
                "omitted": list(_OMITTED_CONTAINERS),
            },
        )

    def container_logs(self, container_id: str, tail: int = DEFAULT_LOG_LINES) -> dict[str, Any]:
        """The most recent lines this container wrote, bounded twice.

        Bounded by lines, because that is what an operator asks for, and bounded again by
        bytes, because a container that writes very long lines would otherwise turn a
        request for two hundred of them into an unbounded read. Both bounds are reported
        when they bite.

        This is a *read of the host*, so it is not served from the store: LocalPlane keeps
        no copy of a container's output, and building one would be a log platform. There is
        no follow, no stream and no websocket.
        """
        lines = max(1, min(int(tail), MAX_LOG_LINES))
        query = {
            "stdout": "1",
            "stderr": "1",
            "timestamps": "1",
            "tail": str(lines),
        }
        raw, truncated = self._read_bytes(
            _container_path(container_id, "/logs"), query, MAX_LOG_BYTES
        )
        entries = _demultiplex(raw)
        return {
            "container_id": container_id,
            "requested_lines": lines,
            "line_count": len(entries),
            "truncated": truncated,
            "byte_limit": MAX_LOG_BYTES,
            "line_limit": MAX_LOG_LINES,
            "source": "docker.container.logs",
            "method": METHOD,
            "read_at": _now(),
            "lines": entries,
        }

    def container_stats(self, container_id: str) -> dict[str, Any]:
        """One current sample of what this container is using, normalised.

        ``stream=false`` and no ``one-shot``: the daemon reads twice about a second apart
        and returns the second sample with the first attached, which is what makes a CPU
        percentage computable at all. Asking for one shot would be faster and would leave
        ``cpu_percent`` permanently unanswerable.

        A snapshot, deliberately. There is no history here and no series: this build stores
        nothing about resource usage, because a metrics database is a product and not a
        field on a container.
        """
        payload = self._get(
            _container_path(container_id, "/stats"),
            {"stream": "false"},
            timeout_s=max(self._timeout_s, 10.0),
        )
        if not isinstance(payload, dict):
            raise DockerFailure(
                ProviderStatus.ERROR,
                "unexpected_shape",
                {"received": type(payload).__name__},
            )
        return _stats(container_id, payload)

    # --------------------------------------------------------------------- lifecycle

    def lifecycle(self, container_id: str, action: str) -> LifecycleResult:
        """Ask the daemon to start, stop or restart one container. The one call that writes.

        The verb is checked against the protocol's closed tuple before a request exists, and
        the path is built from the verb and a hexadecimal id — so there is no argument here
        through which some other Docker operation could be reached.

        **The outcome mapping is the whole safety argument**, and it follows the same rule
        the privileged helper follows: ``not_written`` is a *proof*, never an assumption.

        * ``204`` — the daemon carried the request out. ``written``.
        * ``304`` — the daemon says the container was already in that state and it did
          nothing. ``not_written``, and a genuine one.
        * ``400`` / ``404`` / ``409`` — the daemon refused on a precondition: the request
          was malformed, the container is gone, or its state does not permit this. Nothing
          was carried out and the answer says so. ``not_written``.
        * ``500`` — the daemon began the operation and reports that it failed. Whether
          anything took effect before it failed is not something the response answers, and
          guessing is exactly what this vocabulary exists to avoid. ``write_unknown``.
        * a transport failure **before** the request left this process — ``not_written``.
          After it left — ``write_unknown``.
        """
        if action not in DOCKER_LIFECYCLE_ACTIONS:
            raise ValueError(f"unsupported lifecycle action: {action!r}")
        query: dict[str, str] | None = (
            {"t": str(STOP_TIMEOUT_S)} if action in ("stop", "restart") else None
        )
        try:
            path = _container_path(container_id, f"/{action}")
        except InvalidContainerId as exc:
            return LifecycleResult(
                outcome="not_written",
                reason="invalid_container_id",
                action=action,
                container_id=container_id,
                detail={"error": str(exc)},
            )

        try:
            status, body = self._post(path, query)
        except DockerFailure as failure:
            return LifecycleResult(
                outcome="write_unknown" if failure.dispatched else "not_written",
                reason=failure.reason,
                action=action,
                container_id=container_id,
                http_status=failure.http_status,
                detail={**failure.detail, "dispatched": failure.dispatched},
            )

        detail: dict[str, Any] = {"daemon_message": _message(body), "dispatched": True}
        if status == 204:
            return LifecycleResult("written", "daemon_acknowledged", action, container_id,
                                   status, detail)
        if status == 304:
            return LifecycleResult("not_written", "already_in_that_state", action,
                                   container_id, status, detail)
        if status in (400, 404, 409):
            return LifecycleResult(
                "not_written",
                {400: "bad_request", 404: "container_not_found", 409: "conflict"}[status],
                action, container_id, status, detail,
            )
        return LifecycleResult("write_unknown", "daemon_error", action, container_id,
                               status, detail)

    # ------------------------------------------------------------------------ probes

    def probe_read(self) -> dict[str, Any]:
        """Establish that this agent can read the daemon. Raises :class:`DockerFailure`."""
        version = self._version()
        listed = self._get(CONTAINERS_PATH, {"all": "1", "limit": "1"})
        if not isinstance(listed, list):
            raise DockerFailure(
                ProviderStatus.ERROR, "unexpected_shape", {"received": type(listed).__name__}
            )
        return {
            "engine_version": version.get("Version"),
            "api_version": version.get("ApiVersion"),
            "os": version.get("Os"),
            "arch": version.get("Arch"),
        }

    def probe_lifecycle(self) -> dict[str, Any]:
        """Establish that this agent may *ask the daemon to act*, without asking it to.

        The problem this solves is real and has no other honest answer. Reading the socket
        does not establish that a lifecycle request would be accepted: a daemon published
        through a read-only proxy answers ``GET /containers/json`` and refuses a lifecycle
        ``POST``, and an agent that reported the capability anyway would publish executable
        plans that cannot execute.

        So the probe issues the request the capability is about — ``POST
        /containers/<id>/start`` — against an id that **cannot name a container**. It is 64
        hexadecimal characters and every one of them is ``e``, a constant in this file that
        nothing generates and nothing could collide with, because Docker ids come from a
        cryptographic random source. Docker answers ``404 No such container``, which proves
        that the request was routed and authorised and that nothing was started; a proxy
        that strips write verbs answers ``403`` or ``405``, which proves the opposite.

        Nothing is created, nothing is removed and no existing container is addressed. This
        is the one probe in LocalPlane that uses a mutating verb, and it is here because the
        alternative is to claim a capability nobody checked.
        """
        version = self._version()
        status, body = self._post(_container_path(PROBE_CONTAINER_ID, "/start"), None)
        return {
            "engine_version": version.get("Version"),
            "api_version": version.get("ApiVersion"),
            "probe_http_status": status,
            "probe_container_id": PROBE_CONTAINER_ID,
            "daemon_message": _message(body),
        }

    # --------------------------------------------------------------------- transport

    def _version(self) -> dict[str, Any]:
        payload = self._get(VERSION_PATH)
        return payload if isinstance(payload, dict) else {}

    def _get(
        self,
        path: str,
        query: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> Any:
        raw, _ = self._read_bytes(path, query, MAX_RESPONSE_BYTES, timeout_s=timeout_s)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DockerFailure(
                ProviderStatus.ERROR, "unparseable_response", {"path": path, "error": str(exc)}
            ) from exc

    def _read_bytes(
        self,
        path: str,
        query: dict[str, str] | None,
        limit: int,
        timeout_s: float | None = None,
    ) -> tuple[bytes, bool]:
        """One GET, with the body read to a ceiling. Returns ``(body, truncated)``."""
        status, body, truncated = self._request(
            "GET", path, query, limit, timeout_s or self._timeout_s
        )
        if status != 200:
            raise DockerFailure(
                ProviderStatus.ERROR,
                "unexpected_http_status",
                {"path": path, "http_status": status, "daemon_message": _message(body)},
                dispatched=True,
                http_status=status,
            )
        return body, truncated

    def _post(self, path: str, query: dict[str, str] | None) -> tuple[int, bytes]:
        """One POST. The status is returned rather than judged; judging is the caller's."""
        status, body, _ = self._request(
            "POST", path, query, MAX_RESPONSE_BYTES, self._lifecycle_timeout_s
        )
        return status, body

    def _request(
        self,
        verb: str,
        path: str,
        query: dict[str, str] | None,
        limit: int,
        timeout_s: float,
    ) -> tuple[int, bytes, bool]:
        """The only place this module opens a socket.

        ``dispatched`` flips the instant the connection is established and the request is
        about to be written. Everything before that point is a proof that the daemon was
        never given anything; everything after it is an open question, and on the lifecycle
        path the difference is what separates ``not_written`` from ``write_unknown``.
        """
        url = f"{path}?{urlencode(query)}" if query else path
        connection = _UnixHTTPConnection(str(self._socket_path), timeout_s)
        dispatched = False
        try:
            connection.connect()
            dispatched = True
            connection.request(verb, url, headers={"Host": "localhost", "Accept": "*/*"})
            response = connection.getresponse()
            body = response.read(limit + 1)
            status_code = response.status
        except PermissionError as exc:
            raise DockerFailure(
                ProviderStatus.UNAVAILABLE, "permission_denied", {"error": str(exc)},
                dispatched=dispatched,
            ) from exc
        except (ConnectionError, TimeoutError, socket.timeout) as exc:
            raise DockerFailure(
                ProviderStatus.UNAVAILABLE, "daemon_not_answering", {"error": str(exc)},
                dispatched=dispatched,
            ) from exc
        except OSError as exc:
            raise DockerFailure(
                ProviderStatus.UNAVAILABLE, "socket_error", {"error": str(exc)},
                dispatched=dispatched,
            ) from exc
        except http.client.HTTPException as exc:
            raise DockerFailure(
                ProviderStatus.ERROR, "http_protocol_error", {"error": str(exc)},
                dispatched=dispatched,
            ) from exc
        finally:
            connection.close()

        truncated = len(body) > limit
        return status_code, body[:limit], truncated

    def _unreadable(
        self, status: ProviderStatus, reason: str, observed_at: str, detail: dict[str, Any]
    ) -> ProviderReading:
        return ProviderReading(
            provider=self.provider,
            source=self.source,
            status=status,
            method=METHOD,
            observed_at=observed_at,
            reason=reason,
            detail=detail,
        )


#: The id the lifecycle probe addresses. Sixty-four ``e``s: syntactically a container id,
#: and one no daemon can be holding, because Docker mints ids from a cryptographic random
#: source. Declared as a constant so that "what does the probe touch" is answered by reading
#: one line rather than by trusting a generator.
PROBE_CONTAINER_ID = "e" * 64


# ------------------------------------------------------------------------ normalisation


def _container_observation(inspected: dict[str, Any]) -> ContainerObservation:
    """One inspect answer, reduced to the facts LocalPlane records.

    Every field here answers a question an operator asks of a running workload. What is left
    out is listed in :data:`_OMITTED_CONTAINERS` and it is left out for two reasons —
    ``Config.Env`` and command lines routinely carry credentials, and the rest is a dump.

    No judgement is made. Whether this container is healthy, whether LocalPlane may act on
    it and what its state means are all the backend's to decide; what happens here is that
    Docker's shape becomes LocalPlane's shape.
    """
    container_id = inspected.get("Id") or ""
    state = _obj(inspected, "State")
    config = _obj(inspected, "Config")
    host_config = _obj(inspected, "HostConfig")
    network_settings = _obj(inspected, "NetworkSettings")

    gaps: list[str] = []
    lifecycle_state = _text(state.get("Status"))
    if lifecycle_state is None:
        gaps.append("state")
    running = state.get("Running")
    if not isinstance(running, bool):
        running = None
        gaps.append("running")

    health = _health(state.get("Health"))
    labels, dropped = _labels(config.get("Labels"))

    facts: dict[str, Any] = {
        "container_id": container_id,
        "short_id": container_id[:12],
        # Docker prefixes every name with a slash. The prefix is a wire detail, not part of
        # the name an operator uses, and it is removed here rather than in four renderers.
        "name": _name(inspected.get("Name")),
        "image": _text(config.get("Image")),
        "image_id": _text(inspected.get("Image")),
        "created_at": _text(inspected.get("Created")),
        "state": lifecycle_state,
        "status_text": _text(state.get("Status")),
        "running": running,
        "paused": _bool(state.get("Paused")),
        "restarting": _bool(state.get("Restarting")),
        "exit_code": _int(state.get("ExitCode")),
        "error": _text(state.get("Error")),
        "oom_killed": _bool(state.get("OOMKilled")),
        "pid": _int(state.get("Pid")),
        # The two timestamps a restart is proven from. Docker writes them in RFC 3339 with
        # nanoseconds, and the zero value `0001-01-01T00:00:00Z` means "never".
        "started_at": _instant(state.get("StartedAt")),
        "finished_at": _instant(state.get("FinishedAt")),
        "restart_count": _int(inspected.get("RestartCount")),
        "restart_policy": _restart_policy(host_config.get("RestartPolicy")),
        "health": health,
        "network_mode": _text(host_config.get("NetworkMode")),
        "networks": _networks(network_settings.get("Networks")),
        "ports": _ports(network_settings.get("Ports")),
        "mounts": _mounts(inspected.get("Mounts")),
        "labels": labels,
        "labels_dropped": dropped,
        "log_driver": _log_driver(host_config.get("LogConfig")),
        "platform": _text(inspected.get("Platform")),
        "observed_at": _now(),
    }
    return ContainerObservation(
        container_id=container_id,
        facts=facts,
        gaps=tuple(gaps),
        # Raw, and deliberately only this. `State` is the block every runtime verdict rests
        # on, it is small, and keeping it means "why did LocalPlane call this failed" stays
        # answerable from the row rather than from a re-read of a host that has moved on.
        evidence={
            "source": SOURCE_DOCKER_CONTAINERS,
            "endpoint": "/containers/{id}/json",
            "state": {k: v for k, v in state.items() if k != "Health"},
            "omitted": list(_OMITTED_CONTAINERS),
        },
    )


def _health(health: Any) -> dict[str, Any]:
    """Docker's own health verdict, or the fact that the image declares no check."""
    if not isinstance(health, dict):
        return {"status": None, "failing_streak": None, "checked": False}
    return {
        "status": _text(health.get("Status")),
        "failing_streak": _int(health.get("FailingStreak")),
        "checked": True,
    }


def _restart_policy(policy: Any) -> dict[str, Any]:
    if not isinstance(policy, dict):
        return {"name": None, "maximum_retry_count": None}
    return {
        "name": _text(policy.get("Name")) or "no",
        "maximum_retry_count": _int(policy.get("MaximumRetryCount")),
    }


def _networks(networks: Any) -> list[dict[str, Any]]:
    """Which Docker networks this container is attached to, and with what address.

    The relationship Docker actually reports. LocalPlane does not invent one: a container
    that is attached to nothing has an empty list, and one on the host's namespace has no
    entry here at all — ``network_mode`` is where that is said.
    """
    if not isinstance(networks, dict):
        return []
    attached: list[dict[str, Any]] = []
    for name, entry in sorted(networks.items()):
        if not isinstance(entry, dict):
            continue
        attached.append(
            {
                "name": name,
                "network_id": _text(entry.get("NetworkID")),
                "ip_address": _text(entry.get("IPAddress")),
                "ipv6_address": _text(entry.get("GlobalIPv6Address")),
                "gateway": _text(entry.get("Gateway")),
                "mac_address": _text(entry.get("MacAddress")),
                "aliases": [a for a in (entry.get("Aliases") or []) if isinstance(a, str)],
            }
        )
    return attached


def _ports(ports: Any) -> list[dict[str, Any]]:
    """Published port bindings, one row per host binding, plus unpublished exposed ports."""
    if not isinstance(ports, dict):
        return []
    published: list[dict[str, Any]] = []
    for spec, bindings in sorted(ports.items()):
        port, _, protocol = str(spec).partition("/")
        if not isinstance(bindings, list) or not bindings:
            published.append(
                {
                    "container_port": _int(port),
                    "protocol": protocol or "tcp",
                    "host_ip": None,
                    "host_port": None,
                    "published": False,
                }
            )
            continue
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            published.append(
                {
                    "container_port": _int(port),
                    "protocol": protocol or "tcp",
                    "host_ip": _text(binding.get("HostIp")),
                    "host_port": _int(binding.get("HostPort")),
                    "published": True,
                }
            )
    return published


def _mounts(mounts: Any) -> list[dict[str, Any]]:
    """Where this container keeps state: named volumes and bind mounts alike."""
    if not isinstance(mounts, list):
        return []
    parsed: list[dict[str, Any]] = []
    for mount in mounts:
        if not isinstance(mount, dict):
            continue
        parsed.append(
            {
                "type": _text(mount.get("Type")),
                "name": _text(mount.get("Name")),
                "source": _text(mount.get("Source")),
                "destination": _text(mount.get("Destination")),
                "driver": _text(mount.get("Driver")),
                "mode": _text(mount.get("Mode")),
                "read_write": _bool(mount.get("RW")),
                "propagation": _text(mount.get("Propagation")),
            }
        )
    return parsed


def _labels(labels: Any) -> tuple[dict[str, str], int]:
    """The labels worth operating on, and a count of the ones dropped."""
    if not isinstance(labels, dict):
        return {}, 0
    kept: dict[str, str] = {}
    dropped = 0
    for key, value in sorted(labels.items()):
        if not isinstance(key, str) or not isinstance(value, str):
            dropped += 1
            continue
        if key in _LABEL_NAMES or key.startswith(_LABEL_PREFIXES):
            kept[key] = (
                value if len(value) <= _MAX_LABEL_VALUE else value[:_MAX_LABEL_VALUE] + "…"
            )
        else:
            dropped += 1
    return kept, dropped


def _log_driver(log_config: Any) -> str | None:
    if not isinstance(log_config, dict):
        return None
    return _text(log_config.get("Type"))


def _stats(container_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """One stats sample, reduced to the numbers an operator reads.

    The CPU percentage is Docker's own formula — the container's CPU delta over the system
    delta, times the number of CPUs — and it is ``None`` rather than zero when the sample
    the daemon returned has no previous reading to subtract, because zero is a number
    somebody would act on.
    """
    cpu = _obj(payload, "cpu_stats")
    precpu = _obj(payload, "precpu_stats")
    memory = _obj(payload, "memory_stats")
    gaps: list[str] = []

    cpu_percent = _cpu_percent(cpu, precpu)
    if cpu_percent is None:
        gaps.append("cpu_percent")

    usage = _int(memory.get("usage"))
    limit = _int(memory.get("limit"))
    # Docker's own `docker stats` subtracts the page cache from the reported usage, because
    # the raw number counts reclaimable cache and reads as a container using far more than
    # it needs. cgroup v2 names it `inactive_file`; v1 named it `cache`.
    detail = _obj(memory, "stats")
    cache = _int(detail.get("inactive_file"))
    if cache is None:
        cache = _int(detail.get("cache"))
    working_set = usage - cache if usage is not None and cache is not None else usage
    if usage is None:
        gaps.append("memory_usage")

    rx, tx = _network_totals(payload.get("networks"))
    read_bytes, write_bytes = _block_totals(payload.get("blkio_stats"))
    pids = _obj(payload, "pids_stats")

    return {
        "container_id": container_id,
        "read_at": _now(),
        "sampled_at": _text(payload.get("read")),
        "source": "docker.container.stats",
        "method": METHOD,
        "cpu_percent": cpu_percent,
        "online_cpus": _int(cpu.get("online_cpus")),
        "memory_usage_bytes": working_set,
        "memory_usage_raw_bytes": usage,
        "memory_limit_bytes": limit,
        "memory_percent": (
            round(working_set / limit * 100, 2)
            if working_set is not None and limit
            else None
        ),
        "network_rx_bytes": rx,
        "network_tx_bytes": tx,
        "block_read_bytes": read_bytes,
        "block_write_bytes": write_bytes,
        "pids": _int(pids.get("current")),
        "pids_limit": _int(pids.get("limit")),
        "gaps": gaps,
    }


def _cpu_percent(cpu: dict[str, Any], precpu: dict[str, Any]) -> float | None:
    usage = _obj(cpu, "cpu_usage")
    pre_usage = _obj(precpu, "cpu_usage")
    total = _int(usage.get("total_usage"))
    pre_total = _int(pre_usage.get("total_usage"))
    system = _int(cpu.get("system_cpu_usage"))
    pre_system = _int(precpu.get("system_cpu_usage"))
    if None in (total, pre_total, system, pre_system):
        return None
    cpu_delta = total - pre_total
    system_delta = system - pre_system
    if system_delta <= 0 or cpu_delta < 0:
        return None
    cpus = _int(cpu.get("online_cpus"))
    if not cpus:
        per_cpu = usage.get("percpu_usage")
        cpus = len(per_cpu) if isinstance(per_cpu, list) and per_cpu else 1
    return round(cpu_delta / system_delta * cpus * 100.0, 2)


def _network_totals(networks: Any) -> tuple[int | None, int | None]:
    if not isinstance(networks, dict) or not networks:
        return None, None
    rx = tx = 0
    for entry in networks.values():
        if not isinstance(entry, dict):
            continue
        rx += _int(entry.get("rx_bytes")) or 0
        tx += _int(entry.get("tx_bytes")) or 0
    return rx, tx


def _block_totals(blkio: Any) -> tuple[int | None, int | None]:
    if not isinstance(blkio, dict):
        return None, None
    entries = blkio.get("io_service_bytes_recursive")
    if not isinstance(entries, list) or not entries:
        return None, None
    read = write = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        operation = str(entry.get("op", "")).lower()
        value = _int(entry.get("value")) or 0
        if operation == "read":
            read += value
        elif operation == "write":
            write += value
    return read, write


def _demultiplex(raw: bytes) -> list[dict[str, Any]]:
    """Docker's log stream, in either of the two shapes it comes in.

    A container with a TTY gets the bytes raw. Without one, the daemon frames each chunk
    with an eight-byte header whose first byte is the stream — 1 for stdout, 2 for stderr —
    and whose last four are the payload length. Both shapes are handled because a caller
    should not have to know how the container was started to read what it wrote.

    ``timestamps=1`` is always requested, so each line begins with an RFC 3339 instant; it
    is split off here so that a reader gets a time rather than a prefix to parse.
    """
    entries: list[dict[str, Any]] = []
    if raw[:1] in (b"\x01", b"\x02") and len(raw) >= 8:
        offset = 0
        while offset + 8 <= len(raw):
            stream = raw[offset]
            length = int.from_bytes(raw[offset + 4 : offset + 8], "big")
            chunk = raw[offset + 8 : offset + 8 + length]
            offset += 8 + length
            entries.extend(_lines(chunk, "stdout" if stream == 1 else "stderr"))
        return entries
    return list(_lines(raw, "unknown"))


def _lines(chunk: bytes, stream: str) -> Iterable[dict[str, Any]]:
    for line in chunk.decode("utf-8", errors="replace").splitlines():
        if not line:
            continue
        timestamp, _, message = line.partition(" ")
        if _looks_like_instant(timestamp):
            yield {"timestamp": timestamp, "stream": stream, "message": message}
        else:
            yield {"timestamp": None, "stream": stream, "message": line}


def _looks_like_instant(value: str) -> bool:
    return len(value) >= 20 and value[4:5] == "-" and value[10:11] == "T"


def _network_record(entry: Any) -> dict[str, Any] | None:
    """Normalize one network. Anything without an id is not a record LocalPlane can use."""
    if not isinstance(entry, dict):
        return None
    network_id = entry.get("Id")
    name = entry.get("Name")
    if not isinstance(network_id, str) or not network_id:
        return None

    options = _obj(entry, "Options")
    labels = _obj(entry, "Labels")
    bridge_name = _text(options.get(OPTION_BRIDGE_NAME))
    default_bridge = str(options.get(OPTION_DEFAULT_BRIDGE, "")).lower() == "true"

    return {
        "network_id": network_id,
        "name": _text(name),
        "driver": _text(entry.get("Driver")),
        "scope": _text(entry.get("Scope")),
        "created_at": _text(entry.get("Created")),
        # Docker declares this only when something asked for a specific bridge name — the
        # default bridge does, user-defined networks usually do not. Absent is normal.
        "bridge_name": bridge_name,
        "default_bridge": default_bridge,
        "ipam": _ipam(entry.get("IPAM")),
        "compose_project": _text(labels.get(LABEL_COMPOSE_PROJECT)),
        "compose_network": _text(labels.get(LABEL_COMPOSE_NETWORK)),
    }


def _ipam(ipam: Any) -> list[dict[str, str | None]]:
    if not isinstance(ipam, dict):
        return []
    config = ipam.get("Config")
    if not isinstance(config, list):
        return []
    parsed: list[dict[str, str | None]] = []
    for item in config:
        if not isinstance(item, dict):
            continue
        subnet = item.get("Subnet")
        gateway = item.get("Gateway")
        if not isinstance(subnet, str) and not isinstance(gateway, str):
            continue
        parsed.append({"subnet": _text(subnet), "gateway": _text(gateway)})
    return parsed


def _message(body: bytes) -> str | None:
    """The daemon's own explanation, when it sent one. Never invented."""
    if not body:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body.decode("utf-8", errors="replace")[:512].strip() or None
    if isinstance(payload, dict) and isinstance(payload.get("message"), str):
        return payload["message"][:512]
    return None


def _name(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw:
        return None
    return raw[1:] if raw.startswith("/") else raw


def _instant(raw: Any) -> str | None:
    """A Docker timestamp, with its zero value reported as absent rather than as a date."""
    value = _text(raw)
    if value is None or value.startswith("0001-01-01"):
        return None
    return value


def _obj(mapping: Any, key: str) -> dict[str, Any]:
    """One nested object out of a decoded response, or an empty one.

    The daemon's JSON is nested and every level of it is a thing this module has to reach
    into without trusting. Written once so that "a block that is missing reads as empty"
    is one rule rather than thirteen copies of the same conditional.
    """
    if not isinstance(mapping, dict):
        return {}
    value = mapping.get(key)
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    """One string, or absent. An empty string is absent — Docker uses it for "none"."""
    return value if isinstance(value, str) and value else None


def _bool(value: Any) -> bool | None:
    """One boolean, or absent. ``0`` and ``""`` are not falsehoods here, they are gaps."""
    return value if isinstance(value, bool) else None


def _int(value: Any) -> int | None:
    """One integer, or absent.

    ``bool`` is refused although it is a subclass of ``int``: ``True`` where an exit code
    or a pid belongs is a malformed answer, not the number one. That rule is stated here
    and read from here, so the file has one meaning of "integer" rather than two.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def probe_peer_pidfd_support() -> dict[str, Any]:
    """Feature-detect the Linux socket primitive without reconstructing a PID."""
    left: socket.socket | None = None
    right: socket.socket | None = None
    pidfd: int | None = None
    validated_pidfd = False
    try:
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        pidfd = left.getsockopt(socket.SOL_SOCKET, SO_PEERPIDFD)
        _validate_pidfd_handle(pidfd)
        validated_pidfd = True
        credentials = _UCRED.unpack(
            left.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size)
        )
        visible_pid = _pidfd_visible_pid(pidfd, validate_handle=False)
        poller = select.poll()
        poller.register(pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
        if credentials[0] != visible_pid or poller.poll(0):
            return {
                "status": "unavailable",
                "reason": "so_peerpidfd_identity_or_liveness_mismatch",
            }
        return {
            "status": "available",
            "reason": "so_peerpidfd_available",
            "python_constant_exported": hasattr(socket, "SO_PEERPIDFD"),
            "numeric_pid_reconstruction_used": False,
        }
    except (OSError, ValueError, struct.error) as exc:
        return {
            "status": "unavailable",
            "reason": "so_peerpidfd_unavailable",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "numeric_pid_reconstruction_used": False,
        }
    finally:
        if validated_pidfd and pidfd is not None:
            try:
                os.close(pidfd)
            except OSError:
                pass
        if left is not None:
            left.close()
        if right is not None:
            right.close()


def _validate_pidfd_handle(pidfd: int) -> None:
    """Prove an integer returned by ``SO_PEERPIDFD`` names an actual pidfd.

    This check intentionally precedes every close-on-error path.  If the UAPI option
    number were wrong, LocalPlane must fail without closing an unrelated descriptor.
    """
    if isinstance(pidfd, bool) or not isinstance(pidfd, int) or pidfd < 0:
        raise ValueError("SO_PEERPIDFD did not return a non-negative descriptor")
    target = os.readlink(f"/proc/self/fd/{pidfd}")
    if target != "anon_inode:[pidfd]":
        raise ValueError("SO_PEERPIDFD result is not an anon_inode pidfd")


def _pidfd_visible_pid(pidfd: int, *, validate_handle: bool = True) -> int:
    """Read the one kernel fdinfo record needed to compare peer credentials."""
    if validate_handle:
        _validate_pidfd_handle(pidfd)
    found: int | None = None
    with open(f"/proc/self/fdinfo/{pidfd}", encoding="utf-8") as stream:
        for line in stream:
            name, separator, value = line.partition(":")
            if separator and name == "Pid":
                if found is not None:
                    raise ValueError("pidfd fdinfo contains duplicate Pid fields")
                try:
                    pid = int(value.strip())
                except ValueError as exc:
                    raise ValueError("pidfd Pid field is not an integer") from exc
                if pid <= 0:
                    raise ValueError("pidfd process is not visible in this PID namespace")
                found = pid
    if found is None:
        raise ValueError("pidfd Pid field is unavailable")
    return found


def _parse_api_version(value: str) -> tuple[int, int]:
    if not re.fullmatch(r"[0-9]+\.[0-9]+", value):
        raise DockerRuntimeOwnerFailure(
            "management_path.runtime_owner.engine_snapshot_changed",
            "docker_api_version_invalid",
            {"value": value},
        )
    major, minor = (int(part) for part in value.split(".", 1))
    return major, minor


def _select_runtime_api(maximum: str, minimum: str) -> str:
    engine_max = _parse_api_version(maximum)
    engine_min = _parse_api_version(minimum)
    lower = max(engine_min, RUNTIME_OWNER_API_MIN)
    upper = min(engine_max, RUNTIME_OWNER_API_MAX)
    if lower > upper:
        raise DockerRuntimeOwnerFailure(
            "management_path.runtime_owner.engine_snapshot_changed",
            "docker_api_version_unsupported",
            {
                "engine_minimum": minimum,
                "engine_maximum": maximum,
                "localplane_minimum": ".".join(map(str, RUNTIME_OWNER_API_MIN)),
                "localplane_maximum": ".".join(map(str, RUNTIME_OWNER_API_MAX)),
            },
        )
    return f"{upper[0]}.{upper[1]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
