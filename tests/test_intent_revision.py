"""Revising what LocalPlane intends for an object it already manages.

The same discipline as ``test_management.py``: everything runs through the real agent
service over a fixture ``/sys/class/net``, so drift is produced the way drift happens — a
value on the host changes, the provider reads it, and the comparison notices. Nothing
hand-writes an observation payload.

The machine this runs on is never touched. ``test_live_host.py`` exercises the same path
against the real host, read-only, and proves nothing moved.

Two claims are asserted over and over, because they are the whole of this behaviour: a revision
replaces LocalPlane's retained desired state with a new immutable version, and it does not
write to the host. The second is checked twice over — the outcome says so, and the fixture
tree is compared byte for byte before and after.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from localplane.backend.db.database import MIGRATIONS_DIR, open_database
from localplane.backend.domain.findings import (
    FINDING_TYPE_INTERFACE_DRIFT,
    FindingResolution,
    finding_key,
)
from localplane.backend.domain.intent import (
    INTENT_SCHEMA_VERSION,
    STORED_INTENT_ORIGIN,
    Capture,
    CapturedField,
    RevisionKind,
    RevisionPlan,
    RevisionRefused,
    UncapturableField,
    ValueType,
    plan_explicit_revision,
    plan_runtime_revision,
)
from localplane.backend.domain.states import ManagementState, ReconciliationState
from localplane.backend.management import ManagementRefused, ManagementService
from tests.test_management import Estate, estate  # noqa: F401  (fixture)


# ------------------------------------------------------------------------------ helpers


def adopted(estate: Estate, name: str = "eth0"):
    """Adopt one interface and return the outcome, so a test starts where revision does."""
    return estate.management.adopt(estate.object_named(name))


def revise(estate: Estate, name: str, fields: dict[str, Any], **kwargs):
    record = estate.object_named(name)
    return estate.management.revise_intent(
        record,
        fields=fields,
        expected_intent_id=kwargs.pop("expected_intent_id", record.active_intent_id or ""),
        **kwargs,
    )


def adopt_runtime(estate: Estate, name: str, **kwargs):
    record = estate.object_named(name)
    return estate.management.adopt_runtime_as_intent(
        record,
        expected_intent_id=kwargs.pop("expected_intent_id", record.active_intent_id or ""),
        **kwargs,
    )


def intended(intent) -> dict[str, Any]:
    return {f.field: f.value for f in intent.fields}


def findings_for(estate: Estate, name: str) -> list:
    return estate.management.findings.history_for_object(estate.object_named(name).object_id)


def current() -> tuple[CapturedField, ...]:
    """The controlled set an adopted interface starts with in this fixture."""
    return (
        CapturedField("admin_up", ValueType.BOOLEAN, True),
        CapturedField("mtu", ValueType.INTEGER, 1500),
    )


# ------------------------------------------------------------- planning, as pure functions


def test_an_empty_revision_is_refused_rather_than_treated_as_a_no_op():
    """"Change nothing" and "I have nothing to say" are not the same request."""
    refused = plan_explicit_revision(current(), {})
    assert isinstance(refused, RevisionRefused)
    assert refused.code == "empty_revision"
    assert refused.detail["controlled_fields"] == ["admin_up", "mtu"]


def test_a_field_localplane_does_not_intend_is_refused_not_ignored():
    refused = plan_explicit_revision(current(), {"speed_mbps": 1000})
    assert isinstance(refused, RevisionRefused)
    assert refused.code == "unsupported_field"
    assert refused.detail["field"] == "speed_mbps"
    assert refused.detail["supported_fields"] == ["admin_up", "mtu"]


def test_a_supported_field_this_intent_does_not_control_is_refused():
    """A revision may change what LocalPlane wants, not what it is answerable for."""
    narrow = (CapturedField("mtu", ValueType.INTEGER, 1500),)
    refused = plan_explicit_revision(narrow, {"admin_up": False})
    assert isinstance(refused, RevisionRefused)
    assert refused.code == "field_not_controlled"
    assert refused.detail["controlled_fields"] == ["mtu"]


def test_a_value_of_the_wrong_type_is_refused_rather_than_coerced():
    """True is an int in Python. An admin state is still not an MTU."""
    for supplied in ({"mtu": True}, {"admin_up": 1}, {"mtu": "1400"}, {"mtu": 0}):
        refused = plan_explicit_revision(current(), supplied)
        assert isinstance(refused, RevisionRefused), supplied
        assert refused.code == "invalid_field_value"


def test_a_revision_that_retains_exactly_what_is_retained_is_refused():
    refused = plan_explicit_revision(current(), {"mtu": 1500, "admin_up": True})
    assert isinstance(refused, RevisionRefused)
    assert refused.code == "revision_changes_nothing"
    assert refused.detail["intended"] == {"admin_up": True, "mtu": 1500}


def test_a_plan_carries_the_whole_controlled_set_not_a_delta():
    plan = plan_explicit_revision(current(), {"mtu": 1400})
    assert isinstance(plan, RevisionPlan)
    assert plan.kind is RevisionKind.EXPLICIT
    assert {(f.field, f.value) for f in plan.fields} == {("admin_up", True), ("mtu", 1400)}
    assert [(c.field, c.was, c.now) for c in plan.changed] == [("mtu", 1500, 1400)]
    assert plan.carried_forward == ("admin_up",)


def test_a_restated_value_is_named_by_the_operator_not_carried_forward():
    """Carried forward means "not mentioned", which is not the same as "unchanged"."""
    plan = plan_explicit_revision(current(), {"mtu": 1400, "admin_up": True})
    assert isinstance(plan, RevisionPlan)
    assert plan.carried_forward == ()
    assert [c.field for c in plan.changed] == ["mtu"]


def test_runtime_adoption_takes_the_observed_values_and_carries_nothing():
    capture = Capture(
        (
            CapturedField("admin_up", ValueType.BOOLEAN, True),
            CapturedField("mtu", ValueType.INTEGER, 1400),
        ),
        (),
    )
    plan = plan_runtime_revision(current(), capture)
    assert isinstance(plan, RevisionPlan)
    assert plan.kind is RevisionKind.RUNTIME_ADOPTION
    assert {(f.field, f.value) for f in plan.fields} == {("admin_up", True), ("mtu", 1400)}
    assert plan.carried_forward == ()


def test_runtime_adoption_refuses_when_a_controlled_value_could_not_be_read():
    capture = Capture(
        (CapturedField("mtu", ValueType.INTEGER, 1400),),
        (UncapturableField("admin_up", ValueType.BOOLEAN, "observed_value_unreadable"),),
    )
    refused = plan_runtime_revision(current(), capture)
    assert isinstance(refused, RevisionRefused)
    assert refused.code == "controlled_values_unverified"
    assert [f["field"] for f in refused.detail["fields"]] == ["admin_up"]


def test_runtime_adoption_ignores_an_observed_field_this_intent_does_not_control():
    """Adopting the runtime narrows to what is controlled; it never widens the set."""
    narrow = (CapturedField("mtu", ValueType.INTEGER, 1500),)
    capture = Capture(
        (
            CapturedField("admin_up", ValueType.BOOLEAN, False),
            CapturedField("mtu", ValueType.INTEGER, 1400),
        ),
        (),
    )
    plan = plan_runtime_revision(narrow, capture)
    assert isinstance(plan, RevisionPlan)
    assert [f.field for f in plan.fields] == ["mtu"]


def test_runtime_adoption_of_a_runtime_that_already_agrees_is_refused():
    capture = Capture(current(), ())
    refused = plan_runtime_revision(current(), capture)
    assert isinstance(refused, RevisionRefused)
    assert refused.code == "revision_changes_nothing"


# ------------------------------------------------------------------ what may be revised


def test_revision_requires_a_managed_object(estate: Estate):
    record = estate.object_named("eth0")
    assert record.management_state == ManagementState.OBSERVED
    with pytest.raises(ManagementRefused) as raised:
        estate.management.revise_intent(
            record, fields={"mtu": 1400}, expected_intent_id="int_whatever"
        )
    assert raised.value.code == "not_managed"
    assert estate.database.query("SELECT * FROM intents") == []
    assert estate.database.query("SELECT * FROM intent_revisions") == []


def test_an_observe_only_object_cannot_revise_intent(estate: Estate):
    """Loopback holds no intent and never will. Saying "not managed" would understate it."""
    loopback = estate.object_named("lo")
    assert loopback.management_state == ManagementState.OBSERVE_ONLY
    with pytest.raises(ManagementRefused) as raised:
        estate.management.revise_intent(
            loopback, fields={"mtu": 1400}, expected_intent_id="int_whatever"
        )
    assert raised.value.code == "object_observe_only"
    assert raised.value.detail["reason"] == "loopback"


def test_an_observe_only_object_cannot_adopt_its_runtime_as_intent(estate: Estate):
    with pytest.raises(ManagementRefused) as raised:
        estate.management.adopt_runtime_as_intent(
            estate.object_named("veth0"), expected_intent_id="int_whatever"
        )
    assert raised.value.code == "object_observe_only"


def test_a_released_object_cannot_be_revised(estate: Estate):
    outcome = adopted(estate)
    estate.management.release(estate.object_named("eth0"))
    with pytest.raises(ManagementRefused) as raised:
        estate.management.revise_intent(
            estate.object_named("eth0"),
            fields={"mtu": 1400},
            expected_intent_id=outcome.intent.intent_id,
        )
    assert raised.value.code == "not_managed"
    assert len(estate.management.intents.history(outcome.object_id)) == 1


def test_revision_is_refused_when_the_observation_is_too_old(estate: Estate, database):
    """Even a revision that reads no runtime value is a decision made against a reading."""
    adopted(estate)
    record = estate.object_named("eth0")
    impatient = ManagementService(database, freshness_ttl_s=0.0)
    with pytest.raises(ManagementRefused) as raised:
        impatient.revise_intent(
            record, fields={"mtu": 1400}, expected_intent_id=record.active_intent_id or ""
        )
    assert raised.value.code == "observation_stale"
    assert raised.value.detail["freshness"] == "stale"


def test_runtime_adoption_is_refused_when_the_observation_is_too_old(estate: Estate, database):
    adopted(estate)
    record = estate.object_named("eth0")
    impatient = ManagementService(database, freshness_ttl_s=0.0)
    with pytest.raises(ManagementRefused) as raised:
        impatient.adopt_runtime_as_intent(
            record, expected_intent_id=record.active_intent_id or ""
        )
    assert raised.value.code == "observation_stale"
    assert intended(estate.management.intents.get(record.active_intent_id or "")) == {
        "admin_up": True,
        "mtu": 1500,
    }


def test_runtime_adoption_is_refused_when_a_controlled_value_cannot_be_read(estate: Estate):
    """"What is there is right" cannot be said about a value nobody could see."""
    outcome = adopted(estate)
    estate.write("eth0", "mtu", "1400")
    estate.observe()
    estate.make_unreadable("eth0", "flags")
    estate.observe()

    with pytest.raises(ManagementRefused) as raised:
        adopt_runtime(estate, "eth0")

    assert raised.value.code == "controlled_values_unverified"
    assert [f["field"] for f in raised.value.detail["fields"]] == ["admin_up"]
    assert estate.object_named("eth0").active_intent_id == outcome.intent.intent_id
    assert len(estate.management.intents.history(outcome.object_id)) == 1


# ------------------------------------------------------------------- the version chain


def test_a_revision_writes_a_new_immutable_version(estate: Estate):
    first = adopted(estate)
    outcome = revise(estate, "eth0", {"mtu": 1400})

    assert outcome.intent.version == 2
    assert outcome.intent.supersedes == first.intent.intent_id
    assert outcome.intent.intent_id != first.intent.intent_id
    assert intended(outcome.intent) == {"admin_up": True, "mtu": 1400}
    assert outcome.intent.schema_version == INTENT_SCHEMA_VERSION


def test_the_superseded_version_is_left_exactly_as_it_was(estate: Estate):
    first = adopted(estate)
    before = estate.management.intents.get(first.intent.intent_id)
    revise(estate, "eth0", {"mtu": 1400})
    after = estate.management.intents.get(first.intent.intent_id)

    assert after == before
    assert intended(after) == {"admin_up": True, "mtu": 1500}
    assert after.revision is None


def test_every_version_stays_in_the_history_in_order(estate: Estate):
    adopted(estate)
    revise(estate, "eth0", {"mtu": 1400})
    revise(estate, "eth0", {"mtu": 1300})
    adopt_runtime_after = estate.object_named("eth0")

    history = estate.management.intents.history(adopt_runtime_after.object_id)
    assert [i.version for i in history] == [3, 2, 1]
    assert [intended(i)["mtu"] for i in history] == [1300, 1400, 1500]
    # Each version names the one it replaced, so the chain reads in either direction.
    assert history[0].supersedes == history[1].intent_id
    assert history[1].supersedes == history[2].intent_id
    assert history[2].supersedes is None


def test_the_active_pointer_is_what_says_which_version_is_in_force(estate: Estate):
    first = adopted(estate)
    outcome = revise(estate, "eth0", {"mtu": 1400})
    record = estate.object_named("eth0")

    assert record.active_intent_id == outcome.intent.intent_id
    active = estate.management.intents.active_for([record.object_id])[record.object_id]
    assert active.intent_id == outcome.intent.intent_id
    assert active.version == 2
    # The earlier version still exists and is simply not pointed at.
    assert estate.management.intents.get(first.intent.intent_id) is not None


def test_a_revision_keeps_the_object_managed_for_the_same_reason(estate: Estate):
    adopted(estate)
    before = estate.object_named("eth0")
    outcome = revise(estate, "eth0", {"mtu": 1400})
    after = estate.object_named("eth0")

    assert outcome.management_state == ManagementState.MANAGED
    assert after.management_state == ManagementState.MANAGED
    assert after.management_reason == before.management_reason == "adopted"


def test_a_revision_is_not_recorded_as_a_management_transition(estate: Estate):
    """managed → managed is not a movement, and the table that holds movements says so."""
    adopted(estate)
    revise(estate, "eth0", {"mtu": 1400})
    transitions = estate.database.query("SELECT * FROM management_transitions")
    assert [t["transition"] for t in transitions] == ["adopt"]


def test_the_revision_event_says_which_semantics_were_chosen(estate: Estate):
    adopted(estate)
    explicit = revise(estate, "eth0", {"mtu": 1400})
    estate.write("eth0", "mtu", "9000")
    estate.observe()
    runtime = adopt_runtime(estate, "eth0")

    rows = estate.database.query("SELECT * FROM intent_revisions ORDER BY occurred_at")
    assert [r["kind"] for r in rows] == ["revise", "adopt_runtime"]
    assert [r["intent_id"] for r in rows] == [
        explicit.intent.intent_id,
        runtime.intent.intent_id,
    ]
    assert {r["host_effect"] for r in rows} == {"none"}


def test_the_origin_reported_for_a_version_is_the_event_that_produced_it(estate: Estate):
    """The frozen column says 'adopt' on every row; nothing above the store reads it."""
    adopted(estate)
    revise(estate, "eth0", {"mtu": 1400})
    estate.write("eth0", "mtu", "9000")
    estate.observe()
    adopt_runtime(estate, "eth0")

    object_id = estate.object_named("eth0").object_id
    assert [i.origin for i in estate.management.intents.history(object_id)] == [
        "adopt_runtime",
        "revise",
        "adopt",
    ]
    stored = estate.database.query("SELECT origin FROM intents")
    assert {row["origin"] for row in stored} == {STORED_INTENT_ORIGIN}


def test_every_version_is_explained_by_exactly_one_event(estate: Estate):
    """An adopt in management_transitions, or a revision. There is no third way."""
    adopted(estate)
    revise(estate, "eth0", {"mtu": 1400})
    estate.management.release(estate.object_named("eth0"))
    estate.observe()
    adopted(estate)

    versions = {row["intent_id"] for row in estate.database.query("SELECT intent_id FROM intents")}
    adopts = {
        row["intent_id"]
        for row in estate.database.query(
            "SELECT intent_id FROM management_transitions WHERE transition = 'adopt'"
        )
    }
    revisions = {
        row["intent_id"] for row in estate.database.query("SELECT intent_id FROM intent_revisions")
    }
    assert adopts | revisions == versions
    assert adopts & revisions == set()


# ------------------------------------------------------------------ explicit revision


def test_an_explicit_revision_records_the_value_the_operator_supplied(estate: Estate):
    adopted(estate)
    outcome = revise(estate, "eth0", {"mtu": 1400})

    assert outcome.kind is RevisionKind.EXPLICIT
    assert intended(outcome.intent)["mtu"] == 1400
    assert [(c.field, c.was, c.now) for c in outcome.plan.changed] == [("mtu", 1500, 1400)]


def test_an_unnamed_controlled_field_carries_forward_rather_than_being_dropped(estate: Estate):
    adopted(estate, "eth1")
    outcome = revise(estate, "eth1", {"mtu": 1400})

    assert {f.field for f in outcome.intent.fields} == {"admin_up", "mtu"}
    assert intended(outcome.intent)["admin_up"] is True
    assert outcome.plan.carried_forward == ("admin_up",)


def test_a_revision_cannot_silently_drop_a_controlled_field(estate: Estate):
    """Whatever is named, the version written controls the same set as the one before it."""
    first = adopted(estate)
    outcome = revise(estate, "eth0", {"admin_up": False})
    assert {f.field for f in outcome.intent.fields} == {f.field for f in first.intent.fields}


def test_an_explicit_revision_may_change_both_controlled_fields(estate: Estate):
    adopted(estate)
    outcome = revise(estate, "eth0", {"mtu": 1400, "admin_up": False})
    assert intended(outcome.intent) == {"admin_up": False, "mtu": 1400}
    assert {c.field for c in outcome.plan.changed} == {"admin_up", "mtu"}
    assert outcome.plan.carried_forward == ()


def test_an_unsupported_field_refuses_the_whole_revision(estate: Estate):
    first = adopted(estate)
    with pytest.raises(ManagementRefused) as raised:
        revise(estate, "eth0", {"mtu": 1400, "speed_mbps": 1000})

    assert raised.value.code == "unsupported_field"
    assert raised.value.detail["field"] == "speed_mbps"
    assert estate.object_named("eth0").active_intent_id == first.intent.intent_id
    assert len(estate.management.intents.history(first.object_id)) == 1


def test_a_wrongly_typed_value_refuses_the_whole_revision(estate: Estate):
    first = adopted(estate)
    with pytest.raises(ManagementRefused) as raised:
        revise(estate, "eth0", {"mtu": True})

    assert raised.value.code == "invalid_field_value"
    assert raised.value.detail["expected_type"] == "integer"
    assert intended(estate.management.intents.get(first.intent.intent_id)) == {
        "admin_up": True,
        "mtu": 1500,
    }


def test_an_empty_revision_is_refused_by_the_service(estate: Estate):
    adopted(estate)
    with pytest.raises(ManagementRefused) as raised:
        revise(estate, "eth0", {})
    assert raised.value.code == "empty_revision"


def test_revising_to_what_is_already_intended_is_refused(estate: Estate):
    adopted(estate)
    with pytest.raises(ManagementRefused) as raised:
        revise(estate, "eth0", {"mtu": 1500})
    assert raised.value.code == "revision_changes_nothing"
    assert estate.database.query("SELECT * FROM intent_revisions") == []


# ------------------------------------------------------------- adopting the runtime


def test_adopting_the_runtime_captures_the_current_verified_values(estate: Estate):
    adopted(estate)
    estate.write("eth0", "mtu", "1400")
    estate.observe()

    outcome = adopt_runtime(estate, "eth0")

    assert outcome.kind is RevisionKind.RUNTIME_ADOPTION
    assert intended(outcome.intent) == {"admin_up": True, "mtu": 1400}
    assert outcome.intent.observation_id == estate.object_named("eth0").observation.observation_id
    assert outcome.plan.carried_forward == ()


def test_adopting_the_runtime_names_the_observation_the_values_came_from(estate: Estate):
    adopted(estate)
    estate.write("eth0", "mtu", "9000")
    estate.observe()
    record = estate.object_named("eth0")

    outcome = adopt_runtime(estate, "eth0")

    assert outcome.intent.observation_id == record.observation.observation_id
    assert outcome.intent.sweep_id == record.observation.sweep_id
    assert outcome.intent.observed_at == record.observation.observed_at
    assert outcome.intent.capability == "network.observe"
    assert outcome.intent.provider == "linux.network"


def test_adopting_a_runtime_that_already_agrees_is_refused(estate: Estate):
    adopted(estate)
    with pytest.raises(ManagementRefused) as raised:
        adopt_runtime(estate, "eth0")
    assert raised.value.code == "revision_changes_nothing"


# ------------------------------------------------------------------- reconciliation


def test_a_revision_turns_drift_into_agreement_without_touching_the_host(estate: Estate):
    adopted(estate)
    estate.write("eth0", "mtu", "1400")
    estate.observe()
    before_host = estate.link_snapshot()
    assert estate.management.reconciliation_for(
        estate.object_named("eth0"),
        estate.management.intents.active_for([estate.object_named("eth0").object_id])[
            estate.object_named("eth0").object_id
        ],
    ).state is ReconciliationState.DRIFTED

    outcome = revise(estate, "eth0", {"mtu": 1400})

    assert outcome.reconciliation.state is ReconciliationState.IN_SYNC
    assert outcome.host_effect == "none"
    assert estate.link_snapshot() == before_host


def test_adopting_the_runtime_turns_drift_into_agreement_without_touching_the_host(
    estate: Estate,
):
    adopted(estate)
    estate.write("eth0", "mtu", "1400")
    estate.observe()
    before_host = estate.link_snapshot()

    outcome = adopt_runtime(estate, "eth0")

    assert outcome.reconciliation.state is ReconciliationState.IN_SYNC
    assert outcome.host_effect == "none"
    assert estate.link_snapshot() == before_host


def test_a_revision_that_still_disagrees_stays_drifted(estate: Estate):
    """Revising is not agreeing. A desired value the host does not have is still drift."""
    adopted(estate)
    estate.write("eth0", "mtu", "1400")
    estate.observe()

    outcome = revise(estate, "eth0", {"mtu": 1300})

    assert outcome.reconciliation.state is ReconciliationState.DRIFTED
    comparisons = {f.field: f for f in outcome.reconciliation.fields}
    assert comparisons["mtu"].intended == 1300
    assert comparisons["mtu"].observed == 1400


def test_an_unreadable_controlled_value_leaves_reconciliation_unknown(estate: Estate):
    """A revision cannot manufacture agreement with a value nobody can read.

    The revised field is made to agree on purpose, so that the only thing left deciding
    the answer is the field nobody could read. Unknown is the honest verdict, and it is
    not in_sync.
    """
    adopted(estate)
    estate.write("eth0", "mtu", "1400")
    estate.observe()
    estate.make_unreadable("eth0", "flags")
    estate.observe()

    outcome = revise(estate, "eth0", {"mtu": 1400})

    assert outcome.reconciliation.state is ReconciliationState.UNKNOWN
    comparisons = {f.field: f for f in outcome.reconciliation.fields}
    assert comparisons["admin_up"].observed is None
    assert comparisons["admin_up"].reason == "observed_value_unreadable"


def test_a_revision_is_refused_when_the_observation_comes_from_another_source(
    estate: Estate, database
):
    """The same field name from a different provider is not the same fact."""
    outcome = adopted(estate)
    record = estate.object_named("eth0")
    database.connection.execute(
        "UPDATE observations SET provider = 'other.network' WHERE observation_id = ?",
        (record.observation.observation_id,),
    )
    with pytest.raises(ManagementRefused) as raised:
        revise(estate, "eth0", {"mtu": 1400})

    assert raised.value.code == "observation_source_incompatible"
    assert raised.value.detail["intent_provider"] == "linux.network"
    assert raised.value.detail["observation_provider"] == "other.network"
    assert estate.object_named("eth0").active_intent_id == outcome.intent.intent_id


# ---------------------------------------------------------------- finding lifecycle


def test_a_revision_resolves_drift_as_revised_never_as_remediated(estate: Estate):
    adopted(estate)
    estate.write("eth0", "mtu", "1400")
    estate.observe()
    opened = findings_for(estate, "eth0")
    assert [(f.subject, f.status) for f in opened] == [("mtu", "open")]

    outcome = revise(estate, "eth0", {"mtu": 1400})

    assert [f.finding_id for f in outcome.findings_resolved] == [opened[0].finding_id]
    resolved = estate.management.findings.get(opened[0].finding_id)
    assert resolved.status == "resolved"
    assert resolved.resolution == FindingResolution.INTENT_REVISED
    # Nothing was applied, so no observation may be named as having proved it ended.
    assert resolved.resolved_by_observation_id is None


def test_the_resolved_finding_keeps_the_evidence_of_the_episode_it_was(estate: Estate):
    """The claim was made against the version that was in force. It still says so."""
    first = adopted(estate)
    estate.write("eth0", "mtu", "1400")
    estate.observe()
    opened = findings_for(estate, "eth0")[0]

    revise(estate, "eth0", {"mtu": 1400})

    resolved = estate.management.findings.get(opened.finding_id)
    assert resolved.intent_id == first.intent.intent_id
    assert (resolved.intended_value, resolved.observed_value) == (1500, 1400)
    assert resolved.first_seen_at == opened.first_seen_at


def test_adopting_the_runtime_resolves_drift_as_revised(estate: Estate):
    adopted(estate)
    estate.write("eth0", "mtu", "1400")
    estate.observe()

    outcome = adopt_runtime(estate, "eth0")

    assert len(outcome.findings_resolved) == 1
    assert outcome.findings_resolved[0].resolution == FindingResolution.INTENT_REVISED
    assert outcome.findings_resolved[0].resolved_by_observation_id is None


def test_a_revision_that_creates_a_disagreement_opens_a_finding(estate: Estate):
    adopted(estate)
    outcome = revise(estate, "eth0", {"mtu": 9000})

    assert [f.subject for f in outcome.findings_opened] == ["mtu"]
    finding = outcome.findings_opened[0]
    assert finding.status == "open"
    assert (finding.intended_value, finding.observed_value) == (9000, 1500)
    assert finding.intent_id == outcome.intent.intent_id


def test_a_field_that_came_back_on_its_own_is_not_credited_to_the_revision(estate: Estate):
    """Two things can move at once, and only one of them is the operator's doing."""
    adopted(estate, "eth1")
    estate.write("eth1", "mtu", "1400")
    estate.write("eth1", "flags", "0x1002")
    estate.observe()
    by_subject = {f.subject: f for f in findings_for(estate, "eth1")}
    assert set(by_subject) == {"mtu", "admin_up"}

    # The host is put back by hand, and in the same breath the operator revises the MTU.
    estate.write("eth1", "flags", "0x1003")
    estate.observe()
    outcome = revise(estate, "eth1", {"mtu": 1400})

    mtu = estate.management.findings.get(by_subject["mtu"].finding_id)
    admin = estate.management.findings.get(by_subject["admin_up"].finding_id)
    assert mtu.resolution == FindingResolution.INTENT_REVISED
    assert mtu.resolved_by_observation_id is None
    assert admin.resolution == FindingResolution.OBSERVED_MATCHES_INTENT
    assert admin.resolved_by_observation_id is not None
    assert outcome.reconciliation.state is ReconciliationState.IN_SYNC


