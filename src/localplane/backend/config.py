"""Backend settings, read from the environment once at startup."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from localplane.agent.server import default_socket_path
from localplane.backend.domain.states import DEFAULT_FRESHNESS_TTL_S

ENV_PREFIX = "LOCALPLANE_"


@dataclass(frozen=True)
class Settings:
    database_path: Path
    agent_socket: Path
    agent_timeout_s: float
    freshness_ttl_s: float
    log_level: str
    observe_on_startup: bool
    auth_secret_path: Path = Path("var/localplane-master.secret")
    bind_host: str = "127.0.0.1"
    development_origin: str | None = None
    tls_cert_path: Path | None = None
    tls_key_path: Path | None = None
    console_dir: Path | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)

        def get(name: str, default: str) -> str:
            return env.get(ENV_PREFIX + name, default)

        def optional_path(name: str) -> Path | None:
            value = get(name, "")
            return Path(value) if value else None

        return cls(
            database_path=Path(get("DB_PATH", "var/localplane.db")),
            agent_socket=Path(get("AGENT_SOCKET", str(default_socket_path()))),
            agent_timeout_s=float(get("AGENT_TIMEOUT_S", "10")),
            freshness_ttl_s=float(get("FRESHNESS_TTL_S", str(DEFAULT_FRESHNESS_TTL_S))),
            log_level=get("LOG_LEVEL", "INFO"),
            observe_on_startup=get("OBSERVE_ON_STARTUP", "1") not in {"0", "false", "no"},
            auth_secret_path=Path(get("AUTH_SECRET_PATH", "var/localplane-master.secret")),
            bind_host=get("HOST", "127.0.0.1"),
            development_origin=env.get(ENV_PREFIX + "DEVELOPMENT_ORIGIN") or None,
            tls_cert_path=optional_path("TLS_CERT_PATH"),
            tls_key_path=optional_path("TLS_KEY_PATH"),
            console_dir=optional_path("CONSOLE_DIR"),
        )
