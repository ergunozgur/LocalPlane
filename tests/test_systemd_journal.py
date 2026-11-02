"""Nonsecret fixtures only. Never opens the host journal or reads real MESSAGE data."""

from __future__ import annotations

import ctypes as C
import builtins
import errno
import io
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from localplane.agent.providers import journal as collector_module
from localplane.agent.providers import journal_native as native
from localplane.agent.providers.journal import JournalCollector
from localplane.agent.providers.systemd import SystemdProvider, _LoadedUnitAbsent
from localplane.agent.service import AgentService
from localplane.backend.agent_client import AgentError, AgentClient
from localplane.protocol.journal import (
    ACCESS_GAP, MAX_ENTRIES, MESSAGE_BYTES, OUTPUT_BYTES, SCOPE_GAPS, encode, response, validate, valid_service,
)
from localplane.protocol.wire import Method, MUTATING_METHODS, ProtocolError, encode_ok_response, MAX_MESSAGE_BYTES
from tests.conftest import TEST_MASTER_SECRET
from tests.test_systemd import systemd_api  # Existing synthetic agent/backend/database estate.
from tests.test_agent_server import agent, client  # Existing non-live socket fixture.

UNIT = "alpha.service"
BOOT = "ab" * 16
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
MESSAGE = "NONSECRET fixture: dependency connection refused"


class FakeJournal:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [self.row()]
        self.index = -1
        self.fields = []

    @staticmethod
    def row(**changes):
        return {"timestamp": NOW - timedelta(seconds=1), b"_SYSTEMD_UNIT": UNIT.encode(),
                b"_BOOT_ID": BOOT.encode(), b"PRIORITY": b"3", b"MESSAGE": MESSAGE.encode(),
                b"_SYSTEMD_INVOCATION_ID": b"cd" * 16, **changes}

    def select(self, unit, boot, now):
        assert (unit, boot, now) == (UNIT, BOOT, NOW)

    def previous(self):
        self.index += 1
        return self.index < len(self.rows)

    def timestamp(self):
        return self.rows[self.index]["timestamp"]

    def field(self, name, limit, *, optional=False):
        self.fields.append(name)
        value = self.rows[self.index].get(name)
        if value is None:
            if optional:
                return None, False
            raise native.JournalFailure("malformed")
        return value[:limit], len(value) > limit


def answer():
    return native.collect_entries(FakeJournal(), UNIT, BOOT, NOW)


def test_exact_fields_current_scope_and_wire_bounds():
    journal = FakeJournal()
    result = native.collect_entries(journal, UNIT, BOOT, NOW)
    assert validate(result, UNIT) == result
    assert result["status"] == "ok"
    assert result["entries"][0]["message"] == MESSAGE
    assert result["gaps"] == list(SCOPE_GAPS)
    assert set(journal.fields) == {b"_SYSTEMD_UNIT", b"_BOOT_ID", b"_SYSTEMD_INVOCATION_ID", b"PRIORITY", b"MESSAGE"}
    assert len(encode(result)) < OUTPUT_BYTES
    assert len(encode_ok_response("fixture", {"host_id": "fixture", "journal": result})) < MAX_MESSAGE_BYTES
    assert Method.SYSTEMD_UNIT_JOURNAL not in MUTATING_METHODS


@pytest.mark.parametrize("name", ["--help.service", "/etc/x.service", "x*.service", "x?x.service", "x.service\n", "x.service\x00", "x;echo.service", "x=1.service", "x.socket", "x" * 256 + ".service", "user@1000.service", "user@0.service", "user@\\x31.service"])
def test_invalid_scope_never_spawns(name, monkeypatch):
    collector = JournalCollector()
    monkeypatch.setattr(collector, "_spawn", lambda *args: pytest.fail("must not spawn"))
    # Leading '-' is a legitimate systemd character and is not argv here.
    if name.startswith("--"):
        assert valid_service(name)  # Native API uses a literal match, never options.
    else:
        assert collector.read(None, name)["status"] == "unsupported"


@pytest.mark.parametrize("field,value,status", [
    (b"_SYSTEMD_UNIT", b"alias.service", "identity_changed"),
    (b"_BOOT_ID", b"ef" * 16, "identity_changed"),
    (b"_BOOT_ID", b"bad", "identity_changed"),
    (b"PRIORITY", b"99", "malformed"),
    (b"_SYSTEMD_INVOCATION_ID", b"not-an-id", "malformed"),
])
def test_identity_and_correlation_fail_closed(field, value, status):
    row = FakeJournal.row()
    row[field] = value
    with pytest.raises(native.JournalFailure) as caught:
        native.collect_entries(FakeJournal([row]), UNIT, BOOT, NOW)
    assert caught.value.status == status


