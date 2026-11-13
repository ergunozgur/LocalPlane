"""Fail-closed, bounded kernel address inventory for diagnostic admission.

This is not a management endpoint discovery mechanism. The policy declaration
provides that separate authority. Two equal inventories reduce, but cannot make
atomic, the interval between observing addresses and a subsequent connection.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from localplane.agent.providers import connectivity_iproute

MAX_INTERFACES = 4096
MAX_ADDRESSES = 4096
_NAMESPACE = re.compile(r"net:\[([1-9][0-9]*)\]\Z")


@dataclass(frozen=True)
class KernelExclusions:
    boot_id: str
    network_namespace: str
    local_addresses: tuple[str, ...]
    broadcast_addresses: tuple[str, ...]


def kernel_context() -> tuple[str, str]:
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    namespace = os.readlink("/proc/self/ns/net")
    if str(uuid.UUID(boot)) != boot or not _NAMESPACE.fullmatch(namespace):
        raise ValueError("invalid kernel context")
    return boot, namespace


def clock_namespace() -> str:
    namespace = os.readlink("/proc/self/ns/time")
    if not re.fullmatch(r"time:\[[1-9][0-9]*\]", namespace):
        raise ValueError("unsupported clock namespace")
    return namespace


def _address(value: object, version: int):
    if not isinstance(value, str) or len(value) > 64 or "%" in value:
        raise ValueError("invalid address")
    address = ipaddress.ip_address(value)
    if address.version != version:
        raise ValueError("address family mismatch")
    return address


def _parse(data: bytes):
    if not isinstance(data, bytes) or len(data) > connectivity_iproute.MAX_BYTES:
        raise ValueError("address inventory size")
    rows = json.loads(data, object_pairs_hook=connectivity_iproute._object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid constant")))
    if not isinstance(rows, list) or not rows or len(rows) > MAX_INTERFACES:
        raise ValueError("invalid interface inventory")
    local, broadcast, indices, names = set(), set(), set(), set()
    count = 0
    interfaces = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid interface")
        index, name, flags, addresses = (row.get(k) for k in ("ifindex", "ifname", "flags", "addr_info"))
        if (type(index) is not int or not 0 < index <= 0xffffffff or index in indices
                or not isinstance(name, str) or not name or len(name) > 255 or "\x00" in name or name in names
                or not isinstance(flags, list) or len(flags) > 64
                or any(not isinstance(flag, str) or not flag or len(flag) > 64 for flag in flags)
                or len(set(flags)) != len(flags) or not isinstance(addresses, list)):
            raise ValueError("invalid interface identity or flags")
        indices.add(index)
        names.add(name)
        count += len(addresses)
        if count > MAX_ADDRESSES:
            raise ValueError("too many addresses")
        entries = []
        for item in addresses:
            if not isinstance(item, dict) or item.get("family") not in ("inet", "inet6"):
                raise ValueError("unknown address family")
            version = 4 if item["family"] == "inet" else 6
            address = _address(item.get("local"), version)
            prefix = item.get("prefixlen")
            if type(prefix) is not int or not 0 <= prefix <= address.max_prefixlen:
                raise ValueError("invalid prefix")
            local.add(str(address))
            reported = item.get("broadcast")
            if version == 6:
                if reported is not None:
                    raise ValueError("IPv6 broadcast")
            else:
                if reported is not None:
                    broadcast.add(str(_address(reported, 4)))
                elif "BROADCAST" in flags and "POINTOPOINT" not in flags and prefix <= 30:
                    raise ValueError("missing broadcast evidence")
                if prefix <= 30 and "BROADCAST" in flags:
                    broadcast.add(str(ipaddress.ip_network((address, prefix), strict=False).broadcast_address))
            entries.append((version, str(address), prefix, str(_address(reported, 4)) if reported is not None else ""))
        interfaces.append((index, name, tuple(sorted(flags)), tuple(sorted(entries))))
    return tuple(sorted(local)), tuple(sorted(broadcast)), tuple(sorted(interfaces))


def parse_addresses(data: bytes) -> tuple[tuple[str, ...], tuple[str, ...]]:
    local, broadcast, _ = _parse(data)
    return local, broadcast


def collect(*, runner: Callable = connectivity_iproute._collect,
            context_reader: Callable = kernel_context, deadline: float | None = None,
            clock: Callable = time.monotonic) -> KernelExclusions:
    """Require stable context and two matching complete fixed-command snapshots."""
    start = clock()
    deadline = min(deadline, start + 5.0) if deadline is not None else start + 5.0
    def check():
        if clock() >= deadline:
            raise TimeoutError("diagnostic inventory deadline elapsed")
    check()
    before = context_reader()
    check()
    first_data = runner(connectivity_iproute.ADDRESS_COMMAND, deadline=deadline)
    check()
    first = _parse(first_data)
    check()
    middle = context_reader()
    check()
    second_data = runner(connectivity_iproute.ADDRESS_COMMAND, deadline=deadline)
    check()
    second = _parse(second_data)
    check()
    after = context_reader()
    check()
    if before != middle or middle != after or first != second:
        raise ValueError("kernel inventory changed")
    boot, namespace = before
    if not isinstance(boot, str) or str(uuid.UUID(boot)) != boot or not isinstance(namespace, str) or not _NAMESPACE.fullmatch(namespace):
        raise ValueError("invalid kernel context")
    return KernelExclusions(boot, namespace, second[0], second[1])
