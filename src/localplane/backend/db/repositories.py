"""Typed access to the store.

Every read returns a dataclass, not a row. The point is that a caller cannot accidentally
depend on a column name, and that the shape of what comes out of the database is checked
in one place instead of at every call site.

Current state is always *derived*: the newest observation for an object, chosen by a
correlated subquery. There is no "current state" table to fall out of step with the
observations it summarises.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from localplane.backend.db.database import Database, from_json, to_json
from localplane.backend.domain.intent import STORED_INTENT_ORIGIN
from localplane.backend.domain.management_path import (
    ManagementPathObservation,
    RouteEvidence,
)
from localplane.backend.domain.provenance import ProviderReadingView


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


@dataclass(frozen=True)
class HostRecord:
    host_id: str
    identity_basis: str
    identity_confidence: str
    hostname: str | None
    configured_hostname: str | None
    boot_id: str | None
    os_id: str | None
    os_version_id: str | None
    os_pretty_name: str | None
    kernel_name: str | None
    kernel_release: str | None
    architecture: str | None
    identity_gaps: list[str]
    first_seen_at: str
    last_seen_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "HostRecord":
        data = dict(row)
        data["identity_gaps"] = from_json(data.get("identity_gaps"), [])
        return cls(**data)


@dataclass(frozen=True)
class CapabilityRecord:
    capability: str
    version: int
    status: str
    mutating: bool
    summary: str
    reason: str | None
    detail: dict[str, Any]
    discovered_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "CapabilityRecord":
        return cls(
            capability=row["capability"],
            version=row["version"],
            status=row["status"],
            mutating=bool(row["mutating"]),
            summary=row["summary"],
            reason=row["reason"],
            detail=from_json(row["detail"], {}),
            discovered_at=row["discovered_at"],
        )


@dataclass(frozen=True)
class AgentRecord:
    agent_instance_id: str
    host_id: str
    agent_version: str
    protocol_version: str
    transport: str
    process_isolated: bool
    privilege: str
    effective_uid: int | None
    pid: int | None
    started_at: str
    first_contact_at: str
    last_contact_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "AgentRecord":
        data = dict(row)
        data["process_isolated"] = bool(data["process_isolated"])
        return cls(**data)


@dataclass(frozen=True)
class SweepRecord:
    sweep_id: str
    host_id: str
    agent_instance_id: str | None
    capability: str
    scope: str
    provider: str
    provider_version: str
    status: str
    started_at: str
    completed_at: str
    received_at: str
    object_count: int
    missing: list[str]
    issues: list[dict[str, Any]]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "SweepRecord":
        data = dict(row)
        data["missing"] = from_json(data.get("missing"), [])
        data["issues"] = from_json(data.get("issues"), [])
        return cls(**data)


@dataclass(frozen=True)
class ObservationRecord:
    observation_id: str
    sweep_id: str
    capability: str
    provider: str
    provider_version: str
    method: str
    fidelity: str
    observed_at: str
    received_at: str
    health_state: str
    health_reason: str
    gaps: list[str]
    facts: dict[str, Any]

    @property
    def observed_at_dt(self) -> datetime | None:
        return _parse_ts(self.observed_at)


@dataclass(frozen=True)
class ObjectRecord:
    """An object and its newest observation, if it has one."""

    object_id: str
    host_id: str
    kind: str
    identity_basis: str
    identity_value: str
    identity_confidence: str
    display_name: str
    management_state: str
    management_reason: str
    active_intent_id: str | None
    first_seen_at: str
    last_seen_at: str
    observation: ObservationRecord | None


class HostRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    def upsert(self, identity: dict[str, Any], now: str) -> str:
        """Record what the agent said about the host. First-seen is never overwritten."""
        self._db.connection.execute(
            """
            INSERT INTO hosts (
                host_id, identity_basis, identity_confidence, hostname, configured_hostname,
                boot_id, os_id, os_version_id, os_pretty_name, kernel_name, kernel_release,
                architecture, identity_gaps, first_seen_at, last_seen_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(host_id) DO UPDATE SET
                identity_basis=excluded.identity_basis,
                identity_confidence=excluded.identity_confidence,
                hostname=excluded.hostname,
                configured_hostname=excluded.configured_hostname,
                boot_id=excluded.boot_id,
                os_id=excluded.os_id,
                os_version_id=excluded.os_version_id,
                os_pretty_name=excluded.os_pretty_name,
                kernel_name=excluded.kernel_name,
                kernel_release=excluded.kernel_release,
                architecture=excluded.architecture,
                identity_gaps=excluded.identity_gaps,
                last_seen_at=excluded.last_seen_at
            """,
            (
                identity["host_id"],
                identity["identity_basis"],
                identity["identity_confidence"],
                identity.get("hostname"),
                identity.get("configured_hostname"),
                identity.get("boot_id"),
                identity.get("os_id"),
                identity.get("os_version_id"),
                identity.get("os_pretty_name"),
                identity.get("kernel_name"),
                identity.get("kernel_release"),
                identity.get("architecture"),
                to_json(identity.get("gaps", [])),
                now,
                now,
            ),
        )
        return identity["host_id"]

    def get(self, host_id: str) -> HostRecord | None:
        row = self._db.query_one("SELECT * FROM hosts WHERE host_id = ?", (host_id,))
        return HostRecord.from_row(row) if row else None

    def most_recent(self) -> HostRecord | None:
        row = self._db.query_one("SELECT * FROM hosts ORDER BY last_seen_at DESC LIMIT 1")
        return HostRecord.from_row(row) if row else None


class AgentRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    def upsert(self, host_id: str, agent: dict[str, Any], protocol_version: str, now: str) -> str:
        self._db.connection.execute(
            """
            INSERT INTO agent_instances (
                agent_instance_id, host_id, agent_version, protocol_version, transport,
                process_isolated, privilege, effective_uid, pid, started_at,
                first_contact_at, last_contact_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(agent_instance_id) DO UPDATE SET last_contact_at=excluded.last_contact_at
            """,
            (
                agent["agent_instance_id"],
                host_id,
                agent["agent_version"],
                protocol_version,
                agent["transport"],
                1 if agent["process_isolated"] else 0,
                agent["privilege"],
                agent.get("effective_uid"),
                agent.get("pid"),
                agent["started_at"],
                now,
                now,
            ),
        )
        return agent["agent_instance_id"]

    def replace_capabilities(
        self, agent_instance_id: str, capabilities: list[dict[str, Any]]
    ) -> None:
        """Capabilities are replaced wholesale.

        A capability the agent no longer reports must disappear rather than linger: a
        stale row claiming ``available`` is the exact failure this model exists to
        prevent.
        """
        self._db.connection.execute(
            "DELETE FROM agent_capabilities WHERE agent_instance_id = ?", (agent_instance_id,)
        )
        self._db.connection.executemany(
            """
            INSERT INTO agent_capabilities (
                agent_instance_id, capability, version, status, mutating, summary,
                reason, detail, discovered_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    agent_instance_id,
                    c["capability"],
                    c["version"],
                    c["status"],
                    1 if c["mutating"] else 0,
                    c["summary"],
                    c.get("reason"),
                    to_json(c.get("detail", {})),
                    c["discovered_at"],
                )
                for c in capabilities
            ],
        )

    def most_recent(self, host_id: str | None = None) -> AgentRecord | None:
        if host_id is None:
            row = self._db.query_one(
                "SELECT * FROM agent_instances ORDER BY last_contact_at DESC LIMIT 1"
            )
        else:
            row = self._db.query_one(
                "SELECT * FROM agent_instances WHERE host_id = ? "
                "ORDER BY last_contact_at DESC LIMIT 1",
                (host_id,),
            )
        return AgentRecord.from_row(row) if row else None

    def capabilities(self, agent_instance_id: str) -> list[CapabilityRecord]:
        rows = self._db.query(
            "SELECT * FROM agent_capabilities WHERE agent_instance_id = ? ORDER BY capability",
            (agent_instance_id,),
        )
        return [CapabilityRecord.from_row(r) for r in rows]


_OBJECT_SELECT = """
    SELECT
        o.object_id, o.host_id, o.kind, o.identity_basis, o.identity_value,
        o.identity_confidence, o.display_name, o.management_state, o.management_reason,
        o.active_intent_id, o.first_seen_at, o.last_seen_at,
        obs.observation_id, obs.sweep_id, obs.capability, obs.provider, obs.provider_version,
        obs.method, obs.fidelity, obs.observed_at, obs.received_at,
        obs.health_state, obs.health_reason, obs.gaps, obs.facts
    FROM objects o
    LEFT JOIN observations obs ON obs.observation_id = (
        SELECT observation_id FROM observations
        WHERE object_id = o.object_id
        ORDER BY observed_at DESC, rowid DESC
        LIMIT 1
    )
"""


class ObjectRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    def upsert(
        self,
        object_id: str,
        host_id: str,
        kind: str,
        identity_basis: str,
        identity_value: str,
        identity_confidence: str,
        display_name: str,
        management_state: str,
        management_reason: str,
        now: str,
    ) -> None:
        self._db.connection.execute(
            """
            INSERT INTO objects (
                object_id, host_id, kind, identity_basis, identity_value, identity_confidence,
                display_name, management_state, management_reason, first_seen_at, last_seen_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(object_id) DO UPDATE SET
                display_name=excluded.display_name,
                -- Observing never changes LocalPlane's stance towards a managed object.
                -- The classification the ingestor arrives at answers "could this be
                -- managed"; whether it *is* managed was decided by an operator, and a
                -- sweep silently reverting that would drop the retained intent with it.
                -- Expressed here rather than in Python so it holds for every writer.
                management_state=CASE
                    WHEN objects.management_state = 'managed' THEN objects.management_state
                    ELSE excluded.management_state END,
                management_reason=CASE
                    WHEN objects.management_state = 'managed' THEN objects.management_reason
                    ELSE excluded.management_reason END,
                last_seen_at=excluded.last_seen_at
            """,
            (
                object_id,
                host_id,
                kind,
                identity_basis,
                identity_value,
                identity_confidence,
                display_name,
                management_state,
                management_reason,
                now,
                now,
            ),
        )

    def list_by_kind(self, host_id: str, kind: str) -> list[ObjectRecord]:
        rows = self._db.query(
            _OBJECT_SELECT + " WHERE o.host_id = ? AND o.kind = ? ORDER BY o.display_name",
            (host_id, kind),
        )
        return [self._to_record(r) for r in rows]

    def identity_map(self, host_id: str, kind: str) -> dict[str, str]:
        """Map provider identity values to object ids in one query.

        Relationship projection uses this once per sweep.  Looking up every edge
        independently would turn a systemd estate with thousands of dependency references
        into thousands of correlated database reads.
        """
        rows = self._db.query(
            "SELECT identity_value, object_id FROM objects WHERE host_id = ? AND kind = ?",
            (host_id, kind),
        )
        return {str(row["identity_value"]): str(row["object_id"]) for row in rows}

    def get(self, object_id: str) -> ObjectRecord | None:
        row = self._db.query_one(_OBJECT_SELECT + " WHERE o.object_id = ?", (object_id,))
        return self._to_record(row) if row else None

    def evidence(self, object_id: str) -> tuple[str, dict[str, Any]] | None:
        """The newest observation's raw evidence, kept out of the ordinary read path."""
        row = self._db.query_one(
            "SELECT observation_id, evidence FROM observations WHERE object_id = ? "
            "ORDER BY observed_at DESC, rowid DESC LIMIT 1",
            (object_id,),
        )
        if row is None:
            return None
        return row["observation_id"], from_json(row["evidence"], {})

    def display_names(self, object_ids: list[str]) -> dict[str, str]:
        """Names for a set of objects, for rendering records that only carry ids."""
        if not object_ids:
            return {}
        placeholders = ",".join("?" for _ in object_ids)
        rows = self._db.query(
            f"SELECT object_id, display_name FROM objects WHERE object_id IN ({placeholders})",
            tuple(object_ids),
        )
        return {row["object_id"]: row["display_name"] for row in rows}

    def managed(self, object_ids: list[str]) -> list[ObjectRecord]:
        """Those of ``object_ids`` that are managed, with their newest observation."""
        if not object_ids:
            return []
        placeholders = ",".join("?" for _ in object_ids)
        rows = self._db.query(
            _OBJECT_SELECT
            + f" WHERE o.management_state = 'managed' AND o.object_id IN ({placeholders})",
            tuple(object_ids),
        )
        return [self._to_record(r) for r in rows]

    def set_managed(self, object_id: str, intent_id: str, reason: str) -> None:
        """Point the object at its active intent and move it to ``managed``.

        The two happen in one statement because the schema will not let them happen
        separately: the CHECK on ``objects`` refuses a managed row without an active intent
        and an unmanaged row with one, so there is no order in which this could be done in
        two steps.
        """
        self._db.connection.execute(
            "UPDATE objects SET management_state = 'managed', active_intent_id = ?, "
            "management_reason = ? WHERE object_id = ?",
            (intent_id, reason, object_id),
        )

    def set_active_intent(self, object_id: str, intent_id: str) -> None:
        """Move a managed object's active intent to a newer version of its own intent.

        The management state is not touched, because a revision does not move it: the
        object was managed before and is managed after. ``management_reason`` is not
        touched either — it says why the object is managed, which is still that somebody
        adopted it.

        The ``management_state = 'managed'`` in the WHERE clause is not an optimisation. It
        makes this statement structurally incapable of activating an intent on an object
        that is not managed, whatever a caller believes; the CHECK on ``objects`` would
        catch the write, and the trigger would catch an intent belonging to somebody else,
        but a statement that cannot express the mistake is better than two that refuse it.
        """
        self._db.connection.execute(
            "UPDATE objects SET active_intent_id = ? "
            "WHERE object_id = ? AND management_state = 'managed'",
            (intent_id, object_id),
        )

    def set_observed(self, object_id: str, reason: str) -> None:
        """Drop the active intent and return the object to ``observed``.

        The intent row itself is untouched: it stays as history, and so does every version
        before it. What is released is LocalPlane's claim to be answerable for the object.
        """
        self._db.connection.execute(
            "UPDATE objects SET management_state = 'observed', active_intent_id = NULL, "
            "management_reason = ? WHERE object_id = ?",
            (reason, object_id),
        )

    @staticmethod
    def _to_record(row: sqlite3.Row) -> ObjectRecord:
        observation = None
        if row["observation_id"] is not None:
            observation = ObservationRecord(
                observation_id=row["observation_id"],
                sweep_id=row["sweep_id"],
                capability=row["capability"],
                provider=row["provider"],
                provider_version=row["provider_version"],
                method=row["method"],
                fidelity=row["fidelity"],
                observed_at=row["observed_at"],
                received_at=row["received_at"],
                health_state=row["health_state"],
                health_reason=row["health_reason"],
                gaps=from_json(row["gaps"], []),
                facts=from_json(row["facts"], {}),
            )
        return ObjectRecord(
            object_id=row["object_id"],
            host_id=row["host_id"],
            kind=row["kind"],
            identity_basis=row["identity_basis"],
            identity_value=row["identity_value"],
            identity_confidence=row["identity_confidence"],
            display_name=row["display_name"],
            management_state=row["management_state"],
            management_reason=row["management_reason"],
            active_intent_id=row["active_intent_id"],
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
            observation=observation,
        )


class SweepRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    def insert(self, sweep: dict[str, Any]) -> None:
        self._db.connection.execute(
            """
            INSERT INTO observation_sweeps (
                sweep_id, host_id, agent_instance_id, capability, scope, provider,
                provider_version, status, started_at, completed_at, received_at,
                object_count, missing, issues
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                sweep["sweep_id"],
                sweep["host_id"],
                sweep.get("agent_instance_id"),
                sweep["capability"],
                sweep.get("scope", "inventory"),
                sweep["provider"],
                sweep["provider_version"],
                sweep["status"],
                sweep["started_at"],
                sweep["completed_at"],
                sweep["received_at"],
                sweep["object_count"],
                to_json(sweep.get("missing", [])),
                to_json(sweep.get("issues", [])),
            ),
        )

    def insert_observation(self, observation: dict[str, Any]) -> None:
        self._db.connection.execute(
            """
            INSERT INTO observations (
                observation_id, sweep_id, host_id, object_id, capability, provider,
                provider_version, method, fidelity, observed_at, received_at,
                health_state, health_reason, gaps, facts, evidence
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                observation["observation_id"],
                observation["sweep_id"],
                observation["host_id"],
                observation["object_id"],
                observation["capability"],
                observation["provider"],
                observation["provider_version"],
                observation["method"],
                observation["fidelity"],
                observation["observed_at"],
                observation["received_at"],
                observation["health_state"],
                observation["health_reason"],
                to_json(observation.get("gaps", [])),
                to_json(observation["facts"]),
                to_json(observation.get("evidence", {})),
            ),
        )

    def latest(
        self,
        host_id: str,
        capability: str | None = None,
        *,
        scope: str | None = None,
    ) -> SweepRecord | None:
        clauses = ["host_id = ?"]
        parameters: list[Any] = [host_id]
        if capability is not None:
            clauses.append("capability = ?")
            parameters.append(capability)
        if scope is not None:
            clauses.append("scope = ?")
            parameters.append(scope)
        row = self._db.query_one(
            "SELECT * FROM observation_sweeps WHERE "
            + " AND ".join(clauses)
            + " ORDER BY received_at DESC, rowid DESC LIMIT 1",
            tuple(parameters),
        )
        return SweepRecord.from_row(row) if row else None

    def recent(self, host_id: str, limit: int = 20) -> list[SweepRecord]:
        rows = self._db.query(
            "SELECT * FROM observation_sweeps WHERE host_id = ? "
            "ORDER BY received_at DESC, rowid DESC LIMIT ?",
            (host_id, limit),
        )
        return [SweepRecord.from_row(r) for r in rows]

    def object_ids(self, sweep_id: str) -> set[str]:
        """Return estate membership for one sweep without reading object history N times."""
        return {
            str(row["object_id"])
            for row in self._db.query(
                "SELECT object_id FROM observations WHERE sweep_id = ?", (sweep_id,)
            )
        }

    def facts_by_object(self, sweep_id: str) -> dict[str, dict[str, Any]]:
        """Read one sweep's fact snapshots in one query for coherent projection work."""
        return {
            str(row["object_id"]): from_json(row["facts"], {})
            for row in self._db.query(
                "SELECT object_id, facts FROM observations WHERE sweep_id = ?", (sweep_id,)
            )
        }

    def observation_count(self, object_id: str) -> int:
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM observations WHERE object_id = ?", (object_id,)
        )
        return int(row["n"]) if row else 0


# ------------------------------------------------------------------------------- intent


@dataclass(frozen=True)
class IntentFieldRecord:
    field: str
    value_type: str
    value: bool | int


@dataclass(frozen=True)
class RevisionRecord:
    """The event that produced one intent version by revising the one before it."""

    revision_id: str
    object_id: str
    host_id: str
    kind: str
    intent_id: str
    host_effect: str
    occurred_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "RevisionRecord":
        return cls(**dict(row))


@dataclass(frozen=True)
class IntentRecord:
    """One version of what LocalPlane intends for an object. Immutable once written."""

    intent_id: str
    object_id: str
    host_id: str
    version: int
    supersedes: str | None
    schema_version: int
    origin: str
    """How this version came to exist: ``adopt``, ``revise`` or ``adopt_runtime``.

    Derived, not read from the column of the same name. ``intents.origin`` was CHECKed to a
    single value in 0002 and SQLite cannot widen a CHECK without rebuilding a table that
    five others reference, so 0004 froze it and moved the answer to the events: a version
    named by a row in ``intent_revisions`` was revised, and one that is not was adopted.
    Nothing above the store reads the frozen column.
    """

    capability: str
    provider: str
    provider_version: str
    observation_id: str
    sweep_id: str
    observed_at: str
    created_at: str
    fields: tuple[IntentFieldRecord, ...]
    revision: RevisionRecord | None = None
    """The revision that produced this version, when one did. ``None`` after an adopt."""


@dataclass(frozen=True)
class TransitionRecord:
    transition_id: str
    object_id: str
    host_id: str
    transition: str
    from_state: str
    to_state: str
    intent_id: str
    observation_id: str | None
    host_effect: str
    occurred_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TransitionRecord":
        return cls(**dict(row))