def test_empty_old_and_future_records_remain_distinct():
    assert native.collect_entries(FakeJournal([]), UNIT, BOOT, NOW)["status"] == "empty"
    old = FakeJournal.row(timestamp=NOW - timedelta(minutes=16))
    journal = FakeJournal([old])
    assert native.collect_entries(journal, UNIT, BOOT, NOW)["status"] == "empty"
    assert not journal.fields
    with pytest.raises(native.JournalFailure):
        native.collect_entries(FakeJournal([FakeJournal.row(timestamp=NOW + timedelta(seconds=1))]), UNIT, BOOT, NOW)


def test_missing_middle_message_preserves_all_entry_metadata():
    rows = [FakeJournal.row(timestamp=NOW - timedelta(seconds=i)) for i in (1, 2, 3)]
    del rows[1][b"MESSAGE"]
    result = native.collect_entries(FakeJournal(rows), UNIT, BOOT, NOW)
    validate(result, UNIT)
    assert result["status"] == "ok" and len(result["entries"]) == 3
    assert [e["message"] for e in result["entries"]] == [MESSAGE, None, MESSAGE]
    for entry, row in zip(result["entries"], rows):
        assert datetime.fromisoformat(entry["timestamp"]) == row["timestamp"]
        assert entry["priority"] == "3" and entry["boot_id"] == BOOT
        assert entry["invocation_id"] == "cd" * 16


@pytest.mark.parametrize("access_omitted", [False, True])
def test_all_missing_messages_are_observed_records_not_empty(access_omitted):
    rows = [FakeJournal.row() for _ in range(3)]
    for row in rows:
        del row[b"MESSAGE"]
    result = native.collect_entries(FakeJournal(rows), UNIT, BOOT, NOW, access_omitted=access_omitted)
    validate(result, UNIT)
    assert result["status"] == "ok" and len(result["entries"]) == 3
    assert all(entry["message"] is None for entry in result["entries"])
    assert not result["truncated"]


def test_present_empty_message_is_not_absent():
    empty, absent = FakeJournal.row(), FakeJournal.row()
    empty[b"MESSAGE"] = b""
    del absent[b"MESSAGE"]
    result = native.collect_entries(FakeJournal([empty, absent]), UNIT, BOOT, NOW)
    assert [e["message"] for e in result["entries"]] == ["", None]
    validate(result, UNIT)


@pytest.mark.parametrize("message", [False, 0, [], {}, "x" * (MESSAGE_BYTES + 1)])
def test_message_validator_still_rejects_wrong_types_and_oversize(message):
    result = answer()
    result["entries"][0]["message"] = message
    with pytest.raises(ValueError):
        validate(result, UNIT)


def test_nullable_message_key_remains_required():
    result = answer()
    del result["entries"][0]["message"]
    with pytest.raises(ValueError):
        validate(result, UNIT)


def test_optional_fields_remain_unknown():
    row = FakeJournal.row()
    del row[b"PRIORITY"], row[b"_SYSTEMD_INVOCATION_ID"]
    entry = native.collect_entries(FakeJournal([row]), UNIT, BOOT, NOW)["entries"][0]
    assert entry["priority"] is None and entry["invocation_id"] is None


def test_entry_cap_does_not_read_101st_message():
    journal = FakeJournal([FakeJournal.row() for _ in range(101)])
    result = native.collect_entries(journal, UNIT, BOOT, NOW)
    assert len(result["entries"]) == MAX_ENTRIES
    assert result["truncated"]
    assert journal.fields.count(b"MESSAGE") == 100


@pytest.mark.parametrize("raw", [b"x" * 10000, b"\xff" * 4096, ("🦊" * 1025).encode()])
def test_displayed_utf8_byte_limit_and_truncation(raw):
    row = FakeJournal.row()
    row[b"MESSAGE"] = raw
    result = native.collect_entries(FakeJournal([row]), UNIT, BOOT, NOW)
    assert result["truncated"]
    assert len(result["entries"][0]["message"].encode()) <= MESSAGE_BYTES
    validate(result, UNIT)


@pytest.mark.parametrize("access_omitted", [False, True])
def test_total_serialized_collection_bound_includes_escaping(access_omitted):
    row = FakeJournal.row()
    row[b"MESSAGE"] = b"\x00" * 4096
    result = native.collect_entries(FakeJournal([row] * 100), UNIT, BOOT, NOW, access_omitted=access_omitted)
    assert result["truncated"] and 0 < len(result["entries"]) < 100
    assert len(encode(result)) <= OUTPUT_BYTES
    validate(result, UNIT)
    assert result["coverage"] == ("partial" if access_omitted else "complete")


