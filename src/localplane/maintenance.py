"""Durable maintenance admission, shared by the backend, the agent and the maintenance CLI.

**The witness is a file, and it is deliberately outside the store.** A maintenance window has
to survive the restart of either service and must not be restored along with the database:
a restored copy of the witness would make an old snapshot able to re-open a window that was
already closed. It is therefore a small JSON record in a shared directory, written
atomically and never copied into a backup.

**Nothing here expires.** A window is opened and it stays opened until somebody closes it
with the identifier it was opened with. There is no TTL, no lease renewal and no deadline a
clock can satisfy; a fence that could lapse while an operator was still replacing files
would be a fence that fails exactly when it is needed.

**The two locks answer two different questions.**

* ``control.lock`` is held shared by every request or executor that can write LocalPlane's
  records or change the host, and held exclusively by a maintenance transition (open,
  close, snapshot, restore). An exclusive holder therefore drains every in-flight writer,
  across processes, rather than assuming one process's request counter is the whole story.
  This is what makes the agent's autonomous guard firing visible to the same barrier as a
  backend request: both open the same file and take a shared lock on it.
* ``runtime.lock`` is held shared for the whole lifetime of a backend or agent process and
  exclusively by the maintenance procedure that takes an offline snapshot or restores one.
  A service that is restarted during that procedure cannot acquire its shared lifetime
  lease until the exclusive holder is finished, so a restart races safely rather than
  writing into a half-copied store.

Lock files are opened without following symlinks and are never unlinked, renamed or
chmodded after creation. Replacing a lock file with a new inode would let two processes
hold "the same" lock on different files, so the inode is part of the protocol.

Standard library only: the CLI and the two services must agree without a third-party
cross-process locking package, and the agent's dependency boundary is small on purpose.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

FORMAT_VERSION = 1
"""The only witness format this build writes and the only one it accepts."""

WINDOW_FILENAME = "window.json"
CONTROL_LOCK_FILENAME = "control.lock"
RUNTIME_LOCK_FILENAME = "runtime.lock"
EXECUTOR_LOCK_FILENAME = "executors.lock"
RESTORE_FILENAME = "restore.json"
RESTORE_INCOMPLETE = "maintenance_restore_incomplete"
EXECUTOR_BOOKKEEPING_TIMEOUT_S = 5.0
EXECUTOR_PREFIX = "executor_"
MAX_EXECUTORS = 4096

MAX_WITNESS_BYTES = 64 * 1024
"""A witness record is a handful of scalars. Anything larger is not one."""

DEFAULT_LOCK_TIMEOUT_S = 0.0

ACTIVE = "active"
CLOSED = "closed"
CLEAR = "clear"
UNKNOWN = "unknown"
DISABLED = "disabled"

_REFUSED = "maintenance_refused"
_WITNESS_UNKNOWN = "maintenance_witness_unknown"
_WITNESS_UNSAFE = "maintenance_witness_unsafe"
_WITNESS_MALFORMED = "maintenance_witness_malformed"
_WINDOW_ACTIVE = "maintenance_window_active"
_WINDOW_NOT_ACTIVE = "maintenance_window_not_active"
_WINDOW_MISMATCH = "maintenance_window_mismatch"
_WINDOW_BINDING_INVALID = "maintenance_snapshot_binding_invalid"
_BUSY = "maintenance_busy"
_UNCONFIGURED = "maintenance_unconfigured"
_ADMISSION_UNAVAILABLE = "maintenance_admission_unavailable"
_RUNTIME_HELD = "maintenance_runtime_held"

_ALLOWED_WITNESS_KEYS = frozenset(
    {
        "format_version",
        "window_id",
        "generation",
        "state",
        "opened_at",
        "closed_at",
        "accepted_at",
        "release_build_id",
        "snapshot_id",
        "reason",
        "outcome",
    }
)


class MaintenanceError(RuntimeError):
    """The maintenance mechanism could not answer, or answered "no"."""


class MaintenanceRefused(MaintenanceError):
    """A transition or admission was refused for a typed, reportable reason."""

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}

    def as_body(self) -> dict[str, Any]:
        """The existing API error envelope, without leaking paths or OS text."""
        return self.as_dict()


@dataclass(frozen=True)
class WindowRecord:
    """One durable maintenance window, exactly as it is written to the witness."""

    window_id: str
    generation: int
    state: str
    opened_at: str
    closed_at: str | None = None
    accepted_at: str | None = None
    release_build_id: str | None = None
    snapshot_id: str | None = None
    reason: str | None = None
    outcome: str | None = None

    @property
    def active(self) -> bool:
        return self.state == ACTIVE

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "generation": self.generation,
            "state": self.state,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "accepted_at": self.accepted_at,
            "release_build_id": self.release_build_id,
            "snapshot_id": self.snapshot_id,
            "reason": self.reason,
            "outcome": self.outcome,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "WindowRecord":
        _validate_witness(value)
        return cls(
            window_id=value["window_id"],
            generation=value["generation"],
            state=value["state"],
            opened_at=value["opened_at"],
            closed_at=value.get("closed_at"),
            accepted_at=value.get("accepted_at"),
            release_build_id=value.get("release_build_id"),
            snapshot_id=value.get("snapshot_id"),
            reason=value.get("reason"),
            outcome=value.get("outcome"),
        )


@dataclass(frozen=True)
class MaintenanceState:
    """What a caller can say about the fence from the witness alone.

    ``unknown`` is a real answer: the witness exists but could not be read or parsed, so no
    mutation may proceed and no snapshot may be taken until somebody fixes it. ``disabled``
    means no maintenance root is configured at all (a development or fixture process).
    """

    status: str
    root_id: str | None
    window: WindowRecord | None
    refusal: dict[str, Any] | None
    read_at: str

    @property
    def active(self) -> bool:
        return self.status == ACTIVE

    @property
    def fenced(self) -> bool:
        """Whether normal control/record writes must be refused."""
        return self.status == ACTIVE or self.status == UNKNOWN

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "active": self.active,
            "window": self.window.as_dict() if self.window is not None else None,
            "refusal": self.refusal,
            "root_id": self.root_id,
            "as_of": self.read_at,
        }


@dataclass(frozen=True)
class SnapshotBinding:
    """The part of a snapshot manifest that ties it to the window that produced it.

    A random window identifier is never reused, so a snapshot from a closed window cannot be
    applied to a later one even if the operations look identical. ``generation`` is
    informational and, when present, must agree too.
    """

    window_id: str
    generation: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"window_id": self.window_id, "generation": self.generation}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SnapshotBinding":
        if not isinstance(value, Mapping):
            raise MaintenanceRefused(
                _WINDOW_BINDING_INVALID, "the snapshot has no maintenance window binding"
            )
        window_id = value.get("window_id")
        generation = value.get("generation")
        if not isinstance(window_id, str) or not window_id:
            raise MaintenanceRefused(
                _WINDOW_BINDING_INVALID, "the snapshot's maintenance window id is missing"
            )
        if generation is not None and (
            isinstance(generation, bool) or not isinstance(generation, int)
        ):
            raise MaintenanceRefused(
                _WINDOW_BINDING_INVALID, "the snapshot's window generation is not an integer"
            )
        return cls(window_id=window_id, generation=generation)


class MaintenanceController:
    """Open, query and close the durable window; admit writers under it.

    One instance is cheap; it holds no state except the configured root. All coordination
    happens in the files and locks, which is what lets a CLI process, a backend process and
    an agent process participate in the same decision.
    """

    def __init__(
        self,
        root: str | Path | None,
        *,
        service: str,
        lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
    ) -> None:
        self._root = Path(root) if root is not None else None
        self._service = service
        self._lock_timeout_s = lock_timeout_s

    # ------------------------------------------------------------------------- identity

    @property
    def enabled(self) -> bool:
        return self._root is not None

    @property
    def root(self) -> Path | None:
        """The configured directory. Internal callers may log it; the API must not."""
        return self._root

    @property
    def root_id(self) -> str | None:
        if self._root is None:
            return None
        digest = hashlib.sha256(os.fsencode(str(self._root))).hexdigest()[:24]
        return f"root_{digest}"

    # --------------------------------------------------------------------------- reads

    def initialize(self) -> WindowRecord:
        """Create genesis only in a new, empty controlled directory, before any state exists.

        Existing lock files, witnesses, or any other entry make this refuse; this is not a
        repair operation. The installer must establish that application state is also new.
        """
        if self._root is None:
            raise MaintenanceRefused(_UNCONFIGURED, "no shared maintenance directory is configured")
        _ensure_root(self._root, create=True)
        if any(self._root.iterdir()):
            raise MaintenanceRefused(_WITNESS_UNSAFE, "maintenance directory is not empty")
        with _transition_lock(self._root, self._lock_timeout_s):
            if {p.name for p in self._root.iterdir()} != {CONTROL_LOCK_FILENAME}:
                raise MaintenanceRefused(_WITNESS_UNSAFE, "maintenance directory is not empty")
            now = _now()
            genesis = WindowRecord(window_id=f"w_{secrets.token_hex(16)}", generation=1,
                                   state=CLOSED, opened_at=now, closed_at=now,
                                   outcome="initialized")
            self._write_witness(genesis.as_dict())
            return genesis

    def state(self) -> MaintenanceState:
        """Read the fence without mutating anything and without raising.

        A missing witness is unknown, including after accidental deletion. An
        unreadable, unsafe or malformed witness is *unknown* and every admission caller
        treats that as fenced, because the alternative is to guess that a file which may be
        mid-replacement means "no maintenance".
        """
        now = _now()
        if self._root is None:
            return MaintenanceState(DISABLED, None, None, None, now)
        try:
            witness = self._read_witness()
        except MaintenanceRefused as exc:
            return MaintenanceState(UNKNOWN, self.root_id, None, exc.as_dict(), now)
        except OSError as exc:
            return MaintenanceState(
                UNKNOWN,
                self.root_id,
                None,
                {
                    "code": _ADMISSION_UNAVAILABLE,
                    "message": "the maintenance witness could not be read",
                    "detail": {"errno": exc.errno},
                },
                now,
            )
        if witness is None:
            return MaintenanceState(UNKNOWN, self.root_id, None,
                                    {"code": _WITNESS_UNKNOWN, "message": "maintenance witness missing", "detail": {}}, now)
        record = WindowRecord.from_mapping(witness)
        try:
            pending = self._read_restore()
            markers = self._executor_markers()
        except MaintenanceRefused as exc:
            return MaintenanceState(UNKNOWN, self.root_id, record, exc.as_dict(), now)
        refusal = None
        if pending is not None:
            refusal = {"code": RESTORE_INCOMPLETE, "message": "a restore transaction is incomplete", "detail": {}}
        elif markers:
            refusal = {"code": "maintenance_executor_unresolved",
                       "message": "an executor may still be active or its outcome is unknown",
                       "detail": {"count": len(markers)}}
        return MaintenanceState(record.state, self.root_id, record, refusal, now)

    def active_window(self) -> WindowRecord:
        """The one active window, or a typed refusal."""
        if self._root is None:
            raise MaintenanceRefused(
                _UNCONFIGURED, "no shared maintenance directory is configured"
            )
        witness = self._read_witness()
        if witness is None:
            raise MaintenanceRefused(
                _WINDOW_NOT_ACTIVE, "no maintenance window is open"
            )
        record = WindowRecord.from_mapping(witness)
        if not record.active:
            raise MaintenanceRefused(
                _WINDOW_NOT_ACTIVE,
                "the last maintenance window is closed",
                {"window_id": record.window_id},
            )
        return record

    # ----------------------------------------------------------------------- admission

    @contextmanager
    def admit(self, operation: str) -> Iterator[None]:
        """Hold the shared control lease for one record/host writing operation.

        The witness is checked *after* the lock is acquired. A transition that is opening a
        window holds the exclusive lock at that moment; either this caller got the shared
        lock first (and the transition waits for it), or it observes the new active window
        and is refused. There is no interval in which a writer is admitted against a stale
        read of the fence.
        """
        if self._root is None:
            yield
            return
        try:
            with _locked(
                self._root / CONTROL_LOCK_FILENAME,
                exclusive=False,
                blocking=False,
                timeout_s=self._lock_timeout_s,
                root=self._root,
            ):
                witness = self._read_witness()
                if witness is None:
                    raise MaintenanceRefused(_WITNESS_UNKNOWN, "maintenance witness missing")
                self._require_no_restore()
                if witness is not None:
                    record = WindowRecord.from_mapping(witness)
                    if record.active:
                        raise MaintenanceRefused(
                            _WINDOW_ACTIVE,
                            "a maintenance window is active; control and record writes are refused",
                            {
                                "window_id": record.window_id,
                                "generation": record.generation,
                                "opened_at": record.opened_at,
                                "operation": operation,
                                "service": self._service,
                            },
                        )
                yield
        except MaintenanceRefused:
            raise
        except OSError as exc:
            raise MaintenanceRefused(
                _ADMISSION_UNAVAILABLE,
                "maintenance admission could not be established",
                {"errno": exc.errno, "operation": operation, "service": self._service},
            ) from None

    def service_lifetime(self) -> "_LifetimeLease":
        """Acquire this process's shared runtime lease for its whole lifetime.

        Callers must keep the returned object alive for the process lifetime. The lease is
        released when the object is closed (or garbage-collected), which is what lets a
        snapshot's exclusive runtime acquisition prove that both services have stopped.
        """
        lease = _LifetimeLease(self._root, self._service, self._lock_timeout_s)
        lease.acquire()
        return lease

    @contextmanager
    def startup_lease(self) -> Iterator[MaintenanceState]:
        """Serialize startup store opening with transitions; never migrate on unknown state."""
        if self._root is None:
            yield self.state()
            return
        with _locked(self._root / CONTROL_LOCK_FILENAME, exclusive=False, blocking=False,
                     timeout_s=self._lock_timeout_s, root=self._root):
            state = self.state()
            if state.status == UNKNOWN:
                refusal = state.refusal or {}
                raise MaintenanceRefused(refusal.get("code", _WITNESS_UNKNOWN), "maintenance state is unknown; startup refused")
            if state.refusal and state.refusal.get("code") == RESTORE_INCOMPLETE:
                raise MaintenanceRefused(RESTORE_INCOMPLETE, "a restore transaction is incomplete")
            yield state

    def begin_executor(self, operation: str) -> "ExecutorToken":
        """Persist a blocker before dispatch. The caller settles only on definite completion.

        An abandoned token remains after process death and has no expiry or override.
        Call while holding admission for the entire dispatch boundary.
        """
        if self._root is None:
            return ExecutorToken(None, None)
        with self.admit("executor marker"):
            with _locked(self._root / EXECUTOR_LOCK_FILENAME, exclusive=True, blocking=True,
                         timeout_s=EXECUTOR_BOOKKEEPING_TIMEOUT_S, root=self._root):
                markers = self._executor_markers()
                if len(markers) >= MAX_EXECUTORS:
                    raise MaintenanceRefused("maintenance_executor_capacity", "executor marker capacity reached")
                name = EXECUTOR_PREFIX + secrets.token_hex(16)
                fd = os.open(self._root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                try:
                    _write_all(fd, json.dumps({"operation": operation[:256]}).encode())
                    os.fsync(fd)
                finally:
                    os.close(fd)
                _fsync_directory(self._root)
                return ExecutorToken(self._root, name)

    def _executor_markers(self) -> list[str]:
        assert self._root is not None
        try:
            return [p.name for p in self._root.iterdir() if p.name.startswith(EXECUTOR_PREFIX)]
        except OSError as exc:
            raise MaintenanceRefused(_ADMISSION_UNAVAILABLE, "executor markers unavailable", {"errno": exc.errno}) from None

    def _require_no_executors(self) -> None:
        markers = self._executor_markers()
        if markers:
            raise MaintenanceRefused("maintenance_executor_unresolved", "an executor may still be active or its outcome is unknown", {"count": len(markers)})

    # ---------------------------------------------------------------------- transitions

    def open_window(
        self,
        *,
        release_build_id: str | None = None,
        reason: str | None = None,
        snapshot_id: str | None = None,
    ) -> WindowRecord:
        """Open a new window and return it.

        The exclusive control lock drains every in-flight record/host writer before the
        record is written. The window identifier is freshly random; no earlier snapshot may
        be bound to it by construction.
        """
        if self._root is None:
            raise MaintenanceRefused(
                _UNCONFIGURED, "no shared maintenance directory is configured"
            )
        _validate_optional_text(release_build_id, "release_build_id")
        _validate_optional_text(reason, "reason")
        _validate_optional_text(snapshot_id, "snapshot_id")
        with _transition_lock(self._root, self._lock_timeout_s):
            self._require_no_restore()
            self._require_no_executors()
            current = self._read_witness()
            if current is None:
                raise MaintenanceRefused(_WITNESS_UNKNOWN, "maintenance witness missing")
            if current is not None:
                existing = WindowRecord.from_mapping(current)
                if existing.active:
                    raise MaintenanceRefused(
                        _WINDOW_ACTIVE,
                        "a maintenance window is already active",
                        {"window_id": existing.window_id, "generation": existing.generation},
                    )
                generation = existing.generation + 1
            else:
                generation = 1
            record = WindowRecord(
                window_id=f"w_{secrets.token_hex(16)}",
                generation=generation,
                state=ACTIVE,
                opened_at=_now(),
                release_build_id=release_build_id,
                snapshot_id=snapshot_id,
                reason=reason,
            )
            self._write_witness(record.as_dict())
            return record

    def close_window(
        self,
        window_id: str,
        *,
        accepted: bool,
        outcome: str | None = None,
    ) -> WindowRecord:
        """Close exactly the active window named by ``window_id``.

        A mismatch is refused rather than "closed anyway": closing a window other than the
        one the caller believes it owns would release a fence somebody else is relying on.
        """
        if self._root is None:
            raise MaintenanceRefused(
                _UNCONFIGURED, "no shared maintenance directory is configured"
            )
        _validate_optional_text(window_id, "window_id")
        _validate_optional_text(outcome, "outcome")
        with _transition_lock(self._root, self._lock_timeout_s):
            self._require_no_restore()
            current = self._read_witness()
            if current is None:
                raise MaintenanceRefused(
                    _WINDOW_NOT_ACTIVE, "no maintenance window is open"
                )
            existing = WindowRecord.from_mapping(current)
            if not existing.active:
                raise MaintenanceRefused(
                    _WINDOW_NOT_ACTIVE,
                    "the maintenance window is already closed",
                    {"window_id": existing.window_id},
                )
            if existing.window_id != window_id:
                raise MaintenanceRefused(
                    _WINDOW_MISMATCH,
                    "the active maintenance window is not the one this caller named",
                    {"active_window_id": existing.window_id, "requested_window_id": window_id},
                )
            now = _now()
            closed = WindowRecord(
                window_id=existing.window_id,
                generation=existing.generation,
                state=CLOSED,
                opened_at=existing.opened_at,
                closed_at=now,
                accepted_at=now if accepted else None,
                release_build_id=existing.release_build_id,
                snapshot_id=existing.snapshot_id,
                reason=existing.reason,
                outcome=outcome,
            )
            self._write_witness(closed.as_dict())
            return closed

    def record_snapshot(self, window_id: str, snapshot_id: str) -> WindowRecord:
        """Attach a snapshot identifier to the active window, under the same lock."""
        if self._root is None:
            raise MaintenanceRefused(
                _UNCONFIGURED, "no shared maintenance directory is configured"
            )
        _validate_optional_text(window_id, "window_id")
        _validate_optional_text(snapshot_id, "snapshot_id")
        with _transition_lock(self._root, self._lock_timeout_s):
            self._require_no_restore()
            current = self._read_witness()
            if current is None:
                raise MaintenanceRefused(
                    _WINDOW_NOT_ACTIVE, "no maintenance window is open"
                )
            existing = WindowRecord.from_mapping(current)
            if not existing.active:
                raise MaintenanceRefused(
                    _WINDOW_NOT_ACTIVE,
                    "the maintenance window is already closed",
                    {"window_id": existing.window_id},
                )
            if existing.window_id != window_id:
                raise MaintenanceRefused(
                    _WINDOW_MISMATCH,
                    "the active maintenance window is not the one this caller named",
                    {"active_window_id": existing.window_id, "requested_window_id": window_id},
                )
            updated = WindowRecord(
                **{**existing.as_dict(), "snapshot_id": snapshot_id}
            )
            self._write_witness(updated.as_dict())
            return updated

    @contextmanager
    def snapshot_lease(self, window_id: str) -> Iterator[WindowRecord]:
        """Drain writers, then hold the offline runtime lease for a snapshot.

        The exclusive control lock is acquired first and kept for the whole context, so no
        writer can enter between the drain and the snapshot. The exclusive runtime lock is
        then acquired; it can only succeed once the backend and agent have each released
        their lifetime lease, which is the actual proof that both services are stopped.
        """
        if self._root is None:
            raise MaintenanceRefused(
                _UNCONFIGURED, "no shared maintenance directory is configured"
            )
        _validate_optional_text(window_id, "window_id")
        with _transition_lock(self._root, self._lock_timeout_s):
            self._require_no_restore()
            record = self._require_active(window_id)
            self._require_no_executors()
            with _locked(
                self._root / RUNTIME_LOCK_FILENAME,
                exclusive=True,
                blocking=True,
                timeout_s=self._lock_timeout_s,
                root=self._root,
            ):
                yield record

    @contextmanager
    def restore_lease(self, snapshot: Mapping[str, Any]) -> Iterator[WindowRecord]:
        """Validate a snapshot's window binding and hold the offline lease for restore.

        The snapshot may only be applied to the *same still-active window* that produced it.
        A closed or re-opened window has a different random identifier, so an older snapshot
        is refused before any file or store is touched. The witness itself is never part of
        the snapshot and is never restored.
        """
        binding = SnapshotBinding.from_mapping(snapshot)
        if self._root is None:
            raise MaintenanceRefused(
                _UNCONFIGURED, "no shared maintenance directory is configured"
            )
        with _transition_lock(self._root, self._lock_timeout_s):
            self._require_no_restore()
            record = self._require_active(binding.window_id)
            self._require_no_executors()
            if binding.generation is not None and binding.generation != record.generation:
                raise MaintenanceRefused(
                    _WINDOW_MISMATCH,
                    "the snapshot's window generation does not match the active window",
                    {
                        "active_window_id": record.window_id,
                        "active_generation": record.generation,
                        "snapshot_generation": binding.generation,
                    },
                )
            with _locked(
                self._root / RUNTIME_LOCK_FILENAME,
                exclusive=True,
                blocking=True,
                timeout_s=self._lock_timeout_s,
                root=self._root,
            ):
                yield record

    @contextmanager
    def restore_transaction(self, snapshot: Mapping[str, Any], *, snapshot_id: str) -> Iterator["RestoreTransaction"]:
        """Start or resume one exact offline restore; incomplete work remains fenced.

        The caller must validate and restore the complete matching set before calling
        complete(). The marker is external operational state and must never be restored.
        """
        binding = SnapshotBinding.from_mapping(snapshot)
        if self._root is None:
            raise MaintenanceRefused(_UNCONFIGURED, "no shared maintenance directory is configured")
        if binding.generation is None or binding.generation < 1:
            raise MaintenanceRefused(_WINDOW_BINDING_INVALID, "restore requires a window generation")
        if not isinstance(snapshot_id, str) or not snapshot_id or len(snapshot_id) > 256:
            raise MaintenanceRefused(_WINDOW_BINDING_INVALID, "restore requires a bounded snapshot identifier")
        with _transition_lock(self._root, self._lock_timeout_s):
            record = self._require_active(binding.window_id)
            if binding.generation != record.generation:
                raise MaintenanceRefused(_WINDOW_MISMATCH, "snapshot window generation does not match")
            self._require_no_executors()
            with _locked(self._root / RUNTIME_LOCK_FILENAME, exclusive=True, blocking=True,
                         timeout_s=self._lock_timeout_s, root=self._root):
                pending = self._read_restore()
                if pending is None:
                    pending = {"format_version": 1, "window_id": record.window_id,
                               "generation": record.generation, "snapshot_id": snapshot_id,
                               "transaction_id": "restore_" + secrets.token_hex(16)}
                    self._write_restore(pending)
                elif (pending["window_id"] != record.window_id or
                      pending["generation"] != record.generation or
                      pending["snapshot_id"] != snapshot_id):
                    raise MaintenanceRefused(RESTORE_INCOMPLETE, "a different restore transaction is incomplete")
                transaction = RestoreTransaction(self, record, pending)
                try:
                    yield transaction
                finally:
                    transaction._active = False

    def _require_no_restore(self) -> None:
        if self._read_restore() is not None:
            raise MaintenanceRefused(RESTORE_INCOMPLETE, "a restore transaction is incomplete")

    def _read_restore(self) -> dict[str, Any] | None:
        root = self._require_root()
        try:
            fd = os.open(root / RESTORE_FILENAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError:
            raise MaintenanceRefused(RESTORE_INCOMPLETE, "restore transaction state is unavailable") from None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                    info.st_mode & 0o002 or info.st_size > MAX_WITNESS_BYTES):
                raise MaintenanceRefused(RESTORE_INCOMPLETE, "restore transaction state is unsafe")
            try:
                value = json.loads(_read_bounded(fd, MAX_WITNESS_BYTES).decode("utf-8"))
            except (ValueError, UnicodeDecodeError, MaintenanceRefused, OSError):
                raise MaintenanceRefused(RESTORE_INCOMPLETE, "restore transaction state is malformed") from None
        finally:
            os.close(fd)
        if (not isinstance(value, dict) or set(value) != {"format_version", "window_id", "generation", "snapshot_id", "transaction_id"} or
                type(value.get("format_version")) is not int or value["format_version"] != 1 or
                not isinstance(value.get("window_id"), str) or not value["window_id"] or
                type(value.get("generation")) is not int or value["generation"] < 1 or
                not isinstance(value.get("snapshot_id"), str) or not 0 < len(value["snapshot_id"]) <= 256 or
                not isinstance(value.get("transaction_id"), str) or
                re.fullmatch(r"restore_[0-9a-f]{32}", value["transaction_id"]) is None):
            raise MaintenanceRefused(RESTORE_INCOMPLETE, "restore transaction state is malformed")
        return value

    def _write_restore(self, value: Mapping[str, Any]) -> None:
        root = self._require_root()
        fd, name = tempfile.mkstemp(prefix=".restore.", dir=root)
        try:
            try:
                os.fchmod(fd, 0o640)
                _write_all(fd, json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n")
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(name, root / RESTORE_FILENAME)
            name = ""
            _fsync_directory(root)
        finally:
            if name:
                os.unlink(name)

    # ---------------------------------------------------------------------- internals

    def _require_active(self, window_id: str) -> WindowRecord:
        witness = self._read_witness()
        if witness is None:
            raise MaintenanceRefused(
                _WINDOW_NOT_ACTIVE, "no maintenance window is open"
            )
        record = WindowRecord.from_mapping(witness)
        if not record.active:
            raise MaintenanceRefused(
                _WINDOW_NOT_ACTIVE,
                "the maintenance window is closed; a closed or re-opened window cannot be used",
                {"window_id": record.window_id},
            )
        if record.window_id != window_id:
            raise MaintenanceRefused(
                _WINDOW_MISMATCH,
                "the active maintenance window is not the one this snapshot belongs to",
                {"active_window_id": record.window_id, "snapshot_window_id": window_id},
            )
        return record

    def _read_witness(self) -> dict[str, Any] | None:
        root = self._require_root()
        path = root / WINDOW_FILENAME
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise MaintenanceRefused(
                _WITNESS_UNSAFE, "the maintenance witness is not a single regular file"
            )
        if info.st_nlink != 1:
            raise MaintenanceRefused(
                _WITNESS_UNSAFE, "the maintenance witness has more than one link"
            )
        if info.st_mode & 0o002:
            raise MaintenanceRefused(
                _WITNESS_UNSAFE, "the maintenance witness is writable by other users"
            )
        if info.st_size > MAX_WITNESS_BYTES:
            raise MaintenanceRefused(
                _WITNESS_MALFORMED, "the maintenance witness is larger than its bound"
            )
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            raise MaintenanceRefused(
                _WITNESS_UNKNOWN,
                "the maintenance witness could not be opened",
                {"errno": exc.errno},
            ) from None
        try:
            data = _read_bounded(fd, MAX_WITNESS_BYTES)
        finally:
            os.close(fd)
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise MaintenanceRefused(
                _WITNESS_MALFORMED, "the maintenance witness is not valid JSON"
            ) from None
        if not isinstance(value, dict):
            raise MaintenanceRefused(
                _WITNESS_MALFORMED, "the maintenance witness is not an object"
            )
        _validate_witness(value)
        return value

    def _write_witness(self, record: Mapping[str, Any]) -> None:
        root = self._require_root()
        path = root / WINDOW_FILENAME
        try:
            existing = os.lstat(path)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISREG(existing.st_mode) or stat.S_ISLNK(existing.st_mode):
                raise MaintenanceRefused(
                    _WITNESS_UNSAFE,
                    "the maintenance witness is not a single regular file",
                )
            mode = stat.S_IMODE(existing.st_mode)
        else:
            mode = 0o660
        payload = json.dumps(
            {"format_version": FORMAT_VERSION, **record}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8") + b"\n"
        fd, temporary = tempfile.mkstemp(prefix=".window.", dir=root)
        try:
            try:
                os.fchmod(fd, mode)
                _write_all(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, path)
            temporary = ""
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def _require_root(self) -> Path:
        if self._root is None:
            raise MaintenanceRefused(
                _UNCONFIGURED, "no shared maintenance directory is configured"
            )
        _ensure_root(self._root)
        return self._root


class RestoreTransaction:
    """Offline restore in one still-active window; complete only after full validation."""

    def __init__(self, controller: MaintenanceController, window: WindowRecord, marker: dict[str, Any]) -> None:
        self._controller, self.window, self._marker = controller, window, marker
        self.transaction_id = marker["transaction_id"]
        self._active = True

    def complete(self) -> None:
        if not self._active or self._controller._read_restore() != self._marker:
            raise MaintenanceRefused(RESTORE_INCOMPLETE, "restore transaction no longer matches")
        root = self._controller._require_root()
        os.unlink(root / RESTORE_FILENAME)
        _fsync_directory(root)
        self._active = False


class ExecutorToken:
    """A live dispatch's opaque marker; settle only with operation-specific proof."""

    def __init__(self, root: Path | None, name: str | None) -> None:
        self._root, self._name = root, name

    def settle(self) -> bool:
        if self._root is None or self._name is None:
            return True
        try:
            with _locked(self._root / EXECUTOR_LOCK_FILENAME, exclusive=True, blocking=True,
                     timeout_s=EXECUTOR_BOOKKEEPING_TIMEOUT_S, root=self._root):
                os.unlink(self._root / self._name)
                _fsync_directory(self._root)
                self._name = None
                return True
        except (MaintenanceRefused, OSError):
            # Never turn an already completed provider write into "not written" merely
            # because marker cleanup failed. A retained marker safely blocks maintenance.
            return False


