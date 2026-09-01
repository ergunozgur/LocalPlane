"""The accepted single-credential authentication boundary."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import os
import secrets
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Header, HTTPException, Request, Security
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer

MASTER_SECRET_BYTES = 32
SESSION_TOKEN_BYTES = 32
SESSION_LIFETIME = timedelta(hours=12)
SESSION_COOKIE = "localplane_session"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

MASTER_BEARER = HTTPBearer(auto_error=False, scheme_name="MasterBearer")
BROWSER_COOKIE = APIKeyCookie(
    name=SESSION_COOKIE, auto_error=False, scheme_name="BrowserSession"
)


class AuthenticationConfigurationError(RuntimeError):
    """The configured master-secret boundary is missing or unsafe."""


@dataclass(frozen=True)
class AuthenticatedRequest:
    mechanism: str
    expires_at: datetime | None
    session_token: str | None = None


class SessionStore:
    """Lock-protected in-memory session hashes with fixed absolute expiry."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        lifetime: timedelta = SESSION_LIFETIME,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lifetime = lifetime
        self._sessions: dict[str, datetime] = {}
        self._lock = threading.RLock()

    def create(self) -> tuple[str, datetime]:
        now = self._now()
        token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
        expires_at = now + self._lifetime
        with self._lock:
            self._prune_locked(now)
            self._sessions[_token_digest(token)] = expires_at
        return token, expires_at

    def lookup(self, token: str) -> datetime | None:
        now = self._now()
        with self._lock:
            self._prune_locked(now)
            return self._sessions.get(_token_digest(token))

    def revoke(self, token: str) -> bool:
        with self._lock:
            return self._sessions.pop(_token_digest(token), None) is not None

    def stored_hashes(self) -> tuple[str, ...]:
        """Return only the non-secret values retained by this process."""
        with self._lock:
            return tuple(self._sessions)

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise RuntimeError("authentication clock must return an aware datetime")
        return now.astimezone(timezone.utc)

    def _prune_locked(self, now: datetime) -> None:
        for digest in [
            digest for digest, expiry in self._sessions.items() if expiry <= now
        ]:
            del self._sessions[digest]


class Authentication:
    """Master verification and the current process's browser sessions."""

    def __init__(
        self,
        master_secret: str,
        *,
        bind_host: str = "127.0.0.1",
        development_origin: str | None = None,
        sessions: SessionStore | None = None,
    ) -> None:
        _decode_master_secret(master_secret)
        self._master_secret = master_secret
        self.sessions = sessions or SessionStore()
        self.bind_host = bind_host
        self.development_origin = (
            _normalise_origin(development_origin) if development_origin else None
        )

    def verify_master(self, candidate: str) -> bool:
        return hmac.compare_digest(candidate, self._master_secret)

    def create_session(self, existing_token: str | None = None) -> tuple[str, datetime]:
        if existing_token is not None:
            self.sessions.revoke(existing_token)
        return self.sessions.create()

    @property
    def permits_loopback_http(self) -> bool:
        host = self.bind_host.strip().lower()
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False


def load_master_secret(path: Path) -> str:
    """Read one regular, service-owned, restrictively permissioned secret."""
    path = _absolute_path(path)
    _reject_symlink_components(path, include_leaf=True)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AuthenticationConfigurationError(
            f"authentication secret is not safely readable: {path}: {exc.strerror}"
        ) from exc
    try:
        info = os.fstat(descriptor)
        _validate_secret_metadata(path, info)
        raw = os.read(descriptor, 1025)
    finally:
        os.close(descriptor)
    if len(raw) > 1024:
        raise AuthenticationConfigurationError("authentication secret file is too large")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise AuthenticationConfigurationError(
            "authentication secret must be ASCII URL-safe base64"
        ) from exc
    token = text[:-1] if text.endswith("\n") else text
    if text not in {token, token + "\n"} or not token:
        raise AuthenticationConfigurationError(
            "authentication secret contains surrounding or extra whitespace"
        )
    _decode_master_secret(token)
    return token