def test_native_pointer_copy_is_bounded_and_prefix_checked():
    storage = C.create_string_buffer(b"MESSAGE=" + b"x" * 8000)
    class Library:
        def sd_journal_get_data(self, handle, name, pointer, size):
            assert name == b"MESSAGE"
            C.cast(pointer, C.POINTER(C.c_void_p))[0] = C.cast(storage, C.c_void_p)
            C.cast(size, C.POINTER(C.c_size_t))[0] = 8008
            return 0
    journal = native.NativeJournal.__new__(native.NativeJournal)
    journal.lib, journal.handle = Library(), None
    data, cut = journal.field(b"MESSAGE", MESSAGE_BYTES)
    assert cut and data == b"x" * MESSAGE_BYTES


@pytest.mark.parametrize("raw,code,expected", [(b"MESSAGE=", 0, b""), (b"MESSAGE=fixture", 0, b"fixture"), (None, -errno.ENOENT, None)])
def test_native_message_absence_and_empty_field_encodings(raw, code, expected):
    storage = C.create_string_buffer(raw) if raw is not None else None
    class Library:
        def sd_journal_get_data(self, handle, name, pointer, size):
            assert name == b"MESSAGE"
            if storage is not None:
                C.cast(pointer, C.POINTER(C.c_void_p))[0] = C.cast(storage, C.c_void_p)
                C.cast(size, C.POINTER(C.c_size_t))[0] = len(raw)
            return code
    journal = native.NativeJournal.__new__(native.NativeJournal)
    journal.lib, journal.handle = Library(), None
    assert journal.field(b"MESSAGE", MESSAGE_BYTES, optional=True) == (expected, False)


@pytest.mark.parametrize("raw,null_pointer,size", [(b"", False, 0), (b"MESS", False, 4), (b"INVALID=", False, 8), (b"MESSAGE=", True, 8), (b"", True, 0)])
def test_native_optional_message_still_rejects_malformed_buffers(raw, null_pointer, size):
    storage = C.create_string_buffer(raw)
    class Library:
        def sd_journal_get_data(self, handle, name, pointer, length):
            if not null_pointer:
                C.cast(pointer, C.POINTER(C.c_void_p))[0] = C.cast(storage, C.c_void_p)
            C.cast(length, C.POINTER(C.c_size_t))[0] = size
            return 0
    journal = native.NativeJournal.__new__(native.NativeJournal)
    journal.lib, journal.handle = Library(), None
    with pytest.raises(native.JournalFailure) as caught:
        journal.field(b"MESSAGE", MESSAGE_BYTES, optional=True)
    assert caught.value.status == "malformed"


@pytest.mark.parametrize("code,status", [(errno.EACCES, "denied"), (errno.EPERM, "denied"), (errno.ENOSYS, "unsupported"), (errno.EBADMSG, "malformed"), (errno.EIO, "unavailable")])
def test_native_error_vocabulary_never_quotes_payload(code, status):
    with pytest.raises(native.JournalFailure) as caught:
        native.checked(-code)
    assert str(caught.value) == status


def test_getunit_canonical_identity_before_after_and_no_getall():
    calls = []
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            calls.append("close")
        def get_unit_path(self, unit):
            calls.append(("GetUnit", unit))
            return "/provider/handle"
        def unit_canonical_id(self, handle):
            calls.append(("Id", handle))
            return UNIT
    provider = SystemdProvider(runtime_path=None, connection_factory=Connection)
    assert provider.journal_identity(UNIT) == "ok"
    assert calls == [("GetUnit", UNIT), ("Id", "/provider/handle"), "close"]
    Connection.unit_canonical_id = lambda *args: "alias.service"
    assert provider.journal_identity(UNIT) == "identity_changed"
    def absent(*args):
        raise _LoadedUnitAbsent("nonsecret fixture exception")
    Connection.get_unit_path = absent
    assert provider.journal_identity(UNIT) == "identity_changed"


@pytest.mark.parametrize("changed", ["unit", "boot", "none"])
def test_post_read_identity_recheck_discards_all_messages(monkeypatch, changed):
    checks = iter(["ok", "identity_changed" if changed == "unit" else "ok"])
    boots = iter([BOOT, "ef" * 16 if changed == "boot" else BOOT])
    systemd = SimpleNamespace(journal_identity=lambda unit: next(checks))
    monkeypatch.setattr(native, "boot_id", lambda: next(boots))
    @contextmanager
    def files():
        yield native.JournalFiles([b"fixture"])
    @contextmanager
    def reader(paths):
        yield object()
    monkeypatch.setattr(native, "system_files", files)
    monkeypatch.setattr(native, "NativeJournal", reader)
    fixture_answer = answer()
    monkeypatch.setattr(native, "collect_entries", lambda *args, **kwargs: fixture_answer)
    result = native.collect(systemd, UNIT)
    assert result["status"] == ("ok" if changed == "none" else "identity_changed")
    if changed != "none":
        assert not result["entries"] and MESSAGE not in json.dumps(result)


