"""The FastAPI application.

Startup does two things beyond opening the store: it reaches for the agent, and it takes
one observation. Both are allowed to fail. A backend that refuses to start because the
agent is not running cannot tell anyone *that* the agent is not running, which is exactly
the moment an operator needs it to be up.
"""

from __future__ import annotations

import logging
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
from localplane.backend.context import AppContext
from localplane.backend.db.database import Database, open_database

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


def create_app(
    settings: Settings | None = None,
    database: Database | None = None,
    *,
    authentication: Authentication | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    owns_database = database is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active_authentication = authentication or Authentication(
            load_master_secret(settings.auth_secret_path),
            bind_host=settings.bind_host,
            development_origin=settings.development_origin,
        )
        app.state.authentication = active_authentication
        db = database if database is not None else open_database(settings.database_path)
        context = AppContext.build(settings, db)
        app.state.context = context
        LOG.info(
            "backend started",
            extra={
                "database": str(db.path),
                "agent_socket": str(settings.agent_socket),
                "version": __version__,
            },
        )
        _settle_interrupted_changes(context)
        _initial_observation(context, settings)
        try:
            yield
        finally:
            if owns_database:
                db.close()
            LOG.info("backend stopped")

    app = FastAPI(
        title="LocalPlane",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(session_router)
    app.include_router(router)

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
