"""Only injected I/O and clocks: no agent service, policy file, socket or bus access."""

from __future__ import annotations

import ast
import errno
import inspect
import json
import socket
import threading
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from localplane.agent import diagnostics
from localplane.agent.diagnostics import (
    CurrentExclusions, DiagnosticExecutor, DiagnosticLimiter, DnsAnswer,
)
from localplane.protocol import diagnostic_policy
from localplane.protocol.diagnostic_policy import (
    DiagnosticPolicy, LoadedDiagnosticPolicy, TcpTarget, parse_diagnostic_policy,
)


NAME = "nas.example.internal."
V4 = "192.168.50.20"
V6 = "fd00::20"
EPOCH = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
COMPLETE = CurrentExclusions(("192.168.50.2", "fd00::2"), ("192.168.50.255",), ("192.168.50.1",))


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def utc(self):
        return EPOCH + timedelta(seconds=self.now)


class FakeSocket:
    """Intentionally has no send/recv/bind/TLS methods."""

    def __init__(self):
        self.timeouts = []
        self.connects = []
        self.closed = 0
        self.local = ("192.168.50.2", 23456)
        self.peer = (V4, 443)
        self.connect_error = None
        self.peer_reads = 0

    def settimeout(self, value):
        self.timeouts.append(value)

    def connect(self, endpoint):
        self.connects.append(endpoint)
        if self.connect_error is not None:
            raise self.connect_error

    def getsockname(self):
        return self.local

    def getpeername(self):
        self.peer_reads += 1
        return self.peer

    def close(self):
        self.closed += 1


@pytest.fixture(autouse=True)
def no_live_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("test attempted non-fixture I/O")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(diagnostic_policy, "load_diagnostic_policy", forbidden)


@pytest.fixture
def loaded():
    document = json.dumps({"version": 2, "control_plane": {"complete": True, "addresses": ["192.168.50.1"]}, "dns": [NAME], "tcp": [
        {"address": V4, "port": 443}, {"address": V6, "port": 443},
    ]}).encode()
    return LoadedDiagnosticPolicy("valid", parse_diagnostic_policy(document))


@pytest.fixture
def rig(loaded):
    clock = Clock()
    limiter = DiagnosticLimiter(monotonic=clock.monotonic)
    sock = FakeSocket()
    factory = Mock(return_value=sock)
    resolver = Mock(return_value=DnsAnswer("resolved", ("192.168.50.99",), False, "normalized fixture"))
    exclusions = Mock(return_value=COMPLETE)
    executor = DiagnosticExecutor(loaded, limiter, exclusions, resolver, vantage="fixture-agent network namespace",
        socket_factory=factory, monotonic=clock.monotonic, utc_now=clock.utc)
    return executor, clock, limiter, sock, factory, resolver, exclusions


def test_ipv4_connects_once_no_payload_and_observed_tuples_only(rig, loaded):
    executor, clock, _, sock, factory, resolver, exclusions = rig
    def connect(endpoint):
        sock.connects.append(endpoint)
        clock.now += 0.25
    sock.connect = connect
    result = executor.tcp(V4, 443, loaded.policy.digest)
    assert result.outcome == "connected"
    assert result.completion_proven is True
    assert sock.connects == [(V4, 443)]
    assert sock.timeouts == [3.0]
    assert sock.closed == 1
    factory.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM)
    exclusions.assert_called_once_with()
    resolver.assert_not_called()
    assert result.local.address == "192.168.50.2" and result.local.port == 23456
    assert result.remote.address == V4 and result.remote.port == 443
    wire = result.to_wire()
    assert set(wire) == {"kind", "address", "port", "outcome", "vantage", "started_at", "ended_at", "local", "remote", "detail"}
    assert wire["local"] == {"address": "192.168.50.2", "port": 23456}
    assert wire["started_at"] == EPOCH.isoformat()
    assert wire["ended_at"] == (EPOCH + timedelta(seconds=.25)).isoformat()
    assert wire["vantage"] == "fixture-agent network namespace"
    with pytest.raises(FrozenInstanceError):
        result.outcome = "error"
    with pytest.raises(FrozenInstanceError):
        result.completion_proven = False
    with pytest.raises(FrozenInstanceError):
        result.local.port = 1
    wire["local"]["port"] = 1
    assert result.local.port == 23456


def test_ipv6_family_and_exact_numeric_connect(rig, loaded):
    executor, _, _, sock, factory, _, _ = rig
    sock.local, sock.peer = ("fd00::2", 23456, 0, 0), (V6, 443, 0, 0)
    result = executor.tcp("fd00:0:0:0:0:0:0:20", 443, loaded.policy.digest)
    assert result.outcome == "connected"
    assert result.address == V6
    factory.assert_called_once_with(socket.AF_INET6, socket.SOCK_STREAM)
    assert sock.connects == [(V6, 443, 0, 0)]
    assert result.local.address == "fd00::2" and result.remote.address == V6


