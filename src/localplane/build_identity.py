"""Release identity: what build is actually running, and whether its manifest holds.

**Identity is evidence, not a version string.** ``0.1.0`` says which product line this is;
it does not say which source, which migrations, which compiled console or which dependency
set is loaded. A release manifest fixes all of those as content hashes, and this module
reads that manifest without trusting it: the manifest's own digest is recomputed, its file
paths are constrained, and the files it names are hashed again from the release directory
before the identity is reported.

**Development is not a fake release.** With no manifest configured the answer is
``unbundled`` and every release field is null. Nothing here invents a build id from the
current working tree, presents a dirty base commit as an exact source, or substitutes the
package version for the manifest.

**The manifest is not a caller-supplied path.** The runtime reads the absolute path named by
``LOCALPLANE_RELEASE_MANIFEST`` (or an explicitly constructed test path). No API parameter
reaches this module, and no endpoint may ask it to read a different file.

Standard library only. The agent must be able to state its own identity without importing
FastAPI or the backend.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import importlib.util
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

FORMAT_VERSION = 1
MANIFEST_FILENAME = "release.json"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_RELEASE_FILE_BYTES = 64 * 1024 * 1024
MAX_RELEASE_FILES = 8192
MAX_DEPENDENCIES = 512

REQUIRED_KEYS = frozenset(
    {
        "format_version",
        "build_id",
        "version",
        "source_commit",
        "source_tree",
        "source_dirty",
        "source_digest",
        "files",
        "migrations",
        "dependencies",
        "platform",
        "console_digest",
    }
)

PACKAGED = "packaged"
INSPECTED = "inspected"
UNBUNDLED = "unbundled"
INVALID = "invalid"
UNAVAILABLE = "unavailable"


class BuildIdentityError(RuntimeError):
    """A configured manifest is unusable and the process must not serve as if it were."""


@dataclass(frozen=True)
class DependencyPin:
    name: str
    version: str
    sha256: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "sha256": self.sha256}


@dataclass(frozen=True)
class PlatformIdentity:
    os: str | None
    architecture: str | None
    python: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "os": self.os,
            "architecture": self.architecture,
            "python": self.python,
        }


@dataclass(frozen=True)
class BuildIdentity:
    """One process's answer about the build it actually loaded."""

    status: str
    process: str
    build_id: str | None = None
    version: str | None = None
    source_commit: str | None = None
    source_tree: str | None = None
    source_dirty: bool | None = None
    source_digest: str | None = None
    manifest_sha256: str | None = None
    console_digest: str | None = None
    files: Mapping[str, str] = field(default_factory=dict)
    migrations: Mapping[str, str] = field(default_factory=dict)
    dependencies: tuple[DependencyPin, ...] = ()
    platform: PlatformIdentity | None = None
    reasons: tuple[str, ...] = ()
    loaded_at: str = ""

    @property
    def packaged(self) -> bool:
        return self.status == PACKAGED

    @property
    def valid(self) -> bool:
        return self.status in {PACKAGED, INSPECTED, UNBUNDLED}

    def as_dict(self) -> dict[str, Any]:
        """The public, bounded identity view. Never includes an absolute path."""
        return {
            "status": self.status,
            "process": self.process,
            "build_id": self.build_id,
            "version": self.version,
            "source_commit": self.source_commit,
            "source_tree": self.source_tree,
            "source_dirty": self.source_dirty,
            "source_digest": self.source_digest,
            "manifest_sha256": self.manifest_sha256,
            "console_digest": self.console_digest,
            "file_count": len(self.files) if self.files else None,
            "migration_count": len(self.migrations) if self.migrations else None,
            "dependencies": [pin.as_dict() for pin in self.dependencies],
            "platform": self.platform.as_dict() if self.platform is not None else None,
            "reasons": list(self.reasons),
            "loaded_at": self.loaded_at,
        }


