"""Offline release maintenance: no services, subprocesses, auth initialization or migrations.

Only the shared controller writes the external witness. Offline transitions hold
its runtime/control exclusion. Failures retain the fence and displaced evidence.
"""
from __future__ import annotations

import argparse
import base64
import binascii
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
from importlib import metadata as package_metadata
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shlex
import shutil
import sqlite3
import stat
import sys
from typing import Any, Iterator


class MaintenanceError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def refuse(code: str, message: str) -> None:
    raise MaintenanceError(code, message)


def trusted_uids() -> set[int]:
    service_uid = int(os.environ.get("LOCALPLANE_SERVICE_UID", str(os.geteuid())))
    if os.geteuid() != 0 and service_uid != os.geteuid():
        refuse("service_identity_mismatch", "Invoke as the configured service identity or administrator")
    return {0, os.geteuid(), service_uid}


def shared_group_path(path: Path, info: os.stat_result) -> bool:
    value = os.environ.get("LOCALPLANE_MAINTENANCE_DIR")
    if not value:
        return False
    root = Path(value)
    gid = int(os.environ.get("LOCALPLANE_SERVICE_GID", str(os.getegid())))
    return (path == root or root in path.parents) and info.st_gid == gid


def safe_path(path: Path) -> None:
    """Inspect lexical ancestors without following links or weakening permissions."""
    if not path.is_absolute() or ".." in path.parts:
        refuse("unsafe_path", "An absolute path without traversal is required")
    for part in [*reversed(path.parents), path]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode) or info.st_mode & 0o002
                or (info.st_mode & 0o020 and not shared_group_path(part, info))):
            refuse("unsafe_path", "Symlinks and group/world-writable paths are refused")
        if info.st_uid not in trusted_uids():
            refuse("unsafe_owner", "Path owner is not trusted")


def effective_paths() -> dict[str, Path]:
    defaults = dict(root="/opt/localplane", etc="/etc/localplane",
                    var="/var/lib/localplane", run="/run/localplane")
    paths = {k: Path(os.environ.get("LOCALPLANE_" + k.upper(), v)) for k, v in defaults.items()}
    for path in paths.values():
        safe_path(path)
    roots = list(paths.values())
    if any(a == b or a in b.parents or b in a.parents
           for i, a in enumerate(roots) for b in roots[i + 1:]):
        refuse("overlapping_roots", "Installation roots must be disjoint")
    return paths


def regular(path: Path, *, private: bool = False) -> os.stat_result:
    safe_path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        refuse("unsafe_file", "Expected a single-link regular file")
    if private and info.st_mode & 0o077:
        refuse("unsafe_private_file", "Private state must not be accessible to other identities")
    return info


def protected_directory(path: Path) -> None:
    safe_path(path)
    if path.exists() and (not path.is_dir() or path.stat().st_mode & 0o077):
        refuse("unsafe_private_directory", "Private maintenance/backup directories must be mode 0700 or stricter")


def sha(path: Path) -> str:
    regular(path)
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    regular(path)
    if path.stat().st_size > 4 * 1024 * 1024:
        refuse("metadata_too_large", "Metadata exceeds the supported bound")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        refuse("invalid_metadata", "Metadata must be an object")
    return value


def sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_bytes(path: Path, body: bytes, *, mode: int = 0o600,
                 owner: tuple[int, int] | None = None) -> None:
    safe_path(path)
    temp = path.parent / ("." + path.name + "." + secrets.token_hex(12))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            if owner is not None and (os.geteuid(), os.getegid()) != owner:
                os.fchown(stream.fileno(), *owner)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        sync_dir(path.parent)
    finally:
        if temp.exists():
            temp.unlink()


def write_json(path: Path, body: dict[str, Any]) -> None:
    atomic_bytes(path, (json.dumps(body, sort_keys=True, indent=2) + "\n").encode())