@pytest.mark.parametrize("status", ["missing", "invalid", "unsafe", "unreadable", "unknown"])
@pytest.mark.parametrize("kind", ["dns", "tcp"])
def test_nonvalid_policy_denies_before_io_even_if_it_contains_targets(status, kind, rig, loaded):
    executor, _, _, _, factory, resolver, exclusions = rig
    executor._policy = LoadedDiagnosticPolicy(status, loaded.policy)
    result = executor.dns(NAME, "A", loaded.policy.digest) if kind == "dns" else executor.tcp(V4, 443, loaded.policy.digest)
    assert result.outcome == "denied"
    assert result.completion_proven is True
    assert result.started_at is result.ended_at is result.vantage is None
    factory.assert_not_called()
    resolver.assert_not_called()
    exclusions.assert_not_called()


@pytest.mark.parametrize("digest", ["b" * 64, "", "not-a-digest", "A" * 64])
@pytest.mark.parametrize("kind", ["dns", "tcp"])
def test_policy_mismatch_before_traffic(kind, digest, rig):
    executor, _, _, _, factory, resolver, exclusions = rig
    result = executor.dns(NAME, "A", digest) if kind == "dns" else executor.tcp(V4, 443, digest)
    assert result.outcome == "policy_mismatch"
    assert result.started_at is result.ended_at is None
    factory.assert_not_called()
    resolver.assert_not_called()
    exclusions.assert_not_called()


@pytest.mark.parametrize("address,port", [(V4, 80), ("192.168.50.21", 443), ("fd00::21", 443)])
def test_exact_pair_required(address, port, rig, loaded):
    executor, _, _, _, factory, resolver, exclusions = rig
    assert executor.tcp(address, port, loaded.policy.digest).outcome == "denied"
    factory.assert_not_called()
    resolver.assert_not_called()
    exclusions.assert_not_called()


@pytest.mark.parametrize("address", [
    "127.0.0.1", "0.0.0.0", "169.254.169.254", "168.63.129.16", "224.0.0.1", "255.255.255.255",
    "::", "::1", "fe80::1", "ff02::1", "::ffff:192.168.50.20", "2002:c000:0201::1",
    "2001:0:1234::1", "64:ff9b::c0a8:3214", "fd00::20%eth0", "https://192.168.50.20", "192.168.50.0/24",
])
def test_static_exclusions_rechecked_even_for_a_forged_listed_target(address, rig, loaded):
    executor, _, _, _, factory, resolver, _ = rig
    executor._policy = LoadedDiagnosticPolicy("valid", replace(loaded.policy, tcp=(TcpTarget(address, 443),)))
    result = executor.tcp(address, 443, loaded.policy.digest)
    assert result.outcome == "denied"
    assert result.started_at is result.ended_at is None
    factory.assert_not_called()
    resolver.assert_not_called()


@pytest.mark.parametrize("field", ["local_addresses", "broadcast_addresses", "management_addresses"])
@pytest.mark.parametrize("address", [V4, V6])
def test_each_current_exclusion_denies_listed_private_target(field, address, rig, loaded):
    executor, _, _, _, factory, resolver, exclusions = rig
    exclusions.return_value = replace(COMPLETE, **{field: (address,)})
    assert executor.tcp(address, 443, loaded.policy.digest).outcome == "denied"
    factory.assert_not_called()
    resolver.assert_not_called()


@pytest.mark.parametrize("current", [None, CurrentExclusions(),
    replace(COMPLETE, local_addresses=None), replace(COMPLETE, broadcast_addresses=None),
    replace(COMPLETE, management_addresses=None), replace(COMPLETE, local_addresses=["192.168.50.2"]),
    replace(COMPLETE, management_addresses=(True,)), replace(COMPLETE, local_addresses=("bad-address",)),
    replace(COMPLETE, local_addresses=("fe80::1%eth0",)),
    replace(COMPLETE, local_addresses=("192.168.50.2",) * 4097),
])
def test_unknown_or_malformed_exclusion_sets_fail_closed(current, rig, loaded):
    executor, _, _, _, factory, _, exclusions = rig
    exclusions.return_value = current
    result = executor.tcp(V4, 443, loaded.policy.digest)
    assert result.outcome == "denied"
    assert result.started_at is result.ended_at is None
    factory.assert_not_called()


def test_exclusions_are_current_per_execution_and_errors_do_not_leak(rig, loaded):
    executor, _, _, _, factory, _, exclusions = rig
    assert executor.tcp(V4, 443, loaded.policy.digest).outcome == "connected"
    exclusions.return_value = replace(COMPLETE, management_addresses=(V4,))
    assert executor.tcp(V4, 443, loaded.policy.digest).outcome == "denied"
    exclusions.side_effect = RuntimeError("/private/fixture/path raw provider payload")
    result = executor.tcp(V4, 443, loaded.policy.digest)
    assert result.outcome == "denied" and "private" not in result.detail
    assert factory.call_count == 1