def test_an_unreadable_value_does_not_let_a_revision_resolve_a_drift(estate: Estate):
    adopted(estate, "eth1")
    estate.write("eth1", "flags", "0x1002")
    estate.observe()
    admin_finding = findings_for(estate, "eth1")[0]
    assert admin_finding.subject == "admin_up"

    estate.write("eth1", "mtu", "1400")
    estate.make_unreadable("eth1", "flags")
    estate.observe()
    outcome = revise(estate, "eth1", {"mtu": 1400})

    still_open = estate.management.findings.get(admin_finding.finding_id)
    assert still_open.status == "open"
    assert still_open.resolution is None
    assert outcome.reconciliation.state is ReconciliationState.UNKNOWN


def test_a_later_recurrence_opens_a_new_episode_against_the_new_version(estate: Estate):
    adopted(estate)
    estate.write("eth0", "mtu", "1400")
    estate.observe()
    first_episode = findings_for(estate, "eth0")[0]

    outcome = revise(estate, "eth0", {"mtu": 1400})
    assert outcome.reconciliation.state is ReconciliationState.IN_SYNC

    estate.write("eth0", "mtu", "1300")
    estate.observe()

    episodes = findings_for(estate, "eth0")
    assert len(episodes) == 2
    key = finding_key(
        estate.object_named("eth0").host_id,
        estate.object_named("eth0").object_id,
        FINDING_TYPE_INTERFACE_DRIFT,
        "mtu",
    )
    assert {e.finding_key for e in episodes} == {key}
    latest = estate.management.findings.open_for_key(key)
    assert latest.finding_id != first_episode.finding_id
    assert (latest.intended_value, latest.observed_value) == (1400, 1300)
    assert latest.intent_id == outcome.intent.intent_id
    # The earlier episode is kept, with the ending it actually had.
    assert estate.management.findings.get(first_episode.finding_id).resolution == (
        FindingResolution.INTENT_REVISED
    )


