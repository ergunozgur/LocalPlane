"""Providers translate one concrete source of host truth into normalized evidence.

A provider answers *what the host says*. It does not decide whether that is healthy,
whether LocalPlane manages it, or what it should be instead — those are the backend's
judgements, and keeping them out of here is what lets the judgement change without
redeploying an agent.
"""

from localplane.agent.providers.base import (
    CommandResult,
    CommandRunner,
    Fidelity,
    InterfaceAddress,
    InterfaceFacts,
    InterfaceObservationBatch,
    InterfaceStatistics,
    NetworkProvider,
    ObservedInterface,
    ProviderIssue,
    SweepStatus,
)

__all__ = [
    "CommandResult",
    "CommandRunner",
    "Fidelity",
    "InterfaceAddress",
    "InterfaceFacts",
    "InterfaceObservationBatch",
    "InterfaceStatistics",
    "NetworkProvider",
    "ObservedInterface",
    "ProviderIssue",
    "SweepStatus",
]
