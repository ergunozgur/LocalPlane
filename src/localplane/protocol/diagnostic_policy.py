"""Closed, immutable representation of the administrator's diagnostic targets.

Parses protected startup configuration and supplies pure target eligibility
checks. A v2 completeness declaration is administrator attestation, not automatic
discovery. Execution must independently check identity and current local guards.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import errno
import grp
import os
import re
import stat
from dataclasses import dataclass


_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_PRIVATE_V4 = tuple(map(ipaddress.ip_network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
_PRIVATE_V6 = ipaddress.ip_network("fc00::/7")


class InvalidDiagnosticPolicy(ValueError):
    """The document cannot safely be used as a diagnostic policy."""


@dataclass(frozen=True, order=True)
class TcpTarget:
    address: str
    port: int


@dataclass(frozen=True)
class DiagnosticPolicy:
    dns: tuple[str, ...]
    tcp: tuple[TcpTarget, ...]
    digest: str
    version: int = 1
    control_plane_complete: bool = False
    control_plane_addresses: tuple[str, ...] = ()


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidDiagnosticPolicy("duplicate key")
        result[key] = value
    return result


def _name(value: object) -> str:
    if not isinstance(value, str) or not value.isascii() or not value.endswith(".") or len(value) > 254:
        raise InvalidDiagnosticPolicy("DNS target must be an ASCII fully qualified name")
    name = value.lower()
    labels = name[:-1].split(".")
    if len(labels) < 2 or any(not _LABEL.fullmatch(label) for label in labels):
        raise InvalidDiagnosticPolicy("invalid DNS target")
    try:
        ipaddress.ip_address(name[:-1])
    except ValueError:
        pass
    else:
        raise InvalidDiagnosticPolicy("DNS target must be a name")
    if labels[-1] in {"localhost", "local", "invalid", "onion"} or name == "metadata.google.internal.":
        raise InvalidDiagnosticPolicy("special DNS target")
    return name


def _target(value: object) -> TcpTarget:
    if not isinstance(value, dict) or set(value) != {"address", "port"}:
        raise InvalidDiagnosticPolicy("TCP target must contain address and port")
    address, port = value["address"], value["port"]
    if not isinstance(address, str) or "%" in address or type(port) is not int or not 1 <= port <= 65535:
        raise InvalidDiagnosticPolicy("invalid TCP target")
    try:
        ip = ipaddress.ip_address(address)
    except ValueError as exc:
        raise InvalidDiagnosticPolicy("TCP address must be numeric") from exc
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped or ip.sixtofour or ip.teredo:
            raise InvalidDiagnosticPolicy("transition address is not supported")
        # Exclude translation prefixes outside normal global-unicast space.
        allowed = (ip.is_global and ip in ipaddress.ip_network("2000::/3")) or ip in _PRIVATE_V6
    else:
        allowed = ip.is_global or any(ip in network for network in _PRIVATE_V4)
    if str(ip) == "168.63.129.16" or not allowed or ip.is_multicast or ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_reserved:
        raise InvalidDiagnosticPolicy("special TCP address")
    return TcpTarget(str(ip), port)


def _control_address(value: object) -> str:
    """Normalize an exact address-wide deny, including IPv4-mapped aliases.

    A deny may name any numeric address: it grants no traffic authority.
    """
    if type(value) is not str or len(value) > 45 or "%" in value:
        raise InvalidDiagnosticPolicy("control-plane address must be numeric and unscoped")
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise InvalidDiagnosticPolicy("invalid control-plane address") from exc
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(address)


def _nonfinite(value: str) -> None:
    raise InvalidDiagnosticPolicy("nonfinite policy number")


def parse_diagnostic_policy(document: bytes) -> DiagnosticPolicy:
    """Normalize bounded v1/v2 policy; v2 completeness is admin attestation.

    V1 canonical bytes remain unchanged. It never gains TCP authority implicitly.
    """
    if len(document) > 16384:
        raise InvalidDiagnosticPolicy("policy too large")
    try:
        value = json.loads(document, object_pairs_hook=_object, parse_constant=_nonfinite)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise InvalidDiagnosticPolicy("invalid policy JSON") from exc
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] not in (1, 2):
        raise InvalidDiagnosticPolicy("invalid policy version or keys")
    version = value["version"]
    keys = {"version", "dns", "tcp"} | ({"control_plane"} if version == 2 else set())
    if set(value) != keys:
        raise InvalidDiagnosticPolicy("invalid policy version or keys")
    dns, tcp = value["dns"], value["tcp"]
    if not isinstance(dns, list) or not isinstance(tcp, list) or len(dns) > 32 or len(tcp) > 32:
        raise InvalidDiagnosticPolicy("invalid policy target arrays")
    names = tuple(sorted({_name(item) for item in dns}))
    targets = tuple(sorted({_target(item) for item in tcp}))
    complete = False
    addresses: tuple[str, ...] = ()
    normalized = {"version": version, "dns": names, "tcp": [{"address": target.address, "port": target.port} for target in targets]}
    if version == 2:
        declaration = value["control_plane"]
        if (type(declaration) is not dict or set(declaration) != {"complete", "addresses"}
                or type(declaration["complete"]) is not bool or type(declaration["addresses"]) is not list
                or len(declaration["addresses"]) > 128):
            raise InvalidDiagnosticPolicy("invalid control-plane declaration")
        complete = declaration["complete"]
        addresses = tuple(sorted({_control_address(item) for item in declaration["addresses"]}))
        normalized["control_plane"] = {"complete": complete, "addresses": addresses}
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    return DiagnosticPolicy(names, targets, hashlib.sha256(canonical).hexdigest(), version, complete, addresses)


DEFAULT_POLICY_PATH = "/etc/localplane/diagnostic-targets.json"
POLICY_PATH_ENV = "LOCALPLANE_DIAGNOSTIC_POLICY_PATH"
_EMPTY = parse_diagnostic_policy(b'{"version":1,"dns":[],"tcp":[]}')


def _has_access_acl(fd: int) -> bool:
    """Refuse extended ACLs rather than guessing whether a named principal can write."""
    try:
        os.getxattr(fd, "system.posix_acl_access")
    except OSError as exc:
        if exc.errno in (errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP):
            return False
        raise
    return True


@dataclass(frozen=True)
class LoadedDiagnosticPolicy:
    # Invalid/unsafe/unreadable never carry an empty *valid* policy.
    status: str
    policy: DiagnosticPolicy | None


def tcp_target_permitted(
    loaded: LoadedDiagnosticPolicy,
    digest: str,
    address: str,
    port: int,
    *,
    local_addresses: tuple[str, ...] | None,
    broadcast_addresses: tuple[str, ...] | None,
    management_addresses: tuple[str, ...] | None,
) -> bool:
    """Fail closed on incomplete current host exclusions; this alone is not dispatch authority.

    The caller must obtain authoritative, current, complete exclusion sets and the agent
    must independently repeat all checks, including digest, resource limits and identity.
    """
    if (loaded.status != "valid" or loaded.policy is None or digest != loaded.policy.digest
            or type(loaded.policy.version) is not int or loaded.policy.version != 2
            or loaded.policy.control_plane_complete is not True):
        return False
    sets = (local_addresses, broadcast_addresses, management_addresses)
    if any(type(values) is not tuple for values in sets):
        return False
    try:
        target = _target({"address": address, "port": port})
        declared = loaded.policy.control_plane_addresses
        if type(declared) is not tuple or len(declared) > 128:
            return False
        # Current accepted-connection evidence may ADD to this declared deny set;
        # it cannot remove it or claim universal control-plane discovery.
        excluded = {_control_address(value) for value in (*declared, *local_addresses, *broadcast_addresses, *management_addresses)}
    except (InvalidDiagnosticPolicy, ValueError):
        return False
    return target in loaded.policy.tcp and target.address not in excluded


def load_diagnostic_policy(path: str | None = None) -> LoadedDiagnosticPolicy:
    """Read once, without symlink traversal, through root-owned protected directories.

    Descriptors anchor every lookup, so renaming an ancestor cannot redirect a later
    lookup. No path supplied here may be changed by a request.
    """
    path = path if path is not None else os.environ.get(POLICY_PATH_ENV, DEFAULT_POLICY_PATH)
    if not path.startswith("/") or any(part in (".", "..") for part in path.split("/")):
        return LoadedDiagnosticPolicy("unsafe", None)
    parts = [part for part in path.split("/") if part]
    if not parts:
        return LoadedDiagnosticPolicy("unsafe", None)
    fd = None
    try:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        for part in parts[:-1]:
            info = os.fstat(fd)
            if info.st_uid != 0 or info.st_mode & 0o022 or _has_access_acl(fd):
                return LoadedDiagnosticPolicy("unsafe", None)
            newfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = newfd
        info = os.fstat(fd)
        if info.st_uid != 0 or info.st_mode & 0o022 or _has_access_acl(fd):
            return LoadedDiagnosticPolicy("unsafe", None)
        newfd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
        os.close(fd)
        fd = newfd
        info = os.fstat(fd)
        try:
            gid = grp.getgrnam("localplane").gr_gid
        except KeyError:
            return LoadedDiagnosticPolicy("unsafe", None)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != gid or stat.S_IMODE(info.st_mode) != 0o640 or info.st_nlink != 1 or _has_access_acl(fd):
            return LoadedDiagnosticPolicy("unsafe", None)
        with os.fdopen(fd, "rb") as stream:
            fd = None
            document = stream.read(16385)
        try:
            policy = parse_diagnostic_policy(document)
        except InvalidDiagnosticPolicy:
            return LoadedDiagnosticPolicy("invalid", None)
        return LoadedDiagnosticPolicy("valid", policy)
    except FileNotFoundError:
        return LoadedDiagnosticPolicy("missing", _EMPTY)
    except OSError as exc:
        return LoadedDiagnosticPolicy("unsafe" if exc.errno in (errno.ELOOP, errno.ENOTDIR) else "unreadable", None)
    finally:
        if fd is not None:
            os.close(fd)
