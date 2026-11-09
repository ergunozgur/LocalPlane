"""The FastAPI application.

Startup does two things beyond opening the store: it reaches for the agent, and it takes
one observation. Both are allowed to fail. A backend that refuses to start because the
agent is not running cannot tell anyone *that* the agent is not running, which is exactly
the moment an operator needs it to be up.
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse

from localplane import __version__
from localplane.backend.agent_client import AgentError
from localplane.backend.api.routes import router, session_router
from localplane.backend.auth import Authentication, load_master_secret, require_authentication
from localplane.backend.config import Settings
from localplane.backend.console import install_console, load_console
from localplane.backend.context import AppContext
from localplane.backend.db.database import Database, open_database
from localplane.backend.serving_files import ServingConfigurationError
from localplane.build_identity import (
    BuildIdentityError,
    console_identity_from_loaded,
    load_build_identity,
    require_usable,
    unavailable_console,
)
from localplane.maintenance import MaintenanceController, MaintenanceRefused

LOG = logging.getLogger("localplane.backend.app")

DESCRIPTION = """
LocalPlane's observation and management surface.

**Exactly two endpoints in this API can result in a host write:**
`POST /api/v1/runs/{id}/apply` and
`POST /api/v1/changes/{id}/recovery/retry`.
Everything else reads, or writes LocalPlane's own records. `POST
/api/v1/network/observations/refresh` reads sysfs and rtnetlink and asks Docker,
NetworkManager and Tailscale what they say they own; `POST
/api/v1/systemd/observations/refresh` reads the bounded loaded-unit estate through the
official systemd D-Bus API; `adopt`, `release`, `intent/revise`, `intent/adopt-runtime`,
`POST /runs`, `cancel` and `confirm` write records and nothing else.
No endpoint here brings a link up or down, configures an address, changes a route, a
firewall rule, a DNS setting, a sysctl or anything in any of those daemons.

`apply` can execute only the closed typed operation already fixed by its published Run:
reconcile a managed interface's retained MTU through the fixed helper path, or ask Docker
to start, stop or restart one validated container through Docker's own closed API, or ask
systemd to start, stop or restart one validated service unit through the official D-Bus API.
`recovery/retry` can only re-attempt the original Change's required end state, after a fresh
observation and any required authority. Neither endpoint takes a request body, so there is
no parameter for a value, target, operation, verb, interface, command or provider on either
surface.

Eight things are worth knowing before reading a response:

* **Management, ownership, reconciliation, health and freshness are independent.** A managed
  object can be failed. An observed object can be healthy. A drifted object can be perfectly
  healthy, and an in-sync one can be down.
* **Ownership is not a management state.** A bridge the Docker daemon runs is still
  `observed` — LocalPlane watches it — and what its ownership changes is
  `ownership.adoption`, which refuses to make LocalPlane answerable for it. Every claim is
  evidence-backed and none is made from an interface's name; `unknown` is a real answer and
  says which source left it that way.
* **`null` means unknown.** It is never a zero, an empty string or a default. The speed of
  a link with no carrier is `null` because the kernel does not know it.
* **Observed objects do not drift.** `reconciliation` is `null` unless the object is
  managed, which is not the same as `in_sync`. A managed object whose controlled value
  could not be read is `unknown`, which is not `drifted` either.
* **Drift and findings are different claims.** `reconciliation` is a comparison recomputed
  on every read. A finding is the durable record that LocalPlane noticed, when it first
  noticed, and how it ended.
* **Intent can be revised, and revising is not applying.** A managed object's desired state
  may be replaced by a new immutable version — either with values the operator supplies or
  by declaring the observed runtime correct. Management does not move, the version replaced
  is kept, and the host is not touched. A drift that ends this way is resolved
  `intent_revised`, never as though anything had been put right.
* **A Run is not a Change.** A Run plans a typed operation and publishes an immutable
  preview of what it would involve; a Change is the record that LocalPlane entered the path
  on which a host write may occur. Planning, confirming and arming all happen without a
  Change, because none of them can have moved anything. A preview may exist while execution
  is blocked — that is the useful case — and where LocalPlane cannot prove something, such
  as whether an interface carries the path it is being reached over, the preview says
  `unknown` rather than guessing, and execution stays blocked.
* **"Written" and "we do not know" are different answers.** A change carries
  `mutation.outcome` — `not_written`, `written` or `write_unknown` — and the third is never
  resolved by reading the value back: that answers what the host holds now, which is a
  different question from whether this write occurred. A kernel acknowledgement is also not
  success; only a fresh observation through the ordinary path can prove the intended value.
  A change that cannot be proved safe ends `recovery_required`, which is a truthful ending
  and not an error, and it holds the object's write lock until somebody looks.
"""


_CONTROL_WRITE_TEMPLATES = (
    "/api/v1/network/interfaces/{object_id}/adopt",
    "/api/v1/network/interfaces/{object_id}/release",
    "/api/v1/network/interfaces/{object_id}/intent/revise",
    "/api/v1/network/interfaces/{object_id}/intent/adopt-runtime",
    "/api/v1/runs",
    "/api/v1/runs/{run_id}/confirm",
    "/api/v1/runs/{run_id}/self-impact-override",
    "/api/v1/runs/{run_id}/apply",
    "/api/v1/runs/{run_id}/guard/keep",
    "/api/v1/runs/{run_id}/cancel",
    "/api/v1/changes/{change_id}/recovery/confirm",
    "/api/v1/changes/{change_id}/recovery/assess",
    "/api/v1/changes/{change_id}/recovery/retry",
    "/api/v1/changes/{change_id}/recovery/resolve",
)
"""Every route that can establish, confirm, dispatch or resolve authority."""

_RECORD_WRITE_TEMPLATES = (
    "/api/v1/agent",
    "/api/v1/agent/capabilities",
    "/api/v1/network/observations/refresh",
    "/api/v1/systemd/observations/refresh",
    "/api/v1/docker/containers/observations/refresh",
    "/api/v1/management-path/observations/refresh",
)
"""Routes that write LocalPlane's records even though they change no host state.

The agent hello endpoints are here because the accepted session decision records them as
state-changing GETs: they ingest the host and agent rows. An observation refresh is a
record write even when the provider read fails, because a failed sweep is itself recorded.
Neither may race an offline snapshot.
"""

_READ_POST_TEMPLATES = (
    "/api/v1/storage/filesystems/observe",
    "/api/v1/systemd/units/{object_id}/journal",
    "/api/v1/docker/containers/{object_id}/logs",
    "/api/v1/docker/containers/{object_id}/stats",
)
"""Read-only POSTs: an explicit observation or evidence read, retained in memory only."""


def _template_pattern(template: str) -> re.Pattern[str]:
    pieces = []
    for part in template.split("/"):
        if part.startswith("{") and part.endswith("}"):
            pieces.append(r"[^/]+")
        else:
            pieces.append(re.escape(part))
    return re.compile("^" + "/".join(pieces) + "$")


_CONTROL_WRITE_PATTERNS = tuple(_template_pattern(t) for t in _CONTROL_WRITE_TEMPLATES)
_RECORD_WRITE_PATTERNS = tuple(_template_pattern(t) for t in _RECORD_WRITE_TEMPLATES)
_READ_POST_PATTERNS = tuple(_template_pattern(t) for t in _READ_POST_TEMPLATES)


def classify_request(method: str, path: str) -> str:
    """Classify one request for maintenance admission.

    ``read`` may run under an active window; ``record`` and ``control`` may not. A new
    POST under the API defaults to ``record`` rather than ``read`` so forgetting to
    classify one cannot let it write into a snapshot. The API inventory test asserts every
    registered route is in an explicit list, so the default is a safety net, not a design.
    """
    verb = method.upper()
    path = path.rstrip("/") or "/"
    if path in {"/api/v1/session", "/api/v1/installation"}:
        return "read"
    if verb in {"GET", "HEAD"}:
        if path in {"/api/v1/agent", "/api/v1/agent/capabilities"}:
            return "record"
        return "read"
    if any(pattern.fullmatch(path) for pattern in _CONTROL_WRITE_PATTERNS):
        return "control"
    if any(pattern.fullmatch(path) for pattern in _RECORD_WRITE_PATTERNS):
        return "record"
    if any(pattern.fullmatch(path) for pattern in _READ_POST_PATTERNS):
        return "read"
    if path.startswith("/api/v1/"):
        return "record"
    return "read"


def create_app(
    settings: Settings | None = None,
    database: Database | None = None,
    *,
    authentication: Authentication | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    owns_database = database is None
    settings.validate_install()
    try:
        build_identity = load_build_identity(
            settings.release_manifest,
            process="backend",
            release_root=settings.release_root,
        )
        require_usable(build_identity)
    except BuildIdentityError as exc:
        raise ServingConfigurationError(
            "release_identity_invalid",
            f"The configured release manifest is unusable ({exc}). Refusing to serve with a false build identity.",
        ) from None
    maintenance = MaintenanceController(settings.maintenance_root(), service="backend")

    loaded_console = None
    console_identity = unavailable_console()
    if settings.console_dir is not None:
        loaded_console = load_console(settings.console_dir)
        index, assets = loaded_console
        console_identity = console_identity_from_loaded(
            index.body, {name: asset.body for name, asset in assets.items()}
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        lease = maintenance.service_lifetime() if maintenance.enabled else None
        try:
            with maintenance.startup_lease() as state:
                active_authentication = authentication or Authentication(
                    load_master_secret(settings.auth_secret_path),
                    bind_host=settings.bind_host,
                    development_origin=settings.development_origin,
                )
                app.state.authentication = active_authentication
                db = database if database is not None else open_database(settings.database_path)
                context = AppContext.build(
                    settings, db, maintenance=maintenance, build_identity=build_identity,
                    console_identity=console_identity,
                )
                app.state.context = context
                app.state.maintenance = maintenance
                LOG.info("backend started", extra={"database": str(db.path),
                         "agent_socket": str(settings.agent_socket), "version": __version__,
                         "build_status": build_identity.status, "build_id": build_identity.build_id,
                         "maintenance": state.status})
                if state.fenced or state.refusal is not None:
                    LOG.warning("startup observation skipped: maintenance fence is not clear",
                                extra={"maintenance": state.status})
                else:
                    _settle_interrupted_changes(context)
                    _initial_observation(context, settings)
            try:
                yield
            finally:
                if owns_database:
                    db.close()
                LOG.info("backend stopped")
        finally:
            if lease is not None:
                lease.release()

    app = FastAPI(
        title="LocalPlane",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.build_identity = build_identity
    app.state.console_identity = console_identity
    app.state.maintenance = maintenance
    app.include_router(session_router)
    app.include_router(router)

    @app.exception_handler(MaintenanceRefused)
    async def maintenance_refused(request: Request, exc: MaintenanceRefused):
        return JSONResponse(status_code=503, content={"error": exc.as_body()},
                            headers={"Cache-Control": "no-store"})

    @app.middleware("http")
    async def maintenance_admission(request: Request, call_next):
        """Hold the shared control lease for every record or control write.

        The lock is held for the whole synchronous handler, including the actual executor
        call inside an apply, and the agent takes the same lock around its own provider
        call. A maintenance transition's exclusive acquisition therefore drains writers in
        both processes rather than trusting one process's request counter or SIGTERM.
        """
        classification = classify_request(request.method, request.url.path)
        if classification == "read":
            return await call_next(request)
        controller = getattr(request.app.state, "maintenance", None)
        if controller is None or not controller.enabled:
            return await call_next(request)
        admission = controller.admit(f"{request.method.upper()} {request.url.path}")
        try:
            admission.__enter__()
        except MaintenanceRefused as exc:
            return JSONResponse(
                status_code=503,
                content={"error": exc.as_body()},
                headers={"Cache-Control": "no-store"},
            )
        try:
            return await call_next(request)
        finally:
            admission.__exit__(None, None, None)

    @app.middleware("http")
    async def journal_privacy_boundary(request: Request, call_next):
        # Covers auth/Origin, validation, missing-object and unexpected failures too,
        # not merely successful endpoint responses. Never echo rejected body input.
        sensitive = re.fullmatch(r"/api/v1/systemd/units/[^/]+/journal/?", request.url.path) is not None
        if not sensitive:
            return await call_next(request)
        try:
            result = await call_next(request)
            if result.status_code == 422:
                result = JSONResponse({"error": {"code": "invalid_journal_request", "message": "A strict sensitivity acknowledgment is required; no other parameters are accepted.", "detail": {}}}, status_code=422)
        except Exception:
            LOG.error("journal_request_failed", extra={"code": "journal_unavailable"})
            result = JSONResponse({"error": {"code": "journal_unavailable", "message": "Journal evidence is unavailable.", "detail": {}}}, status_code=500)
        result.headers["Cache-Control"] = "no-store"
        return result

    @app.get(
        "/openapi.json",
        dependencies=[Depends(require_authentication)],
        include_in_schema=False,
    )
    def protected_openapi() -> JSONResponse:
        return JSONResponse(app.openapi())

    @app.get(
        "/docs",
        dependencies=[Depends(require_authentication)],
        include_in_schema=False,
        response_class=HTMLResponse,
    )
    def protected_docs() -> HTMLResponse:
        return get_swagger_ui_html(openapi_url="/openapi.json", title="LocalPlane — Swagger UI")

    @app.get(
        "/redoc",
        dependencies=[Depends(require_authentication)],
        include_in_schema=False,
        response_class=HTMLResponse,
    )
    def protected_redoc() -> HTMLResponse:
        return get_redoc_html(openapi_url="/openapi.json", title="LocalPlane — ReDoc")

    app.add_exception_handler(HTTPException, _http_exception_handler)
    if settings.console_dir is not None:
        install_console(app, settings.console_dir, loaded=loaded_console)
    return app


def _settle_interrupted_changes(context: AppContext) -> None:
    """Interpret every Change that crossed the boundary and never recorded an outcome.

    Runs before anything else, because a store holding an unsettled Change is a store whose
    account of the host is incomplete, and every question asked of it until this has run
    would be answered from that incomplete account.

    The rule is conservative and it is the only safe one: dispatch began and nothing
    settled it means the write may have happened. Such a Change ends `recovery_required`
    holding its object's write lock. Nothing is restored here — writing to a host on the
    strength of a record nobody has looked at since a crash, without the evidence about the
    operator's own path that every other write requires, is not recovery.
    """
    _settle_outstanding_guards(context)
    settled = context.changes.settle_interrupted()
    if settled:
        LOG.warning(
            "interrupted changes settled on startup",
            extra={
                "count": len(settled),
                "changes": [
                    {
                        "change_id": c.change_id,
                        "run_id": c.run_id,
                        "outcome": c.mutation_outcome,
                        "result": c.result,
                    }
                    for c in settled
                ],
            },
        )
    _settle_interrupted_recovery(context)


def _settle_outstanding_guards(context: AppContext) -> None:
    """Ask what became of every connection guard nothing has heard back about.

    Runs **before** the interrupted-Change pass, because a guarded Run is deliberately
    unsettled — it wrote, it proved what it wrote, and what it is waiting for is a
    connection nobody in this process can produce. Reading its Change as "interrupted"
    without first asking the component that actually holds the guard would replace a fact
    LocalPlane established with one it did not.

    **It asks; it never releases, and it writes nothing to the host.** A backend coming back
    while a guard is still counting down must not cancel protection an operator is relying
    on. A guard still armed, and one that could not be interrogated at all, are both left
    exactly as they are: the first is doing its job, and the second has told LocalPlane
    nothing — an agent that has not finished starting is not a guard that is gone.
    """
    settled = context.changes.settle_interrupted_guards()
    if settled:
        LOG.warning(
            "connection guards settled on startup; nothing was written from here",
            extra={
                "count": len(settled),
                "guards": [
                    {
                        "guard_id": g.guard_id,
                        "run_id": g.run_id,
                        "phase": g.settled_phase,
                        "reversal_outcome": g.reversal_outcome,
                    }
                    for g in settled
                ],
            },
        )


def _settle_interrupted_recovery(context: AppContext) -> None:
    """Interpret every recovery attempt that began and never recorded what became of it.

    The recovery path has the same one-transaction crash window the apply path has, and it is
    read by the same rule: dispatch began and nothing came back is `write_unknown`.

    **The hold is kept either way, and nothing is retried here.** A recovery retry needs
    management-path evidence belonging to an operator request, and a restart is not one.
    Trying again on the strength of a record nobody has looked at since a crash would be the
    one thing recovery exists to make unnecessary.
    """
    settled = context.changes.settle_interrupted_recovery()
    if settled:
        LOG.warning(
            "interrupted recovery attempts settled on startup; every hold is kept",
            extra={
                "count": len(settled),
                "attempts": [
                    {
                        "attempt_id": a.attempt_id,
                        "change_id": a.change_id,
                        "run_id": a.run_id,
                        "dispatch_began": a.dispatch_began,
                        "mutation_outcome": a.mutation_outcome,
                        "outcome": a.outcome,
                    }
                    for a in settled
                ],
            },
        )


def _initial_observation(context: AppContext, settings: Settings) -> None:
    if not settings.observe_on_startup:
        return
    try:
        result = context.coordinator.refresh_network()
    except AgentError as exc:
        # Not fatal, and not hidden. The API will report the agent unreachable, and
        # /network/interfaces will return an empty list with no sweep to explain it.
        LOG.warning(
            "startup observation skipped: agent unavailable",
            extra={"code": exc.code, "error": exc.message, "socket": str(settings.agent_socket)},
        )
        return
    LOG.info(
        "startup observation complete",
        extra={"sweep_id": result.sweep_id, "status": result.status, "objects": result.object_count},
    )


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render every error in one structured shape.

    A caller should be able to branch on ``error.code`` without parsing prose, whether the
    failure came from a route, from the agent or from the framework.
    """
    assert isinstance(exc, HTTPException)
    detail: Any = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        body = {
            "code": detail["code"],
            "message": detail.get("message", ""),
            "detail": detail.get("detail", {}),
        }
    else:
        body = {"code": f"http_{exc.status_code}", "message": str(detail), "detail": {}}
    return JSONResponse(status_code=exc.status_code, content={"error": body}, headers=exc.headers)
