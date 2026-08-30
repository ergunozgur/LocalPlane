"""The closed, read-only accepted-socket evidence path used by systemd planning."""

from __future__ import annotations

import socket
import struct
from pathlib import Path
from typing import Any

import pytest

from localplane.agent.docker_runtime_owner import (
    RuntimeOwnerCorrelation,
    RuntimeOwnerObservation,
)
from localplane.agent.providers.linux_socket_diag import (
    INET_DIAG_CGROUP_ID,
    NLMSG_DONE,
    SOCK_DIAG_BY_FAMILY,
    TCP_ESTABLISHED,
    CgroupPathResult,
    LinuxSocketDiag,
    SocketDiagMatch,
    SocketDiagResult,
    TcpSocketTuple,
    decode_diag_message,
    encode_request,
    resolve_cgroup_path,
)
from localplane.agent.providers.systemd import AgentUnitResolution, UnitObservation
from localplane.agent.systemd_lifecycle_context import SystemdLifecycleContextReader
from localplane.backend.api.transport import request_connection_from_scope
from localplane.backend.domain.protection import ProtectionReason, ProtectionStatus
from localplane.backend.domain.systemd_lifecycle import (
    SystemdLifecycleContext,
    assess_lifecycle_protection,
)


_NLMSG = struct.Struct("=IHHII")


def _diag_payload(
    query: TcpSocketTuple,
    *,
    cgroup_id: int | None = 41,
    peer_port: int | None = None,
) -> bytes:
    family = socket.AF_INET if query.family == "ipv4" else socket.AF_INET6
    source = socket.inet_pton(family, query.local_ip)
    destination = socket.inet_pton(family, query.peer_ip)
    if family == socket.AF_INET:
        source += b"\0" * 12
        destination += b"\0" * 12
    payload = (
        struct.pack("=BBBB", family, TCP_ESTABLISHED, 0, 0)
        + struct.pack("!HH", query.local_port, peer_port or query.peer_port)
        + source
        + destination
        + struct.pack("=III", 0, 0xFFFFFFFF, 0xFFFFFFFF)
        + struct.pack("=IIIII", 0, 0, 0, 1000, 222)
    )
    if cgroup_id is not None:
        attribute = struct.pack("=HHQ", 12, INET_DIAG_CGROUP_ID, cgroup_id)
        payload += attribute
    return payload


def _message(kind: int, sequence: int, payload: bytes = b"") -> bytes:
    raw = _NLMSG.pack(_NLMSG.size + len(payload), kind, 0, sequence, 0) + payload
    return raw + b"\0" * ((-len(raw)) % 4)


class _DiagChannel:
    def __init__(self, payloads: list[bytes]) -> None:
        self.payloads = payloads
        self.sequence = 0
        self.sent = b""

    def bind(self, _address: Any) -> None:
        pass

    def send(self, value: bytes) -> int:
        self.sent = value
        self.sequence = _NLMSG.unpack_from(value)[3]
        return len(value)

    def settimeout(self, _value: float) -> None:
        pass

    def recv(self, _size: int) -> bytes:
        messages = b"".join(
            _message(SOCK_DIAG_BY_FAMILY, self.sequence, payload)
            for payload in self.payloads
        )
        return messages + _message(NLMSG_DONE, self.sequence)

    def close(self) -> None:
        pass


def _lookup(query: TcpSocketTuple, payloads: list[bytes]) -> SocketDiagResult:
    channel = _DiagChannel(payloads)
    provider = LinuxSocketDiag(socket_factory=lambda *_args: channel)
    return provider.lookup(query)


@pytest.mark.parametrize(
    ("query", "family"),
    [
        (TcpSocketTuple("ipv4", "192.0.2.2", 43123, "198.51.100.4", 8080), socket.AF_INET),
        (TcpSocketTuple("ipv6", "2001:db8::2", 43123, "2001:db8::4", 8080), socket.AF_INET6),
    ],
)
def test_sock_diag_ipv4_and_ipv6_request_and_response_are_typed(
    query: TcpSocketTuple, family: int
):
    request = encode_request(query, sequence=7)
    length, kind, flags, sequence, _pid = _NLMSG.unpack_from(request)
    assert length == len(request)
    assert kind == SOCK_DIAG_BY_FAMILY and sequence == 7
    assert flags & 1
    assert request[_NLMSG.size] == family

    decoded = decode_diag_message(_diag_payload(query, cgroup_id=99))
    assert decoded is not None
    assert decoded["family"] == family
    assert decoded["local_ip"] == query.local_ip
    assert decoded["local_port"] == query.local_port
    assert decoded["peer_ip"] == query.peer_ip
    assert decoded["peer_port"] == query.peer_port
    assert decoded["cgroup_id"] == 99


