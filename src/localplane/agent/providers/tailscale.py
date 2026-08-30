"""What tailscaled says about itself.

The tailnet interface on a Linux host is a plain tun device. The kernel knows it is a tun
device and nothing more: it does not know which process opened it, and it certainly does
not know the thing is Tailscale's because of what it is called. ``tailscale0`` is a
default, not a fact — the name is configurable, and any process can create a tun device
and call it that.

So the daemon is asked, and one fixed read-only command is what asking looks like:
``tailscale status --json``. What comes back that is worth keeping is the daemon's own
state, whether it is running in TUN mode at all rather than userspace networking, and the
addresses it holds. Those addresses are what let the backend correlate the daemon with a
kernel link — a link carrying the exact addresses tailscaled reports as its own is the
daemon's link, and no link is Tailscale's for any other reason.

Peers, DNS configuration, exit-node state and the rest of the status document are dropped.
They describe a tailnet, not the ownership of a host interface, and carrying them would
make every observation large for no gain.

``tailscale debug`` is deliberately not used anywhere here: it says of itself that it is
not a stable interface, and several of its subcommands mutate the daemon's state.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from localplane.agent.providers.base import CommandFailure, CommandRunner, SubprocessRunner
from localplane.agent.providers.evidence import ProviderReading
from localplane.protocol.providers import (
    PROVIDER_TAILSCALE,
    SOURCE_TAILSCALE_STATUS,
    ProviderStatus,
)

STATUS_ARGV: tuple[str, ...] = ("tailscale", "status", "--json")

#: The backend state in which the daemon actually holds its addresses on an interface.
BACKEND_RUNNING = "Running"

_OMITTED = ("Peer", "Self.Capabilities", "CertDomains", "ClientVersion", "User", "Health")


class TailscaleEvidenceProvider:
    """Read tailscaled's own account of itself. Read-only, by construction."""

    provider = PROVIDER_TAILSCALE
    source = SOURCE_TAILSCALE_STATUS

    def __init__(self, runner: CommandRunner | None = None, timeout_s: float = 5.0) -> None:
        self._runner = runner if runner is not None else SubprocessRunner()
        self._timeout_s = timeout_s

    def read(self) -> ProviderReading:
        observed_at = _now()
        detail: dict[str, Any] = {"argv": list(STATUS_ARGV), "omitted": list(_OMITTED)}

        result = self._runner.run(STATUS_ARGV, timeout_s=self._timeout_s)
        if result.failure is CommandFailure.NOT_FOUND:
            # Tailscale is not installed here, so no interface on this host is its.
            return self._unreadable(
                ProviderStatus.ABSENT, "tailscale_absent", observed_at, detail
            )
        if not result.ok:
            # The usual cause is tailscaled not running: the client exits non-zero because
            # it could not reach the daemon. Whatever the daemon would have said is
            # unknown, which is not the same as it having nothing to say.
            return self._unreadable(
                ProviderStatus.UNAVAILABLE,
                "timeout" if result.failure is CommandFailure.TIMEOUT else "daemon_not_answering",
                observed_at,
                {**detail, "command": result.as_dict()},
            )

        try:
            payload = json.loads(result.stdout or "null")
        except json.JSONDecodeError as exc:
            return self._unreadable(
                ProviderStatus.ERROR, "unparseable_output", observed_at, {**detail, "error": str(exc)}
            )
        if not isinstance(payload, dict):
            return self._unreadable(
                ProviderStatus.ERROR,
                "unexpected_shape",
                observed_at,
                {**detail, "received": type(payload).__name__},
            )

        return ProviderReading(
            provider=self.provider,
            source=self.source,
            status=ProviderStatus.OK,
            method="cli_json",
            observed_at=observed_at,
            version=payload.get("Version") if isinstance(payload.get("Version"), str) else None,
            records=(_daemon_record(payload),),
            detail=detail,
        )

    def _unreadable(
        self, status: ProviderStatus, reason: str, observed_at: str, detail: dict[str, Any]
    ) -> ProviderReading:
        return ProviderReading(
            provider=self.provider,
            source=self.source,
            status=status,
            method="cli_json",
            observed_at=observed_at,
            reason=reason,
            detail=detail,
        )


def _daemon_record(payload: dict[str, Any]) -> dict[str, Any]:
    """The daemon reduced to what could attribute an interface to it.

    ``tun`` is reported as the daemon reports it, including ``None`` when the field is
    absent: a daemon in userspace-networking mode holds no kernel interface at all, and
    guessing ``true`` would attribute a link to a process that never touched one.
    """
    self_node = payload.get("Self") if isinstance(payload.get("Self"), dict) else {}
    return {
        "backend_state": (
            payload.get("BackendState") if isinstance(payload.get("BackendState"), str) else None
        ),
        "tun": payload.get("TUN") if isinstance(payload.get("TUN"), bool) else None,
        "tailscale_ips": [ip for ip in payload.get("TailscaleIPs") or [] if isinstance(ip, str)],
        "hostname": (
            self_node.get("HostName") if isinstance(self_node.get("HostName"), str) else None
        ),
        "dns_name": (
            self_node.get("DNSName") if isinstance(self_node.get("DNSName"), str) else None
        ),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
