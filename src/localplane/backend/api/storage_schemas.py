"""Strict additive API boundary for transient filesystem evidence."""

from typing import Annotated, Literal
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

DecimalCounter = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,38})$")]


class StorageReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class StorageCounters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    total: DecimalCounter
    used: DecimalCounter
    free: DecimalCounter
    available: DecimalCounter
    reserved: DecimalCounter

    @model_validator(mode="after")
    def consistent(self):
        total, used, free, available, reserved = map(int, (self.total, self.used, self.free, self.available, self.reserved))
        if used + free != total or available + reserved != free:
            raise ValueError("inconsistent filesystem counters")
        return self


class StorageMount(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    # Keep JSON bound literals stable whether OpenAPI generation preserves the schema
    # or coerces numeric bounds to floats. Both 0 and 2**53 are exactly representable;
    # runtime validation still uses the strict integer constraints, not these extras.
    mount_id: int = Field(gt=0, lt=2**53, json_schema_extra={
        "exclusiveMinimum": 0.0, "exclusiveMaximum": float(2**53),
    })
    parent_id: int = Field(ge=0, lt=2**53, json_schema_extra={
        "minimum": 0.0, "exclusiveMaximum": float(2**53),
    })
    mountpoint: str = Field(max_length=4096)
    root: str = Field(max_length=4096)
    filesystem: str = Field(max_length=64)
    device: str = Field(pattern=r"^[0-9]{1,10}:[0-9]{1,10}$")
    read_only: bool
    status: Literal["observed", "unsupported", "unavailable", "changed"]
    reason: str = Field(max_length=256)
    bytes: StorageCounters | None
    inodes: StorageCounters | None
    inode_reason: str = Field(max_length=256)
    backing_name: str | None = Field(max_length=128)
    backing_reason: str = Field(max_length=256)

    @model_validator(mode="after")
    def measured(self):
        if self.inodes is not None and (self.inodes.total == "0" or any(
            value in {str(2**32 - 1), str(2**64 - 1)}
            for value in (self.inodes.total, self.inodes.free, self.inodes.available)
        )):
            raise ValueError("unsupported inode accounting")
        if self.status == "observed":
            if self.filesystem not in {"ext4", "xfs"} or self.bytes is None:
                raise ValueError("unsupported measurement")
        elif self.bytes is not None or self.inodes is not None or self.backing_name is not None:
            raise ValueError("unverified measurement")
        return self


class StorageObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    observation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    observed_at: str = Field(max_length=64)
    namespace: str | None = Field(pattern=r"^mnt:\[[0-9]+\]$", max_length=64)
    status: Literal["complete", "partial", "unavailable", "busy"]
    reason: str = Field(max_length=256)
    mounts: list[StorageMount] = Field(max_length=128)
    metadata_limit_bytes: Literal[1048576]
    mount_limit: Literal[128]
    response_limit_bytes: Literal[524288]
    collection_limit_ms: Literal[3000]
    work_limit_ms: Literal[500]

    @model_validator(mode="after")
    def identity(self):
        stamp = datetime.fromisoformat(self.observed_at)
        if stamp.tzinfo is None:
            raise ValueError("missing timestamp timezone")
        if len({m.mount_id for m in self.mounts}) != len(self.mounts):
            raise ValueError("duplicate mount identity")
        if self.mounts and self.namespace is None:
            raise ValueError("missing namespace")
        if self.status in {"complete", "partial"} and self.namespace is None:
            raise ValueError("missing observation namespace")
        if self.status == "busy" and self.mounts:
            raise ValueError("busy observation cannot carry mount evidence")
        if self.status in {"busy", "unavailable"} and any(m.status == "observed" for m in self.mounts):
            raise ValueError("unverified observation")
        if self.status == "complete" and any(m.status != "observed" or m.inodes is None for m in self.mounts):
            raise ValueError("incomplete coverage")
        return self
