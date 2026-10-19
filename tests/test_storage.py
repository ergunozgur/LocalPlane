"""Synthetic filesystem evidence only: no production collector or host storage reads."""

import io
import json
import os
import stat
import subprocess
import sys
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from localplane.agent.providers.storage import CollectionFailure, StorageCollector
from localplane.agent.providers.storage_worker import (
    EvidenceError, KernelEvidence, SUPPORTED, collect, counters, parse_mountinfo,
)
from localplane.backend.api.storage_schemas import StorageObservation as Response
from localplane.protocol.storage import MAX_MOUNTS, METADATA_BYTES, OUTPUT_BYTES
from localplane.protocol.wire import MUTATING_METHODS, Method


def line(mid=1, path="/", fs="ext4", root="/", device="8:1"):
    return f"{mid} 0 {device} {root} {path} rw,private=not-for-output - {fs} /dev/not-an-identity rw\n".encode()


class Kernel:
    def __init__(self, data=None):
        self.rows = parse_mountinfo(data or line())
        self.samples = []
        self.stats = SimpleNamespace(f_blocks=100, f_bfree=10, f_bavail=0, f_frsize=4096,
                                     f_files=50, f_ffree=5, f_favail=0)

    def table(self):
        return self.rows

    def namespace(self):
        return "mnt:[42]"

    def sample(self, mount, table):
        self.samples.append(mount)
        return self.stats

    def backing(self, mount, table):
        return "example-block1"


def test_byte_inode_accounting_and_no_secret_source():
    result = collect(Kernel(), lambda: None).as_dict()
    Response.model_validate(result)
    row = result["mounts"][0]
    assert row["bytes"] == dict(total="409600", used="368640", free="40960", available="0", reserved="40960")
    assert row["inodes"]["available"] == "0"
    assert row["backing_name"] == "example-block1"
    assert "not-for-output" not in json.dumps(result)
    assert "not-an-identity" not in json.dumps(result)


@pytest.mark.parametrize("values", [(0, 0, 0, 0), (10, 11, 0, 1), (10, 2, 3, 1), (10, -1, 0, 1), (2**64-1, 0, 0, 1)])
def test_invalid_accounting_unknown(values):
    assert counters(*values) is None


def test_frsize_and_large_exact_integers():
    assert counters(2**60, 20, 10, 4096).total == str(2**72)
    kernel = Kernel()
    kernel.stats.f_files = 0
    assert collect(kernel, lambda: None).mounts[0].inodes is None
    kernel.stats.f_files = 2**64-1
    assert collect(kernel, lambda: None).mounts[0].inodes is None
    kernel.stats.f_files = 2**32-1
    assert collect(kernel, lambda: None).mounts[0].inodes is None


def test_escaped_non_ascii_bind_identity_and_no_aggregation():
    kernel = Kernel(line(1, "/a\\040b") + line(2, "/é", root="/sub"))
    one, two = collect(kernel, lambda: None), collect(kernel, lambda: None)
    assert [m.mountpoint for m in one.mounts] == ["/a b", "/é"]
    assert one.mounts[0].device == one.mounts[1].device
    assert one.observation_id != two.observation_id
    assert "total" not in one.as_dict()


@pytest.mark.parametrize("fs", ["nfs", "fuse.sshfs", "autofs", "tmpfs", "overlay", "btrfs"])
def test_unsupported_never_sampled(fs):
    kernel = Kernel(line(fs=fs))
    result = collect(kernel, lambda: pytest.fail("unsupported work"))
    assert not kernel.samples
    assert result.mounts[0].bytes is None
    assert result.mounts[0].backing_name is None
    assert result.status == "partial"
    assert "multi_device" not in json.dumps(result.as_dict())


@pytest.mark.parametrize("data", [b"bad\n", line()[:-1], line() + line(), line(path="/bad\\999"), b"x" * (METADATA_BYTES + 1), line(fs="bad/type")])
def test_malformed_bounded_metadata(data):
    with pytest.raises((EvidenceError, UnicodeError)):
        parse_mountinfo(data)


def test_mount_limit_visible_and_no_extra_work():
    kernel = Kernel(b"".join(line(i+1, f"/m{i}") for i in range(MAX_MOUNTS+1)))
    result = collect(kernel, lambda: None)
    assert len(kernel.samples) == MAX_MOUNTS
    assert len(result.mounts) == MAX_MOUNTS
    assert "truncated" in result.reason