def executor_result_proven(method: str, request: Mapping[str, Any], result: Any) -> bool:
    """Recognize correlated completion/negative evidence from the existing closed adapters.

    Transport exceptions, malformed replies, helper errors and unknown outcomes are not
    evidence. This predicate only releases maintenance markers, not operation authority.
    """
    if not isinstance(result, dict) or not isinstance(request, Mapping):
        return False
    if not isinstance(request.get("attempt_id"), str) or not request["attempt_id"]:
        return False
    if result.get("attempt_id") != request["attempt_id"]:
        return False
    outcome = result.get("outcome")
    if (method == "systemd.service_lifecycle" and outcome == "not_written" and
            result.get("reason") == "lifecycle_receipt_capacity_exhausted" and
            result.get("provider") == "systemd" and request.get("action") in {"start", "stop", "restart"} and
            all(isinstance(request.get(k), str) and bool(request[k]) and result.get(k) == request[k]
                for k in ("unit_id", "action"))):
        return True
    detail = result.get("detail")
    if not isinstance(detail, dict) or "helper_error" in detail:
        return False
    if method == "network.set_interface_mtu":
        for key in ("ifindex", "expected_current_mtu", "desired_mtu"):
            if type(request.get(key)) is not int or type(result.get(key)) is not int or result.get(key) != request[key]:
                return False
        return (result.get("provider") == "linux.link" and result.get("provider_version") == "1" and
                result.get("source") == "rtnetlink" and isinstance(result.get("reason"), str) and
                bool(result["reason"]) and result.get("method") == "netlink_rtm_newlink_mtu" and
                outcome in {"written", "not_written"})
    if method == "docker.container_lifecycle":
        if request.get("action") not in {"start", "stop", "restart"}:
            return False
        if any(not isinstance(request.get(k), str) or not request[k] or result.get(k) != request[k]
               for k in ("container_id", "action")):
            return False
        if (result.get("provider") != "docker" or result.get("method") != "unix_socket_http" or
                not isinstance(result.get("reason"), str) or not result["reason"]):
            return False
        return ((outcome == "written" and result.get("http_status") == 204) or
                (outcome == "not_written" and result.get("http_status") in {304, 400, 404, 409}) or
                (outcome == "not_written" and result.get("http_status") is None and
                 result.get("reason") == "invalid_container_id" and
                 re.match(r"^[0-9a-f]{12,64}$", request["container_id"]) is None))
    if method == "systemd.service_lifecycle":
        if request.get("action") not in {"start", "stop", "restart"}:
            return False
        if any(not isinstance(request.get(k), str) or not request[k] or result.get(k) != request[k]
               for k in ("unit_id", "action")):
            return False
        if not {"job_id", "job_result", "reason"}.issubset(result):
            return False
        if outcome == "written":
            return type(result.get("job_id")) is int and result.get("job_result") == "done"
        # LifecycleDispatchResult's not_written constructor is reserved for proven
        # pre-dispatch failures or an authoritative D-Bus refusal, never a lost reply.
        return (outcome == "not_written" and result.get("job_id") is None and
                result.get("job_result") is None and isinstance(result.get("reason"), str) and
                bool(result["reason"]) and detail.get("manager_scope") == "system" and
                detail.get("transport") == "system_bus_dbus" and
                detail.get("dispatch_interface") == "org.freedesktop.systemd1.Unit")
    return False