@dataclass(frozen=True)
class ConsoleIdentity:
    """The digest of the asset snapshot the backend actually loaded at startup."""

    status: str
    actual_digest: str | None
    asset_count: int | None
    loaded_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "actual_digest": self.actual_digest,
            "asset_count": self.asset_count,
            "loaded_at": self.loaded_at,
        }


# ------------------------------------------------------------------------------- loading


def load_build_identity(
    manifest_path: str | Path | None,
    *,
    process: str,
    release_root: str | Path | None = None,
    verify_files: bool = True,
    inspect_only: bool = False,
) -> BuildIdentity:
    """Read and verify one release manifest.

    ``manifest_path`` is the absolute path of ``release.json``. ``release_root`` defaults to
    the manifest's parent; the relative paths in ``files`` and ``migrations`` are resolved
    from it. A path that is relative, a symlink, a non-regular file, a world/group-writable
    manifest, an unknown field, a malformed hash or a file that no longer matches all yield
    an ``invalid`` identity rather than an exception: the caller decides whether that is
    fatal for its process.
    """
    now = _now()
    if manifest_path is None or str(manifest_path) == "":
        return BuildIdentity(
            status=UNBUNDLED,
            process=process,
            reasons=("release_manifest_not_configured",),
            loaded_at=now,
        )
    path = Path(manifest_path)
    if not path.is_absolute():
        return _invalid(process, now, "release_manifest_path_must_be_absolute")
    try:
        raw, manifest_sha256 = _read_manifest(path)
    except _ManifestProblem as problem:
        return _invalid(process, now, problem.code, detail=problem.detail)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _invalid(process, now, "release_manifest_not_json")
    if not isinstance(value, dict):
        return _invalid(process, now, "release_manifest_not_an_object")
    try:
        manifest = _validate_manifest(value)
    except _ManifestProblem as problem:
        return _invalid(process, now, problem.code, detail=problem.detail)

    expected_build_id = _build_id(manifest)
    if manifest["build_id"] != expected_build_id:
        return _invalid(
            process,
            now,
            "release_manifest_build_id_mismatch",
            detail={"declared": manifest["build_id"], "computed": expected_build_id},
        )
    expected_source_digest = _source_digest(
        manifest["files"], manifest["migrations"], manifest["console_digest"]
    )
    if manifest["source_digest"] != expected_source_digest:
        return _invalid(
            process,
            now,
            "release_manifest_source_digest_mismatch",
            detail={
                "declared": manifest["source_digest"],
                "computed": expected_source_digest,
            },
        )

    root = Path(release_root) if release_root is not None else path.parent
    if not root.is_absolute():
        return _invalid(process, now, "release_root_must_be_absolute")
    if not verify_files:
        return _invalid(process, now, "release_file_verification_required")
    if not inspect_only:
        problem = _runtime_binding_problem(root, manifest["files"])
        if problem is not None:
            return _invalid(process, now, problem)
    if verify_files:
        mismatches = _verify_release_files(root, manifest)
        if mismatches:
            return _invalid(
                process,
                now,
                "release_files_do_not_match_manifest",
                detail={"mismatches": mismatches[:32], "count": len(mismatches)},
            )

    platform_value = manifest["platform"]
    dependencies = tuple(
        DependencyPin(
            name=item["name"],
            version=item["version"],
            sha256=item.get("sha256"),
        )
        for item in manifest["dependencies"]
    )
    return BuildIdentity(
        status=INSPECTED if inspect_only else PACKAGED,
        process=process,
        build_id=manifest["build_id"],
        version=manifest["version"],
        source_commit=manifest["source_commit"],
        source_tree=manifest["source_tree"],
        source_dirty=manifest["source_dirty"],
        source_digest=manifest["source_digest"],
        manifest_sha256=f"sha256:{manifest_sha256}",
        console_digest=manifest["console_digest"],
        files=dict(manifest["files"]),
        migrations=dict(manifest["migrations"]),
        dependencies=dependencies,
        platform=PlatformIdentity(
            os=platform_value["os"],
            architecture=platform_value["architecture"],
            python=platform_value["python"],
        ),
        reasons=(),
        loaded_at=now,
    )