def test_denied_never_becomes_empty_or_leaks_error(monkeypatch):
    systemd = SimpleNamespace(journal_identity=lambda unit: "ok")
    monkeypatch.setattr(native, "boot_id", lambda: BOOT)
    def denied():
        raise PermissionError(MESSAGE)
    monkeypatch.setattr(native, "system_files", denied)
    result = native.collect(systemd, UNIT)
    assert result["status"] == "denied" and MESSAGE not in json.dumps(result)


def test_system_file_selection_is_fixed_pinned_and_field_free(tmp_path, monkeypatch):
    machine = "12" * 16
    runtime, persistent = tmp_path / "runtime", tmp_path / "persistent"
    runtime.mkdir()
    persistent.mkdir()
    (runtime / "system.journal").write_bytes(b"NONSECRET synthetic bytes")
    (persistent / "system@1234-abcd.journal").write_bytes(b"NONSECRET synthetic bytes")
    (runtime / "user-1000.journal").write_bytes(b"NONSECRET system service UID-split fixture")
    (persistent / "user-1000@abcd-1234.journal").write_bytes(b"NONSECRET archived UID-split fixture")
    (runtime / "other.txt").write_bytes(b"NONSECRET excluded fixture")
    original_open = os.open
    accesses = []
    def open_fixed(path, flags, **kwargs):
        accesses.append(str(path))
        if path == f"/run/log/journal/{machine}":
            path = runtime
        elif path == f"/var/log/journal/{machine}":
            path = persistent
        else:
            assert path in {"system.journal", "system@1234-abcd.journal", "user-1000.journal", "user-1000@abcd-1234.journal"}
        return original_open(path, flags, **kwargs)
    def machine_id(path, *args, **kwargs):
        assert path == "/etc/machine-id"
        return io.StringIO(machine)
    monkeypatch.setattr(builtins, "open", machine_id)
    monkeypatch.setattr(os, "open", open_fixed)
    with native.system_files() as files:
        assert len(files.paths) == 4 and not files.access_omitted
        fds = [int(path.decode().rsplit("/", 1)[1]) for path in files.paths]
        assert all(os.fstat(fd).st_size for fd in fds)
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)
    assert "user-1000.journal" in accesses and "other.txt" not in accesses


def test_system_file_permission_error_closes_previously_pinned_files(tmp_path, monkeypatch):
    machine = "12" * 16
    directory = tmp_path / "journal"
    directory.mkdir()
    (directory / "system.journal").write_bytes(b"NONSECRET fixture")
    original_open = os.open
    pinned = []
    def open_fixed(path, flags, **kwargs):
        if path == f"/run/log/journal/{machine}":
            return original_open(directory, flags)
        if path == f"/var/log/journal/{machine}":
            raise PermissionError(MESSAGE)
        assert path == "system.journal"
        fd = original_open(path, flags, **kwargs)
        pinned.append(fd)
        return fd
    monkeypatch.setattr(builtins, "open", lambda *args, **kwargs: io.StringIO(machine))
    monkeypatch.setattr(os, "open", open_fixed)
    with native.system_files() as files:
        assert files.access_omitted and len(files.paths) == 1
    assert pinned
    for fd in pinned:
        with pytest.raises(OSError):
            os.fstat(fd)


def fixture_journal_files(tmp_path, monkeypatch, *, omission, readable=True, matches=True):
    """Real pinned fixture descriptors; fake native iteration, never real journals."""
    machine = "12" * 16
    runtime, persistent = tmp_path / "runtime", tmp_path / "persistent"
    runtime.mkdir()
    persistent.mkdir()
    if omission != "missing":
        (runtime / "system.journal").write_bytes(b"NONSECRET system-file fixture")
    if readable:
        (persistent / "user-1000.journal").write_bytes(b"NONSECRET UID-split system-service fixture")
    original_open, original_scandir = os.open, os.scandir
    runtime_fds = set()
    accesses, native_paths = [], []
    def open_fixed(path, flags, **kwargs):
        accesses.append(str(path))
        if path == f"/run/log/journal/{machine}":
            if omission == "directory":
                raise PermissionError("NONSECRET inaccessible directory")
            fd = original_open(runtime, flags)
            runtime_fds.add(fd)
            return fd
        if path == f"/var/log/journal/{machine}":
            fd = original_open(persistent, flags)
            runtime_fds.discard(fd)  # Descriptor numbers can be reused after close.
            return fd
        assert path in {"system.journal", "user-1000.journal"}
        if path == "system.journal" and omission == "file":
            raise PermissionError("NONSECRET inaccessible system file")
        return original_open(path, flags, **kwargs)
    def scan(fd):
        if omission == "scan" and fd in runtime_fds:
            raise PermissionError("NONSECRET inaccessible enumeration")
        return original_scandir(fd)
    def machine_id(path, *args, **kwargs):
        assert path == "/etc/machine-id"
        return io.StringIO(machine)
    @contextmanager
    def reader(paths):
        names = [Path(os.fsdecode(os.readlink(path))).name for path in paths]
        native_paths.extend(names)
        assert set(names) <= {"system.journal", "user-1000.journal"}
        rows = [FakeJournal.row()] if "user-1000.journal" in names and matches else []
        yield FakeJournal(rows)
    class Clock(datetime):
        @classmethod
        def now(cls, *args):
            return NOW
    monkeypatch.setattr(builtins, "open", machine_id)
    monkeypatch.setattr(os, "open", open_fixed)
    monkeypatch.setattr(os, "scandir", scan)
    monkeypatch.setattr(native, "NativeJournal", reader)
    monkeypatch.setattr(native, "boot_id", lambda: BOOT)
    monkeypatch.setattr(native, "datetime", Clock)
    return accesses, native_paths


