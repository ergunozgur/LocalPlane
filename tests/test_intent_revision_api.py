"""The revision half of the HTTP surface.

Same shape as ``test_management_api.py``: a real agent process-in-a-thread over a real
socket, a real store, and a fixture ``/sys/class/net`` the tests rewrite in order to produce
genuine drift. Nothing constructs an observation by hand.

The claim asserted over and over is the one this surface exists to make: revising says what
LocalPlane wants, and says nothing to the host. It is checked twice — the response carries
``host_mutated: false`` and ``host_effect: none``, and the fixture tree is compared byte for
byte across every revision the file performs.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.test_management_api import (  # noqa: F401  (fixtures)
    API,
    adopt,
    client,
    interfaces,
    make_unreadable,
    object_id,
    release,
    runner,
    set_value,
    sysfs,
    tree_snapshot,
)


# ------------------------------------------------------------------------------ helpers


def active_intent(client: TestClient, name: str) -> dict:
    return client.get(f"{API}/network/interfaces/{object_id(client, name)}/intent").json()


def revise(client: TestClient, name: str, fields: dict, **body):
    payload = {"expected_intent_id": active_intent(client, name)["intent_id"], "fields": fields}
    payload.update(body)
    return client.post(
        f"{API}/network/interfaces/{object_id(client, name)}/intent/revise", json=payload
    )


def adopt_runtime(client: TestClient, name: str, **body):
    payload = {"expected_intent_id": active_intent(client, name)["intent_id"]}
    payload.update(body)
    return client.post(
        f"{API}/network/interfaces/{object_id(client, name)}/intent/adopt-runtime", json=payload
    )


def refresh(client: TestClient) -> None:
    client.post(f"{API}/network/observations/refresh")


def drift(client: TestClient, sysfs: Path, name: str = "eth0", mtu: str = "1400") -> None:
    set_value(sysfs, name, "mtu", mtu)
    refresh(client)


# ---------------------------------------------------------------------- explicit revision


def test_revising_writes_a_new_version_and_activates_it(client: TestClient):
    adopted = adopt(client, "eth0").json()
    response = revise(client, "eth0", {"mtu": 1400})
    assert response.status_code == 200
    body = response.json()

    assert body["kind"] == "revise"
    assert body["intent"]["version"] == 2
    assert body["intent"]["active"] is True
    assert body["intent"]["origin"] == "revise"
    assert body["intent"]["supersedes"] == adopted["intent"]["intent_id"]
    assert body["previous_intent"]["intent_id"] == adopted["intent"]["intent_id"]
    assert body["previous_intent"]["active"] is False
    assert body["previous_intent"]["origin"] == "adopt"


def test_the_revision_response_says_the_host_was_not_touched(client: TestClient):
    adopt(client, "eth0")
    body = revise(client, "eth0", {"mtu": 1400}).json()
    assert body["host_mutated"] is False
    assert body["host_effect"] == "none"
    assert body["intent"]["revision"]["host_effect"] == "none"
    assert "Nothing was written to the host" in body["note"]


def test_revising_does_not_change_a_single_value_on_the_host(client: TestClient, sysfs: Path):
    adopt(client, "eth0")
    before = tree_snapshot(sysfs)
    revise(client, "eth0", {"mtu": 9000})
    revise(client, "eth0", {"admin_up": False})
    assert tree_snapshot(sysfs) == before


def test_the_management_state_does_not_move(client: TestClient):
    adopt(client, "eth0")
    body = revise(client, "eth0", {"mtu": 1400}).json()
    assert body["management"] == {"state": "managed", "reason": "adopted"}
    assert interfaces(client)["eth0"]["management"] == {"state": "managed", "reason": "adopted"}


def test_a_revision_is_not_listed_as_a_management_transition(client: TestClient):
    adopt(client, "eth0")
    revise(client, "eth0", {"mtu": 1400})
    history = client.get(
        f"{API}/network/interfaces/{object_id(client, 'eth0')}/intent/history"
    ).json()
    assert [t["transition"] for t in history["transitions"]] == ["adopt"]


def test_the_response_names_what_moved_and_what_was_carried_forward(client: TestClient):
    adopt(client, "eth0")
    body = revise(client, "eth0", {"mtu": 1400}).json()

    assert body["changed_fields"] == [
        {"field": "mtu", "value_type": "integer", "was": 1500, "now": 1400}
    ]
    assert body["carried_forward"] == ["admin_up"]
    controlled = {f["field"]: f["value"] for f in body["intent"]["controlled_fields"]}
    assert controlled == {"admin_up": True, "mtu": 1400}


def test_a_boolean_and_an_integer_stay_apart_through_the_wire(client: TestClient):
    adopt(client, "eth0")
    body = revise(client, "eth0", {"admin_up": False}).json()
    fields = {f["field"]: f for f in body["intent"]["controlled_fields"]}
    assert fields["admin_up"]["value"] is False
    assert fields["admin_up"]["value_type"] == "boolean"
    assert fields["mtu"]["value"] == 1500
    assert fields["mtu"]["value_type"] == "integer"


def test_an_unsupported_field_is_a_structured_400(client: TestClient):
    adopt(client, "eth0")
    response = revise(client, "eth0", {"speed_mbps": 1000})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "unsupported_field"
    assert error["detail"]["field"] == "speed_mbps"
    assert error["detail"]["supported_fields"] == ["admin_up", "mtu"]


def test_a_wrongly_typed_value_is_a_structured_400(client: TestClient):
    adopt(client, "eth0")
    response = revise(client, "eth0", {"mtu": True})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_field_value"


def test_a_value_that_is_not_a_boolean_or_an_integer_is_refused_by_the_schema(
    client: TestClient,
):
    """A string is not one of the two types this API accepts, and it is not coerced."""
    adopt(client, "eth0")
    assert revise(client, "eth0", {"mtu": "1400"}).status_code == 422


def test_an_empty_revision_is_a_structured_400(client: TestClient):
    adopt(client, "eth0")
    response = revise(client, "eth0", {})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "empty_revision"


def test_a_revision_that_retains_what_is_retained_is_a_structured_409(client: TestClient):
    adopt(client, "eth0")
    response = revise(client, "eth0", {"mtu": 1500})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "revision_changes_nothing"


def test_revising_an_observed_interface_is_a_structured_409(client: TestClient):
    response = client.post(
        f"{API}/network/interfaces/{object_id(client, 'eth0')}/intent/revise",
        json={"expected_intent_id": "int_whatever", "fields": {"mtu": 1400}},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_managed"


def test_revising_a_watch_only_interface_is_a_structured_409(client: TestClient):
    response = client.post(
        f"{API}/network/interfaces/{object_id(client, 'lo')}/intent/revise",
        json={"expected_intent_id": "int_whatever", "fields": {"mtu": 9000}},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "object_observe_only"


def test_revising_an_unknown_object_is_a_structured_404(client: TestClient):
    response = client.post(
        f"{API}/network/interfaces/obj_nope/intent/revise",
        json={"expected_intent_id": "int_whatever", "fields": {"mtu": 1400}},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "object_not_found"


def test_a_request_without_an_expected_intent_is_refused_by_the_schema(client: TestClient):
    """The lost-update defence is not optional, so the field is not optional either."""
    adopt(client, "eth0")
    response = client.post(
        f"{API}/network/interfaces/{object_id(client, 'eth0')}/intent/revise",
        json={"fields": {"mtu": 1400}},
    )
    assert response.status_code == 422


# ------------------------------------------------------------------- adopting the runtime


def test_adopting_the_runtime_records_what_the_host_currently_says(
    client: TestClient, sysfs: Path
):
    adopt(client, "eth0")
    drift(client, sysfs)
    before = tree_snapshot(sysfs)

    body = adopt_runtime(client, "eth0").json()

    assert body["kind"] == "adopt_runtime"
    assert body["intent"]["origin"] == "adopt_runtime"
    assert body["intent"]["revision"]["kind"] == "adopt_runtime"
    assert {f["field"]: f["value"] for f in body["intent"]["controlled_fields"]} == {
        "admin_up": True,
        "mtu": 1400,
    }
    assert body["carried_forward"] == []
    assert body["host_mutated"] is False
    assert tree_snapshot(sysfs) == before


def test_adopting_the_runtime_names_the_observation_it_read(client: TestClient, sysfs: Path):
    adopt(client, "eth0")
    drift(client, sysfs)
    current = interfaces(client)["eth0"]["observation"]

    body = adopt_runtime(client, "eth0").json()
    assert body["intent"]["captured_from"]["observation_id"] == current["observation_id"]
    assert body["intent"]["captured_from"]["provider"] == "linux.network"


def test_adopting_a_runtime_that_already_agrees_is_a_structured_409(client: TestClient):
    adopt(client, "eth0")
    response = adopt_runtime(client, "eth0")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "revision_changes_nothing"


def test_adopting_the_runtime_is_refused_when_a_controlled_value_cannot_be_read(
    client: TestClient, sysfs: Path
):
    adopt(client, "eth0")
    drift(client, sysfs)
    make_unreadable(sysfs, "eth0", "flags")
    refresh(client)

    response = adopt_runtime(client, "eth0")
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "controlled_values_unverified"
    assert [f["field"] for f in error["detail"]["fields"]] == ["admin_up"]


# -------------------------------------------------------------------------- concurrency


def test_a_stale_expected_intent_is_a_structured_409(client: TestClient):
    adopted = adopt(client, "eth0").json()
    revise(client, "eth0", {"mtu": 1400})

    response = client.post(
        f"{API}/network/interfaces/{object_id(client, 'eth0')}/intent/revise",
        json={
            "expected_intent_id": adopted["intent"]["intent_id"],
            "fields": {"mtu": 9000},
        },
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "intent_revision_conflict"
    assert error["detail"]["expected_intent_id"] == adopted["intent"]["intent_id"]
    assert error["detail"]["active_version"] == 2
    # The newer operator decision is untouched.
    assert active_intent(client, "eth0")["controlled_fields"] == [
        {"field": "admin_up", "value_type": "boolean", "value": True},
        {"field": "mtu", "value_type": "integer", "value": 1400},
    ]


def test_a_stale_expected_version_is_a_structured_409(client: TestClient):
    adopt(client, "eth0")
    response = revise(client, "eth0", {"mtu": 1400}, expected_version=7)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "intent_revision_conflict"
    assert response.json()["error"]["detail"]["active_version"] == 1


def test_a_matching_expected_version_is_accepted(client: TestClient):
    adopt(client, "eth0")
    assert revise(client, "eth0", {"mtu": 1400}, expected_version=1).status_code == 200


# ----------------------------------------------------------------------- reconciliation


def test_a_revision_can_close_drift_without_anything_being_applied(
    client: TestClient, sysfs: Path
):
    adopt(client, "eth0")
    drift(client, sysfs)
    assert interfaces(client)["eth0"]["reconciliation"] == "drifted"
    before = tree_snapshot(sysfs)

    body = revise(client, "eth0", {"mtu": 1400}).json()

    assert body["reconciliation"]["state"] == "in_sync"
    assert interfaces(client)["eth0"]["reconciliation"] == "in_sync"
    assert tree_snapshot(sysfs) == before


def test_a_revision_that_still_disagrees_is_reported_as_drift(client: TestClient, sysfs: Path):
    adopt(client, "eth0")
    drift(client, sysfs)
    body = revise(client, "eth0", {"mtu": 1300}).json()

    assert body["reconciliation"]["state"] == "drifted"
    mtu = {f["field"]: f for f in body["reconciliation"]["fields"]}["mtu"]
    assert (mtu["intended"], mtu["observed"], mtu["comparison"]) == (1300, 1400, "differs")


def test_the_reconciliation_endpoint_agrees_with_the_revision_response(
    client: TestClient, sysfs: Path
):
    adopt(client, "eth0")
    drift(client, sysfs)
    body = revise(client, "eth0", {"mtu": 1400}).json()

    reconciliation = client.get(
        f"{API}/network/interfaces/{object_id(client, 'eth0')}/reconciliation"
    ).json()
    assert reconciliation["reconciliation"]["state"] == body["reconciliation"]["state"]
    assert reconciliation["intent"]["intent_id"] == body["intent"]["intent_id"]
    assert reconciliation["intent"]["version"] == 2


# --------------------------------------------------------------------------- findings


def test_a_revision_resolves_drift_as_revised_not_as_remediated(
    client: TestClient, sysfs: Path
):
    adopt(client, "eth0")
    drift(client, sysfs)
    opened = client.get(f"{API}/findings").json()["findings"]
    assert [f["subject"] for f in opened] == ["mtu"]

    body = revise(client, "eth0", {"mtu": 1400}).json()
    assert body["findings_resolved"] == [opened[0]["finding_id"]]

    finding = client.get(f"{API}/findings/{opened[0]['finding_id']}").json()
    assert finding["status"] == "resolved"
    assert finding["resolution"] == "intent_revised"
    assert finding["resolved_by_observation_id"] is None
    assert client.get(f"{API}/findings").json()["count"] == 0


def test_a_revision_that_creates_a_disagreement_opens_a_finding(client: TestClient):
    adopt(client, "eth0")
    body = revise(client, "eth0", {"mtu": 9000}).json()

    assert len(body["findings_opened"]) == 1
    finding = client.get(f"{API}/findings/{body['findings_opened'][0]}").json()
    assert finding["subject"] == "mtu"
    assert finding["status"] == "open"
    assert finding["evidence"]["intended"] == {"value_type": "integer", "value": 9000}
    assert finding["evidence"]["observed"] == {"value_type": "integer", "value": 1500}
    assert finding["evidence"]["intent_id"] == body["intent"]["intent_id"]


def test_a_recurrence_after_a_revision_is_a_new_episode(client: TestClient, sysfs: Path):
    adopt(client, "eth0")
    drift(client, sysfs)
    first = client.get(f"{API}/findings").json()["findings"][0]
    revise(client, "eth0", {"mtu": 1400})

    drift(client, sysfs, mtu="1300")

    episodes = client.get(f"{API}/findings?status=all").json()["findings"]
    assert len(episodes) == 2
    assert len({e["finding_key"] for e in episodes}) == 1
    latest = client.get(f"{API}/findings").json()["findings"][0]
    assert latest["finding_id"] != first["finding_id"]
    assert latest["evidence"]["intended"] == {"value_type": "integer", "value": 1400}
    # The first episode kept the ending it actually had.
    assert client.get(f"{API}/findings/{first['finding_id']}").json()["resolution"] == (
        "intent_revised"
    )


# ------------------------------------------------------------------------------ history


def test_the_history_reconstructs_the_whole_chain(client: TestClient, sysfs: Path):
    adopted = adopt(client, "eth0").json()
    second = revise(client, "eth0", {"mtu": 1400}).json()
    drift(client, sysfs, mtu="9000")
    third = adopt_runtime(client, "eth0").json()

    history = client.get(
        f"{API}/network/interfaces/{object_id(client, 'eth0')}/intent/history"
    ).json()

    assert history["count"] == 3
    assert history["active_intent_id"] == third["intent"]["intent_id"]
    assert [i["version"] for i in history["intents"]] == [3, 2, 1]
    assert [i["origin"] for i in history["intents"]] == ["adopt_runtime", "revise", "adopt"]
    assert [i["active"] for i in history["intents"]] == [True, False, False]
    assert [i["supersedes"] for i in history["intents"]] == [
        second["intent"]["intent_id"],
        adopted["intent"]["intent_id"],
        None,
    ]
    assert [i["revision"] is None for i in history["intents"]] == [False, False, True]
    assert {i["revision"]["kind"] for i in history["intents"][:2]} == {"revise", "adopt_runtime"}
    assert [
        {f["field"]: f["value"] for f in i["controlled_fields"]}["mtu"]
        for i in history["intents"]
    ] == [9000, 1400, 1500]


def test_releasing_after_a_revision_keeps_every_version(client: TestClient):
    adopt(client, "eth0")
    revise(client, "eth0", {"mtu": 1400})
    release(client, "eth0")

    history = client.get(
        f"{API}/network/interfaces/{object_id(client, 'eth0')}/intent/history"
    ).json()
    assert history["active_intent_id"] is None
    assert history["count"] == 2
    assert [i["active"] for i in history["intents"]] == [False, False]
    assert {t["transition"] for t in history["transitions"]} == {"adopt", "release"}


def test_the_interface_summary_names_the_version_in_force(client: TestClient):
    adopt(client, "eth0")
    body = revise(client, "eth0", {"mtu": 1400}).json()
    summary = interfaces(client)["eth0"]["intent"]
    assert summary["intent_id"] == body["intent"]["intent_id"]
    assert summary["version"] == 2
    assert summary["controlled_fields"] == ["admin_up", "mtu"]


# ---------------------------------------------------------------------------- purity


def test_reads_do_not_revise_anything(client: TestClient, sysfs: Path):
    adopt(client, "eth0")
    drift(client, sysfs)
    revise(client, "eth0", {"mtu": 1400})
    before_tree = tree_snapshot(sysfs)
    before = client.get(
        f"{API}/network/interfaces/{object_id(client, 'eth0')}/intent/history"
    ).json()

    object_ref = object_id(client, "eth0")
    for path in (
        "/network/interfaces",
        f"/network/interfaces/{object_ref}",
        f"/network/interfaces/{object_ref}/intent",
        f"/network/interfaces/{object_ref}/intent/history",
        f"/network/interfaces/{object_ref}/reconciliation",
        f"/network/interfaces/{object_ref}/provenance",
        f"/network/interfaces/{object_ref}/evidence",
        "/findings?status=all",
    ):
        assert client.get(f"{API}{path}").status_code == 200

    after = client.get(
        f"{API}/network/interfaces/{object_ref}/intent/history"
    ).json()
    assert after == before
    assert tree_snapshot(sysfs) == before_tree


def test_the_whole_revision_surface_leaves_the_fixture_host_byte_identical(
    client: TestClient, sysfs: Path
):
    before = tree_snapshot(sysfs)
    adopt(client, "eth0")
    adopt(client, "eth1")
    revise(client, "eth0", {"mtu": 9000})
    revise(client, "eth0", {"admin_up": False, "mtu": 1400})
    revise(client, "eth1", {"mtu": 1400})
    adopt_runtime(client, "eth1")
    release(client, "eth0")
    assert tree_snapshot(sysfs) == before


def test_revising_intent_gained_no_ability_to_mutate(client: TestClient):
    """Revising intent still writes nothing to the host.

    None of the mutating mechanisms in the product is reachable from these endpoints:
    revising replaces a retained value and never touches the runtime. The systemd lifecycle
    capability describes a passively detected future mechanism; Part A adds no mutating
    protocol method or executor for it.
    """
    body = client.get(f"{API}/agent/capabilities").json()
    assert body["capabilities"]
    mutating = {c["capability"] for c in body["capabilities"] if c["mutating"]}
    assert mutating == {
        "network.interface.set_mtu",
        "network.interface.mtu_guard",
        "docker.container.lifecycle",
        "systemd.service.lifecycle",
    }


# ---------------------------------------------------------------------------- schema


def test_the_schema_documents_the_revision_surface(client: TestClient):
    document = client.get("/openapi.json").json()
    paths = document["paths"]
    for path in (
        f"{API}/network/interfaces/{{object_id}}/intent/revise",
        f"{API}/network/interfaces/{{object_id}}/intent/adopt-runtime",
    ):
        assert set(paths[path]) == {"post"}

    result = document["components"]["schemas"]["IntentRevisionResult"]
    assert {
        "kind",
        "host_mutated",
        "host_effect",
        "previous_intent",
        "intent",
        "changed_fields",
        "carried_forward",
        "reconciliation",
        "findings_resolved",
        "findings_opened",
    } <= set(result["properties"])
    assert "Always false" in result["properties"]["host_mutated"]["description"]
    assert "Always 'none'" in result["properties"]["host_effect"]["description"]


def test_the_schema_requires_the_expected_intent_on_both_revisions(client: TestClient):
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    assert "expected_intent_id" in schemas["ExplicitIntentRevisionRequest"]["required"]
    assert "expected_intent_id" in schemas["IntentRevisionRequest"]["required"]
    assert "fields" in schemas["ExplicitIntentRevisionRequest"]["required"]