@pytest.mark.parametrize("args", [(None, 443, "x"), (V4, True, "x"), (V4, 443.0, "x"),
    (V4, "443", "x"), (V4, 0, "x"), (V4, 65536, "x"), (V4, 443, None)])
def test_malformed_tcp_argument_types_are_rejected_not_fabricated(args, rig):
    executor, _, _, _, factory, resolver, _ = rig
    with pytest.raises(ValueError, match="invalid TCP diagnostic request"):
        executor.tcp(*args)
    factory.assert_not_called()
    resolver.assert_not_called()


@pytest.mark.parametrize("args", [(None, "A", "x"), (NAME, True, "x"), (NAME, "TXT", "x"),
    (NAME, "a", "x"), (NAME, "AAAA", None), ("a" * 255, "A", "x")])
def test_malformed_dns_argument_types_rejected_before_callback(args, rig):
    executor, _, _, _, factory, resolver, _ = rig
    with pytest.raises(ValueError, match="invalid DNS diagnostic request"):
        executor.dns(*args)
    factory.assert_not_called()
    resolver.assert_not_called()


def test_policy_and_exclusions_are_immutable(loaded):
    with pytest.raises(FrozenInstanceError):
        loaded.policy.dns = ()
    with pytest.raises(FrozenInstanceError):
        loaded.status = "unsafe"
    with pytest.raises(FrozenInstanceError):
        COMPLETE.management_addresses = ()


@pytest.mark.parametrize("vantage", [None, "", True, "x" * 257])
def test_execution_requires_an_internally_supplied_vantage(vantage, loaded):
    with pytest.raises(ValueError, match="invalid diagnostic vantage"):
        DiagnosticExecutor(loaded, DiagnosticLimiter(), lambda: COMPLETE, None, vantage=vantage)


@pytest.mark.parametrize("kind", ["dns", "tcp"])
def test_name_and_exact_tcp_pair_authorizations_are_independent(kind, rig, loaded):
    executor, _, _, _, factory, resolver, _ = rig
    policy = loaded.policy
    executor._policy = LoadedDiagnosticPolicy("valid", replace(policy,
        dns=policy.dns if kind == "dns" else (), tcp=policy.tcp if kind == "tcp" else ()))
    dns = executor.dns(NAME, "A", policy.digest)
    tcp = executor.tcp(V4, 443, policy.digest)
    assert dns.outcome == ("resolved" if kind == "dns" else "denied")
    assert tcp.outcome == ("connected" if kind == "tcp" else "denied")
    assert factory.call_count == (1 if kind == "tcp" else 0)
    assert resolver.call_count == (1 if kind == "dns" else 0)


@pytest.mark.parametrize("code,outcome", [
    (errno.ECONNREFUSED, "refused"), (errno.ETIMEDOUT, "timeout"),
    (errno.ENETUNREACH, "no_route"), (errno.EHOSTUNREACH, "no_route"),
    (errno.EACCES, "permission_denied"), (errno.EPERM, "permission_denied"),
    (errno.EAFNOSUPPORT, "unavailable"), (errno.EPROTONOSUPPORT, "unavailable"),
    (errno.ENOSYS, "unavailable"), (errno.EOPNOTSUPP, "unavailable"),
    (errno.ECONNRESET, "error"), (errno.EIO, "error"), (None, "error"),
])
def test_tcp_errno_taxonomy_not_error_text(code, outcome, rig, loaded):
    executor, _, limiter, sock, _, _, _ = rig
    sock.connect_error = OSError(code, "firewall timeout refused /private/fixture/path")
    result = executor.tcp(V4, 443, loaded.policy.digest)
    assert result.outcome == outcome
    assert "/private" not in result.detail
    assert result.local is not None
    assert result.remote is None and sock.peer_reads == 0
    assert sock.closed == 1
    assert limiter.try_acquire() is None
    limiter.release()


@pytest.mark.parametrize("error,outcome", [(TimeoutError("/private/path"), "timeout"), (RuntimeError("refused"), "error")])
def test_tcp_non_errno_errors_do_not_guess_cause(error, outcome, rig, loaded):
    executor, _, _, sock, _, _, _ = rig
    sock.connect_error = error
    assert executor.tcp(V4, 443, loaded.policy.digest).outcome == outcome


@pytest.mark.parametrize("value", [("0.0.0.0", 0), (V4, True), ("bad", 123), [V4, 443], (V4,), ("fd00::1", 123, 0, 0)])
def test_socket_tuple_is_not_invented_when_observation_is_malformed(value, rig, loaded):
    executor, _, _, sock, _, _, _ = rig
    sock.local = sock.peer = value
    result = executor.tcp(V4, 443, loaded.policy.digest)
    assert result.outcome == "connected"
    assert result.local is result.remote is None