def test_changed_namespace_or_tuple_invalidates_all(monkeypatch):
    kernel = Kernel()
    def sample(mount, table):
        kernel.rows = [replace(mount, root="/changed")]
        return kernel.stats
    monkeypatch.setattr(kernel, "sample", sample)
    result = collect(kernel, lambda: None)
    assert result.status == "unavailable"
    assert result.mounts[0].bytes is None
    assert result.mounts[0].backing_name is None
    assert result.mounts[0].status == "changed"


def test_denied_mount_is_partial_not_zero(monkeypatch):
    kernel = Kernel()
    def denied(*args):
        raise PermissionError()
    monkeypatch.setattr(kernel, "sample", denied)
    result = collect(kernel, lambda: None)
    assert result.status == "partial"
    assert result.mounts[0].status == "unavailable"
    assert result.mounts[0].bytes is None


def test_descriptor_identity_before_stat_and_stacked_mount(monkeypatch):
    kernel = KernelEvidence()
    table = parse_mountinfo(line(1) + line(2, fs="nfs"))
    monkeypatch.setattr(kernel, "table", lambda: table)
    monkeypatch.setattr(kernel, "mount_id", lambda fd: 2)
    monkeypatch.setattr(os, "fstat", lambda fd: pytest.fail("must not getattr an unsupported mount"))
    with pytest.raises(EvidenceError):
        kernel.validate(7, table, SUPPORTED)
    monkeypatch.setattr(kernel, "mount_id", lambda fd: 1)
    monkeypatch.setattr(os, "fstat", lambda fd: SimpleNamespace(st_mode=stat.S_IFDIR, st_dev=os.makedev(8, 2)))
    with pytest.raises(EvidenceError):
        kernel.validate(7, table, SUPPORTED)
    monkeypatch.setattr(os, "fstat", lambda fd: SimpleNamespace(st_mode=stat.S_IFLNK, st_dev=os.makedev(8, 1)))
    with pytest.raises(EvidenceError):
        kernel.validate(7, table, SUPPORTED)


def test_component_pinning_stops_before_unsupported_ancestor(monkeypatch):
    kernel = KernelEvidence()
    opened, closed = [], []
    def opened_fd(path, flags, **kwargs):
        assert flags & os.O_PATH and flags & os.O_NOFOLLOW
        opened.append(path)
        return len(opened)
    def validate(fd, table, allowed):
        if fd == 2:
            raise EvidenceError()
    monkeypatch.setattr(os, "open", opened_fd)
    monkeypatch.setattr(os, "close", closed.append)
    monkeypatch.setattr(kernel, "validate", validate)
    with pytest.raises(EvidenceError):
        kernel.pin("/remote/never-traverse", [])
    assert opened == ["/", "remote"]
    assert closed == [1, 2]


def test_exact_final_tuple_required_before_statistics(monkeypatch):
    kernel = KernelEvidence()
    table = parse_mountinfo(line(1) + line(2))
    monkeypatch.setattr(kernel, "pin", lambda *args: 77)
    monkeypatch.setattr(kernel, "validate", lambda *args: table[1])
    monkeypatch.setattr(os, "close", lambda fd: None)
    monkeypatch.setattr(os, "fstatvfs", lambda fd: pytest.fail("hidden mount must not be sampled"))
    with pytest.raises(EvidenceError):
        kernel.sample(table[0], table)


class UnkillableChild:
    def __init__(self):
        self.stdout = io.BytesIO()
        self.exited = False
        self.kills = 0
    def poll(self):
        return 0 if self.exited else None
    def kill(self):
        self.kills += 1


def test_unkillable_slot_retained_until_actual_reap(monkeypatch):
    collector = StorageCollector()
    child = UnkillableChild()
    spawned = []
    def spawn():
        spawned.append(child)
        return child
    monkeypatch.setattr(collector, "_spawn", spawn)
    monkeypatch.setattr(collector, "_receive", lambda child: (_ for _ in ()).throw(CollectionFailure()))
    assert collector.observe()["status"] == "unavailable"
    assert child.kills == 1
    for _ in range(30):
        assert collector.observe()["status"] == "busy"
    assert len(spawned) == 1
    child.exited = True
    collector.observe()
    assert len(spawned) == 2
    collector._lock.acquire()
    try:
        assert collector.observe()["status"] == "busy"
    finally:
        collector._lock.release()


