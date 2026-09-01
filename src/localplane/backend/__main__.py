"""Run the LocalPlane backend.

    python -m localplane.backend

Environment:
    LOCALPLANE_DB_PATH            store location (default: var/localplane.db)
    LOCALPLANE_AUTH_SECRET_PATH   master credential file (default: var/localplane-master.secret)
    LOCALPLANE_DEVELOPMENT_ORIGIN one additional exact Vite development origin
    LOCALPLANE_AGENT_SOCKET       agent socket (default: $XDG_RUNTIME_DIR/localplane/agent.sock)
    LOCALPLANE_AGENT_TIMEOUT_S    default 10
    LOCALPLANE_FRESHNESS_TTL_S    seconds before an observation reads stale (default 60)
    LOCALPLANE_OBSERVE_ON_STARTUP set 0 to start without taking an observation
    LOCALPLANE_HOST               bind address (default 127.0.0.1)
    LOCALPLANE_PORT               bind port (default 8080)
    LOCALPLANE_LOG_LEVEL          default INFO
"""

from __future__ import annotations

import os
import sys

import uvicorn

from localplane.backend.app import create_app
from localplane.backend.config import Settings
from localplane.log import configure_logging


def main(argv: list[str] | None = None) -> int:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    uvicorn.run(
        create_app(settings),
        # Loopback remains the only supported HTTP browser-session topology. Authentication
        # does not promote remote HTTP or make it safe by weakening cookie policy.
        host=settings.bind_host,
        port=int(os.environ.get("LOCALPLANE_PORT", "8080")),
        log_config=None,
        access_log=False,
        # Lifecycle connection evidence is derived from the accepted socket.  Uvicorn's
        # proxy-header rewriting would replace that kernel peer with caller-supplied HTTP
        # data, so it is disabled explicitly rather than relying on a default.
        proxy_headers=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