def test_socket_tuple_observation_errors_remain_unknown(rig, loaded):
    executor, _, _, sock, _, _, _ = rig
    sock.getsockname = Mock(side_effect=OSError("/private/path"))
    sock.getpeername = Mock(side_effect=OSError("/private/path"))
    result = executor.tcp(V4, 443, loaded.policy.digest)
    assert result.local is result.remote is None
    assert result.outcome == "connected"


def test_socket_creation_failure_releases_slot_without_invented_peer(rig, loaded):
    executor, _, limiter, _, factory, _, _ = rig
    factory.side_effect = OSError(errno.EAFNOSUPPORT, "/private/path")
    result = executor.tcp(V4, 443, loaded.policy.digest)
    assert result.outcome == "unavailable"
    assert result.local is result.remote is None
    assert limiter.try_acquire() is None
    limiter.release()


def test_remaining_budget_limits_connect_timeout_and_excludes_preflight_time(rig, loaded):
    executor, clock, _, sock, _, _, exclusions = rig
    def current():
        clock.now = 4.25
        return COMPLETE
    exclusions.side_effect = current
    assert executor.tcp(V4, 443, loaded.policy.digest).outcome == "connected"
    assert sock.timeouts == [.75]


def test_exclusion_deadline_prevents_socket_creation_and_fake_execution_time(rig, loaded):
    executor, clock, _, _, factory, _, exclusions = rig
    def slow_current():
        clock.now = 5.0
        return COMPLETE
    exclusions.side_effect = slow_current
    result = executor.tcp(V4, 443, loaded.policy.digest)
    assert result.outcome == "timeout"
    assert result.started_at is result.ended_at is None
    factory.assert_not_called()


def test_factory_deadline_prevents_connect_but_still_closes_socket(rig, loaded):
    executor, clock, _, sock, factory, _, _ = rig
    def slow_factory(*args):
        clock.now = 5.0
        return sock
    factory.side_effect = slow_factory
    result = executor.tcp(V4, 443, loaded.policy.digest)
    assert result.outcome == "timeout"
    assert sock.connects == [] and sock.closed == 1


def test_cleanup_holds_slot_past_response_deadline_and_real_end_is_after_cleanup(rig, loaded):
    executor, clock, _, sock, _, resolver, _ = rig
    def close():
        clock.now = 7
        assert executor.dns(NAME, "A", loaded.policy.digest).outcome == "busy"
        sock.closed += 1
    sock.close = close
    result = executor.tcp(V4, 443, loaded.policy.digest)
    assert result.outcome == "timeout"
    assert result.ended_at == EPOCH + timedelta(seconds=7)
    assert result.remote.address == V4
    resolver.assert_not_called()


def test_failed_cleanup_cannot_establish_descriptor_release_so_slot_stays_closed(rig, loaded):
    executor, _, _, sock, _, resolver, _ = rig
    sock.close = Mock(side_effect=OSError(errno.EIO, "/private/path"))
    result = executor.tcp(V4, 443, loaded.policy.digest)
    assert result.outcome == "error" and "remain blocked" in result.detail
    assert result.completion_proven is False
    assert "completion_proven" not in result.to_wire()
    assert executor.dns(NAME, "A", loaded.policy.digest).outcome == "busy"
    resolver.assert_not_called()


@pytest.mark.parametrize("record_type,answers", [("A", ("192.168.50.99", "127.0.0.1")), ("AAAA", ("fd00::99", "::1"))])
def test_dns_exact_name_record_type_and_numeric_family_without_connection_chaining(record_type, answers, rig, loaded):
    executor, clock, _, _, factory, resolver, exclusions = rig
    def resolve(name, kind, deadline):
        assert (name, kind, deadline) == (NAME, record_type, 5.0)
        clock.now = .1
        return DnsAnswer("resolved", answers, True, "Never relayed /private/fixture/provider path")
    resolver.side_effect = resolve
    result = executor.dns(NAME.upper(), record_type, loaded.policy.digest)
    assert result.completion_proven is True
    assert result.name == NAME and result.answers == answers and result.truncated is True
    assert result.started_at == EPOCH and result.ended_at == EPOCH + timedelta(seconds=.1)
    assert "/private" not in result.detail
    factory.assert_not_called()
    exclusions.assert_not_called()
    wire = result.to_wire()
    assert set(wire) == {"kind", "name", "record_type", "outcome", "vantage", "started_at", "ended_at", "answers", "truncated", "detail"}
    assert wire["answers"] == list(answers)
    wire["answers"].clear()
    assert result.answers == answers
    with pytest.raises(FrozenInstanceError):
        result.answers = ()
    with pytest.raises(FrozenInstanceError):
        result.completion_proven = False


@pytest.mark.parametrize("name", ["not-listed.example.internal.", "localhost.", "thing.local.", "metadata.google.internal.", "127.0.0.1.", "http://nas.example.internal."])
def test_dns_unlisted_or_special_name_denied_before_resolver(name, rig, loaded):
    executor, _, _, _, factory, resolver, _ = rig
    result = executor.dns(name, "A", loaded.policy.digest)
    assert result.outcome == "denied" and result.started_at is result.ended_at is None
    factory.assert_not_called()
    resolver.assert_not_called()


