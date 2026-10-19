"""On-demand, single-flight filesystem collection outside the serving process.

SIGKILL is a request, not a kernel-I/O cancellation guarantee. The process-wide slot
is retained until poll() actually observes/reaps exit, even after a request timed out.
No replacement child, waiter thread, permanent worker or unbounded wait is created.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import threading
import time
from typing import Any

from localplane.protocol.storage import (
    OUTPUT_BYTES, TOTAL_SECONDS, WORK_SECONDS, StorageObservation,
)
from localplane.agent.providers.storage_worker import collect, KernelEvidence


class CollectionFailure(Exception):
    def __init__(self, reason: str = "Collector returned unreadable or incomplete evidence."):
        super().__init__(reason)


class CollectionChild:
    """Fork without an exec-error-pipe handshake that could block the parent.

    Only fixed, already-imported collection code runs in the child. Inherited locks may
    prevent a child from progressing; the same deadline/retained-slot rule handles that.
    No inherited serving sockets or other descriptors are kept in the child.
    """

    def __init__(self) -> None:
        reader, writer = os.pipe2(os.O_CLOEXEC)
        try:
            self.stdout = os.fdopen(reader, "rb", buffering=0)
        except BaseException:
            os.close(reader)
            os.close(writer)
            raise
        try:
            pid = os.fork()
        except BaseException:
            self.stdout.close()
            os.close(writer)
            raise
        if pid == 0:
            try:
                os.closerange(0, writer)
                # Cover descriptors above a subsequently lowered RLIMIT_NOFILE too.
                # On kernels without efficient close-range support this may consume the
                # child's deadline, but never adds a parent wait or replacement child.
                os.closerange(writer + 1, 2**31 - 1)
                def work() -> None:
                    os.write(writer, b'{"event":"work"}\n')
                answer = collect(KernelEvidence(), work).as_dict()
                encoded = json.dumps({"result": answer}, ensure_ascii=True).encode() + b"\n"
                if len(encoded) + 128 * 32 > OUTPUT_BYTES:
                    encoded = json.dumps({"result": StorageObservation("unavailable", "Response exceeded its byte limit.").as_dict()}).encode() + b"\n"
                view = memoryview(encoded)
                while view:
                    written = os.write(writer, view)
                    view = view[written:]
            except BaseException:
                pass  # EOF without an answer is unavailable; never leak exception paths.
            finally:
                os._exit(0)
        os.close(writer)
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is None:
            try:
                pid, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                # An external SIGCHLD reaper already consumed this exact child.
                self.returncode = 0
                return self.returncode
            if pid:
                self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def kill(self) -> None:
        os.kill(self.pid, signal.SIGKILL)


class StorageCollector:
    """One instance is shared by all production services in this process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._child: Any = None

    def observe(self) -> dict:
        if not self._lock.acquire(blocking=False):
            return StorageObservation("busy", "A filesystem collection is already running.").as_dict()
        try:
            if self._child is not None:
                if self._child.poll() is None:
                    return StorageObservation(
                        "busy", "Previous collector has not exited; no replacement was started."
                    ).as_dict()
                self._child = None
            try:
                self._child = self._spawn()
                return self._receive(self._child)
            except CollectionFailure as exc:
                return StorageObservation("unavailable", str(exc)).as_dict()
            except ValueError:
                return StorageObservation("unavailable", "Collector returned malformed evidence.").as_dict()
            except OSError:
                return StorageObservation("unavailable", "Collector could not be started or read.").as_dict()
            finally:
                if self._child is not None:
                    if self._child.poll() is None:
                        try:
                            self._child.kill()
                        except OSError:
                            pass
                    if self._child.stdout is not None:
                        self._child.stdout.close()
                    # Never clear the slot merely because kill() succeeded.
                    if self._child.poll() is not None:
                        self._child = None
        finally:
            self._lock.release()

    @staticmethod
    def _spawn() -> CollectionChild:
        return CollectionChild()

    @staticmethod
    def _receive(child: Any) -> dict:
        assert child.stdout is not None
        os.set_blocking(child.stdout.fileno(), False)
        total_deadline = time.monotonic() + TOTAL_SECONDS
        work_deadline = total_deadline
        pending = bytearray()
        received = 0
        answer = None
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while True:
                remaining = min(total_deadline, work_deadline) - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise CollectionFailure("Collection timed out; the slot is retained until actual child exit/reap.")
                chunk = os.read(child.stdout.fileno(), 8192)
                if not chunk:
                    if answer is None or pending:
                        raise CollectionFailure()
                    return answer
                received += len(chunk)
                if received > OUTPUT_BYTES:
                    raise CollectionFailure("Collector output exceeded the 512 KiB response limit.")
                pending.extend(chunk)
                while b"\n" in pending:
                    line, _, rest = pending.partition(b"\n")
                    pending = bytearray(rest)
                    message = json.loads(line)
                    if message == {"event": "work"} and answer is None:
                        work_deadline = time.monotonic() + WORK_SECONDS
                    elif (isinstance(message, dict) and set(message) == {"result"}
                          and isinstance(message["result"], dict) and answer is None):
                        answer = message["result"]
                        work_deadline = total_deadline
                    else:
                        raise CollectionFailure()


COLLECTOR = StorageCollector()
