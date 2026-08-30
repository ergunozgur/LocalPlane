"""Migrations and the durable model."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from localplane.backend.db.database import (
    MIGRATIONS_DIR,
    MigrationChecksumMismatch,
    MigrationError,
    apply_migrations,
    load_migrations,
    open_database,
    split_statements,
)


def test_migrations_create_the_expected_schema(database):
    tables = {
        row["name"]
        for row in database.query("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert tables == {
        "schema_migrations",
        "hosts",
        "agent_instances",
        "agent_capabilities",
        "objects",
        "observation_sweeps",
        "observations",
        "intents",
        "intent_fields",
        "intent_revisions",
        "management_transitions",
        "findings",
        "provider_observations",
        "ownership_findings",
        "run_previews",
        "runs",
        "management_path_observations",
        "run_confirmations",
        "run_checkpoints",
        "changes",
        "run_events",
        "object_write_locks",
        "change_recovery_attempts",
        "run_guards",
    }
    sweep_columns = {
        row["name"]: row for row in database.query("PRAGMA table_info(observation_sweeps)")
    }
    assert sweep_columns["scope"]["dflt_value"] == "'inventory'"


def test_ownership_is_not_a_column_on_objects(database):
    """Ownership is derived, and there is nowhere for a stored verdict to go stale.

    The evidence is durable — `provider_observations` keeps what every provider said, per
    sweep — and the conclusion is recomputed from it. A column here would be a second copy
    of a fact that moves whenever either an observation or a provider reading lands.
    """
    columns = {row["name"] for row in database.query("PRAGMA table_info(objects)")}
    assert columns == {
        "object_id", "host_id", "kind", "identity_basis", "identity_value",
        "identity_confidence", "display_name", "management_state", "management_reason",
        "first_seen_at", "last_seen_at", "active_intent_id",
    }


def test_the_management_axis_still_has_exactly_three_values(database):
    """Ownership added no fourth management state, and the schema will not accept one."""
    sql = database.query_one(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='objects'"
    )["sql"]
    assert "management_state IN ('observe_only', 'observed', 'managed')" in sql


def test_migrations_are_recorded_with_a_checksum(database):
    rows = database.query("SELECT * FROM schema_migrations ORDER BY version")
    assert [row["version"] for row in rows] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
    assert all(len(row["checksum"]) == 64 for row in rows)
    assert all(row["applied_at"] for row in rows)


def test_applying_twice_is_a_no_op(database):
    assert apply_migrations(database, MIGRATIONS_DIR) == []


def test_reopening_an_existing_store_does_not_reapply(tmp_path: Path):
    path = tmp_path / "reopen.db"
    open_database(path).close()
    database = open_database(path)
    assert len(database.query("SELECT * FROM schema_migrations")) == 15
    database.close()


def test_a_changed_migration_is_a_hard_failure(tmp_path: Path):
    """The schema in front of us is not the one the code was written against."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_initial.sql").write_text("CREATE TABLE a (x TEXT) STRICT;")
    database = open_database(tmp_path / "drift.db", migrations)
    (migrations / "0001_initial.sql").write_text("CREATE TABLE a (x TEXT, y TEXT) STRICT;")
    with pytest.raises(MigrationChecksumMismatch):
        apply_migrations(database, migrations)
    database.close()


def test_a_store_from_a_newer_build_is_refused(tmp_path: Path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_a.sql").write_text("CREATE TABLE a (x TEXT) STRICT;")
    (migrations / "0002_b.sql").write_text("CREATE TABLE b (x TEXT) STRICT;")
    database = open_database(tmp_path / "newer.db", migrations)
    (migrations / "0002_b.sql").unlink()
    with pytest.raises(MigrationError, match="newer LocalPlane"):
        apply_migrations(database, migrations)
    database.close()


def test_a_failing_migration_leaves_nothing_behind(tmp_path: Path):
    """The schema and the row that records it land together or not at all."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_bad.sql").write_text(
        "CREATE TABLE good (x TEXT) STRICT;\nCREATE TABLE bad (x NOPE) STRICT;"
    )
    with pytest.raises(MigrationError):
        open_database(tmp_path / "bad.db", migrations)
    connection = sqlite3.connect(tmp_path / "bad.db")
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "good" not in tables
    assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0
    connection.close()


def test_badly_named_migrations_are_refused(tmp_path: Path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "initial.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="NNNN_name.sql"):
        load_migrations(migrations)


def test_duplicate_versions_are_refused(tmp_path: Path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_a.sql").write_text("SELECT 1;")
    (migrations / "0001_b.sql").write_text("SELECT 2;")
    with pytest.raises(MigrationError, match="duplicate"):
        load_migrations(migrations)


def test_statement_splitting_respects_literals_and_comments():
    statements = split_statements(
        "-- a comment with ; in it\n"
        "CREATE TABLE t (x TEXT) STRICT;\n"
        "INSERT INTO t VALUES ('a;b');\n"
    )
    assert len(statements) == 2
    assert "a;b" in statements[1]


def test_an_incomplete_final_statement_is_refused():
    with pytest.raises(MigrationError, match="incomplete"):
        split_statements("CREATE TABLE t (x TEXT)")


def test_wal_and_foreign_keys_are_on(database):
    assert database.query_one("PRAGMA journal_mode")[0] == "wal"
    assert database.query_one("PRAGMA foreign_keys")[0] == 1


def test_the_schema_refuses_an_invalid_management_state(database):
    database.connection.execute(
        "INSERT INTO hosts VALUES ('h','machine_id','high',NULL,NULL,NULL,NULL,NULL,NULL,"
        "NULL,NULL,NULL,'[]','t','t')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        database.connection.execute(
            "INSERT INTO objects (object_id, host_id, kind, identity_basis, identity_value, "
            "identity_confidence, display_name, management_state, management_reason, "
            "first_seen_at, last_seen_at) "
            "VALUES ('o','h','network.interface','kernel_name','eth0','low','eth0','adopted',"
            "'because','t','t')"
        )


def test_the_schema_refuses_an_invalid_health_state(database):
    with pytest.raises(sqlite3.IntegrityError):
        database.connection.execute(
            "INSERT INTO observations VALUES ('obs','s','h','o','c','p','1','m','complete',"
            "'t','t','green','fine','[]','{}','{}')"
        )


def test_a_transaction_rolls_back_on_failure(database):
    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction():
            database.connection.execute(
                "INSERT INTO hosts VALUES ('h1','machine_id','high',NULL,NULL,NULL,NULL,NULL,"
                "NULL,NULL,NULL,NULL,'[]','t','t')"
            )
            database.connection.execute(
                "INSERT INTO hosts VALUES ('h1','machine_id','high',NULL,NULL,NULL,NULL,NULL,"
                "NULL,NULL,NULL,NULL,'[]','t','t')"
            )
    assert database.query("SELECT * FROM hosts") == []
