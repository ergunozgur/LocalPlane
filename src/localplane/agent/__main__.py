"""Run the LocalPlane Agent.

    python -m localplane.agent

Environment:
    LOCALPLANE_AGENT_SOCKET   socket path (default: $XDG_RUNTIME_DIR/localplane/agent.sock)
    LOCALPLANE_SYSFS_NET      sysfs network root (default: /sys/class/net)
    LOCALPLANE_ROOT           filesystem root for host identity (default: /)
    LOCALPLANE_DOCKER_SOCKET  docker daemon socket (default: /var/run/docker.sock). Read
                              for containers and networks, and written by exactly three
                              declared container lifecycle verbs — nothing else.
    LOCALPLANE_LOG_LEVEL      default INFO
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from types import FrameType

from localplane.agent.providers.docker import DEFAULT_DOCKER_SOCKET
from localplane.agent.providers.linux_network import DEFAULT_SYSFS_NET
from localplane.agent.server import AgentServer, default_socket_path
from localplane.agent.service import AgentService
from localplane.log import configure_logging

LOG = logging.getLogger("localplane.agent")


def main(argv: list[str] | None = None) -> int:
    configure_logging(os.environ.get("LOCALPLANE_LOG_LEVEL", "INFO"))
    socket_path = os.environ.get("LOCALPLANE_AGENT_SOCKET") or str(default_socket_path())
    stop_requested = False

    def _stop(signum: int, _frame: FrameType | None) -> None:
        nonlocal stop_requested
        if stop_requested:
            return
        LOG.info("agent stopping", extra={"signal": signum})
        stop_requested = True

    # Install these before creating the listening socket, so a startup signal
    # is recorded and the socket is still cleaned up if initialization finishes.
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    service = AgentService(
        root=os.environ.get("LOCALPLANE_ROOT", "/"),
        sysfs_net=os.environ.get("LOCALPLANE_SYSFS_NET", DEFAULT_SYSFS_NET),
        docker_socket=os.environ.get("LOCALPLANE_DOCKER_SOCKET", DEFAULT_DOCKER_SOCKET),
    )
    try:
        server = AgentServer(socket_path, service)
    except (OSError, RuntimeError) as exc:
        LOG.error("agent could not start", extra={"socket": socket_path, "error": str(exc)})
        return 1

    try:
        # Do not call BaseServer.shutdown from the same thread as
        # serve_forever: it waits for that loop and deadlocks. A short accept
        # poll is only lifecycle coordination; request-handler timeouts remain
        # unchanged in LineServer.
        server.timeout = 0.1
        while not stop_requested:
            server.handle_request()
    finally:
        server.server_close()
        LOG.info("agent stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