class IntentRepository:
    """Reads and writes retained intent.

    Every write here belongs inside a transaction opened by the caller: an intent that
    landed without the object pointing at it, or a pointer moved without the intent behind
    it, are the two states the schema exists to forbid, and they must not be reachable
    through a partial write either.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    # ------------------------------------------------------------------------- writes

    def insert(
        self,
        *,
        intent_id: str,
        object_id: str,
        host_id: str,
        version: int,
        supersedes: str | None,
        schema_version: int,
        capability: str,
        provider: str,
        provider_version: str,
        observation_id: str,
        sweep_id: str,
        observed_at: str,
        created_at: str,
        fields: list[tuple[str, str, int]],
    ) -> None:
        """Write one immutable intent version and the values it controls.

        There is no ``origin`` argument. The column takes the only value its 0002 CHECK
        allows and says nothing; what produced this version is recorded as an event — an
        adopt in ``management_transitions`` or a row in ``intent_revisions`` — and exactly
        one of those must be written in the same transaction as this call.
        """
        self._db.connection.execute(
            """
            INSERT INTO intents (
                intent_id, object_id, host_id, version, supersedes, schema_version, origin,
                capability, provider, provider_version, observation_id, sweep_id,
                observed_at, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                intent_id,
                object_id,
                host_id,
                version,
                supersedes,
                schema_version,
                STORED_INTENT_ORIGIN,
                capability,
                provider,
                provider_version,
                observation_id,
                sweep_id,
                observed_at,
                created_at,
            ),
        )
        self._db.connection.executemany(
            "INSERT INTO intent_fields (intent_id, field, value_type, value) VALUES (?,?,?,?)",
            [(intent_id, field, value_type, value) for field, value_type, value in fields],
        )

    def record_transition(
        self,
        *,
        transition_id: str,
        object_id: str,
        host_id: str,
        transition: str,
        from_state: str,
        to_state: str,
        intent_id: str,
        observation_id: str | None,
        occurred_at: str,
    ) -> None:
        """Record an adopt or a release.

        ``host_effect`` is written as ``'none'`` and the column will not accept anything
        else. Neither transition touches the host, and this table is structurally incapable
        of claiming otherwise.
        """
        self._db.connection.execute(
            """
            INSERT INTO management_transitions (
                transition_id, object_id, host_id, transition, from_state, to_state,
                intent_id, observation_id, host_effect, occurred_at
            ) VALUES (?,?,?,?,?,?,?,?,'none',?)
            """,
            (
                transition_id,
                object_id,
                host_id,
                transition,
                from_state,
                to_state,
                intent_id,
                observation_id,
                occurred_at,
            ),
        )

    def record_revision(
        self,
        *,
        revision_id: str,
        object_id: str,
        host_id: str,
        kind: str,
        intent_id: str,
        occurred_at: str,
    ) -> None:
        """Record that ``intent_id`` exists because the version before it was revised.

        ``host_effect`` is written as ``'none'`` and the column will not accept anything
        else, exactly as it will not on a management transition. A revision changes what
        LocalPlane wants and nothing about the machine, and this table cannot say otherwise.

        ``intent_id`` is UNIQUE in the schema, so a second revision claiming the same
        version is refused by the store rather than by whoever remembered to check.
        """
        self._db.connection.execute(
            """
            INSERT INTO intent_revisions (
                revision_id, object_id, host_id, kind, intent_id, host_effect, occurred_at
            ) VALUES (?,?,?,?,?,'none',?)
            """,
            (revision_id, object_id, host_id, kind, intent_id, occurred_at),
        )

    # -------------------------------------------------------------------------- reads

    def next_version(self, object_id: str) -> int:
        row = self._db.query_one(
            "SELECT COALESCE(MAX(version), 0) AS v FROM intents WHERE object_id = ?",
            (object_id,),
        )
        return int(row["v"]) + 1 if row else 1

    def get(self, intent_id: str) -> IntentRecord | None:
        row = self._db.query_one("SELECT * FROM intents WHERE intent_id = ?", (intent_id,))
        if row is None:
            return None
        return self._assemble([row])[0]

    def history(self, object_id: str) -> list[IntentRecord]:
        """Every version ever retained for this object, newest first. Nothing is deleted."""
        rows = self._db.query(
            "SELECT * FROM intents WHERE object_id = ? ORDER BY version DESC", (object_id,)
        )
        return self._assemble(rows)

    def active_for(self, object_ids: list[str]) -> dict[str, IntentRecord]:
        """The intent in force for each of ``object_ids`` that has one.

        Joined through ``objects.active_intent_id`` rather than by picking the highest
        version, because the pointer is the only thing that decides which intent is in
        force. A version that exists but is not pointed at is history.
        """
        if not object_ids:
            return {}
        placeholders = ",".join("?" for _ in object_ids)
        rows = self._db.query(
            f"""
            SELECT i.* FROM intents i
            JOIN objects o ON o.active_intent_id = i.intent_id
            WHERE o.object_id IN ({placeholders})
            """,
            tuple(object_ids),
        )
        return {record.object_id: record for record in self._assemble(rows)}

    def transitions(self, object_id: str, limit: int = 50) -> list[TransitionRecord]:
        rows = self._db.query(
            "SELECT * FROM management_transitions WHERE object_id = ? "
            "ORDER BY occurred_at DESC, rowid DESC LIMIT ?",
            (object_id, limit),
        )
        return [TransitionRecord.from_row(r) for r in rows]

    # ------------------------------------------------------------------------ helpers

    def _assemble(self, rows: list[sqlite3.Row]) -> list[IntentRecord]:
        intent_ids = [r["intent_id"] for r in rows]
        fields = self._fields_for(intent_ids)
        revisions = self._revisions_for(intent_ids)
        return [
            self._to_record(r, fields.get(r["intent_id"], ()), revisions.get(r["intent_id"]))
            for r in rows
        ]

    def _revisions_for(self, intent_ids: list[str]) -> dict[str, RevisionRecord]:
        if not intent_ids:
            return {}
        placeholders = ",".join("?" for _ in intent_ids)
        rows = self._db.query(
            f"SELECT * FROM intent_revisions WHERE intent_id IN ({placeholders})",
            tuple(intent_ids),
        )
        return {row["intent_id"]: RevisionRecord.from_row(row) for row in rows}

    def _fields_for(self, intent_ids: list[str]) -> dict[str, tuple[IntentFieldRecord, ...]]:
        if not intent_ids:
            return {}
        placeholders = ",".join("?" for _ in intent_ids)
        rows = self._db.query(
            f"SELECT * FROM intent_fields WHERE intent_id IN ({placeholders}) ORDER BY field",
            tuple(intent_ids),
        )
        grouped: dict[str, list[IntentFieldRecord]] = {}
        for row in rows:
            grouped.setdefault(row["intent_id"], []).append(
                IntentFieldRecord(
                    field=row["field"],
                    value_type=row["value_type"],
                    value=(
                        bool(row["value"]) if row["value_type"] == "boolean" else int(row["value"])
                    ),
                )
            )
        return {k: tuple(v) for k, v in grouped.items()}

    @staticmethod
    def _to_record(
        row: sqlite3.Row,
        fields: tuple[IntentFieldRecord, ...],
        revision: RevisionRecord | None,
    ) -> IntentRecord:
        return IntentRecord(
            intent_id=row["intent_id"],
            object_id=row["object_id"],
            host_id=row["host_id"],
            version=row["version"],
            supersedes=row["supersedes"],
            schema_version=row["schema_version"],
            # The event, not the frozen column. A version nothing revised was adopted:
            # `management_transitions` holds the adopt that named it, and this is the only
            # other way an intent version can come to exist.
            origin=revision.kind if revision is not None else STORED_INTENT_ORIGIN,
            capability=row["capability"],
            provider=row["provider"],
            provider_version=row["provider_version"],
            observation_id=row["observation_id"],
            sweep_id=row["sweep_id"],
            observed_at=row["observed_at"],
            created_at=row["created_at"],
            fields=fields,
            revision=revision,
        )


# ----------------------------------------------------------------------------- findings


@dataclass(frozen=True)
class FindingRecord:
    finding_id: str
    finding_key: str
    host_id: str
    object_id: str
    finding_type: str
    subject: str
    status: str
    intent_id: str
    intended_type: str
    intended_value: bool | int
    observed_type: str | None
    observed_value: bool | int | None
    comparison: str
    reason: str
    observation_id: str | None
    sweep_id: str | None
    first_seen_at: str
    last_seen_at: str
    updated_at: str
    resolved_at: str | None
    resolution: str | None
    resolved_by_observation_id: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "FindingRecord":
        data = dict(row)
        data["intended_value"] = _typed(data["intended_type"], data["intended_value"])
        data["observed_value"] = _typed(data["observed_type"], data["observed_value"])
        return cls(**data)


def _typed(value_type: str | None, stored: int | None) -> bool | int | None:
    if stored is None or value_type is None:
        return None
    return bool(stored) if value_type == "boolean" else int(stored)


class FindingRepository:
    """Reads and writes LocalPlane's durable claims.

    There is no ``delete``. A claim that stopped being true is resolved, with the reason it
    ended, because "LocalPlane thought this for six days and was right" and "LocalPlane
    never thought this" are different histories and only one of them happened.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def open_for_key(self, finding_key: str) -> FindingRecord | None:
        row = self._db.query_one(
            "SELECT * FROM findings WHERE finding_key = ? AND status = 'open'", (finding_key,)
        )
        return FindingRecord.from_row(row) if row else None

    def open_for_object(self, object_id: str) -> list[FindingRecord]:
        rows = self._db.query(
            "SELECT * FROM findings WHERE object_id = ? AND status = 'open' "
            "ORDER BY first_seen_at DESC, rowid DESC",
            (object_id,),
        )
        return [FindingRecord.from_row(r) for r in rows]

    def get(self, finding_id: str) -> FindingRecord | None:
        row = self._db.query_one("SELECT * FROM findings WHERE finding_id = ?", (finding_id,))
        return FindingRecord.from_row(row) if row else None

    def list_for_host(
        self, host_id: str, status: str | None = "open", limit: int = 100
    ) -> list[FindingRecord]:
        if status is None:
            rows = self._db.query(
                "SELECT * FROM findings WHERE host_id = ? "
                "ORDER BY first_seen_at DESC, rowid DESC LIMIT ?",
                (host_id, limit),
            )
        else:
            rows = self._db.query(
                "SELECT * FROM findings WHERE host_id = ? AND status = ? "
                "ORDER BY first_seen_at DESC, rowid DESC LIMIT ?",
                (host_id, status, limit),
            )
        return [FindingRecord.from_row(r) for r in rows]

    def history_for_object(self, object_id: str, limit: int = 100) -> list[FindingRecord]:
        rows = self._db.query(
            "SELECT * FROM findings WHERE object_id = ? ORDER BY first_seen_at DESC, rowid DESC "
            "LIMIT ?",
            (object_id, limit),
        )
        return [FindingRecord.from_row(r) for r in rows]

    def insert(
        self,
        *,
        finding_id: str,
        finding_key: str,
        host_id: str,
        object_id: str,
        finding_type: str,
        subject: str,
        intent_id: str,
        intended_type: str,
        intended_value: int,
        observed_type: str | None,
        observed_value: int | None,
        comparison: str,
        reason: str,
        observation_id: str | None,
        sweep_id: str | None,
        now: str,
    ) -> None:
        self._db.connection.execute(
            """
            INSERT INTO findings (
                finding_id, finding_key, host_id, object_id, finding_type, subject, status,
                intent_id, intended_type, intended_value, observed_type, observed_value,
                comparison, reason, observation_id, sweep_id,
                first_seen_at, last_seen_at, updated_at
            ) VALUES (?,?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                finding_id,
                finding_key,
                host_id,
                object_id,
                finding_type,
                subject,
                intent_id,
                intended_type,
                intended_value,
                observed_type,
                observed_value,
                comparison,
                reason,
                observation_id,
                sweep_id,
                now,
                now,
                now,
            ),
        )

    def update_evidence(
        self,
        *,
        finding_id: str,
        intent_id: str,
        intended_type: str,
        intended_value: int,
        observed_type: str | None,
        observed_value: int | None,
        comparison: str,
        reason: str,
        observation_id: str | None,
        sweep_id: str | None,
        now: str,
        confirmed: bool,
    ) -> None:
        """Refresh an open finding from a new evaluation.

        ``last_seen_at`` moves only when ``confirmed`` — that is, when the new evidence
        *proved* the claim again. An evaluation that could not read the value updates
        ``updated_at`` and leaves ``last_seen_at`` where it was, so the pair says exactly
        what happened: still open, last confirmed then, looked at since and could not tell.
        """
        self._db.connection.execute(
            """
            UPDATE findings SET
                intent_id = ?, intended_type = ?, intended_value = ?,
                observed_type = ?, observed_value = ?, comparison = ?, reason = ?,
                observation_id = ?, sweep_id = ?,
                last_seen_at = CASE WHEN ? THEN ? ELSE last_seen_at END,
                updated_at = ?
            WHERE finding_id = ?
            """,
            (
                intent_id,
                intended_type,
                intended_value,
                observed_type,
                observed_value,
                comparison,
                reason,
                observation_id,
                sweep_id,
                1 if confirmed else 0,
                now,
                now,
                finding_id,
            ),
        )

    def resolve(
        self,
        *,
        finding_id: str,
        resolution: str,
        resolved_by_observation_id: str | None,
        now: str,
    ) -> None:
        self._db.connection.execute(
            """
            UPDATE findings SET
                status = 'resolved', resolution = ?, resolved_by_observation_id = ?,
                resolved_at = ?, updated_at = ?
            WHERE finding_id = ?
            """,
            (resolution, resolved_by_observation_id, now, now, finding_id),
        )


