"""The closed filesystem observation contract; identity lasts for one observation only."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

METADATA_BYTES = 1024 * 1024
MAX_MOUNTS = 128
OUTPUT_BYTES = 512 * 1024
TOTAL_SECONDS = 3.0
WORK_SECONDS = 0.5


@dataclass
class StorageCounters:
    # Decimal strings preserve exact unsigned kernel counters in JavaScript too.
    total: str
    used: str
    free: str
    available: str
    reserved: str


@dataclass
class StorageMount:
    mount_id: int
    parent_id: int
    mountpoint: str
    root: str
    filesystem: str
    device: str
    read_only: bool
    status: Literal["observed", "unsupported", "unavailable", "changed"] = "unsupported"
    reason: str = "Only ext4 and xfs statistics are sampled."
    bytes: StorageCounters | None = None
    inodes: StorageCounters | None = None
    inode_reason: str = "Not sampled."
    backing_name: str | None = None
    backing_reason: str = "Block-device backing is unresolved."


@dataclass
class StorageObservation:
    status: Literal["complete", "partial", "unavailable", "busy"]
    reason: str
    observation_id: str = field(default_factory=lambda: uuid4().hex)
    observed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    namespace: str | None = None
    mounts: list[StorageMount] = field(default_factory=list)
    metadata_limit_bytes: int = METADATA_BYTES
    mount_limit: int = MAX_MOUNTS
    response_limit_bytes: int = OUTPUT_BYTES
    collection_limit_ms: int = int(TOTAL_SECONDS * 1000)
    work_limit_ms: int = int(WORK_SECONDS * 1000)

    def as_dict(self) -> dict:
        return asdict(self)
