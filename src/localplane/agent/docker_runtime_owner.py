"""Authoritative Docker runtime-owner correlation for ``docker-direct-unix-v1``.

The module composes, but does not replace, the three authoritative boundaries involved:
Docker's fixed API reads, Linux process/cgroup identity, and systemd process containment.
It has no lifecycle write and no caller-selectable runtime, endpoint, PID, container, unit
or D-Bus operation.
"""

from __future__ import annotations

import os
import posixpath
import re
import select
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Final

from localplane.agent.docker_direct_attestation import (
    DockerDirectAttestationReader,
    DockerDirectEngineAttestation,
)
from localplane.agent.providers.docker import (
    MAX_CONTAINERS,
    RUNTIME_OWNER_METHOD_VERSION,
    DockerRuntimeInfo,
    DockerRuntimeOwnerFailure,
    DockerRuntimeVersion,
)
from localplane.protocol.docker_runtime_owner import (
    DOCKER_DIRECT_UNIX_CONTRACT,
    DOCKER_EXECUTION_CGROUP_RELATIONS,
    DOCKER_RUNTIME_OWNER_GAPS,
    DOCKER_RUNTIME_OWNER_PROVIDER,
    DockerRuntimeOwnerStatus,
)


RUNTIME_OWNER_PROVIDER: Final = DOCKER_RUNTIME_OWNER_PROVIDER
RUNTIME_OWNER_STATUS_RESOLVED: Final = str(DockerRuntimeOwnerStatus.RESOLVED)
RUNTIME_OWNER_STATUS_INCOMPLETE: Final = str(DockerRuntimeOwnerStatus.INCOMPLETE)

_DIRECTORY_FLAGS: Final = (
    os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)


@dataclass(frozen=True)
class RuntimeOwnerCorrelation:
    """Normalized semantic result; transient kernel and transport handles are excluded."""

    contract_version: str
    method_version: int
    provider: str
    status: str
    attestation_fingerprint: str | None = None
    endpoint: str | None = None
    container_id: str | None = None
    container_started_at: str | None = None
    engine_id: str | None = None
    direct_transport_verified: bool = False
    peer_service_main_verified: bool = False
    owner_unit_id: str | None = None
    owner_invocation_id: str | None = None
    execution_cgroup_relation: str | None = None
    gaps: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.contract_version != DOCKER_DIRECT_UNIX_CONTRACT:
            raise ValueError("unknown runtime-owner contract")
        if self.method_version != RUNTIME_OWNER_METHOD_VERSION:
            raise ValueError("unknown runtime-owner method version")
        if self.provider != RUNTIME_OWNER_PROVIDER:
            raise ValueError("unknown runtime-owner provider")
        if self.status not in {str(status) for status in DockerRuntimeOwnerStatus}:
            raise ValueError("unknown runtime-owner status")
        if any(gap not in DOCKER_RUNTIME_OWNER_GAPS for gap in self.gaps):
            raise ValueError("unknown runtime-owner gap")
        if tuple(sorted(set(self.gaps))) != self.gaps:
            raise ValueError("runtime-owner gaps must be unique and sorted")
        if self.execution_cgroup_relation not in (
            None,
            *DOCKER_EXECUTION_CGROUP_RELATIONS,
        ):
            raise ValueError("unknown runtime-owner cgroup relation")
        if self.status == RUNTIME_OWNER_STATUS_RESOLVED:
            required = (
                self.attestation_fingerprint,
                self.endpoint,
                self.container_id,
                self.container_started_at,
                self.engine_id,
                self.owner_unit_id,
                self.owner_invocation_id,
                self.execution_cgroup_relation,
            )
            if (
                any(not isinstance(value, str) or not value for value in required)
                or not self.direct_transport_verified
                or not self.peer_service_main_verified
                or self.gaps
            ):
                raise ValueError("resolved runtime-owner correlation is incomplete")
        elif (
            self.direct_transport_verified
            or self.peer_service_main_verified
            or any(
                value is not None
                for value in (
                    self.container_id,
                    self.container_started_at,
                    self.engine_id,
                    self.owner_unit_id,
                    self.owner_invocation_id,
                    self.execution_cgroup_relation,
                )
            )
            or not self.gaps
        ):
            raise ValueError("incomplete runtime-owner correlation claims authority")

    @property
    def resolved(self) -> bool:
        return self.status == RUNTIME_OWNER_STATUS_RESOLVED and not self.gaps

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "method_version": self.method_version,
            "provider": self.provider,
            "status": self.status,
            "attestation_fingerprint": self.attestation_fingerprint,
            "endpoint": self.endpoint,
            "container_id": self.container_id,
            "container_started_at": self.container_started_at,
            "engine_id": self.engine_id,
            "direct_transport_verified": self.direct_transport_verified,
            "peer_service_main_verified": self.peer_service_main_verified,
            "owner_unit_id": self.owner_unit_id,
            "owner_invocation_id": self.owner_invocation_id,
            "execution_cgroup_relation": self.execution_cgroup_relation,
            "gaps": list(self.gaps),
        }


