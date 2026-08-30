"""The closed, fail-closed ``docker-direct-unix-v1`` runtime-owner contract."""

from __future__ import annotations

import errno
import hashlib
import http.server
import inspect
import json
import os
import socket
import socketserver
import struct
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import pytest

from localplane.agent.docker_direct_attestation import (
    DOCKER_DIRECT_ATTESTATION_RELATIVE_PATH,
    DockerDirectAttestationRead,
    DockerDirectAttestationReader,
    DockerDirectEngineAttestation,
)
from localplane.agent.docker_runtime_owner import (
    DockerRuntimeOwnerCorrelator,
    ProcessInspectionFailure,
    RuntimeOwnerCorrelation as AgentRuntimeOwnerCorrelation,
    _cgroup_relation,
)
from localplane.agent.providers.docker import (
    SO_PEERPIDFD,
    DockerPeerProcess,
    DockerProvider,
    DockerRuntimeInfo,
    DockerRuntimeOwnerFailure,
    DockerRuntimeOwnerSession,
    DockerRuntimeVersion,
    _pidfd_visible_pid,
    _validate_pidfd_handle,
)
from localplane.agent.providers.systemd import ProcessUnitResolution, UnitObservation
from localplane.agent.service import AgentService
from localplane.backend.domain.systemd_lifecycle import (
    RuntimeOwnerCorrelation,
    RuntimeOwnerStatus,
    SystemdLifecycleContext,
)
from localplane.protocol.docker_runtime_owner import (
    DOCKER_DIRECT_UNIX_CONTRACT,
    DOCKER_RUNTIME_OWNER_GAPS,
)
from localplane.protocol.wire import METHODS


_CONTAINER_ID = "a" * 64
_OTHER_CONTAINER_ID = "b" * 64
_STARTED_AT = "2026-08-28T10:00:00.123456789Z"
_INVOCATION_ID = "12" * 16
_ENGINE_ID = "opaque-engine-id"
_PEER_PID = 4321


def _attestation_payload(**overrides: str) -> dict[str, str]:
    value = {
        "contract": DOCKER_DIRECT_UNIX_CONTRACT,
        "runtime": "docker",
        "engine_transport": "direct_unix",
        "engine_endpoint": "/run/localplane-test-docker.sock",
        "engine_unit_id": "docker-engine.service",
        "engine_process_role": "service_main",
        "engine_scope": "host_system_manager",
        "engine_privilege": "rootful",
    }
    value.update(overrides)
    return value


def _write_attestation(
    tmp_path: Path,
    payload: dict[str, Any] | None = None,
) -> tuple[DockerDirectAttestationReader, Path, Path]:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    attestation_path = root.joinpath(*DOCKER_DIRECT_ATTESTATION_RELATIVE_PATH.parts)
    attestation_path.parent.mkdir(parents=True, mode=0o700)
    endpoint_parent = root / "run"
    endpoint_parent.mkdir(mode=0o700)
    attestation_path.write_text(
        json.dumps(payload or _attestation_payload()), encoding="utf-8"
    )
    attestation_path.chmod(0o600)
    for directory in (
        root,
        root / "etc",
        root / "etc/localplane",
        root / "etc/localplane/runtime-owner",
        endpoint_parent,
    ):
        directory.chmod(0o700)
    return (
        DockerDirectAttestationReader(
            filesystem_root=root,
            required_uid=os.geteuid(),
        ),
        attestation_path,
        root,
    )


def _trusted_attestation() -> DockerDirectEngineAttestation:
    return DockerDirectEngineAttestation(
        **_attestation_payload(),
        fingerprint="sha256:" + "c" * 64,
    )


def test_protected_root_owned_attestation_is_closed_and_fingerprinted(tmp_path: Path):
    assert DockerDirectAttestationReader()._required_uid == 0  # noqa: SLF001
    reader, path, _root = _write_attestation(tmp_path)
    read = reader.read()
    assert read.trusted and read.attestation is not None
    assert read.attestation.engine_endpoint == "/run/localplane-test-docker.sock"
    assert read.attestation.engine_unit_id == "docker-engine.service"
    assert read.attestation.fingerprint.startswith("sha256:")
    assert len(read.attestation.fingerprint) == 71
    assert read.attestation.fingerprint == (
        "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    )
    assert read.detail == {
        "path": str(path),
        "file_uid": os.geteuid(),
        "file_mode": 0o600,
        "schema_closed": True,
        "symlinks_followed": False,
    }


def test_attestation_wrong_owner_is_untrusted(tmp_path: Path):
    _reader, _path, root = _write_attestation(tmp_path)
    read = DockerDirectAttestationReader(
        filesystem_root=root,
        required_uid=os.geteuid() + 1,
    ).read()
    assert (read.status, read.reason) == ("untrusted", "attestation_untrusted")


@pytest.mark.parametrize("mode", [0o620, 0o602, 0o666])
def test_attestation_group_or_other_writable_is_untrusted(tmp_path: Path, mode: int):
    reader, path, _root = _write_attestation(tmp_path)
    path.chmod(mode)
    assert reader.read().reason == "attestation_untrusted"


def test_attestation_writable_directory_chain_is_untrusted(tmp_path: Path):
    reader, path, _root = _write_attestation(tmp_path)
    path.parent.chmod(0o770)
    assert reader.read().reason == "attestation_untrusted"


def test_attestation_rejects_writable_endpoint_namespace(tmp_path: Path):
    reader, _path, root = _write_attestation(tmp_path)
    (root / "run").chmod(0o777)
    assert reader.read().reason == "attestation_untrusted"


def test_attestation_file_symlink_is_never_followed(tmp_path: Path):
    reader, path, root = _write_attestation(tmp_path)
    target = root / "attacker.json"
    target.write_text(json.dumps(_attestation_payload()), encoding="utf-8")
    path.unlink()
    path.symlink_to(target)
    assert reader.read().reason == "attestation_untrusted"


def test_attestation_parent_symlink_is_never_followed(tmp_path: Path):
    reader, path, root = _write_attestation(tmp_path)
    replacement = root / "replacement"
    replacement.mkdir(mode=0o700)
    (replacement / path.name).write_text(
        json.dumps(_attestation_payload()), encoding="utf-8"
    )
    (replacement / path.name).chmod(0o600)
    path.unlink()
    path.parent.rmdir()
    path.parent.symlink_to(replacement)
    assert reader.read().reason == "attestation_untrusted"


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json",
        b"[]",
        b'{"contract":"docker-direct-unix-v1","contract":"other"}',
    ],
)
def test_attestation_malformed_or_duplicate_schema_is_untrusted(
    tmp_path: Path, raw: bytes
):
    reader, path, _root = _write_attestation(tmp_path)
    path.write_bytes(raw)
    assert reader.read().reason == "attestation_untrusted"


@pytest.mark.parametrize(
    "payload",
    [
        _attestation_payload(contract="docker-direct-unix-v2"),
        {**_attestation_payload(), "engine_pid": "42"},
        {key: value for key, value in _attestation_payload().items() if key != "runtime"},
        _attestation_payload(engine_endpoint="relative/docker.sock"),
        _attestation_payload(engine_endpoint="/run/../tmp/docker.sock"),
        _attestation_payload(engine_unit_id="docker-engine.scope"),
    ],
)
def test_attestation_unknown_or_authority_widening_schema_is_untrusted(
    tmp_path: Path, payload: dict[str, Any]
):
    reader, _path, _root = _write_attestation(tmp_path, payload)
    assert reader.read().reason == "attestation_untrusted"