@pytest.mark.parametrize("answer", [
    None, {"outcome": "resolved", "answers": [V4]},
    DnsAnswer("arbitrary"), DnsAnswer("busy"), DnsAnswer("resolved", None), DnsAnswer("resolved", ()),
    DnsAnswer("resolved", [V4]), DnsAnswer("resolved", (True,)), DnsAnswer("resolved", ("not-an-ip",)),
    DnsAnswer("resolved", ("fd00::1",)), DnsAnswer("resolved", ("192.168.50.20%eth0",)),
    DnsAnswer("resolved", (V4,) * 33), DnsAnswer("resolved", (V4,), "false"),
    DnsAnswer("resolved", (V4,), False, "x" * 513), DnsAnswer("resolved", (V4,), False, None),
    DnsAnswer("no_data", (V4,)),
])
def test_dns_callback_is_untrusted_and_malformed_evidence_is_never_success(answer, rig, loaded):
    executor, _, limiter, _, factory, resolver, _ = rig
    resolver.return_value = answer
    result = executor.dns(NAME, "A", loaded.policy.digest)
    assert result.outcome == "error" and result.answers is result.truncated is None
    assert len(result.detail) <= 512
    factory.assert_not_called()
    assert limiter.try_acquire() is None
    limiter.release()


def test_dns_32_answer_boundary_and_unknown_truncation_are_preserved(rig, loaded):
    executor, _, _, _, _, resolver, _ = rig
    resolver.return_value = DnsAnswer("resolved", (V4,) * 32, None, "x" * 512)
    result = executor.dns(NAME, "A", loaded.policy.digest)
    assert result.outcome == "resolved" and len(result.answers) == 32 and result.truncated is None


@pytest.mark.parametrize("outcome", ["no_data", "not_found", "server_failure", "refused", "timeout", "unavailable", "error"])
def test_dns_controlled_outcomes_without_invented_answers(outcome, rig, loaded):
    executor, _, _, _, _, resolver, _ = rig
    resolver.return_value = DnsAnswer(outcome)
    result = executor.dns(NAME, "AAAA", loaded.policy.digest)
    assert result.outcome == outcome and result.answers is None


def test_resolver_unsupported_never_uses_nss_or_produces_execution_timestamps(rig, loaded):
    executor, _, _, _, factory, resolver, _ = rig
    executor._resolve = None
    result = executor.dns(NAME, "A", loaded.policy.digest)
    assert result.outcome == "unavailable" and result.started_at is result.ended_at is None
    factory.assert_not_called()
    resolver.assert_not_called()


@pytest.mark.parametrize("error,outcome", [(RuntimeError("/private/path"), "error"), (TimeoutError("/private/path"), "timeout")])
def test_resolver_exception_cleanup_and_no_private_error_text(error, outcome, rig, loaded):
    executor, _, limiter, _, _, resolver, _ = rig
    resolver.side_effect = error
    result = executor.dns(NAME, "A", loaded.policy.digest)
    assert result.outcome == outcome and "/private" not in result.detail
    assert limiter.try_acquire() is None
    limiter.release()


def test_synchronous_resolver_still_running_after_deadline_keeps_cross_kind_slot(rig, loaded):
    executor, clock, _, _, factory, resolver, _ = rig
    def resolve(*args):
        clock.now = 9
        nested = executor.tcp(V4, 443, loaded.policy.digest)
        assert nested.outcome == "busy" and nested.started_at is nested.ended_at is None
        return DnsAnswer("resolved", (V4,), False)
    resolver.side_effect = resolve
    result = executor.dns(NAME, "A", loaded.policy.digest)
    assert result.outcome == "timeout" and result.answers is None
    assert result.ended_at == EPOCH + timedelta(seconds=9)
    factory.assert_not_called()
    assert executor.tcp(V4, 443, loaded.policy.digest).outcome == "connected"


def test_shared_limiter_rate_applies_to_both_kinds_and_exact_rolling_boundary(rig, loaded):
    executor, clock, _, _, factory, resolver, _ = rig
    for index in range(6):
        result = executor.dns(NAME, "A", loaded.policy.digest) if index % 2 else executor.tcp(V4, 443, loaded.policy.digest)
        assert result.outcome in {"resolved", "connected"}
    clock.now = 59.999
    for kind in ("dns", "tcp"):
        result = executor.dns(NAME, "A", loaded.policy.digest) if kind == "dns" else executor.tcp(V4, 443, loaded.policy.digest)
        assert result.outcome == "rate_limited" and result.started_at is result.ended_at is None
    assert factory.call_count == resolver.call_count == 3
    clock.now = 60.0
    assert executor.tcp(V4, 443, loaded.policy.digest).outcome == "connected"


