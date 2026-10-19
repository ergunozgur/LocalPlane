"""Private child collection routines. No caller arguments or alternate production roots.

Only this disposable child resolves filesystem paths or calls fstatvfs. Each path
component is pinned with O_PATH and checked before it becomes a traversal ancestor.
This detects changes; it does not freeze a namespace or eliminate every resolution race.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from typing import Callable

from localplane.protocol.storage import (
    MAX_MOUNTS, METADATA_BYTES, StorageCounters, StorageMount, StorageObservation,
)

SUPPORTED = frozenset({"ext4", "xfs"})
_ESCAPES = {"040": " ", "011": "\t", "012": "\n", "134": "\\"}


class EvidenceError(Exception):
    def __init__(self, reason: str = "Mount evidence was malformed, changed or did not match the pinned descriptor."):
        super().__init__(reason)


@dataclass(frozen=True)
class Mount:
    mount_id: int
    parent_id: int
    device: str
    root: str
    path: str
    filesystem: str
    read_only: bool
    # Kept in memory only for exact change comparison; never returned or logged.
    tuple_evidence: str


def unescape(value: str) -> str:
    if "\\" in re.sub(r"\\(?:040|011|012|134)", "", value):
        raise EvidenceError()
    def replace(match: re.Match) -> str:
        if match[1] not in _ESCAPES:
            raise EvidenceError()
        return _ESCAPES[match[1]]
    result = re.sub(r"\\([0-9]{3})", replace, value)
    if len(result) > 4096 or "\x00" in result:
        raise EvidenceError()
    return result


def parse_mountinfo(data: bytes) -> list[Mount]:
    if len(data) > METADATA_BYTES:
        raise EvidenceError("Mount metadata exceeded the 1 MiB bound; no statistics sampled.")
    if not data.endswith(b"\n"):
        raise EvidenceError("Mount metadata is truncated or incomplete; no statistics sampled.")
    mounts = []
    ids = set()
    for line in data.decode("utf-8", errors="strict").splitlines():
        fields = line.split(" ")
        try:
            sep = fields.index("-")
            if sep < 6 or len(fields) != sep + 4:
                raise EvidenceError()
            mid, parent = int(fields[0]), int(fields[1])
            if not 0 < mid < 2**53 or not 0 <= parent < 2**53 or mid in ids or not re.fullmatch(r"[0-9]{1,10}:[0-9]{1,10}", fields[2]):
                raise EvidenceError()
            path, root = unescape(fields[4]), unescape(fields[3])
            if not path.startswith("/") or not root.startswith("/"):
                raise EvidenceError()
            fs = fields[sep + 1]
            if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", fs):
                raise EvidenceError()
            options = fields[5].split(",")
            if ("ro" in options) == ("rw" in options):
                raise EvidenceError()
            mounts.append(Mount(mid, parent, fields[2], root, path, fs, "ro" in options, line))
            ids.add(mid)
        except (ValueError, IndexError) as exc:
            raise EvidenceError() from exc
    return mounts


def bounded_read(path: str, limit: int) -> bytes:
    with open(path, "rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise EvidenceError("Kernel metadata exceeded its byte bound.")
    return value


class KernelEvidence:
    """Fixed kernel metadata only; injectable as a whole in synthetic tests."""

    def table(self) -> list[Mount]:
        return parse_mountinfo(bounded_read("/proc/self/mountinfo", METADATA_BYTES))

    def namespace(self) -> str:
        value = os.readlink("/proc/self/ns/mnt")
        if not re.fullmatch(r"mnt:\[[0-9]+\]", value):
            raise EvidenceError("Mount namespace identity could not be verified.")
        return value

    def mount_id(self, fd: int) -> int:
        data = bounded_read(f"/proc/self/fdinfo/{fd}", 4096).decode("ascii")
        matches = re.findall(r"^mnt_id:\s*([0-9]+)$", data, re.MULTILINE)
        if len(matches) != 1:
            raise EvidenceError()
        return int(matches[0])

    def validate(self, fd: int, original: list[Mount], allowed: frozenset[str]) -> Mount:
        # Read fdinfo before fstat: do not ask an unrecognised filesystem for getattr.
        mid = self.mount_id(fd)
        current = self.table()
        if current != original:
            raise EvidenceError("Mount table changed during descriptor validation.")
        match = next((m for m in current if m.mount_id == mid), None)
        if match is None:
            raise EvidenceError("Pinned mount ID is absent from the current mount table.")
        if match.filesystem not in allowed:
            raise EvidenceError("Unsupported filesystem ancestor or target; path traversal stopped.")
        info = os.fstat(fd)
        if stat.S_ISLNK(info.st_mode):
            raise EvidenceError("Symlink path component excluded; path traversal stopped.")
        if f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}" != match.device:
            raise EvidenceError("Pinned device does not match the authoritative mount tuple.")
        return match

    def pin(self, path: str, table: list[Mount], allowed: frozenset[str] = SUPPORTED) -> int:
        components = path.split("/")[1:]
        if len(components) > 64 or any(p in {".", ".."} for p in components):
            raise EvidenceError()
        fd = os.open("/", os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            self.validate(fd, table, allowed)
            for component in components:
                if not component:
                    continue
                new = os.open(component, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                os.close(fd)
                fd = new
                self.validate(fd, table, allowed)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def sample(self, mount: Mount, table: list[Mount]) -> os.statvfs_result:
        fd = self.pin(mount.path, table)
        try:
            if self.validate(fd, table, SUPPORTED) != mount:
                raise EvidenceError("Mount is hidden, stacked or changed; pinned tuple does not match.")
            value = os.fstatvfs(fd)
            if self.validate(fd, table, SUPPORTED) != mount:
                raise EvidenceError()
            return value
        finally:
            os.close(fd)

    def backing(self, mount: Mount, table: list[Mount]) -> str | None:
        # A kernel major:minor join, not the mount source string. Only the immediate
        # block device is named: device-mapper/RAID lower topology is not enumerated.
        allowed = SUPPORTED | {"sysfs"}
        fd = self.pin("/sys/dev/block", table, allowed)
        try:
            if self.validate(fd, table, allowed).filesystem != "sysfs":
                raise EvidenceError()
            target = os.readlink(mount.device, dir_fd=fd)
            if not target.startswith("../../devices/"):
                raise EvidenceError()
            parts = target[len("../../devices/"):].split("/")
            if any(not p or p in {".", ".."} for p in parts):
                raise EvidenceError()
            device_fd = self.pin("/sys/devices/" + "/".join(parts), table, allowed)
            try:
                if self.validate(device_fd, table, allowed).filesystem != "sysfs":
                    raise EvidenceError()
                number_fd = os.open("dev", os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=device_fd)
                try:
                    if self.validate(number_fd, table, allowed).filesystem != "sysfs":
                        raise EvidenceError()
                    # Reopen the pinned, validated sysfs attribute, not a mutable pathname.
                    read_fd = os.open(f"/proc/self/fd/{number_fd}", os.O_RDONLY | os.O_CLOEXEC)
                    try:
                        if self.validate(read_fd, table, allowed).filesystem != "sysfs":
                            raise EvidenceError()
                        number = os.read(read_fd, 64).decode("ascii").strip()
                    finally:
                        os.close(read_fd)
                finally:
                    os.close(number_fd)
                if number != mount.device or self.table() != table:
                    raise EvidenceError()
                name = parts[-1]
                if not re.fullmatch(r"[a-zA-Z0-9_.!+-]{1,128}", name):
                    raise EvidenceError()
                return name
            finally:
                os.close(device_fd)
        finally:
            os.close(fd)


def counters(total: int, free: int, available: int, unit: int = 1) -> StorageCounters | None:
    values = (total, free, available, unit)
    if any(type(v) is not int or v < 0 or v >= 2**64 - 1 for v in values):
        return None
    if unit == 0 or not 0 <= available <= free <= total:
        return None
    return StorageCounters(*(str(v * unit) for v in (total, total - free, free, available, free - available)))


def collect(kernel: KernelEvidence, work: Callable[[], None]) -> StorageObservation:
    answer = StorageObservation("partial", "Only ext4/xfs in the agent mount namespace are sampled; no host total.")
    try:
        answer.namespace = kernel.namespace()
        table = kernel.table()
        truncated = len(table) > MAX_MOUNTS
        for mount in table[:MAX_MOUNTS]:
            row = StorageMount(mount.mount_id, mount.parent_id, mount.path, mount.root,
                               mount.filesystem, mount.device, mount.read_only)
            answer.mounts.append(row)
            if mount.filesystem not in SUPPORTED:
                if mount.filesystem == "btrfs":
                    row.backing_reason = "Btrfs may span devices; backing topology was not enumerated."
                continue
            work()
            try:
                sample = kernel.sample(mount, table)
                row.bytes = counters(sample.f_blocks, sample.f_bfree, sample.f_bavail, sample.f_frsize)
                if row.bytes is None:
                    raise EvidenceError()
                inode_fields = (sample.f_files, sample.f_ffree, sample.f_favail)
                row.inodes = counters(*inode_fields) if sample.f_files and (2**32 - 1) not in inode_fields else None
                row.inode_reason = "Observed with fstatvfs." if row.inodes else "Zero, sentinel or inconsistent inode accounting; availability is unknown."
                row.status, row.reason = "observed", "Pinned descriptor matched the current mount tuple before and after fstatvfs."
                try:
                    row.backing_name = kernel.backing(mount, table)
                    row.backing_reason = "Immediate kernel block device via major:minor; lower topology not enumerated."
                except (OSError, ValueError, EvidenceError):
                    pass
            except (OSError, ValueError, EvidenceError) as exc:
                row.status = "unavailable"
                row.reason = (str(exc) if isinstance(exc, EvidenceError) else
                              "Permission denied while reading mount evidence." if isinstance(exc, PermissionError) else
                              "Filesystem evidence could not be read.")
                row.bytes = row.inodes = None
            if kernel.namespace() != answer.namespace or kernel.table() != table:
                raise EvidenceError("Namespace or mount table changed during collection.")
        if truncated:
            answer.reason = "Mount list truncated at 128 rows; coverage is partial. No host total."
        elif all(m.status == "observed" and m.inodes is not None for m in answer.mounts):
            answer.status = "complete"
            answer.reason = "All listed mounts sampled in the agent namespace; backing coverage is separate. No host total."
        if kernel.namespace() != answer.namespace or kernel.table() != table:
            raise EvidenceError("Namespace or mount table changed during collection.")
    except (OSError, ValueError, UnicodeError, EvidenceError) as exc:
        answer.status = "unavailable"
        answer.reason = (str(exc) if isinstance(exc, EvidenceError) else
                         "Permission denied reading mount namespace metadata." if isinstance(exc, PermissionError) else
                         "Mount namespace metadata is malformed or unreadable.")
        for row in answer.mounts:
            row.status, row.reason = "changed", "Observation could not be revalidated."
            row.bytes = row.inodes = None
            row.inode_reason = "Observation could not be revalidated."
            row.backing_name = None
            row.backing_reason = "Backing evidence could not be revalidated."
    return answer