def require_usable(identity: BuildIdentity) -> None:
    """Raise for a configured-but-invalid manifest; unbundled is allowed."""
    if identity.status == INVALID:
        message = ",".join(identity.reasons) or "release_manifest_invalid"
        raise BuildIdentityError(message)


# ----------------------------------------------------------------------- console digest


def console_files_digest(index: bytes, assets: Mapping[str, bytes]) -> str:
    """Canonical digest of exactly the files a console snapshot makes public.

    Keys are relative to the build root, without a leading slash: ``index.html`` and
    ``assets/<fingerprinted-name>``. The same function is used by the packaging builder and
    by the running backend, so a mismatch means different bytes rather than a different
    convention.
    """
    files = {"index.html": _sha256_bytes(index)}
    for name, body in assets.items():
        relative = name[1:] if name.startswith("/") else name
        files[relative] = _sha256_bytes(body)
    return "sha256:" + _digest_value(files)


def console_identity_from_loaded(
    index: bytes, assets: Mapping[str, bytes]
) -> ConsoleIdentity:
    return ConsoleIdentity(
        status="loaded",
        actual_digest=console_files_digest(index, assets),
        asset_count=len(assets),
        loaded_at=_now(),
    )


def unavailable_console() -> ConsoleIdentity:
    return ConsoleIdentity(
        status=UNAVAILABLE,
        actual_digest=None,
        asset_count=None,
        loaded_at=_now(),
    )


# -------------------------------------------------------------------------- canonicals


def canonical_digest(value: Any) -> str:
    """``sha256:<hex>`` over canonical JSON. The builder uses this for manifest fields."""
    return "sha256:" + _digest_value(value)


def migration_filename(version: int, name: str) -> str:
    return f"{version:04d}_{name}.sql"


# --------------------------------------------------------------------------- internals


