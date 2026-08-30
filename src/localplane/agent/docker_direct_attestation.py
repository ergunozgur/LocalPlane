"""Read LocalPlane's one supported Docker runtime-owner deployment contract.

This is deliberately not a generic attestation or configuration framework.  It reads one
fixed file, accepts one closed schema, and describes one topology:
``docker-direct-unix-v1``.  Values from HTTP, the backend database, the agent protocol or
the process environment cannot select the file or override any authority-bearing field.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Final

from localplane.protocol.docker_runtime_owner import DOCKER_DIRECT_UNIX_CONTRACT

DOCKER_DIRECT_ATTESTATION_RELATIVE_PATH: Final = PurePosixPath(
    "etc/localplane/runtime-owner/docker-direct-unix-v1.json"
)
MAX_ATTESTATION_BYTES: Final = 16 * 1024

_SCHEMA: Final[dict[str, str | None]] = {
    "contract": DOCKER_DIRECT_UNIX_CONTRACT,
    "runtime": "docker",
    "engine_transport": "direct_unix",
    "engine_endpoint": None,
    "engine_unit_id": None,
    "engine_process_role": "service_main",
    "engine_scope": "host_system_manager",
    "engine_privilege": "rootful",
}
_DIRECTORY_FLAGS: Final = (
    os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)


class _DuplicateJSONKey(ValueError):
    pass


@dataclass(frozen=True)
class DockerDirectEngineAttestation:
    """The only administrator assertion accepted by the Docker runtime correlator."""

    contract: str
    runtime: str
    engine_transport: str
    engine_endpoint: str
    engine_unit_id: str
    engine_process_role: str
    engine_scope: str
    engine_privilege: str
    fingerprint: str

    def as_dict(self) -> dict[str, str]:
        return {
            "contract": self.contract,
            "runtime": self.runtime,
            "engine_transport": self.engine_transport,
            "engine_endpoint": self.engine_endpoint,
            "engine_unit_id": self.engine_unit_id,
            "engine_process_role": self.engine_process_role,
            "engine_scope": self.engine_scope,
            "engine_privilege": self.engine_privilege,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class DockerDirectAttestationRead:
    status: str
    reason: str
    attestation: DockerDirectEngineAttestation | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def trusted(self) -> bool:
        return self.status == "trusted" and self.attestation is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "attestation": (
                self.attestation.as_dict() if self.attestation is not None else None
            ),
            "detail": dict(self.detail),
        }


class DockerDirectAttestationReader:
    """Securely read the fixed ``docker-direct-unix-v1`` deployment binding.

    ``filesystem_root`` and ``required_uid`` are constructor seams for alternate-root and
    ownership tests.  Production constructs this reader with both defaults, independently
    of LocalPlane's environment-selectable observation root.  Neither value is exposed over
    the protocol, and the path beneath the root is a module constant.
    """

    def __init__(
        self,
        *,
        filesystem_root: str | Path = "/",
        required_uid: int = 0,
    ) -> None:
        self._root = Path(filesystem_root)
        self._required_uid = required_uid

    @property
    def path(self) -> Path:
        return self._root.joinpath(*DOCKER_DIRECT_ATTESTATION_RELATIVE_PATH.parts)

    def read(self) -> DockerDirectAttestationRead:
        try:
            raw, file_stat = self._read_fixed_file()
        except FileNotFoundError as exc:
            return self._result("unavailable", "attestation_unavailable", exc)
        except PermissionError as exc:
            return self._result("unavailable", "attestation_unavailable", exc)
        except (OSError, ValueError) as exc:
            return self._result("untrusted", "attestation_untrusted", exc)

        try:
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJSONKey) as exc:
            return self._result("untrusted", "attestation_untrusted", exc)
        if not isinstance(payload, dict):
            return self._result(
                "untrusted", "attestation_untrusted", ValueError("root must be an object")
            )
        if set(payload) != set(_SCHEMA):
            return DockerDirectAttestationRead(
                status="untrusted",
                reason="attestation_untrusted",
                detail={
                    "path": str(self.path),
                    "missing_fields": sorted(set(_SCHEMA) - set(payload)),
                    "unknown_fields": sorted(set(payload) - set(_SCHEMA)),
                },
            )
        if any(not isinstance(value, str) for value in payload.values()):
            return self._result(
                "untrusted",
                "attestation_untrusted",
                ValueError("every attestation field must be a string"),
            )
        for name, required in _SCHEMA.items():
            if required is not None and payload[name] != required:
                return DockerDirectAttestationRead(
                    status="untrusted",
                    reason="attestation_untrusted",
                    detail={
                        "path": str(self.path),
                        "field": name,
                        "expected": required,
                        "received": payload[name],
                    },
                )

        endpoint = payload["engine_endpoint"]
        unit_id = payload["engine_unit_id"]
        try:
            _validate_endpoint(endpoint)
            _validate_service_unit(unit_id)
            self._verify_endpoint_parent(endpoint)
        except (OSError, ValueError) as exc:
            return self._result("untrusted", "attestation_untrusted", exc)

        # Hash the bytes read from the already-open, verified descriptor.  Even a
        # non-semantic administrator edit therefore invalidates an earlier plan, and a
        # pathname replacement after this point cannot change the bytes being identified.
        fingerprint = f"sha256:{hashlib.sha256(raw).hexdigest()}"
        return DockerDirectAttestationRead(
            status="trusted",
            reason="docker_direct_unix_attestation_trusted",
            attestation=DockerDirectEngineAttestation(
                contract=payload["contract"],
                runtime=payload["runtime"],
                engine_transport=payload["engine_transport"],
                engine_endpoint=endpoint,
                engine_unit_id=unit_id,
                engine_process_role=payload["engine_process_role"],
                engine_scope=payload["engine_scope"],
                engine_privilege=payload["engine_privilege"],
                fingerprint=fingerprint,
            ),
            detail={
                "path": str(self.path),
                "file_uid": file_stat.st_uid,
                "file_mode": stat.S_IMODE(file_stat.st_mode),
                "schema_closed": True,
                "symlinks_followed": False,
            },
        )

    def _read_fixed_file(self) -> tuple[bytes, os.stat_result]:
        parent_fd = self._open_secure_root()
        try:
            for component in DOCKER_DIRECT_ATTESTATION_RELATIVE_PATH.parts[:-1]:
                next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent_fd)
                try:
                    self._verify_directory(os.fstat(next_fd), component)
                except Exception:
                    os.close(next_fd)
                    raise
                os.close(parent_fd)
                parent_fd = next_fd
            fd = os.open(
                DOCKER_DIRECT_ATTESTATION_RELATIVE_PATH.name,
                _FILE_FLAGS,
                dir_fd=parent_fd,
            )
            try:
                file_stat = os.fstat(fd)
                self._verify_file(file_stat)
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = os.read(fd, min(4096, MAX_ATTESTATION_BYTES + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > MAX_ATTESTATION_BYTES:
                        raise ValueError("attestation exceeds its byte limit")
                return b"".join(chunks), file_stat
            finally:
                os.close(fd)
        finally:
            os.close(parent_fd)

    def _verify_endpoint_parent(self, endpoint: str) -> None:
        parent_fd = self._open_secure_root()
        try:
            for component in PurePosixPath(endpoint).parts[1:-1]:
                next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent_fd)
                try:
                    self._verify_directory(os.fstat(next_fd), component)
                except Exception:
                    os.close(next_fd)
                    raise
                os.close(parent_fd)
                parent_fd = next_fd
        finally:
            os.close(parent_fd)

    def _open_secure_root(self) -> int:
        if not self._root.is_absolute():
            raise ValueError("attestation filesystem root must be absolute")
        fd = os.open(self._root, _DIRECTORY_FLAGS)
        try:
            self._verify_directory(os.fstat(fd), str(self._root))
        except Exception:
            os.close(fd)
            raise
        return fd

    def _verify_directory(self, value: os.stat_result, component: str) -> None:
        if not stat.S_ISDIR(value.st_mode):
            raise ValueError(f"attestation path component is not a directory: {component}")
        self._verify_owner_and_mode(value, f"directory {component}")

    def _verify_file(self, value: os.stat_result) -> None:
        if not stat.S_ISREG(value.st_mode):
            raise ValueError("attestation is not a regular file")
        self._verify_owner_and_mode(value, "attestation file")

    def _verify_owner_and_mode(self, value: os.stat_result, label: str) -> None:
        if value.st_uid != self._required_uid:
            raise ValueError(f"{label} is not owned by the required administrator uid")
        if stat.S_IMODE(value.st_mode) & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError(f"{label} is writable by group or other")

    def _result(
        self, status: str, reason: str, exc: BaseException
    ) -> DockerDirectAttestationRead:
        return DockerDirectAttestationRead(
            status=status,
            reason=reason,
            detail={
                "path": str(self.path),
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _validate_endpoint(endpoint: str) -> None:
    if (
        not endpoint.startswith("/")
        or endpoint == "/"
        or "\x00" in endpoint
        or "\r" in endpoint
        or "\n" in endpoint
        or posixpath.normpath(endpoint) != endpoint
        or "//" in endpoint
    ):
        raise ValueError("engine_endpoint must be one normalized absolute pathname")
    if any(part in {".", ".."} for part in PurePosixPath(endpoint).parts):
        raise ValueError("engine_endpoint contains a relative path component")


def _validate_service_unit(unit_id: str) -> None:
    if (
        not unit_id.endswith(".service")
        or len(unit_id) > 255
        or not unit_id
        or any(character in unit_id for character in "/\x00\r\n")
    ):
        raise ValueError("engine_unit_id must be one canonical-looking service unit id")
