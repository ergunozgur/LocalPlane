"""The real MTU write, performed by LocalPlane inside a disposable network namespace.

**This module never runs on the host.** It is executed inside a container started with
``--network=none --cap-add=NET_ADMIN``, whose network namespace contains nothing but a
loopback device until this script creates two dummy links in it. Every kernel write it
causes happens in that namespace and is destroyed with it; the host's own interfaces,
addresses, routes, rules, firewall and resolver are not reachable from here and are not
touched. ``tests/test_live_host.py`` proves that from the outside, before and after.

It is not a pytest module and carries no ``test_`` prefix, because it is the *payload*: the
container runs it, it prints one JSON document, and the test on the host asserts against
that document.

**What is real, and what the fixture supplies.** The privileged helper uses its real
``KernelLinkTransport`` — a genuine ``AF_NETLINK`` socket sending a genuine
``RTM_NEWLINK``/``IFLA_MTU`` frame to a genuine kernel. The agent reads real sysfs and real
rtnetlink. The management path is proven from a real ``RTM_GETROUTE`` answer corroborating a
real address on a real link. The one thing the fixture supplies is the pair of transport
addresses that would, in production, come from an accepted socket — because a container has
no operator connected to it.

Two scenarios run, in order, against the same disposable link:

1. **success** — plan, confirm, arm, write, verify, succeed, and watch the drift finding
   resolve from the verification observation;
2. **rollback** — the same path, with a third party changing the value the instant
   LocalPlane's write lands, so verification fails, the restoration runs through the same
   privileged path, and ``rolled_back`` is claimed only after a reading proves it.

and then the three ways a recovery hold ends, each against the same real kernel:

3. **proven** — a third party keeps interfering until the restoration cannot be proved
   either, so the Change ends ``recovery_required`` holding the link. Somebody then puts the
   intended value on the link by hand, and ``recoveryRetry`` establishes it by *looking*:
   the hold is released and the privileged helper is never asked to write;
4. **verified** — the same hold, and nothing has put it right. A retry is refused until an
   operator grants authority the original apply did not leave behind, and then writes for
   real and proves the result;
5. **resolved** — the same hold again, released by a person. No frame is built, no capability
   is used, and the value on the link at the end is the one the third party left there.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from localplane.agent.server import AgentServer
from localplane.agent.service import AgentService
from localplane.backend.agent_client import AgentClient
from localplane.backend.changes import ChangeService
from localplane.backend.db.database import open_database
from localplane.backend.db.repositories import FindingRepository
from localplane.backend.domain.identity import OBJECT_KIND_NETWORK_INTERFACE
from localplane.backend.domain.management_path import read_transport
from localplane.backend.domain.runs import OperationType
from localplane.backend.ingest import Ingestor, ObservationCoordinator
from localplane.backend.management import ManagementService
from localplane.backend.management_path import ManagementPathService
from localplane.backend.operations import OPERATIONS, build_executors
from localplane.backend.provenance import ProvenanceService
from localplane.backend.runs import RunService
from localplane.helper.client import HelperClient
from localplane.helper.mtu import KernelLinkTransport
from localplane.helper.server import HelperServer
from localplane.helper.service import HelperService

TARGET = "lptest0"
MANAGEMENT = "lpmgmt0"
LOCAL_ENDPOINT = "10.77.0.1"
PEER = "10.77.0.2"

STARTING_MTU = 1500
FIRST_TARGET_MTU = 1400
SECOND_TARGET_MTU = 1300
#: What a third party sets the value to the instant LocalPlane's second write lands.
COMPETING_MTU = 1234

#: One intended value per recovery scenario, all distinct from each other and from
#: ``COMPETING_MTU``, so that "the link holds the value LocalPlane wanted" can never be true
#: by accident in any of them.
RECOVERY_PROVEN_MTU = 1281
RECOVERY_RETRY_MTU = 1282
RECOVERY_RESOLVE_MTU = 1283

#: What the *management* link starts at, and the two values the guarded scenarios move it
#: to. Distinct from every value used on the disposable target, so "the link carries what
#: LocalPlane wanted" can never be true by accident.
MANAGEMENT_START_MTU = 1500
GUARD_KEPT_MTU = 1476
GUARD_REVERTED_MTU = 1477


def run(*argv: str) -> None:
    """One fixture command. Never the product's path — this builds the namespace."""
    subprocess.run(list(argv), check=True, capture_output=True)