@dataclass(frozen=True)
class RuntimeOwnerObservation:
    correlation: RuntimeOwnerCorrelation
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProcessInspectionFailure(Exception):
    gap: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class PinnedProcessCgroup:
    """One exact process generation and its unified cgroup membership."""

    inspector: "LinuxProcessCgroupInspector"
    pid: int
    pidfd: int
    owns_pidfd: bool
    proc_fd: int
    cgroup_fd: int
    pid_namespace_fd: int
    start_time_ticks: int
    cgroup_path: str
    cgroup_device: int
    cgroup_inode: int
    pid_namespace_device: int
    pid_namespace_inode: int
    nspid: tuple[int, ...]
    closed: bool = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for fd in (self.pid_namespace_fd, self.cgroup_fd, self.proc_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        if self.owns_pidfd:
            try:
                os.close(self.pidfd)
            except OSError:
                pass

    def stable(self) -> bool:
        if self.closed or not _pidfd_live(self.pidfd):
            return False
        try:
            start_time = _parse_proc_start_time(_read_at(self.proc_fd, "stat", 8192))
            cgroup = _parse_unified_cgroup(_read_at(self.proc_fd, "cgroup", 65536))
            nspid = _parse_nspid(_read_at(self.proc_fd, "status", 256 * 1024))
            held = os.fstat(self.cgroup_fd)
            held_pid_namespace = os.fstat(self.pid_namespace_fd)
            current = self.inspector.cgroup_identity(cgroup)
        except (OSError, ValueError, ProcessInspectionFailure):
            return False
        return (
            _pidfd_live(self.pidfd)
            and start_time == self.start_time_ticks
            and cgroup == self.cgroup_path
            and nspid == self.nspid
            and held.st_dev == self.cgroup_device
            and held.st_ino == self.cgroup_inode
            and current == (self.cgroup_device, self.cgroup_inode)
            and held_pid_namespace.st_dev == self.pid_namespace_device
            and held_pid_namespace.st_ino == self.pid_namespace_inode
        )

    def shares_observer_pid_namespace(self) -> bool:
        try:
            return self.inspector.observer_pid_namespace_identity() == (
                self.pid_namespace_device,
                self.pid_namespace_inode,
            )
        except (OSError, ProcessInspectionFailure):
            return False

    def as_evidence(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "process_start_time_ticks": self.start_time_ticks,
            "cgroup_path": self.cgroup_path,
            "cgroup_device": self.cgroup_device,
            "cgroup_inode": self.cgroup_inode,
            "nspid": list(self.nspid),
            "pid_namespace_matches_observer": self.shares_observer_pid_namespace(),
            "pid_namespace_inode_persisted_as_identity": False,
            "pidfd_number_persisted_as_identity": False,
        }


class LinuxProcessCgroupInspector:
    """Narrow exact-PID/proc/cgroup reader; it never enumerates processes."""

    def __init__(
        self,
        *,
        proc_root: str | Path = "/proc",
        cgroup_root: str | Path = "/sys/fs/cgroup",
    ) -> None:
        self._proc_root = Path(proc_root)
        self._cgroup_root = Path(cgroup_root)

    def pin(self, pid: int) -> PinnedProcessCgroup:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ProcessInspectionFailure(
                "management_path.runtime_owner.pid_namespace_mismatch",
                "docker_state_pid_invalid",
            )
        try:
            pidfd = os.pidfd_open(pid, 0)
        except (AttributeError, OSError) as exc:
            raise ProcessInspectionFailure(
                "management_path.runtime_owner.pid_namespace_mismatch",
                "docker_state_pid_not_pinnable",
                {"pid": pid, "error_type": type(exc).__name__, "error": str(exc)},
            ) from exc
        try:
            return self.inspect_existing(pid, pidfd, owns_pidfd=True)
        except Exception:
            os.close(pidfd)
            raise

    def inspect_existing(
        self, pid: int, pidfd: int, *, owns_pidfd: bool = False
    ) -> PinnedProcessCgroup:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ProcessInspectionFailure(
                "management_path.runtime_owner.pid_namespace_mismatch",
                "process_pid_invalid",
                {"pid": pid},
            )
        if not _pidfd_live(pidfd):
            raise ProcessInspectionFailure(
                "management_path.runtime_owner.peer_process_exited",
                "process_pidfd_not_live",
            )
        proc_root_fd = os.open(self._proc_root, _DIRECTORY_FLAGS)
        try:
            proc_fd = os.open(str(pid), _DIRECTORY_FLAGS, dir_fd=proc_root_fd)
        except OSError as exc:
            os.close(proc_root_fd)
            raise ProcessInspectionFailure(
                "management_path.runtime_owner.pid_namespace_mismatch",
                "process_not_visible_in_proc",
                {"pid": pid, "error": str(exc)},
            ) from exc
        os.close(proc_root_fd)
        cgroup_fd: int | None = None
        pid_namespace_fd: int | None = None
        try:
            start_a = _parse_proc_start_time(_read_at(proc_fd, "stat", 8192))
            cgroup_path = _parse_unified_cgroup(_read_at(proc_fd, "cgroup", 65536))
            nspid = _parse_nspid(_read_at(proc_fd, "status", 256 * 1024))
            if not nspid or nspid[0] != pid:
                raise ProcessInspectionFailure(
                    "management_path.runtime_owner.pid_namespace_mismatch",
                    "proc_nspid_does_not_match_observed_pid",
                    {"pid": pid, "nspid": list(nspid)},
                )
            if cgroup_path == "/":
                raise ProcessInspectionFailure(
                    "management_path.runtime_owner.cgroup_namespace_mismatch",
                    "process_cgroup_is_namespace_root",
                    {"pid": pid},
                )
            cgroup_fd = self._open_cgroup(cgroup_path)
            cgroup_stat = os.fstat(cgroup_fd)
            # ``ns/pid`` is a kernel-owned procfs magic link.  Following this one fixed
            # entry yields a namespace handle; the pinned pidfd and repeated process
            # generation checks prevent it from becoming a numeric-PID lookup race.
            pid_namespace_fd = os.open(
                "ns/pid", os.O_RDONLY | os.O_CLOEXEC, dir_fd=proc_fd
            )
            pid_namespace_stat = os.fstat(pid_namespace_fd)
            start_b = _parse_proc_start_time(_read_at(proc_fd, "stat", 8192))
            if start_a != start_b or not _pidfd_live(pidfd):
                raise ProcessInspectionFailure(
                    "management_path.runtime_owner.peer_process_exited",
                    "process_generation_changed_during_read",
                )
            return PinnedProcessCgroup(
                inspector=self,
                pid=pid,
                pidfd=pidfd,
                owns_pidfd=owns_pidfd,
                proc_fd=proc_fd,
                cgroup_fd=cgroup_fd,
                pid_namespace_fd=pid_namespace_fd,
                start_time_ticks=start_a,
                cgroup_path=cgroup_path,
                cgroup_device=cgroup_stat.st_dev,
                cgroup_inode=cgroup_stat.st_ino,
                pid_namespace_device=pid_namespace_stat.st_dev,
                pid_namespace_inode=pid_namespace_stat.st_ino,
                nspid=nspid,
            )
        except ProcessInspectionFailure:
            if pid_namespace_fd is not None:
                os.close(pid_namespace_fd)
            if cgroup_fd is not None:
                os.close(cgroup_fd)
            os.close(proc_fd)
            raise
        except (OSError, ValueError) as exc:
            if pid_namespace_fd is not None:
                os.close(pid_namespace_fd)
            if cgroup_fd is not None:
                os.close(cgroup_fd)
            os.close(proc_fd)
            raise ProcessInspectionFailure(
                "management_path.runtime_owner.cgroup_namespace_mismatch",
                "process_cgroup_evidence_unreadable",
                {"pid": pid, "error_type": type(exc).__name__, "error": str(exc)},
            ) from exc

    def observer_pid_namespace_identity(self) -> tuple[int, int]:
        try:
            fd = os.open(
                self._proc_root / "self/ns/pid",
                os.O_RDONLY | os.O_CLOEXEC,
            )
        except OSError as exc:
            raise ProcessInspectionFailure(
                "management_path.runtime_owner.pid_namespace_mismatch",
                "observer_pid_namespace_unreadable",
                {"error": str(exc)},
            ) from exc
        try:
            value = os.fstat(fd)
            return value.st_dev, value.st_ino
        finally:
            os.close(fd)

    def cgroup_identity(self, path: str) -> tuple[int, int]:
        fd = self._open_cgroup(path)
        try:
            value = os.fstat(fd)
            return value.st_dev, value.st_ino
        finally:
            os.close(fd)

    def _open_cgroup(self, path: str) -> int:
        _validate_cgroup_path(path)
        root_fd = os.open(self._cgroup_root, _DIRECTORY_FLAGS)
        try:
            try:
                controllers = _read_at(root_fd, "cgroup.controllers", 1024 * 1024)
            except OSError as exc:
                raise ProcessInspectionFailure(
                    "management_path.runtime_owner.cgroup_namespace_mismatch",
                    "unified_cgroup_v2_unavailable",
                    {"error": str(exc)},
                ) from exc
            if controllers is None:  # pragma: no cover - keeps the evidence branch explicit
                raise ProcessInspectionFailure(
                    "management_path.runtime_owner.cgroup_namespace_mismatch",
                    "unified_cgroup_v2_unavailable",
                )
            current_fd = root_fd
            try:
                for component in PurePosixPath(path).parts[1:]:
                    next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current_fd)
                    if current_fd != root_fd:
                        os.close(current_fd)
                    current_fd = next_fd
                if current_fd == root_fd:
                    return os.dup(root_fd)
                result = current_fd
                current_fd = root_fd
                return result
            finally:
                if current_fd != root_fd:
                    os.close(current_fd)
        finally:
            os.close(root_fd)