@pytest.mark.parametrize("omission", ["none", "file", "directory", "scan"])
@pytest.mark.parametrize("matches", [True, False])
def test_uid_split_system_service_evidence_and_access_coverage(tmp_path, monkeypatch, omission, matches):
    accesses, paths = fixture_journal_files(tmp_path, monkeypatch, omission=omission, matches=matches)
    checks = []
    systemd = SimpleNamespace(journal_identity=lambda unit: checks.append(unit) or "ok")
    result = native.collect(systemd, UNIT)
    validate(result, UNIT)
    assert checks == [UNIT, UNIT]
    assert "user-1000.journal" in paths
    partial = omission != "none"
    assert result["coverage"] == ("partial" if partial else "complete")
    assert (ACCESS_GAP in result["gaps"]) == partial
    assert result["status"] == ("ok" if matches else "denied" if partial else "empty")
    assert bool(result["entries"]) == matches
    if matches:
        assert result["entries"][0]["message"] == MESSAGE
    if omission == "file":
        assert "system.journal" not in paths
        assert accesses.count("system.journal") == 1  # No retry/bypass of denial.
    assert "user-1000.journal" not in json.dumps(result)
    assert "inaccessible system file" not in json.dumps(result)


@pytest.mark.parametrize("omission", ["file", "directory", "scan", "missing"])
def test_no_readable_files_denied_or_unavailable_never_empty(tmp_path, monkeypatch, omission):
    _, paths = fixture_journal_files(tmp_path, monkeypatch, omission=omission, readable=False)
    result = native.collect(SimpleNamespace(journal_identity=lambda unit: "ok"), UNIT)
    validate(result, UNIT)
    assert result["status"] == ("unavailable" if omission == "missing" else "denied")
    assert result["coverage"] == ("unknown" if omission == "missing" else "partial")
    assert not result["entries"] and not paths


@pytest.mark.parametrize("target", ["user@1000.service", "user@0.service", "user@\\x31.service"])
def test_user_manager_aggregate_target_refused_before_identity_or_collection(target):
    from localplane.backend.systemd_journal import read_journal
    systemd = SimpleNamespace(journal_identity=lambda unit: pytest.fail("must not resolve aggregate target"))
    assert native.collect(systemd, target)["status"] == "unsupported"
    assert read_journal(None, SimpleNamespace(identity_value=target))["status"] == "unsupported"


@pytest.mark.parametrize("fault", ["missing", "unknown_success", "invalid", "partial_without_gap", "complete_with_gap", "partial_empty", "complete_failure"])
def test_coverage_contract_fails_closed(fault):
    result = answer()
    if fault == "missing":
        del result["coverage"]
    elif fault == "unknown_success":
        result["coverage"] = "unknown"
    elif fault == "invalid":
        result["coverage"] = "all-history"
    elif fault == "partial_without_gap":
        result["coverage"] = "partial"
    elif fault == "complete_with_gap":
        result["gaps"].append(ACCESS_GAP)
    elif fault == "partial_empty":
        result = response("empty", UNIT, now=NOW, coverage="partial")
        result["boot_id"] = BOOT
    else:
        result = response("unavailable", UNIT, now=NOW, coverage="complete")
    with pytest.raises(ValueError):
        validate(result, UNIT)


class PendingChild:
    def __init__(self):
        self.stdout = io.BytesIO()
        self.done = False
        self.killed = 0
    def poll(self):
        return 0 if self.done else None
    def kill(self):
        self.killed += 1