def test_attestation_fingerprint_changes_with_authority_content(tmp_path: Path):
    reader, path, _root = _write_attestation(tmp_path)
    first = reader.read().attestation
    path.write_text(
        json.dumps(_attestation_payload(engine_unit_id="other-engine.service")),
        encoding="utf-8",
    )
    second = reader.read().attestation
    assert first is not None and second is not None
    assert first.fingerprint != second.fingerprint


def test_attestation_fingerprint_identifies_exact_opened_content(tmp_path: Path):
    reader, path, _root = _write_attestation(tmp_path)
    first = reader.read().attestation
    path.write_text(
        json.dumps(_attestation_payload(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    second = reader.read().attestation
    assert first is not None and second is not None
    assert first.as_dict() | {"fingerprint": None} == (
        second.as_dict() | {"fingerprint": None}
    )
    assert first.fingerprint != second.fingerprint


def test_production_attestation_path_ignores_observation_root_and_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("LOCALPLANE_ROOT", str(tmp_path / "attacker"))
    monkeypatch.setattr(AgentService, "_discover", lambda _self: ())
    service = AgentService(root=tmp_path / "alternate-observation-root")
    assert service._runtime_attestation.path == Path(  # noqa: SLF001 - trust-boundary test
        "/etc/localplane/runtime-owner/docker-direct-unix-v1.json"
    )
    assert not any("runtime_owner" in method for method in METHODS)
    source = inspect.getsource(
        __import__(
            "localplane.agent.docker_direct_attestation",
            fromlist=["DockerDirectAttestationReader"],
        )
    )
    assert "os.environ" not in source and "getenv(" not in source


class _PeerSocket:
    def __init__(
        self,
        source_pidfd: int,
        *,
        peer_pid: int | None = None,
        peer_uid: int = 0,
        pidfd_errno: int | None = None,
    ) -> None:
        self.source_pidfd = source_pidfd
        self.peer_pid = os.getpid() if peer_pid is None else peer_pid
        self.peer_uid = peer_uid
        self.pidfd_errno = pidfd_errno
        self.options: list[int] = []
        self.timeouts: list[float] = []
        self.issued_pidfds: list[int] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def getsockopt(self, _level: int, option: int, size: int | None = None):
        self.options.append(option)
        if option == SO_PEERPIDFD:
            if self.pidfd_errno is not None:
                raise OSError(self.pidfd_errno, os.strerror(self.pidfd_errno))
            issued = os.dup(self.source_pidfd)
            self.issued_pidfds.append(issued)
            return issued
        if option == socket.SO_PEERCRED:
            assert size == struct.calcsize("=3i")
            return struct.pack("=3i", self.peer_pid, self.peer_uid, self.peer_uid)
        raise AssertionError(f"unexpected socket option {option}")


class _Response:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        will_close: bool = False,
        connection: str | None = None,
        error: Exception | None = None,
        fully_consumed: bool = True,
    ) -> None:
        self.body = body
        self.status = status
        self.will_close = will_close
        self.connection = connection
        self.error = error
        self.fully_consumed = fully_consumed

    def read(self, _limit: int) -> bytes:
        if self.error is not None:
            raise self.error
        return self.body

    def getheader(self, name: str, default: str = "") -> str:
        return self.connection or default if name == "Connection" else default

    def isclosed(self) -> bool:
        return self.fully_consumed


class _PersistentConnection:
    def __init__(self, sock: _PeerSocket, responses: list[_Response]) -> None:
        self.sock: Any = sock
        self.connect_count = 1
        self.auto_open = 0
        self.responses = list(responses)
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.closed = False

    def request(self, verb: str, path: str, *, headers: dict[str, str]) -> None:
        self.requests.append((verb, path, headers))

    def getresponse(self) -> _Response:
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


def _json_response(value: Any, **kwargs: Any) -> _Response:
    return _Response(json.dumps(value).encode("utf-8"), **kwargs)


def _version_payload(**overrides: Any) -> dict[str, Any]:
    value = {
        "Version": "27.5.1",
        "ApiVersion": "1.47",
        "MinAPIVersion": "1.24",
        "Os": "linux",
        "Arch": "x86_64",
    }
    value.update(overrides)
    return value


def _info_payload(**overrides: Any) -> dict[str, Any]:
    value = {
        "ID": _ENGINE_ID,
        "ServerVersion": "27.5.1",
        "OSType": "linux",
        "CgroupDriver": "systemd",
        "CgroupVersion": "2",
        "LiveRestoreEnabled": False,
    }
    value.update(overrides)
    return value


def _inspect_payload(container_id: str = _CONTAINER_ID, **state: Any) -> dict[str, Any]:
    runtime_state = {
        "Running": True,
        "Pid": 5001,
        "StartedAt": _STARTED_AT,
    }
    runtime_state.update(state)
    return {
        "Id": container_id,
        "State": runtime_state,
        "HostConfig": {"NetworkMode": "host"},
    }


def _wired_session(responses: list[_Response]) -> tuple[
    DockerRuntimeOwnerSession, _PersistentConnection, _PeerSocket
]:
    source_pidfd = os.pidfd_open(os.getpid())
    peer_socket = _PeerSocket(source_pidfd)
    connection = _PersistentConnection(peer_socket, responses)
    session = DockerRuntimeOwnerSession(_trusted_attestation(), timeout_s=1)
    session._connection = connection  # noqa: SLF001 - exact transport seam
    session._socket = peer_socket  # noqa: SLF001 - exact transport seam
    session._peer = session._read_peer(peer_socket)  # noqa: SLF001
    session._deadline = session._monotonic() + 20  # noqa: SLF001
    os.close(source_pidfd)
    return session, connection, peer_socket


def test_runtime_snapshot_uses_one_connected_socket_and_only_fixed_reads():
    responses = [
        _json_response(_version_payload()),
        _json_response(_info_payload()),
        _json_response([{"Id": _CONTAINER_ID}]),
        _json_response(_inspect_payload()),
        _json_response(_inspect_payload()),
        _json_response(_info_payload()),
        _json_response(_version_payload()),
        _json_response(_inspect_payload()),
    ]
    session, connection, peer_socket = _wired_session(responses)
    try:
        version_a = session.read_version()
        info_a = session.read_info()
        assert session.list_container_ids() == (_CONTAINER_ID,)
        assert session.inspect_listed_container(_CONTAINER_ID)["Id"] == _CONTAINER_ID
        assert session.inspect_listed_container(_CONTAINER_ID)["Id"] == _CONTAINER_ID
        assert session.read_info() == info_a
        assert session.read_version() == version_a
        assert session.inspect_listed_container(_CONTAINER_ID)["Id"] == _CONTAINER_ID
        session.require_peer_live()
    finally:
        session.close()
    assert connection.connect_count == 1 and connection.closed
    assert [request[:2] for request in connection.requests] == [
        ("GET", "/version"),
        ("GET", "/v1.47/info"),
        ("GET", "/v1.47/containers/json?all=1"),
        ("GET", f"/v1.47/containers/{_CONTAINER_ID}/json"),
        ("GET", f"/v1.47/containers/{_CONTAINER_ID}/json"),
        ("GET", "/v1.47/info"),
        ("GET", "/version"),
        ("GET", f"/v1.47/containers/{_CONTAINER_ID}/json"),
    ]
    assert all(request[2]["Connection"] == "keep-alive" for request in connection.requests)
    assert SO_PEERPIDFD in peer_socket.options


def test_runtime_peer_pidfd_is_from_exact_socket_and_peercred_must_agree(
    monkeypatch: pytest.MonkeyPatch,
):
    source_pidfd = os.pidfd_open(os.getpid())
    session = DockerRuntimeOwnerSession(_trusted_attestation(), timeout_s=1)
    peer_socket = _PeerSocket(source_pidfd)
    with monkeypatch.context() as scoped:
        scoped.setattr(
            os,
            "pidfd_open",
            lambda *_args: (_ for _ in ()).throw(
                AssertionError("numeric reconstruction")
            ),
        )
        peer = session._read_peer(peer_socket)  # noqa: SLF001
        try:
            assert peer.pid == os.getpid() and peer.uid == 0
            assert peer_socket.options == [socket.SO_PEERCRED, SO_PEERPIDFD]
        finally:
            os.close(peer.pidfd)
            os.close(source_pidfd)

    source_pidfd = os.pidfd_open(os.getpid())
    try:
        mismatch = _PeerSocket(source_pidfd, peer_pid=os.getpid() + 1)
        with pytest.raises(DockerRuntimeOwnerFailure) as caught:
            session._read_peer(mismatch)  # noqa: SLF001
        assert caught.value.gap.endswith("pid_namespace_mismatch")
    finally:
        os.close(source_pidfd)


def test_so_peerpidfd_absence_never_falls_back_to_numeric_pid():
    source_pidfd = os.pidfd_open(os.getpid())
    session = DockerRuntimeOwnerSession(_trusted_attestation(), timeout_s=1)
    try:
        with pytest.raises(DockerRuntimeOwnerFailure) as caught:
            session._read_peer(  # noqa: SLF001
                _PeerSocket(source_pidfd, pidfd_errno=errno.ENOPROTOOPT)
            )
        assert caught.value.gap == "management_path.runtime_owner.peer_pidfd_unavailable"
    finally:
        os.close(source_pidfd)


@pytest.mark.parametrize("peer_pid", [0, -1])
def test_zero_or_negative_peercred_pid_is_rejected_before_pidfd_use(peer_pid: int):
    source_pidfd = os.pidfd_open(os.getpid())
    peer_socket = _PeerSocket(source_pidfd, peer_pid=peer_pid)
    session = DockerRuntimeOwnerSession(_trusted_attestation(), timeout_s=1)
    try:
        with pytest.raises(DockerRuntimeOwnerFailure) as caught:
            session._read_peer(peer_socket)  # noqa: SLF001
        assert caught.value.reason == "docker_peer_credentials_pid_invalid"
        assert peer_socket.options == [socket.SO_PEERCRED]
    finally:
        os.close(source_pidfd)


def test_so_peerpidfd_result_must_be_an_actual_pidfd_before_close():
    unrelated = os.open("/dev/null", os.O_RDONLY)
    peer_socket = _PeerSocket(unrelated)
    session = DockerRuntimeOwnerSession(_trusted_attestation(), timeout_s=1)
    try:
        with pytest.raises(DockerRuntimeOwnerFailure) as caught:
            session._read_peer(peer_socket)  # noqa: SLF001
        assert caught.value.gap == "management_path.runtime_owner.peer_pidfd_unavailable"
        assert caught.value.reason == "so_peerpidfd_result_is_not_pidfd"
        issued = peer_socket.issued_pidfds[0]
        # LocalPlane did not close a descriptor it could not prove was returned as a new
        # pidfd.  The test owns the simulated value and cleans it up explicitly.
        assert os.fstat(issued).st_mode
        os.close(issued)
    finally:
        os.close(unrelated)


def test_pidfd_visible_pid_must_be_positive_and_validated_fd_closes_once(
    monkeypatch: pytest.MonkeyPatch,
):
    import localplane.agent.providers.docker as docker_module

    source_pidfd = os.pidfd_open(os.getpid())
    peer_socket = _PeerSocket(source_pidfd)
    session = DockerRuntimeOwnerSession(_trusted_attestation(), timeout_s=1)
    monkeypatch.setattr(
        docker_module,
        "_pidfd_visible_pid",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("pidfd process is not visible in this PID namespace")
        ),
    )
    try:
        with pytest.raises(DockerRuntimeOwnerFailure) as caught:
            session._read_peer(peer_socket)  # noqa: SLF001
        assert caught.value.gap == "management_path.runtime_owner.pid_namespace_mismatch"
        issued = peer_socket.issued_pidfds[0]
        with pytest.raises(OSError) as closed:
            os.fstat(issued)
        assert closed.value.errno == errno.EBADF
    finally:
        os.close(source_pidfd)


def test_pidfd_validation_and_failure_paths_do_not_leak_or_double_close():
    before = len(os.listdir("/proc/self/fd"))
    source_pidfd = os.pidfd_open(os.getpid())
    try:
        _validate_pidfd_handle(source_pidfd)
        session = DockerRuntimeOwnerSession(_trusted_attestation(), timeout_s=1)
        for _ in range(40):
            peer = session._read_peer(_PeerSocket(source_pidfd))  # noqa: SLF001
            os.close(peer.pidfd)
        for _ in range(40):
            with pytest.raises(DockerRuntimeOwnerFailure):
                session._read_peer(  # noqa: SLF001
                    _PeerSocket(source_pidfd, peer_uid=1000)
                )
    finally:
        os.close(source_pidfd)
    assert len(os.listdir("/proc/self/fd")) == before


def test_exited_pidfd_is_detected_without_waitid_or_reaping_in_liveness_path():
    child = subprocess.Popen(["/bin/true"])
    pidfd = os.pidfd_open(child.pid)
    child.wait(timeout=2)
    session = DockerRuntimeOwnerSession(_trusted_attestation(), timeout_s=1)
    session._peer = DockerPeerProcess(  # noqa: SLF001
        pidfd=pidfd,
        pid=child.pid,
        uid=0,
        gid=0,
    )
    session._deadline = session._monotonic() + 2  # noqa: SLF001
    try:
        with pytest.raises(DockerRuntimeOwnerFailure) as caught:
            session.require_peer_live()
        assert caught.value.gap.endswith("peer_process_exited")
        assert "waitid" not in inspect.getsource(session.require_peer_live)
        assert "wait(" not in inspect.getsource(session.require_peer_live)
    finally:
        session.close()


def test_non_root_peer_is_unattested():
    source_pidfd = os.pidfd_open(os.getpid())
    session = DockerRuntimeOwnerSession(_trusted_attestation(), timeout_s=1)
    try:
        with pytest.raises(DockerRuntimeOwnerFailure) as caught:
            session._read_peer(_PeerSocket(source_pidfd, peer_uid=1000))  # noqa: SLF001
        assert caught.value.reason == "docker_peer_not_rootful"
    finally:
        os.close(source_pidfd)


@pytest.mark.parametrize(
    ("response", "expected_gap"),
    [
        (_Response(b"{}", status=500), "engine_snapshot_changed"),
        (_Response(b"{}", will_close=True), "transport_reconnected"),
        (_Response(b"{}", connection="close"), "transport_reconnected"),
        (
            _Response(b"{}", connection="keep-alive, Close"),
            "transport_reconnected",
        ),
        (
            _Response(b"{}", fully_consumed=False),
            "engine_snapshot_changed",
        ),
        (_Response(b"not-json"), "engine_snapshot_changed"),
        (_Response(b"", error=TimeoutError("timeout")), "transport_reconnected"),
        (_Response(b"", error=ConnectionResetError("reset")), "transport_reconnected"),
    ],
)
def test_runtime_http_failures_are_incomplete(
    response: _Response, expected_gap: str
):
    session, _connection, _socket = _wired_session([response])
    try:
        with pytest.raises(DockerRuntimeOwnerFailure) as caught:
            session.read_version()
        assert caught.value.gap.endswith(expected_gap)
    finally:
        session.close()


def test_runtime_response_limit_is_bounded(monkeypatch: pytest.MonkeyPatch):
    import localplane.agent.providers.docker as docker_module

    monkeypatch.setattr(docker_module, "MAX_RESPONSE_BYTES", 16)
    session, _connection, _socket = _wired_session([_Response(b"x" * 17)])
    try:
        with pytest.raises(DockerRuntimeOwnerFailure) as caught:
            session.read_version()
        assert caught.value.reason == "runtime_response_limit_exceeded"
    finally:
        session.close()


def test_runtime_reconnect_and_socket_replacement_are_rejected():
    session, connection, _socket = _wired_session([_json_response(_version_payload())])
    connection.connect_count = 2
    try:
        with pytest.raises(DockerRuntimeOwnerFailure) as caught:
            session.read_version()
        assert caught.value.gap.endswith("transport_reconnected")
    finally:
        session.close()


def test_http_client_sock_none_cannot_trigger_implicit_auto_open_reconnect():
    session, connection, _socket = _wired_session(
        [_json_response(_version_payload())]
    )
    connection.sock = None
    try:
        with pytest.raises(DockerRuntimeOwnerFailure) as caught:
            session.read_version()
        assert caught.value.gap.endswith("transport_reconnected")
        assert connection.requests == []
    finally:
        session.close()


def test_runtime_snapshot_has_one_fixed_wall_clock_deadline():
    session, connection, _socket = _wired_session(
        [_json_response(_version_payload())]
    )
    session._deadline = 10.0  # noqa: SLF001
    session._monotonic = lambda: 10.001  # noqa: SLF001
    try:
        with pytest.raises(DockerRuntimeOwnerFailure) as caught:
            session.read_version()
        assert caught.value.reason == "docker_runtime_snapshot_deadline_exceeded"
        assert connection.requests == []
    finally:
        session.close()


def test_whole_snapshot_deadline_is_enforced_by_final_liveness_check():
    session, _connection, _socket = _wired_session([])
    session._deadline = 10.0  # noqa: SLF001
    session._monotonic = lambda: 10.001  # noqa: SLF001
    try:
        with pytest.raises(DockerRuntimeOwnerFailure) as caught:
            session.require_peer_live()
        assert caught.value.reason == "docker_runtime_snapshot_deadline_exceeded"
    finally:
        session.close()


def test_only_daemon_enumerated_full_container_ids_may_be_inspected():
    session, connection, _socket = _wired_session(
        [_json_response([{"Id": _CONTAINER_ID}])]
    )
    session._selected_api = "1.47"  # noqa: SLF001 - negotiated state seam
    try:
        assert session.list_container_ids() == (_CONTAINER_ID,)
        with pytest.raises(DockerRuntimeOwnerFailure) as caught:
            session.inspect_listed_container(_OTHER_CONTAINER_ID)
        assert caught.value.reason == "container_id_not_from_daemon_enumeration"
        assert len(connection.requests) == 1
    finally:
        session.close()


def test_daemon_enumeration_limit_and_disappeared_candidate_are_typed():
    too_many = [{"Id": f"{number:064x}"} for number in range(201)]
    session, _connection, _socket = _wired_session([_json_response(too_many)])
    session._selected_api = "1.47"  # noqa: SLF001
    try:
        with pytest.raises(DockerRuntimeOwnerFailure) as caught:
            session.list_container_ids()
        assert caught.value.gap.endswith("container_enumeration_incomplete")
    finally:
        session.close()

    session, _connection, _socket = _wired_session(
        [
            _json_response([{"Id": _CONTAINER_ID}]),
            _json_response({"message": "gone"}, status=404),
        ]
    )
    session._selected_api = "1.47"  # noqa: SLF001
    try:
        session.list_container_ids()
        with pytest.raises(DockerRuntimeOwnerFailure) as caught:
            session.inspect_listed_container(_CONTAINER_ID)
        assert caught.value.gap.endswith("container_changed")
        assert caught.value.reason == "daemon_listed_container_disappeared"
    finally:
        session.close()


@pytest.mark.parametrize(
    "payload",
    [
        [{"Id": "short"}],
        [{"Id": _CONTAINER_ID}, {"Id": _CONTAINER_ID}],
        {"Id": _CONTAINER_ID},
    ],
)
def test_container_enumeration_is_exact_and_unambiguous(payload: Any):
    session, _connection, _socket = _wired_session([_json_response(payload)])
    session._selected_api = "1.47"  # noqa: SLF001
    try:
        with pytest.raises(DockerRuntimeOwnerFailure):
            session.list_container_ids()
    finally:
        session.close()


def test_headers_cannot_supply_runtime_identity():
    session, connection, _socket = _wired_session([_json_response(_version_payload())])
    try:
        session.read_version()
    finally:
        session.close()
    sent_headers = connection.requests[0][2]
    assert set(sent_headers) == {"Host", "Accept", "Connection"}
    assert not any("pid" in name.lower() or "unit" in name.lower() for name in sent_headers)


def test_missing_or_wrong_endpoint_is_direct_transport_unproven(tmp_path: Path):
    attestation = replace(
        _trusted_attestation(),
        engine_endpoint=str(tmp_path / "missing.sock"),
    )
    with pytest.raises(DockerRuntimeOwnerFailure) as caught:
        DockerRuntimeOwnerSession(attestation, timeout_s=1).__enter__()
    assert caught.value.gap.endswith("direct_transport_unproven")
    assert caught.value.reason == "docker_socket_absent"


def test_runtime_session_has_no_generic_http_or_caller_identity_surface():
    public = {
        name: tuple(inspect.signature(member).parameters)
        for name, member in inspect.getmembers(
            DockerRuntimeOwnerSession, predicate=inspect.isfunction
        )
        if not name.startswith("_")
    }
    assert public == {
        "close": ("self",),
        "inspect_listed_container": ("self", "container_id"),
        "list_container_ids": ("self",),
        "read_info": ("self",),
        "read_version": ("self",),
        "require_peer_live": ("self",),
    }
    # The only container argument is internally gated by the daemon-derived list.
    assert "open_runtime_owner_snapshot" in vars(DockerProvider)


class _OneConnectionEngine(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str) -> None:
        self.accepts = 0
        self.requests: list[str] = []
        super().__init__(path, _PersistentEngineHandler)

    def get_request(self):
        request = super().get_request()
        self.accepts += 1
        return request


class _PersistentEngineHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def address_string(self) -> str:
        return "unix"

    def log_message(self, *_args: Any) -> None:
        return

    def do_GET(self) -> None:
        self.server.requests.append(self.path)
        path = self.path.partition("?")[0]
        if path == "/version":
            body = _version_payload()
        elif path == "/v1.47/info":
            body = _info_payload()
        elif path == "/v1.47/containers/json":
            body = [{"Id": _CONTAINER_ID}]
        elif path == f"/v1.47/containers/{_CONTAINER_ID}/json":
            body = _inspect_payload()
        else:
            self.send_error(404)
            return
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def test_successful_real_http_snapshot_has_exactly_one_server_accept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    endpoint = tmp_path / "engine.sock"
    server = _OneConnectionEngine(str(endpoint))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    attestation = replace(
        _trusted_attestation(),
        engine_endpoint=str(endpoint),
    )

    def trusted_test_peer(_self: Any, connected: socket.socket) -> DockerPeerProcess:
        pid, uid, gid = struct.unpack(
            "=3i",
            connected.getsockopt(
                socket.SOL_SOCKET,
                socket.SO_PEERCRED,
                struct.calcsize("=3i"),
            ),
        )
        pidfd = connected.getsockopt(socket.SOL_SOCKET, SO_PEERPIDFD)
        _validate_pidfd_handle(pidfd)
        assert pid == os.getpid() == _pidfd_visible_pid(pidfd)
        assert uid == os.geteuid()
        # Only this fixture's process lacks the production contract's root UID.  All
        # transport and pidfd facts above are still taken from the actual accepted socket.
        return DockerPeerProcess(pidfd=pidfd, pid=pid, uid=0, gid=gid)

    monkeypatch.setattr(DockerRuntimeOwnerSession, "_read_peer", trusted_test_peer)
    try:
        with DockerRuntimeOwnerSession(attestation, timeout_s=2) as session:
            session.read_version()
            session.read_info()
            assert session.list_container_ids() == (_CONTAINER_ID,)
            session.inspect_listed_container(_CONTAINER_ID)
            session.inspect_listed_container(_CONTAINER_ID)
            session.read_info()
            session.read_version()
            session.inspect_listed_container(_CONTAINER_ID)
        assert server.accepts == 1
        assert len(server.requests) == 8
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        endpoint.unlink(missing_ok=True)


def test_unix_peer_identity_is_listener_creator_not_accepting_process(tmp_path: Path):
    """Exercise the kernel fact that makes inherited/socket-activated listeners fail."""
    endpoint = tmp_path / "listener.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(endpoint))
    listener.listen(1)
    acceptor = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import socket,sys; "
                "s=socket.socket(fileno=int(sys.argv[1])); "
                "c,_=s.accept(); c.recv(1); c.sendall(b'x'); c.close()"
            ),
            str(listener.fileno()),
        ],
        pass_fds=(listener.fileno(),),
    )
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    pidfd: int | None = None
    unsupported: OSError | None = None
    try:
        client.connect(str(endpoint))
        peer_pid, _uid, _gid = struct.unpack(
            "=3i",
            client.getsockopt(
                socket.SOL_SOCKET,
                socket.SO_PEERCRED,
                struct.calcsize("=3i"),
            ),
        )
        try:
            pidfd = client.getsockopt(socket.SOL_SOCKET, SO_PEERPIDFD)
        except OSError as exc:
            unsupported = exc
        client.sendall(b"x")
        assert client.recv(1) == b"x"
        assert peer_pid == os.getpid()
        assert peer_pid != acceptor.pid
        if pidfd is not None:
            _validate_pidfd_handle(pidfd)
            assert _pidfd_visible_pid(pidfd) == os.getpid()
    finally:
        if pidfd is not None:
            os.close(pidfd)
        client.close()
        listener.close()
        try:
            acceptor.wait(timeout=2)
        except subprocess.TimeoutExpired:
            acceptor.kill()
            acceptor.wait(timeout=2)
        endpoint.unlink(missing_ok=True)
    if unsupported is not None:
        pytest.skip(f"SO_PEERPIDFD unavailable: {unsupported}")