def initialize_master_secret(path: Path) -> str:
    """Atomically create a new 0600 master-secret file and return it once."""
    path = _absolute_path(path)
    _reject_symlink_components(path, include_leaf=False)
    if not path.parent.is_dir():
        raise AuthenticationConfigurationError(
            f"authentication secret parent does not exist: {path.parent}"
        )
    parent_info = path.parent.stat()
    if parent_info.st_uid != os.geteuid() or parent_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise AuthenticationConfigurationError(
            f"authentication secret parent has unsafe ownership or permissions: {path.parent}"
        )
    try:
        leaf = os.lstat(path)
    except FileNotFoundError:
        leaf = None
    if leaf is not None:
        if stat.S_ISLNK(leaf.st_mode):
            raise AuthenticationConfigurationError(
                f"authentication secret path is a symbolic link: {path}"
            )
        load_master_secret(path)
        raise FileExistsError(f"authentication secret already exists: {path}")

    token = secrets.token_urlsafe(MASTER_SECRET_BYTES)
    _decode_master_secret(token)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(path, flags, 0o600)
        created = True
        _validate_secret_metadata(path, os.fstat(descriptor))
        payload = (token + "\n").encode("ascii")
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        if created:
            os.unlink(path)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return token


def require_authentication(
    request: Request,
    bearer: Annotated[HTTPAuthorizationCredentials | None, Security(MASTER_BEARER)],
    cookie: Annotated[str | None, Security(BROWSER_COOKIE)],
) -> AuthenticatedRequest:
    """Authenticate a normal API request, preferring explicit Bearer."""
    authentication = _authentication(request)
    if request.headers.get("authorization") is not None:
        result = _require_valid_master(authentication, bearer)
    else:
        if cookie is None:
            raise _unauthenticated("authentication_required", "authentication is required")
        expiry = authentication.sessions.lookup(cookie)
        if expiry is None:
            raise _unauthenticated(
                "session_invalid", "the browser session is invalid or expired"
            )
        _require_cookie_origin(request, authentication)
        result = AuthenticatedRequest("session", expiry, cookie)
    request.state.authentication = result
    return result


def require_master_bearer(
    request: Request,
    bearer: Annotated[HTTPAuthorizationCredentials | None, Security(MASTER_BEARER)],
) -> AuthenticatedRequest:
    """Authenticate the session exchange with the master only."""
    if request.headers.get("authorization") is None:
        raise _unauthenticated(
            "master_bearer_required", "a master Bearer credential is required"
        )
    result = _require_valid_master(_authentication(request), bearer)
    request.state.authentication = result
    return result


def require_browser_session(
    request: Request,
    cookie: Annotated[str | None, Security(BROWSER_COOKIE)],
    authorization: Annotated[str | None, Header()] = None,
) -> AuthenticatedRequest:
    """Require a browser session for logout; a master Bearer is not enough.

    Authorization is read as a plain header so the generated contract truthfully declares
    this operation as browser-session-only, while explicit invalid Bearer still cannot fall
    back to the cookie.
    """
    authentication = _authentication(request)
    if authorization is not None:
        _require_valid_master(authentication, _parse_bearer(authorization))
        raise _unauthenticated(
            "browser_session_required", "this operation requires a browser session"
        )
    if cookie is None:
        raise _unauthenticated("browser_session_required", "a browser session is required")
    expiry = authentication.sessions.lookup(cookie)
    if expiry is None:
        raise _unauthenticated(
            "session_invalid", "the browser session is invalid or expired"
        )
    _require_cookie_origin(request, authentication)
    result = AuthenticatedRequest("session", expiry, cookie)
    request.state.authentication = result
    return result


def cookie_is_secure(request: Request, authentication: Authentication) -> bool:
    """Return the Secure flag or refuse unsupported remote plain HTTP."""
    if request.url.scheme == "https":
        return True
    if request.url.scheme == "http" and authentication.permits_loopback_http:
        return False
    raise HTTPException(
        status_code=403,
        detail={
            "code": "browser_session_requires_https",
            "message": "HTTP browser sessions require a loopback bind",
            "detail": {},
        },
    )


def request_authentication(request: Request) -> AuthenticatedRequest:
    result = getattr(request.state, "authentication", None)
    if not isinstance(result, AuthenticatedRequest):
        raise RuntimeError("route executed without authentication")
    return result


def app_authentication(request: Request) -> Authentication:
    """Return the startup authentication state for session routes."""
    return _authentication(request)


def _parse_bearer(value: str) -> HTTPAuthorizationCredentials | None:
    scheme, separator, credential = value.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not credential or " " in credential:
        return None
    return HTTPAuthorizationCredentials(scheme=scheme, credentials=credential)


def _require_valid_master(
    authentication: Authentication,
    bearer: HTTPAuthorizationCredentials | None,
) -> AuthenticatedRequest:
    if bearer is None or bearer.scheme.lower() != "bearer" or not bearer.credentials:
        raise _unauthenticated("bearer_malformed", "the Bearer credential is malformed")
    if not authentication.verify_master(bearer.credentials):
        raise _unauthenticated("bearer_invalid", "the Bearer credential is invalid")
    return AuthenticatedRequest("bearer", None)