# ------------------------------------------------------------------------ concurrency


def test_a_stale_caller_cannot_overwrite_a_newer_decision(estate: Estate):
    """Two operators read the same intent. The second is told, not silently obeyed."""
    first = adopted(estate)
    winner = revise(estate, "eth0", {"mtu": 1400})

    with pytest.raises(ManagementRefused) as raised:
        estate.management.revise_intent(
            estate.object_named("eth0"),
            fields={"mtu": 9000},
            expected_intent_id=first.intent.intent_id,
        )

    assert raised.value.code == "intent_revision_conflict"
    assert raised.value.detail["expected_intent_id"] == first.intent.intent_id
    assert raised.value.detail["active_intent_id"] == winner.intent.intent_id
    assert raised.value.detail["active_version"] == 2
    assert intended(
        estate.management.intents.get(estate.object_named("eth0").active_intent_id)
    ) == {"admin_up": True, "mtu": 1400}
    assert len(estate.management.intents.history(first.object_id)) == 2


def test_a_stale_version_number_is_a_conflict_even_with_the_right_id(estate: Estate):
    adopted(estate)
    record = estate.object_named("eth0")
    with pytest.raises(ManagementRefused) as raised:
        estate.management.revise_intent(
            record,
            fields={"mtu": 1400},
            expected_intent_id=record.active_intent_id or "",
            expected_version=7,
        )
    assert raised.value.code == "intent_revision_conflict"
    assert raised.value.detail["expected_version"] == 7
    assert raised.value.detail["active_version"] == 1