# -------------------------------------------------------------------- provider evidence


class ProviderObservationRepository:
    """What the providers said, kept as it was said.

    There is no "current ownership" here to read. Ownership is derived from the newest
    reading of each provider together with the newest observation of the object, every time
    it is asked for — so improving the derivation improves what LocalPlane can say about
    evidence it collected months ago, and there is no stored conclusion to fall out of step
    with the evidence under it.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def insert(self, reading: dict[str, Any]) -> None:
        self._db.connection.execute(
            """
            INSERT INTO provider_observations (
                provider_observation_id, sweep_id, host_id, provider, source, status, reason,
                method, provider_version, observed_at, received_at, records, detail
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                reading["provider_observation_id"],
                reading["sweep_id"],
                reading["host_id"],
                reading["provider"],
                reading["source"],
                reading["status"],
                reading.get("reason"),
                reading["method"],
                reading.get("provider_version"),
                reading["observed_at"],
                reading["received_at"],
                to_json(reading.get("records", [])),
                to_json(reading.get("detail", {})),
            ),
        )

    def latest(self, host_id: str) -> list[ProviderReadingView]:
        """The newest reading from each provider source on this host.

        Chosen per source by a correlated subquery, the same way an object's current state
        is chosen: there is no table of current readings to maintain, and none to be wrong.
        """
        rows = self._db.query(
            """
            SELECT o.* FROM provider_observations o
            WHERE o.host_id = ?
              AND o.provider_observation_id = (
                  SELECT provider_observation_id FROM provider_observations
                  WHERE host_id = o.host_id AND provider = o.provider AND source = o.source
                  ORDER BY observed_at DESC, rowid DESC
                  LIMIT 1
              )
            ORDER BY o.provider
            """,
            (host_id,),
        )
        return [self._to_view(r) for r in rows]

    def for_sweep(self, sweep_id: str) -> list[ProviderReadingView]:
        rows = self._db.query(
            "SELECT * FROM provider_observations WHERE sweep_id = ? ORDER BY provider",
            (sweep_id,),
        )
        return [self._to_view(r) for r in rows]

    @staticmethod
    def _to_view(row: sqlite3.Row) -> ProviderReadingView:
        return ProviderReadingView(
            provider=row["provider"],
            source=row["source"],
            status=row["status"],
            observed_at=row["observed_at"],
            reason=row["reason"],
            version=row["provider_version"],
            records=tuple(from_json(row["records"], [])),
            detail=from_json(row["detail"], {}),
            provider_observation_id=row["provider_observation_id"],
            sweep_id=row["sweep_id"],
        )


# ------------------------------------------------------------------- ownership findings


@dataclass(frozen=True)
class OwnershipFindingRecord:
    finding_id: str
    finding_key: str
    host_id: str
    object_id: str
    finding_type: str
    subject: str
    status: str
    intent_id: str
    owner_provider: str
    owner_instance: str | None
    owner_label: str | None
    confidence: str
    evidence_source: str
    reason: str
    provider_observation_id: str | None
    observation_id: str | None
    sweep_id: str | None
    first_seen_at: str
    last_seen_at: str
    updated_at: str
    resolved_at: str | None
    resolution: str | None
    resolved_by_provider_observation_id: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "OwnershipFindingRecord":
        return cls(**dict(row))


class OwnershipFindingRepository:
    """LocalPlane's durable claims about objects it manages and does not own.

    The same lifecycle as :class:`FindingRepository`, and for the same reasons: one open
    row per logical conflict, a new row per episode, resolution instead of deletion, and a
    ``last_seen_at`` that moves only when the claim was proven again.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def open_for_key(self, finding_key: str) -> OwnershipFindingRecord | None:
        row = self._db.query_one(
            "SELECT * FROM ownership_findings WHERE finding_key = ? AND status = 'open'",
            (finding_key,),
        )
        return OwnershipFindingRecord.from_row(row) if row else None

    def open_for_object(self, object_id: str) -> list[OwnershipFindingRecord]:
        rows = self._db.query(
            "SELECT * FROM ownership_findings WHERE object_id = ? AND status = 'open' "
            "ORDER BY first_seen_at DESC, rowid DESC",
            (object_id,),
        )
        return [OwnershipFindingRecord.from_row(r) for r in rows]

    def get(self, finding_id: str) -> OwnershipFindingRecord | None:
        row = self._db.query_one(
            "SELECT * FROM ownership_findings WHERE finding_id = ?", (finding_id,)
        )
        return OwnershipFindingRecord.from_row(row) if row else None

    def list_for_host(
        self, host_id: str, status: str | None = "open", limit: int = 100
    ) -> list[OwnershipFindingRecord]:
        if status is None:
            rows = self._db.query(
                "SELECT * FROM ownership_findings WHERE host_id = ? "
                "ORDER BY first_seen_at DESC, rowid DESC LIMIT ?",
                (host_id, limit),
            )
        else:
            rows = self._db.query(
                "SELECT * FROM ownership_findings WHERE host_id = ? AND status = ? "
                "ORDER BY first_seen_at DESC, rowid DESC LIMIT ?",
                (host_id, status, limit),
            )
        return [OwnershipFindingRecord.from_row(r) for r in rows]

    def history_for_object(
        self, object_id: str, limit: int = 100
    ) -> list[OwnershipFindingRecord]:
        rows = self._db.query(
            "SELECT * FROM ownership_findings WHERE object_id = ? "
            "ORDER BY first_seen_at DESC, rowid DESC LIMIT ?",
            (object_id, limit),
        )
        return [OwnershipFindingRecord.from_row(r) for r in rows]

    def insert(
        self,
        *,
        finding_id: str,
        finding_key: str,
        host_id: str,
        object_id: str,
        finding_type: str,
        subject: str,
        intent_id: str,
        owner_provider: str,
        owner_instance: str | None,
        owner_label: str | None,
        confidence: str,
        evidence_source: str,
        reason: str,
        provider_observation_id: str | None,
        observation_id: str | None,
        sweep_id: str | None,
        now: str,
    ) -> None:
        self._db.connection.execute(
            """
            INSERT INTO ownership_findings (
                finding_id, finding_key, host_id, object_id, finding_type, subject, status,
                intent_id, owner_provider, owner_instance, owner_label, confidence,
                evidence_source, reason, provider_observation_id, observation_id, sweep_id,
                first_seen_at, last_seen_at, updated_at
            ) VALUES (?,?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                finding_id,
                finding_key,
                host_id,
                object_id,
                finding_type,
                subject,
                intent_id,
                owner_provider,
                owner_instance,
                owner_label,
                confidence,
                evidence_source,
                reason,
                provider_observation_id,
                observation_id,
                sweep_id,
                now,
                now,
                now,
            ),
        )

    def update_evidence(
        self,
        *,
        finding_id: str,
        intent_id: str,
        owner_provider: str,
        owner_instance: str | None,
        owner_label: str | None,
        confidence: str,
        evidence_source: str,
        reason: str,
        provider_observation_id: str | None,
        observation_id: str | None,
        sweep_id: str | None,
        now: str,
    ) -> None:
        """Refresh an open conflict from evidence that proved it again.

        Only called when the claim was actually re-proven, so ``last_seen_at`` moves
        unconditionally here. An evaluation that could not read the provider does not reach
        this method at all — it calls :meth:`touch`, which says "looked, could not tell".
        """
        self._db.connection.execute(
            """
            UPDATE ownership_findings SET
                intent_id = ?, owner_provider = ?, owner_instance = ?, owner_label = ?,
                confidence = ?, evidence_source = ?, reason = ?,
                provider_observation_id = ?, observation_id = ?, sweep_id = ?,
                last_seen_at = ?, updated_at = ?
            WHERE finding_id = ?
            """,
            (
                intent_id,
                owner_provider,
                owner_instance,
                owner_label,
                confidence,
                evidence_source,
                reason,
                provider_observation_id,
                observation_id,
                sweep_id,
                now,
                now,
                finding_id,
            ),
        )

    def touch(self, *, finding_id: str, now: str) -> None:
        """Record that the claim was evaluated and could not be confirmed either way.

        ``last_seen_at`` deliberately does not move: the pair then says exactly what
        happened — still open, last proven then, looked at since and the evidence was not
        readable. An unreadable provider is not grounds for closing anything.
        """
        self._db.connection.execute(
            "UPDATE ownership_findings SET updated_at = ? WHERE finding_id = ?",
            (now, finding_id),
        )

    def resolve(
        self,
        *,
        finding_id: str,
        resolution: str,
        resolved_by_provider_observation_id: str | None,
        now: str,
    ) -> None:
        self._db.connection.execute(
            """
            UPDATE ownership_findings SET
                status = 'resolved', resolution = ?, resolved_by_provider_observation_id = ?,
                resolved_at = ?, updated_at = ?
            WHERE finding_id = ?
            """,
            (resolution, resolved_by_provider_observation_id, now, now, finding_id),
        )


