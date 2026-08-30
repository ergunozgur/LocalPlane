"""Object identity.

An object's identity has to outlive the things people change about it. Kernel names are
renamed, ifindexes are reassigned at every boot, addresses move, and containers are
renamed. So identity is derived from the most durable evidence available, and the evidence
that was used travels with the object — a caller can see that ``docker0`` is identified
only by its name and draw the appropriate conclusion, instead of being handed an opaque id
that looks equally authoritative in every case.

The bases are not all facts about kernel links, and that is the point of naming them.
``provider_id`` is what a Docker container's identity rests on, and calling that a kernel
name would be a falsehood stored in the column whose entire job is to say what the identity
rests on.

Ids are deterministic: the same host, the same object, the same evidence gives the same
id across restarts and across rebuilds of the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256

OBJECT_KIND_NETWORK_INTERFACE = "network.interface"
OBJECT_KIND_DOCKER_CONTAINER = "docker.container"
OBJECT_KIND_SYSTEMD_UNIT = "systemd.unit"

_UNUSABLE_MACS = frozenset({"00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"})


class IdentityBasis(StrEnum):
    """The evidence an object's identity rests on, most durable first."""

    PROVIDER_ID = "provider_id"
    """The identity the system that manages a resource assigned to it, and guarantees.

    The strongest basis there is, where it exists — a Docker container id is immutable for
    the container's whole life, unique on the host, and unaffected by renaming. It is a
    separate basis rather than a kind of name because it is not a name: it is an opaque
    identifier minted by the provider, and it is only as good as that provider's own
    guarantee, which is exactly what a reader needs to know about it."""

    PERMANENT_MAC = "permanent_mac"
    """The NIC's burned-in address. Follows the hardware, not the slot or the name."""

    DEVICE_PATH = "device_path"
    """The bus address the device sits at. Follows the slot."""

    KERNEL_NAME = "kernel_name"
    """The last resort. Stable only for as long as nothing renames or recreates it."""


class IdentityConfidence(StrEnum):
    HIGH = "high"
    LOW = "low"


@dataclass(frozen=True)
class ObjectIdentity:
    object_id: str
    basis: IdentityBasis
    value: str
    confidence: IdentityConfidence


def derive_object_id(host_id: str, kind: str, basis: IdentityBasis, value: str) -> str:
    digest = sha256(f"{host_id}|{kind}|{basis}|{value}".encode("utf-8")).hexdigest()
    return f"obj_{digest[:32]}"


def identify_container(host_id: str, container_id: str) -> ObjectIdentity:
    """A container's identity: the id the daemon minted for it, and nothing else.

    Not the name, which ``docker rename`` moves and which Compose reuses across recreations
    of a service. Not the image, which several containers share. The container id is
    immutable for the container's life and unique on the host, so it is the identity — and
    the confidence is high because that is Docker's own guarantee about it rather than an
    inference from anything LocalPlane observed.

    A recreated container is deliberately a **new object**: ``docker compose up`` after an
    image change destroys the container and makes another, and the two have different ids
    because they are different containers. Keying identity to the name would quietly merge
    their histories, which is exactly the mistake ``permanent_mac`` exists to avoid for a
    link that was destroyed and recreated.
    """
    return ObjectIdentity(
        object_id=derive_object_id(
            host_id, OBJECT_KIND_DOCKER_CONTAINER, IdentityBasis.PROVIDER_ID, container_id
        ),
        basis=IdentityBasis.PROVIDER_ID,
        value=container_id,
        confidence=IdentityConfidence.HIGH,
    )


def identify_systemd_unit(host_id: str, canonical_id: str) -> ObjectIdentity:
    """A runtime unit is the canonical ``Unit.Id`` the manager returned on this host.

    Object paths, PIDs, cgroups and InvocationID values all change across manager or unit
    executions.  The canonical id is systemd's stable name for the runtime unit object and
    aliases resolve back to it, so it is the provider identity and the only identity input.
    """
    return ObjectIdentity(
        object_id=derive_object_id(
            host_id, OBJECT_KIND_SYSTEMD_UNIT, IdentityBasis.PROVIDER_ID, canonical_id
        ),
        basis=IdentityBasis.PROVIDER_ID,
        value=canonical_id,
        confidence=IdentityConfidence.HIGH,
    )


def identify_interface(
    host_id: str,
    name: str,
    mac_address: str | None,
    mac_is_permanent: bool | None,
    device_path: str | None,
) -> ObjectIdentity:
    """Choose the most durable identity evidence this interface actually offers.

    A MAC that the kernel reports as generated rather than permanent
    (``addr_assign_type != 0``) is not used: virtual links are given a random address at
    creation, so keying an object to one would mint a new object every time the link is
    recreated. The all-zero and broadcast addresses are rejected for the same reason —
    they identify nothing.
    """
    if mac_is_permanent and mac_address and mac_address.lower() not in _UNUSABLE_MACS:
        basis, value, confidence = (
            IdentityBasis.PERMANENT_MAC,
            mac_address.lower(),
            IdentityConfidence.HIGH,
        )
    elif device_path:
        basis, value, confidence = (
            IdentityBasis.DEVICE_PATH,
            device_path,
            IdentityConfidence.HIGH,
        )
    else:
        basis, value, confidence = (
            IdentityBasis.KERNEL_NAME,
            name,
            IdentityConfidence.LOW,
        )
    return ObjectIdentity(
        object_id=derive_object_id(host_id, OBJECT_KIND_NETWORK_INTERFACE, basis, value),
        basis=basis,
        value=value,
        confidence=confidence,
    )
