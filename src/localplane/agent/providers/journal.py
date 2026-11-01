"""Memory-only, process-wide single-slot journal collection with bounded parent wait.

Like filesystem collection, a timeout is not proof of kernel-I/O cancellation. Keep
the exact child until waitpid actually reaps it, including after SIGKILL. No waiter
threads, replacement loop, inherited serving descriptors, stderr capture or disk spool.
"""

from __future__ import annotations

import json
import os
import resource
import selectors
import signal
import threading
import time

from localplane.agent.providers.journal_native import collect
from localplane.protocol.journal import OUTPUT_BYTES, TOTAL_SECONDS, encode, response, validate, valid_service


class JournalChild:
    def __init__(self, systemd, unit: str):
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
                os.closerange(writer + 1, 2**31 - 1)
                null = os.open("/dev/null", os.O_RDWR)
                for fd in (0, 1, 2):
                    os.dup2(null, fd)
                if null > 2:
                    os.close(null)
                # Native faults must not create a core containing MESSAGE text.
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
                answer = encode(collect(systemd, unit))
                if len(answer) > OUTPUT_BYTES:
                    answer = encode(response("malformed", unit))
                view = memoryview(answer)
                while view:
                    view = view[os.write(writer, view):]
            except BaseException:
                pass  # Never print arbitrary errors or native journal payloads.
            finally:
                os._exit(0)
        os.close(writer)
        self.pid, self.returncode = pid, None

    def poll(self):
        if self.returncode is None:
            try:
                pid, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                self.returncode = 0
            else:
                if pid:
                    self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def kill(self):
        os.kill(self.pid, signal.SIGKILL)


class JournalCollector:
    def __init__(self):
        self._lock = threading.Lock()
        self._child = None

    def read(self, systemd, unit: str) -> dict:
        if not valid_service(unit):
            return response("unsupported")
        deadline = time.monotonic() + TOTAL_SECONDS
        if not self._lock.acquire(blocking=False):
            return response("busy", unit)
        try:
            if self._child is not None:
                if self._child.poll() is None:
                    return response("busy", unit)
                self._child = None
            try:
                self._child = self._spawn(systemd, unit)
                return validate(self._receive(self._child, deadline), unit)
            except TimeoutError:
                return response("timeout", unit)
            except (ValueError, TypeError, KeyError, UnicodeError):
                return response("malformed", unit)
            except Exception:
                return response("unavailable", unit)
            finally:
                if self._child is not None:
                    if self._child.poll() is None:
                        try:
                            self._child.kill()
                        except OSError:
                            pass
                    self._child.stdout.close()
                    if self._child.poll() is not None:
                        self._child = None
        finally:
            self._lock.release()

    @staticmethod
    def _spawn(systemd, unit):
        return JournalChild(systemd, unit)

    @staticmethod
    def _receive(child, deadline):
        os.set_blocking(child.stdout.fileno(), False)
        pending = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError()
                # One extra byte detects an overrun, but never accumulates >256 KiB.
                chunk = os.read(child.stdout.fileno(), min(8192, OUTPUT_BYTES + 1 - len(pending)))
                if not chunk:
                    return json.loads(pending)
                if len(pending) + len(chunk) > OUTPUT_BYTES:
                    raise ValueError()
                pending.extend(chunk)


COLLECTOR = JournalCollector()