# --------------------------------------------------------------------------------- runs


#: The two shapes a plan's change can take, named once so that every reader and writer of a
#: preview or a Change branches on the same two strings. The vocabulary is the store's — the
#: CHECKs on both tables enumerate exactly these — and it is repeated here rather than
#: imported from the domain so that the persistence layer keeps its own copy of what it
#: enforces.
CHANGE_KIND_FIELD = "field"
CHANGE_KIND_ACTION = "action"


#: The columns of `run_previews` that hold JSON, and what an unreadable one falls back to.
#: Listed once so that reading a preview and writing one cannot disagree about which is
#: which — a mismatch would surface as a string where a list was expected, at the point
#: furthest from the cause.
_PREVIEW_JSON: dict[str, Any] = {
    "ownership_claims": [],
    "ownership_gaps": [],
    "provider_readings": {},
    "protection_reasons": [],
    "protection_unresolved": [],
    "protection_missing_evidence": [],
    "protection_assessments": [],
    "authorization_assessment": None,
    "lifecycle_context": None,
    "self_impact": None,
    "risk_factors": [],
    "confirmation_reasons": [],
    "execution_blockers": [],
    "guard_prerequisites": [],
    "guard_unmet": [],
}

_PREVIEW_BOOLEAN = (
    "confirmation_required",
    "confirmation_token_issued",
    "capability_declared",
    "recovery_rollback_possible",
    "recovery_armed",
    "guard_armed",
    "verification_executed",
)


@dataclass(frozen=True)
class RunPreviewRecord:
    """A published plan, exactly as it was published.

    Immutable in the store — a trigger refuses every UPDATE — so this record is a faithful
    copy of what an operator was shown and not a rendering of what would be decided now.
    """

    preview_id: str
    preview_digest: str
    digest_version: int
    operation: str

    #: ``field`` or ``action``. The discriminator, and the only honest way to read the two
    #: halves below: which of them carries anything is decided here, not by probing for
    #: nulls.
    change_kind: str

    field: str | None
    value_type: str | None
    current_value: bool | int | None
    desired_value: bool | int | None

    action: str | None
    observed_state: str | None
    expected_state: str | None

    intent_id: str | None
    intent_version: int | None
    intent_capability: str | None
    intent_provider: str | None

    observation_id: str
    sweep_id: str
    observed_at: str
    drift_finding_id: str | None

    ownership_state: str
    ownership_reason: str
    ownership_claims: list[dict[str, Any]]
    ownership_gaps: list[str]
    provider_readings: dict[str, Any]

    protection_status: str
    protection_reasons: list[str]
    protection_unresolved: list[str]
    protection_management_path: str
    protection_reason: str
    protection_missing_evidence: list[str]
    protection_evidence_id: str | None
    protection_evidence_observed_at: str | None
    protection_assessments: list[dict[str, Any]]

    authorization_assessment: dict[str, Any] | None
    lifecycle_context: dict[str, Any] | None
    self_impact: dict[str, Any] | None

    risk_tier: str
    risk_factors: list[dict[str, Any]]

    confirmation_required: bool
    confirmation_method: str
    confirmation_source: str
    confirmation_reasons: list[str]
    confirmation_policy: str
    confirmation_token_issued: bool

    execution_availability: str
    execution_eligibility: str
    execution_blockers: list[str]
    execution_provider: str | None
    required_capability: str
    capability_declared: bool

    guard_availability: str
    guard_reason: str
    guard_window_s: int
    guard_prerequisites: list[str]
    guard_unmet: list[str]
    guard_guarantee: str
    guard_armed: bool

    recovery_mode: str
    recovery_rollback_possible: bool
    recovery_armed: bool
    recovery_guarantee: str
    recovery_reason: str

    verification_capability: str
    verification_provider: str
    verification_condition: str
    verification_executed: bool

    published_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row | dict[str, Any]) -> "RunPreviewRecord":
        values = dict(row)
        for column, fallback in _PREVIEW_JSON.items():
            values[column] = from_json(values[column], fallback)
        for column in _PREVIEW_BOOLEAN:
            values[column] = bool(values[column])
        # Both ends of a *field* plan are stored as integers, exactly as an intent's
        # controlled value is, with `value_type` saying how to read them back. An action has
        # neither, and reading one back would mean inventing a type for a null.
        if values["change_kind"] == CHANGE_KIND_FIELD:
            for column in ("current_value", "desired_value"):
                values[column] = _typed(values["value_type"], values[column])
        return cls(**values)


@dataclass(frozen=True)
class RunRecord:
    """One Run and the plan it published. Never a Change: no host was written."""

    run_id: str
    host_id: str
    object_id: str
    operation: str
    state: str
    host_effect: str
    created_at: str
    cancelled_at: str | None
    finished_at: str | None
    preview: RunPreviewRecord


class RunRepository:
    """Reads and writes Runs and the immutable previews they publish.

    Both writes belong inside a transaction opened by the caller. A preview with no Run, or
    a Run naming a preview that was never written, are the two half-states this pair can
    produce, and neither may be reachable through a partial write.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    # ------------------------------------------------------------------------- writes

    def insert_preview(self, preview: dict[str, Any]) -> None:
        """Write one published plan. Nothing may update it afterwards; a trigger says so."""
        columns = list(preview)
        self._db.connection.execute(
            f"INSERT INTO run_previews ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            tuple(preview[c] for c in columns),
        )

    def insert_run(
        self,
        *,
        run_id: str,
        host_id: str,
        object_id: str,
        operation: str,
        state: str,
        preview_id: str,
        created_at: str,
    ) -> None:
        """Record that a Run exists and which plan it published.

        ``host_effect`` is written as ``'none'`` and the column will not accept anything
        else, exactly as it will not on a management transition or an intent revision.
        Planning is not applying, and this table cannot say otherwise.
        """
        self._db.connection.execute(
            """
            INSERT INTO runs (
                run_id, host_id, object_id, operation, state, preview_id, host_effect,
                created_at, cancelled_at
            ) VALUES (?,?,?,?,?,?,'none',?,NULL)
            """,
            (run_id, host_id, object_id, operation, state, preview_id, created_at),
        )

    def cancel(self, *, run_id: str, now: str) -> None:
        """Mark a Run cancelled. The preview it published stays exactly as it is."""
        self._db.connection.execute(
            "UPDATE runs SET state = 'cancelled', cancelled_at = ?, finished_at = ? "
            "WHERE run_id = ?",
            (now, now, run_id),
        )

    def set_state(
        self,
        *,
        run_id: str,
        state: str | None = None,
        host_effect: str | None = None,
        finished_at: str | None = None,
    ) -> None:
        """Move a Run's lifecycle, and what it may claim about the host.

        ``host_effect`` is passed only where it moves. It is not a field a caller edits
        freely: the schema will refuse a state and an effect that disagree — ``failed``
        with anything but ``none``, ``succeeded`` with anything but ``written`` — so a
        wrong pairing is a failed write rather than a stored falsehood.

        Belongs inside a transaction opened by the caller, because every state change in
        this engine lands with the row or rows that justify it.
        """
        assignments: list[str] = []
        params: list[Any] = []
        if state is not None:
            assignments.append("state = ?")
            params.append(state)
        if host_effect is not None:
            assignments.append("host_effect = ?")
            params.append(host_effect)
        if finished_at is not None:
            assignments.append("finished_at = ?")
            params.append(finished_at)
        if not assignments:
            return
        params.append(run_id)
        self._db.connection.execute(
            f"UPDATE runs SET {', '.join(assignments)} WHERE run_id = ?", tuple(params)
        )

    # -------------------------------------------------------------------------- reads

    def get(self, run_id: str) -> RunRecord | None:
        row = self._db.query_one(
            f"{_RUN_SELECT} WHERE r.run_id = ?",
            (run_id,),
        )
        return self._to_record(row) if row else None

    def list_for_host(
        self,
        host_id: str,
        *,
        state: str | None = None,
        object_id: str | None = None,
        limit: int = 100,
    ) -> list[RunRecord]:
        clauses = ["r.host_id = ?"]
        params: list[Any] = [host_id]
        if state is not None:
            clauses.append("r.state = ?")
            params.append(state)
        if object_id is not None:
            clauses.append("r.object_id = ?")
            params.append(object_id)
        params.append(limit)
        rows = self._db.query(
            f"{_RUN_SELECT} WHERE {' AND '.join(clauses)} "
            "ORDER BY r.created_at DESC, r.rowid DESC LIMIT ?",
            tuple(params),
        )
        return [self._to_record(r) for r in rows]

    def for_object(self, object_id: str, limit: int = 100) -> list[RunRecord]:
        rows = self._db.query(
            f"{_RUN_SELECT} WHERE r.object_id = ? ORDER BY r.created_at DESC, r.rowid DESC "
            "LIMIT ?",
            (object_id, limit),
        )
        return [self._to_record(r) for r in rows]

    @staticmethod
    def _to_record(row: sqlite3.Row) -> RunRecord:
        values = dict(row)
        run = {
            key: values.pop(key)
            for key in (
                "run_id",
                "host_id",
                "object_id",
                "operation",
                "state",
                "host_effect",
                "created_at",
                "cancelled_at",
                "finished_at",
            )
        }
        # `operation` is on both tables and the join would collide on the name, so the
        # preview's copy is aliased in the SELECT and restored to its own name here.
        values["operation"] = values.pop("preview_operation")
        return RunRecord(**run, preview=RunPreviewRecord.from_row(values))


_RUN_SELECT = """
    SELECT r.run_id, r.host_id, r.object_id, r.operation, r.state, r.host_effect,
           r.created_at, r.cancelled_at, r.finished_at,
           p.preview_id, p.preview_digest, p.digest_version,
           p.operation AS preview_operation,
           p.change_kind,
           p.field, p.value_type, p.current_value, p.desired_value,
           p.action, p.observed_state, p.expected_state,
           p.intent_id, p.intent_version, p.intent_capability, p.intent_provider,
           p.observation_id, p.sweep_id, p.observed_at, p.drift_finding_id,
           p.ownership_state, p.ownership_reason, p.ownership_claims, p.ownership_gaps,
           p.provider_readings,
           p.protection_status, p.protection_reasons, p.protection_unresolved,
           p.protection_management_path, p.protection_reason, p.protection_missing_evidence,
           p.protection_evidence_id, p.protection_evidence_observed_at,
           p.protection_assessments, p.authorization_assessment, p.lifecycle_context,
           p.self_impact,
           p.risk_tier, p.risk_factors,
           p.confirmation_required, p.confirmation_method, p.confirmation_source,
           p.confirmation_reasons, p.confirmation_policy, p.confirmation_token_issued,
           p.execution_availability, p.execution_eligibility, p.execution_blockers,
           p.execution_provider, p.required_capability, p.capability_declared,
           p.guard_availability, p.guard_reason, p.guard_window_s,
           p.guard_prerequisites, p.guard_unmet, p.guard_guarantee, p.guard_armed,
           p.recovery_mode, p.recovery_rollback_possible, p.recovery_armed,
           p.recovery_guarantee, p.recovery_reason,
           p.verification_capability, p.verification_provider, p.verification_condition,
           p.verification_executed,
           p.published_at
    FROM runs r
    JOIN run_previews p ON p.preview_id = r.preview_id
