"""Closed passive collector, with a kernel identity guard around independent sources."""
from __future__ import annotations

import ipaddress
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from localplane.agent.providers import connectivity_iproute, connectivity_nm, connectivity_resolved

MAX_INTERFACES = 4096
MAX_ADDRESSES = 4096
TOTAL_SECONDS = 32.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def kernel_identity(ifindex: int, sysfs: Path = Path("/sys/class/net")) -> dict:
    if type(ifindex) is not int or ifindex <= 0:
        raise ValueError("invalid interface index")
    matches = []
    with os.scandir(sysfs) as entries:
        for count, entry in enumerate(entries, 1):
            if count > MAX_INTERFACES:
                raise ValueError("interface inventory too large")
            if not entry.is_dir():
                continue
            try:
                if int((Path(entry.path) / "ifindex").read_text().strip()) == ifindex:
                    matches.append(entry)
            except (OSError, ValueError) as exc:
                raise ValueError("interface identity unreadable") from exc
    if len(matches) != 1:
        raise ValueError("interface identity missing or ambiguous")
    selected = matches[0]
    stat = os.stat(selected.path)
    base = Path(selected.path)
    try:
        address = (base / "address").read_text().strip().lower()
        assign_type = int((base / "addr_assign_type").read_text().strip())
    except (OSError, ValueError):
        address, assign_type = None, None
    def link_name(path: Path) -> str | None:
        try:
            return os.path.basename(os.readlink(path)) or None
        except OSError:
            return None
    device = link_name(base / "device")
    subsystem = link_name(base / "device" / "subsystem") if device else None
    device_path = f"{subsystem}/{device}" if subsystem else device
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    netns = os.readlink("/proc/self/ns/net")
    if str(uuid.UUID(boot)) != boot.lower() or not netns.startswith("net:[") or not netns.endswith("]"):
        raise ValueError("host context unavailable")
    return {"ifindex": ifindex, "name": selected.name, "mac_address": address,
            "mac_is_permanent": None if assign_type is None else assign_type == 0,
            "device_path": device_path, "kernel_device": f"{stat.st_dev}:{stat.st_ino}",
            "sysfs_device": os.path.realpath(selected.path), "boot_id": boot, "network_namespace": netns}


def kernel_addresses(identity: dict, runner: Callable = connectivity_iproute._collect) -> dict:
    data = runner(connectivity_iproute.ADDRESS_COMMAND)
    if len(data) > connectivity_iproute.MAX_BYTES:
        raise ValueError("address inventory too large")
    rows = json.loads(data, object_pairs_hook=connectivity_iproute._object)
    if not isinstance(rows, list) or len(rows) > MAX_INTERFACES:
        raise ValueError("invalid address inventory")
    matches = [row for row in rows if isinstance(row, dict) and row.get("ifindex") == identity["ifindex"]]
    if len(matches) != 1 or matches[0].get("ifname") != identity["name"]:
        raise ValueError("kernel address identity changed")
    raw = matches[0].get("addr_info")
    if not isinstance(raw, list) or len(raw) > MAX_ADDRESSES:
        raise ValueError("invalid address evidence")
    addresses = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("local"), str):
            raise ValueError("invalid address")
        address = ipaddress.ip_address(item["local"])
        prefix = item.get("prefixlen")
        if type(prefix) is not int or not 0 <= prefix <= address.max_prefixlen:
            raise ValueError("invalid address prefix")
        addresses.append({"address": str(address), "prefix": prefix})
    return {"addresses": addresses}


def client_resolver(path: Path = Path("/etc/resolv.conf")) -> dict:
    with path.open("rb") as stream:
        data = stream.read(8193)
    if len(data) > 8192:
        raise ValueError("resolver file too large")
    servers = []
    for line in data.decode("utf-8").splitlines():
        parts = line.split("#", 1)[0].split(";", 1)[0].split()
        if not parts or parts[0] != "nameserver":
            continue
        if len(parts) != 2 or len(servers) >= 32 or "%" in parts[1]:
            raise ValueError("invalid client resolver")
        servers.append(str(ipaddress.ip_address(parts[1])))
    mode = "stub" if any(s in {"127.0.0.53", "127.0.0.54"} for s in servers) else "unknown"
    return {"client_nameservers": servers, "client_mode": mode}


