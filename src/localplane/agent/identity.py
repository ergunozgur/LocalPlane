"""Host identity.

A host needs an identifier that survives reboots, hostname changes and interface renames,
because everything LocalPlane records is eventually keyed to it.

``/etc/machine-id`` is the one identifier on a Linux system that is meant to be exactly
that. It is not used directly: systemd treats it as confidential, because it is stable and
unique enough to correlate a machine across unrelated services. The host id is an
application-specific derivation of it — HMAC-SHA256 keyed by the machine id — following
the same reasoning as ``sd_id128_get_machine_app_specific``. It is stable for LocalPlane
and useless anywhere else.

When there is no machine id the identity falls back to the hostname, and says so: the
basis and its confidence travel with the identity so that nothing downstream has to
assume the id is as stable as it looks. When there is neither, identification fails
rather than inventing something.
"""

from __future__ import annotations

import hmac
import os
import re
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

_APP_ID = b"localplane.host.v1"
_MACHINE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

BASIS_MACHINE_ID = "machine_id"
BASIS_DBUS_MACHINE_ID = "dbus_machine_id"
BASIS_HOSTNAME = "hostname_fallback"


class HostIdentityUnavailable(RuntimeError):
    """Neither a machine id nor a hostname could be read.

    Raised rather than substituted: an identifier that LocalPlane made up would key
    durable records to a host it cannot recognise again.
    """


@dataclass(frozen=True)
class HostIdentity:
    """What the agent knows about the host it is running on.

    Every optional field is ``None`` when its source was not readable, and the source is
    named in :attr:`gaps`. No field is defaulted to a plausible value.
    """

    host_id: str
    identity_basis: str
    identity_confidence: str
    hostname: str | None = None
    configured_hostname: str | None = None
    boot_id: str | None = None
    os_id: str | None = None
    os_version_id: str | None = None
    os_pretty_name: str | None = None
    kernel_name: str | None = None
    kernel_release: str | None = None
    architecture: str | None = None
    gaps: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        return {
            "host_id": self.host_id,
            "identity_basis": self.identity_basis,
            "identity_confidence": self.identity_confidence,
            "hostname": self.hostname,
            "configured_hostname": self.configured_hostname,
            "boot_id": self.boot_id,
            "os_id": self.os_id,
            "os_version_id": self.os_version_id,
            "os_pretty_name": self.os_pretty_name,
            "kernel_name": self.kernel_name,
            "kernel_release": self.kernel_release,
            "architecture": self.architecture,
            "gaps": list(self.gaps),
        }


def identify_host(root: Path | str = "/", uname: os.uname_result | None = None) -> HostIdentity:
    """Read this host's identity.

    ``root`` exists so the reader can be pointed at a fixture tree; ``uname`` likewise,
    because ``os.uname()`` has no such seam. Both default to the running system.
    """
    root = Path(root)
    uname = uname if uname is not None else os.uname()
    gaps: list[str] = []

    machine_id, basis = _read_machine_id(root)
    running_hostname = uname.nodename or None
    configured_hostname = _read_text(root / "etc/hostname")

    if machine_id is not None:
        digest = hmac.new(machine_id.encode("ascii"), _APP_ID, sha256).hexdigest()
        host_id = f"host_{digest[:32]}"
        confidence = "high"
    else:
        gaps.append("machine_id")
        seed = running_hostname or configured_hostname
        if not seed:
            raise HostIdentityUnavailable(
                "no machine id and no hostname: this host cannot be identified"
            )
        digest = sha256(_APP_ID + b"|" + seed.encode("utf-8")).hexdigest()
        host_id = f"host_{digest[:32]}"
        basis = BASIS_HOSTNAME
        confidence = "low"

    boot_id = _read_text(root / "proc/sys/kernel/random/boot_id")
    if boot_id is None:
        gaps.append("boot_id")
    if configured_hostname is None:
        gaps.append("etc_hostname")

    os_release = _read_os_release(root / "etc/os-release")
    if os_release is None:
        gaps.append("os_release")
        os_release = {}

    return HostIdentity(
        host_id=host_id,
        identity_basis=basis,
        identity_confidence=confidence,
        hostname=running_hostname,
        configured_hostname=configured_hostname,
        boot_id=boot_id,
        os_id=os_release.get("ID"),
        os_version_id=os_release.get("VERSION_ID"),
        os_pretty_name=os_release.get("PRETTY_NAME"),
        kernel_name=uname.sysname or None,
        kernel_release=uname.release or None,
        architecture=uname.machine or None,
        gaps=tuple(gaps),
    )


def _read_machine_id(root: Path) -> tuple[str | None, str]:
    for relative, basis in (
        ("etc/machine-id", BASIS_MACHINE_ID),
        ("var/lib/dbus/machine-id", BASIS_DBUS_MACHINE_ID),
    ):
        raw = _read_text(root / relative)
        if raw and _MACHINE_ID_RE.match(raw):
            return raw, basis
    return None, BASIS_HOSTNAME


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _read_os_release(path: Path) -> dict[str, str] | None:
    raw = _read_text(path)
    if raw is None:
        return None
    values: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values