def atomic_copy(source: Path, target: Path, *, mode: int = 0o600,
                owner: tuple[int, int] | None = None) -> None:
    """Bounded-memory file retention/restore; live backups use SQLite instead."""
    regular(source)
    safe_path(target)
    temp = target.parent / (".copy_" + secrets.token_hex(16))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
            if owner is not None:
                info = os.fstat(output.fileno())
                if (info.st_uid, info.st_gid) != owner:
                    os.fchown(output.fileno(), *owner)
            shutil.copyfileobj(input_file, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        if sha(source) != sha(temp):
            refuse("copy_changed", "Retained file changed during copy")
        os.replace(temp, target)
        sync_dir(target.parent)
    finally:
        if temp.exists():
            temp.unlink()


def mkdir_parents(path: Path, mode: int) -> None:
    """Set exact requested modes on newly created directories, never existing ones.

    mkdir's mode is masked by the administrator's umask. Create privately first,
    then adjust only the no-follow directory descriptor for this new inode.
    """
    safe_path(path)
    for part in [*reversed(path.parents), path]:
        if not part.exists():
            part.mkdir(mode=0o700)
            created = part.lstat()
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                opened = os.fstat(fd)
                current = part.lstat()
                if (not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.geteuid()
                        or (opened.st_dev, opened.st_ino) != (created.st_dev, created.st_ino)
                        or (current.st_dev, current.st_ino) != (created.st_dev, created.st_ino)):
                    refuse("directory_changed", "New directory identity changed before mode initialization")
                os.fchmod(fd, mode)
                os.fsync(fd)
            finally:
                os.close(fd)
            sync_dir(part.parent)


def inventory(root: Path) -> dict[str, str]:
    safe_path(root)
    result = {}
    for path in sorted(root.rglob("*")):
        safe_path(path)
        if path.is_dir():
            continue
        result[path.relative_to(root).as_posix()] = sha(path)
    return result


def copy_tree(source: Path, target: Path) -> None:
    """Copy verified regular files; never dereference a link."""
    if not source.is_dir():
        refuse("copy_source_missing", "A complete source directory is required")
    before = inventory(source)
    target.mkdir(mode=0o700)
    for path in sorted(source.rglob("*")):
        if path.is_dir():
            mkdir_parents(target / path.relative_to(source), 0o700)
    for name in before:
        src, dst = source / name, target / name
        info = regular(src)
        mkdir_parents(dst.parent, 0o700)
        atomic_bytes(dst, src.read_bytes(), mode=stat.S_IMODE(info.st_mode))
    if inventory(source) != before or inventory(target) != before:
        refuse("copy_changed", "Source changed while being copied")
    for path in sorted((p for p in target.rglob("*") if p.is_dir()), reverse=True):
        sync_dir(path)
    sync_dir(target)
    sync_dir(target.parent)


def shared_modules():
    # No fallback witness or dynamic imports from external source trees.
    sys.dont_write_bytecode = True
    try:
        from localplane import maintenance, build_identity
    except ImportError:
        refuse("shared_contract_unavailable", "Maintenance and build identity modules are required")
    return maintenance, build_identity


def controller(paths: dict[str, Path]):
    maintenance, _ = shared_modules()
    expected = paths["var"] / "maintenance"
    if os.environ.get("LOCALPLANE_MAINTENANCE_DIR") != str(expected):
        refuse("maintenance_root_mismatch", "Explicit shared root must equal data-root/maintenance")
    safe_path(expected)
    if not expected.is_dir():
        refuse("maintenance_root_missing", "Administrator must prepare the shared maintenance directory first")
    return maintenance.MaintenanceController(expected, service="cli", lock_timeout_s=1.0)


@contextmanager
def command_lock(paths: dict[str, Path]) -> Iterator[None]:
    """Serialize CLI commands independently of the controller's non-reentrant leases."""
    root = paths["var"] / "maintenance"
    safe_path(root)
    fd = os.open(root / "cli.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o660)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_mode & 0o007 or info.st_uid not in trusted_uids()
                or not shared_group_path(root / "cli.lock", info)):
            refuse("unsafe_lock", "CLI lock must be a stable regular file confined to the service group")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            refuse("cli_busy", "Another maintenance command is active")
        yield
    finally:
        os.close(fd)


def verify_release(root: Path, *, installed: bool = False) -> dict[str, Any]:
    safe_path(root)
    _, identity = shared_modules()
    loaded = identity.load_build_identity(root / "release.json", process="inspection", inspect_only=True)
    if loaded.status != identity.INSPECTED:
        refuse("invalid_release", "Release identity verification failed: " + ",".join(loaded.reasons))
    manifest = read_json(root / "release.json")
    actual = inventory(root)
    actual.pop("release.json", None)
    if actual != manifest["files"]:
        refuse("release_inventory_mismatch", "Release must contain exactly its manifested files")
    if (not (root / "src/localplane/__init__.py").is_file() or not (root / "vendor").is_dir()
            or not (root / "docs").is_dir()):
        refuse("release_layout_invalid", "Release requires source, documentation and unpacked vendor dependencies")
    if any(name.endswith(".whl") for name in actual):
        refuse("packed_dependency", "Runtime dependencies must be unpacked")
    for name in ("console/index.html", "bin/localplane-backend", "bin/localplane-agent",
                 "bin/localplane-maintenance"):
        if name not in actual:
            refuse("release_layout_invalid", "Required release component is missing")
    units = {name for name in actual if name.startswith("packaging/") and name.endswith(".service")}
    if units != {"packaging/localplane-agent.service", "packaging/localplane-backend.service"}:
        refuse("release_units_invalid", "Exactly two ordinary service templates are required")
    tested = manifest["platform"]
    if (tested.get("architecture") != platform.machine() or tested.get("python") != platform.python_version()
            or tested.get("os") != platform_os()):
        refuse("release_platform_mismatch", "Use the exact tested architecture, OS release and Python version")
    pins = manifest["dependencies"]
    def normalized(name):
        return re.sub(r"[-_.]+", "-", name).lower()
    declared = {normalized(pin["name"]): pin["version"] for pin in pins}
    observed = {}
    for distribution in package_metadata.distributions(path=[str(root / "vendor")]):
        name = normalized(distribution.metadata["Name"])
        if name in observed:
            refuse("duplicate_dependency", "Vendor contains duplicate distribution metadata")
        observed[name] = distribution.version
    if (declared != observed or not {"fastapi", "uvicorn", "jeepney"}.issubset(declared)
            or len(declared) != len(pins) or any(not pin.get("sha256") for pin in pins)):
        refuse("release_dependencies_mismatch", "Vendor must match every pinned, hashed dependency exactly")
    if installed:
        for path in [*root.parents, root, *root.rglob("*")]:
            if path.lstat().st_uid != 0:
                refuse("release_not_root_owned", "Installed release must be root-owned")
    return manifest


def platform_os() -> str:
    """Distro release, not the builder's kernel or an unsupported generic OS label."""
    if sys.platform != "linux":
        refuse("unsupported_platform", "Only Linux installations are supported")
    release = platform.freedesktop_os_release()
    if not release.get("ID") or not release.get("VERSION_ID"):
        refuse("platform_unknown", "Cannot establish exact distribution release")
    return release["ID"] + ":" + release["VERSION_ID"]


def release_path(paths: dict[str, Path], build_id: str) -> Path:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", build_id):
        refuse("invalid_build_id", "Expected a content-addressed build identifier")
    return paths["root"] / "releases" / build_id.removeprefix("sha256:")


def configuration(paths: dict[str, Path], *, body: bytes | None = None) -> tuple[dict[str, str], bytes]:
    if body is None:
        path = paths["etc"] / "environment"
        regular(path)
        body = path.read_bytes()
    env = {}
    for line in body.decode().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"(LOCALPLANE_[A-Z0-9_]+)=(.*)", line)
        if not match or match[1] in env:
            refuse("unsupported_environment", "Use unique simple LOCALPLANE_NAME=value assignments")
        words = shlex.split(match[2])
        if len(words) != 1 or any(c in words[0] for c in "\n\r\x00$`"):
            refuse("unsupported_environment", "Environment values must be literal single values")
        env[match[1]] = words[0]
    expected = {
        "LOCALPLANE_DB_PATH": paths["var"] / "localplane.db",
        "LOCALPLANE_AUTH_SECRET_PATH": paths["var"] / "localplane-master.secret",
        "LOCALPLANE_MAINTENANCE_DIR": paths["var"] / "maintenance",
        "LOCALPLANE_AGENT_SOCKET": paths["run"] / "agent.sock",
    }
    if any(env.get(k) != str(v) for k, v in expected.items()):
        refuse("configuration_path_mismatch", "Database, credential, socket and maintenance paths must match installation roots")
    manifest = Path(env.get("LOCALPLANE_RELEASE_MANIFEST", ""))
    safe_path(manifest)
    if manifest.name != "release.json" or manifest.parent.parent != paths["root"] / "releases":
        refuse("release_path_mismatch", "Configuration must name an exact immutable installed release")
    if manifest.parent != release_path(paths, "sha256:" + manifest.parent.name):
        refuse("release_path_mismatch", "Invalid immutable release path")
    if env.get("LOCALPLANE_CONSOLE_DIR") != str(manifest.parent / "console"):
        refuse("console_path_mismatch", "Console must belong to the exact release")
    for name in ("LOCALPLANE_TLS_CERT_PATH", "LOCALPLANE_TLS_KEY_PATH"):
        path = Path(env.get(name, ""))
        safe_path(path)
        if paths["etc"] not in path.parents:
            refuse("tls_path_mismatch", "TLS files must be beneath the configuration root")
    return env, body


