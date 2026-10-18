"""Incoming references preserve direction, snapshot provenance and unknown coverage."""

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from localplane.backend.api.views import systemd_reference_context
from localplane.backend.app import create_app
from localplane.backend.config import Settings
from localplane.backend.db.repositories import ObjectRecord, ObservationRecord


def record(object_id, name, *, host="host", relationships=(), names=()):
    now = datetime.now(timezone.utc).isoformat()
    observation = ObservationRecord(
        "obs_" + object_id, "sweep", "systemd.units.observe", "systemd", "1",
        "dbus", "partial", now, now, "unknown", "unavailable", ["property unreadable"],
        {"canonical_id": name, "names": list(names), "relationships": list(relationships)},
    )
    return ObjectRecord(
        object_id, host, "systemd.unit", "systemd_unit", name, "authoritative", name,
        "observed", "discovered", None, now, now, observation,
    )


def edge():
    return dict(
        kind="Requires", group="requirement", target_unit="target.service",
        canonical_target=None, target_object_id="target", resolution="resolved",
        estate_state="current", source="systemd.dbus",
    )


def project(estate):
    return systemd_reference_context(estate[0], estate, None, None, 60)


def test_exact_identity_direction_self_reference_and_source_evidence():
    target = record("target", "target.service", relationships=[edge()])
    source = record("source", "source.service", relationships=[edge()])
    other_host = record("other", "source.service", host="elsewhere", relationships=[edge()])
    result = project([target, source, other_host])
    assert {r.source_object_id for r in result.references} == {"target", "source"}
    assert all(r.relationship.kind == "Requires" for r in result.references)
    assert all(r.relationship.target_object_id == "target" for r in result.references)
    assert result.references[0].source_observation.gaps == ["property unreadable"]
    assert result.references[0].source_observation.fidelity == "partial"


@pytest.mark.parametrize("change", [
    {"resolution": "referenced"}, {"resolution": "external"},
    {"estate_state": "not_observed"}, {"target_object_id": None},
    {"target_object_id": "same-name-other-id"},
])
def test_unresolved_historical_ids_and_names_do_not_join(change):
    relationship = edge() | change
    result = project([record("target", "target.service"), record("source", "source.service", relationships=[relationship])])
    assert result.references == []


def test_conflicting_alias_ownership_is_excluded_not_guessed():
    relation = edge() | {"target_unit": "shared.service"}
    target = record("target", "target.service", names=["shared.service"])
    conflict = record("conflict", "conflict.service", names=["shared.service"])
    source = record("source", "source.service", relationships=[relation])
    result = project([target, conflict, source])
    assert result.references == []
    assert result.excluded_ambiguous_count == 1


def test_missing_relationship_properties_and_stale_positive_evidence():
    target = record("target", "target.service")
    source = record("source", "source.service", relationships=[edge()])
    source = replace(source, observation=replace(source.observation, observed_at="2000-01-01T00:00:00+00:00"))
    result = project([target, source])
    assert result.references[0].source_observation.freshness == "stale"
    assert result.last_inventory_sweep is None
    assert result.inventory_freshness == "unknown"


@pytest.mark.parametrize("kind", ["After", "Before", "Requires", "RequiredBy", "Wants", "Triggers", "TriggeredBy", "PartOf", "BoundBy", "OnFailure"])
def test_original_kind_is_not_inverted(kind):
    source = record("source", "source.service", relationships=[edge() | {"kind": kind}])
    result = project([record("target", "target.service"), source])
    assert result.references[0].relationship.kind == kind


def test_alias_is_only_used_to_exclude_ambiguity_not_to_create_a_join():
    target = record("target", "target.service", names=["alias.service"])
    resolved = edge() | {"target_unit": "alias.service", "canonical_target": "target.service"}
    source = record("source", "source.service", relationships=[resolved])
    assert len(project([target, source]).references) == 1
    unresolved = record("other", "other.service", relationships=[resolved | {"target_object_id": None}])
    assert project([target, unresolved]).references == []


def test_committed_openapi_matches_backend_without_startup(tmp_path):
    settings = Settings(tmp_path / "unused.db", tmp_path / "unused.sock", 1, 60, "WARNING", False)
    expected = json.loads((Path(__file__).parents[1] / "web/openapi.json").read_text())
    assert create_app(settings).openapi() == expected
    assert not settings.database_path.exists()