def test_a_stale_caller_cannot_adopt_the_runtime_over_a_newer_decision(estate: Estate):
    first = adopted(estate)
    estate.write("eth0", "mtu", "1400")
    estate.observe()
    revise(estate, "eth0", {"mtu": 1300})

    with pytest.raises(ManagementRefused) as raised:
        estate.management.adopt_runtime_as_intent(
            estate.object_named("eth0"), expected_intent_id=first.intent.intent_id
        )
    assert raised.value.code == "intent_revision_conflict"
    assert len(estate.management.intents.history(first.object_id)) == 2


def test_a_revision_racing_a_release_loses_truthfully(estate: Estate):
    outcome = adopted(estate)
    stale = estate.object_named("eth0")
    estate.management.release(estate.object_named("eth0"))

    with pytest.raises(ManagementRefused) as raised:
        estate.management.revise_intent(
            stale, fields={"mtu": 1400}, expected_intent_id=outcome.intent.intent_id
        )
    assert raised.value.code == "not_managed"
    assert estate.object_named("eth0").management_state == ManagementState.OBSERVED


def test_the_version_written_is_the_one_after_the_newest_not_the_caller_s(estate: Estate):
    """Version numbers come from the store under the write lock, never from a caller."""
    adopted(estate)
    revise(estate, "eth0", {"mtu": 1400})
    third = revise(estate, "eth0", {"mtu": 1300})
    assert third.intent.version == 3
    rows = estate.database.query("SELECT version FROM intents ORDER BY version")
    assert [r["version"] for r in rows] == [1, 2, 3]


