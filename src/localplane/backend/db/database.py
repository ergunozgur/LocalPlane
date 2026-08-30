"""SQLite, opened and migrated.

**Why SQLite.** LocalPlane runs on the host it manages, and the store has to be there
whenever the backend is — a control plane that cannot record what it saw because a
database server is down is worse than useless, because it will still answer questions.
SQLite is a file, it is in the standard library, it is transactional, and in WAL mode a
reader is never blocked by the writer. The data is relational — hosts have objects, objects
have observations — and wanting "the newest observation for each object" is a query, not a
projection somebody has to maintain. Nothing about this workload wants a server.

**Why no ORM and no Alembic.** The durable model is five tables. Hand-written SQL keeps the
persistence boundary small enough to read, and migrations are numbered files applied in
order and checksummed on every start. A checksum that no longer matches is a hard failure:
a file that changed after it was applied means the schema in front of us is not the schema
the code was written against, and continuing would be guessing.

**Why not a JSON document.** One source of truth per fact, and no continuous dual-write.
Observations are appended constantly and queried by object and by time; a rewritten JSON
document would lose both the append semantics and the query.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

LOG = logging.getLogger("localplane.backend.db")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class MigrationError(RuntimeError):
    """A migration could not be applied."""


class MigrationChecksumMismatch(MigrationError):
    """An already-applied migration file has changed on disk.

    Never repaired automatically. The database in front of the process is not the one the
    code was written against, and silently proceeding would put the two permanently out of
    step in a way nothing downstream could detect.
    """


#: A migration may declare, in a comment on a line of its own, that it needs foreign-key
#: enforcement suspended while it runs. It is spelled as a comment so it travels inside the
#: checksummed file — the declaration cannot be changed without the checksum noticing — and
#: it is greppable, so "which migrations turned this off" is one search rather than a
#: reading of every file.
FOREIGN_KEYS_OFF_DIRECTIVE = "-- localplane:foreign-keys=off"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return sha256(self.sql.encode("utf-8")).hexdigest()

    @property
    def suspends_foreign_keys(self) -> bool:
        """Whether this migration declared that it rebuilds a referenced table.

        SQLite cannot widen a CHECK in place, and it cannot drop a table that other tables
        still reference: ``DROP TABLE`` performs an implicit delete, which is an immediate
        foreign-key violation. ``PRAGMA defer_foreign_keys`` does not help — the drop's
        violation counter is still set at COMMIT — and ``PRAGMA legacy_alter_table`` has no
        effect inside a transaction, so a rename still rewrites every child's REFERENCES
        clause. Both were tried; both fail. The remaining route is the one SQLite documents
        for exactly this case, and it requires the pragma to be set outside the transaction.

        **Atomicity is not what is given up.** Every statement still runs inside one
        ``BEGIN IMMEDIATE``, together with the row that records the migration, and
        :func:`apply_migrations` runs ``PRAGMA foreign_key_check`` *inside* that transaction
        before committing — so such a migration is verified more strictly than an ordinary
        one, which is only ever checked row by row as it writes.
        """
        return any(
            line.strip() == FOREIGN_KEYS_OFF_DIRECTIVE for line in self.sql.splitlines()
        )


class Database:
    """A connection to the LocalPlane store, with migrations already applied."""

    def __init__(self, connection: sqlite3.Connection, path: Path) -> None:
        self._connection = connection
        self.path = path

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One unit of durable truth.

        A sweep either lands whole or not at all: a half-ingested batch would leave
        objects claiming a last-seen time for an observation that was never written.

        The connection runs in autocommit mode and transactions are opened here by name,
        so there is exactly one place that decides where a transaction begins and ends.
        ``BEGIN IMMEDIATE`` takes the write lock up front rather than discovering a
        conflict partway through.
        """
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self._connection
        except BaseException:
            self._connection.execute("ROLLBACK")
            LOG.exception("transaction rolled back")
            raise
        self._connection.execute("COMMIT")

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return self._connection.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self._connection.execute(sql, params).fetchone()

    def close(self) -> None:
        self._connection.close()


def open_database(path: str | Path, migrations_dir: Path | None = None) -> Database:
    """Open (creating if needed), configure and migrate the store."""
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA synchronous=FULL")

    database = Database(connection, path)
    apply_migrations(database, migrations_dir or MIGRATIONS_DIR)
    return database


