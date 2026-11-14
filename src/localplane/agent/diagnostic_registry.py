"""Bounded one-use admission tickets for diagnostic request correlation.

Only an issued, retained ticket can begin execution. Unknown tickets are never
completion evidence and cannot initiate work. This stores neither targets nor
results and offers no cancellation or durable history.
"""
from __future__ import annotations

import re
import secrets
import time
from collections import OrderedDict
from threading import Lock

_ID = re.compile(r"[0-9a-f]{32}\Z")


class DiagnosticRegistry:
    def __init__(self, *, clock=time.monotonic):
        self._lock = Lock()
        self._entries: OrderedDict[str, str] = OrderedDict()
        self._bindings: dict[str, tuple[str | None, float]] = {}
        self._clock = clock
        # The process epoch plus an increasing sequence prevents reissuance by
        # this process without storing a lifetime's worth of tombstones.
        self._epoch = secrets.token_hex(8)
        self._sequence = 0

    @staticmethod
    def _valid(request_id: object) -> bool:
        return isinstance(request_id, str) and _ID.fullmatch(request_id) is not None

    def _expire(self):
        for ticket, state in self._entries.items():
            if state == "issued" and self._clock() >= self._bindings[ticket][1]:
                self._entries[ticket] = "expired"

    def issue(self, *, binding: str | None = None, deadline: float | None = None) -> str | None:
        with self._lock:
            self._expire()
            if any(state in ("issued", "running") for state in self._entries.values()):
                return None
            if self._sequence >= 1 << 64:
                return None
            while len(self._entries) >= 32:
                removed, _ = self._entries.popitem(last=False)
                del self._bindings[removed]
            ticket = f"{self._epoch}{self._sequence:016x}"
            self._sequence += 1
            self._entries[ticket] = "issued"
            self._bindings[ticket] = (binding, min(self._clock() + 5, deadline) if deadline is not None else self._clock() + 5)
            return ticket

    def begin(self, request_id: str, *, binding: str | None = None) -> bool:
        if not self._valid(request_id):
            return False
        with self._lock:
            self._expire()
            if self._entries.get(request_id) != "issued" or self._bindings[request_id][0] != binding:
                return False
            self._entries[request_id] = "running"
            return True

    def complete(self, request_id: str, proof: object) -> None:
        with self._lock:
            if proof is True and self._entries.get(request_id) == "running":
                self._entries[request_id] = "completed"

    def status(self, request_id: str) -> str:
        if not self._valid(request_id):
            return "unknown"
        with self._lock:
            self._expire()
            state = self._entries.get(request_id)
            return "pending" if state in ("issued", "running") else state or "unknown"