# ------------------------------------------------------------- transactional invariants


def test_a_revision_that_fails_part_way_leaves_the_old_version_in_force(
    estate: Estate, monkeypatch
):
    first = adopted(estate)
    estate.write("eth0", "mtu", "1400")
    estate.observe()
    open_before = findings_for(estate, "eth0")[0]

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("interrupted after the version was written")

    monkeypatch.setattr(estate.management.intents, "record_revision", explode)
    with pytest.raises(RuntimeError):
        revise(estate, "eth0", {"mtu": 1400})

    after = estate.object_named("eth0")
    assert after.management_state == ManagementState.MANAGED
    assert after.active_intent_id == first.intent.intent_id
    assert len(estate.management.intents.history(after.object_id)) == 1
    assert estate.database.query("SELECT * FROM intent_revisions") == []
    assert estate.management.findings.get(open_before.finding_id).status == "open"
    # And the reconciliation that was true before is still the one that is true.
    active = estate.management.intents.active_for([after.object_id])[after.object_id]
    assert (
        estate.management.reconciliation_for(after, active).state is ReconciliationState.DRIFTED
    )


def test_a_revision_whose_finding_update_fails_writes_no_version_at_all(
    estate: Estate, monkeypatch
):
    """The version, the pointer, the event and the findings land together or not at all."""
    first = adopted(estate)
    estate.write("eth0", "mtu", "1400")
    estate.observe()

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("interrupted while settling the findings")

    monkeypatch.setattr(estate.management.findings, "resolve", explode)
    with pytest.raises(RuntimeError):
        revise(estate, "eth0", {"mtu": 1400})

    assert estate.object_named("eth0").active_intent_id == first.intent.intent_id
    assert estate.database.query("SELECT * FROM intent_revisions") == []
    assert len(estate.database.query("SELECT * FROM intents")) == 1
    assert len(estate.database.query("SELECT * FROM intent_fields")) == 2


