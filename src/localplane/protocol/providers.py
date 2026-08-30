"""The provider vocabulary shared by both sides of the protocol.

A **provider** is a concrete system that has something to say about this host: the Docker
daemon, NetworkManager, tailscaled — and the kernel, which says a great deal and is asked
differently. The agent reads them; the backend decides what their answers mean. Both sides
need the same names for them, so the names live here rather than in either.

:class:`ProviderStatus` is the load-bearing part. The four values are not degrees of the
same thing — ``absent`` and ``unavailable`` are opposite in what they license. A provider
that is not installed cannot own anything on this host, which is a *conclusion*. A provider
that is installed and would not answer leaves the question open, which is a *gap*. A model
that cannot tell those apart will either invent certainty or refuse to conclude anything.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class ProviderStatus(StrEnum):
    """What happened when a provider was consulted."""

    OK = "ok"
    """It answered, and the answer could be read."""

    ABSENT = "absent"
    """The provider is not installed on this host, so it owns nothing here.

    A complete answer, not a gap.
    """

    UNAVAILABLE = "unavailable"
    """Present, but not consultable — permission, timeout, daemon not running. A gap."""

    ERROR = "error"
    """It answered with something this build cannot read. Also a gap."""


PROVIDER_DOCKER: Final = "docker"
PROVIDER_NETWORKMANAGER: Final = "networkmanager"
PROVIDER_TAILSCALE: Final = "tailscale"
PROVIDER_SYSTEMD: Final = "systemd"

PROVIDER_KERNEL: Final = "kernel"
"""The host's own kernel.

Not consulted over a socket: what the kernel says arrives as the interface observation
itself. It is named as a provider because "this link exists because the kernel bound a
driver to a device" is an answer to the same question the daemons are asked.
"""

PROVIDER_LOCALPLANE: Final = "localplane"
"""LocalPlane itself.

No evidence source produces this today, and none can: LocalPlane has never created or
configured anything on a host. The name exists so that the model does not have to change
shape on the day it does.
"""

SOURCE_DOCKER_NETWORKS: Final = "docker.networks"
SOURCE_DOCKER_CONTAINERS: Final = "docker.containers"
"""What the daemon says about the containers it is running.

Deliberately **not** in :data:`PROVIDER_SOURCES`. That mapping answers "which source did
each provider owe this sweep", and the sweep it describes is the network one. Containers are
observed by their own capability, in their own sweep, against their own objects; folding
them in would make an interface sweep look incomplete because nobody asked about containers.
"""
SOURCE_SYSTEMD_UNITS: Final = "systemd.units"
SOURCE_NETWORKMANAGER_DEVICES: Final = "networkmanager.devices"
SOURCE_TAILSCALE_STATUS: Final = "tailscale.status"
SOURCE_KERNEL_INTERFACE: Final = "kernel.interface"

PROVIDER_SOURCES: Final[dict[str, str]] = {
    PROVIDER_DOCKER: SOURCE_DOCKER_NETWORKS,
    PROVIDER_NETWORKMANAGER: SOURCE_NETWORKMANAGER_DEVICES,
    PROVIDER_TAILSCALE: SOURCE_TAILSCALE_STATUS,
}
"""The providers the agent knows how to consult, and the source each one answers from.

The backend uses this to notice a provider that reported nothing at all: a source absent
from a sweep was never consulted, which is different again from one that refused.
"""
