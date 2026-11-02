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
    LOCALPLANE_TLS_CERT_PATH      operator-supplied PEM certificate chain
    LOCALPLANE_TLS_KEY_PATH       matching unencrypted private key (0400 or 0600)
    LOCALPLANE_CONSOLE_DIR        optional built web/dist directory
"""

from __future__ import annotations

import os
import sys

import uvicorn

from localplane.backend.app import create_app
from localplane.backend.config import Settings
from localplane.backend.serving_files import ServingConfigurationError
from localplane.backend.tls import server_config, validate_transport
from localplane.log import configure_logging


def main(argv: list[str] | None = None) -> int:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    try:
        validate_transport(settings)
        config = server_config(settings, create_app(settings), int(os.environ.get("LOCALPLANE_PORT", "8080")))
    except ServingConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    uvicorn.Server(config).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