def sysfs_mtu(name: str) -> int:
    return int(Path(f"/sys/class/net/{name}/mtu").read_text().strip())


class CompetingWriter(KernelLinkTransport):
    """The real kernel transport, with somebody else writing immediately afterwards.

    LocalPlane's own mutation is entirely real and entirely unmodified: the frame is built
    by the helper and accepted by the kernel. What this adds is the ordinary hazard the
    verification step exists for — another writer moving the value between the
    acknowledgement and the read-back — and it adds it *outside* the product, through the
    same tool an administrator would use.
    """

    def __init__(self, when: dict[str, bool]) -> None:
        super().__init__()
        self.when = when
        self.interfered = 0
        #: Every frame the privileged helper actually put on a netlink socket. Counted here
        #: because "a retry proved the end state without writing" is a claim about this
        #: number, and reading it from the product's own records would be checking the claim
        #: against the code that made it.
        self.mutations = 0

    def mutate(self, request: bytes) -> bytes:
        self.mutations += 1
        reply = super().mutate(request)
        if self.when.get("active"):
            # One interference by default — the rollback scenario — and every time while
            # `always` is set, which is how a restoration is made unprovable too.
            self.when["active"] = bool(self.when.get("always"))
            run("ip", "link", "set", TARGET, "mtu", str(COMPETING_MTU))
            self.interfered += 1
        return reply


class ManualDeadlines:
    """Every connection-guard deadline the agent arms, fired on demand.

    The seam is the timer and nothing else. Firing one runs exactly the code a real expiry
    runs, and what comes out the far end is a genuine netlink frame reaching a genuine
    kernel — which is what makes "the guard put the link back with nobody asking it to" a
    claim about this machine rather than about a simulation.
    """

    def __init__(self) -> None:
        self.armed: list[Any] = []

    def __call__(self, delay_s: float, action: Any) -> Any:
        pending = _Deadline(delay_s, action)
        self.armed.append(pending)
        return pending

    @property
    def live(self) -> list[Any]:
        return [p for p in self.armed if not p.cancelled]

    def fire(self) -> int:
        fired = 0
        for pending in list(self.live):
            # Marked before the action runs, because a deadline that has expired is no
            # longer armed — `live` means "still holding", and a fired guard is not.
            pending.cancelled = True
            pending.action()
            fired += 1
        return fired


class _Deadline:
    def __init__(self, delay_s: float, action: Any) -> None:
        self.delay_s = delay_s
        self.action = action
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


def build_namespace() -> None:
    """Two dummy links in an otherwise empty namespace, and a route to a fictional peer."""
    run("ip", "link", "add", TARGET, "type", "dummy")
    run("ip", "link", "set", TARGET, "up")
    run("ip", "link", "set", TARGET, "mtu", str(STARTING_MTU))
    run("ip", "link", "add", MANAGEMENT, "type", "dummy")
    run("ip", "addr", "add", f"{LOCAL_ENDPOINT}/24", "dev", MANAGEMENT)
    run("ip", "link", "set", MANAGEMENT, "up")
    run("ip", "link", "set", MANAGEMENT, "mtu", str(MANAGEMENT_START_MTU))
    run("ip", "route", "add", f"{PEER}/32", "dev", MANAGEMENT)