def load_migrations(migrations_dir: Path) -> list[Migration]:
    """Read ``NNNN_name.sql`` files in version order."""
    migrations: list[Migration] = []
    for file in sorted(migrations_dir.glob("*.sql")):
        prefix, _, name = file.stem.partition("_")
        try:
            version = int(prefix)
        except ValueError as exc:
            raise MigrationError(f"migration filename is not NNNN_name.sql: {file.name}") from exc
        migrations.append(Migration(version, name or file.stem, file.read_text(encoding="utf-8")))

    versions = [m.version for m in migrations]
    if len(set(versions)) != len(versions):
        raise MigrationError(f"duplicate migration versions in {migrations_dir}: {versions}")
    return migrations


def apply_migrations(database: Database, migrations_dir: Path) -> list[Migration]:
    """Apply everything not yet applied. Returns what was applied this call."""
    connection = database.connection
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER NOT NULL PRIMARY KEY,
            name       TEXT    NOT NULL,
            checksum   TEXT    NOT NULL,
            applied_at TEXT    NOT NULL
        ) STRICT
        """
    )
    connection.commit()

    applied = {
        row["version"]: row
        for row in connection.execute("SELECT * FROM schema_migrations").fetchall()
    }
    migrations = load_migrations(migrations_dir)
    known = {m.version for m in migrations}

    orphaned = sorted(set(applied) - known)
    if orphaned:
        raise MigrationError(
            f"database has migrations that this build does not ship: {orphaned}. "
            "This store was written by a newer LocalPlane."
        )

    newly_applied: list[Migration] = []
    for migration in migrations:
        existing = applied.get(migration.version)
        if existing is not None:
            if existing["checksum"] != migration.checksum:
                raise MigrationChecksumMismatch(
                    f"migration {migration.version:04d}_{migration.name} changed after it was "
                    f"applied (recorded {existing['checksum'][:12]}, "
                    f"on disk {migration.checksum[:12]})"
                )
            continue

        LOG.info(
            "applying migration",
            # Not "name": logging reserves it on LogRecord and raises if it is overwritten.
            extra={"version": migration.version, "migration": migration.name},
        )
        # A declared rebuild of a referenced table needs the pragma set *outside* the
        # transaction, because SQLite ignores it inside one. Nothing else about the
        # migration changes: the DDL and the row that records it still land together, and
        # the check below re-establishes what the pragma stopped enforcing.
        suspend = migration.suspends_foreign_keys
        if suspend:
            LOG.warning(
                "migration declares a rebuild of a referenced table; foreign-key "
                "enforcement is suspended for it and verified before it commits",
                extra={"version": migration.version, "migration": migration.name},
            )
            connection.execute("PRAGMA foreign_keys=OFF")

        # Statement by statement inside one transaction, rather than executescript():
        # executescript commits any pending transaction before it runs, which would let
        # the schema land without the row that records it.
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in split_statements(migration.sql):
                connection.execute(statement)
            if suspend:
                violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise MigrationError(
                        f"migration {migration.version:04d}_{migration.name} left "
                        f"{len(violations)} foreign-key violation(s): "
                        f"{[tuple(v) for v in violations[:5]]}"
                    )
            connection.execute(
                "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    migration.version,
                    migration.name,
                    migration.checksum,
                    datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                ),
            )
        except (sqlite3.Error, MigrationError) as exc:
            connection.execute("ROLLBACK")
            if suspend:
                connection.execute("PRAGMA foreign_keys=ON")
            raise MigrationError(
                f"migration {migration.version:04d}_{migration.name} failed: {exc}"
            ) from exc
        connection.execute("COMMIT")
        if suspend:
            connection.execute("PRAGMA foreign_keys=ON")
        newly_applied.append(migration)

    return newly_applied


def split_statements(script: str) -> list[str]:
    """Split a migration into complete SQL statements.

    ``sqlite3.complete_statement`` is the parser SQLite itself uses to decide whether a
    line ends a statement, so semicolons inside string literals and comments do not split
    anything by accident.
    """
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                statements.append(buffer)
            buffer = ""
    if buffer.strip():
        raise MigrationError(f"migration ends with an incomplete statement: {buffer.strip()[:80]}")
    return statements


def to_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def from_json(raw: str | None, fallback: Any = None) -> Any:
    """Decode a JSON column.

    A malformed column is reported, never silently replaced with a default: it means
    something wrote state this build cannot read, and hiding that turns a visible fault
    into a wrong answer.
    """
    if raw is None:
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        LOG.error("stored JSON column could not be decoded", extra={"raw_prefix": raw[:120]})
        raise
