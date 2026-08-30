"""Closed normalized vocabulary for ``docker-direct-unix-v1`` evidence.

This module defines no request and grants no authority. It only keeps the agent and
backend on one versioned vocabulary when the existing lifecycle-context read carries a
runtime-owner correlation.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


DOCKER_DIRECT_UNIX_CONTRACT: Final = "docker-direct-unix-v1"
DOCKER_RUNTIME_OWNER_METHOD_VERSION: Final = 1
DOCKER_RUNTIME_OWNER_PROVIDER: Final = "docker"
RUNTIME_OWNER_UMBRELLA_GAP: Final = "management_path.runtime_owner_unproven"


class DockerRuntimeOwnerStatus(StrEnum):
    RESOLVED = "resolved"
    INCOMPLETE = "incomplete"


class DockerExecutionCgroupRelation(StrEnum):
    EQUAL = "equal"
    ANCESTOR = "ancestor"


class DockerRuntimeOwnerGap(StrEnum):
    ATTESTATION_UNAVAILABLE = "management_path.runtime_owner.attestation_unavailable"
    ATTESTATION_UNTRUSTED = "management_path.runtime_owner.attestation_untrusted"
    ATTESTATION_CHANGED = "management_path.runtime_owner.attestation_changed"
    DIRECT_TRANSPORT_UNPROVEN = (
        "management_path.runtime_owner.direct_transport_unproven"
    )
    PEER_PIDFD_UNAVAILABLE = "management_path.runtime_owner.peer_pidfd_unavailable"
    PEER_PROCESS_EXITED = "management_path.runtime_owner.peer_process_exited"
    DAEMON_PROCESS_UNATTESTED = (
        "management_path.runtime_owner.daemon_process_unattested"
    )
    DAEMON_UNIT_UNRESOLVED = "management_path.runtime_owner.daemon_unit_unresolved"
    DAEMON_UNIT_MISMATCH = "management_path.runtime_owner.daemon_unit_mismatch"
    DAEMON_MAIN_PID_MISMATCH = (
        "management_path.runtime_owner.daemon_main_pid_mismatch"
    )
    SOCKET_ACTIVATION_UNSUPPORTED = (
        "management_path.runtime_owner.socket_activation_unsupported"
    )
    ENGINE_SNAPSHOT_CHANGED = "management_path.runtime_owner.engine_snapshot_changed"
    TRANSPORT_RECONNECTED = "management_path.runtime_owner.transport_reconnected"
    CONTAINER_ENUMERATION_INCOMPLETE = (
        "management_path.runtime_owner.container_enumeration_incomplete"
    )
    CONTAINER_NOT_FOUND = "management_path.runtime_owner.container_not_found"
    CONTAINER_AMBIGUOUS = "management_path.runtime_owner.container_ambiguous"
    CONTAINER_CHANGED = "management_path.runtime_owner.container_changed"
    PID_NAMESPACE_MISMATCH = "management_path.runtime_owner.pid_namespace_mismatch"
    CGROUP_NAMESPACE_MISMATCH = (
        "management_path.runtime_owner.cgroup_namespace_mismatch"
    )


DOCKER_RUNTIME_OWNER_GAPS: Final[frozenset[str]] = frozenset(
    str(gap) for gap in DockerRuntimeOwnerGap
)
DOCKER_EXECUTION_CGROUP_RELATIONS: Final[frozenset[str]] = frozenset(
    str(relation) for relation in DockerExecutionCgroupRelation
)