def test_rate_window_does_not_clear_younger_starts_or_charge_rejected_targets(rig, loaded):
    executor, clock, _, _, _, resolver, _ = rig
    for index in range(6):
        clock.now = index * 10
        assert executor.dns(NAME, "A", loaded.policy.digest).outcome == "resolved"
    clock.now = 60
    assert executor.dns("unlisted.example.internal.", "A", loaded.policy.digest).outcome == "denied"
    assert executor.dns(NAME, "A", loaded.policy.digest).outcome == "resolved"
    assert executor.dns(NAME, "A", loaded.policy.digest).outcome == "rate_limited"
    clock.now = 69.999
    assert executor.dns(NAME, "A", loaded.policy.digest).outcome == "rate_limited"
    clock.now = 70
    assert executor.dns(NAME, "A", loaded.policy.digest).outcome == "resolved"
    assert resolver.call_count == 8


def test_limiter_admission_is_nonblocking_when_lock_is_contended():
    limiter = DiagnosticLimiter(monotonic=lambda: 0.0)
    limiter._lock.acquire()
    try:
        assert limiter.try_acquire() == "busy"
    finally:
        limiter._lock.release()


def test_atomic_concurrent_admission_and_single_remaining_rate_token():
    limiter = DiagnosticLimiter(monotonic=lambda: 0.0)
    for _ in range(5):
        assert limiter.try_acquire() is None
        limiter.release()
    ready = threading.Barrier(8)
    admitted = threading.Barrier(8)
    results = []
    def contender():
        ready.wait(timeout=5)
        outcome = limiter.try_acquire()
        results.append(outcome)
        admitted.wait(timeout=5)
        if outcome is None:
            limiter.release()
    threads = [threading.Thread(target=contender) for _ in range(8)]
    try:
        for thread in threads:
            thread.start()
    finally:
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=6)
    assert all(not thread.is_alive() for thread in threads)
    assert results.count(None) == 1
    assert all(result in {None, "busy"} for result in results)
    assert limiter.try_acquire() == "rate_limited"


def test_executor_has_no_worker_process_nss_binding_payload_or_backend_dependency():
    source = inspect.getsource(diagnostics)
    tree = ast.parse(source)
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not any(name and name.startswith("localplane.backend") for name in imports)
    calls = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert not calls & {"getaddrinfo", "send", "sendall", "recv", "recvfrom", "bind", "Thread", "Process", "Popen", "run", "load_diagnostic_policy"}


@pytest.mark.parametrize("policy_version,complete", [(1, False), (1, True), (2, False)])
def test_v1_or_incomplete_attestation_keeps_dns_working_but_tcp_unavailable_before_any_live_callback(policy_version, complete, rig, loaded):
    executor, _, _, _, factory, resolver, exclusions = rig
    executor._policy = LoadedDiagnosticPolicy("valid", replace(loaded.policy, version=policy_version, control_plane_complete=complete))
    result = executor.tcp(V4, 443, loaded.policy.digest)
    assert result.outcome == "unavailable"
    assert result.started_at is result.ended_at is result.vantage is None
    factory.assert_not_called()
    exclusions.assert_not_called()
    assert executor.dns(NAME, "A", loaded.policy.digest).outcome == "resolved"
    resolver.assert_called_once()


def test_legacy_three_field_policy_never_silently_gains_tcp_authority(rig, loaded):
    executor, _, _, _, factory, _, _ = rig
    legacy = DiagnosticPolicy(loaded.policy.dns, loaded.policy.tcp, loaded.policy.digest)
    executor._policy = LoadedDiagnosticPolicy("valid", legacy)
    assert executor.tcp(V4, 443, legacy.digest).outcome == "unavailable"
    factory.assert_not_called()


@pytest.mark.parametrize("address,declared", [(V4, V4), (V6, "fd00:0:0:0:0:0:0:20"), (V4, "::ffff:192.168.50.20")])
@pytest.mark.parametrize("port", [22, 443, 65535])
def test_declared_control_plane_deny_wins_even_if_live_callback_omits_it(address, declared, port, rig, loaded):
    executor, _, _, _, factory, resolver, exclusions = rig
    document = {"version": 2, "dns": [NAME], "tcp": [{"address": address, "port": port}],
        "control_plane": {"complete": True, "addresses": [declared]}}
    policy = parse_diagnostic_policy(json.dumps(document).encode())
    executor._policy = LoadedDiagnosticPolicy("valid", policy)
    exclusions.return_value = CurrentExclusions((), (), ())
    result = executor.tcp(address, port, policy.digest)
    assert result.outcome == "denied" and result.started_at is result.ended_at is None
    factory.assert_not_called()
    resolver.assert_not_called()