def test_timeout_retains_single_slot_through_actual_reap(monkeypatch):
    collector = JournalCollector()
    child = PendingChild()
    spawned = []
    monkeypatch.setattr(collector, "_spawn", lambda *args: spawned.append(child) or child)
    def timeout(*args):
        raise TimeoutError(MESSAGE)
    monkeypatch.setattr(collector, "_receive", timeout)
    result = collector.read(None, UNIT)
    assert result["status"] == "timeout" and child.killed == 1 and child.stdout.closed
    for _ in range(20):
        assert collector.read(None, UNIT)["status"] == "busy"
    assert len(spawned) == 1
    child.done = True
    next_child = PendingChild()
    monkeypatch.setattr(collector, "_spawn", lambda *args: spawned.append(next_child) or next_child)
    assert collector.read(None, UNIT)["status"] == "timeout"
    assert len(spawned) == 2


def test_overlapping_calls_busy_without_queue(monkeypatch):
    collector = JournalCollector()
    entered, release = threading.Event(), threading.Event()
    child = PendingChild()
    monkeypatch.setattr(collector, "_spawn", lambda *args: child)
    def receive(*args):
        entered.set()
        assert release.wait(2)
        return answer()
    monkeypatch.setattr(collector, "_receive", receive)
    thread = threading.Thread(target=lambda: collector.read(None, UNIT))
    thread.start()
    try:
        assert entered.wait(2)
        assert collector.read(None, UNIT)["status"] == "busy"
    finally:
        release.set()
        thread.join(2)


def test_actual_disposable_child_nonsecret_output_and_timeout(monkeypatch):
    # Real fork/pipe/select/kill/waitpid; collect replaced before fork, so never host data.
    monkeypatch.setattr(collector_module, "collect", lambda *args: answer())
    collector = JournalCollector()
    assert collector.read(None, UNIT)["entries"][0]["message"] == MESSAGE
    if collector._child is not None:
        os.waitpid(collector._child.pid, 0)
        collector._child = None
    def stall(*args):
        time.sleep(10)
    monkeypatch.setattr(collector_module, "collect", stall)
    monkeypatch.setattr(collector_module, "TOTAL_SECONDS", 0.1)
    started = time.monotonic()
    assert collector.read(None, UNIT)["status"] == "timeout"
    assert time.monotonic() - started < 1
    if collector._child is not None:
        os.waitpid(collector._child.pid, 0)


@pytest.mark.parametrize("output", [b"{bad", b"x" * (OUTPUT_BYTES + 1), b"{}"])
def test_malformed_or_oversize_child_output_is_sanitized(monkeypatch, output):
    monkeypatch.setattr(collector_module, "collect", lambda *args: answer())
    monkeypatch.setattr(collector_module, "encode", lambda *args: output)
    collector = JournalCollector()
    result = collector.read(None, UNIT)
    assert result["status"] == "malformed" and not result["entries"]
    if collector._child is not None:
        os.waitpid(collector._child.pid, 0)


def api_target(client):
    client.post("/api/v1/systemd/observations/refresh").raise_for_status()
    units = client.get("/api/v1/systemd/units").json()["units"]
    unit = next(u for u in units if u["canonical_id"] == UNIT)
    return f"/api/v1/systemd/units/{unit['object_id']}/journal", unit["object_id"]


def test_http_seam_ack_auth_no_store_and_no_persistence(systemd_api, monkeypatch, caplog):
    client, state = systemd_api
    url, object_id = api_target(client)
    context = client.app.state.context
    record = context.ingestor.objects.get(object_id)
    calls = []
    def read(unit, host):
        calls.append((unit, host))
        return {"host_id": host, "journal": answer()}
    monkeypatch.setattr(context.client, "systemd_unit_journal", read)
    # All durable tables, including runs/changes/observations, stay byte-for-byte equal.
    before = list(context.database.connection.iterdump())
    for body in ({}, {"acknowledge_sensitive": False}, {"acknowledge_sensitive": "true"}, {"acknowledge_sensitive": 1}, {"acknowledge_sensitive": True, "query": MESSAGE}):
        result = client.post(url, json=body)
        assert result.status_code == 422 and result.headers["cache-control"] == "no-store"
        assert MESSAGE not in result.text
    assert client.post(url + "?unit=other.service", json={"acknowledge_sensitive": True}).status_code == 422
    assert not calls
    result = client.post(url, json={"acknowledge_sensitive": True})
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    assert result.json()["object_id"] == object_id and result.json()["entries"][0]["message"] == MESSAGE
    assert calls == [(UNIT, record.host_id)]
    assert list(context.database.connection.iterdump()) == before
    assert MESSAGE not in caplog.text
    client.headers.pop("Authorization")
    denied = client.post(url, json={"acknowledge_sensitive": True})
    assert denied.status_code == 401 and denied.headers["cache-control"] == "no-store"
    # Normal cookie login followed by exact Origin enforcement, with no bypass.
    login = client.post("/api/v1/session", headers={"Authorization": f"Bearer {TEST_MASTER_SECRET}"})
    assert login.status_code == 200
    for origin in (None, "http://hostile.invalid", "null", "http://testserver:invalid"):
        headers = {} if origin is None else {"Origin": origin}
        denied = client.post(url, json={"acknowledge_sensitive": True}, headers=headers)
        assert denied.status_code == 403 and denied.headers["cache-control"] == "no-store"
    assert client.post(url, json={"acknowledge_sensitive": True}, headers={"Origin": "http://testserver"}).status_code == 200


