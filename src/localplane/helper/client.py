"""The agent's client for the privileged helper socket.

One connection per call, over :func:`localplane.ipc.call`, which is shared with the backend's
client for the agent. What it preserves and this path depends on is
:attr:`HelperError.dispatched`: on a read path "the call did not work" is one condition, and
on the one path that mutates it is two — the request never left this process, or it may
already have reached the kernel. Nothing above is allowed to collapse them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from localplane import ipc
from localplane.helper.protocol import CODEC, HelperErrorCode, HelperMethod

DEFAULT_TIMEOUT_S = 10.0


class HelperError(RuntimeError):
    """The helper could not be reached, or refused.

    ``dispatched`` says whether the request had already been written to the socket. False
    means the helper was never given it, which on the mutating path is a proof that nothing
    was written. True means it may have acted, and the honest answer becomes "we do not know".
    """

    def __init__(
        self,
        code: HelperErrorCode | str,
        message: str,
        detail: dict[str, Any] | None = None,
        dispatched: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = message
        self.detail = detail or {}
        self.dispatched = dispatched

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class HelperClient:
    """Typed calls to the privileged helper. Two methods, mirroring the protocol."""

    def __init__(self, socket_path: str | Path, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.socket_path = Path(socket_path)
        self.timeout_s = timeout_s

    def hello(self) -> dict[str, Any]:
        return self.call(HelperMethod.HELLO)

    def set_interface_mtu(
        self,
        *,
        attempt_id: str,
        ifindex: int,
        expected_current_mtu: int,
        desired_mtu: int,
        expected_interface_name: str | None = None,
    ) -> dict[str, Any]:
        """Ask the helper to set one MTU. Every argument is a typed scalar.

        There is no argument here for a command, an executable, a provider, a message type or
        a name to be resolved on the far side; the parameters are exactly the ones the
        protocol declares, and the helper refuses any other key.
        """
        params: dict[str, Any] = {
            "attempt_id": attempt_id,
            "ifindex": ifindex,
            "expected_current_mtu": expected_current_mtu,
            "desired_mtu": desired_mtu,
        }
        if expected_interface_name is not None:
            params["expected_interface_name"] = expected_interface_name
        return self.call(HelperMethod.SET_INTERFACE_MTU, params)

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return ipc.call(
            self.socket_path,
            CODEC,
            self.timeout_s,
            method,
            params,
            HelperError,
            unavailable=HelperErrorCode.HELPER_UNAVAILABLE,
            timeout_code=HelperErrorCode.TIMEOUT,
        )