def current_release(paths: dict[str, Path]) -> tuple[Path, dict[str, Any]]:
    env, _ = configuration(paths)
    root = Path(env["LOCALPLANE_RELEASE_MANIFEST"]).parent
    manifest = verify_release(root, installed=True)
    if release_path(paths, manifest["build_id"]) != root:
        refuse("release_path_identity_mismatch", "Installed path must match its exact build identifier")
    return root, manifest


def schema(db: Path) -> dict[str, str]:
    """No application opener, migration, auth initialization or record settlement."""
    regular(db)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            regular(sidecar)
    connection = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            refuse("database_integrity", "SQLite integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            refuse("database_foreign_keys", "SQLite foreign-key check failed")
        rows = connection.execute("SELECT version, name, checksum FROM schema_migrations ORDER BY version").fetchall()
        if not rows:
            refuse("schema_unknown", "An initialized recorded schema is required")
        result = {f"{version:04d}_{name}.sql": checksum for version, name, checksum in rows}
        if len(result) != len(rows):
            refuse("schema_unknown", "Duplicate schema migration identity")
        return result
    finally:
        connection.close()


def compatible(applied: dict[str, str], manifest: dict[str, Any], *, exact: bool = False) -> None:
    shipped = manifest["migrations"]
    if not applied or any(shipped.get(k, "").removeprefix("sha256:") != v for k, v in applied.items()):
        refuse("schema_incompatible", "Release cannot run against the current recorded schema")
    if exact and set(shipped) != set(applied):
        refuse("schema_not_exact", "Snapshot must match all migrations of its release")
    if sorted(shipped)[:len(applied)] != sorted(applied):
        refuse("schema_not_prefix", "Applied schema must be an ordered release prefix")


def sqlite_backup(source: Path, target: Path) -> dict[str, str]:
    before = schema(source)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=1)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        # Backup can inherit WAL journal mode. Seal the offline destination as a
        # standalone DB so later verification never needs mutable backup sidecars.
        if dst.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
            refuse("backup_journal_mode", "Could not seal standalone backup")
    finally:
        dst.close()
        src.close()
    if schema(target) != before or schema(source) != before:
        refuse("schema_changed", "Schema changed during offline backup")
    with target.open("rb") as stream:
        os.fsync(stream.fileno())
    return before


