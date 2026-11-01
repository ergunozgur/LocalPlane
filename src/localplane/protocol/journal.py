"""Closed, ephemeral service-journal vocabulary. No query or persistence contract."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Literal

MAX_ENTRIES = 100
OUTPUT_BYTES = 256 * 1024
MESSAGE_BYTES = 4096
TOTAL_SECONDS = 3.0
WINDOW_SECONDS = 15 * 60
JournalStatus = Literal[
    "ok", "empty", "denied", "unavailable", "unsupported", "timeout", "busy",
    "identity_changed", "malformed",
]
STATUSES = frozenset(JournalStatus.__args__)
ID128 = re.compile(r"[0-9a-f]{32}")
# Literal systemd escaped names only; no aliases, paths, globs, assignments or options.
SERVICE = re.compile(r"(?:[A-Za-z0-9_:.@-]|\\x[0-9a-fA-F]{2})+\.service")
SCOPE_GAPS = (
    "Exact trusted unit field only; some manager messages are omitted. Default system journal namespace only.",
    "Correlation is not causality or proof of a frozen service invocation.",
    "Coverage describes the bounded file selection, not all history or a frozen snapshot.",
)
ACCESS_GAP = "Some default-namespace journal files or directories are inaccessible to the agent's existing OS identity; coverage is partial."
REASONS = {
    "denied": "The agent's existing OS identity cannot read this evidence.",
    "unavailable": "Journal evidence is unavailable; no complete read was established.",
    "unsupported": "The required system journal mechanism is unsupported.",
    "timeout": "The three-second collection deadline expired; cleanup retains the slot until exit.",
    "busy": "A previous collection has not completed or been reaped; no replacement was started.",
    "identity_changed": "Canonical unit, host or boot identity could not be confirmed unchanged.",
    "malformed": "Journal evidence did not satisfy the closed response contract.",
}


def valid_service(value: object) -> bool:
    # The system-manager user@ instance aggregates user-manager journal traffic under
    # its trusted unit field. It is not an eligible per-service diagnosis target.
    return (isinstance(value, str) and not value.startswith("user@")
            and len(value.encode("utf-8")) <= 255 and bool(SERVICE.fullmatch(value)))


def stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def response(status: str, canonical_id: str | None = None, *, now: datetime | None = None,
             coverage: str = "unknown") -> dict:
    now = now or datetime.now(timezone.utc)
    return {
        "canonical_id": canonical_id, "status": status, "retrieved_at": stamp(now),
        "since": stamp(now - timedelta(seconds=WINDOW_SECONDS)), "boot_id": None,
        "truncated": False, "entries": [], "coverage": coverage,
        "gaps": [*SCOPE_GAPS, *([ACCESS_GAP] if coverage == "partial" else []),
                 *([REASONS[status]] if status in REASONS else [])],
    }


def encode(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def validate(value: object, canonical_id: str) -> dict:
    """Validate the child/agent boundary without ever quoting rejected data."""
    if not isinstance(value, dict) or set(value) != set(response("empty")):
        raise ValueError()
    if len(encode(value)) > OUTPUT_BYTES or value["status"] not in STATUSES:
        raise ValueError()
    if value["canonical_id"] not in (None, canonical_id):
        raise ValueError()
    if type(value["truncated"]) is not bool or not isinstance(value["entries"], list):
        raise ValueError()
    if len(value["entries"]) > MAX_ENTRIES:
        raise ValueError()
    if value["coverage"] not in {"complete", "partial", "unknown"}:
        raise ValueError()
    allowed_gaps = {*SCOPE_GAPS, ACCESS_GAP, *REASONS.values()}
    if not isinstance(value["gaps"], list) or any(g not in allowed_gaps for g in value["gaps"]):
        raise ValueError()
    if (value["coverage"] == "partial") != (ACCESS_GAP in value["gaps"]):
        raise ValueError()
    retrieved = datetime.fromisoformat(value["retrieved_at"])
    since = datetime.fromisoformat(value["since"])
    if retrieved.tzinfo is None or since.tzinfo is None or retrieved - since != timedelta(seconds=WINDOW_SECONDS):
        raise ValueError()
    boot = value["boot_id"]
    if boot is not None and (not isinstance(boot, str) or not ID128.fullmatch(boot)):
        raise ValueError()
    if value["status"] in {"ok", "empty"}:
        if value["coverage"] == "unknown" or (value["status"] == "empty" and value["coverage"] != "complete"):
            raise ValueError()
        if boot is None or value["canonical_id"] != canonical_id:
            raise ValueError()
        if (value["status"] == "ok") != bool(value["entries"]):
            raise ValueError()
    elif value["entries"] or value["truncated"]:
        raise ValueError()
    elif value["coverage"] == "complete":
        raise ValueError()
    for entry in value["entries"]:
        if not isinstance(entry, dict) or set(entry) != {"timestamp", "priority", "message", "boot_id", "invocation_id"}:
            raise ValueError()
        if entry["boot_id"] != boot or entry["priority"] not in (None, *map(str, range(8))):
            raise ValueError()
        invocation = entry["invocation_id"]
        if invocation is not None and (not isinstance(invocation, str) or not ID128.fullmatch(invocation)):
            raise ValueError()
        message = entry["message"]
        if message is not None and (not isinstance(message, str) or len(message.encode("utf-8")) > MESSAGE_BYTES):
            raise ValueError()
        # Native entries have authoritative timestamps, even though the public optional
        # shape leaves room for unknown evidence in future supported mechanisms.
        timestamp = datetime.fromisoformat(entry["timestamp"])
        if timestamp.tzinfo is None or not since <= timestamp <= retrieved:
            raise ValueError()
    return value