class _AttestationReader:
    def __init__(
        self,
        reads: list[DockerDirectAttestationRead] | None = None,
    ) -> None:
        trusted = DockerDirectAttestationRead(
            status="trusted",
            reason="docker_direct_unix_attestation_trusted",
            attestation=_trusted_attestation(),
        )
        self.reads = list(reads or [trusted, trusted])
        self.calls = 0

    def read(self) -> DockerDirectAttestationRead:
        selected = self.reads[min(self.calls, len(self.reads) - 1)]
        self.calls += 1
        return selected


class _Pinned:
    def __init__(
        self,
        pid: int,
        cgroup_path: str,
        *,
        stable: bool = True,
        observer_pid_namespace: bool = True,
    ) -> None:
        self.pid = pid
        self.pidfd = pid + 10000
        self.cgroup_path = cgroup_path
        self._stable = stable
        self._observer_pid_namespace = observer_pid_namespace
        self.closed = False
        self.close_calls = 0

    def stable(self) -> bool:
        return self._stable and not self.closed

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True

    def shares_observer_pid_namespace(self) -> bool:
        return self._observer_pid_namespace

    def as_evidence(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "cgroup_path": self.cgroup_path,
            "pidfd_number_persisted_as_identity": False,
        }


class _ProcessInspector:
    def __init__(
        self,
        cgroups: dict[int, str] | None = None,
        *,
        unstable_pids: set[int] | None = None,
        failure: ProcessInspectionFailure | None = None,
        accepted_identity_changes: bool = False,
        peer_namespace_compatible: bool = True,
    ) -> None:
        self.cgroups = cgroups or {
            _PEER_PID: "/system.slice/docker-engine.service",
            5001: f"/docker/{_CONTAINER_ID}",
            5002: f"/docker/{_CONTAINER_ID}/nested/{_OTHER_CONTAINER_ID}",
        }
        self.unstable_pids = unstable_pids or set()
        self.failure = failure
        self.accepted_identity_changes = accepted_identity_changes
        self.peer_namespace_compatible = peer_namespace_compatible
        self.identity_calls = 0
        self.pins: list[_Pinned] = []

    def cgroup_identity(self, path: str) -> tuple[int, int]:
        if self.failure is not None:
            raise self.failure
        self.identity_calls += 1
        generation = self.identity_calls if self.accepted_identity_changes else 1
        return (7, hash((path, generation)))

    def inspect_existing(self, pid: int, _pidfd: int) -> _Pinned:
        if self.failure is not None:
            raise self.failure
        pinned = _Pinned(
            pid,
            self.cgroups[pid],
            stable=pid not in self.unstable_pids,
            observer_pid_namespace=(
                self.peer_namespace_compatible if pid == _PEER_PID else False
            ),
        )
        self.pins.append(pinned)
        return pinned

    def pin(self, pid: int) -> _Pinned:
        return self.inspect_existing(pid, pid + 10000)


