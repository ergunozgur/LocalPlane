"""Private libsystemd reader, run only in the disposable journal child.

No journal field enumeration: only the six approved fields are ever requested. Native
data pointers are copied with explicit bounds (the library's threshold is only a hint).
No shell, command line, pager, environment-selected journal or caller path is involved.
"""

from __future__ import annotations

import ctypes as C
import errno
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from localplane.protocol.journal import (
    ID128, MAX_ENTRIES, MESSAGE_BYTES, OUTPUT_BYTES, encode, response, stamp, valid_service,
)


class JournalFailure(Exception):
    def __init__(self, status: str, *, access_omitted: bool = False):
        self.status = status
        self.access_omitted = access_omitted
        super().__init__(status)


def checked(code: int) -> int:
    if code < 0:
        status = {
            errno.EACCES: "denied", errno.EPERM: "denied",
            errno.ENOSYS: "unsupported", errno.EPROTONOSUPPORT: "unsupported",
            errno.EBADMSG: "malformed", errno.EINVAL: "malformed",
        }.get(-code, "unavailable")
        raise JournalFailure(status)
    return code


def boot_id() -> str:
    with open("/proc/sys/kernel/random/boot_id", "r", encoding="ascii") as stream:
        value = stream.read(64).strip().replace("-", "")
    if not ID128.fullmatch(value):
        raise JournalFailure("unavailable")
    return value