class DockerRuntimeOwnerCorrelator:
    """Compose the accepted direct-Engine proof into one normalized answer."""

    def __init__(
        self,
        *,
        docker: Any,
        systemd: Any,
        attestation_reader: DockerDirectAttestationReader,
        process_inspector: LinuxProcessCgroupInspector | Any | None = None,
    ) -> None:
        self._docker = docker
        self._systemd = systemd
        self._attestation_reader = attestation_reader
        self._processes = process_inspector or LinuxProcessCgroupInspector()

    def correlate(self, accepted_cgroup_path: str) -> RuntimeOwnerObservation:
        evidence: dict[str, Any] = {
            "contract": DOCKER_DIRECT_UNIX_CONTRACT,
            "method_version": RUNTIME_OWNER_METHOD_VERSION,
            "caller_supplied_runtime_identity": False,
            "heuristics_used": False,
        }
        attestation_read = self._attestation_reader.read()
        evidence["attestation_a"] = attestation_read.as_dict()
        if not attestation_read.trusted or attestation_read.attestation is None:
            gap = f"management_path.runtime_owner.{attestation_read.reason}"
            return self._incomplete((gap,), evidence)
        attestation = attestation_read.attestation
        semantic = {
            "attestation_fingerprint": attestation.fingerprint,
            "endpoint": attestation.engine_endpoint,
        }

        peer_process: PinnedProcessCgroup | None = None
        matched_processes: list[PinnedProcessCgroup] = []
        try:
            accepted_identity_a = self._processes.cgroup_identity(accepted_cgroup_path)
            with self._docker.open_runtime_owner_snapshot(attestation) as session:
                peer = session.peer
                evidence["peer"] = {
                    "pid": peer.pid,
                    "uid": peer.uid,
                    "gid": peer.gid,
                    "pidfd_number_persisted_as_identity": False,
                    "source": "actual_connected_af_unix_socket",
                }
                peer_process = self._processes.inspect_existing(peer.pid, peer.pidfd)
                evidence["peer_process"] = peer_process.as_evidence()
                if not peer_process.shares_observer_pid_namespace():
                    raise ProcessInspectionFailure(
                        "management_path.runtime_owner.pid_namespace_mismatch",
                        "docker_engine_pid_namespace_differs_from_localplane",
                    )

                systemd_a = self._systemd.resolve_process_unit_pidfd(peer.pidfd)
                evidence["systemd_a"] = systemd_a.as_dict()
                systemd_semantic_a = _validate_systemd_owner(
                    systemd_a, attestation, peer.pid, peer_process.cgroup_path
                )

                version_a = session.read_version()
                info_a = session.read_info()
                evidence["engine_a"] = _engine_evidence(
                    version_a, info_a, session.selected_api_version
                )
                listed_ids = session.list_container_ids()
                evidence["container_enumeration"] = {
                    "listed": len(listed_ids),
                    "limit": MAX_CONTAINERS,
                    "ids_persisted": False,
                    "complete": True,
                }

                matches: list[tuple[str, str, str, PinnedProcessCgroup]] = []
                inspected = 0
                candidates = 0
                for container_id in listed_ids:
                    inspect_a = session.inspect_listed_container(container_id)
                    inspected += 1
                    candidate_a = _runtime_candidate(inspect_a, container_id)
                    if candidate_a is None:
                        continue
                    candidates += 1
                    pinned = self._processes.pin(candidate_a[1])
                    retained = False
                    try:
                        relation = _cgroup_relation(
                            pinned.cgroup_path, accepted_cgroup_path
                        )
                        if relation is None:
                            continue
                        inspect_b = session.inspect_listed_container(container_id)
                        candidate_b = _runtime_candidate(inspect_b, container_id)
                        if candidate_b != candidate_a or not pinned.stable():
                            raise DockerRuntimeOwnerFailure(
                                "management_path.runtime_owner.container_changed",
                                "docker_container_generation_changed",
                                {"container_id": container_id},
                            )
                        if (
                            self._processes.cgroup_identity(accepted_cgroup_path)
                            != accepted_identity_a
                        ):
                            raise DockerRuntimeOwnerFailure(
                                "management_path.runtime_owner.container_changed",
                                "accepted_socket_cgroup_changed",
                            )
                        matches.append(
                            (container_id, candidate_a[0], relation, pinned)
                        )
                        matched_processes.append(pinned)
                        retained = True
                    finally:
                        # A failed second inspect or cgroup refresh must not leak the
                        # candidate pidfd/proc/cgroup/namespace handles.  A retained match
                        # stays pinned through the final Engine/systemd sandwich.
                        if not retained:
                            pinned.close()

                evidence["container_enumeration"].update(
                    {
                        "inspected": inspected,
                        "eligible_candidates": candidates,
                        "matches": len(matches),
                    }
                )
                if not matches:
                    raise DockerRuntimeOwnerFailure(
                        "management_path.runtime_owner.container_not_found",
                        "accepted_socket_container_not_found",
                    )
                if len(matches) != 1:
                    raise DockerRuntimeOwnerFailure(
                        "management_path.runtime_owner.container_ambiguous",
                        "accepted_socket_container_ambiguous",
                        {"match_count": len(matches)},
                    )
                container_id, started_at, relation, matched_process = matches[0]

                info_b = session.read_info()
                version_b = session.read_version()
                evidence["engine_b"] = _engine_evidence(
                    version_b, info_b, session.selected_api_version
                )
                if version_a != version_b or info_a != info_b:
                    raise DockerRuntimeOwnerFailure(
                        "management_path.runtime_owner.engine_snapshot_changed",
                        "docker_engine_snapshot_changed",
                    )

                systemd_b = self._systemd.resolve_process_unit_pidfd(peer.pidfd)
                evidence["systemd_b"] = systemd_b.as_dict()
                systemd_semantic_b = _validate_systemd_owner(
                    systemd_b, attestation, peer.pid, peer_process.cgroup_path
                )
                if systemd_semantic_a != systemd_semantic_b:
                    raise DockerRuntimeOwnerFailure(
                        "management_path.runtime_owner.daemon_process_unattested",
                        "systemd_owner_generation_changed",
                    )
                inspect_final = session.inspect_listed_container(container_id)
                if inspect_final.get("Id") != container_id:
                    raise DockerRuntimeOwnerFailure(
                        "management_path.runtime_owner.container_changed",
                        "docker_container_generation_changed",
                        {"container_id": container_id},
                    )
                candidate_final = _runtime_candidate(inspect_final, container_id)
                final_relation = _cgroup_relation(
                    matched_process.cgroup_path, accepted_cgroup_path
                )
                if (
                    candidate_final != (started_at, matched_process.pid)
                    or final_relation != relation
                ):
                    raise DockerRuntimeOwnerFailure(
                        "management_path.runtime_owner.container_changed",
                        "docker_container_generation_changed",
                        {"container_id": container_id},
                    )
                if (
                    not peer_process.stable()
                    or not matched_process.stable()
                    or self._processes.cgroup_identity(accepted_cgroup_path)
                    != accepted_identity_a
                ):
                    raise DockerRuntimeOwnerFailure(
                        "management_path.runtime_owner.container_changed",
                        "kernel_process_or_cgroup_evidence_changed",
                    )
                attestation_b = self._attestation_reader.read()
                evidence["attestation_b"] = attestation_b.as_dict()
                if (
                    not attestation_b.trusted
                    or attestation_b.attestation is None
                    or attestation_b.attestation != attestation
                ):
                    raise DockerRuntimeOwnerFailure(
                        "management_path.runtime_owner.attestation_changed",
                        "docker_direct_attestation_changed",
                    )
                session.require_peer_live()

                correlation = RuntimeOwnerCorrelation(
                    contract_version=attestation.contract,
                    method_version=RUNTIME_OWNER_METHOD_VERSION,
                    provider=RUNTIME_OWNER_PROVIDER,
                    status=RUNTIME_OWNER_STATUS_RESOLVED,
                    attestation_fingerprint=attestation.fingerprint,
                    endpoint=attestation.engine_endpoint,
                    container_id=container_id,
                    container_started_at=started_at,
                    engine_id=info_a.engine_id,
                    direct_transport_verified=True,
                    peer_service_main_verified=True,
                    owner_unit_id=systemd_a.canonical_id,
                    owner_invocation_id=systemd_a.invocation_id,
                    execution_cgroup_relation=relation,
                )
                evidence["result"] = {
                    "status": "resolved",
                    "container_id": container_id,
                    "container_started_at": started_at,
                    "cgroup_relation": relation,
                    "live_restore_exemption_used": False,
                }
                return RuntimeOwnerObservation(correlation=correlation, evidence=evidence)
        except DockerRuntimeOwnerFailure as exc:
            evidence["failure"] = {
                "reason": exc.reason,
                "gap": exc.gap,
                "detail": dict(exc.detail),
            }
            return self._incomplete((exc.gap,), evidence, semantic)
        except ProcessInspectionFailure as exc:
            evidence["failure"] = {
                "reason": exc.reason,
                "gap": exc.gap,
                "detail": dict(exc.detail),
            }
            return self._incomplete((exc.gap,), evidence, semantic)
        except Exception as exc:
            evidence["failure"] = {
                "reason": "runtime_owner_observation_failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            return self._incomplete(
                ("management_path.runtime_owner.daemon_process_unattested",),
                evidence,
                semantic,
            )
        finally:
            for pinned in matched_processes:
                pinned.close()
            if peer_process is not None:
                peer_process.close()

    @staticmethod
    def _incomplete(
        gaps: tuple[str, ...],
        evidence: dict[str, Any],
        semantic: dict[str, Any] | None = None,
    ) -> RuntimeOwnerObservation:
        semantic = semantic or {}
        closed = tuple(sorted(set(gaps)))
        return RuntimeOwnerObservation(
            correlation=RuntimeOwnerCorrelation(
                contract_version=DOCKER_DIRECT_UNIX_CONTRACT,
                method_version=RUNTIME_OWNER_METHOD_VERSION,
                provider=RUNTIME_OWNER_PROVIDER,
                status=RUNTIME_OWNER_STATUS_INCOMPLETE,
                attestation_fingerprint=semantic.get("attestation_fingerprint"),
                endpoint=semantic.get("endpoint"),
                gaps=closed,
            ),
            evidence=evidence,
        )