def test_declared_and_current_live_exclusions_are_additive_not_a_discovery_claim(rig, loaded):
    executor, _, _, _, factory, _, exclusions = rig
    exclusions.return_value = CurrentExclusions((), (), ())
    assert executor.tcp(V4, 443, loaded.policy.digest).outcome == "connected"
    exclusions.return_value = CurrentExclusions((), (), (V4,))
    assert executor.tcp(V4, 443, loaded.policy.digest).outcome == "denied"
    executor._policy = LoadedDiagnosticPolicy("valid", replace(loaded.policy, control_plane_addresses=(V6,)))
    exclusions.return_value = CurrentExclusions((), (), ())
    assert executor.tcp(V6, 443, loaded.policy.digest).outcome == "denied"
    assert factory.call_count == 1


def test_readonly_active_accessor_is_truthful_nonblocking_and_does_not_clear_rate_history():
    limiter = DiagnosticLimiter(monotonic=lambda: 0.0)
    assert limiter.active() is False
    assert limiter.try_acquire() is None
    assert limiter.active() is True
    limiter._lock.acquire()
    try:
        assert limiter.active() is None
    finally:
        limiter._lock.release()
    assert limiter.active() is True
    limiter.release()
    for _ in range(5):
        assert limiter.active() is False
        assert limiter.try_acquire() is None
        assert limiter.active() is True
        limiter.release()
    assert limiter.active() is False
    assert limiter.try_acquire() == "rate_limited"
    assert limiter.active() is False


def test_active_remains_true_during_actual_callback_after_response_deadline_and_only_clears_on_return(rig, loaded):
    executor, clock, limiter, _, _, resolver, _ = rig
    def callback(*args):
        clock.now = 8
        assert limiter.active() is True
        return DnsAnswer("resolved", (V4,))
    resolver.side_effect = callback
    result = executor.dns(NAME, "A", loaded.policy.digest)
    assert result.outcome == "timeout" and limiter.active() is False


@pytest.mark.parametrize("outcome,answers", [("unavailable", None), ("resolved", (V4,))])
def test_resolver_changed_is_a_controlled_sanitized_reason_not_callback_detail(outcome, answers, rig, loaded):
    executor, _, _, _, factory, resolver, _ = rig
    resolver.return_value = DnsAnswer(outcome, answers, False, "/private/provider/path with raw owner identity", "resolver_changed")
    result = executor.dns(NAME, "A", loaded.policy.digest)
    assert result.outcome == "unavailable"
    assert result.answers is result.truncated is None
    assert result.detail == "resolver_changed: Resolver identity changed during execution; returned answers are not usable evidence."
    assert "/private" not in result.detail
    assert "reason" not in result.to_wire()
    factory.assert_not_called()


@pytest.mark.parametrize("reason", ["/private/provider/path", "not-a-reason", True, 1, ["resolver_changed"]])
def test_unknown_resolver_reason_is_not_relayed_or_treated_as_success(reason, rig, loaded):
    executor, _, _, _, _, resolver, _ = rig
    resolver.return_value = DnsAnswer("resolved", (V4,), False, "", reason)
    result = executor.dns(NAME, "A", loaded.policy.digest)
    assert result.outcome == "error" and result.answers is None
    assert "private" not in result.detail and "not-a-reason" not in result.detail


@pytest.mark.parametrize("kind", ["dns", "tcp"])
def test_only_tighter_internal_absolute_deadlines_are_applied(kind, rig, loaded):
    executor, clock, _, sock, _, resolver, _ = rig
    if kind == "dns":
        assert executor.dns(NAME, "A", loaded.policy.digest, absolute_monotonic_deadline=1.25).outcome == "resolved"
        assert resolver.call_args.args == (NAME, "A", 1.25)
        executor.dns(NAME, "A", loaded.policy.digest, absolute_monotonic_deadline=100)
        assert resolver.call_args.args == (NAME, "A", 5.0)
    else:
        assert executor.tcp(V4, 443, loaded.policy.digest, absolute_monotonic_deadline=1.25).outcome == "connected"
        assert sock.timeouts == [1.25]
        executor.tcp(V4, 443, loaded.policy.digest, absolute_monotonic_deadline=100)
        assert sock.timeouts == [1.25, 3.0]
    assert clock.now == 0


@pytest.mark.parametrize("kind", ["dns", "tcp"])
def test_already_expired_combined_budget_never_creates_io_or_execution_times(kind, rig, loaded):
    executor, _, limiter, _, factory, resolver, _ = rig
    result = executor.dns(NAME, "A", loaded.policy.digest, absolute_monotonic_deadline=-1) if kind == "dns" else executor.tcp(V4, 443, loaded.policy.digest, absolute_monotonic_deadline=-1)
    assert result.outcome == "timeout" and result.started_at is result.ended_at is None
    assert limiter.active() is False
    factory.assert_not_called()
    resolver.assert_not_called()


