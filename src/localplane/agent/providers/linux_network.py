"""The Linux network provider.

Two machine-readable sources, both structured, neither of them human-oriented output:

* **sysfs** (``/sys/class/net``) is the kernel's own file-based interface. One value per
  file, no parsing ambiguity, no subprocess. It carries everything about the link layer
  that LocalPlane needs, plus counters, and it is the only source for ``speed``,
  ``duplex`` and ``addr_assign_type``.
* **rtnetlink**, reached through ``ip --json --details``. iproute2's JSON mode is a
  machine-readable rendering of netlink attributes, not a scrape of the human output. It
  supplies the two things sysfs cannot: L3 addresses, and ``IFLA_INFO_KIND`` — the
  kernel's own name for what a virtual link *is* (``bridge``, ``veth``, ``tun``), which
  is how this provider avoids guessing device classes from interface names.

The two disagree about one thing, and it matters. ``/sys/class/net/*/flags`` exposes
``dev->flags`` unmodified, so it does **not** contain ``IFF_RUNNING`` or ``IFF_LOWER_UP``:
a link with a cable and a link without one both read ``0x1003``. Carrier therefore comes
from ``/sys/class/net/*/carrier``, which returns ``EINVAL`` while the link is
administratively down — that read failing is a real answer, and it is recorded as
``carrier=None`` rather than as ``False``.

Nothing here writes. There is no code path in this module that opens a file for writing,
and the only commands it runs are the two fixed read-only argv below.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from localplane.agent.providers.base import (
    CommandResult,
    CommandRunner,
    Fidelity,
    InterfaceAddress,
    InterfaceFacts,
    InterfaceObservationBatch,
    InterfaceStatistics,
    ObservedInterface,
    ProviderIssue,
    SubprocessRunner,
)
from localplane.protocol.capabilities import CAPABILITY_NETWORK_OBSERVE

PROVIDER_NAME = "linux.network"
PROVIDER_VERSION = "1"

DEFAULT_SYSFS_NET = "/sys/class/net"

LINK_ARGV: tuple[str, ...] = ("ip", "--json", "--details", "link", "show")
ADDR_ARGV: tuple[str, ...] = ("ip", "--json", "addr", "show")

# IFNAMSIZ is 16 including the terminator, so 15 characters is the kernel's own ceiling.
INTERFACE_NAME_RE = re.compile(r"^[A-Za-z0-9_.@:+-]{1,15}$")
MAX_REQUESTED_NAMES = 64

ARPHRD_ETHER = 1
ARPHRD_LOOPBACK = 772
ARPHRD_NONE = 65534

IFF_UP = 0x1

_SCALAR_FIELDS = (
    "ifindex",
    "address",
    "addr_assign_type",
    "mtu",
    "operstate",
    "carrier",
    "flags",
    "type",
    "speed",
    "duplex",
    "carrier_changes",
)
_CORE_FIELDS = ("ifindex", "flags", "operstate", "mtu", "type")
_STATISTIC_FIELDS = (
    "rx_bytes",
    "tx_bytes",
    "rx_packets",
    "tx_packets",
    "rx_errors",
    "tx_errors",
    "rx_dropped",
    "tx_dropped",
)
# Verbose netlink sub-objects. Dropping them keeps stored evidence proportionate; the
# omission is declared on every observation rather than left for a reader to discover.
_OMITTED_LINK_KEYS = ("info_data", "info_slave_data")


class InvalidInterfaceName(ValueError):
    """A requested interface name is not a name the kernel could have given a link."""


class LinuxNetworkProvider:
    """Observe network interfaces from sysfs and rtnetlink. Read-only."""

    name = PROVIDER_NAME
    version = PROVIDER_VERSION

    def __init__(
        self,
        sysfs_net: str | Path = DEFAULT_SYSFS_NET,
        runner: CommandRunner | None = None,
        command_timeout_s: float = 5.0,
    ) -> None:
        self._sysfs_net = Path(sysfs_net)
        self._runner = runner if runner is not None else SubprocessRunner()
        self._command_timeout_s = command_timeout_s

    # ---------------------------------------------------------------- public interface

    def observe_interfaces(
        self, names: Sequence[str] | None = None
    ) -> InterfaceObservationBatch:
        started_at = _now()
        requested = _validate_names(names)
        issues: list[ProviderIssue] = []

        discovered = self._list_sysfs_interfaces(issues)
        if discovered is None:
            return InterfaceObservationBatch(
                provider=self.name,
                provider_version=self.version,
                capability=CAPABILITY_NETWORK_OBSERVE,
                started_at=started_at,
                completed_at=_now(),
                issues=tuple(issues),
                missing=tuple(requested or ()),
            )

        link_index, link_command = self._read_rtnetlink(LINK_ARGV, "rtnetlink.link", issues)
        addr_index, addr_command = self._read_rtnetlink(ADDR_ARGV, "rtnetlink.addr", issues)
        commands = [c.as_dict() for c in (link_command, addr_command) if c is not None]

        if requested is None:
            selected = discovered
            missing: tuple[str, ...] = ()
        else:
            present = set(discovered)
            selected = [n for n in discovered if n in requested]
            missing = tuple(n for n in requested if n not in present)

        observed = tuple(
            self._observe_one(
                name,
                link_entry=link_index.get(name) if link_index is not None else None,
                addr_entry=addr_index.get(name) if addr_index is not None else None,
                link_source_available=link_index is not None,
                addr_source_available=addr_index is not None,
                commands=commands,
            )
            for name in selected
        )

        return InterfaceObservationBatch(
            provider=self.name,
            provider_version=self.version,
            capability=CAPABILITY_NETWORK_OBSERVE,
            started_at=started_at,
            completed_at=_now(),
            interfaces=observed,
            missing=missing,
            issues=tuple(issues),
        )

    # ------------------------------------------------------------------------- sysfs

    def _list_sysfs_interfaces(self, issues: list[ProviderIssue]) -> list[str] | None:
        try:
            entries = sorted(os.listdir(self._sysfs_net))
        except OSError as exc:
            issues.append(
                ProviderIssue(
                    source="sysfs",
                    code="sysfs_unreadable",
                    message=f"cannot list {self._sysfs_net}: {exc.strerror or exc}",
                    detail={"path": str(self._sysfs_net), "errno": exc.errno},
                )
            )
            return None
        # `bonding_masters` and similar control files live beside the links; an entry is
        # an interface only if the kernel gave it an ifindex.
        return [e for e in entries if (self._sysfs_net / e / "ifindex").exists()]

    def _read_sysfs(self, name: str) -> tuple[dict[str, str], dict[str, str]]:
        base = self._sysfs_net / name
        values: dict[str, str] = {}
        errors: dict[str, str] = {}
        for field in _SCALAR_FIELDS:
            try:
                values[field] = (base / field).read_text(encoding="utf-8", errors="replace").strip()
            except OSError as exc:
                errors[field] = exc.strerror or str(exc)
        for field in _STATISTIC_FIELDS:
            try:
                raw = (base / "statistics" / field).read_text(encoding="utf-8").strip()
            except OSError as exc:
                errors[f"statistics/{field}"] = exc.strerror or str(exc)
            else:
                values[f"statistics/{field}"] = raw
        for field in ("uevent",):
            try:
                values[field] = (base / field).read_text(encoding="utf-8", errors="replace").strip()
            except OSError as exc:
                errors[field] = exc.strerror or str(exc)
        return values, errors

    def _read_topology(self, name: str) -> dict[str, Any]:
        """Structural facts that are present as a directory or a symlink, not a value."""
        base = self._sysfs_net / name
        return {
            "has_device": (base / "device").exists(),
            "is_bridge": (base / "bridge").is_dir(),
            "is_wireless": (base / "wireless").is_dir() or (base / "phy80211").exists(),
            "is_tun": (base / "tun_flags").exists(),
            "device_path": _device_path(base),
            "master": _link_basename(base / "master"),
        }

    # -------------------------------------------------------------------- rtnetlink

    def _read_rtnetlink(
        self, argv: tuple[str, ...], source: str, issues: list[ProviderIssue]
    ) -> tuple[dict[str, dict[str, Any]] | None, CommandResult | None]:
        result = self._runner.run(argv, timeout_s=self._command_timeout_s)
        if not result.ok:
            issues.append(
                ProviderIssue(
                    source=source,
                    code="command_failed",
                    message=(result.error or result.stderr.strip() or "command returned non-zero"),
                    detail={"argv": list(argv), "returncode": result.returncode},
                )
            )
            return None, result
        try:
            parsed = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            issues.append(
                ProviderIssue(
                    source=source,
                    code="unparseable_output",
                    message=f"{' '.join(argv)} did not return JSON: {exc}",
                    detail={"argv": list(argv)},
                )
            )
            return None, result
        if not isinstance(parsed, list):
            issues.append(
                ProviderIssue(
                    source=source,
                    code="unexpected_shape",
                    message=f"{' '.join(argv)} returned {type(parsed).__name__}, expected a list",
                    detail={"argv": list(argv)},
                )
            )
            return None, result
        index = {
            entry["ifname"]: entry
            for entry in parsed
            if isinstance(entry, dict) and isinstance(entry.get("ifname"), str)
        }
        return index, result

    # ----------------------------------------------------------------- normalization

    def _observe_one(
        self,
        name: str,
        link_entry: dict[str, Any] | None,
        addr_entry: dict[str, Any] | None,
        link_source_available: bool,
        addr_source_available: bool,
        commands: list[dict[str, Any]],
    ) -> ObservedInterface:
        values, errors = self._read_sysfs(name)
        topology = self._read_topology(name)
        # Stamped per interface rather than per sweep: a sweep spans a window, and an
        # observation is entitled to say when *it* was read.
        observed_at = _now()
        gaps: list[str] = []

        flags = _parse_int(values.get("flags"))
        arphrd = _parse_int(values.get("type"))
        devtype = _uevent_value(values.get("uevent"), "DEVTYPE")
        link_kind = None
        if isinstance(link_entry, dict):
            info = link_entry.get("linkinfo")
            if isinstance(info, dict) and isinstance(info.get("info_kind"), str):
                link_kind = info["info_kind"]

        if addr_source_available:
            addresses = _parse_addresses(addr_entry)
        else:
            addresses = None
            gaps.append("addresses")
        if not link_source_available:
            gaps.append("link_kind")

        speed = _parse_speed(values.get("speed"))
        if speed is None:
            gaps.append("speed_mbps")
        duplex = _parse_duplex(values.get("duplex"))
        if duplex is None:
            gaps.append("duplex")
        carrier = _parse_carrier(values.get("carrier"))
        if carrier is None:
            gaps.append("carrier")

        assign_type = _parse_int(values.get("addr_assign_type"))

        facts = InterfaceFacts(
            name=name,
            kind=classify_kind(
                arphrd_type=arphrd,
                link_kind=link_kind,
                devtype=devtype,
                is_bridge=bool(topology["is_bridge"]),
                is_wireless=bool(topology["is_wireless"]),
                is_tun=bool(topology["is_tun"]),
            ),
            ifindex=_parse_int(values.get("ifindex")),
            link_kind=link_kind,
            arphrd_type=arphrd,
            mac_address=_parse_mac(values.get("address")),
            mac_is_permanent=None if assign_type is None else assign_type == 0,
            mtu=_parse_int(values.get("mtu")),
            admin_up=None if flags is None else bool(flags & IFF_UP),
            operstate=(values.get("operstate") or None),
            carrier=carrier,
            speed_mbps=speed,
            duplex=duplex,
            is_physical=bool(topology["has_device"]),
            device_path=topology["device_path"],
            master=topology["master"],
            carrier_changes=_parse_int(values.get("carrier_changes")),
            addresses=addresses,
            statistics=_parse_statistics(values),
        )

        missing_core = [f for f in _CORE_FIELDS if f in errors]
        if missing_core:
            fidelity = Fidelity.DEGRADED
            gaps.extend(f"sysfs.{f}" for f in missing_core)
        elif not link_source_available or not addr_source_available:
            fidelity = Fidelity.PARTIAL
        else:
            fidelity = Fidelity.COMPLETE

        method = "sysfs"
        if link_source_available or addr_source_available:
            method = "sysfs+rtnetlink_json"

        evidence: dict[str, Any] = {
            "sysfs_path": str(self._sysfs_net / name),
            "sysfs": values,
            "sysfs_errors": errors,
            "sysfs_topology": topology,
            "rtnetlink_link": _strip_verbose_link(link_entry),
            "rtnetlink_addr": addr_entry,
            "commands": commands,
            "omitted": [f"rtnetlink_link.linkinfo.{k}" for k in _OMITTED_LINK_KEYS],
        }

        return ObservedInterface(
            facts=facts,
            method=method,
            fidelity=fidelity,
            observed_at=observed_at,
            evidence=evidence,
            gaps=tuple(dict.fromkeys(gaps)),
        )


# ------------------------------------------------------------------------------ helpers


def classify_kind(
    *,
    arphrd_type: int | None,
    link_kind: str | None,
    devtype: str | None,
    is_bridge: bool,
    is_wireless: bool,
    is_tun: bool,
) -> str:
    """Name what a link *is*, from kernel evidence only.

    Order matters: a wireless NIC and a veth both report ``ARPHRD_ETHER``, so the
    specific evidence — ``IFLA_INFO_KIND``, ``DEVTYPE``, the presence of a ``bridge/`` or
    ``phy80211`` node — is consulted before falling back to the address family.

    No branch here looks at the interface name. ``docker0`` is classified a bridge
    because the kernel says ``info_kind=bridge``, not because of what it is called.
    """
    if arphrd_type == ARPHRD_LOOPBACK:
        return "loopback"
    if link_kind == "bridge" or is_bridge:
        return "bridge"
    if link_kind == "veth":
        return "virtual_ethernet"
    if link_kind in {"tun", "tap"} or is_tun:
        return "tunnel"
    if devtype == "wlan" or is_wireless:
        return "wireless"
    if devtype == "wwan":
        return "wwan"
    if link_kind == "vlan":
        return "vlan"
    if link_kind in {"bond", "team"}:
        return "bond"
    if link_kind:
        return "virtual"
    if arphrd_type == ARPHRD_ETHER:
        return "ethernet"
    return "unknown"


def _validate_names(names: Sequence[str] | None) -> set[str] | None:
    """Accept a filter, never an argument.

    Validated names are only ever compared against what sysfs already listed. They are
    not interpolated into a command, so this check is defence in depth rather than the
    thing standing between a caller and the shell — that is the absence of any code path
    that would pass them to one.
    """
    if names is None:
        return None
    names = list(names)
    if len(names) > MAX_REQUESTED_NAMES:
        raise InvalidInterfaceName(
            f"at most {MAX_REQUESTED_NAMES} interface names may be requested at once"
        )
    for name in names:
        if not isinstance(name, str) or not INTERFACE_NAME_RE.match(name):
            raise InvalidInterfaceName(f"not a valid interface name: {name!r}")
    return set(names)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _parse_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    text = raw.strip()
    try:
        return int(text, 16) if text.lower().startswith("0x") else int(text)
    except ValueError:
        return None


def _parse_mac(raw: str | None) -> str | None:
    if not raw:
        return None
    mac = raw.strip().lower()
    return mac or None


def _parse_carrier(raw: str | None) -> bool | None:
    """``carrier`` reads ``EINVAL`` while the link is admin-down, and that is not False."""
    value = _parse_int(raw)
    if value is None:
        return None
    return bool(value)


def _parse_speed(raw: str | None) -> int | None:
    """The kernel writes ``-1`` when it does not know the speed. That is not a speed."""
    value = _parse_int(raw)
    if value is None or value < 0:
        return None
    return value


def _parse_duplex(raw: str | None) -> str | None:
    """``unknown`` is the driver saying it does not know. Preserve that as ``None``."""
    if raw is None:
        return None
    value = raw.strip().lower()
    if not value or value == "unknown":
        return None
    return value


def _parse_statistics(values: dict[str, str]) -> InterfaceStatistics | None:
    parsed = {f: _parse_int(values.get(f"statistics/{f}")) for f in _STATISTIC_FIELDS}
    if all(v is None for v in parsed.values()):
        return None
    return InterfaceStatistics(**parsed)


def _parse_addresses(entry: dict[str, Any] | None) -> tuple[InterfaceAddress, ...]:
    """An interface absent from the address dump genuinely has no addresses.

    ``ip addr show`` lists every link, so a link that is present with an empty
    ``addr_info`` and a link that is present at all both mean "no addresses" rather than
    "not asked". The caller distinguishes "not asked" by passing ``None`` for the whole
    source instead of calling this.
    """
    if not entry:
        return ()
    raw = entry.get("addr_info")
    if not isinstance(raw, list):
        return ()
    addresses = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        local = item.get("local")
        prefix = item.get("prefixlen")
        family = item.get("family")
        if not isinstance(local, str) or not isinstance(prefix, int) or not isinstance(family, str):
            continue
        addresses.append(
            InterfaceAddress(
                family=family,
                address=local,
                prefix_length=prefix,
                scope=item.get("scope") if isinstance(item.get("scope"), str) else None,
                dynamic=item.get("dynamic") if isinstance(item.get("dynamic"), bool) else None,
                valid_lifetime_s=_lifetime(item.get("valid_life_time")),
                preferred_lifetime_s=_lifetime(item.get("preferred_life_time")),
            )
        )
    return tuple(addresses)


def _lifetime(value: Any) -> int | None:
    """``0xffffffff`` is iproute2 for 'forever', which is an absence of a lifetime."""
    if not isinstance(value, int) or value == 0xFFFFFFFF:
        return None
    return value


def _uevent_value(raw: str | None, key: str) -> str | None:
    if not raw:
        return None
    for line in raw.splitlines():
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip() or None
    return None


def _device_path(base: Path) -> str | None:
    """``subsystem/device`` — e.g. ``platform/fd580000.ethernet`` — or ``None``.

    Stable across reboots and interface renames for a device on a fixed bus slot, which
    is what makes it usable as an identity basis when a MAC is not permanent.
    """
    device = _link_basename(base / "device")
    if device is None:
        return None
    subsystem = _link_basename(base / "device" / "subsystem")
    return f"{subsystem}/{device}" if subsystem else device


def _link_basename(path: Path) -> str | None:
    try:
        return os.path.basename(os.readlink(path)) or None
    except OSError:
        return None


def _strip_verbose_link(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if not entry:
        return entry
    info = entry.get("linkinfo")
    if not isinstance(info, dict):
        return entry
    trimmed_info = {k: v for k, v in info.items() if k not in _OMITTED_LINK_KEYS}
    return {**entry, "linkinfo": trimmed_info}


def iter_interface_names(sysfs_net: str | Path = DEFAULT_SYSFS_NET) -> Iterable[str]:
    """Interface names visible in sysfs. Used by capability probing."""
    root = Path(sysfs_net)
    for entry in sorted(os.listdir(root)):
        if (root / entry / "ifindex").exists():
            yield entry
