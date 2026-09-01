"""The first real host write, performed where it cannot reach this machine's network.

**Nothing here mutates this machine.** The write happens inside a container started with
``--network=none``, whose network namespace contains a loopback device and nothing else
until the payload creates two dummy links in it. The host's interfaces, addresses, routes,
policy rules, firewall, resolver and forwarding settings are not reachable from that
namespace, and this module captures all of them before and after and fails if any moved.

Why a container at all: unprivileged user namespaces are unusable on this machine —
``kernel.apparmor_restrict_unprivileged_userns`` is 1, so a process that creates one is
transitioned into the ``unprivileged_userns`` AppArmor profile, which denies every
capability and with it the id-map write. ``bwrap`` and ``unshare`` both fail there, there is
no ``newuidmap``, and elevating with ``sudo`` was not on the table. A container with
``--network=none`` and exactly one added capability is the narrowest remaining way to get a
namespace LocalPlane can safely write in.

**Docker is a test fixture here and nothing else.** It is not part of the privileged-helper
architecture, nothing in ``src/`` knows about it on this path, and the product's write is
the same ``RTM_NEWLINK``/``IFLA_MTU`` frame it would send anywhere. The container is
``--rm``, gets no host network, no ``--privileged``, no host namespaces, no socket mounts,
and exactly one capability: ``NET_ADMIN``, without which the kernel would refuse the write
that is the entire point.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.test_live_host import (
    link_configuration,
    provider_state,
    read_only,
    routing_state,
)

pytestmark = pytest.mark.live

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = "/lp/tests/live_container_write.py"

#: A local image that already carries a Python this build runs on and iproute2. Chosen
#: because it is present: this test pulls nothing, reaches no registry, and the container
#: has no network to reach one with.
IMAGE = "localplane-backend:latest"

#: Everything the container is allowed. Written out here rather than assembled in-line so
#: that a reader can check the whole isolation posture in one place, and so the test below
#: can assert it has not grown.
DOCKER_ARGV: tuple[str, ...] = (
    "docker", "run", "--rm",
    # No host network, no Docker network, no shared namespace: the namespace starts empty.
    "--network=none",
    # The one capability the kernel requires to accept a link mutation. Not --privileged.
    "--cap-add=NET_ADMIN",
    # The product's source, read-only. No socket, no /run, no /sys, no host control surface.
    "-v", f"{ROOT}/src:/lp/src:ro",
    "-v", f"{ROOT}/tests:/lp/tests:ro",
    # A writable scratch that exists only in memory, so the run touches no host filesystem.
    "--tmpfs", "/tmp:rw,exec,nosuid,size=64m",
    "-e", "PYTHONPATH=/lp/src:/lp",
    "-e", "PYTHONDONTWRITEBYTECODE=1",
    "--entrypoint", "python3",
    IMAGE,
    PAYLOAD,
)

REPORT_MARKER = "LOCALPLANE-LIVE-REPORT "


def _docker_available() -> bool:
    if read_only(["docker", "version", "--format", "{{.Server.Version}}"]) is None:
        return False
    images = read_only(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"])
    return bool(images) and IMAGE in images.splitlines()


@pytest.fixture(scope="module")
def host_state_before() -> dict:
    """Everything about this machine's networking that a write could disturb."""
    return {
        "links": link_configuration(),
        "routing": routing_state(),
        "providers": provider_state(),
        "netns": read_only(["ip", "netns", "list"]),
        "nftables": read_only(["nft", "list", "ruleset"]),
        "sysfs_mtu": {
            entry.name: (entry / "mtu").read_text().strip()
            for entry in sorted(Path("/sys/class/net").iterdir())
            if (entry / "ifindex").exists()
        },
    }


