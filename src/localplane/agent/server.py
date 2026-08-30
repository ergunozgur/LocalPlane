"""AF_UNIX transport for the agent.

The socket, the framing and the peer-credential check are :mod:`localplane.ipc`, shared with
the privileged helper's channel. What this file adds is which codec the agent speaks and
which uids may reach it — by default only the uid running it.

The transport fails closed in both directions: a frame that cannot be validated is answered
with a code rather than a guess, and an unexpected exception inside a handler becomes
``internal_error`` rather than a dropped connection a client would have to interpret.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from localplane import ipc
from localplane.agent.service import AgentService
from localplane.protocol.wire import CODEC

LOG = logging.getLogger("localplane.agent.server")

DEFAULT_SOCKET_MODE = ipc.DEFAULT_SOCKET_MODE
DEFAULT_DIR_MODE = ipc.DEFAULT_DIR_MODE
DEFAULT_READ_TIMEOUT_S = ipc.DEFAULT_READ_TIMEOUT_S


def default_socket_path() -> Path:
    """``$XDG_RUNTIME_DIR/localplane/agent.sock``, falling back to ``/run/localplane``.

    A per-user runtime directory is preferred because it is already mode 0700 and is cleaned
    up by the system, which keeps a stale socket from outliving the agent.
    """
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime_dir) if runtime_dir else Path("/run")
    return base / "localplane" / "agent.sock"


class AgentServer(ipc.LineServer):
    """The agent's listening socket."""

    def __init__(
        self,
        socket_path: str | Path,
        service: AgentService,
        allowed_uids: set[int] | None = None,
        socket_mode: int = DEFAULT_SOCKET_MODE,
        read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
    ) -> None:
        self.service = service
        super().__init__(
            socket_path,
            CODEC,
            service.handle,
            name="localplane-agent",
            allowed_uids=allowed_uids,
            socket_mode=socket_mode,
            read_timeout_s=read_timeout_s,
        )
        LOG.info(
            "agent listening",
            extra={
                "socket": str(self.socket_path),
                "allowed_uids": sorted(self.allowed_uids),
                "agent_instance_id": service.instance_id,
                "capabilities": {c.capability: str(c.status) for c in service.capabilities},
            },
        )