@pytest.mark.parametrize("script", [
    "import time;time.sleep(10)",
    "print('malformed')",
    f"print('x' * {OUTPUT_BYTES + 1})",
    "import time;print('{\"event\":\"work\"}',flush=True);time.sleep(10)",
])
def test_real_supervisor_bounded_synthetic_child(monkeypatch, script):
    # These children contain only synthetic output/sleep, never the real collector.
    import localplane.agent.providers.storage as module
    monkeypatch.setattr(module, "TOTAL_SECONDS", 0.2)
    monkeypatch.setattr(module, "WORK_SECONDS", 0.05)
    collector = StorageCollector()
    children = []
    def spawn():
        child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE)
        children.append(child)
        return child
    monkeypatch.setattr(collector, "_spawn", spawn)
    assert collector.observe()["status"] == "unavailable"
    for child in children:
        child.wait(timeout=2)  # test cleanup is finite too


def test_schema_rejects_malformed_identity_and_counter_evidence():
    answer = collect(Kernel(), lambda: None).as_dict()
    for bad in [answer | {"mount_limit": 129}, answer | {"namespace": None},
                answer | {"mounts": answer["mounts"] * 2}]:
        with pytest.raises(ValidationError):
            Response.model_validate(bad)
    answer["mounts"][0]["bytes"]["available"] = "999999"
    with pytest.raises(ValidationError):
        Response.model_validate(answer)


def test_read_method_does_not_expand_mutating_vocabulary():
    assert Method.STORAGE_OBSERVE_FILESYSTEMS not in MUTATING_METHODS
    assert MUTATING_METHODS == {"network.set_interface_mtu", "network.arm_mtu_guard",
                                "docker.container_lifecycle", "systemd.service_lifecycle"}


def test_real_fork_child_uses_only_injected_synthetic_collection(monkeypatch):
    import localplane.agent.providers.storage as module
    monkeypatch.setattr(module, "KernelEvidence", Kernel)
    collector = StorageCollector()
    answer = collector.observe()
    assert answer["status"] == "complete", answer
    assert answer["namespace"] == "mnt:[42]"
    deadline = time.monotonic() + 2
    while collector._child is not None and collector._child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert collector._child is None or collector._child.poll() is not None


def test_fdinfo_mount_id_is_bounded_and_required(monkeypatch):
    import localplane.agent.providers.storage_worker as module
    kernel = KernelEvidence()
    reads = []
    def read(path, limit):
        reads.append((path, limit))
        return b"pos:\t0\nmnt_id:\t42\n"
    monkeypatch.setattr(module, "bounded_read", read)
    assert kernel.mount_id(7) == 42
    assert reads == [("/proc/self/fdinfo/7", 4096)]
    monkeypatch.setattr(module, "bounded_read", lambda *args: b"pos: 0\n")
    with pytest.raises(EvidenceError):
        kernel.mount_id(7)


@pytest.mark.parametrize("number,expected", [(b"8:1\n", "example1"), (b"8:2\n", None)])
def test_backing_joins_kernel_number_not_source(monkeypatch, number, expected):
    kernel = KernelEvidence()
    table = parse_mountinfo(line())
    sysfs = replace(table[0], filesystem="sysfs")
    monkeypatch.setattr(kernel, "pin", lambda *args: 5)
    monkeypatch.setattr(kernel, "validate", lambda *args: sysfs)
    monkeypatch.setattr(kernel, "table", lambda: table)
    calls = []
    def readlink(path, **kwargs):
        calls.append(path)
        return "../../devices/pci/block/example/example1"
    monkeypatch.setattr(os, "readlink", readlink)
    monkeypatch.setattr(os, "open", lambda *args, **kwargs: 6)
    monkeypatch.setattr(os, "close", lambda fd: None)
    monkeypatch.setattr(os, "read", lambda *args: number)
    if expected is None:
        with pytest.raises(EvidenceError):
            kernel.backing(table[0], table)
    else:
        assert kernel.backing(table[0], table) == expected
    assert calls == ["8:1"]


def test_mount_revalidation_after_stats_rejects_change(monkeypatch):
    kernel = KernelEvidence()
    mount = parse_mountinfo(line())[0]
    monkeypatch.setattr(kernel, "pin", lambda *args: 7)
    values = iter([mount, replace(mount, root="/replaced")])
    monkeypatch.setattr(kernel, "validate", lambda *args: next(values))
    monkeypatch.setattr(os, "fstatvfs", lambda fd: Kernel().stats)
    monkeypatch.setattr(os, "close", lambda fd: None)
    with pytest.raises(EvidenceError):
        kernel.sample(mount, [mount])