class _ManifestProblem(Exception):
    def __init__(self, code: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail or {}


def _invalid(
    process: str, now: str, reason: str, *, detail: dict[str, Any] | None = None
) -> BuildIdentity:
    reasons = (reason,) if not detail else (reason,)
    return BuildIdentity(
        status=INVALID,
        process=process,
        reasons=reasons,
        loaded_at=now,
    )


def _read_manifest(path: Path) -> tuple[bytes, str]:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise _ManifestProblem("release_manifest_missing") from None
    except OSError as exc:
        raise _ManifestProblem(
            "release_manifest_unreadable", {"errno": exc.errno}
        ) from None
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise _ManifestProblem("release_manifest_not_a_regular_file")
    if info.st_nlink != 1:
        raise _ManifestProblem("release_manifest_has_multiple_links")
    if info.st_uid not in {0, os.geteuid()}:
        raise _ManifestProblem("release_manifest_owner_is_untrusted")
    if info.st_mode & 0o022:
        raise _ManifestProblem("release_manifest_is_group_or_world_writable")
    if info.st_size > MAX_MANIFEST_BYTES:
        raise _ManifestProblem("release_manifest_too_large")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise _ManifestProblem(
            "release_manifest_unreadable", {"errno": exc.errno}
        ) from None
    try:
        data = _read_bounded(fd, MAX_MANIFEST_BYTES)
    finally:
        os.close(fd)
    return data, _sha256_bytes(data)


def _validate_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(value) - REQUIRED_KEYS)
    missing = sorted(REQUIRED_KEYS - set(value))
    if unknown:
        raise _ManifestProblem(
            "release_manifest_unknown_fields", {"unknown": unknown}
        )
    if missing:
        raise _ManifestProblem(
            "release_manifest_missing_fields", {"missing": missing}
        )
    if value.get("format_version") != FORMAT_VERSION:
        raise _ManifestProblem("release_manifest_format_version_unsupported")
    if not isinstance(value.get("version"), str) or not value["version"]:
        raise _ManifestProblem("release_manifest_version_invalid")
    _require_commit(value.get("source_commit"), "source_commit")
    tree = value.get("source_tree")
    if tree is not None:
        _require_commit(tree, "source_tree")
    dirty = value.get("source_dirty")
    if not isinstance(dirty, bool):
        raise _ManifestProblem("release_manifest_source_dirty_invalid")
    if dirty and tree is not None:
        raise _ManifestProblem(
            "release_manifest_dirty_tree_marked_exact",
            {"source_tree": tree},
        )
    _require_sha256_text(value.get("build_id"), "build_id")
    _require_sha256_text(value.get("source_digest"), "source_digest")
    _require_sha256_text(value.get("console_digest"), "console_digest")

    files = _validate_hash_map(value.get("files"), "files", max_entries=MAX_RELEASE_FILES)
    migrations = _validate_hash_map(
        value.get("migrations"), "migrations", max_entries=MAX_RELEASE_FILES
    )
    for name in migrations:
        if not name.endswith(".sql") or "/" in name:
            raise _ManifestProblem(
                "release_manifest_migration_name_invalid", {"name": name}
            )
        if files.get("src/localplane/backend/db/migrations/" + name) != migrations[name]:
            raise _ManifestProblem("release_manifest_migration_file_mismatch", {"name": name})

    dependencies = value.get("dependencies")
    if not isinstance(dependencies, list) or len(dependencies) > MAX_DEPENDENCIES:
        raise _ManifestProblem("release_manifest_dependencies_invalid")
    normalized_dependencies: list[dict[str, Any]] = []
    for item in dependencies:
        if not isinstance(item, dict) or set(item) - {"name", "version", "sha256"}:
            raise _ManifestProblem("release_manifest_dependency_entry_invalid")
        name, version, digest = item.get("name"), item.get("version"), item.get("sha256")
        if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
            raise _ManifestProblem("release_manifest_dependency_entry_invalid")
        if digest is not None:
            _require_sha256_hex(digest, "dependency.sha256")
        normalized_dependencies.append(
            {"name": name, "version": version, "sha256": digest}
        )

    platform = value.get("platform")
    if not isinstance(platform, dict) or set(platform) != {"os", "architecture", "python"}:
        raise _ManifestProblem("release_manifest_platform_invalid")
    for name in ("os", "architecture", "python"):
        if not isinstance(platform.get(name), str) or not platform[name]:
            raise _ManifestProblem(
                "release_manifest_platform_invalid", {"field": name}
            )

    return {
        "format_version": FORMAT_VERSION,
        "build_id": value["build_id"],
        "version": value["version"],
        "source_commit": value["source_commit"],
        "source_tree": tree,
        "source_dirty": dirty,
        "source_digest": value["source_digest"],
        "files": files,
        "migrations": migrations,
        "dependencies": normalized_dependencies,
        "platform": {
            "os": platform["os"],
            "architecture": platform["architecture"],
            "python": platform["python"],
        },
        "console_digest": value["console_digest"],
    }


def _validate_hash_map(
    value: Any, name: str, *, max_entries: int
) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > max_entries:
        raise _ManifestProblem(f"release_manifest_{name}_invalid")
    result: dict[str, str] = {}
    for key, digest in value.items():
        if not isinstance(key, str) or not key:
            raise _ManifestProblem(f"release_manifest_{name}_path_invalid")
        _validate_relative_path(key)
        _require_sha256_hex(digest, f"{name}.{key}")
        result[key] = digest
    return result


def _validate_relative_path(key: str) -> None:
    pure = PurePosixPath(key)
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
        or "\\" in key
        or str(pure) != key
    ):
        raise _ManifestProblem(
            "release_manifest_path_invalid", {"path": key}
        )