class _RuntimeSession:
    def __init__(
        self,
        *,
        listed: tuple[str, ...] = (_CONTAINER_ID,),
        inspections: dict[
            str, list[dict[str, Any] | DockerRuntimeOwnerFailure]
        ]
        | None = None,
        versions: list[DockerRuntimeVersion] | None = None,
        infos: list[DockerRuntimeInfo] | None = None,
        enter_failure: DockerRuntimeOwnerFailure | None = None,
        final_peer_failure: DockerRuntimeOwnerFailure | None = None,
        inspect_hook: Callable[[str, int], None] | None = None,
    ) -> None:
        self.peer = DockerPeerProcess(pidfd=88, pid=_PEER_PID, uid=0, gid=0)
        self.selected_api_version = "1.47"
        self.listed = listed
        self.inspections = inspections or {
            _CONTAINER_ID: [
                _inspect_payload(),
                _inspect_payload(),
                _inspect_payload(),
            ]
        }
        self.versions = versions or [
            DockerRuntimeVersion("27.5.1", "1.47", "1.24", "linux", "x86_64"),
            DockerRuntimeVersion("27.5.1", "1.47", "1.24", "linux", "x86_64"),
        ]
        self.infos = infos or [
            DockerRuntimeInfo(_ENGINE_ID, "27.5.1", "linux", "systemd", "2", False),
            DockerRuntimeInfo(_ENGINE_ID, "27.5.1", "linux", "systemd", "2", False),
        ]
        self.enter_failure = enter_failure
        self.final_peer_failure = final_peer_failure
        self.inspect_hook = inspect_hook
        self.inspect_calls: dict[str, int] = {}
        self.list_calls = 0
        self.version_calls = 0
        self.info_calls = 0
        self.peer_checks = 0
        self.closed = False
        self.events: list[str] = []

    def __enter__(self) -> "_RuntimeSession":
        if self.enter_failure is not None:
            raise self.enter_failure
        return self

    def __exit__(self, *_args: Any) -> None:
        self.closed = True

    def read_version(self) -> DockerRuntimeVersion:
        self.events.append("version")
        value = self.versions[min(self.version_calls, len(self.versions) - 1)]
        self.version_calls += 1
        return value

    def read_info(self) -> DockerRuntimeInfo:
        self.events.append("info")
        value = self.infos[min(self.info_calls, len(self.infos) - 1)]
        self.info_calls += 1
        return value

    def list_container_ids(self) -> tuple[str, ...]:
        self.events.append("list")
        self.list_calls += 1
        return self.listed

    def inspect_listed_container(self, container_id: str) -> dict[str, Any]:
        assert container_id in self.listed
        call = self.inspect_calls.get(container_id, 0)
        self.inspect_calls[container_id] = call + 1
        self.events.append(f"inspect:{container_id}")
        if self.inspect_hook is not None:
            self.inspect_hook(container_id, call + 1)
        values = self.inspections[container_id]
        value = values[min(call, len(values) - 1)]
        if isinstance(value, DockerRuntimeOwnerFailure):
            raise value
        return value

    def require_peer_live(self) -> None:
        self.peer_checks += 1
        if self.final_peer_failure is not None:
            raise self.final_peer_failure


