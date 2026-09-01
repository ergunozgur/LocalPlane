"""Recovery over HTTP: what a caller can name, and what a caller is told.

The same estate ``test_changes_api.py`` uses — a real agent socket, a real privileged helper
socket, a simulated kernel behind the helper's transport — driven through the actual API,
because the questions here are about the surface rather than about the engine.

The claims:

* **`recovery/retry` takes no request body.** There is no parameter through which a value, a
  verb, a target, a provider, a command or a shell could arrive, and bodies naming all of
  them change nothing, byte for byte;
* **the endpoints are scoped to one change** — there is no `/recover`, no `/execute`, no
  generic `/rollback` and no way to name an object, an operation or an outcome;
* **the `GET` answers what an operator needs to act**: why recovery is required, whether the
  object is held, what the original operation was, what was last observed, what recovery
  actions have happened, and what may be done next;
* **reading a change in recovery is still a read** — the temptation to "check whether it is
  fine now" is exactly the read that must not quietly become a write.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from localplane.backend.domain.runs import OperationType

from tests.test_changes_api import API, _Wired  # noqa: F401 - the estate under test
from tests.test_changes_api import planned as _planned
from tests.test_changes_api import sysfs as _sysfs
from tests.test_changes_api import wired as _wired


def _raw(fixture: Any) -> Any:
    return getattr(fixture, "__wrapped__", fixture)


sysfs = pytest.fixture(_raw(_sysfs))
wired = pytest.fixture(_raw(_wired))
planned = pytest.fixture(_raw(_planned))


def into_recovery(planned: _Wired) -> dict:
    """A write that lands, a verification that fails, and a restoration that proves nothing."""
    from localplane.helper.mtu import _NLMSGHDR

    from tests.conftest import netlink_ack

    run = planned.plan()
    planned.client.post(
        f"{API}/runs/{run['run_id']}/confirm",
        json={"preview_id": run["preview"]["preview_id"], "acknowledge": True},
    )
    state = {"n": 0}
    original = planned.kernel.mutate

    def acknowledge_and_diverge(frame: bytes) -> bytes:
        state["n"] += 1
        if state["n"] == 1:
            reply = original(frame)
            planned.kernel.links[2] = ("eth0", 9000)
            (planned.sysfs / "eth0" / "mtu").write_text("9000\n")
            return reply
        return netlink_ack(_NLMSGHDR.unpack_from(frame, 0)[3], errno=0)

    planned.kernel.mutate = acknowledge_and_diverge  # type: ignore[method-assign]
    try:
        body = planned.client.post(f"{API}/runs/{run['run_id']}/apply").json()
    finally:
        planned.kernel.mutate = original  # type: ignore[method-assign]
    assert body["state"] == "recovery_required", body
    return body["change"]


# ------------------------------------------------------------------------------ the surface


def test_retry_takes_no_body_and_there_is_nothing_to_smuggle_into_one(planned: _Wired):
    """Sixteen endpoints are not a `GET` and this is one of the two that can write.

    It has no request body at all, so a value, a verb, a target, a provider, a command or a
    shell has nowhere to arrive. Every one of these is sent and the answer is the same one an
    empty request gets, byte for byte — including the refusal, which is what a retry with
    nothing proven and no authority is entitled to.
    """
    change = into_recovery(planned)
    url = f"{API}/changes/{change['change_id']}/recovery/retry"

    baseline = planned.client.post(url)
    assert baseline.status_code == 409
    assert baseline.json()["error"]["code"] == "recovery_confirmation_required"
    first = baseline.json()["error"]["code"]

    for body in (
        {"mtu": 9000},
        {"desired_value": 9000},
        {"value": 1},
        {"field": "mtu"},
        {"action": "stop"},
        {"operation": "network.interface.reconcile_mtu"},
        {"object_id": planned.object_id("eth1")},
        {"command": "ip", "argv": ["link", "set", "eth0", "mtu", "9000"], "shell": True},
        {"provider": "linux.link", "method": "network.interface.set_mtu"},
        {"confirmation_id": "cnf_forged"},
        {"acknowledge": True},
        {"outcome": "verified", "releases_hold": True},
        {"force": True},
    ):
        answer = planned.client.post(url, json=body)
        assert answer.status_code == 409, body
        assert answer.json()["error"]["code"] == first, body

    # Nothing was written and the object is still held, after all of it.
    assert planned.mtu() == 9000
    assert planned.client.get(
        f"{API}/changes/{change['change_id']}"
    ).json()["recovery"]["object_write_locked"] is True


def test_the_recovery_requests_refuse_a_body_naming_anything_they_do_not_declare(
    planned: _Wired,
):
    """`extra` is forbidden, so an unexpected key is a 422 rather than something ignored."""
    change = into_recovery(planned)
    base = f"{API}/changes/{change['change_id']}/recovery"
    assert planned.client.post(
        f"{base}/confirm", json={"acknowledge": True, "mtu": 9000}
    ).status_code == 422
    assert planned.client.post(
        f"{base}/resolve",
        json={"acknowledge": True, "acknowledge_object": "eth0", "release_all": True},
    ).status_code == 422
    # And the required fields are required.
    assert planned.client.post(f"{base}/resolve", json={"acknowledge": True}).status_code == 422


def test_there_is_still_no_generic_recovery_or_execution_surface(planned: _Wired):
    """Closed typed operations remain the rule; recovery added no way round them."""
    change = into_recovery(planned)
    for path in (
        f"{API}/recover",
        f"{API}/recovery",
        f"{API}/recovery/retry",
        f"{API}/changes/{change['change_id']}/retry",
        f"{API}/changes/{change['change_id']}/resolve",
        f"{API}/changes/{change['change_id']}/rollback",
        f"{API}/changes/{change['change_id']}/recovery",
        f"{API}/changes/{change['change_id']}/recovery/execute",
        f"{API}/changes/{change['change_id']}/recovery/unlock",
        f"{API}/changes/{change['change_id']}/execute",
        f"{API}/locks",
        f"{API}/object-write-locks",
        f"{API}/execute",
        f"{API}/operations/network.interface.reconcile_mtu/execute",
    ):
        assert planned.client.post(path, json={}).status_code == 404, path
        assert planned.client.delete(path).status_code == 404, path


def test_recovery_endpoints_on_a_change_that_does_not_exist_are_a_404(planned: _Wired):
    for suffix, body in (
        ("retry", None), ("confirm", {"acknowledge": True}),
        ("resolve", {"acknowledge": True, "acknowledge_object": "eth0"}),
    ):
        answer = planned.client.post(f"{API}/changes/chg_nope/recovery/{suffix}", json=body)
        assert answer.status_code == 404
        assert answer.json()["error"]["code"] == "change_not_found"


# ------------------------------------------------------------------- what the GET publishes


def test_the_change_publishes_everything_a_ui_needs_to_act_on_a_hold(planned: _Wired):
    """Why, whether it is held, what it was for, what was seen, and what may be done next."""
    change = into_recovery(planned)
    body = planned.client.get(f"{API}/changes/{change['change_id']}").json()

    recovery = body["recovery"]
    assert recovery["required"] is True
    assert recovery["state"] == "unresolved"
    assert recovery["reason"] == "rollback_verification_failed"
    assert recovery["object_write_locked"] is True
    assert recovery["unknown"], "recovery must say what it does not know"
    assert recovery["released_at"] is None and recovery["released_by"] is None
    assert recovery["attempts"] == []
    assert recovery["last_observed"] == {}
    assert recovery["authority"] is None
    assert recovery["available_actions"] == ["retry", "confirm_retry", "resolve"]

    # What the original operation was, in the shape it actually had.
    assert body["change_kind"] == "field"
    assert (body["operation"], body["field"]) == ("network.interface.reconcile_mtu", "mtu")
    assert (body["before_value"], body["desired_value"]) == (1400, 1500)
    assert (body["action"], body["expected_state"]) == (None, None)
    assert body["result"] == "recovery_required"

    # And the list view agrees about the hold without needing the detail view.
    listed = planned.client.get(f"{API}/changes").json()["changes"]
    assert [(c["result"], c["recovery_state"]) for c in listed] == [
        ("recovery_required", "unresolved")]


def test_granting_authority_shows_up_and_changes_what_may_be_done_next(planned: _Wired):
    change = into_recovery(planned)
    answer = planned.client.post(
        f"{API}/changes/{change['change_id']}/recovery/confirm", json={"acknowledge": True})
    assert answer.status_code == 200

    recovery = answer.json()["recovery"]
    assert recovery["authority"]["method"] == "acknowledge"
    assert recovery["authority"]["source"] == "authenticated_request"
    assert recovery["available_actions"] == ["retry", "resolve"]
    assert recovery["state"] == "unresolved"
    # No token comes back that a caller could present anywhere else.
    assert "token" not in json.dumps(recovery)

    # A second grant while one is outstanding is refused.
    second = planned.client.post(
        f"{API}/changes/{change['change_id']}/recovery/confirm", json={"acknowledge": True})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "recovery_confirmation_already_satisfied"


def test_an_unacknowledged_grant_and_a_mismatched_reason_are_both_refused(planned: _Wired):
    change = into_recovery(planned)
    base = f"{API}/changes/{change['change_id']}/recovery"
    assert planned.client.post(
        f"{base}/confirm", json={"acknowledge": False}
    ).json()["error"]["code"] == "confirmation_not_acknowledged"
    assert planned.client.post(
        f"{base}/confirm",
        json={"acknowledge": True, "expected_recovery_reason": "apply_write_unknown"},
    ).json()["error"]["code"] == "recovery_reason_mismatch"
    assert planned.client.get(
        f"{API}/changes/{change['change_id']}"
    ).json()["recovery"]["authority"] is None


# ------------------------------------------------------------------------- the two ways out


def test_a_retry_over_http_writes_verifies_and_hands_the_object_back(planned: _Wired):
    change = into_recovery(planned)
    planned.client.post(
        f"{API}/changes/{change['change_id']}/recovery/confirm", json={"acknowledge": True})

    answer = planned.client.post(f"{API}/changes/{change['change_id']}/recovery/retry")
    assert answer.status_code == 200, answer.text
    body = answer.json()

    recovery = body["recovery"]
    assert recovery["state"] == "resolved"
    assert recovery["object_write_locked"] is False
    assert recovery["released_by"] == "retry"
    assert recovery["available_actions"] == []
    assert recovery["authority"] is None
    attempt = recovery["attempts"][-1]
    assert attempt["kind"] == "retry"
    assert attempt["outcome"] == "verified"
    assert attempt["evidence"]["outcome"] == "mismatch"
    assert attempt["evidence"]["observed_value"] == 9000
    assert attempt["mutation"]["outcome"] == "written"
    assert attempt["host_effect"] == "written"
    assert attempt["verification"]["outcome"] == "verified"
    assert attempt["verification"]["observed_value"] == 1500
    assert attempt["management_path"] == "not_on_management_path"
    assert recovery["last_observed"]["proves_intended_state"] is True
    assert planned.mtu() == 1500

    # The change itself is not rewritten by any of it.
    assert body["result"] == "recovery_required"
    assert body["recovery"]["reason"] == "rollback_verification_failed"
    assert body["host_effect"] == "written"

    # A second change against the object is now possible, because the object is free.
    planned.observe()
    second = planned.client.post(
        f"{API}/runs",
        json={"operation": {"type": "network.interface.reconcile_mtu",
                            "object_id": planned.object_id("eth0")}},
    )
    assert second.status_code in (201, 409)


def test_a_resolution_over_http_writes_nothing_and_claims_nothing(planned: _Wired):
    """It reads the host — that is how the evidence beside the decision gets recorded — and
    it writes nothing. No mutation reaches the kernel, and the store would refuse a
    resolution row that claimed one."""
    change = into_recovery(planned)
    before = planned.client.get(f"{API}/changes/{change['change_id']}").json()
    # The *facts* about every link, not the observation identity: a resolution takes a fresh
    # reading, which is a read of the host and moves the sweep it came from. What it may not
    # move is anything the host holds.
    links_before = json.dumps(
        [i["link"] for i in planned.client.get(f"{API}/network/interfaces").json()["interfaces"]],
        sort_keys=True)
    kernel_before = dict(planned.kernel.links)
    mutations_before = list(planned.kernel.mutations)

    answer = planned.client.post(
        f"{API}/changes/{change['change_id']}/recovery/resolve",
        json={"acknowledge": True, "acknowledge_object": "eth0",
              "note": "third writer identified"},
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()

    recovery = body["recovery"]
    assert recovery["state"] == "resolved"
    assert recovery["released_by"] == "resolve"
    assert recovery["object_write_locked"] is False
    attempt = recovery["attempts"][-1]
    assert attempt["kind"] == "resolve"
    assert attempt["outcome"] == "resolved"
    assert attempt["mutation"] is None
    assert attempt["verification"] is None
    assert attempt["host_effect"] == "none"
    assert attempt["operator_statement"] == "eth0"
    assert attempt["note"] == "third writer identified"
    # What could be seen is recorded, and it is not a verification.
    assert attempt["evidence"]["outcome"] == "mismatch"
    assert attempt["evidence"]["observed_value"] == 9000
    assert recovery["last_observed"]["proves_intended_state"] is False

    # Nothing about the change moved, and nothing about the host did.
    for key in ("result", "host_effect", "mutation", "verification", "rollback"):
        assert body[key] == before[key], key
    assert body["recovery"]["reason"] == before["recovery"]["reason"]
    assert planned.mtu() == 9000
    assert json.dumps(
        [i["link"] for i in planned.client.get(f"{API}/network/interfaces").json()["interfaces"]],
        sort_keys=True) == links_before
    assert dict(planned.kernel.links) == kernel_before
    assert planned.kernel.mutations == mutations_before


def test_typing_the_wrong_name_does_not_release_a_hold(planned: _Wired):
    change = into_recovery(planned)
    answer = planned.client.post(
        f"{API}/changes/{change['change_id']}/recovery/resolve",
        json={"acknowledge": True, "acknowledge_object": "eth1"},
    )
    assert answer.status_code == 409
    assert answer.json()["error"]["code"] == "recovery_resolution_object_mismatch"
    assert planned.client.get(
        f"{API}/changes/{change['change_id']}"
    ).json()["recovery"]["object_write_locked"] is True


def test_a_released_hold_cannot_be_released_again_by_either_route(planned: _Wired):
    change = into_recovery(planned)
    base = f"{API}/changes/{change['change_id']}/recovery"
    planned.client.post(
        f"{base}/resolve", json={"acknowledge": True, "acknowledge_object": "eth0"})
    for path, body in (
        ("resolve", {"acknowledge": True, "acknowledge_object": "eth0"}),
        ("retry", None),
        ("confirm", {"acknowledge": True}),
    ):
        answer = planned.client.post(f"{base}/{path}", json=body)
        assert answer.status_code == 409, path
        assert answer.json()["error"]["code"] == "recovery_already_resolved", path


def test_a_post_write_verification_that_could_not_be_taken_is_the_latest_reading(
    planned: _Wired,
):
    """An absence after a write must not be reported as the reading from before it.

    A verification that could not be taken at all has no observation id. Choosing the phase
    on that column silently falls back to the *pre-write* evidence — so an operator would be
    shown the value the target held **before** a mutation that has since happened, labelled
    as the latest thing LocalPlane saw. That is the one answer this field must never give.
    """
    from localplane.backend.runs import ObservationAttempt

    change = into_recovery(planned)
    planned.client.post(
        f"{API}/changes/{change['change_id']}/recovery/confirm", json={"acknowledge": True})

    executor = planned.app.state.context.changes._executors[
        OperationType.NETWORK_INTERFACE_RECONCILE_MTU]
    original = executor.observe
    calls = {"n": 0}

    def observe_then_go_dark(record):
        calls["n"] += 1
        if calls["n"] == 1:
            return original(record)
        return ObservationAttempt(record=None, failure="observation_unavailable")

    executor.observe = observe_then_go_dark  # type: ignore[method-assign]
    try:
        answer = planned.client.post(
            f"{API}/changes/{change['change_id']}/recovery/retry")
    finally:
        executor.observe = original  # type: ignore[method-assign]

    assert answer.status_code == 200, answer.text
    recovery = answer.json()["recovery"]
    attempt = recovery["attempts"][-1]

    # The write really happened and really could not be judged.
    assert attempt["mutation"]["outcome"] == "written"
    assert attempt["host_effect"] == "written"
    assert attempt["verification"]["outcome"] == "observation_unavailable"
    assert attempt["verification"]["observation_id"] is None
    assert attempt["outcome"] == "not_proven"
    assert recovery["state"] == "unresolved"
    assert recovery["object_write_locked"] is True

    # And the latest reading reported is that failure, not the pre-write evidence.
    latest = recovery["last_observed"]
    assert latest["phase"] == "verification"
    assert latest["outcome"] == "observation_unavailable"
    assert latest["observation_id"] is None
    assert latest["proves_intended_state"] is False
    # The pre-write evidence is still on the attempt, and it is not what `last_observed` says.
    assert attempt["evidence"]["outcome"] == "mismatch"
    assert attempt["evidence"]["observed_value"] == 9000
    assert latest["value"] != 9000

    # Reading it again says the same thing.
    again = planned.client.get(f"{API}/changes/{change['change_id']}").json()
    assert again["recovery"]["last_observed"] == latest


# --------------------------------------------------------------- evidence belongs to a request


def test_automation_that_cannot_prove_its_path_cannot_retry_but_can_still_resolve(
    planned: _Wired,
):
    """The reason the original write was safe is evidence a later caller does not have.

    A recovery arriving over loopback has proven nothing about where it is connected from, so
    it may not authorise or dispatch a write against an object whose change could remove a
    management path. It may still *release* the hold, because releasing writes nothing — and
    a build that blocked that too would leave an operator on a loopback deployment with a hold
    they could neither retry nor resolve.
    """
    change = into_recovery(planned)
    loopback = planned.loopback_client()
    base = f"{API}/changes/{change['change_id']}/recovery"

    for path, body in (("confirm", {"acknowledge": True}), ("retry", None)):
        answer = loopback.post(f"{base}/{path}", json=body)
        assert answer.status_code == 409, path
        assert answer.json()["error"]["code"] == "management_path_unproven", path

    assert planned.mtu() == 9000
    released = loopback.post(
        f"{base}/resolve", json={"acknowledge": True, "acknowledge_object": "eth0"})
    assert released.status_code == 200
    assert released.json()["recovery"]["state"] == "resolved"


def test_reading_a_change_in_recovery_is_still_a_read(planned: _Wired):
    """No agent contact, no observation, no attempt, no release. Twice, byte for byte."""
    change = into_recovery(planned)
    first = planned.client.get(f"{API}/changes/{change['change_id']}").json()
    second = planned.client.get(f"{API}/changes/{change['change_id']}").json()
    assert first == second
    assert planned.client.get(f"{API}/changes").json()["changes"][0]["recovery_state"] == (
        "unresolved")
    assert planned.mtu() == 9000