def verify_state_files(paths: dict[str, Path]) -> None:
    env, _ = configuration(paths)
    secret = paths["var"] / "localplane-master.secret"
    validate_secret(secret)
    uid = int(os.environ.get("LOCALPLANE_SERVICE_UID", str(os.geteuid())))
    if regular(secret, private=True).st_uid != uid:
        refuse("credential_owner_mismatch", "Credential must belong to the explicit service identity")
    if regular(Path(env["LOCALPLANE_TLS_KEY_PATH"]), private=True).st_uid != uid:
        refuse("tls_key_owner_mismatch", "TLS key must belong to the explicit service identity")
    regular(Path(env["LOCALPLANE_TLS_CERT_PATH"]))
    inventory(paths["etc"])


def validate_secret(path: Path) -> None:
    info = regular(path, private=True)
    if not 1 <= info.st_size <= 1024:
        refuse("credential_invalid", "Invalid master credential format")
    raw = path.read_bytes()
    token = raw[:-1] if raw.endswith(b"\n") else raw
    try:
        decoded = base64.b64decode(token + b"=" * (-len(token) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        refuse("credential_invalid", "Invalid master credential format")
    if len(decoded) != 32:
        refuse("credential_invalid", "Invalid master credential format")


def capture(paths: dict[str, Path], target: Path, window: Any,
            ) -> dict[str, Any]:
    root, manifest = current_release(paths)
    verify_state_files(paths)
    target.mkdir(mode=0o700)
    applied = sqlite_backup(paths["var"] / "localplane.db", target / "localplane.db")
    compatible(applied, manifest, exact=True)
    copy_tree(paths["etc"], target / "config")
    secret = paths["var"] / "localplane-master.secret"
    atomic_bytes(target / "localplane-master.secret", secret.read_bytes())
    ownership = {}
    for path in [paths["etc"], *paths["etc"].rglob("*")]:
        info = path.lstat()
        name = path.relative_to(paths["etc"]).as_posix()
        ownership[name] = [info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)]
    snapshot = dict(format_version=2, window_id=window.window_id, generation=window.generation,
                    release_path=str(root), release_build_id=manifest["build_id"], schema=applied,
                    database_metadata=file_metadata(paths["var"] / "localplane.db"),
                    secret_metadata=file_metadata(secret),
                    config_metadata=ownership, files=inventory(target),
                    created_at=datetime.now(timezone.utc).isoformat(),
                    sqlite_version=sqlite3.sqlite_version,
                    platform={"os": platform_os(), "architecture": platform.machine(),
                              "python": platform.python_version()})
    write_json(target / "snapshot.json", snapshot)
    sync_dir(target)
    return snapshot


def retain_displaced(paths: dict[str, Path], target: Path, window: Any) -> None:
    """Raw evidence retention, NOT a restorable snapshot; permits a corrupt failed store."""
    target.mkdir(mode=0o700)
    copy_tree(paths["etc"], target / "config")
    secret = paths["var"] / "localplane-master.secret"
    regular(secret, private=True)
    atomic_bytes(target / "localplane-master.secret", secret.read_bytes())
    db = paths["var"] / "localplane.db"
    for suffix in ("", "-wal", "-shm"):
        source = Path(str(db) + suffix)
        if source.exists():
            regular(source)
            atomic_copy(source, target / ("raw-localplane.db" + suffix))
    write_json(target / "displaced.json", {
        "window_id": window.window_id, "generation": window.generation,
        "kind": "raw evidence only; not a recovery snapshot", "files": inventory(target),
    })
    sync_dir(target)


def snapshot_path(paths: dict[str, Path], name: str) -> Path:
    if not re.fullmatch(r"snapshot_[0-9a-f]{32}", name):
        refuse("snapshot_id_invalid", "Invalid snapshot identifier")
    return paths["var"] / "backups" / name


def verify_snapshot(paths: dict[str, Path], name: str) -> tuple[Path, dict[str, Any]]:
    root = snapshot_path(paths, name)
    protected_directory(root.parent)
    protected_directory(root)
    regular(root / "snapshot.json", private=True)
    metadata = read_json(root / "snapshot.json")
    if type(metadata.get("format_version")) is not int or metadata["format_version"] != 2:
        refuse("snapshot_format_unsupported", "Only snapshot format 2 with explicit ownership metadata is supported; older snapshots are refused")
    actual = inventory(root)
    actual.pop("snapshot.json", None)
    if actual != metadata.get("files"):
        refuse("snapshot_invalid", "Snapshot file inventory does not match")
    release = release_path(paths, metadata["release_build_id"])
    if str(release) != metadata["release_path"]:
        refuse("snapshot_release_invalid", "Snapshot release path does not match identity")
    manifest = verify_release(release, installed=True)
    if manifest["build_id"] != metadata["release_build_id"]:
        refuse("snapshot_release_invalid", "Snapshot code identity differs from its release path")
    applied = schema(root / "localplane.db")
    if applied != metadata["schema"]:
        refuse("snapshot_schema_invalid", "Snapshot schema does not match metadata")
    compatible(applied, manifest, exact=True)
    required = {"localplane.db", "localplane-master.secret", "config/environment"}
    if not required.issubset(actual):
        refuse("snapshot_incomplete", "Snapshot lacks database, credential or configuration")
    env, _ = configuration(paths, body=(root / "config/environment").read_bytes())
    if env["LOCALPLANE_RELEASE_MANIFEST"] != str(release / "release.json"):
        refuse("snapshot_configuration_mismatch", "Saved configuration does not select snapshot release")
    for key in ("LOCALPLANE_TLS_CERT_PATH", "LOCALPLANE_TLS_KEY_PATH"):
        relative = Path(env[key]).relative_to(paths["etc"])
        regular(root / "config" / relative, private=key.endswith("KEY_PATH"))
    validate_secret(root / "localplane-master.secret")
    for key in ("database_metadata", "secret_metadata"):
        entry = metadata.get(key)
        uid = int(os.environ.get("LOCALPLANE_SERVICE_UID", str(os.geteuid())))
        if (not isinstance(entry, list) or len(entry) != 3 or any(type(v) is not int for v in entry)
                or entry[0] != uid or entry[1] < 0 or entry[2] & ~0o777 or entry[2] & 0o022
                or (key == "secret_metadata" and entry[2] & 0o077)):
            refuse("snapshot_metadata_invalid", "Snapshot data/credential ownership metadata is missing or unsafe")
    config_names = {".", *(p.relative_to(root / "config").as_posix()
                            for p in (root / "config").rglob("*"))}
    metadata_owners = metadata.get("config_metadata", {})
    if set(metadata_owners) != config_names:
        refuse("snapshot_metadata_invalid", "Configuration metadata inventory does not match")
    for name, entry in metadata_owners.items():
        if (not isinstance(entry, list) or len(entry) != 3
                or any(type(value) is not int for value in entry)
                or entry[0] not in trusted_uids() or entry[1] < 0
                or entry[2] & ~0o777 or entry[2] & 0o022):
            refuse("snapshot_metadata_invalid", "Unsafe restored configuration metadata")
    return root, metadata


def same_filesystem(path: Path, parent: Path) -> None:
    """Refuse mount points/file mounts before any active replacement."""
    safe_path(path)
    safe_path(parent)
    if path.exists() and path.stat().st_dev != parent.stat().st_dev:
        refuse("restore_cross_filesystem", "Restore transitions require the destination and its parent on one filesystem")


def file_metadata(path: Path) -> list[int]:
    info = regular(path)
    return [info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)]