def test_the_store_refuses_a_second_revision_claiming_one_version(estate: Estate, database):
    adopted(estate)
    outcome = revise(estate, "eth0", {"mtu": 1400})
    with pytest.raises(sqlite3.IntegrityError):
        database.connection.execute(
            "INSERT INTO intent_revisions VALUES ('rev_x', ?, ?, 'adopt_runtime', ?, 'none', 't')",
            (outcome.object_id, outcome.host_id, outcome.intent.intent_id),
        )


def test_the_store_refuses_a_revision_that_claims_a_host_write(estate: Estate, database):
    adopted(estate)
    outcome = revise(estate, "eth0", {"mtu": 1400})
    with pytest.raises(sqlite3.IntegrityError):
        database.connection.execute(
            "UPDATE intent_revisions SET host_effect = 'wrote' WHERE intent_id = ?",
            (outcome.intent.intent_id,),
        )


def test_the_store_refuses_a_revision_kind_it_does_not_know(estate: Estate, database):
    adopted(estate)
    outcome = revise(estate, "eth0", {"mtu": 1400})
    with pytest.raises(sqlite3.IntegrityError):
        database.connection.execute(
            "UPDATE intent_revisions SET kind = 'applied' WHERE intent_id = ?",
            (outcome.intent.intent_id,),
        )