def _validate_systemd_owner(
    resolution: Any,
    attestation: DockerDirectEngineAttestation,
    peer_pid: int,
    peer_cgroup: str,
) -> tuple[Any, ...]:
    if resolution.status != "resolved" or resolution.unit is None:
        gap = (
            "management_path.runtime_owner.socket_activation_unsupported"
            if resolution.status == "resolved"
            else "management_path.runtime_owner.daemon_unit_unresolved"
        )
        raise DockerRuntimeOwnerFailure(
            gap,
            resolution.reason or "docker_peer_systemd_unit_unresolved",
        )
    facts = resolution.unit.facts
    service = facts.get("service") if isinstance(facts.get("service"), dict) else {}
    if facts.get("unit_type") != "service":
        raise DockerRuntimeOwnerFailure(
            "management_path.runtime_owner.socket_activation_unsupported",
            "docker_listener_peer_not_service",
            {"unit_type": facts.get("unit_type")},
        )
    if resolution.canonical_id != attestation.engine_unit_id:
        raise DockerRuntimeOwnerFailure(
            "management_path.runtime_owner.daemon_unit_mismatch",
            "docker_peer_unit_does_not_match_attestation",
            {"observed_unit": resolution.canonical_id},
        )
    if (
        facts.get("canonical_id") != resolution.canonical_id
        or facts.get("load_state") != "loaded"
        or facts.get("active_state") != "active"
        or facts.get("transient") is not False
        or facts.get("current_job") is not None
        or not isinstance(resolution.invocation_id, str)
        or not re.fullmatch(r"[0-9a-f]{32}", resolution.invocation_id)
        or set(resolution.invocation_id) == {"0"}
        or facts.get("invocation_id") != resolution.invocation_id
    ):
        raise DockerRuntimeOwnerFailure(
            "management_path.runtime_owner.daemon_process_unattested",
            "docker_peer_service_state_unstable",
        )
    main_pid = service.get("main_pid")
    if (
        isinstance(peer_pid, bool)
        or not isinstance(peer_pid, int)
        or peer_pid <= 0
        or isinstance(main_pid, bool)
        or not isinstance(main_pid, int)
        or main_pid <= 0
        or main_pid != peer_pid
    ):
        raise DockerRuntimeOwnerFailure(
            "management_path.runtime_owner.daemon_main_pid_mismatch",
            "docker_listener_peer_is_not_service_main_pid",
        )
    service_cgroup = service.get("control_group")
    if not isinstance(service_cgroup, str) or _cgroup_relation(
        service_cgroup, peer_cgroup
    ) is None:
        raise DockerRuntimeOwnerFailure(
            "management_path.runtime_owner.daemon_process_unattested",
            "docker_peer_service_cgroup_mismatch",
        )
    return (
        resolution.canonical_id,
        resolution.invocation_id,
        facts.get("load_state"),
        facts.get("active_state"),
        facts.get("sub_state"),
        facts.get("current_job"),
        main_pid,
        service_cgroup,
    )