def _section(source: str, status: str, value: Any, gaps: list[str], *, observed_at: str, version: str | None = None, completeness: str | None = None) -> dict:
    return {"evidence": {"source": source, "provider_version": version, "agent_instance_id": None,
                         "host_id": None, "vantage": "agent network namespace", "observed_at": observed_at,
                         "freshness": "unknown", "completeness": completeness or ("partial" if gaps and value is not None else "complete" if value is not None else "unknown"),
                         "status": status, "gaps": gaps}, "value": value}


def collect(ifindex: int, *, identity_reader: Callable = kernel_identity,
            address_reader: Callable = kernel_addresses, inventory_reader: Callable = connectivity_iproute.observe,
            nm_reader: Callable = connectivity_nm.observe, resolver_reader: Callable = connectivity_resolved.observe,
            client_reader: Callable = client_resolver, clock: Callable = time.monotonic) -> dict:
    """Collect bounded independent sections; changing kernel identity invalidates the join."""
    started = _now()
    deadline = clock() + TOTAL_SECONDS
    before = identity_reader(ifindex)
    sections = {}
    def check():
        if clock() >= deadline:
            raise TimeoutError("connectivity observation deadline")
    try:
        check()
        stamp = _now()
        try:
            sections["kernel"] = _section("iproute2.address", "observed", address_reader(before), [], observed_at=stamp)
        except Exception:
            sections["kernel"] = _section("iproute2.address", "unavailable", None, ["Kernel addresses unavailable or changed."], observed_at=stamp)
        check()
        stamp = _now()
        try:
            inventory = inventory_reader()
            for kind in ("routes", "rules"):
                part = inventory[kind]
                sections[kind] = _section("iproute2." + kind, "observed" if part["value"] is not None else "unavailable", part["value"], part["gaps"], observed_at=stamp, completeness=part["completeness"])
        except Exception:
            for kind in ("routes", "rules"):
                sections[kind] = _section("iproute2." + kind, "unavailable", None, ["Kernel inventory unavailable."], observed_at=stamp)
        check()
        stamp = _now()
        try:
            nm = nm_reader(before["name"])
        except Exception:
            nm = {"status": "unavailable", "profile": None, "applied": None, "version": None, "gaps": ["NetworkManager collection failed."]}
        for kind in ("profile", "applied"):
            sections[kind] = _section("NetworkManager", nm.get(kind + "_status", nm["status"]), nm[kind], nm.get(kind + "_gaps", nm["gaps"]), observed_at=stamp, version=nm["version"])
        check()
        stamp = _now()
        try:
            resolved = resolver_reader(ifindex, before["name"])
        except Exception:
            resolved = {"status": "unavailable", "value": None, "version": None, "gaps": ["Resolver collection failed."]}
        sections["resolver"] = _section("systemd-resolved", resolved["status"], resolved["value"], resolved["gaps"], observed_at=stamp, version=resolved["version"])
        check()
        stamp = _now()
        try:
            sections["client_resolver"] = _section("resolv.conf", "observed", client_reader(), [], observed_at=stamp)
        except Exception:
            sections["client_resolver"] = _section("resolv.conf", "unavailable", None, ["Client resolver configuration unavailable."], observed_at=stamp)
        check()
        after = identity_reader(ifindex)
    except Exception:
        return {"status": "unavailable", "identity": None, "started_at": started, "completed_at": _now(), "sections": None, "gaps": ["Collection could not be completed within the identity and time boundary."]}
    if before != after:
        return {"status": "changed", "identity": None, "started_at": started, "completed_at": _now(), "sections": None, "gaps": ["Kernel interface or host context changed during collection."]}
    if sections["profile"]["value"] is not None:
        sections["profile"]["value"]["device_ifindex"] = ifindex
    for section in sections.values():
        section["evidence"]["vantage"] = "agent network namespace " + before["network_namespace"]
    result = {"status": "observed", "identity": before, "started_at": started, "completed_at": _now(), "sections": sections, "gaps": []}
    if len(json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode()) > connectivity_iproute.MAX_BYTES:
        return {"status": "unavailable", "identity": None, "started_at": started, "completed_at": _now(), "sections": None, "gaps": ["Connectivity evidence exceeded its output bound."]}
    return result