def _require_commit(value: Any, name: str) -> None:
    if not isinstance(value, str) or len(value) not in {40, 64}:
        raise _ManifestProblem(
            "release_manifest_commit_invalid", {"field": name}
        )
    try:
        int(value, 16)
    except ValueError:
        raise _ManifestProblem(
            "release_manifest_commit_invalid", {"field": name}
        ) from None


def _require_sha256_text(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise _ManifestProblem(
            "release_manifest_digest_invalid", {"field": name}
        )
    _require_sha256_hex(value[7:], name)


def _require_sha256_hex(value: Any, name: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise _ManifestProblem(
            "release_manifest_digest_invalid", {"field": name}
        )
    try:
        int(value, 16)
    except ValueError:
        raise _ManifestProblem(
            "release_manifest_digest_invalid", {"field": name}
        ) from None


def _build_id(manifest: Mapping[str, Any]) -> str:
    payload = {
        key: manifest[key]
        for key in REQUIRED_KEYS
        if key != "build_id"
    }
    return "sha256:" + _digest_value(payload)


def _source_digest(
    files: Mapping[str, str],
    migrations: Mapping[str, str],
    console_digest: str,
) -> str:
    return "sha256:" + _digest_value(
        {
            "files": dict(files),
            "migrations": dict(migrations),
            "console_digest": console_digest,
        }
    )


def _verify_release_files(
    root: Path, manifest: Mapping[str, Any]
) -> list[str]:
    problems: list[str] = []
    for name, expected in manifest["files"].items():
        try:
            actual = _hash_relative_file(root, name)
        except FileNotFoundError:
            problems.append(f"missing:{name}")
            continue
        except _ManifestProblem as problem:
            problems.append(f"{problem.code}:{name}")
            continue
        except OSError as exc:
            problems.append(f"os_error_{exc.errno}:{name}")
            continue
        if actual != expected:
            problems.append(f"hash:{name}")
    return problems


def _runtime_binding_problem(root: Path, files: Mapping[str, str]) -> str | None:
    """Bind the actual imported LocalPlane modules to declared files in this release.

    Staged-bundle inspection explicitly opts out and must not be presented as runtime identity.
    """
    expected_root = root / "src" / "localplane"
    if Path(__file__).absolute() != expected_root / "build_identity.py":
        return "release_runtime_module_root_mismatch"
    for name, module in tuple(sys.modules.items()):
        if name != "localplane" and not name.startswith("localplane."):
            continue
        filename = getattr(module, "__file__", None)
        if not filename:
            return "release_runtime_module_path_unavailable"
        if filename.endswith(".pyc"):
            try:
                filename = importlib.util.source_from_cache(filename)
            except ValueError:
                return "release_runtime_module_path_invalid"
        path = Path(filename).absolute()
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            return "release_runtime_module_root_mismatch"
        if not relative.startswith("src/localplane/") or relative not in files:
            return "release_runtime_module_not_declared"
    return None


def _hash_relative_file(root: Path, key: str) -> str:
    parts = tuple(PurePosixPath(key).parts)
    if not parts:
        raise _ManifestProblem("file_path_invalid")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for part in parts[:-1]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory,
            )
            os.close(directory)
            directory = child
        fd = os.open(
            parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory,
        )
    finally:
        os.close(directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _ManifestProblem("file_not_regular")
        if info.st_size > MAX_RELEASE_FILE_BYTES:
            raise _ManifestProblem("file_too_large")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


def _read_bounded(fd: int, limit: int) -> bytes:
    result = bytearray()
    while len(result) <= limit:
        chunk = os.read(fd, min(1024 * 1024, limit + 1 - len(result)))
        if not chunk:
            break
        result.extend(chunk)
    if len(result) > limit:
        raise _ManifestProblem("release_manifest_too_large")
    return bytes(result)


def _digest_value(value: Any) -> str:
    return _sha256_bytes(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
