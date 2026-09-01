"""The v1 API.

Reads are pure. ``GET`` never contacts the host and never writes an observation — a page
refresh must not be able to change what LocalPlane has recorded, or the record stops being
a record of the host and becomes a record of who was looking.

Observing is therefore explicit: the network, Docker and systemd refresh endpoints are
read-only with respect to the host and write only LocalPlane's own store.

Two endpoints — ``/agent`` and ``/agent/capabilities`` — do reach the agent, because the
question they answer is "is it there right now". They always say whether the answer is
live or remembered.

``adopt`` and ``release`` write to LocalPlane's store and to nothing else. They are the two
transitions on the management axis, and neither of them is a host operation: adopt retains
values the host already had, release forgets them. Their responses say so in a field a
client can branch on rather than only in prose, and the store behind them cannot record a
host write on this path at all.

``intent/revise`` and ``intent/adopt-runtime`` are not transitions — they start and end at
``managed`` — and they are not host operations either. Each replaces the retained desired
state with a new immutable version and moves the active-intent pointer to it. They are two
endpoints rather than one with a mode flag because they are two different acts, and a
history that could not tell them apart afterwards would not be a history.

``management-path`` answers one safety question: which currently known LocalPlane-managed
object carries the operator's actual management path. ``POST
/management-path/observations/refresh`` takes the evidence — the transport of *that* request
and the kernel's route to its peer — and records it; the ``GET`` derives the answer from
what has already been recorded and writes nothing. Neither takes a peer address, an
interface or any other caller-selected target: there is no parameter one could arrive
through. Proxy headers are read by nothing on this path, so behind a reverse proxy the
answer is that the direct peer is the proxy and the management path is unknown.

``runs`` is the Change Engine. Creating one plans a typed operation against records
LocalPlane already holds and publishes an immutable preview of what it would involve;
``confirm`` records that somebody satisfied the confirmation that plan requires; ``apply``
executes it; ``cancel`` ends it before any boundary is crossed.

**A Run is still not a Change.** A Change is the record that LocalPlane entered the path on
which a host write may occur, and it comes into existence in ``apply`` and nowhere else —
not when a plan is published, not when it is confirmed, and not when a checkpoint is armed,
because none of those can have moved anything about the host. ``GET /changes`` and
``GET /changes/{id}`` read that history and write nothing.

``apply`` is the only endpoint here that can change the host, it takes **no request body**,
and there is no parameter anywhere on its path for a value, an interface, a command, an
argv, a provider or a shell. There is deliberately no ``/execute``, no
``/operations/{name}/execute``, no ``/rollback``, no terminal and no passthrough to the
privileged helper: an attempt to reach any of them fails the only way it can, with a 404.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, Security

from localplane import __version__
from localplane.backend.agent_client import AgentError
from localplane.backend.api import schemas, views
from localplane.backend.api.transport import request_connection_of, transport_of
from localplane.backend.auth import (
    SESSION_COOKIE,
    AuthenticatedRequest,
    app_authentication,
    cookie_is_secure,
    require_authentication,
    require_browser_session,
    require_master_bearer,
)
from localplane.backend.context import AppContext
from localplane.backend.db.repositories import ChangeRecord, ObjectRecord
from localplane.backend.domain.identity import (
    OBJECT_KIND_DOCKER_CONTAINER,
    OBJECT_KIND_NETWORK_INTERFACE,
    OBJECT_KIND_SYSTEMD_UNIT,
)
from localplane.backend.domain.management_path import TransportEvidence
from localplane.backend.domain.protection import (
    ManagementPathVerdict,
    assess_resource_protection,
)
from localplane.backend.domain.provenance import derive_adoption_eligibility
from localplane.backend.domain.runs import OperationType
from localplane.backend.domain.systemd_lifecycle import SystemdServiceAction
from localplane.backend.management import ManagementRefused, RevisionOutcome
from localplane.backend.runs import RunRefused
from localplane.protocol.capabilities import (
    CAPABILITY_DOCKER_CONTAINERS_OBSERVE,
    CAPABILITY_NETWORK_OBSERVE,
    CAPABILITY_SYSTEMD_UNITS_OBSERVE,
)
from localplane.protocol.wire import (
    DOCKER_LOG_LINES_DEFAULT,
    DOCKER_LOG_LINES_MAX,
    ErrorCode,
)

session_router = APIRouter(prefix="/api/v1")
router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(require_authentication)],
    responses={
        401: {"model": schemas.ErrorResponse},
        403: {"model": schemas.ErrorResponse},
    },
)

# An agent failure is not one kind of failure. "You asked for an interface name the kernel
# could not have given a link" and "the agent is not running" are different conditions
# with different fixes, and answering 503 to both would tell a caller to retry a request
# that will never succeed.
_AGENT_ERROR_STATUS: dict[str, int] = {
    ErrorCode.INVALID_PARAMS: 400,
    ErrorCode.UNKNOWN_FIELD: 400,
    ErrorCode.UNSUPPORTED_METHOD: 400,
    ErrorCode.MESSAGE_TOO_LARGE: 400,
    ErrorCode.AGENT_UNAVAILABLE: 503,
    ErrorCode.TIMEOUT: 504,
    ErrorCode.CAPABILITY_UNAVAILABLE: 503,
    ErrorCode.UNAUTHORIZED_PEER: 502,
    ErrorCode.PROVIDER_ERROR: 502,
    ErrorCode.INTERNAL_ERROR: 502,
    ErrorCode.MALFORMED_MESSAGE: 502,
    ErrorCode.PROTOCOL_VERSION_UNSUPPORTED: 502,
}


def _status_for(error: AgentError) -> int:
    return _AGENT_ERROR_STATUS.get(error.code, 502)


def _context(request: Request) -> AppContext:
    return request.app.state.context


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_host_id(context: AppContext) -> str:
    host = context.ingestor.hosts.most_recent()
    if host is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "host_unknown",
                "message": "no host has been identified yet; the agent has not been reached",
                "detail": {"agent_socket": str(context.settings.agent_socket)},
            },
        )
    return host.host_id


def _management_path(
    context: AppContext, request: Request, host_id: str
) -> tuple[ManagementPathVerdict, TransportEvidence]:
    """The management path as it stands for *this* request's connection.

    Derived per request rather than cached, because that is what it is: a judgement about
    the session asking. A verdict reached for a remote operator must never answer for a
    call arriving over loopback, and deriving it once per request from that request's own
    transport is what makes it structurally impossible for it to.

    Pure — it reads persisted evidence and contacts nothing — so every ``GET`` that needs it
    stays a read.
    """
    transport = transport_of(request)
    return context.management_path.assess(host_id, transport), transport


def _require_interface(context: AppContext, object_id: str) -> ObjectRecord:
    record = context.ingestor.objects.get(object_id)
    if record is None or record.kind != OBJECT_KIND_NETWORK_INTERFACE:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "object_not_found",
                "message": f"no network interface object with id {object_id}",
                "detail": {"object_id": object_id},
            },
        )
    return record


# ----------------------------------------------------------------------------- session


@session_router.post(
    "/session",
    response_model=schemas.SessionStatus,
    tags=["session"],
    responses={401: {"model": schemas.ErrorResponse}, 403: {"model": schemas.ErrorResponse}},
)
def create_browser_session(
    request: Request,
    response: Response,
    _master: Annotated[AuthenticatedRequest, Security(require_master_bearer)],
) -> schemas.SessionStatus:
    authentication = app_authentication(request)
    secure = cookie_is_secure(request, authentication)
    token, expires_at = authentication.create_session(request.cookies.get(SESSION_COOKIE))
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=12 * 60 * 60,
        expires=expires_at,
        path="/",
        secure=secure,
        httponly=True,
        samesite="strict",
    )
    return schemas.SessionStatus(
        authenticated=True, mechanism="session", expires_at=expires_at
    )


@session_router.get(
    "/session",
    response_model=schemas.SessionStatus,
    tags=["session"],
    responses={401: {"model": schemas.ErrorResponse}},
)
def get_browser_session(
    request: Request,
    authenticated: Annotated[AuthenticatedRequest, Security(require_authentication)],
) -> schemas.SessionStatus:
    return schemas.SessionStatus(
        authenticated=True,
        mechanism=authenticated.mechanism,
        expires_at=authenticated.expires_at,
    )


@session_router.delete(
    "/session",
    status_code=204,
    response_class=Response,
    tags=["session"],
    responses={401: {"model": schemas.ErrorResponse}, 403: {"model": schemas.ErrorResponse}},
)
def delete_browser_session(
    request: Request,
    response: Response,
    authenticated: Annotated[AuthenticatedRequest, Security(require_browser_session)],
) -> Response:
    assert authenticated.session_token is not None
    app_authentication(request).sessions.revoke(authenticated.session_token)
    response.status_code = 204
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=cookie_is_secure(request, app_authentication(request)),
        httponly=True,
        samesite="strict",
    )
    return response


# ------------------------------------------------------------------------------- status


@router.get("/status", response_model=schemas.BackendStatus, tags=["status"])
def get_status(request: Request) -> schemas.BackendStatus:
    """Backend liveness only. Says nothing about whether the host can be seen."""
    context = _context(request)
    versions = [
        row["version"]
        for row in context.database.query("SELECT version FROM schema_migrations ORDER BY version")
    ]
    return schemas.BackendStatus(
        version=__version__,
        database=schemas.DatabaseStatus(
            path=str(context.database.path), schema_versions=versions
        ),
    )


# --------------------------------------------------------------------------------- host


@router.get("/host", response_model=schemas.Host, tags=["host"])
def get_host(request: Request) -> schemas.Host:
    context = _context(request)
    host = context.ingestor.hosts.most_recent()
    if host is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "host_unknown",
                "message": "no host has been identified yet",
                "detail": {"agent_socket": str(context.settings.agent_socket)},
            },
        )
    return views.host_view(host, context.settings.freshness_ttl_s)


# -------------------------------------------------------------------------------- agent


@router.get("/agent", response_model=schemas.AgentStatus, tags=["agent"])
def get_agent(request: Request) -> schemas.AgentStatus:
    """Probe the agent. An unreachable agent is a 200 that says so, not a 500.

    "The agent is not answering" is a true and useful answer about the state of the
    system, and a caller should not have to interpret a transport error to learn it.
    """
    context = _context(request)
    socket_path = str(context.settings.agent_socket)
    try:
        context.coordinator.handshake()
    except AgentError as exc:
        recorded = context.ingestor.agents.most_recent()
        return schemas.AgentStatus(
            reachable=False,
            source="recorded" if recorded else "live",
            as_of=_now(),
            error=schemas.ErrorBody(**exc.as_dict()),
            agent=views.agent_identity_view(recorded) if recorded else None,
            socket=socket_path,
        )

    recorded = context.ingestor.agents.most_recent()
    return schemas.AgentStatus(
        reachable=True,
        source="live",
        as_of=_now(),
        error=None,
        agent=views.agent_identity_view(recorded) if recorded else None,
        socket=socket_path,
    )


@router.get("/agent/capabilities", response_model=schemas.Capabilities, tags=["agent"])
def get_agent_capabilities(request: Request) -> schemas.Capabilities:
    """What the agent can actually do, re-probed if it is answering.

    A capability absent from this list is not available. A capability listed
    ``unavailable`` is not available either, and ``reason`` says why.
    """
    context = _context(request)
    try:
        hello = context.coordinator.handshake()
    except AgentError as exc:
        recorded_agent = context.ingestor.agents.most_recent()
        recorded = (
            context.ingestor.agents.capabilities(recorded_agent.agent_instance_id)
            if recorded_agent
            else []
        )
        return schemas.Capabilities(
            reachable=False,
            source="recorded",
            as_of=_now(),
            agent_instance_id=recorded_agent.agent_instance_id if recorded_agent else None,
            error=schemas.ErrorBody(**exc.as_dict()),
            capabilities=[views.capability_view(c) for c in recorded],
        )

    return schemas.Capabilities(
        reachable=True,
        source="live",
        as_of=_now(),
        agent_instance_id=hello["agent"]["agent_instance_id"],
        error=None,
        capabilities=[
            views.capability_view_from_payload(c) for c in hello.get("capabilities", [])
        ],
    )


# ------------------------------------------------------------------------------ network


@router.get(
    "/network/interfaces", response_model=schemas.NetworkInterfaceList, tags=["network"]
)
def list_network_interfaces(request: Request) -> schemas.NetworkInterfaceList:
    """Every network interface object LocalPlane has recorded on this host.

    Reads the store. It does not observe: what comes back is what was last seen, with the
    freshness of each observation and the sweep it came from attached, so a caller can
    tell current truth from remembered truth.
    """
    context = _context(request)
    host_id = _resolve_host_id(context)
    latest = context.ingestor.sweeps.latest(
        host_id, CAPABILITY_NETWORK_OBSERVE, scope="inventory"
    )
    latest_members = (
        context.ingestor.sweeps.object_ids(latest.sweep_id) if latest is not None else None
    )
    records = context.ingestor.objects.list_by_kind(host_id, OBJECT_KIND_NETWORK_INTERFACE)
    intents = context.management.intents.active_for([r.object_id for r in records])
    # The provider readings are fetched once for the whole list and reused. Ownership is
    # still derived per object — there is no stored verdict to read — but the evidence
    # behind it is the same evidence for every interface in one response.
    evidence = context.provenance.evidence(host_id)
    interfaces = []
    for r in records:
        provenance = context.provenance.for_object(r, evidence)
        interfaces.append(
            views.interface_view(
                r,
                context.settings.freshness_ttl_s,
                latest_members,
                intent=intents.get(r.object_id),
                reconciliation=context.management.reconciliation_for(r, intents.get(r.object_id)),
                provenance=provenance,
                eligibility=context.provenance.eligibility(r, provenance),
            )
        )
    return schemas.NetworkInterfaceList(
        host_id=host_id,
        last_sweep=views.sweep_view(latest) if latest else None,
        count=len(interfaces),
        interfaces=interfaces,
    )


@router.get(
    "/network/interfaces/{object_id}",
    response_model=schemas.NetworkInterface,
    tags=["network"],
)
def get_network_interface(object_id: str, request: Request) -> schemas.NetworkInterface:
    context = _context(request)
    record = _require_interface(context, object_id)
    latest = context.ingestor.sweeps.latest(
        record.host_id, CAPABILITY_NETWORK_OBSERVE, scope="inventory"
    )
    latest_members = (
        context.ingestor.sweeps.object_ids(latest.sweep_id) if latest is not None else None
    )
    intent = context.management.intents.active_for([record.object_id]).get(record.object_id)
    provenance = context.provenance.for_object(record)
    return views.interface_view(
        record,
        context.settings.freshness_ttl_s,
        latest_members,
        intent=intent,
        reconciliation=context.management.reconciliation_for(record, intent),
        provenance=provenance,
        eligibility=context.provenance.eligibility(record, provenance),
    )


@router.get(
    "/network/interfaces/{object_id}/protection",
    response_model=schemas.ObjectProtection,
    tags=["network"],
)
def get_network_interface_protection(
    object_id: str, request: Request
) -> schemas.ObjectProtection:
    """Whether this interface is protected, why, and on what evidence.

    Pure: derived for this request from evidence LocalPlane already holds, contacting
    nothing and writing nothing.

    **A separate axis from ownership.** Ownership asks whose this object is; protection asks
    what changing it would put at risk. An interface can be LocalPlane's own and protected,
    or externally configured and not protected at all, and neither answer implies the other
    — a Run against a Docker-configured bridge can truthfully publish `protection: clear`
    while remaining blocked because Docker demonstrably configures it.

    One protection reason is implemented: the management path. `implemented_reasons` says
    so out loud, because `clear` means "these were evaluated and none applied" and a reader
    is entitled to know what "these" were.

    The answer depends on the connection asking. Read over a transport that cannot establish
    a management path — from loopback, or through a reverse proxy — every interface is
    `unknown`, including the ones obviously not carrying it. Marking the rest clear while
    the path itself is unresolved would be inventing a negative from an absence.
    """
    context = _context(request)
    record = _require_interface(context, object_id)
    verdict, _transport = _management_path(context, request, record.host_id)
    return views.object_protection_view(
        assess_resource_protection(verdict, resource_id=record.object_id),
        object_id=record.object_id,
        object_name=record.display_name,
    )


@router.get(
    "/network/interfaces/{object_id}/evidence",
    response_model=schemas.Evidence,
    tags=["network"],
)
def get_network_interface_evidence(object_id: str, request: Request) -> schemas.Evidence:
    """The raw sysfs values and netlink objects the newest observation was derived from.

    A separate resource because it is large and rarely wanted — but it is available,
    because a claim about an operator's host that cannot be checked is worth less than one
    that can.
    """
    context = _context(request)
    record = _require_interface(context, object_id)
    found = context.ingestor.objects.evidence(object_id)
    if found is None or record.observation is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "no_observation",
                "message": "this object has never been observed",
                "detail": {"object_id": object_id},
            },
        )
    observation_id, evidence = found
    return schemas.Evidence(
        object_id=object_id,
        observation_id=observation_id,
        observed_at=datetime.fromisoformat(record.observation.observed_at),
        evidence=evidence,
    )


@router.get(
    "/network/interfaces/{object_id}/provenance",
    response_model=schemas.Provenance,
    tags=["network"],
)
def get_network_interface_provenance(object_id: str, request: Request) -> schemas.Provenance:
    """Who made this interface, who configures it, and the evidence for both.

    A separate resource from the interface because the evidence is heavy: the exact Docker
    network a bridge's address belongs to, the NetworkManager connection bound to a device,
    the daemon addresses that matched. The interface itself carries the conclusion and the
    reason codes; this carries what they rest on, plus every source that was consulted —
    including the ones that answered "nothing to do with me", because a settled question
    and an unexamined one are different states and both are worth seeing.

    Computed for this request. Ownership is never stored: it is a function of the newest
    observation and the newest provider readings, and a recorded verdict would be a second
    copy of something that moves whenever either does.
    """
    context = _context(request)
    record = _require_interface(context, object_id)
    provenance = context.provenance.for_object(record)
    return views.provenance_view(
        record,
        provenance,
        context.provenance.eligibility(record, provenance),
        context.settings.freshness_ttl_s,
    )


@router.post(
    "/network/observations/refresh",
    response_model=schemas.RefreshResult,
    tags=["network"],
)
def refresh_network_observations(
    request: Request,
    names: list[str] | None = Query(
        default=None,
        description=(
            "Optional interface names to observe. A name that does not exist on the host "
            "is returned in `missing`; nothing is invented for it."
        ),
    ),
) -> schemas.RefreshResult:
    """Observe the host's network interfaces now and take ownership of the result.

    Read-only with respect to the host: this reads sysfs and rtnetlink and writes only
    LocalPlane's own store. No interface is created, modified or brought up or down.
    """
    context = _context(request)
    try:
        result = context.coordinator.refresh_network(names)
    except AgentError as exc:
        raise HTTPException(status_code=_status_for(exc), detail=exc.as_dict()) from exc
    return schemas.RefreshResult(
        sweep_id=result.sweep_id,
        host_id=result.host_id,
        agent_instance_id=result.agent_instance_id,
        status=result.status,
        object_count=result.object_count,
        observation_count=result.observation_count,
        missing=result.missing,
        issues=[schemas.ProviderIssue(**issue) for issue in result.issues],
        providers=[schemas.ProviderReadingSummary(**p) for p in result.providers],
    )


# ------------------------------------------------------------------------------- systemd


def _recorded_systemd_capability(
    context: AppContext, host_id: str
) -> schemas.Capability | None:
    agent = context.ingestor.agents.most_recent(host_id)
    if agent is None:
        return None
    record = next(
        (
            capability
            for capability in context.ingestor.agents.capabilities(agent.agent_instance_id)
            if capability.capability == CAPABILITY_SYSTEMD_UNITS_OBSERVE
        ),
        None,
    )
    return views.capability_view(record) if record is not None else None


@router.get("/systemd/units", response_model=schemas.SystemdUnitList, tags=["systemd"])
def list_systemd_units(request: Request) -> schemas.SystemdUnitList:
    """Stored loaded-unit observations.  Pure: this never contacts systemd or the agent."""
    context = _context(request)
    host_id = _resolve_host_id(context)
    records = context.ingestor.objects.list_by_kind(host_id, OBJECT_KIND_SYSTEMD_UNIT)
    sweep = context.ingestor.sweeps.latest(
        host_id, CAPABILITY_SYSTEMD_UNITS_OBSERVE, scope="inventory"
    )
    current_members = (
        context.ingestor.sweeps.object_ids(sweep.sweep_id) if sweep is not None else set()
    )
    triggers = views.systemd_trigger_context(records, current_members)
    return schemas.SystemdUnitList(
        host_id=host_id,
        capability=_recorded_systemd_capability(context, host_id),
        last_sweep=views.sweep_view(sweep) if sweep else None,
        count=len(records),
        units=[
            views.systemd_unit_view(
                record,
                context.settings.freshness_ttl_s,
                current_members if sweep is not None else None,
                active_socket_triggers=triggers.get(record.object_id, ()),
            )
            for record in records
        ],
    )


@router.get(
    "/systemd/units/{object_id}", response_model=schemas.SystemdUnit, tags=["systemd"]
)
def get_systemd_unit(object_id: str, request: Request) -> schemas.SystemdUnit:
    """One stored unit by LocalPlane object id, never by caller-supplied unit name."""
    context = _context(request)
    record = _require_systemd_unit(context, object_id)
    sweep = context.ingestor.sweeps.latest(
        record.host_id, CAPABILITY_SYSTEMD_UNITS_OBSERVE, scope="inventory"
    )
    current_members = (
        context.ingestor.sweeps.object_ids(sweep.sweep_id) if sweep is not None else set()
    )
    estate = context.ingestor.objects.list_by_kind(
        record.host_id, OBJECT_KIND_SYSTEMD_UNIT
    )
    triggers = views.systemd_trigger_context(estate, current_members)
    return views.systemd_unit_view(
        record,
        context.settings.freshness_ttl_s,
        current_members if sweep is not None else None,
        active_socket_triggers=triggers.get(record.object_id, ()),
    )


@router.post(
    "/systemd/observations/refresh",
    response_model=schemas.SystemdObservationResult,
    tags=["systemd"],
)
def refresh_systemd_observations(request: Request) -> schemas.SystemdObservationResult:
    """Read the fixed loaded-unit scope and store it.  No body, filter or D-Bus fields."""
    context = _context(request)
    try:
        result = context.coordinator.refresh_systemd_units()
    except AgentError as exc:
        raise HTTPException(status_code=_status_for(exc), detail=exc.as_dict()) from exc
    detail = result.detail
    containment = detail.get("agent_unit_resolution")
    return schemas.SystemdObservationResult(
        host_id=result.host_id,
        sweep_id=result.sweep_id,
        status=result.status,
        unit_count=result.object_count,
        issues=[schemas.ProviderIssue(**issue) for issue in result.issues],
        provider_version=result.providers[0]["version"] if result.providers else "unknown",
        listed_count=detail.get("listed_count"),
        selected_count=detail.get("selected_count"),
        inventory_limit=detail.get("inventory_limit"),
        inventory_complete=detail.get("inventory_complete"),
        truncated=detail.get("truncated"),
        cap_reached=detail.get("cap_reached"),
        inventory_method=detail.get("inventory_method"),
        agent_unit_resolution=(
            schemas.SystemdAgentContainment(**containment)
            if isinstance(containment, dict)
            else None
        ),
    )


# ------------------------------------------------------------------------ docker containers


def _require_container(context: AppContext, object_id: str) -> ObjectRecord:
    record = context.ingestor.objects.get(object_id)
    if record is None or record.kind != OBJECT_KIND_DOCKER_CONTAINER:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "object_not_found",
                "message": f"no docker container object with id {object_id}",
                "detail": {"object_id": object_id},
            },
        )
    return record


def _require_systemd_unit(context: AppContext, object_id: str) -> ObjectRecord:
    record = context.ingestor.objects.get(object_id)
    if record is None or record.kind != OBJECT_KIND_SYSTEMD_UNIT:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "object_not_found",
                "message": f"no systemd unit object with id {object_id}",
                "detail": {"object_id": object_id},
            },
        )
    return record


@router.get(
    "/docker/containers",
    response_model=schemas.DockerContainerList,
    tags=["docker"],
)
def list_docker_containers(request: Request) -> schemas.DockerContainerList:
    """Every container LocalPlane has observed on this host, newest observation each.

    Pure: this reads what the last sweep recorded and contacts neither the agent nor the
    Docker daemon. `POST /docker/containers/observations/refresh` is how a fresh reading is
    taken, exactly as it is for interfaces.

    Docker is authoritative for all of this and LocalPlane keeps no copy it maintains — what
    is here is the newest observation, with its own freshness, of what the daemon said.
    """
    context = _context(request)
    host_id = _resolve_host_id(context)
    records = context.ingestor.objects.list_by_kind(host_id, OBJECT_KIND_DOCKER_CONTAINER)
    sweep = context.ingestor.sweeps.latest(
        host_id, CAPABILITY_DOCKER_CONTAINERS_OBSERVE, scope="inventory"
    )
    current_members = (
        context.ingestor.sweeps.object_ids(sweep.sweep_id) if sweep is not None else None
    )
    evidence = context.provenance.evidence(host_id)
    return schemas.DockerContainerList(
        host_id=host_id,
        last_sweep=views.sweep_view(sweep) if sweep else None,
        count=len(records),
        containers=[
            _container_view(context, record, current_members, evidence)
            for record in records
        ],
    )


@router.get(
    "/docker/containers/{object_id}",
    response_model=schemas.DockerContainer,
    tags=["docker"],
)
def get_docker_container(object_id: str, request: Request) -> schemas.DockerContainer:
    """One container: what it is, what it is doing, and what it is attached to.

    Enough that an operator does not need `docker ps` or `docker inspect` for routine
    inspection — image and digest, creation and start times, restart policy, health, port
    bindings, mounts, attached networks and the labels worth operating on.
    """
    context = _context(request)
    record = _require_container(context, object_id)
    sweep = context.ingestor.sweeps.latest(
        record.host_id, CAPABILITY_DOCKER_CONTAINERS_OBSERVE, scope="inventory"
    )
    current_members = (
        context.ingestor.sweeps.object_ids(sweep.sweep_id) if sweep is not None else None
    )
    return _container_view(
        context, record, current_members, context.provenance.evidence(record.host_id)
    )


@router.post(
    "/docker/containers/observations/refresh",
    response_model=schemas.ContainerObservationResult,
    tags=["docker"],
)
def refresh_container_observations(request: Request) -> schemas.ContainerObservationResult:
    """Ask the Docker daemon about every container on this host now, and record the result.

    Read-only with respect to the host: this lists and inspects, and there is no method on
    the path it takes that could start, stop, create or remove anything. No body and no
    parameter — there is nothing to select, because the sweep is of the estate.
    """
    context = _context(request)
    try:
        result = context.coordinator.refresh_containers()
    except AgentError as exc:
        raise HTTPException(status_code=_status_for(exc), detail=exc.as_dict()) from exc
    return schemas.ContainerObservationResult(
        host_id=result.host_id,
        sweep_id=result.sweep_id,
        status=result.status,
        container_count=result.object_count,
        issues=[schemas.ProviderIssue(**issue) for issue in result.issues],
        provider_version=result.providers[0]["version"] if result.providers else "unknown",
    )


@router.post(
    "/docker/containers/{object_id}/logs",
    response_model=schemas.ContainerLogs,
    tags=["docker"],
)
def read_container_logs(
    object_id: str,
    request: Request,
    tail: int = Query(
        default=DOCKER_LOG_LINES_DEFAULT,
        ge=1,
        le=DOCKER_LOG_LINES_MAX,
        description=(
            "How many recent lines to return, clamped to the agent's own ceiling. There is "
            "no `follow` and no `since`: this is a bounded read, not a stream."
        ),
    ),
) -> schemas.ContainerLogs:
    """The most recent lines this container wrote.

    `POST` rather than `GET` for the reason every observation in this API is a `POST`: it
    contacts the host. LocalPlane stores no copy of a container's output — Docker keeps the
    logs, and building a second copy of them would be a log platform rather than a control
    plane. Read-only with respect to the host, and bounded by lines and by bytes.
    """
    context = _context(request)
    record = _require_container(context, object_id)
    try:
        answer = context.client.container_logs(record.identity_value, tail=tail)
    except AgentError as exc:
        raise HTTPException(status_code=_status_for(exc), detail=exc.as_dict()) from exc
    return views.container_logs_view(record.object_id, answer["logs"])


@router.post(
    "/docker/containers/{object_id}/stats",
    response_model=schemas.ContainerStats,
    tags=["docker"],
)
def read_container_stats(object_id: str, request: Request) -> schemas.ContainerStats:
    """One current sample of what this container is using.

    A snapshot, and deliberately only a snapshot: there is no history, no series and no
    retention, because a metrics database is a product this build does not have and a field
    on a container is not the place to grow one. Read-only with respect to the host, and a
    `POST` for the same reason the log read is.
    """
    context = _context(request)
    record = _require_container(context, object_id)
    try:
        answer = context.client.container_stats(record.identity_value)
    except AgentError as exc:
        raise HTTPException(status_code=_status_for(exc), detail=exc.as_dict()) from exc
    return views.container_stats_view(record.object_id, answer["stats"])


def _container_view(
    context: AppContext, record: ObjectRecord, latest_inventory_members: set[str] | None, evidence
) -> schemas.DockerContainer:
    provenance = context.provenance.for_object(record, evidence)
    return views.container_view(
        record,
        context.settings.freshness_ttl_s,
        latest_inventory_members,
        provenance=provenance,
        eligibility=context.provenance.eligibility(record, provenance),
    )


# ---------------------------------------------------------------------- management path


_MANAGEMENT_PATH_NOTE_RECORDED = (
    "Read-only with respect to the host. The kernel was asked which route it would use to "
    "reach this connection's peer — an RTM_GETROUTE netlink query, a question — and nothing "
    "was created, modified, brought up or brought down."
)
_MANAGEMENT_PATH_NOTE_NOT_RECORDED = (
    "Nothing was recorded and the host was not contacted: this transport cannot establish a "
    "management path, so there was no evidence to take. A record that looks like evidence "
    "and proves nothing is worse than no record at all."
)


def _management_path_response(
    context: AppContext,
    host_id: str,
    verdict: ManagementPathVerdict,
    transport: TransportEvidence,
) -> schemas.ManagementPath:
    names = (
        context.ingestor.objects.display_names([verdict.resource_id])
        if verdict.resource_id
        else {}
    )
    return views.management_path_view(
        verdict,
        host_id=host_id,
        transport=transport,
        object_name=names.get(verdict.resource_id or "", None),
        evidence=context.management_path.evidence(verdict),
        ttl_s=context.management_path.freshness_ttl_s,
    )


@router.post(
    "/management-path/observations/refresh",
    response_model=schemas.ManagementPathObservationResult,
    tags=["management-path"],
)
def refresh_management_path(request: Request) -> schemas.ManagementPathObservationResult:
    """Observe how *this* request reached LocalPlane, and record what the kernel says.

    **There is no request body and no parameter, and that is the design.** The peer and the
    local endpoint come from the server side of this connection — the accepted socket — and
    the route lookup's destination is the peer LocalPlane read there. A caller cannot name a
    peer address, a local address, an interface, a route target, a command or an argv,
    because there is nothing here to name them to. `X-Forwarded-For`, `X-Real-IP` and
    `Forwarded` are read by nothing on this path, and no setting turns them on: a management
    path proven from something the caller wrote is a request that has been believed rather
    than evidence that has been gathered.

    **Read-only with respect to the host.** The one thing this contacts is the kernel's
    routing table, through a typed agent method that sends `RTM_GETROUTE` and can send
    nothing else. No interface, address, route, rule or service is created, modified or
    removed: this path reaches none of the agent's mutating methods, which belong to the
    apply of a Run and to nothing here.

    **A request that cannot prove anything records nothing.** From loopback, from a reverse
    proxy on this host, or over a transport whose endpoints cannot be read, the answer is
    `unresolved` with the reason saying which, and no row is written — a record that looks
    like evidence, sorts as the newest evidence and proves nothing is worse than none.

    Confirming the path needs two independent sources to agree: the local address this
    connection terminated on must be present on exactly one currently observed object, and
    the kernel's route to the peer must leave by that same object. Where they disagree the
    answer is `unresolved`, because choosing between two disagreeing sources is how a
    control plane reaches a confident wrong answer.
    """
    context = _context(request)
    host_id = _resolve_host_id(context)
    transport = transport_of(request)
    verdict = context.management_path.observe(host_id, transport)
    return schemas.ManagementPathObservationResult(
        host_id=host_id,
        host_mutated=False,
        host_effect="none",
        recorded=transport.usable,
        note=(
            _MANAGEMENT_PATH_NOTE_RECORDED
            if transport.usable
            else _MANAGEMENT_PATH_NOTE_NOT_RECORDED
        ),
        management_path=_management_path_response(context, host_id, verdict, transport),
    )


@router.get("/management-path", response_model=schemas.ManagementPath, tags=["management-path"])
def get_management_path(request: Request) -> schemas.ManagementPath:
    """Which object carries the connection this request arrived on, from what is recorded.

    Pure. It contacts no agent, queries no kernel, refreshes no evidence and writes nothing
    — not a row, not a timestamp. Reading where you are connected from must not be able to
    change what LocalPlane has recorded, or the record stops being a record of the host and
    becomes a record of who was looking.

    The answer depends on the connection asking, deliberately. Evidence taken from one
    operator's session answers for that session and no other: not for a second operator, not
    for automation calling over loopback, and not for the same operator arriving on a
    different address. Where nothing recorded matches this connection, the answer is
    `unresolved` with `management_path_unobserved`, and the remedy is to observe.
    """
    context = _context(request)
    host_id = _resolve_host_id(context)
    verdict, transport = _management_path(context, request, host_id)
    return _management_path_response(context, host_id, verdict, transport)


# ------------------------------------------------------------------------- observations


@router.get("/observations/sweeps", response_model=schemas.SweepList, tags=["observations"])
def list_sweeps(
    request: Request, limit: int = Query(default=20, ge=1, le=200)
) -> schemas.SweepList:
    context = _context(request)
    host_id = _resolve_host_id(context)
    sweeps = context.ingestor.sweeps.recent(host_id, limit)
    return schemas.SweepList(
        host_id=host_id, count=len(sweeps), sweeps=[views.sweep_view(s) for s in sweeps]
    )


# --------------------------------------------------------------------------- management

# Adopt and release are the only two POSTs here that change LocalPlane's stance towards an
# object, and neither of them is a host operation. The refusal codes are enumerated rather
# than collapsed into one, because "this is a loopback device" and "nobody has looked at it
# recently enough" have completely different remedies.
_REFUSAL_STATUS: dict[str, int] = {
    "object_not_found": 404,
    "object_observe_only": 409,
    "already_managed": 409,
    "not_managed": 409,
    "no_observation": 409,
    "observation_stale": 409,
    "controlled_values_unverified": 409,
    # Revision refusals. A caller that cannot tell "you named a field LocalPlane does not
    # control" from "somebody revised this while you were deciding" will offer the wrong
    # remedy, so neither is folded into the other.
    "empty_revision": 400,
    "unsupported_field": 400,
    "invalid_field_value": 400,
    "field_not_controlled": 409,
    "revision_changes_nothing": 409,
    "intent_revision_conflict": 409,
    "intent_schema_unsupported": 409,
    "observation_source_incompatible": 409,
    # Ownership refusals. Distinct codes because the remedies differ: an object Docker
    # runs will never be adoptable by this build, while conflicting claims mean two
    # systems disagree and somebody has to look.
    "externally_configured": 409,
    "externally_created": 409,
    "conflicting_ownership_claims": 409,
    "active_intent_missing": 500,
    # Run planning. A plan is refused when there is no honest one to make, and the codes
    # say which: "the value you want is the one it already has" and "nobody has looked at
    # this recently enough" send an operator to completely different places.
    "current_value_unreadable": 409,
    "already_reconciled": 409,
    "unsupported_operation": 400,
    "run_not_found": 404,
    "run_not_cancellable": 409,
    # The write boundary. Every one of these is a refusal to write, and they are separate
    # codes because the remedies are: confirm the plan, plan again, wait for the other run,
    # prove where you are connected from, or look at an object nobody can prove is safe.
    "run_not_confirmable": 409,
    "confirmation_preview_mismatch": 409,
    "preview_digest_mismatch": 409,
    "confirmation_not_required": 409,
    "confirmation_method_unsupported": 409,
    "confirmation_not_acknowledged": 400,
    "confirmation_already_satisfied": 409,
    "confirmation_required": 409,
    "confirmation_already_consumed": 409,
    "run_not_appliable": 409,
    "object_write_locked": 409,
    "preview_stale": 409,
    "preview_not_executable": 409,
    "execution_blocked": 409,
    "execution_not_implemented": 409,
    "target_is_management_path": 409,
    "management_path_unproven": 409,
    "execution_identity_unreadable": 409,
    # Arming is an execution in its own right, and its failure is LocalPlane's own fault
    # rather than the caller's. The run ends `failed` before the boundary and no change
    # exists, which is what the response says.
    "checkpoint_not_written": 500,
    "change_not_found": 404,
}

_ADOPT_NOTE = (
    "LocalPlane recorded the values this interface already had as its intended state. "
    "Nothing was written to the host: no link was brought up or down, no MTU was set, and "
    "this transition reaches none of the agent's mutating methods — those belong to the "
    "apply of a Run."
)
_RELEASE_NOTE = (
    "LocalPlane stopped retaining intent for this interface. Nothing was written to the "
    "host and nothing was restored — release is not a rollback. The interface is exactly "
    "as it was, and every intent version is kept as history."
)


_REVISE_NOTE = (
    "LocalPlane recorded a new version of what it intends for this interface. Nothing was "
    "written to the host: no link was brought up or down, no MTU was set, and this "
    "revision reaches none of the agent's mutating methods — those belong to the apply of "
    "a Run. If the runtime and the intent now agree, the intent is what moved."
)
_ADOPT_RUNTIME_NOTE = (
    "LocalPlane recorded the values this interface already has as its intended state, "
    "replacing the ones it was holding. Nothing was written to the host — every value here "
    "was read from it — and the version this replaced is kept as history."
)


def _refused(exc: ManagementRefused | RunRefused) -> HTTPException:
    return HTTPException(status_code=_REFUSAL_STATUS.get(exc.code, 409), detail=exc.as_dict())


@router.post(
    "/network/interfaces/{object_id}/adopt",
    response_model=schemas.TransitionResult,
    tags=["management"],
)
def adopt_network_interface(object_id: str, request: Request) -> schemas.TransitionResult:
    """Adopt this interface: ``observed → managed``.

    Records the currently verified values of the fields LocalPlane can be answerable for —
    ``admin_up`` and ``mtu`` — as a new intent version, and starts comparing against them.

    **This does not change the host.** Adoption is LocalPlane agreeing to notice, not
    LocalPlane taking control: the values it stores are the ones that were already there.
    It is refused rather than approximated when the object is observe-only, when it is
    already managed, when the newest observation is too old, when a value it would control
    could not be read — or when another system is demonstrably running the object.

    That last refusal is what the ownership axis is for. A bridge the Docker daemon created
    and configures, an interface NetworkManager holds an active profile for: LocalPlane has
    no write model that could share either of them, so it declines to become answerable
    rather than retaining an intent it could not honour. The refusal names the owner and
    the evidence.
    """
    context = _context(request)
    record = _require_interface(context, object_id)
    try:
        outcome = context.management.adopt(record)
    except ManagementRefused as exc:
        raise _refused(exc) from exc
    return schemas.TransitionResult(
        transition=outcome.transition,
        object_id=outcome.object_id,
        host_id=outcome.host_id,
        from_state=outcome.from_state,
        to_state=outcome.to_state,
        host_mutated=False,
        host_effect=outcome.host_effect,
        note=_ADOPT_NOTE,
        transition_id=outcome.transition_id,
        occurred_at=datetime.fromisoformat(outcome.occurred_at),
        intent=views.intent_view(outcome.intent, outcome.intent.intent_id),
        reconciliation=(
            views.reconciliation_view(outcome.reconciliation, outcome.intent)
            if outcome.reconciliation is not None
            else None
        ),
        ownership=(
            views.ownership_view(
                outcome.provenance,
                # The state the object is in *now*, which is why this reads
                # 'already_managed': it is the same question asked a moment later.
                derive_adoption_eligibility(outcome.to_state, outcome.provenance),
            )
            if outcome.provenance is not None
            else None
        ),
    )


@router.post(
    "/network/interfaces/{object_id}/release",
    response_model=schemas.TransitionResult,
    tags=["management"],
)
def release_network_interface(object_id: str, request: Request) -> schemas.TransitionResult:
    """Release this interface: ``managed → observed``.

    Drops the retained intent so LocalPlane stops being answerable for the object, and
    resolves any drift it had open about it — because a claim that the runtime disagrees
    with the intent stops being true when there is no intent.

    **This does not change the host, and it is not a rollback.** Nothing is restored,
    because nothing was ever applied. An interface released while it is down stays down.
    Every intent version is retained as history.
    """
    context = _context(request)
    record = _require_interface(context, object_id)
    open_before = [f.finding_id for f in context.management.findings.open_for_object(object_id)]
    try:
        outcome = context.management.release(record)
    except ManagementRefused as exc:
        raise _refused(exc) from exc
    return schemas.TransitionResult(
        transition=outcome.transition,
        object_id=outcome.object_id,
        host_id=outcome.host_id,
        from_state=outcome.from_state,
        to_state=outcome.to_state,
        host_mutated=False,
        host_effect=outcome.host_effect,
        note=_RELEASE_NOTE,
        transition_id=outcome.transition_id,
        occurred_at=datetime.fromisoformat(outcome.occurred_at),
        intent=views.intent_view(outcome.intent, None),
        reconciliation=None,
        findings_resolved=open_before,
    )


def _revision_result(
    outcome: RevisionOutcome, note: str, eligibility_state: str
) -> schemas.IntentRevisionResult:
    return schemas.IntentRevisionResult(
        kind=str(outcome.kind),
        object_id=outcome.object_id,
        host_id=outcome.host_id,
        management=schemas.Management(
            state=outcome.management_state, reason=outcome.management_reason
        ),
        host_mutated=False,
        host_effect=outcome.host_effect,
        note=note,
        revision_id=outcome.revision_id,
        occurred_at=datetime.fromisoformat(outcome.occurred_at),
        previous_intent=views.intent_view(outcome.previous_intent, outcome.intent.intent_id),
        intent=views.intent_view(outcome.intent, outcome.intent.intent_id),
        changed_fields=[
            schemas.FieldRevision(
                field=change.field,
                value_type=str(change.value_type),
                was=change.was,
                now=change.now,
            )
            for change in outcome.plan.changed
        ],
        carried_forward=list(outcome.plan.carried_forward),
        reconciliation=views.reconciliation_view(outcome.reconciliation, outcome.intent),
        findings_resolved=[f.finding_id for f in outcome.findings_resolved],
        findings_opened=[f.finding_id for f in outcome.findings_opened],
        ownership=views.ownership_view(
            outcome.provenance,
            derive_adoption_eligibility(eligibility_state, outcome.provenance),
        ),
    )


@router.post(
    "/network/interfaces/{object_id}/intent/revise",
    response_model=schemas.IntentRevisionResult,
    tags=["management"],
)
def revise_network_interface_intent(
    object_id: str, body: schemas.ExplicitIntentRevisionRequest, request: Request
) -> schemas.IntentRevisionResult:
    """Retain new desired values for an interface LocalPlane already manages.

    This is the operator saying what they now want. It writes a **new immutable intent
    version**, moves the active-intent pointer to it and leaves the version it replaced
    exactly as it was — the whole chain stays readable, so what was intended at any point
    and why it stopped being intended are both still answerable.

    **This does not change the host, and it is not an apply.** An MTU revised to 1400 is an
    MTU nobody has set; if the interface already carries 1400 the two now agree, and they
    agree because the intent moved. That is the second truthful answer to drift, and it is
    the answer LocalPlane uses when the machine is right and the declaration is out of date.

    Management does not move: the object was ``managed`` before and is ``managed`` after.
    Only fields the active intent already controls may be given a value — widening what
    LocalPlane is answerable for is an adoption decision, made against verified evidence,
    not something a request body may do. A field left out keeps the value it had.

    Refused rather than approximated when the object is not managed, when the version named
    in ``expected_intent_id`` is no longer in force, when the newest observation is too old
    to have decided against, when a name is not one LocalPlane controls, when a value is of
    the wrong type, when the request would retain exactly what is already retained — or
    when another system is demonstrably running the object, which is the same gate adopt
    applies and for the same reason.
    """
    context = _context(request)
    record = _require_interface(context, object_id)
    try:
        outcome = context.management.revise_intent(
            record,
            fields=body.fields,
            expected_intent_id=body.expected_intent_id,
            expected_version=body.expected_version,
        )
    except ManagementRefused as exc:
        raise _refused(exc) from exc
    return _revision_result(outcome, _REVISE_NOTE, outcome.management_state)


@router.post(
    "/network/interfaces/{object_id}/intent/adopt-runtime",
    response_model=schemas.IntentRevisionResult,
    tags=["management"],
)
def adopt_runtime_as_network_interface_intent(
    object_id: str, body: schemas.IntentRevisionRequest, request: Request
) -> schemas.IntentRevisionResult:
    """Make what this interface currently has the retained desired state.

    A separate endpoint from ``revise`` on purpose. The two are different acts — one is a
    statement about what should be, the other a statement that what *is* was right all
    along — and folding them into one request with a mode flag would leave a history in
    which nobody could tell afterwards which had happened.

    Only currently verified values are taken, and only for the fields the active intent
    already controls. A controlled field the newest observation could not read refuses the
    whole revision: "adopt what is there" cannot be said about a value nobody could see,
    and filling it in from the version being replaced would be inventing agreement.

    **This does not change the host.** Every value recorded here was read from it, which is
    why the result is ``in_sync`` and why nothing needed applying to get there. Any drift
    this settles is resolved as ``intent_revised`` — never as though the runtime had been
    put right.
    """
    context = _context(request)
    record = _require_interface(context, object_id)
    try:
        outcome = context.management.adopt_runtime_as_intent(
            record,
            expected_intent_id=body.expected_intent_id,
            expected_version=body.expected_version,
        )
    except ManagementRefused as exc:
        raise _refused(exc) from exc
    return _revision_result(outcome, _ADOPT_RUNTIME_NOTE, outcome.management_state)


@router.get(
    "/network/interfaces/{object_id}/intent",
    response_model=schemas.Intent,
    tags=["management"],
)
def get_network_interface_intent(object_id: str, request: Request) -> schemas.Intent:
    """The intent currently in force for this interface.

    404 when there is none. That is not an error condition — an observed object is a
    perfectly healthy thing to be — but "no intent" and "an intent controlling nothing"
    are different answers and this resource only has one of them to give.
    """
    context = _context(request)
    record = _require_interface(context, object_id)
    intent = context.management.intents.active_for([object_id]).get(object_id)
    if intent is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "no_active_intent",
                "message": "LocalPlane retains no intent for this object",
                "detail": {
                    "object_id": object_id,
                    "management_state": record.management_state,
                },
            },
        )
    return views.intent_view(intent, record.active_intent_id)


@router.get(
    "/network/interfaces/{object_id}/intent/history",
    response_model=schemas.IntentHistory,
    tags=["management"],
)
def get_network_interface_intent_history(
    object_id: str, request: Request
) -> schemas.IntentHistory:
    """Every intent version ever retained for this interface, and every transition.

    Release does not delete anything, so a released object still answers here with what it
    was managed as, and the transition that ended it.
    """
    context = _context(request)
    record = _require_interface(context, object_id)
    intents = context.management.intents.history(object_id)
    return schemas.IntentHistory(
        object_id=object_id,
        active_intent_id=record.active_intent_id,
        count=len(intents),
        intents=[views.intent_view(i, record.active_intent_id) for i in intents],
        transitions=[
            views.transition_view(t) for t in context.management.intents.transitions(object_id)
        ],
    )


@router.get(
    "/network/interfaces/{object_id}/reconciliation",
    response_model=schemas.ReconciliationResult,
    tags=["management"],
)
def get_network_interface_reconciliation(
    object_id: str, request: Request
) -> schemas.ReconciliationResult:
    """Compare this interface's retained intent with its newest observation.

    Computed for this request, not read from a column. An object that is not managed
    answers ``reconciliation: null`` with a 200: "there is nothing to compare" is the
    correct answer, and it is not the same as ``in_sync``.
    """
    context = _context(request)
    record = _require_interface(context, object_id)
    intent = context.management.intents.active_for([object_id]).get(object_id)
    result = context.management.reconciliation_for(record, intent)
    return schemas.ReconciliationResult(
        object_id=object_id,
        management=schemas.Management(
            state=record.management_state, reason=record.management_reason
        ),
        reconciliation=(
            views.reconciliation_view(result, intent)
            if result is not None and intent is not None
            else None
        ),
        intent=views.intent_summary_view(intent) if intent is not None else None,
    )


# ----------------------------------------------------------------------------- findings


@router.get("/findings", response_model=schemas.FindingList, tags=["findings"])
def list_findings(
    request: Request,
    status: str = Query(
        default="open",
        pattern="^(open|resolved|all)$",
        description="open — currently claimed; resolved — history; all — both.",
    ),
    object_id: str | None = Query(
        default=None, description="Restrict to one object's findings."
    ),
    limit: int = Query(default=100, ge=1, le=500),
) -> schemas.FindingList:
    """LocalPlane's claims about this host.

    A finding is an interpretation, and it is not the same thing as a state: an object's
    ``reconciliation`` is recomputed on every read, while a finding remembers when the
    disagreement started and survives being resolved. Re-observing the same disagreement
    updates one finding rather than adding another, and a finding that stops being true is
    resolved with the reason — never deleted.

    Two kinds are reported here, and ``evidence`` is shaped by ``finding_type``. Drift is a
    managed object disagreeing with its retained intent. An ownership conflict is a managed
    object another system is demonstrably running — which is a safety claim rather than a
    configuration one, and is never raised for an object LocalPlane merely watches.
    """
    context = _context(request)
    host_id = _resolve_host_id(context)
    wanted = None if status == "all" else status

    if object_id is not None:
        records = [
            f
            for f in context.management.findings.history_for_object(object_id, limit)
            if wanted is None or f.status == wanted
        ]
        conflicts = [
            f
            for f in context.management.ownership_findings.history_for_object(object_id, limit)
            if wanted is None or f.status == wanted
        ]
    else:
        records = context.management.findings.list_for_host(host_id, wanted, limit)
        conflicts = context.management.ownership_findings.list_for_host(host_id, wanted, limit)

    names = context.ingestor.objects.display_names(
        [f.object_id for f in records] + [f.object_id for f in conflicts]
    )
    findings = [
        views.finding_view(f, names.get(f.object_id, f.object_id)) for f in records
    ] + [views.ownership_finding_view(f, names.get(f.object_id, f.object_id)) for f in conflicts]
    findings.sort(key=lambda f: f.first_seen_at, reverse=True)
    findings = findings[:limit]
    return schemas.FindingList(
        host_id=host_id,
        status=status,
        count=len(findings),
        findings=findings,
    )


@router.get("/findings/{finding_id}", response_model=schemas.Finding, tags=["findings"])
def get_finding(finding_id: str, request: Request) -> schemas.Finding:
    context = _context(request)
    record = context.management.findings.get(finding_id)
    if record is not None:
        names = context.ingestor.objects.display_names([record.object_id])
        return views.finding_view(record, names.get(record.object_id, record.object_id))

    conflict = context.management.ownership_findings.get(finding_id)
    if conflict is not None:
        names = context.ingestor.objects.display_names([conflict.object_id])
        return views.ownership_finding_view(
            conflict, names.get(conflict.object_id, conflict.object_id)
        )

    raise HTTPException(
        status_code=404,
        detail={
            "code": "finding_not_found",
            "message": f"no finding with id {finding_id}",
            "detail": {"finding_id": finding_id},
        },
    )


# --------------------------------------------------------------------------------- runs


_RUN_PLANNED_NOTE = (
    "LocalPlane planned this operation and published the preview. Nothing was written to "
    "the host: no MTU was set, no link was touched, and this planning path reaches none of "
    "the agent's mutating methods. This is a Run, not a Change — no write boundary was "
    "crossed, so there is nothing for a Change record to be about."
)
_RUN_READ_NOTE = (
    "The plan below is what was published, not what would be decided now. Whether it still "
    "holds is in `preview.validity`, which is derived for this request from records "
    "LocalPlane already had. Nothing was written to the host, then or now."
)
_RUN_KEPT_NOTE = (
    "A guarded change is settled from evidence this request carried: the management path "
    "was re-proved over the object that was changed, or it was not and the guard's own "
    "account of what it did is what this run now records. Nothing was written from here."
)

_RUN_CANCELLED_NOTE = (
    "This run was cancelled before any write boundary. No Change was created, the host was "
    "not touched, the retained intent is unchanged, and no finding or reconciliation moved. "
    "The preview it published stays exactly as it was — a plan somebody decided against is "
    "still a record of what they were shown."
)


def _require_run(context: AppContext, run_id: str):
    run = context.runs.get(run_id)
    if run is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "run_not_found",
                "message": f"no run with id {run_id}",
                "detail": {"run_id": run_id},
            },
        )
    return run


def _require_target(context: AppContext, operation: OperationType, object_id: str):
    """The object this operation is to be planned against, or a 404.

    The kind it must be comes from the operation's own declaration rather than from this
    route, so an operation whose target is a service or a container resolves its object the
    same way without the endpoint learning what either of those is.
    """
    definition = context.runs.definition(operation)
    record = context.ingestor.objects.get(object_id)
    if record is None or record.kind != definition.target_kind:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "object_not_found",
                "message": f"no {definition.target_kind} object with id {object_id}",
                "detail": {"object_id": object_id, "target_kind": definition.target_kind},
            },
        )
    return record


def _definition_for(context: AppContext, operation: str):
    """The static contract of a Run's operation, or ``None`` if this build dropped it.

    A store outlives the code that wrote to it. A Run whose operation no longer exists is
    still readable — its published plan says what it would have done — and its validity is
    stale, which is the honest answer when nothing can evaluate it any more.
    """
    try:
        return context.runs.definition(OperationType(operation))
    except (ValueError, RunRefused):
        return None


#: The three operations whose planning rests on request-scoped systemd safety evidence, and
#: the closed action each one is. Declared once so that every path needing that evidence asks
#: for it the same way rather than each deciding for itself which operations are which.
_SYSTEMD_ACTIONS: dict[OperationType, SystemdServiceAction] = {
    OperationType.SYSTEMD_SERVICE_START: SystemdServiceAction.START,
    OperationType.SYSTEMD_SERVICE_STOP: SystemdServiceAction.STOP,
    OperationType.SYSTEMD_SERVICE_RESTART: SystemdServiceAction.RESTART,
}


def _lifecycle_context_for(context: AppContext, request: Request, run, management_path):
    """Fresh lifecycle evidence for *this* request, or ``None`` where the operation has none.

    Read-only, and acquired from the socket this request actually arrived on. A gate
    re-derived from evidence belonging to an older request would be a gate about then.
    """
    action = _SYSTEMD_ACTIONS.get(OperationType(run.operation))
    if action is None:
        return None
    record = context.ingestor.objects.get(run.object_id)
    if record is None:
        return None
    return context.systemd_lifecycle_context.observe(
        record=record,
        action=action,
        connection=request_connection_of(request),
        management_path=management_path,
    )


def _run_response(
    context: AppContext, run, note: str, management_path: ManagementPathVerdict
) -> schemas.Run:
    names = context.ingestor.objects.display_names([run.object_id])
    change = context.changes.change_for_run(run.run_id)
    return views.run_view(
        run,
        context.runs.published_plan(run),
        context.runs.validity(run, management_path),
        object_name=names.get(run.object_id, run.object_id),
        definition=_definition_for(context, run.operation),
        note=note,
        confirmation=context.changes.confirmation_for(run.run_id),
        self_impact_override=context.changes.self_impact_override_for(run.run_id),
        checkpoint=context.changes.checkpoint_for(run.run_id),
        guard=context.changes.guards.for_run(run.run_id),
        change=change,
        events=context.changes.transcript(run.run_id),
        write_locked=context.changes.lock_for(run.run_id) is not None,
    )


@router.post("/runs", response_model=schemas.Run, status_code=201, tags=["runs"])
def create_run(body: schemas.CreateRunRequest, request: Request) -> schemas.Run:
    """Plan one typed operation and publish its preview.

    **This endpoint does not change the host.** What it creates is a Run — the durable
    record that somebody asked what an operation would involve — together with an immutable
    preview answering it. Confirmation and apply remain separate generic endpoints. The
    systemd operations have a registered executor like the container operations do, so an
    eligible one proceeds through those endpoints to a real host write; planning here
    reaches none of it.

    The preview answers seven questions as precisely as current truth allows: what would
    change, why, how it would run, what evidence it rests on, what it risks, what would have
    to be verified afterwards and what could be recovered. Where LocalPlane does not know,
    it says `unknown` — most importantly about whether this interface carries the path the
    operator is reaching it over, which nothing in the current observation model can prove
    and which is therefore never guessed.

    **A preview can exist while execution is blocked, and that is the useful case.** An
    interface Docker configures still gets a plan saying "MTU 1400 → 1500", with the
    ownership conflict listed as a blocker. Refusing to describe it would be less
    informative and no safer.

    Planning is refused, with nothing written, when there is no honest plan to make: the
    object is not managed, its active intent does not control the field, nobody has
    observed it recently enough, the current value cannot be read — or the runtime already
    carries the intended value, which is a truthful no-op rather than a plan that would
    change nothing.

    The desired value is never taken from this request. It comes from the retained intent,
    and an operator who wants a different one revises the intent first.
    """
    context = _context(request)
    operation = OperationType(body.operation.type)
    record = _require_target(context, operation, body.operation.object_id)
    management_path, _transport = _management_path(context, request, record.host_id)
    lifecycle_context = None
    systemd_actions = _SYSTEMD_ACTIONS
    if operation in systemd_actions:
        # Explicit, read-only acquisition for the exact accepted request socket.  This is
        # the sole reason creating this kind of Run contacts the agent; the planner itself
        # remains pure, and the D-Bus lifecycle method belongs to the executor on apply.
        lifecycle_context = context.systemd_lifecycle_context.observe(
            record=record,
            action=systemd_actions[operation],
            connection=request_connection_of(request),
            management_path=management_path,
        )
        # The context read ingests its target through Slice 11A's targeted-observation
        # seam.  Plan against that exact newest generic record, not the older object that
        # entered the route.
        record = _require_target(context, operation, body.operation.object_id)
    try:
        outcome = context.runs.create(
            operation,
            record,
            management_path,
            systemd_lifecycle_context=lifecycle_context,
        )
    except RunRefused as exc:
        raise _refused(exc) from exc
    names = context.ingestor.objects.display_names([outcome.run.object_id])
    return views.run_view(
        outcome.run,
        outcome.plan,
        outcome.validity,
        object_name=names.get(outcome.run.object_id, outcome.run.object_id),
        definition=context.runs.definition(outcome.plan.operation),
        note=_RUN_PLANNED_NOTE,
    )


@router.get("/runs", response_model=schemas.RunList, tags=["runs"])
def list_runs(
    request: Request,
    state: str = Query(
        default="all",
        pattern="^(preview|cancelled|all)$",
        description=(
            "preview — Runs still in the preview state; cancelled — Runs cancelled before "
            "any write; all — every Run whatever state its lifecycle has reached. "
            "Publishing a plan leaves a Run in `preview`; confirming and applying one moves "
            "it through the rest of the lifecycle."
        ),
    ),
    object_id: str | None = Query(default=None, description="Restrict to one object."),
    limit: int = Query(default=50, ge=1, le=200),
) -> schemas.RunList:
    """Every Run on this host, newest first.

    None of them is a Change. Nothing here has written to the host, and the `validity` on
    each says whether the plan it published still describes what would happen.
    """
    context = _context(request)
    host_id = _resolve_host_id(context)
    # Decided once for the whole list, from this request's transport. Per-Run derivation
    # would answer the same question up to fifty times and could, in principle, answer it
    # differently within one response.
    management_path, _transport = _management_path(context, request, host_id)
    runs = context.runs.list_for_host(
        host_id,
        state=None if state == "all" else state,
        object_id=object_id,
        limit=limit,
    )
    names = context.ingestor.objects.display_names([r.object_id for r in runs])
    return schemas.RunList(
        host_id=host_id,
        state=state,
        count=len(runs),
        runs=[
            views.run_summary_view(
                run,
                context.runs.validity(run, management_path),
                object_name=names.get(run.object_id, run.object_id),
                change_id=(
                    change.change_id
                    if (change := context.changes.change_for_run(run.run_id))
                    else None
                ),
            )
            for run in runs
        ],
    )


@router.get("/runs/{run_id}", response_model=schemas.Run, tags=["runs"])
def get_run(run_id: str, request: Request) -> schemas.Run:
    """One Run and the plan it published.

    Pure. Reading a Run does not contact the host, does not refresh an observation, does
    not re-plan the stored preview and does not move a timestamp because somebody looked.
    `preview.validity` *is* derived for this request — from records LocalPlane already had,
    and writing nothing — because whether a plan still holds is a question about now.
    """
    context = _context(request)
    run = _require_run(context, run_id)
    management_path, _transport = _management_path(context, request, run.host_id)
    return _run_response(context, run, _RUN_READ_NOTE, management_path)


@router.get("/runs/{run_id}/preview", response_model=schemas.RunPreview, tags=["runs"])
def get_run_preview(run_id: str, request: Request) -> schemas.RunPreview:
    """The immutable plan this Run published.

    This is the document a future confirmation would be checked against: its
    `preview_digest` is a hash of a canonical form of the plan, so "the operator confirmed
    this" can one day be verified rather than assumed. It is stored, never rewritten — a
    trigger in the store refuses every update — so a stale plan stays readable exactly as
    it was published, and the remedy for staleness is a new Run.
    """
    context = _context(request)
    run = _require_run(context, run_id)
    management_path, _transport = _management_path(context, request, run.host_id)
    names = context.ingestor.objects.display_names([run.object_id])
    return views.run_preview_view(
        run,
        context.runs.published_plan(run),
        context.runs.validity(run, management_path),
        object_name=names.get(run.object_id, run.object_id),
        definition=_definition_for(context, run.operation),
        confirmation=context.changes.confirmation_for(run.run_id),
        self_impact_override=context.changes.self_impact_override_for(run.run_id),
    )


_RUN_CONFIRMED_NOTE = (
    "The confirmation this plan requires has been recorded for this run. Nothing was "
    "written to the host and no Change exists: a confirmation is permission to act, not an "
    "act. It is single-use, it names this run and this plan, and no token was issued that "
    "could authorise anything else."
)
_SELF_IMPACT_OVERRIDE_NOTE = (
    "A self-impact override has been recorded for this run. Nothing was written to the host "
    "and no Change exists. It does not soften the plan's protection verdict or remove any "
    "blocker: `protection` and `how.blockers` say exactly what they said before. It is "
    "single-use, it names this run and this plan, and it does not stand in for the "
    "confirmation this plan separately requires."
)
_RUN_APPLIED_NOTE = (
    "LocalPlane crossed the write boundary for this run. What became of the host is in "
    "`change` — `mutation.outcome` says whether the write is proven to have happened, "
    "proven not to have happened, or unknown, and `verification` says whether an "
    "independent reading proved the intended value afterwards. A successful acknowledgement "
    "is not success."
)


@router.post(
    "/runs/{run_id}/confirm", response_model=schemas.Run, tags=["runs"]
)
def confirm_run(run_id: str, body: schemas.ConfirmRunRequest, request: Request) -> schemas.Run:
    """Satisfy the confirmation this run's published plan requires.

    **The body names the preview, not just a digest.** Two identical concurrent plans share
    one digest, so a confirmation keyed on content could not say which of them an operator
    looked at, and a confirmation for one run must never authorise another. `preview_id` is
    required and must be the one this run published; a digest may be supplied as an
    optional cross-check.

    **Nothing is issued.** No token comes back and none is stored. The confirmation is a row
    naming this run and this plan, it is single-use, and it is consumed atomically by an
    apply of *this* run and by nothing else.

    **Nobody is identified.** The record says only that this request crossed the accepted
    authentication boundary rather than inventing an actor to attribute it to.

    A stale plan cannot be confirmed, and neither can a blocked one. Confirming work that
    could not proceed is how blocked work acquires the means to reach an apply review.

    Nothing about the host is touched here, and no Change is created: confirming is a
    decision about LocalPlane's own records, exactly as adopting and revising are.
    """
    context = _context(request)
    run = _require_run(context, run_id)
    management_path, _transport = _management_path(context, request, run.host_id)
    try:
        context.changes.confirm(
            run,
            preview_id=body.preview_id,
            acknowledge=body.acknowledge,
            acknowledge_object=body.acknowledge_object,
            expected_preview_digest=body.expected_preview_digest,
            management_path=management_path,
            systemd_lifecycle_context=_lifecycle_context_for(
                context, request, run, management_path
            ),
        )
    except RunRefused as exc:
        raise _refused(exc) from exc
    confirmed = _require_run(context, run_id)
    return _run_response(context, confirmed, _RUN_CONFIRMED_NOTE, management_path)


@router.post(
    "/runs/{run_id}/self-impact-override", response_model=schemas.Run, tags=["runs"]
)
def grant_self_impact_override(
    run_id: str, body: schemas.SelfImpactOverrideRequest, request: Request
) -> schemas.Run:
    """Accept that carrying this plan out may interrupt LocalPlane itself.

    **A second, separate authority, and it is not a confirmation.** Confirming answers "do
    you want this change". This answers a different question about a different fact: this
    plan's effect closure reaches the infrastructure LocalPlane's own backend depends on, so
    executing it may take LocalPlane away — temporarily for a restart, indefinitely for a
    stop — and nothing here has verified another way to reach this host. Both are required,
    neither substitutes for the other, and the write boundary demands each through its own
    database trigger.

    **It is offered for one hazard and one shape of evidence.** The plan's `how.eligibility`
    must be `self_impact_override_required`, which a plan reaches only where its self-impact
    derivation is `proven` under `docker-direct-unix-v1`, the closure touches the management
    path through the backend's runtime owner and nothing else, the agent is proven to be a
    host-side service outside that closure, and no gap remains. A `possible` impact is
    surfaced and never authorised. An unresolved one, an agent in a container, an incomplete
    effect graph, a provider whose owner could not be resolved, an applicability failure, a
    stale preview, a digest mismatch or any blocker a later build adds all leave the plan
    `blocked` — and a blocked plan has no override to grant.

    **There is nothing to force and nothing to name.** No `force`, no `ignore_safety`, no
    list of blockers to bypass: a caller cannot choose what is being overridden, because the
    backend derived whether this exact published plan is eligible for this one authority.
    Nor is any display text echoed back — everything the acknowledgement is about is in the
    immutable preview this request names, bound by the digest it optionally cross-checks.

    **Protection does not move.** `protected` stays `protected` and `unknown` stays
    `unknown`; every blocker stays published; the risk tier and the confirmation requirement
    are untouched. Granting this opens a path, it does not soften a verdict.

    **It is single-use and bound to one plan.** One grant per run, ever; it names the
    preview that run published; it is re-validated against fresh evidence when granted and
    again when spent; and it is consumed atomically with the apply confirmation by the one
    attempt that uses it. Nothing is issued and no token comes back.
    """
    context = _context(request)
    run = _require_run(context, run_id)
    management_path, _transport = _management_path(context, request, run.host_id)
    try:
        context.changes.grant_self_impact_override(
            run,
            preview_id=body.preview_id,
            acknowledge=body.acknowledge,
            expected_preview_digest=body.expected_preview_digest,
            management_path=management_path,
            systemd_lifecycle_context=_lifecycle_context_for(
                context, request, run, management_path
            ),
        )
    except RunRefused as exc:
        raise _refused(exc) from exc
    granted = _require_run(context, run_id)
    return _run_response(context, granted, _SELF_IMPACT_OVERRIDE_NOTE, management_path)


@router.post("/runs/{run_id}/apply", response_model=schemas.Run, tags=["runs"])
def apply_run(run_id: str, request: Request) -> schemas.Run:
    """Execute this run's published plan. One of the two endpoints that can write.

    The other is `POST /changes/{id}/recovery/retry`, which re-attempts the end state of a
    change that could not be settled. Both dispatch through the same executors, and neither
    takes a request body.

    **There is no request body, and that is the design.** Every authoritative value is
    already named by the run: the object, the operation, the intent version, the field and
    both ends of the change. There is no parameter here for an MTU, a desired value, an
    interface, an object id, a command, an argv, an executable, a provider, a shell, a
    patch or a field set — not because they are validated away, but because there is
    nothing to name them to. An operator who wants a different value revises the intent and
    plans a new run.

    **Everything is re-proved for this request before anything is armed.** The object is
    re-read, the published preview must itself say execution was available and this plan
    eligible, its validity is re-derived under *this* request's management-path evidence,
    and every execution gate — ownership, protection, capability, and that the target is
    proven **not** to be the path this request arrived over — is checked again against
    current truth. Nothing is silently replanned: the remedy for a stale run is a new run.

    **A second concurrent apply against the same object and field is refused**, with a typed
    conflict, by a durable lock in the database rather than a mutex in one process.

    **The order after that is the safety argument.** The confirmation is consumed
    atomically; a checkpoint holding the value to restore is written and committed *before*
    anything else can be; only then does a Change come into existence; and only then is the
    mutation dispatched. A checkpoint that cannot be written ends the run `failed` with no
    Change, because nothing about the host could have moved.

    **What comes back is what is known.** `change.mutation.outcome` is `not_written`,
    `written` or `write_unknown`, and the third is never converted into either of the others
    by reading the value back — that answers what the host holds now, which is a different
    question from whether this write occurred. A `written` mutation is verified by a fresh
    observation through the ordinary path before the run can succeed, and a verification
    that cannot prove the intended value sends the run to restoration. A restoration that
    cannot be proven ends in `recovery_required`, which is a truthful ending rather than an
    error, and holds this object's write lock until somebody looks.
    """
    context = _context(request)
    run = _require_run(context, run_id)
    management_path, _transport = _management_path(context, request, run.host_id)
    try:
        outcome = context.changes.apply(
            run, management_path,
            systemd_lifecycle_context=_lifecycle_context_for(
                context, request, run, management_path
            ),
        )
    except RunRefused as exc:
        raise _refused(exc) from exc
    return _run_response(context, outcome.run, _RUN_APPLIED_NOTE, management_path)


@router.post("/runs/{run_id}/guard/keep", response_model=schemas.Run, tags=["runs"])
def keep_guarded_run(
    run_id: str, body: schemas.KeepGuardedRunRequest, request: Request
) -> schemas.Run:
    """Keep a guarded change, by proving over the changed object that you are still here.

    **The proof is this request.** A guarded change is one made to the object carrying the
    operator's own management path, and nothing LocalPlane can read from the host settles
    whether that path survived it — the value comes back perfectly over a link nobody can
    talk to. What settles it is a request that *arrived*, whose own transport re-establishes
    the management path by the same two-source rule the path is always proven by and
    resolves it to the very object that was changed. That evidence travels over the path
    under test, which is what makes it a proof.

    So a request that cannot prove it can never keep the change — not because it is refused
    a permission, but because the one thing it would have to establish is the one thing it
    has failed to establish. It is refused with `guard_connection_not_proved` and the guard
    goes on holding.

    **This endpoint cannot write to the host**, and there is no parameter through which it
    could. The only body field is a statement of intent; it names no object, no value, no
    interface, no route, no command and no deadline. What it can do is *prevent* a write:
    releasing the guard is what stops the reversal that would otherwise happen.

    **Doing nothing is a decision, and it is the safe one.** If the window passes with no
    such proof, the agent on the host dispatches the reversal on its own — without this
    process, without this connection and without being asked again. Calling this afterwards
    does not undo that; it collects what the guard did, reads the target back, and ends the
    run truthfully: `rolled_back` where a reading proves the previous value is back,
    `recovery_required` where nothing does.
    """
    context = _context(request)
    run = _require_run(context, run_id)
    management_path, _transport = _management_path(context, request, run.host_id)
    try:
        outcome = context.changes.guard_keep(
            run, management_path, acknowledge=body.acknowledge
        )
    except RunRefused as exc:
        raise _refused(exc) from exc
    return _run_response(context, outcome.run, _RUN_KEPT_NOTE, management_path)


@router.post("/runs/{run_id}/cancel", response_model=schemas.Run, tags=["runs"])
def cancel_run(run_id: str, request: Request) -> schemas.Run:
    """Cancel a Run before the write boundary.

    The easy cancellation, and the only one that exists: nothing has been written, so
    nothing has to be put back. **No Change is created** — LocalPlane's first invariant is that a
    cancelled run carries no change record, and here there is not even a table one could
    live in. The host is untouched, the retained intent is untouched, reconciliation and
    findings are untouched, and the published preview remains inspectable as the history it
    is.

    This says nothing about cancelling an execution. Cancelling during an apply means the
    effect of the interrupted step is unknown and the run goes to recovery; cancelling
    during a rollback is not a rollback. Neither is designed here, and neither is
    reachable — there is no state to interrupt.
    """
    context = _context(request)
    run = _require_run(context, run_id)
    management_path, _transport = _management_path(context, request, run.host_id)
    try:
        cancelled = context.runs.cancel(run)
    except RunRefused as exc:
        raise _refused(exc) from exc
    return _run_response(context, cancelled, _RUN_CANCELLED_NOTE, management_path)


# ------------------------------------------------------------------------------- changes


@router.get("/changes", response_model=schemas.ChangeList, tags=["changes"])
def list_changes(
    request: Request,
    object_id: str | None = Query(default=None, description="Only changes to this object."),
    result: str | None = Query(
        default=None,
        description=(
            "in_flight | succeeded | failed | rolled_back | recovery_required. "
            "`recovery_required` is the one worth watching."
        ),
    ),
    limit: int = Query(default=100, ge=1, le=500),
) -> schemas.ChangeList:
    """Every Change on this host, newest first. Pure: reading history changes nothing."""
    context = _context(request)
    host_id = _resolve_host_id(context)
    records = context.changes.list_changes(
        host_id, object_id=object_id, result=result, limit=limit
    )
    names = context.ingestor.objects.display_names([r.object_id for r in records])
    holds = context.changes.recovery_states(records)
    return schemas.ChangeList(
        host_id=host_id,
        count=len(records),
        changes=[
            views.change_summary_view(
                record,
                object_name=names.get(record.object_id, record.object_id),
                recovery_state=str(holds[record.change_id]),
            )
            for record in records
        ],
    )


@router.get("/changes/{change_id}", response_model=schemas.Change, tags=["changes"])
def get_change(change_id: str, request: Request) -> schemas.Change:
    """One Change, its apply transcript, and what LocalPlane does and does not know.

    Pure. Reading a Change contacts no host, refreshes no observation and writes nothing —
    including for a Change that ended in `recovery_required`, where the temptation to "check
    whether it is fine now" is exactly the read that must not silently become a write.
    """
    context = _context(request)
    record = context.changes.get_change(change_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "change_not_found",
                "message": f"no change with id {change_id}",
                "detail": {"change_id": change_id},
            },
        )
    return _change_response(context, record)


def _require_change(context: AppContext, change_id: str) -> ChangeRecord:
    record = context.changes.get_change(change_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "change_not_found",
                "message": f"no change with id {change_id}",
                "detail": {"change_id": change_id},
            },
        )
    return record


def _change_response(context: AppContext, record: ChangeRecord) -> schemas.Change:
    """One Change and everything a reader needs to act on it, from persisted truth only."""
    names = context.ingestor.objects.display_names([record.object_id])
    return views.change_view(
        record,
        object_name=names.get(record.object_id, record.object_id),
        events=context.changes.transcript(record.run_id),
        write_locked=context.changes.lock_for(record.run_id) is not None,
        hold=context.changes.recovery_hold(record),
        attempts=context.changes.recovery_history(record),
        authority=context.changes.recovery_confirmation_for(record),
    )


@router.post(
    "/changes/{change_id}/recovery/confirm", response_model=schemas.Change, tags=["changes"]
)
def confirm_change_recovery(
    change_id: str, body: schemas.RecoveryConfirmRequest, request: Request
) -> schemas.Change:
    """Authorise one recovery retry of this change to write to the host again.

    **The confirmation that authorised the original apply is not reusable and this is not
    it.** That one authorised an attempt and the attempt happened. This is a second, separate
    grant, recorded in the same table under the same single-use rule, consumed by exactly one
    retry — and at most one may be outstanding at a time, so authority cannot be accumulated.

    **A retry does not need this to begin.** The first thing a retry does is take a fresh
    reading and ask the operation whether it already proves the end state; one that does
    completes with no host mutation and consumes nothing. Grant this when a retry has told you
    it cannot proceed without it, or in advance if you already know the write must happen.

    The current management-path gate applies here as it does to the retry itself: authority
    over a write that could remove the operator's own path is granted over a connection that
    has proven it is not the one at risk, or it is not granted.

    Nothing about the host is touched, and no Change is created or altered.
    """
    context = _context(request)
    record = _require_change(context, change_id)
    management_path, _transport = _management_path(context, request, record.host_id)
    try:
        context.changes.recovery_confirm(
            record,
            acknowledge=body.acknowledge,
            expected_recovery_reason=body.expected_recovery_reason,
            management_path=management_path,
        )
    except RunRefused as exc:
        raise _refused(exc) from exc
    return _change_response(context, _require_change(context, change_id))


@router.post(
    "/changes/{change_id}/recovery/retry", response_model=schemas.Change, tags=["changes"]
)
def retry_change_recovery(change_id: str, request: Request) -> schemas.Change:
    """Try again to reach the end state this change wanted. The way out of a recovery hold.

    **There is no request body, and that is the design.** Every authoritative value is
    already named by the change: the operation, the object, the field and both ends of it, or
    the verb and the state it promised. There is no parameter here for a value, a verb, a
    target, a provider, a command or a shell — not because they are validated away, but
    because there is nothing to name them to. A retry re-attempts *this* change; an operator
    who wants something else plans a new run.

    **It looks before it writes.** The first thing it does is re-read the target through the
    ordinary observation path and ask the operation whether that reading already proves the
    end state — an uncertain write may in fact have landed, a container may now be proven
    running, a restart may now be provable from the daemon's own record of when it last
    started. If it is proven, recovery completes with **no host mutation at all** and the
    object is given back. Writing again merely because somebody pressed retry would be a host
    change nobody needed.

    **Everything is re-proved for this request.** The management-path judgement comes from the
    transport this call arrived on, not from the failed run's; ownership, capability,
    observation currency and eligibility are re-derived against current truth; and a re-plan
    that would now produce a different desired value, verb or expected state is **refused**,
    because recovery may not substitute today's intent for the one the change was for.

    **A retry that must write needs authority nobody has spent.** It is refused with
    `recovery_confirmation_required` until one is granted, and the refusal is recorded as an
    attempt that provably wrote nothing.

    **The hold is kept through every ending but two.** `not_written`, `write_unknown` and a
    write that could not be proven all leave the question open, so the object stays held. The
    original change is never rewritten by any of this: it goes on saying it required recovery
    and goes on saying why.
    """
    context = _context(request)
    record = _require_change(context, change_id)
    management_path, _transport = _management_path(context, request, record.host_id)
    try:
        run = context.runs.get(record.run_id)
        context.changes.recovery_retry(
            record, management_path,
            systemd_lifecycle_context=(
                _lifecycle_context_for(context, request, run, management_path)
                if run is not None
                else None
            ),
        )
    except RunRefused as exc:
        raise _refused(exc) from exc
    return _change_response(context, _require_change(context, change_id))


@router.post(
    "/changes/{change_id}/recovery/resolve", response_model=schemas.Change, tags=["changes"]
)
def resolve_change_recovery(
    change_id: str, body: schemas.RecoveryResolveRequest, request: Request
) -> schemas.Change:
    """Release this change's recovery hold by hand. **No host mutation happens here.**

    The explicit human escape, and the store enforces what it may be: a resolution row may
    carry no confirmation, no dispatch, no mutation outcome, no host effect and no
    verification, so a build that decided otherwise would fail to write it.

    **It claims nothing.** Not that the change succeeded, not that the mutation happened, not
    that the host is safe, not that anything was rolled back, not that the intended state was
    reached. The original result and the original recovery reason are left saying exactly what
    they said and stay inspectable afterwards; what is added is that at this time a person
    released the hold.

    **Whatever can be observed is recorded beside it**, through the ordinary observation path,
    and recorded as what it is. If the reading happens to prove the end state the change
    wanted, the record says so and the outcome is still `resolved` — a human's decision is not
    silently upgraded into a verification. If nothing can be read, that stays visible rather
    than becoming an absence a reader could mistake for a clean result.

    `acknowledge_object` is the object's name, typed out. The operator types the held
    domain's name for the same reason: an escape from a safety hold should not be one
    accidental click, and the statement is recorded rather than merely required.

    Only the lock this change holds is released, and only while this change still holds it.
    """
    context = _context(request)
    record = _require_change(context, change_id)
    names = context.ingestor.objects.display_names([record.object_id])
    try:
        context.changes.recovery_resolve(
            record,
            acknowledge=body.acknowledge,
            operator_statement=body.acknowledge_object,
            object_name=names.get(record.object_id, record.object_id),
            note=body.note,
            expected_recovery_reason=body.expected_recovery_reason,
        )
    except RunRefused as exc:
        raise _refused(exc) from exc
    return _change_response(context, _require_change(context, change_id))
