"""Newline-JSON over AF_UNIX, with peer-credential authorisation.

LocalPlane has two of these channels and they are deliberately not the same channel: the
backend talks to an unprivileged agent, and that agent talks to a privileged helper. What
must stay separate is the **vocabulary** — a method the agent gains must not appear at the
root end for free — and that is what :class:`Codec` takes as a parameter and what each side
declares for itself in its own module.

What does *not* need to be separate is the plumbing. Two hand-written copies of "frame a
JSON object per line, refuse unknown keys, refuse a version you do not speak, check
``SO_PEERCRED`` before parsing" are two places for a fix to land in one of. This module is
that plumbing, written once.

Three rules decide the shape of it, and they are the reason it is worth having at all:

* **The caller names an operation, never a command.** ``method`` is checked against a closed
  set supplied by the channel, and per-method parameter names are checked against a closed
  set too. No executable path, argument vector or shell fragment is transported by either
  channel, and neither has a method that would accept one.
* **Success and failure are structurally exclusive.** A response carries ``result`` or
  ``error``, and ``ok`` is recomputed from which — so a green ``ok`` over a failed operation
  cannot be constructed.
* **Unknown input is rejected, not ignored.** Unknown envelope keys, unknown parameters,
  unknown methods and unsupported versions all fail closed with a structured code.

And one rule that belongs to the client half: **a failure carries whether the request had
already been sent.** On a read path that is a curiosity. On the one path that mutates it is
the difference between a provable negative and an open question, and nothing above it is
allowed to collapse the two.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import socketserver
import struct
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

LOG = logging.getLogger("localplane.ipc")

UNCORRELATED_REQUEST_ID = "uncorrelated"
"""The request id used when a refusal happens before a request id could be read.