class _Docker:
    def __init__(self, session: _RuntimeSession) -> None:
        self.session = session
        self.attestations: list[DockerDirectEngineAttestation] = []

    def open_runtime_owner_snapshot(
        self, attestation: DockerDirectEngineAttestation
    ) -> _RuntimeSession:
        self.attestations.append(attestation)
        return self.session


def _unit_resolution(
    *,
    unit_id: str = "docker-engine.service",
    unit_type: str = "service",
    invocation_id: str = _INVOCATION_ID,
    main_pid: int = _PEER_PID,
    active_state: str = "active",
    load_state: str = "loaded",
    transient: bool = False,
    current_job: dict[str, Any] | None = None,
    control_group: str = "/system.slice/docker-engine.service",
    status: str = "resolved",
    reason: str | None = None,
) -> ProcessUnitResolution:
    unit = None
    if status == "resolved":
        unit = UnitObservation(
            canonical_id=unit_id,
            observed_at="2026-08-28T10:00:00+00:00",
            facts={
                "canonical_id": unit_id,
                "unit_type": unit_type,
                "load_state": load_state,
                "active_state": active_state,
                "sub_state": "running",
                "transient": transient,
                "current_job": current_job,
                "invocation_id": invocation_id,
                "service": {
                    "main_pid": main_pid,
                    "control_group": control_group,
                },
            },
        )
    return ProcessUnitResolution(
        status=status,
        observed_at="2026-08-28T10:00:00+00:00",
        canonical_id=unit_id if unit is not None else None,
        invocation_id=invocation_id if unit is not None else None,
        unit=unit,
        reason=reason,
    )