def _fsync_directory(root: Path) -> None:
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class _LifetimeLease:
    """A shared ``runtime.lock`` lease held for one service process's lifetime."""

    def __init__(self, root: Path | None, service: str, timeout_s: float) -> None:
        self._root = root
        self._service = service
        self._timeout_s = timeout_s
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._root is None:
            return
        try:
            _ensure_root(self._root)
            fd = _open_lock_file(self._root / RUNTIME_LOCK_FILENAME, self._root)
        except OSError as exc:
            raise MaintenanceRefused(
                _ADMISSION_UNAVAILABLE,
                "the runtime lifetime lock could not be opened",
                {"errno": exc.errno, "service": self._service},
            ) from None
        try:
            _flock(fd, exclusive=False, blocking=False)
        except MaintenanceRefused as exc:
            os.close(fd)
            raise MaintenanceRefused(
                _RUNTIME_HELD,
                "a maintenance snapshot or restore holds the exclusive runtime lease",
                {**exc.detail, "service": self._service},
            ) from None
        self._fd = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def close(self) -> None:
        self.release()

    def __enter__(self) -> "_LifetimeLease":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()

    def __del__(self) -> None:  # pragma: no cover - best-effort process teardown
        self.release()


# --------------------------------------------------------------------------- lock helpers