def test_sock_diag_requires_one_exact_tuple_and_cgroup_attribute():
    query = TcpSocketTuple("ipv4", "192.0.2.2", 43123, "198.51.100.4", 8080)

    matched = _lookup(
        query,
        [_diag_payload(query, peer_port=43124), _diag_payload(query, cgroup_id=88)],
    )
    assert matched.status == "matched"
    assert matched.match == SocketDiagMatch(cgroup_id=88, socket_inode=222, uid=1000)
    assert matched.match_count == 1 and matched.inspected_count == 2

    absent = _lookup(query, [_diag_payload(query, peer_port=43124)])
    assert (absent.status, absent.reason) == ("not_found", "exact_tcp_socket_not_found")

    ambiguous = _lookup(query, [_diag_payload(query), _diag_payload(query)])
    assert (ambiguous.status, ambiguous.reason, ambiguous.match_count) == (
        "ambiguous", "exact_tcp_socket_ambiguous", 2
    )

    unsupported = _lookup(query, [_diag_payload(query, cgroup_id=None)])
    assert (unsupported.status, unsupported.reason) == (
        "unsupported", "inet_diag_cgroup_id_unavailable"
    )


def test_sock_diag_result_count_is_hard_bounded():
    query = TcpSocketTuple("ipv4", "192.0.2.2", 43123, "198.51.100.4", 8080)
    channel = _DiagChannel(
        [_diag_payload(query, peer_port=43124), _diag_payload(query, peer_port=43125)]
    )
    result = LinuxSocketDiag(
        max_messages=1, socket_factory=lambda *_args: channel
    ).lookup(query)
    assert (result.status, result.reason) == (
        "failed", "socket_diag_result_limit_exceeded"
    )


def test_cgroup_id_resolution_is_v2_only_and_bounded(tmp_path: Path):
    root = tmp_path / "cgroup"
    root.mkdir()
    (root / "cgroup.controllers").write_text("cpu\n")
    child = root / "system.slice" / "backend.service"
    child.mkdir(parents=True)
    resolved = resolve_cgroup_path(child.stat().st_ino, root=root)
    assert (resolved.status, resolved.path) == (
        "resolved", "/system.slice/backend.service"
    )
    bounded = resolve_cgroup_path(child.stat().st_ino, root=root, max_directories=1)
    assert bounded.reason == "cgroup_walk_limit_exceeded"
    (root / "cgroup.controllers").unlink()
    assert resolve_cgroup_path(child.stat().st_ino, root=root).status == "unsupported"


def test_request_connection_uses_only_the_exact_asgi_tuple_and_netns():
    scope = {
        "client": ("192.0.2.7", 44000),
        "server": ("198.51.100.8", 8080),
        "headers": [
            (b"forwarded", b"for=203.0.113.10"),
            (b"x-forwarded-for", b"203.0.113.11"),
            (b"x-real-ip", b"203.0.113.12"),
            (b"host", b"attacker.invalid"),
        ],
        "query_string": b"peer_ip=203.0.113.13",
    }
    evidence = request_connection_from_scope(scope, netns_inode=1234)
    assert evidence.as_dict() == {
        "status": "observed",
        "family": "ipv4",
        "peer_ip": "192.0.2.7",
        "peer_port": 44000,
        "local_ip": "198.51.100.8",
        "local_port": 8080,
        "backend_netns_inode": 1234,
        "reason": None,
    }
    ipv6 = request_connection_from_scope(
        {"client": ("2001:db8::7", 44000), "server": ("2001:db8::8", 8080)},
        netns_inode=1234,
    )
    assert ipv6.family == "ipv6"
    assert request_connection_from_scope(
        {"client": ("testclient", 1), "server": ("testserver", 2)},
        netns_inode=1234,
    ).status == "unsupported"


