"""The privileged helper's socket: the privilege boundary itself.

The transport is :class:`localplane.ipc.LineServer`, shared with the agent's. What this file
adds is the two things that are the helper's own: which codec it speaks, and the default that
only the uid running the helper may reach it.

**Peer credentials are the authentication and there is no other.** ``SO_PEERCRED`` is a kernel
fact about the process on the other end — not a token, not a header, not a claim in a message
— which is exactly what makes it usable as a boundary in a product with no authentication.
The check happens before a byte of the connection is parsed, so an unauthorised caller cannot
even reach the parser. An empty allowlist refuses everybody.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from localplane import ipc
from localplane.helper.protocol import CODEC
from localplane.helper.service import HelperService

LOG = logging.getLogger("localplane.helper.server")

DEFAULT_SOCKET_MODE = ipc.DEFAULT_SOCKET_MODE
DEFAULT_READ_TIMEOUT_S = ipc.DEFAULT_READ_TIMEOUT_S


def default_helper_socket_path() -> Path:
    """``/run/localplane/helper.sock``.

    A system path rather than a per-user runtime directory, because the helper runs as a
    system service and the agent that talks to it does not necessarily share a session.
    """
    return Path("/run") / "localplane" / "helper.sock"


class HelperServer(ipc.LineServer):
    """The privileged helper's listening socket."""

    request_queue_size = 8

    def __init__(
        self,
        socket_path: str | Path,
        service: HelperService,
        allowed_uids: set[int] | None = None,
        allowed_gids: set[int] | None = None,
        socket_mode: int = DEFAULT_SOCKET_MODE,
        read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
    ) -> None:
        self.service = service
        super().__init__(
            socket_path,
            CODEC,
            service.handle,
            name="localplane-helper",
            allowed_uids=allowed_uids,
            allowed_gids=allowed_gids,
            socket_mode=socket_mode,
            read_timeout_s=read_timeout_s,
        )
        LOG.info(
            "privileged helper listening",
            extra={
                "socket": str(self.socket_path),
                "allowed_uids": sorted(self.allowed_uids),
                "allowed_gids": sorted(self.allowed_gids),
                "effective_uid": os.geteuid(),
                "methods": service.methods,
                "mutating_methods": service.mutating_methods,
            },
        )
