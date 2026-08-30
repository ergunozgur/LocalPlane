"""What the backend needs to answer a request, assembled once at startup."""

from __future__ import annotations

from dataclasses import dataclass

from localplane.backend.agent_client import AgentClient
from localplane.backend.changes import ChangeService
from localplane.backend.config import Settings
from localplane.backend.db.database import Database
from localplane.backend.ingest import Ingestor, ObservationCoordinator
from localplane.backend.management import ManagementService
from localplane.backend.management_path import ManagementPathService
from localplane.backend.operations import OPERATIONS, build_executors
from localplane.backend.provenance import ProvenanceService
from localplane.backend.runs import RunService
from localplane.backend.systemd_lifecycle_context import SystemdLifecycleContextService


@dataclass(frozen=True)
class AppContext:
    settings: Settings
    database: Database
    client: AgentClient
    ingestor: Ingestor
    coordinator: ObservationCoordinator
    management: ManagementService
    provenance: ProvenanceService
    management_path: ManagementPathService
    systemd_lifecycle_context: SystemdLifecycleContextService
    runs: RunService
    changes: ChangeService

    @classmethod
    def build(cls, settings: Settings, database: Database) -> "AppContext":
        client = AgentClient(settings.agent_socket, settings.agent_timeout_s)
        # One provenance service and one management service, shared. The ingestor uses
        # management to keep findings level with new evidence; the API uses it to adopt and
        # release. They must agree about what "current enough to adopt" means and about who
        # owns an object, so both settings and both services are established in one place.
        provenance = ProvenanceService(database)
        management = ManagementService(database, settings.freshness_ttl_s, provenance)
        ingestor = Ingestor(database, management)
        coordinator = ObservationCoordinator(client, ingestor)
        # The Run engine is handed its operations rather than importing them, so the
        # engine never depends on what it plans against. This is the only place a planner
        # is bound to the service that will call it.
        runs = RunService(database, settings.freshness_ttl_s, OPERATIONS, provenance)
        # And the only place an *executor* is. Planners are pure functions of recorded
        # truth and are bound at import time; executors hold the agent client and the
        # observation coordinator, which is to say they hold the one path in this product
        # that can change a host, so they are built here where their dependencies exist.
        changes = ChangeService(
            database, runs, build_executors(client, coordinator, ingestor.objects)
        )
        # Management-path evidence shares the observation freshness horizon rather than
        # carrying its own. Two settings would eventually differ, and the state they would
        # differ into is a plan LocalPlane still calls current whose proof of safety it no
        # longer vouches for.
        management_path = ManagementPathService(
            database, client, settings.freshness_ttl_s
        )
        systemd_lifecycle_context = SystemdLifecycleContextService(
            client, ingestor, provenance
        )
        return cls(
            settings=settings,
            database=database,
            client=client,
            ingestor=ingestor,
            coordinator=coordinator,
            management=management,
            provenance=provenance,
            management_path=management_path,
            systemd_lifecycle_context=systemd_lifecycle_context,
            runs=runs,
            changes=changes,
        )
