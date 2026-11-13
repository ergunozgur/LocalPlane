"""Bounded fixed iproute2 JSON inventories; never an arbitrary command interface."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from typing import Callable

MAX_BYTES = 1024 * 1024
MAX_ITEMS = 4096
TIMEOUT = 3.0
_COMMAND_SLOT = threading.Lock()
COMMANDS = {
    "routes4": ("ip", "-j", "-4", "route", "show", "table", "all"),
    "routes6": ("ip", "-j", "-6", "route", "show", "table", "all"),
    "rules4": ("ip", "-j", "-4", "rule", "show"),
    "rules6": ("ip", "-j", "-6", "rule", "show"),
}
ADDRESS_COMMAND = ("ip", "-j", "address", "show")


def _collect(argv: tuple[str, ...], *, deadline: float | None = None) -> bytes:
    """One fixed command, with bounded output and child cleanup."""
    if argv not in (*COMMANDS.values(), ADDRESS_COMMAND):
        raise ValueError("not a fixed inventory command")
    if deadline is not None and deadline <= time.monotonic():
        raise TimeoutError("iproute deadline elapsed")
    if not _COMMAND_SLOT.acquire(blocking=False):
        raise ValueError("iproute reader busy")
    try:
        read_fd, write_fd = os.pipe()
    except BaseException:
        _COMMAND_SLOT.release()
        raise
    chunks = bytearray()
    exceeded = threading.Event()
    command_finished = threading.Event()

    def read_output() -> None:
        try:
            with os.fdopen(read_fd, "rb", buffering=0) as stream:
                while True:
                    data = stream.read(min(65536, MAX_BYTES + 1 - len(chunks)))
                    if not data:
                        return
                    chunks.extend(data)
                    if len(chunks) > MAX_BYTES:
                        exceeded.set()
                        return
        finally:
            command_finished.wait()
            _COMMAND_SLOT.release()

    reader = threading.Thread(target=read_output, daemon=True)
    try:
        reader.start()
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        _COMMAND_SLOT.release()
        raise
    try:
        with os.fdopen(write_fd, "wb", buffering=0) as output:
            remaining = TIMEOUT if deadline is None else min(TIMEOUT, deadline - time.monotonic())
            if remaining <= 0:
                raise TimeoutError("iproute deadline elapsed")
            result = subprocess.run(argv, stdout=output, stderr=subprocess.DEVNULL,
                                    stdin=subprocess.DEVNULL, shell=False, timeout=remaining,
                                    env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "LANG": "C"},
                                    check=False)
    finally:
        command_finished.set()
        reader.join(timeout=1 if deadline is None else max(0, min(1, deadline - time.monotonic())))
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("iproute deadline elapsed")
    if reader.is_alive() or exceeded.is_set() or result.returncode != 0:
        raise ValueError("iproute output unavailable or exceeded limit")
    return bytes(chunks)


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and len(value) <= 256 and "\x00" not in value:
        return value
    if type(value) is int and 0 <= value <= 0xffffffff:
        return str(value)
    raise ValueError("invalid iproute scalar")


def _number(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is int and 0 <= value <= 0xffffffff:
        return value
    raise ValueError("invalid iproute number")


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate iproute field")
        result[key] = value
    return result


def parse_inventory(data: bytes, family: str, kind: str) -> list[dict]:
    if family not in ("ipv4", "ipv6") or kind not in ("routes", "rules") or len(data) > MAX_BYTES:
        raise ValueError("invalid inventory")
    value = json.loads(data, object_pairs_hook=_object)
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise ValueError("invalid inventory")
    records = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("invalid inventory record")
        if kind == "rules":
            records.append({"family": family, "priority": _number(item.get("priority")),
                            "source": _text(item.get("src")), "destination": _text(item.get("dst")),
                            "table": _text(item.get("table")), "action": _text(item.get("action")),
                            "fwmark": _text(item.get("fwmark")), "fwmask": _text(item.get("fwmask")),
                            "incoming_interface": _text(item.get("iif")), "outgoing_interface": _text(item.get("oif")),
                            "uid_range": _text(item.get("uidrange")), "suppress_prefix_length": _number(item.get("suppress_prefixlength"))})
            continue
        hops = item.get("nexthops")
        if hops is not None and (not isinstance(hops, list) or len(hops) > 64):
            raise ValueError("invalid multipath")
        if hops is None and any(key in item for key in ("gateway", "dev")):
            hops = [item]
        result_hops = None
        if hops is not None:
            result_hops = []
            for hop in hops:
                if not isinstance(hop, dict):
                    raise ValueError("invalid nexthop")
                result_hops.append({"gateway": _text(hop.get("gateway")), "interface": _text(hop.get("dev")),
                                    "weight": _number(hop.get("weight")), "ifindex": None, "object_id": None})
        destination = _text(item.get("dst"))
        if destination is None:
            raise ValueError("route destination missing")
        records.append({"family": family, "destination": destination,
                        "table": _text(item.get("table")), "route_type": _text(item.get("type")),
                        "scope": _text(item.get("scope")), "protocol": _text(item.get("protocol")),
                        "metric": _number(item.get("metric")), "nexthops": result_hops})
    return records


def observe(runner: Callable[[tuple[str, ...]], bytes] = _collect) -> dict:
    """Independent family failures remain gaps, never an empty successful list."""
    sections = {}
    for kind in ("routes", "rules"):
        records, gaps, successes = [], [], 0
        for suffix, family in (("4", "ipv4"), ("6", "ipv6")):
            try:
                data = runner(COMMANDS[kind + suffix])
                records.extend(parse_inventory(data, family, kind))
                successes += 1
                allowed = ({"dst", "table", "type", "scope", "protocol", "metric", "nexthops", "gateway", "dev"}
                           if kind == "routes" else {"priority", "src", "dst", "table", "action", "fwmark", "fwmask", "iif", "oif", "uidrange", "suppress_prefixlength"})
                raw_items = json.loads(data, object_pairs_hook=_object)
                if any(set(item) - allowed or any(set(hop) - {"gateway", "dev", "weight"} for hop in (item.get("nexthops") or [])) for item in raw_items):
                    gaps.append(f"{family} {kind} include fields not represented by this reader")
            except (OSError, ValueError, UnicodeError, TimeoutError, subprocess.TimeoutExpired, RecursionError):
                gaps.append(f"{family} {kind} inventory unavailable or malformed")
        sections[kind] = {"value": records if successes else None,
                          "completeness": "complete" if not gaps else "partial" if successes else "unknown",
                          "gaps": gaps}
    return sections