@pytest.fixture(scope="module")
def live_report(host_state_before: dict) -> dict[str, Any]:
    """Run the payload in a disposable namespace and return what it reported.

    Module-scoped: the write happens once, and every assertion below reads the same run.
    """
    if not _docker_available():
        pytest.skip(f"docker or the {IMAGE} image is not available on this host")
    completed = subprocess.run(
        list(DOCKER_ARGV),
        capture_output=True,
        text=True,
        timeout=600,
        env={"LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    lines = [
        line for line in completed.stdout.splitlines() if line.startswith(REPORT_MARKER)
    ]
    assert len(lines) == 1, completed.stdout
    return json.loads(lines[0][len(REPORT_MARKER) :])


# --------------------------------------------------------------------- the isolation


def test_the_fixture_asks_for_no_more_isolation_than_it_needs():
    """The whole posture, asserted rather than described.

    ``--privileged`` would hand the container the host. ``--network=host`` would hand it
    this machine's interfaces, which is precisely the thing the namespace exists to keep it
    away from. A second capability would be a second thing to argue for. None of them is
    here, and this fails if one arrives.
    """
    argv = list(DOCKER_ARGV)
    assert "--network=none" in argv
    assert [a for a in argv if a.startswith("--cap-add")] == ["--cap-add=NET_ADMIN"]
    for forbidden in (
        "--privileged",
        "--network=host",
        "--net=host",
        "--pid=host",
        "--userns=host",
        "--cap-add=ALL",
        "--security-opt",
        "--device",
    ):
        assert forbidden not in argv, forbidden
    # No host control surface is mounted, for convenience or otherwise.
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    for mount in mounts:
        source = mount.split(":", 1)[0]
        assert source.startswith(str(ROOT)), mount
        assert mount.endswith(":ro"), mount
    for forbidden in ("/run", "/var/run/docker.sock", "/sys", "/proc", "/etc", "/dev"):
        assert not any(m.split(":", 1)[0] == forbidden for m in mounts), forbidden
    assert "--rm" in argv


def test_the_namespace_starts_empty_and_holds_only_disposable_links(live_report: dict):
    """Nothing of this machine's is in there, and nothing of its own escapes."""
    assert live_report["namespace_links"] == ["lo", "lpmgmt0", "lptest0"]
    assert live_report["objects"] == ["lo", "lpmgmt0", "lptest0"]
    host_links = {entry["ifname"] for entry in link_configuration()}
    assert "lptest0" not in host_links
    assert "lpmgmt0" not in host_links


# ------------------------------------------------------------------- the real write


def test_the_privileged_helper_really_held_the_capability(live_report: dict):
    assert live_report["helper"]["effective_uid"] == 0
    assert live_report["helper"]["can_configure_network"] is True
    assert live_report["agent"]["mutating_methods"] == [
        "docker.container_lifecycle",
        "network.arm_mtu_guard",
        "network.set_interface_mtu",
    ]
    capabilities = live_report["agent"]["capabilities"]
    assert capabilities["network.interface.set_mtu"] == {
        "status": "available",
        "mutating": True,
    }
    assert {
        name for name, c in capabilities.items() if c["mutating"]
    } == {"network.interface.set_mtu", "network.interface.mtu_guard",
          "docker.container.lifecycle", "systemd.service.lifecycle"}
    # The guard is available exactly where the write it would reverse is: it *is* that
    # write, deferred, so a host that could not perform one could not undo one either.
    assert capabilities["network.interface.mtu_guard"] == {
        "status": "available",
        "mutating": True,
    }
    # And inside this container the *other* mutating capability has nowhere to go: no
    # Docker socket is mounted, so the only write path that exists here is the kernel one.
    assert capabilities["docker.container.lifecycle"] == {
        "status": "unavailable",
        "mutating": True,
    }
    # Part A passively describes the future typed mechanism even though this disposable
    # container has no system manager and no systemd mutation method exists in the agent.
    assert capabilities["systemd.service.lifecycle"]["mutating"] is True


def test_the_management_path_was_proven_from_real_kernel_evidence(live_report: dict):
    """Two independent sources agreeing, both real: an address and a routing answer.

    The local endpoint is genuinely on ``lpmgmt0`` and the kernel's own ``RTM_GETROUTE``
    for the peer genuinely leaves by it. The target is therefore proven *not* to be the
    path — which is the only condition under which this build will write at all.
    """
    path = live_report["management_path"]
    assert path["confirmed"] is True
    assert path["reason"] == "management_path_confirmed"
    assert path["resource_is_management_link"] is True
    assert path["target_is_the_path"] is False
    assert path["evidence_id"].startswith("mpo_")


def test_localplane_really_set_an_mtu_and_proved_it(live_report: dict):
    """The whole path, on a real kernel: plan, confirm, arm, write, verify, succeed."""
    success = live_report["scenarios"]["success"]

    published = success["published"]
    assert published["state"] == "preview"
    assert published["execution_availability"] == "available"
    assert published["execution_eligibility"] == "eligible"
    assert published["execution_blockers"] == []
    assert published["execution_provider"] == "linux.link"
    assert published["protection_status"] == "clear"
    assert published["management_path"] == "not_on_management_path"
    assert published["risk_tier"] == "medium"
    assert published["confirmation_method"] == "acknowledge"
    assert (published["current_value"], published["desired_value"]) == (1500, 1400)

    assert success["confirmation"]["source"] == "authenticated_request"
    assert success["confirmation"]["consumed_before_apply"] is False
    assert success["confirmation"]["consumed_after_apply"] is True
    assert success["checkpoint_existed_before_apply"] is True, "armed before it was needed"
    assert success["checkpoint"]["restores_value"] == 1500
    assert success["checkpoint"]["interface_name"] == "lptest0"
    assert success["checkpoint"]["management_path"] == "not_on_management_path"

    change = success["change"]
    assert change["mutation_outcome"] == "written"
    assert change["mutation_reason"] == "kernel_acknowledged"
    assert change["mutation_provider"] == "linux.link"
    assert change["mutation_method"] == "netlink_rtm_newlink_mtu"
    assert change["host_effect"] == "written"
    assert change["verification_outcome"] == "verified"
    assert change["verification_observed_value"] == 1400
    assert change["result"] == "succeeded"
    assert change["rollback_required"] is False
    assert success["run_state"] == "succeeded"
    assert success["run_host_effect"] == "written"

    # The kernel itself, read outside the product's own claim.
    assert live_report["mtu_at_start"] == 1500
    assert success["kernel_mtu_after"] == 1400
    assert success["write_lock_held"] is False


def test_the_transcript_of_the_real_write_records_every_transition(live_report: dict):
    assert live_report["scenarios"]["success"]["events"] == [
        "run_planned",
        "confirmation_satisfied",
        "confirmation_required",
        "confirmation_consumed",
        "arming_started",
        "checkpoint_written",
        "write_boundary_crossed",
        "mutation_dispatched",
        "mutation_result",
        "verification_started",
        "verification_result",
        "run_finished",
    ]


def test_the_verification_observation_is_what_resolved_the_drift(live_report: dict):
    """Reconciliation moved because a reading proved it, not because a Change existed."""
    success = live_report["scenarios"]["success"]
    assert success["reconciliation"] == "in_sync"
    assert success["open_findings"] == []
    resolved = success["resolved_findings"]
    assert len(resolved) == 1
    assert resolved[0]["subject"] == "mtu"
    assert resolved[0]["resolution"] == "observed_matches_intent"
    assert (
        resolved[0]["resolved_by_observation_id"]
        == success["change"]["verification_observation_id"]
    )


# ---------------------------------------------------------------------- the rollback


def test_a_real_write_that_fails_verification_is_really_rolled_back(live_report: dict):
    """A third party moves the value the instant LocalPlane's write lands.

    Everything about LocalPlane's own write is real and unmodified. What is arranged is the
    ordinary hazard verification exists for, and the restoration that follows goes through
    the same privileged typed path, is acknowledged by the same kernel, and is only called
    ``rolled_back`` once a fresh reading proves the checkpoint's value.
    """
    rollback = live_report["scenarios"]["rollback"]
    assert rollback["interfered"] is True

    change = rollback["change"]
    assert change["mutation_outcome"] == "written"
    assert change["verification_outcome"] == "mismatch"
    assert change["verification_observed_value"] == 1234, "the competing writer's value"
    assert change["rollback_required"] is True
    assert change["rollback_outcome"] == "written"
    assert change["rollback_verification_outcome"] == "verified"
    assert change["rollback_verification_observed_value"] == 1400
    assert change["result"] == "rolled_back"
    assert change["recovery_required"] is False
    assert rollback["run_state"] == "rolled_back"

    assert rollback["events"][-6:] == [
        "rollback_started",
        "rollback_mutation_dispatched",
        "rollback_mutation_result",
        "rollback_verification_started",
        "rollback_verification_result",
        "run_finished",
    ]
    # The kernel carries the checkpoint's value, and the intent it disagrees with is the
    # one the operator revised to — so LocalPlane reports drift rather than success.
    assert rollback["kernel_mtu_after"] == 1400
    # `mtu_at_end` is not asserted here any more: the recovery scenarios below continue from
    # this link and move it again, and their own endings are where that is checked.
    assert rollback["reconciliation"] == "drifted"
    assert rollback["open_findings"] == ["mtu"]


# --------------------------------------------------------------- and the two ways out of a hold


def test_a_hold_is_reached_honestly_before_any_of_it_is_recovered(live_report: dict):
    """Three real holds, each produced by a real kernel and a real third writer.

    Not simulated and not forced: a third party moves the value after every write, so
    LocalPlane's own write cannot be verified and its restoration cannot be proved either.
    That is the situation `recovery_required` exists for, and it is the only honest way to
    reach one without faking a result.
    """
    for name in ("proven", "verified", "resolved"):
        before = live_report["recovery"][name]["before"]
        assert before["result"] == "recovery_required", name
        assert before["recovery_reason"] == "rollback_verification_failed", name
        assert before["mutation_outcome"] == "written", name
        assert before["host_effect"] == "written", name
        assert before["write_lock_held"] is True, name
        assert before["mtu_after_apply"] == 1234, name


def test_a_real_retry_can_settle_a_hold_by_looking_rather_than_by_writing(live_report: dict):
    """The best ending there is, and it is proved by a number the product does not own.

    An administrator put the intended value on the link. A retry then established it through
    the ordinary observation path — a real sweep of real sysfs — and the privileged helper was
    **not asked to build a single frame**. The mutation count comes from the netlink transport
    itself rather than from LocalPlane's records, so it is not the claim checking itself.
    """
    facts = live_report["recovery"]["proven"]
    assert facts["helper_mutations"] == 0
    assert [a["outcome"] for a in facts["attempts"]] == ["proven"]
    attempt = facts["attempts"][0]
    assert attempt["kind"] == "retry"
    assert attempt["evidence_outcome"] == "verified"
    assert attempt["evidence_observed_value"] == 1281
    assert attempt["mutation_outcome"] is None
    assert attempt["host_effect"] == "none"
    assert attempt["management_path"] == "not_on_management_path"
    assert attempt["releases_hold"] is True
    assert attempt["confirmation_id"] is None

    assert facts["hold"] == {
        "state": "resolved", "released_by": "retry", "object_write_locked": False}
    assert facts["mtu_at_end"] == 1281
    # And the Change is exactly what it was. A later act does not rewrite what happened.
    assert facts["after"] == {
        "result": "recovery_required", "recovery_reason": "rollback_verification_failed",
        "mutation_outcome": "written", "host_effect": "written", "write_lock_held": False}
    assert facts["run_state"] == "recovery_required"


def test_a_real_retry_that_must_write_needs_authority_the_apply_did_not_leave_behind(
    live_report: dict,
):
    """A second real write to a real link, and the grant that had to exist before it.

    The confirmation the original apply consumed authorises nothing here: it authorised an
    attempt, and the attempt happened. The retry is refused until a separate grant exists,
    then dispatches one real `RTM_NEWLINK` through the privileged helper and proves the result
    with an independent reading.
    """
    facts = live_report["recovery"]["verified"]
    assert facts["refusals"] == ["recovery_confirmation_required"]
    assert facts["authority"]["purpose"] == "recovery_retry"
    assert facts["authority"]["method"] == "acknowledge"
    assert facts["authority"]["source"] == "authenticated_request"
    assert facts["authority"]["consumed_when_granted"] is False
    assert facts["authority"]["is_the_apply_confirmation"] is False

    # The refused attempt is on the record and it wrote nothing.
    assert [a["outcome"] for a in facts["attempts"]] == ["refused", "verified"]
    refused, wrote = facts["attempts"]
    assert refused["refusal_code"] == "recovery_confirmation_required"
    assert refused["mutation_outcome"] is None and refused["host_effect"] == "none"

    assert wrote["evidence_outcome"] == "mismatch"
    assert wrote["evidence_observed_value"] == 1234
    assert wrote["mutation_outcome"] == "written"
    assert wrote["mutation_provider"] == "linux.link"
    assert wrote["mutation_method"] == "netlink_rtm_newlink_mtu"
    assert wrote["host_effect"] == "written"
    assert wrote["verification_outcome"] == "verified"
    assert wrote["verification_observed_value"] == 1282
    assert wrote["confirmation_id"] is not None

    # Exactly one frame reached the kernel across both attempts.
    assert facts["helper_mutations"] == 1
    assert facts["mtu_at_end"] == 1282
    assert facts["hold"]["state"] == "resolved"
    assert facts["hold"]["object_write_locked"] is False
    assert facts["after"]["result"] == "recovery_required"
    assert facts["events"][-8:] == [
        "recovery_retry_started", "recovery_evidence_result", "recovery_retry_finished",
        "recovery_confirmation_satisfied", "recovery_retry_started",
        "recovery_evidence_result", "recovery_confirmation_consumed",
        "recovery_mutation_dispatched",
    ] or facts["events"][-6:] == [
        "recovery_confirmation_consumed", "recovery_mutation_dispatched",
        "recovery_mutation_result", "recovery_verification_result",
        "recovery_retry_finished", "recovery_hold_released",
    ]


def test_a_real_resolution_releases_the_hold_and_touches_no_link(live_report: dict):
    """A person says they have dealt with it. Nothing is built, sent, or claimed."""
    facts = live_report["recovery"]["resolved"]
    assert facts["helper_mutations"] == 0
    assert [a["outcome"] for a in facts["attempts"]] == ["resolved"]
    attempt = facts["attempts"][0]
    assert attempt["kind"] == "resolve"
    assert attempt["mutation_outcome"] is None
    assert attempt["host_effect"] == "none"
    assert attempt["verification_outcome"] == "not_attempted"
    assert attempt["confirmation_id"] is None
    assert attempt["operator_statement"] == "lptest0"
    # What could be seen is recorded, and it does not prove the intended value.
    assert attempt["evidence_outcome"] == "mismatch"
    assert attempt["evidence_observed_value"] == 1234

    assert facts["hold"] == {
        "state": "resolved", "released_by": "resolve", "object_write_locked": False}
    # The link still holds what the third writer left there. A resolution changes nothing.
    assert facts["mtu_at_end"] == 1234
    assert facts["after"]["result"] == "recovery_required"
    assert facts["after"]["recovery_reason"] == "rollback_verification_failed"
    assert facts["events"][-2:] == ["recovery_resolved", "recovery_hold_released"]


# ------------------------------------------------------- the guarded change, for real


def test_a_real_change_to_the_link_this_session_arrives_over_is_guarded_not_blocked(
    live_report: dict,
):
    """The situation every earlier build refused, on a real kernel, with a real guard.

    ``lpmgmt0`` is not nominally the management path — it is proven to be, from the same two
    independent sources every other judgement rests on: the local endpoint really is an
    address on it, and the kernel's own ``RTM_GETROUTE`` for the peer really leaves by it.
    Changing its MTU is therefore genuinely a change to the object this session is reached
    over, and the preview says so in as many words: ordinary execution blocked, guarded
    execution available, a typed confirmation, and nothing armed yet.
    """
    kept = live_report["guarded"]["kept"]
    published = kept["published"]

    assert published["management_path"] == "on_management_path"
    assert published["protection_status"] == "protected"
    assert published["execution_eligibility"] == "guarded"
    assert published["execution_blockers"] == ["target_is_management_path"]
    assert published["risk_tier"] == "high"
    assert published["confirmation_method"] == "typed"
    assert published["guard_availability"] == "available"
    assert published["guard_reason"] == "guarded_execution_available"
    assert published["guard_unmet"] == []
    assert published["guard_window_s"] > 0
    assert published["guard_armed"] == 0
    assert kept["confirmation"] == {"method": "typed", "typed_statement": "lpmgmt0"}


def test_a_real_guard_is_armed_on_the_host_before_the_kernel_is_written(live_report: dict):
    """Armed, by the agent, and durably recorded before the Change exists.

    The holder is the agent process in this namespace — not the backend, which in the
    deployment this is built for is a container that can disappear — and it says when its
    deadline is. One netlink frame reached the kernel for the change itself.
    """
    held = live_report["guarded"]["kept"]["held"]

    assert held["run_state"] == "guarded"
    assert held["run_host_effect"] == "written"
    assert held["mutation_outcome"] == "written"
    assert held["verification_outcome"] == "verified"
    assert held["guard_armed_before_the_change"] is True
    assert held["guard_holder_is_the_agent"] is True
    assert held["guard_expires_at"] is not None
    assert held["deadlines_live"] == 1
    assert held["kernel_mtu_while_held"] == 1476
    assert held["frames_for_the_write"] == 1


def test_a_real_guarded_change_is_kept_by_coming_back_over_the_changed_link(
    live_report: dict,
):
    """The proof is a request that arrived, and it releases the guard without writing.

    Fresh evidence: the observation that keeps the change is a *different* record from the
    one the guard was armed against, taken after the write over the path the write could
    have destroyed. Zero further frames reach the kernel, counted by the netlink transport
    itself rather than by anything LocalPlane recorded about itself.
    """
    kept = live_report["guarded"]["kept"]

    assert kept["deadlines_fired"] == 0
    assert kept["frames_for_the_reversal"] == 0
    assert kept["run_state"] == "succeeded"
    assert kept["change"]["result"] == "succeeded"
    assert kept["change"]["rollback_outcome"] is None
    assert kept["guard"] == {
        "settled_phase": "disarmed", "reversal_outcome": None,
        "kept": True, "kept_evidence_is_fresh": True}
    assert kept["deadlines_left"] == 0
    assert kept["kernel_mtu_after"] == 1476
    assert kept["write_lock_held"] is False
    assert kept["events"][-4:] == [
        "guard_hold_started", "guard_connection_proved", "guard_settled", "run_finished"]


def test_a_real_deadline_expiring_puts_a_real_link_back_with_nobody_asking(
    live_report: dict,
):
    """The mechanism doing the thing it exists for, against a real kernel.

    Nothing in the backend runs this. The deadline expires in the agent, and the agent
    dispatches the reversal it was armed with through the privileged helper — **one further
    netlink frame**, counted by the transport — and the link goes back to the value the
    checkpoint held. Only then is that collected and judged, and ``rolled_back`` is claimed
    only because a fresh reading through the ordinary observation path proves it.
    """
    reverted = live_report["guarded"]["reverted"]

    assert reverted["held"]["run_state"] == "guarded"
    assert reverted["held"]["kernel_mtu_while_held"] == 1477
    assert reverted["deadlines_fired"] == 1
    assert reverted["frames_for_the_reversal"] == 1

    assert reverted["run_state"] == "rolled_back"
    assert reverted["change"]["result"] == "rolled_back"
    assert reverted["change"]["mutation_outcome"] == "written"
    assert reverted["change"]["rollback_outcome"] == "written"
    assert reverted["change"]["rollback_verification_outcome"] == "verified"
    assert reverted["change"]["rollback_verification_observed_value"] == 1476
    assert reverted["change"]["recovery_reason"] is None
    assert reverted["guard"]["settled_phase"] == "fired"
    assert reverted["guard"]["reversal_outcome"] == "written"
    assert reverted["guard"]["kept"] is False
    # The link really is back, and nothing is left holding it.
    assert reverted["kernel_mtu_after"] == 1476
    assert reverted["write_lock_held"] is False
    assert reverted["deadlines_left"] == 0


def test_seven_changes_were_recorded_no_more_and_nothing_is_left_held(live_report: dict):
    """Two from the write-boundary scenarios, three recoveries and two guarded. No locks."""
    assert live_report["changes_recorded"] == 7
    assert live_report["recovery_attempts_recorded"] == 4
    assert live_report["guards_recorded"] == 2
    # One interference in the rollback scenario, and two in each of the three holds — every
    # write LocalPlane made *to the disposable target* was answered by a third party moving
    # the value. The guarded scenarios interfere with nothing.
    assert live_report["competing_writes"] == 7
    assert live_report["write_locks_left"] == 0
    assert live_report["mtu_at_end"] == 1234
    # And the management link ends where the guard left it, not where the second guarded
    # change tried to put it.
    assert live_report["management_mtu_at_start"] == 1500
    assert live_report["management_mtu_at_end"] == 1476


# ------------------------------------------------------------- and this machine is untouched


def test_nothing_about_this_machine_moved(host_state_before: dict, live_report: dict):
    """Every axis a write could disturb, compared before and after the real mutation.

    One test rather than six, because they answer one question and a partial answer to it is
    not useful: if any of these moved, the isolation failed and which axis noticed first is a
    detail. The most important line is the sysfs one — LocalPlane gained the ability to set an
    MTU, it set three of them, and not one was on this machine.
    """
    after_sysfs = {
        entry.name: (entry / "mtu").read_text().strip()
        for entry in sorted(Path("/sys/class/net").iterdir())
        if (entry / "ifindex").exists()
    }
    assert after_sysfs == host_state_before["sysfs_mtu"], "an MTU on this machine moved"
    assert link_configuration() == host_state_before["links"]

    after, before = routing_state(), host_state_before["routing"]
    for key in ("routes_v4", "routes_v6", "rules_v4", "rules_v6", "resolv_conf",
                "net.ipv4.ip_forward", "net.ipv6.conf.all.forwarding"):
        assert after[key] == before[key], key
    # Addresses are compared with the uplink's DHCP lease counters excluded: a lease ticking
    # down while a test runs is the host doing its own job, not a change.
    assert _addresses_without_lifetimes(after["addresses"]) == _addresses_without_lifetimes(
        before["addresses"])

    assert provider_state() == host_state_before["providers"]
    assert read_only(["ip", "netns", "list"]) == host_state_before["netns"]
    assert read_only(["nft", "list", "ruleset"]) == host_state_before["nftables"]

    # And the disposable container took its own record away with it.
    running = read_only(["docker", "ps", "-a", "--format", "{{.Names}}"]) or ""
    assert "localplane-live" not in running


def _addresses_without_lifetimes(entries: Any) -> Any:
    if entries is None:
        return None
    stripped = []
    for entry in entries:
        copy = dict(entry)
        copy["addr_info"] = [
            {k: v for k, v in info.items()
             if k not in ("valid_life_time", "preferred_life_time")}
            for info in entry.get("addr_info", [])
        ]
        stripped.append(copy)
    return stripped