@contextmanager
def _locked(
    path: Path,
    *,
    exclusive: bool,
    blocking: bool,
    timeout_s: float,
    root: Path,
) -> Iterator[int]:
    _ensure_root(root)
    try:
        fd = _open_lock_file(path, root)
    except OSError as exc:
        raise MaintenanceRefused(
            _ADMISSION_UNAVAILABLE,
            "a maintenance lock file could not be opened",
            {"errno": exc.errno},
        ) from None
    try:
        _flock(fd, exclusive=exclusive, blocking=blocking, timeout_s=timeout_s)
        try:
            yield fd
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def _transition_lock(root: Path, timeout_s: float) -> Iterator[int]:
    with _locked(
        root / CONTROL_LOCK_FILENAME,
        exclusive=True,
        blocking=True,
        timeout_s=timeout_s,
        root=root,
    ) as fd:
        yield fd


def _open_lock_file(path: Path, root: Path) -> int:
    _ensure_root(root)
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path, flags, 0o660)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, "lock file is not a regular file")
        if info.st_nlink != 1:
            raise OSError(errno.EINVAL, "lock file has more than one link")
        if info.st_mode & 0o002:
            raise OSError(errno.EACCES, "lock file is writable by other users")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _flock(
    fd: int,
    *,
    exclusive: bool,
    blocking: bool,
    timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
) -> None:
    operation = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
    deadline = None if not blocking else time.monotonic() + max(0.0, timeout_s)
    while True:
        try:
            fcntl.flock(fd, operation)
            return
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            if not blocking:
                raise MaintenanceRefused(
                    _BUSY, "another operation holds the maintenance lock"
                ) from None
            if deadline is not None and time.monotonic() >= deadline:
                raise MaintenanceRefused(
                    _BUSY,
                    "the maintenance lock was not released within the configured timeout",
                    {"timeout_s": timeout_s},
                ) from None
            time.sleep(0.05)