def _require_cookie_origin(request: Request, authentication: Authentication) -> None:
    if request.method.upper() not in UNSAFE_METHODS:
        return
    supplied = request.headers.get("origin")
    if supplied is None:
        raise _origin_refusal("origin_required", "an Origin is required")
    try:
        supplied = _normalise_origin(supplied)
    except AuthenticationConfigurationError:
        raise _origin_refusal("origin_invalid", "the Origin is invalid") from None
    accepted = {_request_origin(request)}
    if authentication.development_origin is not None:
        accepted.add(authentication.development_origin)
    if supplied not in accepted:
        raise _origin_refusal("origin_not_allowed", "the Origin is not accepted")


def _authentication(request: Request) -> Authentication:
    authentication = getattr(request.app.state, "authentication", None)
    if not isinstance(authentication, Authentication):
        raise RuntimeError("authentication was not established during startup")
    return authentication


def _unauthenticated(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": code, "message": message, "detail": {}},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _origin_refusal(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={"code": code, "message": message, "detail": {}},
    )


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _decode_master_secret(token: str) -> bytes:
    padding = "=" * (-len(token) % 4)
    try:
        decoded = base64.b64decode(token + padding, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise AuthenticationConfigurationError(
            "authentication secret is not valid URL-safe base64"
        ) from exc
    if len(decoded) != MASTER_SECRET_BYTES:
        raise AuthenticationConfigurationError(
            f"authentication secret must encode {MASTER_SECRET_BYTES} bytes"
        )
    return decoded


def _validate_secret_metadata(path: Path, info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise AuthenticationConfigurationError(
            f"authentication secret is not a regular file: {path}"
        )
    if info.st_uid != os.geteuid():
        raise AuthenticationConfigurationError(
            f"authentication secret is not owned by the service identity: {path}"
        )
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077 or not mode & stat.S_IRUSR:
        raise AuthenticationConfigurationError(
            f"authentication secret permissions are not restrictive: {path}"
        )


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _reject_symlink_components(path: Path, *, include_leaf: bool) -> None:
    parts = path.parts[1:] if include_leaf else path.parts[1:-1]
    current = Path(path.anchor)
    for index, part in enumerate(parts):
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError as exc:
            raise AuthenticationConfigurationError(
                f"authentication path component does not exist: {current}"
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise AuthenticationConfigurationError(
                f"authentication path traverses a symbolic link: {current}"
            )
        leaf = include_leaf and index == len(parts) - 1
        if not leaf and not stat.S_ISDIR(info.st_mode):
            raise AuthenticationConfigurationError(
                f"authentication parent is not a directory: {current}"
            )


def _normalise_origin(value: str) -> str:
    """Return one canonical HTTP origin or one controlled configuration error.

    urllib defers parts of authority validation until hostname and port are accessed.
    Keep the whole parse inside this boundary so malformed bracketed IPv6, non-numeric
    ports, and out-of-range ports cannot escape as ValueError.
    """
    try:
        if not value or any(character.isspace() or ord(character) == 0x7F for character in value):
            raise ValueError("origin contains whitespace or a control character")
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port_number = parsed.port
        if (
            scheme not in {"http", "https"}
            or host is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("not an HTTP origin")
        host = host.lower()
        if ":" in host:
            ipaddress.IPv6Address(host)
            authority_host = f"[{host}]"
        else:
            host.encode("ascii")
            authority_host = host
        parsed_authority = parsed.netloc.lower()
        expected_authority = authority_host + (
            "" if port_number is None else f":{port_number}"
        )
        if parsed_authority != expected_authority:
            raise ValueError("malformed HTTP authority")
        default_port = 80 if scheme == "http" else 443
        port = "" if port_number in {None, default_port} else f":{port_number}"
        return f"{scheme}://{authority_host}{port}"
    except (ValueError, UnicodeError) as exc:
        raise AuthenticationConfigurationError(f"not an HTTP origin: {value}") from exc


def _request_origin(request: Request) -> str:
    host = request.url.hostname
    if host is None:
        raise _origin_refusal("origin_unavailable", "the request origin is unavailable")
    if ":" in host:
        host = f"[{host}]"
    default_port = 80 if request.url.scheme == "http" else 443
    port = "" if request.url.port in {None, default_port} else f":{request.url.port}"
    return f"{request.url.scheme}://{host.lower()}{port}"
