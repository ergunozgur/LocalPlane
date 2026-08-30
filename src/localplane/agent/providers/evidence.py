"""The shape of raw provider evidence, and nothing that interprets it.

A *provider* here is a concrete external system that can be asked what it believes about
this host's networking — the Docker daemon, NetworkManager, tailscaled. Each one is read
through its own narrow seam, and each returns :class:`ProviderReading`: what was asked,
what answered, when, and the normalized records it gave back.

Three rules hold for everything in this module, and they are the whole reason it is
separate from the backend:

* **The agent reports, it does not conclude.** A reading says "Docker declares a network
  with this id, this gateway and this bridge name". It never says which kernel link that
  is, whether Docker owns it, or what LocalPlane may do about it. Correlating a provider
  record with an interface is a judgement, and judgements belong to the backend.
* **Not answering is an answer, and the kinds differ.** A provider that is not installed
  (:attr:`ProviderStatus.ABSENT`) cannot own anything on this host; one that is installed
  and could not be read (:attr:`ProviderStatus.UNAVAILABLE`) leaves a real gap in what can
  be concluded. Collapsing the two would let an unreadable daemon look like an absent one,
  and turn "LocalPlane could not check" into "LocalPlane checked and found nothing".
* **A reading that did not succeed carries no records.** The field is empty by
  construction, so no downstream reader can act on half of a failed consultation.

Records are normalized and deliberately small. A provider's full output is a dump — the
container list of every Docker network, every Tailscale peer — and none of it is evidence
of ownership. What each provider keeps is documented on the provider itself, and what it
dropped is declared in ``detail`` rather than left for a reader to discover.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from localplane.agent.providers.base import ProviderIssue, SweepStatus
from localplane.protocol.providers import ProviderStatus


@dataclass(frozen=True)
class ProviderReading:
    """One consultation of one provider source."""

    provider: str
    """The system that was asked: ``docker``, ``networkmanager``, ``tailscale``."""

    source: str
    """The specific thing that was read: ``docker.networks``, ``networkmanager.devices``."""

    status: ProviderStatus
    method: str
    """How it was read — ``unix_socket_http``, ``nmcli_terse``, ``cli_json``."""

    observed_at: str
    version: str | None = None
    """The provider's own version, when it reports one. Never inferred."""

    reason: str | None = None
    """A machine-readable code. Required whenever ``status`` is not ``ok``."""

    records: tuple[dict[str, Any], ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status is not ProviderStatus.OK:
            if not self.reason:
                raise ValueError("a reading that is not ok must carry a reason")
            if self.records:
                raise ValueError("a reading that is not ok must not carry records")

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "source": self.source,
            "status": str(self.status),
            "method": self.method,
            "observed_at": self.observed_at,
            "version": self.version,
            "reason": self.reason,
            "records": [dict(r) for r in self.records],
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ProviderEvidenceBatch:
    """One sweep of every provider the agent knows how to consult."""

    capability: str
    started_at: str
    completed_at: str
    readings: tuple[ProviderReading, ...] = ()
    issues: tuple[ProviderIssue, ...] = ()

    @property
    def status(self) -> SweepStatus:
        return self.derive_status()

    def derive_status(self) -> SweepStatus:
        """Derived from the readings, never asserted.

        A host where every provider is simply absent is ``ok``: nothing was there to read
        and that is the complete truth about it. ``failed`` is reserved for the case where
        nothing at all could be consulted — every provider that exists refused.
        """
        if not self.readings:
            return SweepStatus.FAILED
        unreadable = [
            r
            for r in self.readings
            if r.status in (ProviderStatus.UNAVAILABLE, ProviderStatus.ERROR)
        ]
        if not unreadable:
            return SweepStatus.OK
        if len(unreadable) == len(self.readings):
            return SweepStatus.FAILED
        return SweepStatus.PARTIAL

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": str(self.derive_status()),
            "readings": [r.as_dict() for r in self.readings],
            "issues": [i.as_dict() for i in self.issues],
        }


@runtime_checkable
class EvidenceProvider(Protocol):
    """Read one external system's own account of this host's networking.

    One method, and it reads. There is no ``apply``, no ``create`` and no ``execute`` on
    this seam: LocalPlane consults these systems to find out who owns what, and consulting
    is the whole of the contract.
    """

    provider: str
    source: str

    def read(self) -> ProviderReading: ...