def _runtime_candidate(
    inspected: dict[str, Any], listed_id: str
) -> tuple[str, int] | None:
    if inspected.get("Id") != listed_id:
        raise DockerRuntimeOwnerFailure(
            "management_path.runtime_owner.engine_snapshot_changed",
            "docker_inspect_id_mismatch",
            {"listed_id": listed_id},
        )
    state = inspected.get("State")
    host = inspected.get("HostConfig")
    if not isinstance(state, dict) or not isinstance(host, dict):
        raise DockerRuntimeOwnerFailure(
            "management_path.runtime_owner.engine_snapshot_changed",
            "docker_inspect_shape_invalid",
            {"container_id": listed_id},
        )
    if state.get("Running") is not True or host.get("NetworkMode") != "host":
        return None
    pid = state.get("Pid")
    started_at = state.get("StartedAt")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or not isinstance(started_at, str)
        or not started_at
        or not _valid_started_at(started_at)
    ):
        return None
    return started_at, pid


def _valid_started_at(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return "T" in value and parsed.tzinfo is not None and parsed.year > 1


def _engine_evidence(
    version: DockerRuntimeVersion,
    info: DockerRuntimeInfo,
    selected_api: str | None,
) -> dict[str, Any]:
    return {
        "version": version.as_dict(),
        "info": info.as_dict(),
        "selected_api_version": selected_api,
        "same_transport_required": True,
        "live_restore_exemption_used": False,
    }


def _pidfd_live(pidfd: int) -> bool:
    try:
        poller = select.poll()
        poller.register(
            pidfd,
            select.POLLIN | select.POLLHUP | select.POLLERR | getattr(select, "POLLNVAL", 0),
        )
        return not poller.poll(0)
    except OSError:
        return False


def _read_at(directory_fd: int, name: str, limit: int) -> str:
    fd = os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(4096, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise ValueError(f"{name} exceeds its read limit")
        return b"".join(chunks).decode("utf-8")
    finally:
        os.close(fd)


def _parse_proc_start_time(raw: str) -> int:
    closing = raw.rfind(")")
    if closing < 0:
        raise ValueError("proc stat command field is malformed")
    fields = raw[closing + 1 :].split()
    if len(fields) <= 19:
        raise ValueError("proc stat start time is unavailable")
    value = int(fields[19])
    if value <= 0:
        raise ValueError("proc stat start time is invalid")
    return value


def _parse_unified_cgroup(raw: str) -> str:
    unified: list[str] = []
    legacy = False
    for line in raw.splitlines():
        fields = line.split(":", 2)
        if len(fields) != 3:
            raise ValueError("proc cgroup row is malformed")
        hierarchy, controllers, path = fields
        if hierarchy == "0" and controllers == "":
            unified.append(path)
        elif controllers:
            legacy = True
    if legacy or len(unified) != 1:
        raise ValueError("process does not expose one unified cgroup-v2 membership")
    _validate_cgroup_path(unified[0])
    return unified[0]


def _parse_nspid(raw: str) -> tuple[int, ...]:
    for line in raw.splitlines():
        name, separator, value = line.partition(":")
        if separator and name == "NSpid":
            result = tuple(int(part) for part in value.split())
            if result and all(pid > 0 for pid in result):
                return result
            break
    raise ValueError("proc status NSpid is unavailable")


def _validate_cgroup_path(path: str) -> None:
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or "\x00" in path
        or "\r" in path
        or "\n" in path
        or posixpath.normpath(path) != path
        or "//" in path
        or any(part in {".", ".."} for part in PurePosixPath(path).parts)
    ):
        raise ValueError("cgroup path is not one normalized absolute v2 path")


def _cgroup_relation(ancestor: str, descendant: str) -> str | None:
    try:
        _validate_cgroup_path(ancestor)
        _validate_cgroup_path(descendant)
    except ValueError:
        return None
    # The cgroup-v2 hierarchy root contains every process.  Treating it as a container
    # ancestor would turn "one running container" into false unique authority.
    if ancestor == "/" or descendant == "/":
        return None
    ancestor_parts = PurePosixPath(ancestor).parts
    descendant_parts = PurePosixPath(descendant).parts
    if ancestor_parts == descendant_parts:
        return "equal"
    if len(ancestor_parts) < len(descendant_parts) and (
        descendant_parts[: len(ancestor_parts)] == ancestor_parts
    ):
        return "ancestor"
    return None