"""


class ManagementPathRepository:
    """Raw evidence about how connections reach LocalPlane. Append-only, never rewritten.

    The store holds what was observed and never the conclusion drawn from it. "The
    management path is eth0" depends on facts that move independently of this evidence —
    which addresses an object carries, whether its own observation is still current — so it
    is derived at read time, exactly as ownership is derived from `provider_observations`.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def insert(self, observation: dict[str, Any]) -> None:
        """Record one observation. Belongs inside a transaction opened by the caller."""
        columns = list(observation)
        self._db.connection.execute(
            f"INSERT INTO management_path_observations ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            tuple(observation[c] for c in columns),
        )

    def newest_matching(
        self, *, host_id: str, peer_address: str, local_endpoint_address: str
    ) -> ManagementPathObservation | None:
        """The newest evidence taken from *this* connection's two endpoints.

        Both addresses are in the WHERE clause rather than filtered afterwards, so there is
        no code path on which evidence about one operator's connection can be handed to
        another's. The ephemeral source port is deliberately not part of the match: it
        changes on every connection, so matching on it would mean no evidence was ever
        reusable and every read would need a write to answer.
        """
        row = self._db.query_one(
            "SELECT * FROM management_path_observations "
            "WHERE host_id = ? AND transport_peer_address = ? AND local_endpoint_address = ? "
            "ORDER BY observed_at DESC, rowid DESC LIMIT 1",
            (host_id, peer_address, local_endpoint_address),
        )
        return None if row is None else self._to_record(row)

    def get(self, observation_id: str) -> ManagementPathObservation | None:
        row = self._db.query_one(
            "SELECT * FROM management_path_observations WHERE observation_id = ?",
            (observation_id,),
        )
        return None if row is None else self._to_record(row)

    def recent(self, host_id: str, limit: int = 20) -> list[ManagementPathObservation]:
        rows = self._db.query(
            "SELECT * FROM management_path_observations WHERE host_id = ? "
            "ORDER BY observed_at DESC, rowid DESC LIMIT ?",
            (host_id, limit),
        )
        return [self._to_record(r) for r in rows]

    @staticmethod
    def _to_record(row: sqlite3.Row) -> ManagementPathObservation:
        values = dict(row)
        return ManagementPathObservation(
            observation_id=values["observation_id"],
            host_id=values["host_id"],
            observed_at=values["observed_at"],
            agent_instance_id=values["agent_instance_id"],
            transport_peer_address=values["transport_peer_address"],
            transport_peer_family=values["transport_peer_family"],
            local_endpoint_address=values["local_endpoint_address"],
            local_endpoint_family=values["local_endpoint_family"],
            capability=values["capability"],
            provider=values["provider"],
            provider_version=values["provider_version"],
            method=values["method"],
            route=RouteEvidence(
                status=values["route_status"],
                reason=values["route_reason"],
                family=values["route_family"],
                destination=values["route_destination"],
                destination_prefix_length=values["route_destination_prefix_length"],
                preferred_source=values["route_preferred_source"],
                gateway=values["route_gateway"],
                oif_index=values["route_oif_index"],
                table=values["route_table"],
                route_type=values["route_type"],
                scope=values["route_scope"],
                protocol=values["route_protocol"],
                priority=values["route_priority"],
                error=from_json(values["route_error"], None),
            ),
        )


# ------------------------------------------------------------------------- write boundary


@dataclass(frozen=True)
class ConfirmationRecord:
    """One operator confirmation: durable, bound to a Run and a plan, single-use.

    Bound to ``preview_id`` and not only to ``preview_digest``, because two identical
    concurrent plans share one digest and a confirmation that could not tell them apart
    would authorise the wrong Run. The digest is kept beside it as evidence of *what* was
    confirmed, never as the thing that authorises.

    There is no actor. LocalPlane has no authentication, so ``source`` records the only
    thing that is true: an unauthenticated request satisfied the requirement.
    """

    confirmation_id: str
    run_id: str
    #: ``apply`` or ``recovery_retry``. The same act — a person saying yes to a write that
    #: may happen — against two different documents, and the column is what keeps one from
    #: being read as the other. Both obey the same single-use rule.
    purpose: str
    preview_id: str
    preview_digest: str
    digest_version: int
    required_method: str
    method: str
    #: What the operator actually wrote, when the method was ``typed``, and ``None`` when it
    #: was not. Stored as their statement rather than compared and discarded: a record that
    #: says only "typed" has kept the ceremony and thrown away the evidence.
    typed_statement: str | None
    policy: str
    source: str
    satisfied_at: str
    consumed_at: str | None
    consumed_by_attempt_id: str | None

    @property
    def consumed(self) -> bool:
        return self.consumed_at is not None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ConfirmationRecord":
        return cls(**dict(row))


#: The one value ``run_confirmations.source`` may hold in a build with no authentication.
CONFIRMATION_SOURCE_UNAUTHENTICATED = "unauthenticated_request"

#: What a confirmation authorises. An apply confirmation authorises crossing the write
#: boundary once; a recovery confirmation authorises one recovery retry of an already-crossed
#: Change to dispatch a *new* mutation; a self-impact override answers the further question an
#: apply confirmation does not ask — whether the operator accepts that this operation may take
#: LocalPlane itself away. They live in one table because the rule that matters — authority is
#: single-use — must have one implementation, not three that can drift.
#:
#: They are not interchangeable and no code path may substitute one for another: each is read
#: by purpose, each has its own partial unique index, and the write boundary demands the ones
#: it needs through separate triggers that do not know about each other.
CONFIRMATION_PURPOSE_APPLY = "apply"
CONFIRMATION_PURPOSE_RECOVERY_RETRY = "recovery_retry"
CONFIRMATION_PURPOSE_SELF_IMPACT_OVERRIDE = "self_impact_override"


class ConfirmationRepository:
    """Records confirmations and consumes them exactly once."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def insert(self, confirmation: dict[str, Any]) -> None:
        """Record a satisfied confirmation. Inside a transaction opened by the caller."""
        columns = list(confirmation)
        self._db.connection.execute(
            f"INSERT INTO run_confirmations ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            tuple(confirmation[c] for c in columns),
        )

    def for_run(
        self, run_id: str, purpose: str = CONFIRMATION_PURPOSE_APPLY
    ) -> ConfirmationRecord | None:
        """The confirmation of one purpose for this Run.

        Scoped by purpose because a Run may now hold both: the one that authorised its apply,
        consumed long ago, and — if that apply ended in a recovery hold — one authorising a
        retry. A read that returned "the confirmation" without saying which would eventually
        hand the wrong one to whichever caller asked last.
        """
        row = self._db.query_one(
            "SELECT * FROM run_confirmations WHERE run_id = ? AND purpose = ? "
            "ORDER BY satisfied_at DESC, rowid DESC",
            (run_id, purpose),
        )
        return None if row is None else ConfirmationRecord.from_row(row)

    def outstanding(self, run_id: str, purpose: str) -> ConfirmationRecord | None:
        """The unconsumed confirmation of this purpose, if one is waiting to be used.

        A partial unique index allows at most one, so authority never accumulates: a second
        cannot be recorded while one is outstanding, and a new one becomes possible only once
        the previous has been consumed by an attempt.
        """
        row = self._db.query_one(
            "SELECT * FROM run_confirmations "
            "WHERE run_id = ? AND purpose = ? AND consumed_at IS NULL",
            (run_id, purpose),
        )
        return None if row is None else ConfirmationRecord.from_row(row)

    def for_run_all(self, run_id: str) -> list[ConfirmationRecord]:
        rows = self._db.query(
            "SELECT * FROM run_confirmations WHERE run_id = ? ORDER BY satisfied_at, rowid",
            (run_id,),
        )
        return [ConfirmationRecord.from_row(r) for r in rows]

    def consume(self, *, confirmation_id: str, attempt_id: str, now: str) -> bool:
        """Consume a confirmation for one execution attempt. Returns whether it was ours.

        The ``consumed_at IS NULL`` guard is the whole mechanism, and it is in the WHERE
        clause rather than in a preceding read: two applies racing for the same
        confirmation both see it unconsumed, and only the one whose UPDATE changes a row
        may proceed. A trigger refuses the loser's write in any case, so the guarantee does
        not rest on this method being called correctly.
        """
        cursor = self._db.connection.execute(
            "UPDATE run_confirmations SET consumed_at = ?, consumed_by_attempt_id = ? "
            "WHERE confirmation_id = ? AND consumed_at IS NULL",
            (now, attempt_id, confirmation_id),
        )
        return cursor.rowcount == 1


@dataclass(frozen=True)
class CheckpointRecord:
    """The durable material recovery rests on, written before anything can be written.

    "Recovery is armed" means this row exists. It deliberately does not mean that a running
    process is holding the previous value, which is what every build before the write
    boundary could have claimed and none of them did.
    """

    checkpoint_id: str
    run_id: str
    preview_id: str
    host_id: str
    object_id: str
    intent_id: str
    intent_version: int
    field: str
    value_type: str
    before_value: bool | int
    desired_value: bool | int
    observation_id: str
    observed_at: str
    protection_management_path: str
    protection_evidence_id: str | None
    execution_correlation: dict[str, Any]
    armed_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "CheckpointRecord":
        values = dict(row)
        values["execution_correlation"] = from_json(values["execution_correlation"], {})
        for column in ("before_value", "desired_value"):
            values[column] = _typed(values["value_type"], values[column])
        return cls(**values)


class CheckpointRepository:
    """Writes and reads recovery checkpoints. Immutable once written; a trigger says so."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def insert(self, checkpoint: dict[str, Any]) -> None:
        columns = list(checkpoint)
        self._db.connection.execute(
            f"INSERT INTO run_checkpoints ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            tuple(checkpoint[c] for c in columns),
        )

    def for_run(self, run_id: str) -> CheckpointRecord | None:
        row = self._db.query_one("SELECT * FROM run_checkpoints WHERE run_id = ?", (run_id,))
        return None if row is None else CheckpointRecord.from_row(row)

    def get(self, checkpoint_id: str) -> CheckpointRecord | None:
        row = self._db.query_one(
            "SELECT * FROM run_checkpoints WHERE checkpoint_id = ?", (checkpoint_id,)
        )
        return None if row is None else CheckpointRecord.from_row(row)


