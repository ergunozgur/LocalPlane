"""The unresolved recovery-hold list.

The endpoint answers an attention question, not a history question: which Changes still
hold an object, how many there are in total, and a bounded page of them. A released hold
keeps saying ``recovery_required`` forever, so the release marker is part of the rule.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from localplane.backend.db.repositories import ChangeRecord
from tests.test_changes_api import API, _Wired, planned as _planned, sysfs as _sysfs
from tests.test_changes_api import wired as _wired
from tests.test_recovery_api import into_recovery


def _raw(fixture):
    return getattr(fixture, "__wrapped__", fixture)


sysfs = pytest.fixture(_raw(_sysfs))
wired = pytest.fixture(_raw(_wired))
planned = pytest.fixture(_raw(_planned))


def _insert_row(database, table: str, values: dict) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    database.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(values.values())
    )


def _clone_change(database, source_change_id: str, *, suffix: str, created_at: str, result: str) -> dict:
    """Insert a valid parallel Change so the total can exceed one page.

    Every structural invariant is satisfied: the cloned preview/run/checkpoint/confirmation
    belong to each other, the confirmation is consumed, and the checkpoint belongs to the
    new run. Only the identity and the simulated time differ.
    """
    change = dict(
        database.query_one("SELECT * FROM changes WHERE change_id = ?", (source_change_id,))
    )
    preview = dict(
        database.query_one(
            "SELECT * FROM run_previews WHERE preview_id = ?", (change["preview_id"],)
        )
    )
    run = dict(database.query_one("SELECT * FROM runs WHERE run_id = ?", (change["run_id"],)))
    checkpoint = dict(
        database.query_one(
            "SELECT * FROM run_checkpoints WHERE checkpoint_id = ?",
            (change["checkpoint_id"],),
        )
    )
    confirmations = [
        dict(row)
        for row in database.query(
            "SELECT * FROM run_confirmations WHERE run_id = ?", (change["run_id"],)
        )
    ]
    assert any(row["purpose"] == "apply" for row in confirmations)

    preview_id = f"prv_{suffix}"
    run_id = f"run_{suffix}"
    checkpoint_id = f"chk_{suffix}"
    apply_attempt_id = f"apply_{suffix}"

    preview["preview_id"] = preview_id
    _insert_row(database, "run_previews", preview)

    run["run_id"] = run_id
    run["preview_id"] = preview_id
    run["created_at"] = created_at
    _insert_row(database, "runs", run)

    checkpoint["checkpoint_id"] = checkpoint_id
    checkpoint["run_id"] = run_id
    checkpoint["preview_id"] = preview_id
    _insert_row(database, "run_checkpoints", checkpoint)

    for index, confirmation in enumerate(confirmations):
        confirmation["confirmation_id"] = f"cnf_{suffix}_{index}"
        confirmation["run_id"] = run_id
        confirmation["preview_id"] = preview_id
        if confirmation["purpose"] == "apply":
            confirmation["consumed_by_attempt_id"] = apply_attempt_id
        _insert_row(database, "run_confirmations", confirmation)

    clone = dict(change)
    clone["change_id"] = f"chg_{suffix}"
    clone["run_id"] = run_id
    clone["preview_id"] = preview_id
    clone["checkpoint_id"] = checkpoint_id
    clone["created_at"] = created_at
    clone["apply_attempt_id"] = apply_attempt_id
    clone["rollback_attempt_id"] = f"rollback_{suffix}"
    clone["result"] = result
    clone["finished_at"] = created_at if result != "in_flight" else None
    if result == "recovery_required":
        clone["recovery_reason"] = change["recovery_reason"]
    else:
        observation = database.query_one("SELECT observation_id FROM observations LIMIT 1")
        clone.update(
            {
                "recovery_reason": None,
                "mutation_outcome": "written",
                "host_effect": "written",
                "verification_outcome": "verified",
                "verification_observation_id": observation["observation_id"],
                "verification_observed_value": clone["desired_value"],
                "verification_observed_state": None,
                "verification_reason": None,
                "rollback_required": 0,
                "rollback_attempt_id": None,
                "rollback_dispatch_began_at": None,
                "rollback_outcome": None,
                "rollback_reason": None,
                "rollback_detail": "{}",
                "rollback_verification_outcome": "not_attempted",
                "rollback_verification_observation_id": None,
                "rollback_verification_observed_value": None,
            }
        )
    _insert_row(database, "changes", clone)
    return clone


def _insert_resolution(database, change_id: str) -> None:
    """Record the durable release marker a real resolve would write, without a host read."""
    change = dict(database.query_one("SELECT * FROM changes WHERE change_id = ?", (change_id,)))
    _insert_row(
        database,
        "change_recovery_attempts",
        {
            "attempt_id": f"attempt_resolve_{change_id}",
            "change_id": change_id,
            "run_id": change["run_id"],
            "host_id": change["host_id"],
            "object_id": change["object_id"],
            "sequence": 1,
            "kind": "resolve",
            "started_at": change["created_at"],
            "protection_management_path": "unknown",
            "evidence_outcome": "not_attempted",
            "mutation_detail": "{}",
            "host_effect": "none",
            "verification_outcome": "not_attempted",
            "outcome": "resolved",
            "releases_hold": 1,
            "finished_at": change["created_at"],
            "operator_statement": "eth0",
        },
    )


def _now_iso(offset: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + offset).isoformat(timespec="microseconds")


def test_an_estate_with_no_changes_reports_zero_not_an_absent_answer(wired: _Wired):
    wired.observe()
    body = wired.client.get(f"{API}/changes/recovery-holds").json()
    assert body["total_count"] == 0
    assert body["count"] == 0
    assert body["limit"] == 12
    assert body["truncated"] is False
    assert body["changes"] == []


def test_a_recovery_required_change_appears_as_an_unresolved_hold(planned: _Wired):
    change = into_recovery(planned)
    body = planned.client.get(f"{API}/changes/recovery-holds").json()
    assert body["total_count"] == 1
    assert body["count"] == 1
    assert body["truncated"] is False
    assert [item["change_id"] for item in body["changes"]] == [change["change_id"]]
    assert body["changes"][0]["recovery_state"] == "unresolved"
    assert body["changes"][0]["result"] == "recovery_required"
    assert body["changes"][0]["recovery_reason"] == "rollback_verification_failed"


def test_total_is_the_whole_count_even_when_the_list_is_bounded(
    planned: _Wired, monkeypatch
):
    change = into_recovery(planned)
    database = planned.app.state.context.database
    old = _clone_change(
        database,
        change["change_id"],
        suffix="hold_old",
        created_at=_now_iso(timedelta(hours=-1)),
        result="recovery_required",
    )
    # Newer successful Changes are exactly the history a recent-page query would return.
    for index in range(5):
        _clone_change(
            database,
            change["change_id"],
            suffix=f"general_{index}",
            created_at=_now_iso(timedelta(minutes=index + 1)),
            result="succeeded",
        )

    original_from_row = ChangeRecord.from_row
    materialized: list[str] = []

    def counting_from_row(cls, row):
        materialized.append("record")
        return original_from_row(row)

    monkeypatch.setattr(ChangeRecord, "from_row", classmethod(counting_from_row))

    body = planned.client.get(f"{API}/changes/recovery-holds", params={"limit": 1}).json()
    assert body["total_count"] == 2, "older hold is still counted behind newer history"
    assert body["count"] == 1
    assert body["limit"] == 1
    assert body["truncated"] is True
    assert body["changes"][0]["change_id"] == change["change_id"]
    assert len(materialized) == 1, "only the bounded page is materialized"

    materialized.clear()
    body = planned.client.get(f"{API}/changes/recovery-holds", params={"limit": 100}).json()
    assert (body["total_count"], body["count"]) == (2, 2)
    assert len(materialized) == 2

    _insert_resolution(database, change["change_id"])
    after_release = planned.client.get(
        f"{API}/changes/recovery-holds", params={"limit": 1}
    ).json()
    assert after_release["total_count"] == 1, "a released hold is not attention"
    assert after_release["count"] == 1
    assert after_release["truncated"] is False
    assert after_release["changes"][0]["change_id"] == old["change_id"]
    assert after_release["changes"][0]["recovery_state"] == "unresolved"


def test_the_list_is_a_pure_read_and_never_contacts_the_agent(planned: _Wired, monkeypatch):
    into_recovery(planned)
    context = planned.app.state.context
    database = context.database
    tables = (
        "changes",
        "change_recovery_attempts",
        "observations",
        "observation_sweeps",
        "runs",
        "run_events",
    )
    before = {
        table: database.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
        for table in tables
    }

    def explode(*_args, **_kwargs):
        raise AssertionError("the recovery-hold read must not contact the agent")

    monkeypatch.setattr(context.client, "call", explode)
    monkeypatch.setattr(context.coordinator, "refresh_network", explode)

    body = planned.client.get(f"{API}/changes/recovery-holds").json()
    assert body["total_count"] == 1
    after = {
        table: database.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
        for table in tables
    }
    assert after == before


def test_the_limit_is_bounded_and_does_not_control_the_total(planned: _Wired):
    into_recovery(planned)
    for params in ({"limit": 0}, {"limit": 101}):
        assert planned.client.get(f"{API}/changes/recovery-holds", params=params).status_code == 422