def test_backend_keeps_loopback_default_and_disables_proxy_rewriting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import localplane.backend.__main__ as backend_main

    called: dict[str, Any] = {}
    monkeypatch.delenv("LOCALPLANE_HOST", raising=False)
    monkeypatch.setenv("LOCALPLANE_DB_PATH", str(tmp_path / "localplane.db"))
    monkeypatch.setenv("LOCALPLANE_OBSERVE_ON_STARTUP", "0")
    monkeypatch.setattr(
        backend_main.uvicorn,
        "run",
        lambda app, **kwargs: called.update({"app": app, **kwargs}),
    )
    assert backend_main.main([]) == 0
    assert called["host"] == "127.0.0.1"
    assert called["proxy_headers"] is False


class _Graph:
    status = "complete"
    complete = True
    reason = None
    detail: dict[str, Any] = {}
    provider_version = "255"
    gaps: tuple[str, ...] = ()
    units = ("target.service",)
    edges: tuple[dict[str, str], ...] = ()
    active_activation_sources: tuple[str, ...] = ()
    active_upholding_sources: tuple[str, ...] = ()
    target = UnitObservation(
        canonical_id="target.service",
        observed_at="2026-08-27T00:00:00+00:00",
        facts={"canonical_id": "target.service", "unit_type": "service"},
    )


class _Systemd:
    def __init__(
        self,
        *,
        connection_unit: str = "backend.service",
        connection_unit_type: str = "service",
        provider_resolutions: dict[str, AgentUnitResolution] | None = None,
    ) -> None:
        self.control_groups: list[str] = []
        self.connection_unit = connection_unit
        self.connection_unit_type = connection_unit_type
        self.provider_resolutions = provider_resolutions or {}
        self.provider_calls: list[str] = []

    def observe_effect_graph(self, target: str, action: str) -> _Graph:
        assert (target, action) == ("target.service", "stop")
        return _Graph()

    def resolve_control_group_unit(self, path: str) -> AgentUnitResolution:
        self.control_groups.append(path)
        return AgentUnitResolution(
            status="resolved",
            observed_at="now",
            canonical_id=self.connection_unit,
            unit=UnitObservation(
                canonical_id=self.connection_unit,
                observed_at="now",
                facts={
                    "canonical_id": self.connection_unit,
                    "unit_type": self.connection_unit_type,
                },
            ),
        )

    def resolve_current_process_unit(self) -> AgentUnitResolution:
        return AgentUnitResolution(
            status="resolved", observed_at="now", canonical_id="agent-holder.service"
        )

    def resolve_provider_unit(self, provider: str) -> AgentUnitResolution:
        self.provider_calls.append(provider)
        try:
            return self.provider_resolutions[provider]
        except KeyError as exc:
            raise AssertionError(f"unexpected provider lookup: {provider}") from exc


class _SocketDiag:
    def __init__(self, *, inode: int = 77, result: SocketDiagResult | None = None) -> None:
        self.inode = inode
        self.result = result or SocketDiagResult(
            "matched", "exact_tcp_socket_matched",
            match=SocketDiagMatch(cgroup_id=44, socket_inode=222, uid=1000),
            match_count=1,
        )
        self.lookups = 0

    def network_namespace_inode(self) -> int:
        return self.inode

    def lookup(self, _query: TcpSocketTuple) -> SocketDiagResult:
        self.lookups += 1
        return self.result


def _connection(netns: int = 77) -> dict[str, Any]:
    return {
        "family": "ipv4",
        "peer_ip": "192.0.2.2",
        "peer_port": 43123,
        "local_ip": "198.51.100.4",
        "local_port": 8080,
        "backend_netns_inode": netns,
    }


def test_namespace_mismatch_is_unknown_before_socket_lookup():
    diag = _SocketDiag()
    observed = SystemdLifecycleContextReader(_Systemd(), diag).observe(
        target_unit="target.service",
        action="stop",
        connection=_connection(78),
        management_providers=(),
        provider_evidence_complete=True,
    )
    assert observed.management_complete is False
    assert "management_path.network_namespace_mismatch" in observed.gaps
    assert diag.lookups == 0