A peer refused on its credentials, or a frame that could not be parsed, has nothing to echo.
A client surfaces such a response *only* when it carries an error — an uncorrelated result
is refused, because data that cannot be tied to a request must never be believed.
"""

# Codes the framing itself can produce. Each channel declares these same strings in its own
# error enum, so a caller branches on one vocabulary while the codec stays channel-agnostic.
MALFORMED_MESSAGE = "malformed_message"
UNKNOWN_FIELD = "unknown_field"
UNSUPPORTED_METHOD = "unsupported_method"
INVALID_PARAMS = "invalid_params"
MESSAGE_TOO_LARGE = "message_too_large"
PROTOCOL_VERSION_UNSUPPORTED = "protocol_version_unsupported"
UNAUTHORIZED_PEER = "unauthorized_peer"
INTERNAL_ERROR = "internal_error"

_UCRED = struct.Struct("iII")  # pid, uid, gid

DEFAULT_SOCKET_MODE = 0o600
DEFAULT_DIR_MODE = 0o700
DEFAULT_READ_TIMEOUT_S = 30.0


class LineProtocolError(Exception):
    """A message that could not be accepted, carrying the code to answer with.

    Each channel subclasses this so that ``except`` in one cannot catch the other's, while
    the shape a caller reads — ``code``, ``message``, ``detail`` — is the same on both.
    """

    def __init__(self, code: Any, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": str(self.code), "message": self.message, "detail": self.detail}


@dataclass(frozen=True)
class Codec:
    """One channel's envelope: its key, its version, its closed method and parameter sets.

    ``params`` maps each method to exactly the parameter names it accepts. A channel that
    does not want per-method checking passes an empty mapping and validates in its handlers
    instead — which is what the agent does for the one method whose parameter is a list.
    """

    envelope_key: str
    version: str
    max_message_bytes: int
    methods: frozenset[str]
    error_type: type[LineProtocolError]
    #: The channel's own code vocabulary. The codec raises the shared string constants and
    #: coerces them through this, so a caller branches on its own enum rather than on a
    #: string that happens to match.
    codes: Any = None
    params: Mapping[str, frozenset[str]] | None = None

    # ------------------------------------------------------------------------- encoding

    def request(self, request_id: str, method: str, params: dict[str, Any] | None = None) -> bytes:
        if method not in self.methods:
            raise self._refuse(UNSUPPORTED_METHOD, f"unsupported method: {method!r}",
                               {"method": method, "supported": sorted(self.methods)})
        return self._frame({
            self.envelope_key: self.version,
            "request_id": request_id,
            "method": method,
            "params": params or {},
        })

    def ok(self, request_id: str, result: dict[str, Any]) -> bytes:
        return self._frame({
            self.envelope_key: self.version,
            "request_id": request_id,
            "ok": True,
            "result": result,
        })

    def failure(self, request_id: str, code: Any, message: str,
                detail: dict[str, Any] | None = None) -> bytes:
        return self._frame({
            self.envelope_key: self.version,
            "request_id": request_id,
            "ok": False,
            "error": {"code": str(code), "message": message, "detail": detail or {}},
        })

    def _frame(self, payload: dict[str, Any]) -> bytes:
        body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(body) + 1 > self.max_message_bytes:
            raise self._refuse(MESSAGE_TOO_LARGE, "encoded message exceeds the protocol ceiling",
                               {"bytes": len(body) + 1, "limit": self.max_message_bytes})
        return body + b"\n"

    # ------------------------------------------------------------------------- decoding

    def decode_request(self, raw: bytes) -> tuple[str, str, dict[str, Any]]:
        """Validate a request frame. Returns ``(request_id, method, params)``."""
        payload = self._object(raw)
        self._only(payload, {self.envelope_key, "request_id", "method", "params"}, "request")
        self._require_version(payload)
        request_id = self._request_id(payload)

        method = payload.get("method")
        if not isinstance(method, str):
            raise self._refuse(MALFORMED_MESSAGE, "method must be a string")
        if method not in self.methods:
            raise self._refuse(UNSUPPORTED_METHOD, f"unsupported method: {method!r}",
                               {"method": method, "supported": sorted(self.methods)})

        params = payload.get("params") or {}
        if not isinstance(params, dict):
            raise self._refuse(INVALID_PARAMS, "params must be an object")
        if self.params is not None:
            allowed = self.params[method]
            unknown = sorted(set(params) - allowed)
            if unknown:
                raise self._refuse(
                    UNKNOWN_FIELD,
                    f"unknown parameter(s) for {method}: {', '.join(unknown)}",
                    {"method": method, "unknown": unknown, "allowed": sorted(allowed)},
                )
        return request_id, method, params

    def decode_response(
        self, raw: bytes
    ) -> tuple[str, bool, dict[str, Any] | None, dict[str, Any] | None]:
        """Validate a response frame. Returns ``(request_id, ok, result, error)``.

        ``ok`` is recomputed from which of ``result``/``error`` is present. A frame claiming
        ``ok: true`` while carrying an error — or carrying both, or neither — is rejected
        rather than believed.
        """
        payload = self._object(raw)
        self._only(payload, {self.envelope_key, "request_id", "ok", "result", "error"}, "response")
        self._require_version(payload)
        request_id = self._request_id(payload)

        has_result, has_error = "result" in payload, "error" in payload
        if has_result == has_error:
            raise self._refuse(MALFORMED_MESSAGE,
                               "a response must carry exactly one of result or error",
                               {"has_result": has_result, "has_error": has_error})
        claimed = payload.get("ok")
        if not isinstance(claimed, bool):
            raise self._refuse(MALFORMED_MESSAGE, "ok must be a boolean")
        if claimed is not has_result:
            raise self._refuse(MALFORMED_MESSAGE, "ok does not agree with the body of the response",
                               {"ok": claimed, "has_result": has_result})

        if has_result:
            result = payload["result"]
            if not isinstance(result, dict):
                raise self._refuse(MALFORMED_MESSAGE, "result must be an object")
            return request_id, True, result, None

        err = payload["error"]
        if not isinstance(err, dict) or not isinstance(err.get("code"), str):
            raise self._refuse(MALFORMED_MESSAGE, "error must be an object with a code")
        return request_id, False, None, err

    # ------------------------------------------------------------------ stream framing

    def read_message(self, stream: Any, limit: int | None = None) -> bytes | None:
        """Read one newline-framed message. ``None`` means the peer closed cleanly.

        ``readline`` is given the limit so an unterminated flood is refused rather than
        buffered.
        """
        limit = self.max_message_bytes if limit is None else limit
        line = stream.readline(limit + 1)
        if not line:
            return None
        if len(line) > limit:
            raise self._refuse(MESSAGE_TOO_LARGE, "message exceeds the protocol ceiling",
                               {"limit": limit})
        if not line.endswith(b"\n"):
            raise self._refuse(MALFORMED_MESSAGE, "frame is not newline terminated")
        return line[:-1]

    @staticmethod
    def write_message(stream: Any, frame: bytes) -> None:
        stream.write(frame)
        stream.flush()

    # ------------------------------------------------------------------------ internals

    def _object(self, raw: bytes) -> dict[str, Any]:
        if len(raw) > self.max_message_bytes:
            raise self._refuse(MESSAGE_TOO_LARGE, "message exceeds the protocol ceiling",
                               {"bytes": len(raw), "limit": self.max_message_bytes})
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise self._refuse(MALFORMED_MESSAGE, f"could not parse frame: {exc}") from exc
        if not isinstance(payload, dict):
            raise self._refuse(MALFORMED_MESSAGE, "a frame must be a JSON object")
        return payload

    def _only(self, payload: dict[str, Any], allowed: set[str], what: str) -> None:
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise self._refuse(UNKNOWN_FIELD, f"unknown {what} field(s): {', '.join(unknown)}",
                               {"unknown": unknown, "allowed": sorted(allowed)})

    def _require_version(self, payload: dict[str, Any]) -> None:
        version = payload.get(self.envelope_key)
        if version != self.version:
            raise self._refuse(PROTOCOL_VERSION_UNSUPPORTED,
                               f"unsupported protocol version: {version!r}",
                               {"received": version, "supported": self.version})

    def _request_id(self, payload: dict[str, Any]) -> str:
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise self._refuse(MALFORMED_MESSAGE, "request_id must be a non-empty string")
        return request_id

    def _refuse(self, code: str, message: str,
                detail: dict[str, Any] | None = None) -> LineProtocolError:
        return self.error_type(self.codes(code) if self.codes else code, message, detail)


# ----------------------------------------------------------------------------- the server


class _Handler(socketserver.BaseRequestHandler):
    """One connection: credentials first, then requests until the peer closes."""

    server: "LineServer"

    def handle(self) -> None:
        peer = _peer_credentials(self.request)
        if peer is None:
            self._refuse("peer credentials could not be read", {})
            return
        allowed, reason = self.server.authorize(peer)
        if not allowed:
            LOG.warning("refused connection from unauthorized peer",
                        extra={"channel": self.server.name, "peer_uid": peer["uid"],
                               "peer_gid": peer["gid"], "peer_pid": peer["pid"]})
            self._refuse("peer is not authorized to reach this socket", {
                "reason": reason,
                "peer_uid": peer["uid"],
                "peer_gid": peer["gid"],
                "allowed_uids": sorted(self.server.allowed_uids),
                "allowed_gids": sorted(self.server.allowed_gids),
            })
            return

        codec = self.server.codec
        self.request.settimeout(self.server.read_timeout_s)
        with self.request.makefile("rb") as reader, self.request.makefile("wb") as writer:
            while True:
                try:
                    raw = codec.read_message(reader)
                except LineProtocolError as exc:
                    codec.write_message(writer, codec.failure(
                        UNCORRELATED_REQUEST_ID, exc.code, exc.message, exc.detail))
                    return
                except (OSError, ValueError):
                    return
                if raw is None:
                    return
                codec.write_message(writer, self._respond(raw, peer))

    def _respond(self, raw: bytes, peer: dict[str, int]) -> bytes:
        codec = self.server.codec
        try:
            request_id, method, params = codec.decode_request(raw)
        except LineProtocolError as exc:
            LOG.info("rejected request", extra={"channel": self.server.name,
                                                "code": str(exc.code), "peer_uid": peer["uid"]})
            return codec.failure(UNCORRELATED_REQUEST_ID, exc.code, exc.message, exc.detail)

        try:
            result = self.server.handler(method, params)
        except LineProtocolError as exc:
            LOG.info("operation refused", extra={"channel": self.server.name, "method": method,
                                                 "code": str(exc.code), "request_id": request_id})
            return codec.failure(request_id, exc.code, exc.message, exc.detail)
        except Exception as exc:  # noqa: BLE001 - the boundary: report, never swallow
            LOG.exception("operation failed", extra={"channel": self.server.name,
                                                     "method": method, "request_id": request_id})
            return codec.failure(request_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}",
                               {"method": method})

        LOG.info("operation served", extra={"channel": self.server.name, "method": method,
                                            "request_id": request_id})
        try:
            return codec.ok(request_id, result)
        except LineProtocolError as exc:
            return codec.failure(request_id, exc.code, exc.message, exc.detail)

    def _refuse(self, message: str, detail: dict[str, Any]) -> None:
        codec = self.server.codec
        try:
            with self.request.makefile("wb") as writer:
                codec.write_message(writer, codec.failure(
                    UNCORRELATED_REQUEST_ID, UNAUTHORIZED_PEER, message, detail))

            # ``socketserver`` closes the accepted socket as soon as this handler returns.
            # Closing while the peer's request is still unread can reset the connection and
            # race the refusal we just flushed. Half-close the response, then discard at most
            # one bounded frame while the client receives it and closes. The bytes are never
            # decoded, so an unauthorised peer still cannot reach the parser or the handler.
            self.request.shutdown(socket.SHUT_WR)
            self.request.settimeout(self.server.read_timeout_s)
            remaining = codec.max_message_bytes + 1
            while remaining:
                chunk = self.request.recv(min(8192, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass


class LineServer(socketserver.ThreadingUnixStreamServer):
    """An AF_UNIX socket serving one channel's codec, authorised by peer credentials.

    **The authorisation is a kernel fact, not a token.** ``SO_PEERCRED`` is read from the
    accepted socket before a byte of the connection is parsed, which is what makes it usable
    as a boundary in a product with no authentication: a peer cannot choose it, and cannot
    put it in a message.

    **An empty allowlist refuses everybody.** "Nobody said who is allowed" reads as nobody,
    never as everybody — the one default that must not exist on a socket that reaches
    privilege.
    """

    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 16

    def __init__(
        self,
        socket_path: str | Path,
        codec: Codec,
        handler: Callable[[str, dict[str, Any]], dict[str, Any]],
        *,
        name: str,
        allowed_uids: set[int] | None = None,
        allowed_gids: set[int] | None = None,
        socket_mode: int = DEFAULT_SOCKET_MODE,
        read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.codec = codec
        self.handler = handler
        self.name = name
        self.allowed_uids = set(allowed_uids) if allowed_uids is not None else {os.geteuid()}
        self.allowed_gids = set(allowed_gids) if allowed_gids is not None else set()
        self.read_timeout_s = read_timeout_s

        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=DEFAULT_DIR_MODE)
        self._remove_stale_socket()
        previous_umask = os.umask(0o177)
        try:
            super().__init__(str(self.socket_path), _Handler)
        finally:
            os.umask(previous_umask)
        os.chmod(self.socket_path, socket_mode)

    def authorize(self, peer: dict[str, int]) -> tuple[bool, str]:
        """Whether this peer may speak here, and the typed reason when it may not."""
        if peer["uid"] in self.allowed_uids:
            return True, "uid_allowed"
        if self.allowed_gids and peer["gid"] in self.allowed_gids:
            return True, "gid_allowed"
        if not self.allowed_uids and not self.allowed_gids:
            return False, "no_peer_is_allowed"
        return False, "peer_uid_not_allowed"

    def _remove_stale_socket(self) -> None:
        """Replace a socket left behind by a dead process; refuse a live one."""
        if not self.socket_path.exists():
            return
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.5)
            probe.connect(str(self.socket_path))
        except OSError:
            LOG.info("removing stale socket", extra={"socket": str(self.socket_path)})
            self.socket_path.unlink(missing_ok=True)
            return
        finally:
            probe.close()
        raise RuntimeError(f"another process is already listening on {self.socket_path}")

    def server_close(self) -> None:
        super().server_close()
        self.socket_path.unlink(missing_ok=True)

    def serve_in_thread(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, name=self.name, daemon=True)
        thread.start()
        return thread


def _peer_credentials(sock: socket.socket) -> dict[str, int] | None:
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size)
    except OSError:
        return None
    pid, uid, gid = _UCRED.unpack(raw)
    return {"pid": pid, "uid": uid, "gid": gid}


# ----------------------------------------------------------------------------- the client


def call(
    socket_path: Path,
    codec: Codec,
    timeout_s: float,
    method: str,
    params: dict[str, Any] | None,
    fail: Callable[..., Exception],
    *,
    unavailable: str,
    timeout_code: str,
) -> dict[str, Any]:
    """One request, one response, one connection. Raises via ``fail`` on every failure.

    ``fail(code, message, detail, dispatched)`` builds the channel's own error. ``dispatched``
    is the fact this function exists to preserve: it becomes true the instant the request
    frame is written, and everything after that line may be happening while the far side
    acts on it. On the one path that mutates a host, that is the difference between "nothing
    can have happened" and "we do not know".
    """
    request_id = uuid.uuid4().hex
    try:
        frame = codec.request(request_id, str(method), params)
    except LineProtocolError as exc:
        raise fail(exc.code, exc.message, exc.detail, False) from exc

    dispatched = False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout_s)
            sock.connect(str(socket_path))
            with sock.makefile("wb") as writer, sock.makefile("rb") as reader:
                codec.write_message(writer, frame)
                dispatched = True
                raw = codec.read_message(reader)
    except FileNotFoundError as exc:
        raise fail(unavailable, f"no socket at {socket_path}",
                   {"socket": str(socket_path)}, False) from exc
    except socket.timeout as exc:
        raise fail(timeout_code, f"no answer within {timeout_s}s",
                   {"socket": str(socket_path), "method": str(method)}, dispatched) from exc
    except ConnectionError as exc:
        raise fail(unavailable, f"connection failed: {exc}",
                   {"socket": str(socket_path)}, dispatched) from exc
    except OSError as exc:
        raise fail(unavailable, f"socket error: {exc.strerror or exc}",
                   {"socket": str(socket_path), "errno": exc.errno}, dispatched) from exc
    except LineProtocolError as exc:
        raise fail(exc.code, exc.message, exc.detail, dispatched) from exc

    if raw is None:
        raise fail(unavailable, "closed the connection without answering",
                   {"method": str(method)}, dispatched)

    try:
        reply_id, ok, result, error = codec.decode_response(raw)
    except LineProtocolError as exc:
        raise fail(exc.code, exc.message, exc.detail, dispatched) from exc

    if reply_id != request_id:
        # A refusal that happened before the request id could be read carries the sentinel,
        # and its reason is the truth worth reporting. Anything else — and any *result* that
        # cannot be correlated — is refused: an answer that might belong to another request
        # is not an answer.
        if not ok and reply_id == UNCORRELATED_REQUEST_ID:
            assert error is not None
            raise fail(error["code"], error.get("message", ""), error.get("detail", {}), dispatched)
        raise fail(MALFORMED_MESSAGE, "answered a different request",
                   {"expected": request_id, "received": reply_id}, dispatched)
    if not ok:
        assert error is not None
        raise fail(error["code"], error.get("message", ""), error.get("detail", {}), dispatched)

    assert result is not None
    return result