def apply_config_metadata(root: Path, metadata: dict[str, list[int]]) -> None:
    for name, (uid, gid, mode) in metadata.items():
        path = root / name
        info = path.lstat()
        if (info.st_uid, info.st_gid) != (uid, gid):
            os.chown(path, uid, gid)
        path.chmod(mode)
        if path.is_file():
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
    for path in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        sync_dir(path)
    sync_dir(root)


def check_config_metadata(root: Path, metadata: dict[str, list[int]]) -> None:
    names = {".", *(p.relative_to(root).as_posix() for p in root.rglob("*"))}
    if names != set(metadata):
        refuse("restore_config_metadata", "Restored configuration entry inventory differs")
    for name, expected in metadata.items():
        info = (root / name).lstat()
        if [info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)] != expected:
            refuse("restore_config_metadata", "Restored configuration ownership or mode differs")


def write_restore_journal(path: Path, journal: dict[str, Any], phase: str) -> None:
    """A small progress receipt, not authority to clear the external pending marker."""
    journal["phase"] = phase
    write_json(path, journal)


def restore_files(paths: dict[str, Path], backup: Path, snapshot: dict[str, Any],
                  snapshot_id: str, transaction: Any) -> dict[str, Any]:
    """Explicit matching manual resume; original evidence is never overwritten.

    The controller must already have durably marked pending and hold both offline
    locks. No phase here releases that guard. Only complete(), after full checking,
    makes startup eligible again; the normal maintenance window stays active.
    """
    window = transaction.window
    transaction_id = transaction.transaction_id
    if not isinstance(transaction_id, str) or not transaction_id:
        refuse("restore_transaction_invalid", "A stable external restore transaction identifier is required")
    if window.window_id != snapshot["window_id"] or window.generation != snapshot["generation"]:
        refuse("restore_transaction_mismatch", "Restore transaction does not match snapshot binding")
    token = hashlib.sha256(transaction_id.encode()).hexdigest()
    displaced = paths["var"] / "displaced"
    protected_directory(displaced)
    displaced.mkdir(mode=0o700, exist_ok=True)
    retained = displaced / ("restore_" + token)
    protected_directory(retained)
    journal_file = retained / "journal.json"
    original = retained / "original"
    config_stage = paths["etc"].parent / ("." + paths["etc"].name + ".restore_" + token)
    config_old = paths["etc"].parent / ("." + paths["etc"].name + ".displaced_" + token)
    db = paths["var"] / "localplane.db"
    secret = paths["var"] / "localplane-master.secret"
    binding = {"format_version": 1, "transaction_id": transaction_id, "snapshot_id": snapshot_id,
               "window_id": window.window_id, "generation": window.generation,
               "snapshot_sha256": sha(backup / "snapshot.json")}
    if retained.exists():
        if not journal_file.exists():
            refuse("restore_retention_incomplete", "Interrupted retention needs explicit investigation; originals will not be overwritten")
        journal = read_json(journal_file)
        if any(journal.get(key) != value for key, value in binding.items()):
            refuse("restore_journal_mismatch", "Retained transaction binding differs; refusing manual resume")
    else:
        # Missing current paths are only resumable with an existing validated journal.
        db_metadata = file_metadata(db)
        secret_metadata = file_metadata(secret)
        if not paths["etc"].is_dir():
            refuse("restore_current_config_missing", "Cannot retain original configuration")
        retained.mkdir(mode=0o700)
        retain_displaced(paths, original, window)
        journal = {**binding, "db_metadata": snapshot["database_metadata"],
                   "secret_metadata": snapshot["secret_metadata"],
                   "displaced_db_metadata": db_metadata, "displaced_secret_metadata": secret_metadata}
        write_restore_journal(journal_file, journal, "retained")
    evidence = read_json(original / "displaced.json")
    original_files = inventory(original)
    original_files.pop("displaced.json", None)
    if (evidence.get("window_id") != window.window_id or evidence.get("generation") != window.generation
            or original_files != evidence.get("files")):
        refuse("restore_retained_evidence_changed", "Original displaced evidence changed; manual resume refused")
    for key in ("db_metadata", "secret_metadata"):
        entry = journal.get(key)
        if (not isinstance(entry, list) or len(entry) != 3 or any(type(v) is not int for v in entry)
                or entry[0] not in trusted_uids() or entry[1] < 0 or entry[2] & ~0o777 or entry[2] & 0o022):
            refuse("restore_journal_metadata", "Unsafe retained ownership metadata")
    # Every rename source/destination has the SAME parent filesystem. A separate
    # /etc and data filesystem is supported: evidence retention uses copies only.
    for path, parent in ((paths["etc"], paths["etc"].parent), (config_stage, paths["etc"].parent),
                         (config_old, paths["etc"].parent), (db, db.parent), (secret, secret.parent)):
        same_filesystem(path, parent)
    config_hashes = inventory(backup / "config")
    if config_stage.exists():
        if inventory(config_stage) != config_hashes:
            refuse("restore_stage_incomplete", "Incomplete configuration stage retained; refusing to overwrite it")
    else:
        copy_tree(backup / "config", config_stage)
    apply_config_metadata(config_stage, snapshot["config_metadata"])
    check_config_metadata(config_stage, snapshot["config_metadata"])
    if inventory(config_stage) != config_hashes:
        refuse("restore_stage_invalid", "Complete staged configuration does not match snapshot")
    staged_db = db.parent / (".localplane.db.restore_" + token)
    staged_secret = secret.parent / (".localplane-master.secret.restore_" + token)
    for source, target, metadata in ((backup / "localplane.db", staged_db, journal["db_metadata"]),
                                     (backup / "localplane-master.secret", staged_secret, journal["secret_metadata"])):
        uid, gid, mode = metadata
        atomic_copy(source, target, mode=mode, owner=(uid, gid))
        if file_metadata(target) != metadata:
            refuse("restore_stage_metadata", "Staged data ownership or mode differs")
    if schema(staged_db) != snapshot["schema"]:
        refuse("restore_stage_schema", "Staged database schema differs")
    validate_secret(staged_secret)
    if inventory(backup) != {**snapshot["files"], "snapshot.json": binding["snapshot_sha256"]}:
        refuse("snapshot_changed", "Snapshot changed before active restore writes")
    if config_old.exists():
        if inventory(config_old) != inventory(original / "config"):
            refuse("restore_displaced_config_changed", "Original configuration changed; refusing to overwrite evidence")
        if paths["etc"].exists() and inventory(paths["etc"]) != config_hashes:
            refuse("restore_active_config_changed", "Unexpected active configuration during manual resume")
    elif not paths["etc"].exists() or inventory(paths["etc"]) != inventory(original / "config"):
        refuse("restore_original_config_changed", "Original configuration is missing or changed without a recorded displacement")
    write_restore_journal(journal_file, journal, "prepared")
    # Pending is persistent BEFORE this first active change. All original data,
    # config and credential evidence has been copied, verified and fsynced above.
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            saved = original / ("raw-localplane.db" + suffix)
            if not saved.exists() or sha(sidecar) != sha(saved):
                refuse("restore_sidecar_changed", "Unexpected current SQLite sidecar; refusing to discard it")
            sidecar.unlink()
    sync_dir(db.parent)
    os.replace(staged_db, db)
    sync_dir(db.parent)
    write_restore_journal(journal_file, journal, "database_replaced")
    os.replace(staged_secret, secret)
    sync_dir(secret.parent)
    write_restore_journal(journal_file, journal, "credential_replaced")
    if not config_old.exists():
        os.rename(paths["etc"], config_old)
        sync_dir(paths["etc"].parent)
        write_restore_journal(journal_file, journal, "config_displaced")
    if not paths["etc"].exists():
        os.replace(config_stage, paths["etc"])
        sync_dir(paths["etc"].parent)
    write_restore_journal(journal_file, journal, "config_replaced")
    if inventory(paths["etc"]) != config_hashes:
        refuse("restore_config_invalid", "Active restored configuration differs from snapshot")
    check_config_metadata(paths["etc"], snapshot["config_metadata"])
    for source, target, metadata in ((backup / "localplane.db", db, journal["db_metadata"]),
                                     (backup / "localplane-master.secret", secret, journal["secret_metadata"])):
        if sha(source) != sha(target) or file_metadata(target) != metadata:
            refuse("restore_data_invalid", "Restored data or credential bytes/metadata differ")
    env, _ = configuration(paths)
    if env["LOCALPLANE_RELEASE_MANIFEST"] != str(Path(snapshot["release_path"]) / "release.json"):
        refuse("restore_release_invalid", "Restored configuration does not select matching code")
    compatible(schema(db), verify_release(Path(snapshot["release_path"]), installed=True), exact=True)
    validate_secret(secret)
    verify_state_files(paths)
    sync_dir(db.parent)
    sync_dir(paths["etc"])
    write_restore_journal(journal_file, journal, "validated")
    transaction.complete()  # Only the controller can clear its pending marker.
    return {"restored_snapshot": snapshot_id, "displaced": str(retained),
            "displaced_config": str(config_old), "transaction_id": transaction_id,
            "window_id": window.window_id, "services": "not started; install exact units and verify before accept"}