def test_socket_cgroup_and_systemd_context_is_normalised(monkeypatch: pytest.MonkeyPatch):
    import localplane.agent.systemd_lifecycle_context as context_module

    monkeypatch.setattr(
        context_module,
        "resolve_cgroup_path",
        lambda _cgroup_id: CgroupPathResult(
            "resolved", "cgroup_path_resolved", path="/system.slice/backend.service"
        ),
    )
    systemd = _Systemd()
    observed = SystemdLifecycleContextReader(systemd, _SocketDiag()).observe(
        target_unit="target.service",
        action="stop",
        connection=_connection(),
        management_providers=(),
        provider_evidence_complete=True,
    )
    assert observed.status == "complete"
    assert observed.management_units == ("backend.service",)
    assert observed.agent_unit == "agent-holder.service"
    assert systemd.control_groups == ["/system.slice/backend.service"]
    assert observed.evidence["accepted_socket"]["diag"]["status"] == "matched"
    assert observed.evidence["accepted_socket"]["runtime_dependency"]["status"] == (
        "not_applicable"
    )


def test_container_scope_runtime_owner_must_be_proven_before_management_clear(
    monkeypatch: pytest.MonkeyPatch,
):
    """A disjoint systemd graph cannot prove a container runtime is harmless."""
    import localplane.agent.systemd_lifecycle_context as context_module

    monkeypatch.setattr(
        context_module,
        "resolve_cgroup_path",
        lambda _cgroup_id: CgroupPathResult(
            "resolved", "cgroup_path_resolved", path="/system.slice/backend-container.scope"
        ),
    )
    observed = SystemdLifecycleContextReader(
        _Systemd(
            connection_unit="backend-container.scope",
            connection_unit_type="scope",
        ),
        _SocketDiag(),
    ).observe(
        target_unit="target.service",
        action="stop",
        connection=_connection(),
        management_providers=(),
        provider_evidence_complete=True,
    )
    assert observed.management_units == ("backend-container.scope",)
    assert observed.management_complete is False
    assert "management_path.runtime_owner_unproven" in observed.gaps
    dependency = observed.evidence["accepted_socket"]["runtime_dependency"]
    assert dependency == {
        "status": "unknown",
        "reason": "accepted_connection_unit_runtime_owner_unproven",
        "containing_unit": "backend-container.scope",
        "containing_unit_type": "scope",
        "authoritative_owner_unit": None,
    }

    protection = assess_lifecycle_protection(
        SystemdLifecycleContext.from_dict(observed.as_dict())
    )
    by_reason = {entry.reason: entry for entry in protection.assessed}
    assert by_reason[ProtectionReason.MANAGEMENT_PATH].status is ProtectionStatus.UNKNOWN
    assert protection.status is ProtectionStatus.UNKNOWN


class _RuntimeOwner:
    def __init__(self, correlation: RuntimeOwnerCorrelation) -> None:
        self.correlation = correlation
        self.cgroups: list[str] = []

    def correlate(self, cgroup_path: str) -> RuntimeOwnerObservation:
        self.cgroups.append(cgroup_path)
        return RuntimeOwnerObservation(
            correlation=self.correlation,
            evidence={"source": "fixture-closed-runtime-correlation"},
        )


def _resolved_runtime_owner() -> RuntimeOwnerCorrelation:
    return RuntimeOwnerCorrelation(
        contract_version="docker-direct-unix-v1",
        method_version=1,
        provider="docker",
        status="resolved",
        attestation_fingerprint="sha256:" + "a" * 64,
        endpoint="/run/docker-direct.sock",
        container_id="b" * 64,
        container_started_at="2026-08-28T10:00:00Z",
        engine_id="opaque-engine",
        direct_transport_verified=True,
        peer_service_main_verified=True,
        owner_unit_id="docker-engine.service",
        owner_invocation_id="02" * 16,
        execution_cgroup_relation="ancestor",
    )


def _provider_resolution(
    canonical_id: str | None,
    *,
    status: str = "resolved",
    reason: str | None = None,
) -> AgentUnitResolution:
    return AgentUnitResolution(
        status=status,
        observed_at="now",
        canonical_id=canonical_id,
        reason=reason,
    )