@contextmanager
def system_files():
    """Pin permitted local default-namespace journal files, under existing OS access.

    UID-split files can contain system-manager service messages: physical placement is
    not unit identity. Only the later exact trusted field match selects messages.
    sd_journal_open silently skips denied files. Explicit opens make omissions truthful,
    without retrying or bypassing denial. /proc/self/fd paths keep the same readable
    inodes pinned through native open, with no caller-controlled path or file contents.
    Rotation/new files during a read are not a snapshot guarantee.
    """
    with open("/etc/machine-id", "r", encoding="ascii") as stream:
        machine = stream.read(64).strip()
    if not ID128.fullmatch(machine):
        raise JournalFailure("unavailable")
    fds = []
    access_omitted = False
    try:
        for root in ("/run/log/journal", "/var/log/journal"):
            try:
                directory = os.open(f"{root}/{machine}", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            except FileNotFoundError:
                continue
            except PermissionError:
                access_omitted = True
                continue
            try:
                with os.scandir(directory) as items:
                    for index, item in enumerate(items):
                        if index >= 1024:
                            raise JournalFailure("unavailable")
                        if not re.fullmatch(r"(?:system|user-[0-9]+)(?:@[0-9a-f-]+)?\.journal~?", item.name):
                            continue
                        if len(fds) >= 128:
                            raise JournalFailure("unavailable")
                        try:
                            fd = os.open(item.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
                        except PermissionError:
                            access_omitted = True
                            continue
                        fds.append(fd)
                        if not stat.S_ISREG(os.fstat(fd).st_mode):
                            raise JournalFailure("malformed")
            except PermissionError:
                access_omitted = True
            finally:
                os.close(directory)
        if not fds:
            raise JournalFailure("denied" if access_omitted else "unavailable", access_omitted=access_omitted)
        yield JournalFiles([f"/proc/self/fd/{fd}".encode("ascii") for fd in fds], access_omitted)
    except JournalFailure as exc:
        exc.access_omitted |= access_omitted
        raise
    except OSError:
        raise JournalFailure("unavailable", access_omitted=access_omitted) from None
    finally:
        for fd in fds:
            os.close(fd)


@dataclass(frozen=True)
class JournalFiles:
    paths: list[bytes]
    access_omitted: bool = False


class NativeJournal:
    """Small binding to the installed official API; no added Python dependency."""

    def __init__(self, paths: list[bytes]):
        try:
            self.lib = C.CDLL("libsystemd.so.0")
            signatures = {
                "sd_journal_open_files": ([C.POINTER(C.c_void_p), C.POINTER(C.c_char_p), C.c_int], C.c_int),
                "sd_journal_close": ([C.c_void_p], None),
                "sd_journal_add_match": ([C.c_void_p, C.c_char_p, C.c_size_t], C.c_int),
                "sd_journal_set_data_threshold": ([C.c_void_p, C.c_size_t], C.c_int),
                "sd_journal_seek_realtime_usec": ([C.c_void_p, C.c_uint64], C.c_int),
                "sd_journal_previous": ([C.c_void_p], C.c_int),
                "sd_journal_get_realtime_usec": ([C.c_void_p, C.POINTER(C.c_uint64)], C.c_int),
                "sd_journal_get_data": ([C.c_void_p, C.c_char_p, C.POINTER(C.c_void_p), C.POINTER(C.c_size_t)], C.c_int),
            }
            for name, (args, result) in signatures.items():
                function = getattr(self.lib, name)
                function.argtypes, function.restype = args, result
        except (OSError, AttributeError):
            raise JournalFailure("unsupported") from None
        self.handle = C.c_void_p()
        array = (C.c_char_p * (len(paths) + 1))(*paths, None)
        checked(self.lib.sd_journal_open_files(C.byref(self.handle), array, 0))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.lib.sd_journal_close(self.handle)

    def select(self, unit: str, boot: str, until: datetime):
        for match in (f"_SYSTEMD_UNIT={unit}", f"_BOOT_ID={boot}"):
            raw = match.encode("ascii")
            checked(self.lib.sd_journal_add_match(self.handle, raw, len(raw)))
        checked(self.lib.sd_journal_set_data_threshold(self.handle, MESSAGE_BYTES + 64))
        checked(self.lib.sd_journal_seek_realtime_usec(self.handle, int(until.timestamp() * 1_000_000)))

    def previous(self) -> bool:
        return bool(checked(self.lib.sd_journal_previous(self.handle)))

    def timestamp(self) -> datetime:
        value = C.c_uint64()
        checked(self.lib.sd_journal_get_realtime_usec(self.handle, C.byref(value)))
        return datetime.fromtimestamp(value.value / 1_000_000, timezone.utc)

    def field(self, name: bytes, limit: int, *, optional: bool = False) -> tuple[bytes | None, bool]:
        pointer, size = C.c_void_p(), C.c_size_t()
        code = self.lib.sd_journal_get_data(self.handle, name, C.byref(pointer), C.byref(size))
        if optional and code == -errno.ENOENT:
            return None, False
        checked(code)
        prefix = name + b"="
        if not pointer.value or size.value < len(prefix):
            raise JournalFailure("malformed")
        # Never copy a whole oversized native data object into Python.
        raw = C.string_at(pointer, min(size.value, len(prefix) + limit))
        if not raw.startswith(prefix):
            raise JournalFailure("malformed")
        return raw[len(prefix):], size.value > len(prefix) + limit


def collect_entries(journal, unit: str, boot: str, now: datetime, *, access_omitted: bool = False) -> dict:
    answer = response("empty", unit, now=now, coverage="partial" if access_omitted else "complete")
    answer["boot_id"] = boot
    since = datetime.fromisoformat(answer["since"])
    journal.select(unit, boot, now)
    while journal.previous():
        timestamp = journal.timestamp()
        if timestamp < since:
            break
        if timestamp > now:
            raise JournalFailure("malformed")
        actual_unit, unit_cut = journal.field(b"_SYSTEMD_UNIT", 255)
        actual_boot, boot_cut = journal.field(b"_BOOT_ID", 32)
        if unit_cut or boot_cut or actual_unit != unit.encode("ascii") or actual_boot != boot.encode("ascii"):
            raise JournalFailure("identity_changed")
        if len(answer["entries"]) == MAX_ENTRIES:
            answer["truncated"] = True
            break  # Probe scope/time only, not the 101st MESSAGE.
        priority, priority_cut = journal.field(b"PRIORITY", 1, optional=True)
        invocation, invocation_cut = journal.field(b"_SYSTEMD_INVOCATION_ID", 32, optional=True)
        if priority_cut or priority not in (None, *[str(i).encode() for i in range(8)]):
            raise JournalFailure("malformed")
        if invocation_cut or (invocation is not None and not ID128.fullmatch(invocation.decode("ascii"))):
            raise JournalFailure("malformed")
        message, cut = journal.field(b"MESSAGE", MESSAGE_BYTES, optional=True)
        # Binary journal text is displayed as plain replacement-decoded text, still capped
        # in UTF-8 bytes after replacement. No ANSI/HTML interpretation or redaction claim.
        displayed = None
        if message is not None:
            text = message.decode("utf-8", errors="replace").encode("utf-8")
            cut = cut or len(text) > MESSAGE_BYTES
            displayed = text[:MESSAGE_BYTES].decode("utf-8", errors="ignore")
        entry = {"timestamp": stamp(timestamp), "priority": priority.decode() if priority else None,
                 "message": displayed,
                 "boot_id": boot, "invocation_id": invocation.decode() if invocation else None}
        answer["entries"].append(entry)
        answer["status"] = "ok"
        answer["truncated"] |= cut
        if len(encode(answer)) > OUTPUT_BYTES - 1:
            answer["entries"].pop()
            answer["truncated"] = True
            break
    if not answer["entries"] and access_omitted:
        return response("denied", unit, now=now, coverage="partial")
    return answer


def collect(systemd, unit: str) -> dict:
    if not valid_service(unit):
        return response("unsupported")
    access_omitted = False
    try:
        status = systemd.journal_identity(unit)
        if status != "ok":
            return response(status, unit)
        boot = boot_id()
        now = datetime.now(timezone.utc)
        with system_files() as files:
            access_omitted = files.access_omitted
            with NativeJournal(files.paths) as journal:
                answer = collect_entries(journal, unit, boot, now, access_omitted=access_omitted)
        if systemd.journal_identity(unit) != "ok" or boot_id() != boot:
            return response("identity_changed", unit)
        return answer
    except JournalFailure as exc:
        return response(exc.status, unit, coverage="partial" if access_omitted or exc.access_omitted else "unknown")
    except PermissionError:
        return response("denied", unit, coverage="partial" if access_omitted else "unknown")
    except (ValueError, UnicodeError, OverflowError):
        return response("malformed", unit, coverage="partial" if access_omitted else "unknown")
    except Exception:
        return response("unavailable", unit, coverage="partial" if access_omitted else "unknown")