def test_the_store_refuses_a_drift_resolution_it_does_not_know(estate: Estate, database):
    adopted(estate)
    estate.write("eth0", "mtu", "1400")
    estate.observe()
    finding = findings_for(estate, "eth0")[0]
    with pytest.raises(sqlite3.IntegrityError):
        database.connection.execute(
            "UPDATE findings SET status = 'resolved', resolved_at = 't', "
            "resolution = 'remediated' WHERE finding_id = ?",
            (finding.finding_id,),
        )


def test_the_store_refuses_a_revised_resolution_that_names_an_observation(
    estate: Estate, database
):
    """`intent_revised` structurally cannot claim the runtime was put right."""
    adopted(estate)
    estate.write("eth0", "mtu", "1400")
    estate.observe()
    finding = findings_for(estate, "eth0")[0]
    revise(estate, "eth0", {"mtu": 1400})
    with pytest.raises(sqlite3.IntegrityError):
        database.connection.execute(
            "UPDATE findings SET resolved_by_observation_id = ? WHERE finding_id = ?",
            (finding.observation_id, finding.finding_id),
        )


def test_a_revision_cannot_activate_an_intent_belonging_to_another_object(
    estate: Estate, database
):
    adopted(estate)
    outcome = revise(estate, "eth0", {"mtu": 1400})
    other = estate.management.adopt(estate.object_named("eth1"))
    with pytest.raises(sqlite3.IntegrityError):
        database.connection.execute(
            "UPDATE objects SET active_intent_id = ? WHERE object_id = ?",
            (outcome.intent.intent_id, other.object_id),
        )


def test_a_superseded_version_cannot_be_deleted_out_of_the_chain(estate: Estate, database):
    first = adopted(estate)
    revise(estate, "eth0", {"mtu": 1400})
    with pytest.raises(sqlite3.IntegrityError):
        database.connection.execute(
            "DELETE FROM intents WHERE intent_id = ?", (first.intent.intent_id,)
        )


# ---------------------------------------------------------------------- host safety


def test_no_revision_changes_a_single_value_on_the_fixture_host(estate: Estate):
    adopted(estate)
    adopted(estate, "eth1")
    before = estate.link_snapshot()

    revise(estate, "eth0", {"mtu": 9000})
    revise(estate, "eth0", {"admin_up": False})
    estate.write("eth1", "mtu", "1400")
    estate.observe()
    adopt_runtime(estate, "eth1")

    # The only value that moved on the fixture host is the one the test itself wrote.
    expected = {name: dict(values) for name, values in before.items()}
    expected["eth1"]["mtu"] = "1400\n"
    assert estate.link_snapshot() == expected


def test_the_observation_a_revision_read_is_not_rewritten(estate: Estate):
    adopted(estate)
    estate.write("eth0", "mtu", "1400")
    estate.observe()
    before = estate.database.query("SELECT * FROM observations ORDER BY observation_id")

    revise(estate, "eth0", {"mtu": 1400})

    after = estate.database.query("SELECT * FROM observations ORDER BY observation_id")
    assert [dict(r) for r in after] == [dict(r) for r in before]


# ------------------------------------------------------------------------ persistence


def test_a_revised_intent_survives_a_restart(estate: Estate, tmp_path: Path):
    adopted(estate)
    estate.write("eth0", "mtu", "1400")
    estate.observe()
    outcome = revise(estate, "eth0", {"mtu": 1400})
    object_id = outcome.object_id
    path = Path(estate.database.path)
    estate.database.close()

    reopened = open_database(path)
    try:
        service = ManagementService(reopened, freshness_ttl_s=60.0)
        active = service.intents.active_for([object_id])[object_id]
        assert active.intent_id == outcome.intent.intent_id
        assert active.version == 2
        assert active.origin == "revise"
        assert active.revision is not None
        assert active.revision.host_effect == "none"
        assert intended(active) == {"admin_up": True, "mtu": 1400}
        assert [i.version for i in service.intents.history(object_id)] == [2, 1]
    finally:
        reopened.close()