def test_runtime_owner_and_distinct_docker_provider_owner_are_both_retained(
    monkeypatch: pytest.MonkeyPatch,
):
    import localplane.agent.systemd_lifecycle_context as context_module

    monkeypatch.setattr(
        context_module,
        "resolve_cgroup_path",
        lambda _cgroup_id: CgroupPathResult(
            "resolved",
            "cgroup_path_resolved",
            path="/docker/container/backend.scope",
        ),
    )
    runtime = _RuntimeOwner(_resolved_runtime_owner())
    systemd = _Systemd(
        connection_unit="backend-container.scope",
        connection_unit_type="scope",
        provider_resolutions={
            "docker": _provider_resolution("docker-provider.service")
        },
    )
    observed = SystemdLifecycleContextReader(
        systemd,
        _SocketDiag(),
        runtime,
    ).observe(
        target_unit="target.service",
        action="stop",
        connection=_connection(),
        management_providers=("docker",),
        provider_evidence_complete=True,
    )
    assert observed.status == "complete"
    assert observed.management_complete is True
    assert observed.management_units == (
        "backend-container.scope",
        "docker-engine.service",
        "docker-provider.service",
    )
    assert observed.runtime_owner == runtime.correlation
    assert runtime.cgroups == ["/docker/container/backend.scope"]
    assert systemd.provider_calls == ["docker"]
    assert observed.evidence["provider_owners"]["docker"]["canonical_id"] == (
        "docker-provider.service"
    )
    assert observed.gaps == ()


def test_identical_runtime_and_provider_owner_deduplicate_but_both_paths_execute(
    monkeypatch: pytest.MonkeyPatch,
):
    import localplane.agent.systemd_lifecycle_context as context_module

    monkeypatch.setattr(
        context_module,
        "resolve_cgroup_path",
        lambda _cgroup_id: CgroupPathResult(
            "resolved",
            "cgroup_path_resolved",
            path="/docker/container/backend.scope",
        ),
    )
    runtime = _RuntimeOwner(_resolved_runtime_owner())
    systemd = _Systemd(
        connection_unit="backend-container.scope",
        connection_unit_type="scope",
        provider_resolutions={
            "docker": _provider_resolution("docker-engine.service")
        },
    )
    observed = SystemdLifecycleContextReader(
        systemd,
        _SocketDiag(),
        runtime,
    ).observe(
        target_unit="target.service",
        action="stop",
        connection=_connection(),
        management_providers=("docker",),
        provider_evidence_complete=True,
    )

    assert observed.status == "complete"
    assert observed.management_complete is True
    assert observed.management_units == (
        "backend-container.scope",
        "docker-engine.service",
    )
    assert runtime.cgroups == ["/docker/container/backend.scope"]
    assert systemd.provider_calls == ["docker"]
    assert observed.evidence["runtime_owner"]["source"] == (
        "fixture-closed-runtime-correlation"
    )
    assert observed.evidence["provider_owners"]["docker"]["canonical_id"] == (
        "docker-engine.service"
    )


def test_resolved_runtime_owner_cannot_complete_failed_docker_provider_resolution(
    monkeypatch: pytest.MonkeyPatch,
):
    import localplane.agent.systemd_lifecycle_context as context_module

    monkeypatch.setattr(
        context_module,
        "resolve_cgroup_path",
        lambda _cgroup_id: CgroupPathResult(
            "resolved",
            "cgroup_path_resolved",
            path="/docker/container/backend.scope",
        ),
    )
    systemd = _Systemd(
        connection_unit="backend-container.scope",
        connection_unit_type="scope",
        provider_resolutions={
            "docker": _provider_resolution(
                None, status="failed", reason="provider_owner_unavailable"
            )
        },
    )
    observed = SystemdLifecycleContextReader(
        systemd,
        _SocketDiag(),
        _RuntimeOwner(_resolved_runtime_owner()),
    ).observe(
        target_unit="target.service",
        action="stop",
        connection=_connection(),
        management_providers=("docker",),
        provider_evidence_complete=True,
    )

    assert observed.management_complete is False
    assert observed.status == "partial"
    assert observed.management_units == (
        "backend-container.scope",
        "docker-engine.service",
    )
    assert observed.gaps == ("management_path.provider_owner:docker",)
    assert systemd.provider_calls == ["docker"]
    assert observed.evidence["provider_owners"]["docker"]["status"] == "failed"