@pytest.mark.parametrize("status", ["empty", "denied", "unavailable", "unsupported", "timeout", "busy", "identity_changed", "malformed"])
def test_typed_failures_no_store_no_messages(systemd_api, monkeypatch, status):
    client, _ = systemd_api
    url, _ = api_target(client)
    result = response(status, UNIT, now=NOW)
    if status == "empty":
        result["boot_id"] = BOOT
        result["coverage"] = "complete"
    monkeypatch.setattr(client.app.state.context.client, "systemd_unit_journal", lambda unit, host: {"host_id": host, "journal": result})
    reply = client.post(url, json={"acknowledge_sensitive": True})
    assert reply.status_code == 200 and reply.json()["status"] == status
    assert not reply.json()["entries"] and reply.headers["cache-control"] == "no-store"
    assert reply.json()["coverage"] == ("complete" if status == "empty" else "unknown")


@pytest.mark.parametrize("missing_message", [False, True])
def test_partial_permitted_evidence_crosses_http_without_persistence(systemd_api, monkeypatch, missing_message):
    client, _ = systemd_api
    url, _ = api_target(client)
    result = native.collect_entries(FakeJournal(), UNIT, BOOT, NOW, access_omitted=True)
    if missing_message:
        result["entries"][0]["message"] = None
    context = client.app.state.context
    monkeypatch.setattr(context.client, "systemd_unit_journal", lambda unit, host: {"host_id": host, "journal": result})
    before = list(context.database.connection.iterdump())
    reply = client.post(url, json={"acknowledge_sensitive": True})
    assert reply.status_code == 200 and reply.headers["cache-control"] == "no-store"
    assert reply.json()["status"] == "ok" and reply.json()["coverage"] == "partial"
    assert reply.json()["entries"][0]["message"] == (None if missing_message else MESSAGE)
    assert ACCESS_GAP in reply.json()["gaps"]
    assert list(context.database.connection.iterdump()) == before


@pytest.mark.parametrize("fault", ["host", "unit", "boot", "extra", "error", "message_limit", "gaps"])
def test_backend_rejects_agent_identity_shape_or_error_without_echo(systemd_api, monkeypatch, fault, caplog):
    client, _ = systemd_api
    url, _ = api_target(client)
    def read(unit, host):
        result = answer()
        if fault == "host":
            host = "another-host"
        elif fault == "unit":
            result["canonical_id"] = "other.service"
        elif fault == "boot":
            result["entries"][0]["boot_id"] = "ef" * 16
        elif fault == "extra":
            result["entries"][0]["environment"] = MESSAGE
        elif fault == "message_limit":
            result["entries"][0]["message"] = "x" * 4097
        elif fault == "gaps":
            result["gaps"] = [MESSAGE]
        else:
            raise AgentError("provider_error", MESSAGE, {"stderr": MESSAGE})
        return {"host_id": host, "journal": result}
    monkeypatch.setattr(client.app.state.context.client, "systemd_unit_journal", read)
    reply = client.post(url, json={"acknowledge_sensitive": True})
    assert reply.status_code == 200 and reply.json()["status"] not in {"ok", "empty"}
    assert not reply.json()["entries"]
    assert MESSAGE not in reply.text and MESSAGE not in caplog.text


def test_missing_object_no_store_and_get_cannot_read(systemd_api):
    client, _ = systemd_api
    url = "/api/v1/systemd/units/not-an-object/journal"
    assert client.post(url, json={"acknowledge_sensitive": True}).status_code == 404
    for method in (client.get, client.post):
        result = method(url)
        assert result.headers["cache-control"] == "no-store"


def test_privacy_boundary_matches_only_actual_single_object_journal_subroute(systemd_api):
    client, _ = systemd_api
    for path in (
        "/api/v1/systemd/units/not-an-object/journal",
        "/api/v1/systemd/units/not-an-object/journal/",
        "/api/v1/systemd/units/journal/journal",
    ):
        result = client.get(path, follow_redirects=False)
        assert result.headers["cache-control"] == "no-store"
    for path in (
        "/api/v1/systemd/units/journal",
        "/api/v1/systemd/units/journal/",
        "/api/v1/systemd/units//journal",
        "/api/v1/systemd/units/a/b/journal",
        "/api/v1/systemd/units/a/journal//",
    ):
        result = client.get(path, follow_redirects=False)
        assert "cache-control" not in result.headers