@pytest.mark.parametrize("deadline", [True, "1", float("nan"), float("inf"), -float("inf"), 10**1000])
@pytest.mark.parametrize("kind", ["dns", "tcp"])
def test_malformed_internal_deadlines_are_rejected_before_traffic(deadline, kind, rig, loaded):
    executor, _, _, _, factory, resolver, _ = rig
    with pytest.raises(ValueError, match="invalid internal diagnostic deadline"):
        if kind == "dns":
            executor.dns(NAME, "A", loaded.policy.digest, absolute_monotonic_deadline=deadline)
        else:
            executor.tcp(V4, 443, loaded.policy.digest, absolute_monotonic_deadline=deadline)
    factory.assert_not_called()
    resolver.assert_not_called()


@pytest.mark.parametrize("kind", ["dns", "tcp"])
@pytest.mark.parametrize("mode", ["policy", "mismatch", "unlisted", "expired", "busy", "rate_limited"])
def test_before_traffic_refusals_prove_only_their_own_synchronous_completion(kind, mode, rig, loaded):
    executor, _, limiter, _, factory, resolver, _ = rig
    digest, name, address = loaded.policy.digest, NAME, V4
    options = {}
    if mode == "policy":
        executor._policy = LoadedDiagnosticPolicy("unsafe", None)
    elif mode == "mismatch":
        digest = "b" * 64
    elif mode == "unlisted":
        name, address = "unlisted.example.internal.", "192.168.50.21"
    elif mode == "expired":
        options["absolute_monotonic_deadline"] = -1
    elif mode == "busy":
        assert limiter.try_acquire() is None
    else:
        for _ in range(6):
            assert limiter.try_acquire() is None
            limiter.release()
    try:
        result = executor.dns(name, "A", digest, **options) if kind == "dns" else executor.tcp(address, 443, digest, **options)
        assert result.completion_proven is True
        assert result.started_at is result.ended_at is None
        assert "completion_proven" not in result.to_wire()
        # A busy invocation is finished even though another invocation owns the
        # resource. Conversely, idle cannot prove an unregistered invocation done.
        assert limiter.active() is (mode == "busy")
    finally:
        if mode == "busy":
            limiter.release()
    factory.assert_not_called()
    resolver.assert_not_called()


@pytest.mark.parametrize("stage", ["preflight", "resolver", "cleanup"])
def test_blocked_invocation_cannot_return_completion_proof_even_if_global_state_is_idle(stage, rig, loaded):
    executor, clock, limiter, sock, _, resolver, exclusions = rig
    entered, release = threading.Event(), threading.Event()
    returned, errors = [], []
    def block():
        entered.set()
        if not release.wait(timeout=10):
            raise RuntimeError("fixture release did not arrive")
    if stage == "preflight":
        def current():
            block()
            return COMPLETE
        exclusions.side_effect = current
    elif stage == "resolver":
        def resolve(*args):
            block()
            return DnsAnswer("resolved", (V4,))
        resolver.side_effect = resolve
    else:
        def close():
            block()
            sock.closed += 1
        sock.close = close
    def invocation():
        try:
            result = executor.dns(NAME, "A", loaded.policy.digest, absolute_monotonic_deadline=1) if stage == "resolver" else executor.tcp(V4, 443, loaded.policy.digest, absolute_monotonic_deadline=1)
            returned.append(result)
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=invocation)
    thread.start()
    try:
        assert entered.wait(timeout=5)
        assert returned == [] and errors == []
        assert limiter.active() is (stage != "preflight")
        clock.now = 8
        assert returned == []
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive() and errors == []
    assert len(returned) == 1
    result = returned[0]
    assert result.outcome == "timeout" and result.completion_proven is True
    assert limiter.active() is False
    assert "completion_proven" not in result.to_wire()
    if stage == "preflight":
        assert result.started_at is result.ended_at is None
    else:
        assert result.ended_at == EPOCH + timedelta(seconds=8)
    if stage == "cleanup":
        assert sock.closed == 1


@pytest.mark.parametrize("kind", ["dns", "tcp"])
def test_exception_without_a_result_cannot_fabricate_completion_from_an_idle_limiter(kind, rig, loaded):
    executor, _, limiter, _, factory, resolver, _ = rig
    executor._utc_now = Mock(side_effect=RuntimeError("fixture internal clock failure"))
    result = None
    with pytest.raises(RuntimeError, match="fixture internal clock failure"):
        result = executor.dns(NAME, "A", loaded.policy.digest) if kind == "dns" else executor.tcp(V4, 443, loaded.policy.digest)
    assert result is None and limiter.active() is False
    factory.assert_not_called()
    resolver.assert_not_called()


@pytest.mark.parametrize("result_class,args", [
    (diagnostics.DnsResult, (NAME, "A", "denied", "fixture refusal")),
    (diagnostics.TcpResult, (V4, 443, "denied", "fixture refusal")),
])
def test_private_completion_field_has_no_default_that_could_be_mistaken_for_proof(result_class, args):
    with pytest.raises(TypeError, match="completion_proven"):
        result_class(*args)
    result = result_class(*args, completion_proven=False)
    assert result.completion_proven is False
    assert "completion_proven" not in result.to_wire()