def _ensure_root(root: Path, *, create: bool = False) -> None:
    try:
        info = os.lstat(root)
    except FileNotFoundError:
        if not create:
            raise MaintenanceRefused(_WITNESS_UNKNOWN, "the shared maintenance directory is missing") from None
        try:
            os.makedirs(root, mode=0o770, exist_ok=True)
        except OSError as exc:
            raise MaintenanceRefused(
                _ADMISSION_UNAVAILABLE,
                "the shared maintenance directory could not be created",
                {"errno": exc.errno},
            ) from None
        info = os.lstat(root)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise MaintenanceRefused(
            _ADMISSION_UNAVAILABLE,
            "the shared maintenance path is not a directory",
        )
    if info.st_mode & 0o002:
        raise MaintenanceRefused(_ADMISSION_UNAVAILABLE, "the shared maintenance directory is world writable")


def _read_bounded(fd: int, limit: int) -> bytes:
    chunks = bytearray()
    while len(chunks) <= limit:
        chunk = os.read(fd, min(65536, limit + 1 - len(chunks)))
        if not chunk:
            break
        chunks.extend(chunk)
    if len(chunks) > limit:
        raise MaintenanceRefused(
            _WITNESS_MALFORMED, "the maintenance witness is larger than its bound"
        )
    return bytes(chunks)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "short write")
        view = view[written:]


