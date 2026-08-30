"""Run the LocalPlane privileged helper.

    python -m localplane.helper

This is the only LocalPlane process that is expected to hold privilege, and it holds it to
perform exactly one kind of kernel write. It answers two methods and no others.

Environment:
    LOCALPLANE_HELPER_SOCKET      socket path (default: /run/localplane/helper.sock)
    LOCALPLANE_HELPER_ALLOW_UID   comma-separated uids allowed to connect
                                  (default: the uid running the helper)
    LOCALPLANE_HELPER_ALLOW_GID   comma-separated gids allowed to connect (default: none)
    LOCALPLANE_LOG_LEVEL          default INFO

The allowlist is the privilege boundary. Setting neither variable on a helper started as
root means only root may connect, which is safe and usually not what a deployment wants:
the agent runs unprivileged, so its uid is what belongs in ``LOCALPLANE_HELPER_ALLOW_UID``.
An empty allowlist refuses everybody rather than everybody being allowed.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from types import FrameType

from localplane.helper.server import HelperServer, default_helper_socket_path
from localplane.helper.service import HelperService
from localplane.log import configure_logging

LOG = logging.getLogger("localplane.helper")


def _ids(raw: str | None) -> set[int] | None:
    if raw is None:
        return None
    values = {part.strip() for part in raw.split(",") if part.strip()}
    return {int(value) for value in values}


def main() -> int:
    """Start the helper. It takes no command-line arguments, deliberately.

    Every knob is an environment variable read once, and there is no argument vector
    anywhere in this package — not for the process, and not inside it. A privileged process
    that accepts positional arguments is one argument away from accepting the wrong one.
    """
    configure_logging(os.environ.get("LOCALPLANE_LOG_LEVEL", "INFO"))
    socket_path = os.environ.get("LOCALPLANE_HELPER_SOCKET") or str(default_helper_socket_path())
    try:
        allowed_uids = _ids(os.environ.get("LOCALPLANE_HELPER_ALLOW_UID"))
        allowed_gids = _ids(os.environ.get("LOCALPLANE_HELPER_ALLOW_GID"))
    except ValueError:
        LOG.error("helper allowlist must be comma-separated numeric ids")
        return 2

    service = HelperService()
    if os.geteuid() != 0:
        # Started without privilege. Not fatal — the helper is honest about what it is, the
        # agent reports what it was told, and a capability probe that finds an unprivileged
        # helper reports the capability degraded rather than available.
        LOG.warning(
            "helper is running unprivileged; MTU writes will be refused by the kernel",
            extra={"effective_uid": os.geteuid()},
        )
    try:
        server = HelperServer(
            socket_path, service, allowed_uids=allowed_uids, allowed_gids=allowed_gids
        )
    except (OSError, RuntimeError) as exc:
        LOG.error("helper could not start", extra={"socket": socket_path, "error": str(exc)})
        return 1

    def _stop(signum: int, _frame: FrameType | None) -> None:
        LOG.info("helper stopping", extra={"signal": signum})
        server.shutdown()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        LOG.info("helper stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
