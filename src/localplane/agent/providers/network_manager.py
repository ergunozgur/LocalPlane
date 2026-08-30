"""What NetworkManager says about the devices it can see.

NetworkManager is consulted because on most Linux hosts it is the thing that actually
configures interfaces — and because it is scrupulously explicit about the devices it does
*not* configure, which is evidence of exactly equal value.

Its device table distinguishes four postures that must not be flattened into one:

``unmanaged``
    NetworkManager has been told to leave this device alone. It is not the configurer.
``connected (externally)``
    The device is up and NetworkManager represents it, but the configuration was made by
    something else and NetworkManager generated an in-memory profile to describe what it
    found. This is NetworkManager saying *this is not mine* — reading it as ownership is
    the single easiest mistake to make here, and it would mark every Docker bridge on a
    NetworkManager host as NetworkManager-controlled.
``connected`` with a real profile
    NetworkManager is applying a connection it holds to this device. This, and only this,
    is control.
anything else (``unavailable``, ``disconnected``, ``connecting``…)
    The device is NetworkManager's to manage, but nothing is being applied to it right
    now. A future profile activation could change that; a present-tense claim of control
    would be wrong.

Two fixed read-only ``nmcli`` invocations, terse and field-selected so the output is
parsed as records rather than scraped as prose. ``nmcli`` is used rather than D-Bus
because the agent depends on nothing outside the standard library; terse mode with an
explicit field list is a stable machine interface, and it is what the daemon's own client
offers. Nothing here activates, modifies, adds or deletes a connection, and no value from
anywhere reaches the argument vectors below.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from localplane.agent.providers.base import CommandFailure, CommandRunner, SubprocessRunner
from localplane.agent.providers.evidence import ProviderReading
from localplane.protocol.providers import (
    PROVIDER_NETWORKMANAGER,
    SOURCE_NETWORKMANAGER_DEVICES,
    ProviderStatus,
)

GENERAL_ARGV: tuple[str, ...] = (
    "nmcli", "--terse", "--escape", "yes", "--fields", "RUNNING,VERSION,STATE",
    "general", "status",
)
DEVICE_ARGV: tuple[str, ...] = (
    "nmcli", "--terse", "--escape", "yes",
    "--fields", "DEVICE,TYPE,STATE,CONNECTION,CON-UUID",
    "device", "status",
)

#: The device state that means NetworkManager is applying a connection right now.
STATE_CONNECTED = "connected"
STATE_UNMANAGED = "unmanaged"

#: The qualifier NetworkManager appends when the configuration on a connected device is
#: not its own. Present in the C locale, which the runner pins.
EXTERNAL_QUALIFIER = "(externally)"


class NetworkManagerEvidenceProvider:
    """Read NetworkManager's device table. Read-only, by construction."""

    provider = PROVIDER_NETWORKMANAGER
    source = SOURCE_NETWORKMANAGER_DEVICES

    def __init__(self, runner: CommandRunner | None = None, timeout_s: float = 5.0) -> None:
        self._runner = runner if runner is not None else SubprocessRunner()
        self._timeout_s = timeout_s

    def read(self) -> ProviderReading:
        observed_at = _now()
        detail: dict[str, Any] = {"argv": [list(GENERAL_ARGV), list(DEVICE_ARGV)]}

        general = self._runner.run(GENERAL_ARGV, timeout_s=self._timeout_s)
        if general.failure is CommandFailure.NOT_FOUND:
            # No nmcli on this host: NetworkManager is not here, so it configures nothing.
            return self._unreadable(
                ProviderStatus.ABSENT, "nmcli_absent", observed_at, detail
            )
        if not general.ok:
            return self._unreadable(
                ProviderStatus.UNAVAILABLE,
                "timeout" if general.failure is CommandFailure.TIMEOUT else "nmcli_failed",
                observed_at,
                {**detail, "command": general.as_dict()},
            )

        running, version, daemon_state = _parse_general(general.stdout)
        if running is not None and not running:
            # The client answered for a daemon that is not there. Whatever NetworkManager
            # would have said about these devices is unknown, not absent.
            return self._unreadable(
                ProviderStatus.UNAVAILABLE,
                "daemon_not_running",
                observed_at,
                {**detail, "running": False},
            )

        devices = self._runner.run(DEVICE_ARGV, timeout_s=self._timeout_s)
        if not devices.ok:
            return self._unreadable(
                ProviderStatus.UNAVAILABLE,
                "timeout" if devices.failure is CommandFailure.TIMEOUT else "nmcli_failed",
                observed_at,
                {**detail, "command": devices.as_dict()},
            )

        records = tuple(r for r in (_device_record(line) for line in devices.stdout.splitlines()) if r)
        return ProviderReading(
            provider=self.provider,
            source=self.source,
            status=ProviderStatus.OK,
            method="nmcli_terse",
            observed_at=observed_at,
            version=version,
            records=records,
            detail={**detail, "daemon_state": daemon_state, "devices": len(records)},
        )

    def _unreadable(
        self, status: ProviderStatus, reason: str, observed_at: str, detail: dict[str, Any]
    ) -> ProviderReading:
        return ProviderReading(
            provider=self.provider,
            source=self.source,
            status=status,
            method="nmcli_terse",
            observed_at=observed_at,
            reason=reason,
            detail=detail,
        )


def _device_record(line: str) -> dict[str, Any] | None:
    fields = split_terse(line)
    if len(fields) < 5 or not fields[0]:
        return None
    raw_state = fields[2]
    return {
        "device": fields[0],
        "device_type": fields[1] or None,
        # The bare state, with any qualifier NetworkManager appended kept separately: a
        # caller branches on `state` and `external`, and `state_raw` is what was actually
        # printed, so a qualifier this build does not know about is still visible.
        "state": raw_state.split(" (", 1)[0] or None,
        "state_raw": raw_state or None,
        "external": EXTERNAL_QUALIFIER in raw_state,
        "connection": fields[3] or None,
        "connection_uuid": fields[4] or None,
    }


def _parse_general(stdout: str) -> tuple[bool | None, str | None, str | None]:
    for line in stdout.splitlines():
        fields = split_terse(line)
        if len(fields) < 3:
            continue
        running = fields[0].lower() == "running" if fields[0] else None
        return running, fields[1] or None, fields[2] or None
    return None, None, None


def split_terse(line: str) -> list[str]:
    """Split one ``nmcli --terse --escape yes`` row.

    Terse mode escapes ``:`` and ``\\`` inside values, so a connection called ``lan:2``
    survives the round trip. Splitting on a bare ``:`` would tear it in half and shift
    every field after it.
    """
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    fields.append("".join(current))
    return fields


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