# -------------------------------------------------------------------------- validation


def _validate_witness(value: Mapping[str, Any]) -> None:
    unknown = sorted(set(value) - _ALLOWED_WITNESS_KEYS)
    if unknown:
        raise MaintenanceRefused(
            _WITNESS_UNSAFE,
            "the maintenance witness carries unknown fields",
            {"unknown": unknown},
        )
    if value.get("format_version") != FORMAT_VERSION:
        raise MaintenanceRefused(
            _WITNESS_MALFORMED,
            "the maintenance witness format version is not supported",
        )
    window_id = value.get("window_id")
    if not isinstance(window_id, str) or not window_id:
        raise MaintenanceRefused(
            _WITNESS_MALFORMED, "the maintenance witness has no window id"
        )
    generation = value.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise MaintenanceRefused(
            _WITNESS_MALFORMED, "the maintenance witness generation is not a positive integer"
        )
    state = value.get("state")
    if state not in {ACTIVE, CLOSED}:
        raise MaintenanceRefused(
            _WITNESS_MALFORMED, "the maintenance witness state is not active or closed"
        )
    opened_at = value.get("opened_at")
    if not isinstance(opened_at, str):
        raise MaintenanceRefused(
            _WITNESS_MALFORMED, "the maintenance witness has no opening time"
        )
    try:
        datetime.fromisoformat(opened_at)
    except ValueError:
        raise MaintenanceRefused(
            _WITNESS_MALFORMED, "the maintenance witness opening time is not ISO-8601"
        ) from None
    for name in ("closed_at", "accepted_at"):
        moment = value.get(name)
        if moment is not None:
            if not isinstance(moment, str):
                raise MaintenanceRefused(
                    _WITNESS_MALFORMED, f"the maintenance witness {name} is not text"
                )
            try:
                datetime.fromisoformat(moment)
            except ValueError:
                raise MaintenanceRefused(
                    _WITNESS_MALFORMED,
                    f"the maintenance witness {name} is not ISO-8601",
                ) from None
    for name in ("release_build_id", "snapshot_id", "reason", "outcome"):
        _validate_optional_text(value.get(name), name)
    if state == ACTIVE:
        if value.get("closed_at") is not None or value.get("accepted_at") is not None:
            raise MaintenanceRefused(
                _WITNESS_MALFORMED, "an active maintenance window carries a closing time"
            )
    elif value.get("closed_at") is None:
        raise MaintenanceRefused(
            _WITNESS_MALFORMED, "a closed maintenance window has no closing time"
        )


def _validate_optional_text(value: Any, name: str) -> None:
    if value is not None and (not isinstance(value, str) or len(value) > 256):
        raise MaintenanceRefused(
            _WITNESS_MALFORMED, f"{name} is not a bounded string"
        )


# ------------------------------------------------------------------------------- clocks


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
