"""Consulting every provider once, and surviving each of them failing.

The providers are independent and are treated that way: each is read on its own, and one
of them being unreachable, slow or broken produces one unreadable reading rather than a
failed sweep. That isolation is the point — the whole reason provider evidence is a
separate operation from observing interfaces is that Docker being unreadable must never
cost LocalPlane its view of the host's links.

An unexpected exception from a provider is caught here and turned into an ``error``
reading with an issue beside it. A provider that raises is a bug in that provider, and the
honest report of a bug is "this source could not be read", not a traceback that takes the
observation down with it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from localplane.agent.providers.base import CommandRunner, ProviderIssue
from localplane.agent.providers.docker import DEFAULT_DOCKER_SOCKET, DockerProvider
from localplane.agent.providers.evidence import (
    EvidenceProvider,
    ProviderEvidenceBatch,
    ProviderReading,
    ProviderStatus,
)
from localplane.agent.providers.network_manager import NetworkManagerEvidenceProvider
from localplane.agent.providers.tailscale import TailscaleEvidenceProvider
from localplane.protocol.capabilities import CAPABILITY_NETWORK_PROVIDERS_OBSERVE

LOG = logging.getLogger("localplane.agent.providers")


class NetworkProviderEvidenceCollector:
    """Read every provider that could account for a network object on this host."""

    def __init__(
        self,
        runner: CommandRunner | None = None,
        docker_socket: str | Path = DEFAULT_DOCKER_SOCKET,
        providers: Sequence[EvidenceProvider] | None = None,
    ) -> None:
        self._providers: tuple[EvidenceProvider, ...] = tuple(
            providers
            if providers is not None
            else (
                DockerProvider(socket_path=docker_socket),
                NetworkManagerEvidenceProvider(runner=runner),
                TailscaleEvidenceProvider(runner=runner),
            )
        )

    @property
    def providers(self) -> tuple[EvidenceProvider, ...]:
        return self._providers

    def collect(self) -> ProviderEvidenceBatch:
        started_at = _now()
        readings: list[ProviderReading] = []
        issues: list[ProviderIssue] = []

        for provider in self._providers:
            try:
                readings.append(provider.read())
            except Exception as exc:  # noqa: BLE001 - the boundary: report, never swallow
                LOG.exception(
                    "provider evidence failed",
                    extra={"provider": provider.provider, "source": provider.source},
                )
                readings.append(
                    ProviderReading(
                        provider=provider.provider,
                        source=provider.source,
                        status=ProviderStatus.ERROR,
                        method="unknown",
                        observed_at=_now(),
                        reason="provider_raised",
                        detail={"error": f"{type(exc).__name__}: {exc}"},
                    )
                )
                issues.append(
                    ProviderIssue(
                        source=provider.source,
                        code="provider_raised",
                        message=f"{type(exc).__name__}: {exc}",
                        detail={"provider": provider.provider},
                    )
                )

        return ProviderEvidenceBatch(
            capability=CAPABILITY_NETWORK_PROVIDERS_OBSERVE,
            started_at=started_at,
            completed_at=_now(),
            readings=tuple(readings),
            issues=tuple(issues),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