@dataclass(frozen=True)
class ChangeRecord:
    """The durable record that LocalPlane entered a path on which a host write may occur.

    Not a claim that one happened — ``mutation_outcome`` answers that, and it has three
    values because there are three truths. A Change may exist with ``not_written``: the
    boundary was crossed and the privileged path refused before the kernel could accept
    anything, which is a true and useful thing to have recorded.
    """

    change_id: str
    run_id: str
    preview_id: str
    #: ``None`` for an action. There is no previous value to restore, so there is no
    #: checkpoint to name, and the store refuses one that claims otherwise.
    checkpoint_id: str | None
    host_id: str
    object_id: str
    operation: str

    change_kind: str
    field: str | None
    value_type: str | None
    before_value: bool | int | None
    desired_value: bool | int | None

    action: str | None
    observed_state: str | None
    expected_state: str | None

    #: The stable material the executor used to reach this target, and the lifecycle
    #: evidence a verification is judged against. Opaque here on purpose.
    execution_correlation: dict[str, Any]
    created_at: str

    apply_attempt_id: str
    dispatch_began_at: str | None
    mutation_outcome: str | None
    mutation_reason: str | None
    mutation_provider: str | None
    mutation_method: str | None
    mutation_detail: dict[str, Any]
    settled_at: str | None
    host_effect: str

    verification_outcome: str
    verification_observation_id: str | None
    verification_observed_value: bool | int | None
    verification_observed_state: str | None
    verification_reason: str | None

    rollback_required: bool
    rollback_attempt_id: str | None
    rollback_dispatch_began_at: str | None
    rollback_outcome: str | None
    rollback_reason: str | None
    rollback_detail: dict[str, Any]
    rollback_verification_outcome: str
    rollback_verification_observation_id: str | None
    rollback_verification_observed_value: bool | int | None

    result: str
    recovery_reason: str | None
    finished_at: str | None

    @property
    def dispatch_began(self) -> bool:
        return self.dispatch_began_at is not None

    @property
    def recovery_required(self) -> bool:
        """Derived, not stored. One fact, one home: the result *is* the answer."""
        return self.result == "recovery_required"

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ChangeRecord":
        values = dict(row)
        values["mutation_detail"] = from_json(values["mutation_detail"], {})
        values["rollback_detail"] = from_json(values["rollback_detail"], {})
        values["execution_correlation"] = from_json(values["execution_correlation"], {})
        values["rollback_required"] = bool(values["rollback_required"])
        if values["change_kind"] == CHANGE_KIND_FIELD:
            for column in (
                "before_value",
                "desired_value",
                "verification_observed_value",
                "rollback_verification_observed_value",
            ):
                values[column] = _typed(values["value_type"], values[column])
        return cls(**values)


class ChangeRepository:
    """Writes and reads Changes.

    Every write here belongs inside a transaction opened by the caller, and the ordering of
    those transactions is the crash-safety argument rather than an implementation detail:
    the row exists before the dispatch marker, and the dispatch marker exists before the
    request is sent.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def insert(self, change: dict[str, Any]) -> None:
        columns = list(change)
        self._db.connection.execute(
            f"INSERT INTO changes ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            tuple(change[c] for c in columns),
        )

    def update(self, change_id: str, values: dict[str, Any]) -> None:
        """Move what became of a Change. What it was *about* is refused by a trigger."""
        assignments = ", ".join(f"{column} = ?" for column in values)
        self._db.connection.execute(
            f"UPDATE changes SET {assignments} WHERE change_id = ?",
            (*values.values(), change_id),
        )

    def get(self, change_id: str) -> ChangeRecord | None:
        row = self._db.query_one("SELECT * FROM changes WHERE change_id = ?", (change_id,))
        return None if row is None else ChangeRecord.from_row(row)

    def for_run(self, run_id: str) -> ChangeRecord | None:
        row = self._db.query_one("SELECT * FROM changes WHERE run_id = ?", (run_id,))
        return None if row is None else ChangeRecord.from_row(row)

    def list_for_host(
        self,
        host_id: str,
        *,
        object_id: str | None = None,
        result: str | None = None,
        limit: int = 100,
    ) -> list[ChangeRecord]:
        clauses = ["host_id = ?"]
        params: list[Any] = [host_id]
        if object_id is not None:
            clauses.append("object_id = ?")
            params.append(object_id)
        if result is not None:
            clauses.append("result = ?")
            params.append(result)
        params.append(limit)
        rows = self._db.query(
            f"SELECT * FROM changes WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            tuple(params),
        )
        return [ChangeRecord.from_row(r) for r in rows]

    def unsettled(self, host_id: str | None = None) -> list[ChangeRecord]:
        """Changes that crossed the boundary and never recorded what became of them.

        The read that makes the crash window visible rather than merely acknowledged: on
        restart these are the rows whose mutation outcome has to be interpreted, and
        ``domain.changes.outcome_on_recovery`` is what interprets them.
        """
        if host_id is None:
            rows = self._db.query(
                "SELECT * FROM changes WHERE result = 'in_flight' ORDER BY created_at"
            )
        else:
            rows = self._db.query(
                "SELECT * FROM changes WHERE result = 'in_flight' AND host_id = ? "
                "ORDER BY created_at",
                (host_id,),
            )
        return [ChangeRecord.from_row(r) for r in rows]


@dataclass(frozen=True)
class RecoveryAttemptRecord:
    """One recovery action against one Change: a retry, or a person releasing the hold.

    A **later, separate event**. The Change it names is not touched by it and never will be:
    ``result`` goes on saying ``recovery_required``, ``mutation_outcome`` goes on saying what
    the original dispatch produced, and this row says what somebody did about it afterwards.
    Rewriting the first to record the second would make the history say something that did
    not happen, which is precisely the failure the three-valued mutation vocabulary exists to
    prevent one layer down.

    ``evidence_*`` is the reading taken **before** anything was written and what the
    operation made of it; ``verification_*`` is the reading taken **after** a new write.
    Keeping them apart is what lets "the end state was already there" and "the end state is
    there because we wrote it again" stay different findings.
    """

    attempt_id: str
    change_id: str
    run_id: str
    host_id: str
    object_id: str
    sequence: int

    kind: str
    started_at: str

    #: The management-path evidence belonging to *this* operator request, never the one the
    #: original Run rested on.
    protection_management_path: str
    protection_evidence_id: str | None

    evidence_outcome: str
    evidence_observation_id: str | None
    evidence_observed_value: bool | int | None
    evidence_observed_state: str | None
    evidence_reason: str | None

    confirmation_id: str | None
    execution_correlation: dict[str, Any] | None
    mutation_attempt_id: str | None
    dispatch_began_at: str | None
    mutation_outcome: str | None
    mutation_reason: str | None
    mutation_provider: str | None
    mutation_method: str | None
    mutation_detail: dict[str, Any]
    host_effect: str

    verification_outcome: str
    verification_observation_id: str | None
    verification_observed_value: bool | int | None
    verification_observed_state: str | None
    verification_reason: str | None

    outcome: str
    refusal_code: str | None
    releases_hold: bool
    finished_at: str | None

    operator_statement: str | None
    note: str | None

    @property
    def dispatch_began(self) -> bool:
        return self.dispatch_began_at is not None

    @property
    def wrote(self) -> bool:
        """Whether this attempt itself put something on the host, or may have."""
        return self.host_effect != "none"

    @classmethod
    def from_row(cls, row: sqlite3.Row, value_type: str | None = None) -> "RecoveryAttemptRecord":
        values = dict(row)
        values["mutation_detail"] = from_json(values["mutation_detail"], {})
        values["execution_correlation"] = (
            None if values["execution_correlation"] is None
            else from_json(values["execution_correlation"], {})
        )
        values["releases_hold"] = bool(values["releases_hold"])
        # The two observed values are stored the way every other controlled value is — as an
        # integer, with the Change's `value_type` saying how to read it back. An action has no
        # value type and no value, and inventing one for a null would be inventing a type.
        if value_type is not None:
            for column in ("evidence_observed_value", "verification_observed_value"):
                values[column] = _typed(value_type, values[column])
        return cls(**values)


class RecoveryAttemptRepository:
    """The append-only history of what was done about a recovery hold.

    Two structural guarantees live in partial unique indexes rather than in this class, so
    they hold against a second process and against SQL typed into a shell: **one attempt in
    flight per Change**, and **one release per hold, ever**. This repository's job is to make
    the read-then-write pairs happen inside the caller's transaction so the indexes are
    reached rather than raced past.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def insert(self, attempt: dict[str, Any]) -> None:
        columns = list(attempt)
        self._db.connection.execute(
            f"INSERT INTO change_recovery_attempts ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            tuple(attempt[c] for c in columns),
        )

    def update(self, attempt_id: str, values: dict[str, Any]) -> None:
        """Move what became of an attempt. What it was about is refused by a trigger."""
        assignments = ", ".join(f"{column} = ?" for column in values)
        self._db.connection.execute(
            f"UPDATE change_recovery_attempts SET {assignments} WHERE attempt_id = ?",
            (*values.values(), attempt_id),
        )

    def next_sequence(self, change_id: str) -> int:
        row = self._db.query_one(
            "SELECT COALESCE(MAX(sequence), 0) AS n FROM change_recovery_attempts "
            "WHERE change_id = ?",
            (change_id,),
        )
        return (int(row["n"]) if row is not None else 0) + 1

    def get(self, attempt_id: str, value_type: str | None = None) -> "RecoveryAttemptRecord | None":
        row = self._db.query_one(
            "SELECT * FROM change_recovery_attempts WHERE attempt_id = ?", (attempt_id,)
        )
        return None if row is None else RecoveryAttemptRecord.from_row(row, value_type)

    def for_change(
        self, change_id: str, value_type: str | None = None
    ) -> list["RecoveryAttemptRecord"]:
        rows = self._db.query(
            "SELECT * FROM change_recovery_attempts WHERE change_id = ? ORDER BY sequence",
            (change_id,),
        )
        return [RecoveryAttemptRecord.from_row(r, value_type) for r in rows]

    def in_flight(self, change_id: str) -> "RecoveryAttemptRecord | None":
        row = self._db.query_one(
            "SELECT * FROM change_recovery_attempts "
            "WHERE change_id = ? AND outcome = 'in_flight'",
            (change_id,),
        )
        return None if row is None else RecoveryAttemptRecord.from_row(row)

    def released_change_ids(self, change_ids: list[str]) -> set[str]:
        """Which of these Changes have had their hold released. One query, not one each.

        A list endpoint asking per row would be an N+1 hidden inside a renderer, and the
        answer is a single indexed lookup for the whole page.
        """
        if not change_ids:
            return set()
        placeholders = ", ".join("?" for _ in change_ids)
        rows = self._db.query(
            f"SELECT change_id FROM change_recovery_attempts "
            f"WHERE releases_hold = 1 AND change_id IN ({placeholders})",
            tuple(change_ids),
        )
        return {row["change_id"] for row in rows}

    def release_of(self, change_id: str) -> "RecoveryAttemptRecord | None":
        """The attempt that gave this object back, if one has. At most one can exist."""
        row = self._db.query_one(
            "SELECT * FROM change_recovery_attempts "
            "WHERE change_id = ? AND releases_hold = 1",
            (change_id,),
        )
        return None if row is None else RecoveryAttemptRecord.from_row(row)

    def unsettled(self, host_id: str | None = None) -> list["RecoveryAttemptRecord"]:
        """Attempts that began and never recorded what became of them.

        The recovery path's own crash window, read on restart by the same rule the apply
        path's is: dispatch began and nothing came back is ``write_unknown``, and either way
        the hold is kept. Nothing is retried automatically — a record nobody has looked at
        since a crash is not authority to write to a host.
        """
        if host_id is None:
            rows = self._db.query(
                "SELECT * FROM change_recovery_attempts WHERE outcome = 'in_flight' "
                "ORDER BY started_at"
            )
        else:
            rows = self._db.query(
                "SELECT * FROM change_recovery_attempts "
                "WHERE outcome = 'in_flight' AND host_id = ? ORDER BY started_at",
                (host_id,),
            )
        return [RecoveryAttemptRecord.from_row(r) for r in rows]