class _Systemd:
    def __init__(self, resolutions: list[ProcessUnitResolution] | None = None) -> None:
        self.resolutions = resolutions or [_unit_resolution(), _unit_resolution()]
        self.calls: list[int] = []

    def resolve_process_unit_pidfd(self, pidfd: int) -> ProcessUnitResolution:
        self.calls.append(pidfd)
        return self.resolutions[min(len(self.calls) - 1, len(self.resolutions) - 1)]


def _correlate(
    *,
    session: _RuntimeSession | None = None,
    reader: _AttestationReader | None = None,
    processes: _ProcessInspector | None = None,
    systemd: _Systemd | None = None,
    accepted_cgroup: str | None = None,
):
    session = session or _RuntimeSession()
    reader = reader or _AttestationReader()
    processes = processes or _ProcessInspector()
    systemd = systemd or _Systemd()
    accepted = accepted_cgroup or f"/docker/{_CONTAINER_ID}/backend.scope"
    result = DockerRuntimeOwnerCorrelator(
        docker=_Docker(session),
        systemd=systemd,
        attestation_reader=reader,
        process_inspector=processes,
    ).correlate(accepted)
    return result, session, reader, processes, systemd


@pytest.mark.parametrize("live_restore", [False, True])
def test_complete_direct_engine_conjunction_resolves_owner(live_restore: bool):
    info = DockerRuntimeInfo(
        _ENGINE_ID,
        "27.5.1",
        "linux",
        "systemd",
        "2",
        live_restore,
    )
    result, session, reader, processes, systemd = _correlate(
        session=_RuntimeSession(infos=[info, info])
    )
    correlation = result.correlation
    assert correlation.resolved
    assert correlation.as_dict() == {
        "contract_version": DOCKER_DIRECT_UNIX_CONTRACT,
        "method_version": 1,
        "provider": "docker",
        "status": "resolved",
        "attestation_fingerprint": "sha256:" + "c" * 64,
        "endpoint": "/run/localplane-test-docker.sock",
        "container_id": _CONTAINER_ID,
        "container_started_at": _STARTED_AT,
        "engine_id": _ENGINE_ID,
        "direct_transport_verified": True,
        "peer_service_main_verified": True,
        "owner_unit_id": "docker-engine.service",
        "owner_invocation_id": _INVOCATION_ID,
        "execution_cgroup_relation": "ancestor",
        "gaps": [],
    }
    assert result.evidence["result"]["live_restore_exemption_used"] is False
    assert result.evidence["engine_a"]["info"]["live_restore_enabled"] is live_restore
    assert session.info_calls == session.version_calls == 2
    assert session.list_calls == 1
    assert session.inspect_calls == {_CONTAINER_ID: 3}
    assert session.events == [
        "version",
        "info",
        "list",
        f"inspect:{_CONTAINER_ID}",
        f"inspect:{_CONTAINER_ID}",
        "info",
        "version",
        f"inspect:{_CONTAINER_ID}",
    ]
    assert systemd.calls == [88, 88]
    assert reader.calls == 2
    assert all(pinned.closed for pinned in processes.pins)


@pytest.mark.parametrize(
    ("read", "gap"),
    [
        (
            DockerDirectAttestationRead("unavailable", "attestation_unavailable"),
            "management_path.runtime_owner.attestation_unavailable",
        ),
        (
            DockerDirectAttestationRead("untrusted", "attestation_untrusted"),
            "management_path.runtime_owner.attestation_untrusted",
        ),
    ],
)
def test_missing_or_untrusted_attestation_is_incomplete(
    read: DockerDirectAttestationRead, gap: str
):
    result, session, _reader, _processes, systemd = _correlate(
        reader=_AttestationReader([read])
    )
    assert result.correlation.status == "incomplete"
    assert result.correlation.gaps == (gap,)
    assert systemd.calls == [] and session.version_calls == 0


def test_attestation_change_during_snapshot_is_incomplete():
    first = _trusted_attestation()
    second = replace(first, engine_unit_id="other.service", fingerprint="sha256:" + "d" * 64)
    reads = [
        DockerDirectAttestationRead("trusted", "trusted", first),
        DockerDirectAttestationRead("trusted", "trusted", second),
    ]
    result, *_rest = _correlate(reader=_AttestationReader(reads))
    assert result.correlation.gaps == (
        "management_path.runtime_owner.attestation_changed",
    )


@pytest.mark.parametrize(
    ("resolution", "gap"),
    [
        (_unit_resolution(unit_id="wrong.service"), "daemon_unit_mismatch"),
        (_unit_resolution(unit_type="scope"), "socket_activation_unsupported"),
        (_unit_resolution(main_pid=-1), "daemon_main_pid_mismatch"),
        (_unit_resolution(main_pid=0), "daemon_main_pid_mismatch"),
        (_unit_resolution(main_pid=False), "daemon_main_pid_mismatch"),
        (_unit_resolution(main_pid=_PEER_PID + 1), "daemon_main_pid_mismatch"),
        (_unit_resolution(active_state="inactive"), "daemon_process_unattested"),
        (_unit_resolution(load_state="not-found"), "daemon_process_unattested"),
        (_unit_resolution(transient=True), "daemon_process_unattested"),
        (_unit_resolution(current_job={"id": 3}), "daemon_process_unattested"),
        (
            _unit_resolution(control_group="/system.slice/other.service"),
            "daemon_process_unattested",
        ),
        (
            _unit_resolution(status="unsupported", reason="method_missing"),
            "daemon_unit_unresolved",
        ),
        (
            _unit_resolution(status="failed", reason="permission_denied"),
            "daemon_unit_unresolved",
        ),
    ],
)
def test_systemd_peer_containment_breaks_fail_closed(
    resolution: ProcessUnitResolution, gap: str
):
    result, *_rest = _correlate(systemd=_Systemd([resolution]))
    assert result.correlation.gaps == (f"management_path.runtime_owner.{gap}",)
    assert result.correlation.owner_unit_id is None