def test_resolved_runtime_owner_never_erases_an_independent_provider_owner_gap(
    monkeypatch: pytest.MonkeyPatch,
):
    import localplane.agent.systemd_lifecycle_context as context_module

    monkeypatch.setattr(
        context_module,
        "resolve_cgroup_path",
        lambda _cgroup_id: CgroupPathResult(
            "resolved",
            "cgroup_path_resolved",
            path="/docker/container/backend.scope",
        ),
    )
    systemd = _Systemd(
        connection_unit="backend-container.scope",
        connection_unit_type="scope",
        provider_resolutions={
            "docker": _provider_resolution("docker-provider.service"),
            "tailscale": _provider_resolution(
                None, status="failed", reason="provider_owner_unavailable"
            ),
        },
    )
    observed = SystemdLifecycleContextReader(
        systemd,
        _SocketDiag(),
        _RuntimeOwner(_resolved_runtime_owner()),
    ).observe(
        target_unit="target.service",
        action="stop",
        connection=_connection(),
        management_providers=("docker", "tailscale"),
        provider_evidence_complete=True,
    )

    assert observed.management_complete is False
    assert observed.management_units == (
        "backend-container.scope",
        "docker-engine.service",
        "docker-provider.service",
    )
    assert observed.gaps == ("management_path.provider_owner:tailscale",)
    assert systemd.provider_calls == ["docker", "tailscale"]


def test_failed_runtime_attestation_never_adds_configured_owner_or_clears_unknown(
    monkeypatch: pytest.MonkeyPatch,
):
    import localplane.agent.systemd_lifecycle_context as context_module

    monkeypatch.setattr(
        context_module,
        "resolve_cgroup_path",
        lambda _cgroup_id: CgroupPathResult(
            "resolved",
            "cgroup_path_resolved",
            path="/docker/container/backend.scope",
        ),
    )
    incomplete = RuntimeOwnerCorrelation(
        contract_version="docker-direct-unix-v1",
        method_version=1,
        provider="docker",
        status="incomplete",
        attestation_fingerprint="sha256:" + "a" * 64,
        endpoint="/run/docker-direct.sock",
        gaps=("management_path.runtime_owner.daemon_unit_mismatch",),
    )
    observed = SystemdLifecycleContextReader(
        _Systemd(
            connection_unit="backend-container.scope",
            connection_unit_type="scope",
        ),
        _SocketDiag(),
        _RuntimeOwner(incomplete),
    ).observe(
        target_unit="target.service",
        action="stop",
        connection=_connection(),
        management_providers=(),
        provider_evidence_complete=True,
    )
    assert observed.management_units == ("backend-container.scope",)
    assert "docker-engine.service" not in observed.management_units
    assert observed.management_complete is False
    assert set(observed.gaps) >= {
        "management_path.runtime_owner_unproven",
        "management_path.runtime_owner.daemon_unit_mismatch",
    }
    protection = assess_lifecycle_protection(
        SystemdLifecycleContext.from_dict(observed.as_dict())
    )
    assert protection.status is ProtectionStatus.UNKNOWN


def test_ordinary_service_backend_never_requires_or_calls_docker_runtime_owner(
    monkeypatch: pytest.MonkeyPatch,
):
    import localplane.agent.systemd_lifecycle_context as context_module

    monkeypatch.setattr(
        context_module,
        "resolve_cgroup_path",
        lambda _cgroup_id: CgroupPathResult(
            "resolved",
            "cgroup_path_resolved",
            path="/system.slice/backend.service",
        ),
    )

    class MustNotRun:
        def correlate(self, _path: str):
            raise AssertionError("Docker correlation ran for ordinary service")

    observed = SystemdLifecycleContextReader(
        _Systemd(),
        _SocketDiag(),
        MustNotRun(),
    ).observe(
        target_unit="target.service",
        action="stop",
        connection=_connection(),
        management_providers=(),
        provider_evidence_complete=True,
    )
    assert observed.status == "complete"
    assert observed.management_units == ("backend.service",)
    assert observed.runtime_owner is None


def test_missing_cgroup_socket_evidence_is_explicit_unknown():
    diag = _SocketDiag(
        result=SocketDiagResult("unsupported", "inet_diag_cgroup_id_unavailable")
    )
    observed = SystemdLifecycleContextReader(_Systemd(), diag).observe(
        target_unit="target.service",
        action="stop",
        connection=_connection(),
        management_providers=(),
        provider_evidence_complete=True,
    )
    assert observed.management_complete is False
    assert "management_path.socket_diag:inet_diag_cgroup_id_unavailable" in observed.gaps
