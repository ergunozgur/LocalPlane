"""Direct single-process transport for the supported backend entry point."""

from __future__ import annotations

import ipaddress
import ssl
from contextlib import ExitStack
from typing import Any

import uvicorn

from localplane.backend.config import Settings
from localplane.backend.serving_files import (
    ServingConfigurationError,
    pinned_directory,
    pinned_file,
)


def validate_transport(settings: Settings) -> None:
    cert, key = settings.tls_cert_path, settings.tls_key_path
    if (cert is None) != (key is None):
        raise ServingConfigurationError("tls_pair_required", "Set both LOCALPLANE_TLS_CERT_PATH and LOCALPLANE_TLS_KEY_PATH.")
    # Hostnames, including localhost, are not a proof of a loopback bind. Avoid
    # DNS resolution and its bind-time race; use a numeric loopback address for HTTP.
    try:
        loopback = ipaddress.ip_address(settings.bind_host).is_loopback
    except ValueError:
        loopback = False
    if not loopback and cert is None:
        raise ServingConfigurationError("tls_required", "Non-loopback binds require direct TLS; configure both TLS paths or bind to a numeric loopback address.")


def server_config(settings: Settings, app: Any, port: int) -> uvicorn.Config:
    validate_transport(settings)
    options: dict[str, Any] = dict(
        host=settings.bind_host, port=port, log_config=None, access_log=False,
        proxy_headers=False, workers=1,
    )
    if settings.tls_cert_path is None:
        return uvicorn.Config(app, **options)
    assert settings.tls_key_path is not None
    with ExitStack() as stack:
        try:
            cert_dir = stack.enter_context(pinned_directory(settings.tls_cert_path.parent))
            cert = stack.enter_context(pinned_file(cert_dir, settings.tls_cert_path.name))
        except (OSError, ValueError):
            raise ServingConfigurationError("tls_certificate_file_invalid", "Use a readable regular certificate file in trusted, non-symlink directories, without group/other write access.") from None
        try:
            key_dir = stack.enter_context(pinned_directory(settings.tls_key_path.parent))
            key = stack.enter_context(pinned_file(key_dir, settings.tls_key_path.name, private=True))
        except (OSError, ValueError):
            raise ServingConfigurationError("tls_key_file_invalid", "Use a readable service-owned private key with mode 0400 or 0600, no links, and trusted parent directories.") from None
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.options |= ssl.OP_NO_COMPRESSION
            context.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20")

            def reject_password() -> str:
                raise ValueError("encrypted private keys are unsupported")

            # SSL alone reads these pinned descriptors; no Python key buffer,
            # second path lookup, environment secret, or interactive prompt.
            context.load_cert_chain(f"/proc/self/fd/{cert}", f"/proc/self/fd/{key}", password=reject_password)
        except (OSError, ValueError):
            raise ServingConfigurationError("tls_certificate_load_failed", "Supply a valid PEM certificate chain and matching unencrypted private key; check the certificate with your issuer.") from None
    # Uvicorn's path-only loader cannot reject password prompts via a callback.
    # Load its application/protocol config once, then attach the validated context
    # consumed by Server.startup. is_ssl uses ssl_certfile; keep only a safe marker,
    # not a stale descriptor or secret path. No reload/multiprocess mode is used.
    config = uvicorn.Config(app, **options, ssl_version=ssl.PROTOCOL_TLS_SERVER)
    config.load()
    config.ssl = context
    config.ssl_certfile = "<in-process>"
    # Config is already loaded; Server uses its in-process SSLContext and never
    # reopens certificate/key paths. Certificate rotation requires a restart.
    return config