def test_unexpected_http_failure_is_no_store_without_raw_exception(systemd_api, monkeypatch, caplog):
    from localplane.backend.api import routes
    client, _ = systemd_api
    url, _ = api_target(client)
    def fail(*args):
        raise RuntimeError(MESSAGE)
    monkeypatch.setattr(routes, "read_journal", fail)
    caplog.clear()
    result = client.post(url, json={"acknowledge_sensitive": True}, headers={"X-Fixture": "NONSECRET header fixture"})
    assert result.status_code == 500 and result.headers["cache-control"] == "no-store"
    assert MESSAGE not in result.text and MESSAGE not in caplog.text
    events = [record for record in caplog.records if record.name == "localplane.backend.app"]
    assert len(events) == 1
    event = events[0]
    assert event.levelno == logging.ERROR and event.msg == "journal_request_failed"
    assert event.code == "journal_unavailable" and event.args == ()
    assert event.exc_info is None and event.stack_info is None
    assert MESSAGE not in repr(event.__dict__)
    assert TEST_MASTER_SECRET not in repr(event.__dict__)
    assert "NONSECRET header fixture" not in repr(event.__dict__)


def test_agent_closed_params_and_host_mismatch_do_not_collect():
    service = AgentService.__new__(AgentService)
    service._identity = lambda: SimpleNamespace(host_id="fixture-host")
    service._root = Path("/")
    service._systemd = None
    service._journal = SimpleNamespace(read=lambda *args: answer())
    for params in ({}, {"unit_id": UNIT}, {"unit_id": UNIT, "host_id": "fixture-host", "argv": [MESSAGE]}, {"unit_id": "*.service", "host_id": "fixture-host"}):
        with pytest.raises(ProtocolError) as caught:
            service._systemd_unit_journal(params)
        assert MESSAGE not in str(caught.value)
    result = service._systemd_unit_journal({"unit_id": UNIT, "host_id": "other"})
    assert result["journal"]["status"] == "identity_changed"
    assert service._systemd_unit_journal({"unit_id": UNIT, "host_id": "fixture-host"})["journal"] == answer()
    identities = iter(["fixture-host", "changed-host"])
    service._identity = lambda: SimpleNamespace(host_id=next(identities))
    assert service._systemd_unit_journal({"unit_id": UNIT, "host_id": "fixture-host"})["journal"]["status"] == "identity_changed"


def test_capability_discovery_never_reads_journal_content(monkeypatch):
    from localplane.agent.capabilities import _probe_journal
    from localplane.protocol.capabilities import CAPABILITY_SYSTEMD_UNIT_JOURNAL
    monkeypatch.setattr(native, "collect", lambda *args: pytest.fail("must be on demand"))
    capability = _probe_journal()
    assert capability.capability == CAPABILITY_SYSTEMD_UNIT_JOURNAL
    assert capability.status == "degraded" and not capability.mutating
    assert capability.reason == "journal_access_unverified"


def test_exact_generated_journal_contract():
    from localplane.backend.app import create_app
    current = create_app().openapi()
    committed = json.loads((Path(__file__).parents[1] / "web/openapi.json").read_text())
    assert current == committed
    schema = current["components"]["schemas"]
    assert schema["SystemdJournalRequest"]["additionalProperties"] is False
    assert "coverage" in schema["SystemdJournalResponse"]["required"]
    assert set(schema["SystemdJournalEntry"]["required"]) == {"timestamp", "priority", "message", "boot_id", "invocation_id"}
    assert schema["SystemdJournalEntry"]["properties"]["message"]["anyOf"] == [{"type": "string"}, {"type": "null"}]


def test_typed_client_has_fixed_parameters_and_timeout(monkeypatch):
    client = AgentClient("/nonexistent-fixture")
    calls = []
    monkeypatch.setattr(client, "call", lambda *args, **kwargs: calls.append((args, kwargs)))
    client.systemd_unit_journal(UNIT, "fixture-host")
    assert calls == [((Method.SYSTEMD_UNIT_JOURNAL, {"unit_id": UNIT, "host_id": "fixture-host"}), {"timeout_s": 4.0})]


def test_real_ipc_carries_only_validated_ephemeral_evidence(agent, client, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    host = agent.service._identity().host_id
    monkeypatch.setattr(agent.service, "_journal", SimpleNamespace(read=lambda *args: answer()))
    assert client.systemd_unit_journal(UNIT, host)["journal"] == answer()
    def failure(*args):
        raise RuntimeError(MESSAGE)
    monkeypatch.setattr(agent.service, "_journal", SimpleNamespace(read=failure))
    failed = client.systemd_unit_journal(UNIT, host)
    assert failed["journal"]["status"] == "unavailable"
    assert MESSAGE not in json.dumps(failed) and MESSAGE not in caplog.text