def test_socket_activation_and_relay_topologies_are_not_rescued():
    socket_activated, *_rest = _correlate(
        systemd=_Systemd([_unit_resolution(unit_type="scope")])
    )
    assert socket_activated.correlation.gaps == (
        "management_path.runtime_owner.socket_activation_unsupported",
    )

    relay_in_other_unit, *_rest = _correlate(
        systemd=_Systemd([_unit_resolution(unit_id="docker-proxy.service")])
    )
    assert relay_in_other_unit.correlation.gaps == (
        "management_path.runtime_owner.daemon_unit_mismatch",
    )

    helper_in_same_service, *_rest = _correlate(
        systemd=_Systemd([_unit_resolution(main_pid=_PEER_PID + 1)])
    )
    assert helper_in_same_service.correlation.gaps == (
        "management_path.runtime_owner.daemon_main_pid_mismatch",
    )


def test_rootless_direct_listener_remains_unsupported():
    source_pidfd = os.pidfd_open(os.getpid())
    session = DockerRuntimeOwnerSession(_trusted_attestation(), timeout_s=1)
    try:
        with pytest.raises(DockerRuntimeOwnerFailure) as caught:
            session._read_peer(_PeerSocket(source_pidfd, peer_uid=1000))  # noqa: SLF001
        assert caught.value.reason == "docker_peer_not_rootful"
    finally:
        os.close(source_pidfd)


def test_daemon_unit_generation_change_across_snapshot_is_incomplete():
    changed = _unit_resolution(invocation_id="34" * 16)
    result, *_rest = _correlate(
        systemd=_Systemd([_unit_resolution(), changed])
    )
    assert result.correlation.gaps == (
        "management_path.runtime_owner.daemon_process_unattested",
    )


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (_inspect_payload(), _inspect_payload(Pid=5002)),
        (_inspect_payload(), _inspect_payload(StartedAt="2026-08-28T10:01:00Z")),
        (_inspect_payload(), _inspect_payload(Running=False)),
    ],
)
def test_container_generation_change_is_incomplete(
    first: dict[str, Any], second: dict[str, Any]
):
    session = _RuntimeSession(
        inspections={_CONTAINER_ID: [first, second]}
    )
    result, *_rest = _correlate(session=session)
    assert result.correlation.gaps == (
        "management_path.runtime_owner.container_changed",
    )
    assert all(pinned.close_calls == 1 for pinned in _rest[2].pins)


@pytest.mark.parametrize(
    "final",
    [
        _inspect_payload(Pid=5002),
        _inspect_payload(StartedAt="2026-08-28T10:01:00Z"),
        _inspect_payload(Running=False),
        {**_inspect_payload(), "HostConfig": {"NetworkMode": "bridge"}},
        _inspect_payload(_OTHER_CONTAINER_ID),
    ],
    ids=[
        "pid-changed",
        "started-at-changed",
        "not-running",
        "not-host-network",
        "full-id-changed",
    ],
)
def test_final_container_generation_and_state_change_is_incomplete(
    final: dict[str, Any],
):
    session = _RuntimeSession(
        inspections={
            _CONTAINER_ID: [_inspect_payload(), _inspect_payload(), final]
        }
    )
    result, *_rest = _correlate(session=session)

    assert result.correlation.gaps == (
        "management_path.runtime_owner.container_changed",
    )
    assert session.list_calls == 1
    assert session.inspect_calls == {_CONTAINER_ID: 3}
    assert all(pinned.close_calls == 1 for pinned in _rest[2].pins)


def test_final_container_disappearance_after_engine_sandwich_is_incomplete():
    disappeared = DockerRuntimeOwnerFailure(
        "management_path.runtime_owner.container_changed",
        "daemon_listed_container_disappeared",
        {"container_id": _CONTAINER_ID, "http_status": 404},
    )
    session = _RuntimeSession(
        inspections={
            _CONTAINER_ID: [
                _inspect_payload(),
                _inspect_payload(),
                disappeared,
            ]
        }
    )
    result, *_rest = _correlate(session=session)

    assert result.correlation.gaps == (
        "management_path.runtime_owner.container_changed",
    )
    assert result.evidence["failure"]["reason"] == (
        "daemon_listed_container_disappeared"
    )
    assert session.list_calls == 1
    assert session.inspect_calls == {_CONTAINER_ID: 3}
    assert all(pinned.close_calls == 1 for pinned in _rest[2].pins)


def test_final_container_cgroup_relation_change_is_incomplete():
    processes = _ProcessInspector()

    def change_relation_on_final_inspect(container_id: str, call: int) -> None:
        assert container_id == _CONTAINER_ID
        if call == 3:
            matched = next(pinned for pinned in processes.pins if pinned.pid == 5001)
            matched.cgroup_path = "/docker/unrelated"

    session = _RuntimeSession(inspect_hook=change_relation_on_final_inspect)
    result, *_rest = _correlate(session=session, processes=processes)

    assert result.correlation.gaps == (
        "management_path.runtime_owner.container_changed",
    )
    assert session.list_calls == 1
    assert session.inspect_calls == {_CONTAINER_ID: 3}
    assert all(pinned.close_calls == 1 for pinned in processes.pins)


@pytest.mark.parametrize(
    "inspect_payload",
    [
        _inspect_payload(Running=False),
        _inspect_payload(Pid=-1),
        _inspect_payload(Pid=0),
        _inspect_payload(Pid=False),
        _inspect_payload(StartedAt="0001-01-01T00:00:00Z"),
        _inspect_payload(StartedAt="not-a-time"),
        {**_inspect_payload(), "HostConfig": {"NetworkMode": "bridge"}},
    ],
)
def test_non_running_non_host_or_invalid_generation_container_never_matches(
    inspect_payload: dict[str, Any]
):
    result, *_rest = _correlate(
        session=_RuntimeSession(
            inspections={_CONTAINER_ID: [inspect_payload]}
        )
    )
    assert result.correlation.gaps == (
        "management_path.runtime_owner.container_not_found",
    )


def test_zero_and_multiple_container_matches_are_distinct_incomplete_states():
    zero, *_rest = _correlate(
        processes=_ProcessInspector(cgroups={
            _PEER_PID: "/system.slice/docker-engine.service",
            5001: "/docker/unrelated",
        })
    )
    assert zero.correlation.gaps == (
        "management_path.runtime_owner.container_not_found",
    )

    inspections = {
        _CONTAINER_ID: [_inspect_payload(_CONTAINER_ID), _inspect_payload(_CONTAINER_ID)],
        _OTHER_CONTAINER_ID: [
            _inspect_payload(_OTHER_CONTAINER_ID, Pid=5002),
            _inspect_payload(_OTHER_CONTAINER_ID, Pid=5002),
        ],
    }
    multiple, *_rest = _correlate(
        session=_RuntimeSession(
            listed=(_CONTAINER_ID, _OTHER_CONTAINER_ID),
            inspections=inspections,
        ),
        accepted_cgroup=(
            f"/docker/{_CONTAINER_ID}/nested/{_OTHER_CONTAINER_ID}/backend.scope"
        ),
    )
    assert multiple.correlation.gaps == (
        "management_path.runtime_owner.container_ambiguous",
    )