def activate(paths: dict[str, Path], release: Path) -> None:
    """Select exact code/console; unit replacement/start remains an administrator step."""
    _, body = configuration(paths)
    values = {"LOCALPLANE_RELEASE_MANIFEST": str(release / "release.json"),
              "LOCALPLANE_CONSOLE_DIR": str(release / "console")}
    lines = []
    for line in body.decode().splitlines():
        key = line.partition("=")[0]
        lines.append(key + "=" + shlex.quote(values[key]) if key in values else line)
    info = regular(paths["etc"] / "environment")
    atomic_bytes(paths["etc"] / "environment", ("\n".join(lines) + "\n").encode(),
                 mode=stat.S_IMODE(info.st_mode), owner=(info.st_uid, info.st_gid))


def stage(paths: dict[str, Path], bundle: Path) -> Path:
    manifest = verify_release(bundle)
    target = release_path(paths, manifest["build_id"])
    safe_path(target)
    if target.exists():
        if verify_release(target, installed=True) != manifest:
            refuse("release_drift", "Existing immutable release differs")
        return target
    if os.geteuid() != 0:
        refuse("administrator_required", "Installing immutable root-owned code requires an explicit administrator invocation")
    for path in [paths["root"], *paths["root"].parents]:
        if path.exists() and path.stat().st_uid != 0:
            refuse("release_parent_not_root_owned", "Installed release ancestors must be root-owned")
    mkdir_parents(target.parent, 0o755)
    temp = target.parent / (".stage_" + secrets.token_hex(16))
    copy_tree(bundle, temp)
    # Only newly created code paths; never chmod existing installed state.
    for path in [temp, *temp.rglob("*")]:
        mode = 0o755 if path.is_dir() or path.relative_to(temp).parts[0] == "bin" else 0o644
        path.chmod(mode)
    verify_release(temp, installed=True)
    os.rename(temp, target)
    sync_dir(target.parent)
    return target