@dataclass(frozen=True)
class RunEventRecord:
    """One typed entry in a Run's transcript. Append-only; triggers refuse both edits."""

    event_id: str
    run_id: str
    change_id: str | None
    sequence: int
    event: str
    state_from: str | None
    state_to: str | None
    occurred_at: str
    detail: dict[str, Any]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "RunEventRecord":
        values = dict(row)
        values["detail"] = from_json(values["detail"], {})
        return cls(**values)


class RunEventRepository:
    """The append-only transcript.

    ``sequence`` is allocated under the caller's write lock from what is already there, so
    two writers cannot mint the same number — and if they somehow did, ``UNIQUE (run_id,
    sequence)`` refuses the second rather than letting a transcript claim two versions of
    one moment.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def append(
        self,
        *,
        event_id: str,
        run_id: str,
        event: str,
        occurred_at: str,
        change_id: str | None = None,
        state_from: str | None = None,
        state_to: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> int:
        row = self._db.query_one(
            "SELECT COALESCE(MAX(sequence), 0) AS n FROM run_events WHERE run_id = ?",
            (run_id,),
        )
        sequence = int(row["n"]) + 1 if row is not None else 1
        self._db.connection.execute(
            "INSERT INTO run_events (event_id, run_id, change_id, sequence, event, "
            "state_from, state_to, occurred_at, detail) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                run_id,
                change_id,
                sequence,
                event,
                state_from,
                state_to,
                occurred_at,
                to_json(detail or {}),
            ),
        )
        return sequence

    def for_run(self, run_id: str) -> list[RunEventRecord]:
        rows = self._db.query(
            "SELECT * FROM run_events WHERE run_id = ? ORDER BY sequence", (run_id,)
        )
        return [RunEventRecord.from_row(r) for r in rows]

    def for_change(self, change_id: str) -> list[RunEventRecord]:
        rows = self._db.query(
            "SELECT * FROM run_events WHERE change_id = ? ORDER BY sequence", (change_id,)
        )
        return [RunEventRecord.from_row(r) for r in rows]


@dataclass(frozen=True)
class RunGuardRecord:
    """One connection guard: what was armed, by whom, until when, and how it ended.

    The durable half of a mechanism that lives somewhere else. The guard itself is held by
    the agent on the host — which is the point of it, because the backend is expected to be
    a container and a guard that died with the backend would protect nothing — and this row
    is what LocalPlane knows about it: enough to interrogate it afterwards, enough to say
    what it was for, and never enough to be mistaken for the guard.

    **Two moves and a settlement.** Inserted with ``arm_began_at`` and no ``armed_at`` — the
    request has gone out and the answer is not back, which is a crash window and is meant to
    be visible. Updated with the holder's own answer, at which point the boundary opens.
    Settled exactly once, with what the guard actually did.
    """

    guard_id: str
    run_id: str
    preview_id: str
    checkpoint_id: str
    host_id: str
    object_id: str

    protection_management_path: str
    protection_evidence_id: str
    confirmation_id: str

    window_s: int
    arm_began_at: str
    armed_at: str | None
    expires_at: str | None
    holder_id: str | None
    reversal_attempt_id: str

    kept_at: str | None
    kept_evidence_id: str | None

    settled_at: str | None
    settled_phase: str | None
    settled_reason: str | None
    fired_at: str | None
    reversal_outcome: str | None
    reversal_reason: str | None
    settled_detail: dict[str, Any]

    @property
    def armed(self) -> bool:
        """Whether the holder confirmed it has this guard. Not whether we asked for it."""
        return self.armed_at is not None

    @property
    def settled(self) -> bool:
        return self.settled_at is not None

    @classmethod
    def from_row(cls, row: sqlite3.Row | dict[str, Any]) -> "RunGuardRecord":
        values = dict(row)
        values["settled_detail"] = from_json(values["settled_detail"], {})
        return cls(**values)


class GuardRepository:
    """Connection guards: armed, settled, and read back afterwards."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def insert(self, values: dict[str, Any]) -> None:
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        self._db.connection.execute(
            f"INSERT INTO run_guards ({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        )

    def update(self, guard_id: str, values: dict[str, Any]) -> None:
        assignments = ", ".join(f"{column} = ?" for column in values)
        self._db.connection.execute(
            f"UPDATE run_guards SET {assignments} WHERE guard_id = ?",
            (*values.values(), guard_id),
        )

    def get(self, guard_id: str) -> RunGuardRecord | None:
        row = self._db.query_one("SELECT * FROM run_guards WHERE guard_id = ?", (guard_id,))
        return None if row is None else RunGuardRecord.from_row(row)

    def for_run(self, run_id: str) -> RunGuardRecord | None:
        row = self._db.query_one("SELECT * FROM run_guards WHERE run_id = ?", (run_id,))
        return None if row is None else RunGuardRecord.from_row(row)

    def unsettled(self, host_id: str | None = None) -> list[RunGuardRecord]:
        """Guards that were armed, or asked for, and never recorded what became of them.

        The read a restart makes. It deliberately includes rows whose arming was never
        confirmed: a guard LocalPlane asked for and did not hear back about is exactly the
        state that has to be resolved by *asking the holder*, and one that was skipped here
        would leave a Run that can never be settled.
        """
        if host_id is None:
            rows = self._db.query(
                "SELECT * FROM run_guards WHERE settled_at IS NULL ORDER BY arm_began_at"
            )
        else:
            rows = self._db.query(
                "SELECT * FROM run_guards WHERE settled_at IS NULL AND host_id = ? "
                "ORDER BY arm_began_at",
                (host_id,),
            )
        return [RunGuardRecord.from_row(r) for r in rows]


@dataclass(frozen=True)
class WriteLockRecord:
    """A durable claim that one Run is mutating one object's controlled field."""

    lock_key: str
    host_id: str
    object_id: str
    field: str
    run_id: str
    acquired_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "WriteLockRecord":
        return cls(**dict(row))


def write_lock_key(host_id: str, object_id: str, field: str) -> str:
    return f"{host_id}|{object_id}|{field}"


class WriteLockRepository:
    """Durable serialisation of mutating Runs, per object and controlled field.

    A process-local mutex would serialise one backend and silently permit two. This is a
    primary key: the second attempt fails whoever is asking, including a second process, a
    second machine sharing the file, or SQL typed into a shell.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def acquire(
        self, *, host_id: str, object_id: str, field: str, run_id: str, now: str
    ) -> bool:
        """Take the lock, or report that somebody else has it. Never waits.

        Inside a transaction opened by the caller with ``BEGIN IMMEDIATE``, so the read
        that finds it free and the insert that takes it cannot be interleaved with
        another writer's.
        """
        key = write_lock_key(host_id, object_id, field)
        try:
            self._db.connection.execute(
                "INSERT INTO object_write_locks "
                "(lock_key, host_id, object_id, field, run_id, acquired_at) "
                "VALUES (?,?,?,?,?,?)",
                (key, host_id, object_id, field, run_id, now),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def held(self, *, host_id: str, object_id: str, field: str) -> WriteLockRecord | None:
        row = self._db.query_one(
            "SELECT * FROM object_write_locks WHERE lock_key = ?",
            (write_lock_key(host_id, object_id, field),),
        )
        return None if row is None else WriteLockRecord.from_row(row)

    def for_run(self, run_id: str) -> WriteLockRecord | None:
        row = self._db.query_one(
            "SELECT * FROM object_write_locks WHERE run_id = ?", (run_id,)
        )
        return None if row is None else WriteLockRecord.from_row(row)

    def release(self, run_id: str) -> int:
        """Release the lock a Run holds, and report whether there was one. Scoped by run.

        Deliberately *not* called when a Run ends in ``recovery_required``. The object's
        state is unproven, and letting a second change start against it would be building
        on a foundation nobody has checked — a domain hold implemented as a row that outlives the
        run rather than a flag that outlives a page.

        What eventually releases such a hold is a recovery attempt that proved the end state
        or an operator who resolved it, and both go through here: the ``WHERE run_id`` is why
        "only the Run that owns the hold can release it" needs no extra machinery, and why no
        other object's lock can be touched by either act.
        """
        cursor = self._db.connection.execute(
            "DELETE FROM object_write_locks WHERE run_id = ?", (run_id,)
        )
        return cursor.rowcount