def test_a_store_written_before_0004_upgrades_and_keeps_its_findings(tmp_path: Path):
    """A database created by 0001–0003 opens, and every finding it held is still there."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for name in ("0001_initial.sql", "0002_management.sql", "0003_provenance.sql"):
        shutil.copy(MIGRATIONS_DIR / name, migrations / name)

    old = open_database(tmp_path / "upgrade.db", migrations)
    old.connection.execute("BEGIN IMMEDIATE")
    old.connection.execute(
        "INSERT INTO hosts VALUES ('h','machine_id','high',NULL,NULL,NULL,NULL,NULL,NULL,"
        "NULL,NULL,NULL,'[]','t','t')"
    )
    old.connection.execute(
        "INSERT INTO objects (object_id, host_id, kind, identity_basis, identity_value, "
        "identity_confidence, display_name, management_state, management_reason, "
        "first_seen_at, last_seen_at, active_intent_id) VALUES ('o','h','network.interface',"
        "'kernel_name','eth0','low','eth0','observed','management_candidate','t','t',NULL)"
    )
    old.connection.execute(
        "INSERT INTO observation_sweeps VALUES ('s','h',NULL,'network.observe',"
        "'linux.network','1','ok','t','t','t',1,'[]','[]')"
    )
    old.connection.execute(
        "INSERT INTO observations VALUES ('ob','s','h','o','network.observe','linux.network',"
        "'1','sysfs','complete','t','t','healthy','ok','[]','{\"mtu\":1400}','{}')"
    )
    old.connection.execute(
        "INSERT INTO intents VALUES ('i1','o','h',1,NULL,1,'adopt','network.observe',"
        "'linux.network','1','ob','s','t','t')"
    )
    old.connection.execute("INSERT INTO intent_fields VALUES ('i1','mtu','integer',1500)")
    old.connection.execute(
        "UPDATE objects SET management_state='managed', active_intent_id='i1' WHERE object_id='o'"
    )
    old.connection.execute(
        "INSERT INTO management_transitions VALUES "
        "('t1','o','h','adopt','observed','managed','i1','ob','none','t')"
    )
    old.connection.execute(
        "INSERT INTO findings (finding_id, finding_key, host_id, object_id, finding_type, "
        "subject, status, intent_id, intended_type, intended_value, observed_type, "
        "observed_value, comparison, reason, observation_id, sweep_id, first_seen_at, "
        "last_seen_at, updated_at, resolved_at, resolution, resolved_by_observation_id) "
        "VALUES ('f1','k1','h','o','network.interface.drift','mtu','open','i1','integer',"
        "1500,'integer',1400,'differs','observed_value_differs','ob','s','t','t','t',"
        "NULL,NULL,NULL)"
    )
    old.connection.execute(
        "INSERT INTO findings (finding_id, finding_key, host_id, object_id, finding_type, "
        "subject, status, intent_id, intended_type, intended_value, observed_type, "
        "observed_value, comparison, reason, observation_id, sweep_id, first_seen_at, "
        "last_seen_at, updated_at, resolved_at, resolution, resolved_by_observation_id) "
        "VALUES ('f0','k0','h','o','network.interface.drift','admin_up','resolved','i1',"
        "'boolean',1,'boolean',0,'differs','observed_value_differs','ob','s','t','t','t',"
        "'t','observed_matches_intent','ob')"
    )
    old.connection.execute("COMMIT")
    assert [r["version"] for r in old.query("SELECT version FROM schema_migrations")] == [1, 2, 3]
    old.close()

    shutil.copy(MIGRATIONS_DIR / "0004_intent_revision.sql", migrations / "0004_intent_revision.sql")
    upgraded = open_database(tmp_path / "upgrade.db", migrations)
    try:
        assert [
            r["version"] for r in upgraded.query("SELECT version FROM schema_migrations")
        ] == [1, 2, 3, 4]
        # Every finding is still there, with the ending it had.
        rows = upgraded.query("SELECT * FROM findings ORDER BY finding_id")
        assert [(r["finding_id"], r["status"], r["resolution"]) for r in rows] == [
            ("f0", "resolved", "observed_matches_intent"),
            ("f1", "open", None),
        ]
        assert rows[0]["resolved_by_observation_id"] == "ob"
        # The intents, the pointer and the transitions are untouched.
        assert upgraded.query_one("SELECT active_intent_id FROM objects")[0] == "i1"
        assert upgraded.query_one("SELECT version, origin FROM intents")[0] == 1
        assert upgraded.query("SELECT * FROM intent_revisions") == []
        assert upgraded.query("PRAGMA foreign_key_check") == []
        assert upgraded.query("PRAGMA integrity_check")[0][0] == "ok"
        # And the new resolution is now accepted, where it was not before.
        upgraded.connection.execute(
            "UPDATE findings SET status = 'resolved', resolved_at = 't', "
            "resolution = 'intent_revised' WHERE finding_id = 'f1'"
        )
    finally:
        upgraded.close()


def test_the_earlier_migrations_are_the_ones_older_stores_were_built_with(tmp_path: Path):
    """0004 adds. It does not edit history, and this is what would catch it if it had.

    A store built by 0001–0003 alone and then upgraded must record exactly the checksums a
    store built by all four records for those three. If any of the earlier files had been
    edited, the two would disagree here — and an existing store would refuse to open at
    all, because a migration that changed after it was applied is a hard failure.
    """
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    earlier = ("0001_initial.sql", "0002_management.sql", "0003_provenance.sql")
    for name in earlier:
        shutil.copy(MIGRATIONS_DIR / name, migrations / name)

    old = open_database(tmp_path / "staged.db", migrations)
    staged = {r["version"]: r["checksum"] for r in old.query("SELECT * FROM schema_migrations")}
    old.close()

    shutil.copy(MIGRATIONS_DIR / "0004_intent_revision.sql", migrations / "0004_intent_revision.sql")
    upgraded = open_database(tmp_path / "staged.db", migrations)
    # Built from the same four files, so the comparison stays about *these* migrations as
    # later ones are added alongside them.
    fresh = open_database(tmp_path / "fresh.db", migrations)
    try:
        upgraded_sums = {
            r["version"]: r["checksum"] for r in upgraded.query("SELECT * FROM schema_migrations")
        }
        fresh_sums = {
            r["version"]: r["checksum"] for r in fresh.query("SELECT * FROM schema_migrations")
        }
        assert {v: staged[v] for v in (1, 2, 3)} == {v: upgraded_sums[v] for v in (1, 2, 3)}
        assert upgraded_sums == fresh_sums
        assert sorted(fresh_sums) == [1, 2, 3, 4]
    finally:
        upgraded.close()
        fresh.close()


def test_the_fourth_migration_is_deterministic(tmp_path: Path):
    """Applied from scratch twice, the same schema comes out."""

    def schema_of(name: str) -> list[str]:
        db = open_database(tmp_path / name)
        try:
            return sorted(
                r["sql"] for r in db.query("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")
            )
        finally:
            db.close()

    assert schema_of("one.db") == schema_of("two.db")