def run_status(paths: dict[str, Path]) -> dict[str, Any]:
    ctl = controller(paths)
    result = {"release_build_id": None, "source_commit": None, "source_dirty": None,
              "source_digest": None, "maintenance": ctl.state().as_dict(),
              "configuration": {"status": "unavailable", "reason": "configuration_unavailable"},
              "identity": {"status": "unknown", "reason": "configuration_unavailable"},
              "runtime": "unknown; offline inspection does not establish running identity"}
    try:
        env, _ = configuration(paths)
    except (OSError, ValueError, UnicodeError, MaintenanceError):
        # A pending restore can legitimately have no active config directory.
        # Return the external evidence first; never initialize/open store/auth.
        return result
    result["configuration"] = {"status": "available", "reason": None}
    root = Path(env["LOCALPLANE_RELEASE_MANIFEST"]).parent
    try:
        manifest = verify_release(root, installed=True)
    except (OSError, ValueError, UnicodeError, MaintenanceError):
        result["identity"] = {"status": "unavailable", "reason": "release_inspection_unavailable"}
        return result
    result.update(release_build_id=manifest["build_id"], source_commit=manifest["source_commit"],
                  source_dirty=manifest["source_dirty"], source_digest=manifest["source_digest"],
                  identity={"status": "inspected", "reason": None})
    return result


def plan(action: str) -> str:
    return (f"Action: {action}\n"
            "Review exact paths, identity and administrator commands before system changes.\n"
            "Open the external window; separately stop backend and agent. Offline actions require both lifetime locks released.\n"
            "Backup uses SQLite backup API, integrity/FK/schema checks and matching config/TLS/credential.\n"
            "Update/revert selects an exact immutable release in the non-secret environment, never a current symlink.\n"
            "Install exact release unit templates separately; no service/unit/package commands are executed here.\n"
            "Start agent then backend separately, verify authenticated diagnostics and reauthenticate after restart.\n"
            "Accept explicitly closes the exact window; older restore is then refused.\n"
            "Failures retain fence and displaced evidence. No automatic retry, action replay, hold release or approval renewal.")


def empty_installation(paths: dict[str, Path]) -> bool:
    return ({p.name for p in paths["var"].iterdir()} == {"maintenance"}
            and not any((paths["var"] / "maintenance").iterdir())
            and not any(paths["etc"].iterdir())
            and (not paths["run"].exists() or not any(paths["run"].iterdir())))


