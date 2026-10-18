"""Read one inventory's source observations without mixing newer targeted reads."""

from dataclasses import fields, replace
import json
from typing import Sequence

from localplane.backend.db.database import Database
from localplane.backend.db.repositories import ObjectRecord, ObservationRecord, SweepRecord


def inventory_sources(
    database: Database, estate: Sequence[ObjectRecord], sweep: SweepRecord | None,
) -> list[ObjectRecord]:
    if sweep is None:
        return []
    records = {record.object_id: record for record in estate if record.host_id == sweep.host_id}
    sources = []
    for row in database.query(
        "SELECT * FROM observations WHERE sweep_id = ? AND host_id = ?",
        (sweep.sweep_id, sweep.host_id),
    ):
        record = records.get(row["object_id"])
        if record is None:
            continue
        values = {field.name: row[field.name] for field in fields(ObservationRecord)}
        values["facts"] = json.loads(values["facts"])
        values["gaps"] = json.loads(values["gaps"])
        sources.append(replace(record, observation=ObservationRecord(**values)))
    return sources