def main() -> int:
    report: dict[str, Any] = {"namespace_links": [], "scenarios": {}}
    build_namespace()
    report["namespace_links"] = sorted(
        p.name for p in Path("/sys/class/net").iterdir() if (p / "ifindex").exists()
    )
    report["target_ifindex"] = int(Path(f"/sys/class/net/{TARGET}/ifindex").read_text())
    report["mtu_at_start"] = sysfs_mtu(TARGET)

    workdir = Path(tempfile.mkdtemp(prefix="localplane-live-"))
    interference = {"active": False, "always": False}
    transport = CompetingWriter(interference)

    helper = HelperServer(workdir / "helper.sock", HelperService(transport=transport))
    helper.serve_in_thread()
    report["helper"] = {
        "effective_uid": os.geteuid(),
        "can_configure_network": HelperService(transport=transport).handle(
            "helper.hello", {}
        )["can_configure_network"],
    }

    # The one thing scripted in the guarded scenarios, and it is the passage of time. The
    # reversal a deadline dispatches is entirely real: the agent's own typed write, through
    # the real privileged helper, as a real RTM_NEWLINK to this namespace's kernel. Waiting
    # out a real two-minute window would make the suite unusable and would test the same
    # code path.
    deadlines = ManualDeadlines()
    service = AgentService(
        root="/",
        sysfs_net="/sys/class/net",
        helper_client=HelperClient(workdir / "helper.sock"),
        helper_socket=workdir / "helper.sock",
        guard_timer=deadlines,
    )
    agent = AgentServer(workdir / "agent.sock", service)
    agent.serve_in_thread()

    client = AgentClient(agent.socket_path, timeout_s=15.0)
    database = open_database(workdir / "localplane.db")
    provenance = ProvenanceService(database)
    management = ManagementService(database, freshness_ttl_s=60.0, provenance=provenance)
    ingestor = Ingestor(database, management)
    coordinator = ObservationCoordinator(client, ingestor)
    runs = RunService(database, 60.0, OPERATIONS, provenance)
    changes = ChangeService(
        database, runs, build_executors(client, coordinator, ingestor.objects)
    )
    path_service = ManagementPathService(database, client, 60.0)

    hello = client.hello()
    report["agent"] = {
        "mutating_methods": hello["agent"]["mutating_methods"],
        "privilege": hello["agent"]["privilege"],
        "capabilities": {
            c["capability"]: {"status": c["status"], "mutating": c["mutating"]}
            for c in hello["capabilities"]
        },
    }

    result = coordinator.refresh_network()
    host_id = result.host_id

    def object_named(name: str):
        for record in ingestor.objects.list_by_kind(host_id, OBJECT_KIND_NETWORK_INTERFACE):
            if record.display_name == name:
                return record
        raise AssertionError(f"no object named {name} in the namespace")

    report["objects"] = sorted(
        o.display_name
        for o in ingestor.objects.list_by_kind(host_id, OBJECT_KIND_NETWORK_INTERFACE)
    )

    # Adopt the disposable link at the value it already carries.
    adoption = management.adopt(object_named(TARGET))
    report["adopted"] = {
        "management_state": adoption.to_state,
        "host_effect": adoption.host_effect,
        "intent_mtu": next(f.value for f in adoption.intent.fields if f.field == "mtu"),
    }

    # Prove the management path from real kernel evidence: the local endpoint really is on
    # `lpmgmt0`, and the kernel's own RTM_GETROUTE for the peer really leaves by it.
    evidence = read_transport(PEER, LOCAL_ENDPOINT)
    verdict = path_service.observe(host_id, evidence)
    management_object = object_named(MANAGEMENT)
    report["management_path"] = {
        "confirmed": verdict.confirmed,
        "reason": verdict.reason,
        "resource_is_management_link": verdict.resource_id == management_object.object_id,
        "target_is_the_path": verdict.resource_id == object_named(TARGET).object_id,
        "evidence_id": verdict.evidence_id,
    }

    def revise(desired: int) -> None:
        record = object_named(TARGET)
        management.revise_intent(
            record, expected_intent_id=record.active_intent_id, fields={"mtu": desired}
        )

    def scenario(name: str, desired: int, interfere: bool) -> dict[str, Any]:
        revise(desired)
        coordinator.refresh_network()
        current_verdict = path_service.assess(host_id, evidence)
        record = object_named(TARGET)
        outcome = runs.create(
            OperationType.NETWORK_INTERFACE_RECONCILE_MTU, record, current_verdict
        )
        run_record = outcome.run
        published = {
            "state": run_record.state,
            "execution_availability": run_record.preview.execution_availability,
            "execution_eligibility": run_record.preview.execution_eligibility,
            "execution_blockers": list(run_record.preview.execution_blockers),
            "execution_provider": run_record.preview.execution_provider,
            "protection_status": run_record.preview.protection_status,
            "management_path": run_record.preview.protection_management_path,
            "risk_tier": run_record.preview.risk_tier,
            "confirmation_method": run_record.preview.confirmation_method,
            "current_value": run_record.preview.current_value,
            "desired_value": run_record.preview.desired_value,
            "validity": str(outcome.validity.state),
        }
        confirmation = changes.confirm(
            run_record,
            preview_id=run_record.preview.preview_id,
            acknowledge=True,
            expected_preview_digest=run_record.preview.preview_digest,
            management_path=current_verdict,
        )
        checkpoint_before_apply = changes.checkpoint_for(run_record.run_id)
        interference["active"] = interfere
        applied = changes.apply(run_record, current_verdict)
        interference["active"] = False

        change = applied.change
        checkpoint = changes.checkpoint_for(run_record.run_id)
        findings = FindingRepository(database)
        target = object_named(TARGET)
        intent = runs.intents.get(target.active_intent_id)
        reconciliation = management.reconciliation_for(target, intent)
        return {
            "published": published,
            "confirmation": {
                "source": confirmation.source,
                "method": confirmation.method,
                "consumed_before_apply": confirmation.consumed,
                "consumed_after_apply": changes.confirmation_for(
                    run_record.run_id
                ).consumed,
            },
            "checkpoint_existed_before_apply": checkpoint_before_apply is None,
            "checkpoint": {
                "restores_value": checkpoint.before_value,
                "desired_value": checkpoint.desired_value,
                "ifindex": checkpoint.execution_correlation.get("ifindex"),
                "interface_name": checkpoint.execution_correlation.get("interface_name"),
                "management_path": checkpoint.protection_management_path,
            },
            "run_state": applied.run.state,
            "run_host_effect": applied.run.host_effect,
            "change": {
                "change_id": change.change_id,
                "mutation_outcome": change.mutation_outcome,
                "mutation_reason": change.mutation_reason,
                "mutation_provider": change.mutation_provider,
                "mutation_method": change.mutation_method,
                "host_effect": change.host_effect,
                "verification_outcome": change.verification_outcome,
                "verification_observed_value": change.verification_observed_value,
                "verification_observation_id": change.verification_observation_id,
                "rollback_required": change.rollback_required,
                "rollback_outcome": change.rollback_outcome,
                "rollback_verification_outcome": change.rollback_verification_outcome,
                "rollback_verification_observed_value": (
                    change.rollback_verification_observed_value
                ),
                "result": change.result,
                "recovery_required": change.recovery_required,
                "recovery_reason": change.recovery_reason,
            },
            "events": [e.event for e in changes.transcript(run_record.run_id)],
            "kernel_mtu_after": sysfs_mtu(TARGET),
            "reconciliation": None if reconciliation is None else str(reconciliation.state),
            "open_findings": [f.subject for f in findings.open_for_object(target.object_id)],
            "resolved_findings": [
                {
                    "subject": f.subject,
                    "resolution": f.resolution,
                    "resolved_by_observation_id": f.resolved_by_observation_id,
                }
                for f in findings.history_for_object(target.object_id)
                if f.status == "resolved"
            ],
            "write_lock_held": changes.lock_for(run_record.run_id) is not None,
            "interfered": interfere,
        }

    def attempt_facts(change) -> list[dict[str, Any]]:
        return [
            {
                "kind": a.kind,
                "outcome": a.outcome,
                "refusal_code": a.refusal_code,
                "evidence_outcome": a.evidence_outcome,
                "evidence_observed_value": a.evidence_observed_value,
                "mutation_outcome": a.mutation_outcome,
                "mutation_provider": a.mutation_provider,
                "mutation_method": a.mutation_method,
                "host_effect": a.host_effect,
                "verification_outcome": a.verification_outcome,
                "verification_observed_value": a.verification_observed_value,
                "management_path": a.protection_management_path,
                "releases_hold": a.releases_hold,
                "operator_statement": a.operator_statement,
                "confirmation_id": a.confirmation_id,
            }
            for a in changes.recovery_history(change)
        ]

    def into_recovery(desired: int) -> Any:
        """Drive one Change to ``recovery_required`` against the real kernel.

        A third party moves the value after *every* write, so LocalPlane's own write cannot
        be verified and its restoration cannot be proved either — which is the situation the
        hold exists for and the only honest way to reach it without faking a result.
        """
        revise(desired)
        coordinator.refresh_network()
        current = path_service.assess(host_id, evidence)
        outcome = runs.create(
            OperationType.NETWORK_INTERFACE_RECONCILE_MTU, object_named(TARGET), current)
        changes.confirm(
            outcome.run, preview_id=outcome.run.preview.preview_id, acknowledge=True,
            expected_preview_digest=None, management_path=current)
        interference["always"] = True
        interference["active"] = True
        try:
            applied = changes.apply(outcome.run, current)
        finally:
            interference["always"] = False
            interference["active"] = False
        return applied.change

    def recovery_scenario(name: str, desired: int) -> dict[str, Any]:
        change = into_recovery(desired)
        before = {
            "result": change.result,
            "recovery_reason": change.recovery_reason,
            "mutation_outcome": change.mutation_outcome,
            "host_effect": change.host_effect,
            "write_lock_held": changes.lock_for(change.run_id) is not None,
            "mtu_after_apply": sysfs_mtu(TARGET),
        }
        mutations_before = transport.mutations
        facts: dict[str, Any] = {"before": before, "refusals": []}

        if name == "proven":
            # Somebody else puts the intended value on the link, the way an administrator
            # would. LocalPlane then establishes it by looking, and writes nothing.
            run("ip", "link", "set", TARGET, "mtu", str(desired))
            outcome = changes.recovery_retry(change, path_service.assess(host_id, evidence))
        elif name == "verified":
            try:
                changes.recovery_retry(change, path_service.assess(host_id, evidence))
            except Exception as exc:  # noqa: BLE001 - the refusal is the fact being recorded
                facts["refusals"].append(getattr(exc, "code", type(exc).__name__))
            grant = changes.recovery_confirm(
                change, acknowledge=True, expected_recovery_reason=change.recovery_reason,
                management_path=path_service.assess(host_id, evidence))
            facts["authority"] = {
                "purpose": grant.purpose, "method": grant.method, "source": grant.source,
                "consumed_when_granted": grant.consumed,
                "is_the_apply_confirmation": grant.confirmation_id == changes.confirmation_for(
                    change.run_id).confirmation_id,
            }
            outcome = changes.recovery_retry(change, path_service.assess(host_id, evidence))
        else:
            outcome = changes.recovery_resolve(
                change, acknowledge=True, operator_statement=TARGET, object_name=TARGET,
                note="handled by hand in the live namespace",
                expected_recovery_reason=change.recovery_reason)

        settled = changes.get_change(change.change_id)
        facts.update({
            "helper_mutations": transport.mutations - mutations_before,
            "attempts": attempt_facts(settled),
            "hold": {
                "state": str(outcome.hold.state),
                "released_by": outcome.hold.released_by,
                "object_write_locked": outcome.hold.object_write_locked,
            },
            "after": {
                "result": settled.result,
                "recovery_reason": settled.recovery_reason,
                "mutation_outcome": settled.mutation_outcome,
                "host_effect": settled.host_effect,
                "write_lock_held": changes.lock_for(change.run_id) is not None,
            },
            "run_state": runs.get(change.run_id).state,
            "events": [e.event for e in changes.transcript(change.run_id)],
            "mtu_at_end": sysfs_mtu(TARGET),
            "intended_mtu": desired,
        })
        return facts

    def guarded_scenario(name: str, desired: int, keep: bool) -> dict[str, Any]:
        """A real change to the link this session is actually reached over.

        The management path here is proven from real kernel evidence — the local endpoint is
        on ``lpmgmt0`` and the kernel's own ``RTM_GETROUTE`` for the peer leaves by it — so
        ``lpmgmt0`` is genuinely the object every request in this namespace arrives over,
        and changing it is genuinely the thing every earlier build refused.

        Two endings, and both of them are real writes to a real kernel:

        * **kept** — the operator comes back over the same link and the guard is released;
        * **reverted** — nothing comes back, the deadline expires, and the *agent* puts the
          value back through the privileged helper with the backend doing nothing at all.
        """
        record = object_named(MANAGEMENT)
        if record.active_intent_id is None:
            management.adopt(record)
            record = object_named(MANAGEMENT)
        management.revise_intent(
            record, expected_intent_id=record.active_intent_id, fields={"mtu": desired})
        coordinator.refresh_network()
        current_verdict = path_service.observe(host_id, evidence)
        record = object_named(MANAGEMENT)

        outcome = runs.create(
            OperationType.NETWORK_INTERFACE_RECONCILE_MTU, record, current_verdict)
        run_record = outcome.run
        published = {
            "execution_eligibility": run_record.preview.execution_eligibility,
            "execution_blockers": list(run_record.preview.execution_blockers),
            "management_path": run_record.preview.protection_management_path,
            "protection_status": run_record.preview.protection_status,
            "risk_tier": run_record.preview.risk_tier,
            "confirmation_method": run_record.preview.confirmation_method,
            "guard_availability": run_record.preview.guard_availability,
            "guard_reason": run_record.preview.guard_reason,
            "guard_unmet": list(run_record.preview.guard_unmet),
            "guard_window_s": run_record.preview.guard_window_s,
            "guard_armed": run_record.preview.guard_armed,
            "current_value": run_record.preview.current_value,
            "desired_value": run_record.preview.desired_value,
        }
        confirmation = changes.confirm(
            run_record,
            preview_id=run_record.preview.preview_id,
            acknowledge=True,
            acknowledge_object=MANAGEMENT,
            expected_preview_digest=run_record.preview.preview_digest,
            management_path=current_verdict,
        )

        frames_before = transport.mutations
        applied = changes.apply(run_record, current_verdict)
        guard = changes.guards.for_run(run_record.run_id)
        held = {
            "run_state": applied.run.state,
            "run_host_effect": applied.run.host_effect,
            "mutation_outcome": applied.change.mutation_outcome,
            "verification_outcome": applied.change.verification_outcome,
            "guard_armed_before_the_change": (
                guard.armed_at is not None and guard.armed_at <= applied.change.created_at),
            "guard_holder_is_the_agent": guard.holder_id == service.instance_id,
            "guard_expires_at": guard.expires_at,
            "deadlines_live": len(deadlines.live),
            "kernel_mtu_while_held": sysfs_mtu(MANAGEMENT),
            "frames_for_the_write": transport.mutations - frames_before,
        }

        frames_before_ending = transport.mutations
        fired = 0
        if not keep:
            # Nobody comes back. The deadline expires and the agent acts on its own.
            fired = deadlines.fire()
        settled = changes.guard_keep(
            runs.get(run_record.run_id),
            path_service.observe(host_id, evidence),
            acknowledge=True,
        )
        guard = changes.guards.for_run(run_record.run_id)
        return {
            "published": published,
            "confirmation": {"method": confirmation.method,
                             "typed_statement": confirmation.typed_statement},
            "held": held,
            "deadlines_fired": fired,
            "frames_for_the_reversal": transport.mutations - frames_before_ending,
            "run_state": settled.run.state,
            "change": {
                "result": settled.change.result,
                "mutation_outcome": settled.change.mutation_outcome,
                "rollback_outcome": settled.change.rollback_outcome,
                "rollback_verification_outcome": settled.change.rollback_verification_outcome,
                "rollback_verification_observed_value": (
                    settled.change.rollback_verification_observed_value),
                "recovery_reason": settled.change.recovery_reason,
            },
            "guard": {
                "settled_phase": guard.settled_phase,
                "reversal_outcome": guard.reversal_outcome,
                "kept": guard.kept_at is not None,
                "kept_evidence_is_fresh": (
                    guard.kept_evidence_id is not None
                    and guard.kept_evidence_id != guard.protection_evidence_id),
            },
            "events": [e.event for e in changes.transcript(run_record.run_id)],
            "deadlines_left": len(deadlines.live),
            "kernel_mtu_after": sysfs_mtu(MANAGEMENT),
            "intended_mtu": desired,
            "write_lock_held": changes.lock_for(run_record.run_id) is not None,
        }

    report["scenarios"]["success"] = scenario("success", FIRST_TARGET_MTU, interfere=False)
    report["scenarios"]["rollback"] = scenario("rollback", SECOND_TARGET_MTU, interfere=True)
    report["recovery"] = {
        "proven": recovery_scenario("proven", RECOVERY_PROVEN_MTU),
        "verified": recovery_scenario("verified", RECOVERY_RETRY_MTU),
        "resolved": recovery_scenario("resolved", RECOVERY_RESOLVE_MTU),
    }
    report["management_mtu_at_start"] = MANAGEMENT_START_MTU
    report["guarded"] = {
        "kept": guarded_scenario("kept", GUARD_KEPT_MTU, keep=True),
        "reverted": guarded_scenario("reverted", GUARD_REVERTED_MTU, keep=False),
    }
    report["management_mtu_at_end"] = sysfs_mtu(MANAGEMENT)
    report["guards_recorded"] = len(database.query("SELECT * FROM run_guards"))
    report["competing_writes"] = transport.interfered
    report["mtu_at_end"] = sysfs_mtu(TARGET)
    report["changes_recorded"] = len(database.query("SELECT * FROM changes"))
    report["recovery_attempts_recorded"] = len(
        database.query("SELECT * FROM change_recovery_attempts"))
    report["write_locks_left"] = len(database.query("SELECT * FROM object_write_locks"))

    database.close()
    agent.shutdown()
    agent.server_close()
    helper.shutdown()
    helper.server_close()

    sys.stdout.write("LOCALPLANE-LIVE-REPORT " + json.dumps(report) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