def test_engine_snapshot_change_is_incomplete():
    info_a = DockerRuntimeInfo(
        _ENGINE_ID, "27.5.1", "linux", "systemd", "2", False
    )
    info_b = replace(info_a, engine_id="different-engine")
    result, *_rest = _correlate(
        session=_RuntimeSession(infos=[info_a, info_b])
    )
    assert result.correlation.gaps == (
        "management_path.runtime_owner.engine_snapshot_changed",
    )


def test_kernel_cgroup_or_process_race_is_incomplete():
    changed, *_rest = _correlate(
        processes=_ProcessInspector(accepted_identity_changes=True)
    )
    assert changed.correlation.gaps == (
        "management_path.runtime_owner.container_changed",
    )

    unstable, *_rest = _correlate(
        processes=_ProcessInspector(unstable_pids={5001})
    )
    assert unstable.correlation.gaps == (
        "management_path.runtime_owner.container_changed",
    )


@pytest.mark.parametrize(
    "failure",
    [
        DockerRuntimeOwnerFailure(
            "management_path.runtime_owner.peer_pidfd_unavailable",
            "so_peerpidfd_unavailable",
        ),
        DockerRuntimeOwnerFailure(
            "management_path.runtime_owner.direct_transport_unproven",
            "permission_denied",
        ),
        DockerRuntimeOwnerFailure(
            "management_path.runtime_owner.transport_reconnected",
            "socket_closed",
        ),
    ],
)
def test_transport_topology_and_permission_failures_preserve_typed_gap(
    failure: DockerRuntimeOwnerFailure,
):
    result, *_rest = _correlate(session=_RuntimeSession(enter_failure=failure))
    assert result.correlation.gaps == (failure.gap,)


def test_final_peer_exit_is_incomplete():
    failure = DockerRuntimeOwnerFailure(
        "management_path.runtime_owner.peer_process_exited",
        "docker_peer_process_exited",
    )
    result, *_rest = _correlate(
        session=_RuntimeSession(final_peer_failure=failure)
    )
    assert result.correlation.gaps == (failure.gap,)


def test_pid_and_cgroup_namespace_failures_are_typed():
    failure = ProcessInspectionFailure(
        "management_path.runtime_owner.cgroup_namespace_mismatch",
        "unified_cgroup_v2_unavailable",
    )
    result, *_rest = _correlate(processes=_ProcessInspector(failure=failure))
    assert result.correlation.gaps == (failure.gap,)


def test_engine_must_share_localplanes_pid_namespace_before_docker_pids_are_used():
    processes = _ProcessInspector(peer_namespace_compatible=False)
    result, session, _reader, processes, systemd = _correlate(processes=processes)
    assert result.correlation.gaps == (
        "management_path.runtime_owner.pid_namespace_mismatch",
    )
    assert session.version_calls == 0
    assert systemd.calls == []
    assert all(pinned.closed for pinned in processes.pins)


def test_cgroup_root_and_textual_prefixes_can_never_match_a_container():
    assert _cgroup_relation("/", f"/docker/{_CONTAINER_ID}/backend.scope") is None
    assert _cgroup_relation("/", "/") is None
    assert _cgroup_relation("/docker/abc", "/docker/abcd/backend.scope") is None
    assert _cgroup_relation("/docker/abc", "/docker/abc/backend.scope") == "ancestor"
    assert _cgroup_relation("/docker/abc", "/docker/abc") == "equal"

    processes = _ProcessInspector(
        cgroups={
            _PEER_PID: "/system.slice/docker-engine.service",
            5001: "/",
        }
    )
    result, *_rest = _correlate(processes=processes)
    assert result.correlation.gaps == (
        "management_path.runtime_owner.container_not_found",
    )


def test_normalized_correlation_rejects_unknown_gaps_contracts_and_partial_success():
    with pytest.raises(ValueError, match="unknown runtime-owner gap"):
        AgentRuntimeOwnerCorrelation(
            DOCKER_DIRECT_UNIX_CONTRACT,
            1,
            "docker",
            "incomplete",
            gaps=("management_path.runtime_owner.future_guess",),
        )
    with pytest.raises(ValueError, match="unknown runtime-owner contract"):
        AgentRuntimeOwnerCorrelation("docker-v2", 1, "docker", "incomplete")
    with pytest.raises(ValueError, match="is incomplete"):
        AgentRuntimeOwnerCorrelation(
            DOCKER_DIRECT_UNIX_CONTRACT,
            1,
            "docker",
            "resolved",
        )
    with pytest.raises(ValueError, match="claims authority"):
        AgentRuntimeOwnerCorrelation(
            DOCKER_DIRECT_UNIX_CONTRACT,
            1,
            "docker",
            "incomplete",
            owner_unit_id="configured-alone.service",
            gaps=("management_path.runtime_owner.daemon_unit_mismatch",),
        )
    assert all(gap.startswith("management_path.runtime_owner.") for gap in DOCKER_RUNTIME_OWNER_GAPS)


def test_backend_runtime_correlation_parser_is_closed_and_does_not_coerce_verdicts():
    resolved, *_rest = _correlate()
    domain = RuntimeOwnerCorrelation.from_dict(resolved.correlation.as_dict())
    assert domain.status is RuntimeOwnerStatus.RESOLVED
    assert domain.as_dict() == resolved.correlation.as_dict()

    unknown = resolved.correlation.as_dict() | {"future_authority": "yes"}
    with pytest.raises(ValueError, match="unknown or missing"):
        RuntimeOwnerCorrelation.from_dict(unknown)
    wrong_bool = resolved.correlation.as_dict() | {"direct_transport_verified": "true"}
    with pytest.raises(ValueError, match="must be booleans"):
        RuntimeOwnerCorrelation.from_dict(wrong_bool)


def test_malformed_or_inconsistent_runtime_owner_cannot_silently_complete_context():
    resolved, *_rest = _correlate()
    base = {
        "status": "complete",
        "observed_at": "2026-08-28T10:00:00Z",
        "target_unit": "target.service",
        "action": "stop",
        "target_facts": {},
        "effect_units": ["target.service"],
        "effect_edges": [],
        "effect_complete": True,
        "active_activation_sources": [],
        "active_upholding_sources": [],
        "management_units": ["backend.scope", "docker-engine.service"],
        "management_complete": True,
        "agent_unit": "localplane-agent.service",
        "agent_complete": True,
        "gaps": [],
        "evidence": {},
        "runtime_owner": resolved.correlation.as_dict(),
        "observation_id": "obs",
    }
    assert SystemdLifecycleContext.from_dict(base).management_complete is True
    with pytest.raises(ValueError, match="object or null"):
        SystemdLifecycleContext.from_dict(base | {"runtime_owner": []})
    with pytest.raises(ValueError, match="absent from management_units"):
        SystemdLifecycleContext.from_dict(
            base | {"management_units": ["backend.scope"]}
        )

    incomplete = {
        **resolved.correlation.as_dict(),
        "status": "incomplete",
        "container_id": None,
        "container_started_at": None,
        "engine_id": None,
        "direct_transport_verified": False,
        "peer_service_main_verified": False,
        "owner_unit_id": None,
        "owner_invocation_id": None,
        "execution_cgroup_relation": None,
        "gaps": ["management_path.runtime_owner.container_not_found"],
    }
    with pytest.raises(ValueError, match="cannot complete"):
        SystemdLifecycleContext.from_dict(base | {"runtime_owner": incomplete})