def execute(args: argparse.Namespace, paths: dict[str, Path]) -> dict[str, Any]:
    if args.command == "plan" or args.dry_run:
        return {"plan": plan(getattr(args, "action", args.command))}
    if args.command in {"check", "status"}:
        return run_status(paths)  # Never opens DB/secret or creates a lock.
    ctl = controller(paths)
    if args.command == "install" and empty_installation(paths):
        # Bootstrap code only, before genesis. Do not create a CLI lock here:
        # initialize requires a truly empty maintenance root. No state/config/auth
        # is written; staging still enforces immutable root-owned code.
        release = stage(paths, Path(args.bundle))
        return {"installed_release": str(release), "services": "not started",
                "maintenance": "not initialized; initialize as the service identity before any application state"}
    if args.command == "initialize":
        # No CLI lock before genesis: initialize refuses ANY preexisting lock/file.
        # This is intentionally not an idempotent repair or missing-witness reset.
        uid = int(os.environ.get("LOCALPLANE_SERVICE_UID", str(os.geteuid())))
        if os.geteuid() != uid:
            refuse("initialization_identity", "Initialize as the dedicated service identity")
        if not empty_installation(paths):
            refuse("installation_not_empty", "Initialize only before application/store/auth/config/lock state exists")
        return ctl.initialize().as_dict()
    with command_lock(paths):
        if args.command != "restore":
            state = ctl.state()
            if state.status == "unknown" or state.refusal is not None:
                refuse("maintenance_unresolved", "Maintenance is unknown or pending/unresolved; transitions refused")
        if args.command == "open":
            _, current = current_release(paths)
            return ctl.open_window(release_build_id=current["build_id"], reason="operator maintenance").as_dict()
        if args.command == "install":
            release = stage(paths, Path(args.bundle))
            if (paths["etc"] / "environment").exists():
                current, _ = current_release(paths)
                if current != release:
                    refuse("install_drift", "Existing installation selects another release; use update")
            return {"installed_release": str(release), "services": "not started",
                    "configuration": "administrator-owned; not overwritten"}
        if args.command == "accept":
            if not args.verified:
                refuse("verification_required", "Explicit --verified acknowledgement of authenticated runtime/schema/access checks is required")
            current_release(paths)
            return ctl.close_window(args.window, accepted=True, outcome="operator verified").as_dict()
        if args.command == "backup":
            name = "snapshot_" + secrets.token_hex(16)
            target = snapshot_path(paths, name)
            protected_directory(target.parent)
            target.parent.mkdir(mode=0o700, exist_ok=True)
            with ctl.snapshot_lease(args.window) as window:
                capture(paths, target, window)
            # The controller's transition lock is deliberately non-reentrant.
            recorded = ctl.record_snapshot(args.window, name)
            if recorded.window_id != args.window or recorded.snapshot_id != name:
                refuse("snapshot_record_mismatch", "Controller did not record this exact snapshot")
            return {"snapshot": name, "window_id": args.window}
        if args.command in {"update", "revert"}:
            with ctl.snapshot_lease(args.window):
                current_release(paths)
                _, snapshot = verify_snapshot(paths, args.snapshot)
                active = ctl.state().window
                if not active or snapshot["window_id"] != args.window or snapshot["generation"] != active.generation:
                    refuse("snapshot_window_mismatch", "A verified snapshot from this active window is required")
                target = stage(paths, Path(args.bundle)) if args.command == "update" else release_path(paths, args.build_id)
                manifest = verify_release(target, installed=True)
                if release_path(paths, manifest["build_id"]) != target:
                    refuse("release_path_identity_mismatch", "Target path must match release identity")
                compatible(schema(paths["var"] / "localplane.db"), manifest)
                saved = paths["var"] / "displaced"
                protected_directory(saved)
                saved.mkdir(mode=0o700, exist_ok=True)
                copy_tree(paths["etc"], saved / ("activation_" + secrets.token_hex(16)))
                activate(paths, target)
                return {"selected_release": str(target), "window_id": args.window,
                        "services": "not started; install exact units and verify before accept"}
        if args.command == "restore":
            backup, snapshot = verify_snapshot(paths, args.snapshot)
            if snapshot["window_id"] != args.window:
                refuse("snapshot_window_mismatch", "Snapshot belongs to another window")
            restore_transaction = getattr(ctl, "restore_transaction", None)
            if not callable(restore_transaction):
                refuse("restore_transaction_unavailable", "Safe restore requires the external interrupted-restore guard")
            with restore_transaction(snapshot, snapshot_id=args.snapshot) as transaction:
                return restore_files(paths, backup, snapshot, args.snapshot, transaction)
    refuse("unknown_command", "Unknown maintenance command")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="localplane-maintenance", description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("check", "status", "initialize", "open"):
        sub.add_parser(command)
    sub.add_parser("plan").add_argument("action", choices=["initialize", "install", "open", "backup", "update", "revert", "restore", "accept"])
    sub.add_parser("install").add_argument("bundle")
    for command in ("backup", "update", "revert", "restore", "accept"):
        p = sub.add_parser(command)
        p.add_argument("--window", required=True, help="Exact active external window identifier")
        if command == "update":
            p.add_argument("bundle")
        if command == "revert":
            p.add_argument("build_id")
        if command in {"update", "revert", "restore"}:
            p.add_argument("--snapshot", required=True)
        if command == "accept":
            p.add_argument("--verified", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        print(json.dumps(execute(args, effective_paths()), sort_keys=True, indent=2))
        return 0
    except Exception as exc:
        # SQLite/config/OS exceptions may embed sensitive values. Never dump them.
        code = getattr(exc, "code", "maintenance_failed")
        message = str(exc) if isinstance(exc, MaintenanceError) else "Maintenance refused; preserve fence and inspect protected local state"
        print(json.dumps({"error": {"code": code, "message": message}}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
