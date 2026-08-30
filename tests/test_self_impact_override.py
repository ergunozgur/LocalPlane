"""One narrowly typed authority for one narrowly typed hazard, and nothing else.

Step 1 derived whether a plan could interrupt the LocalPlane backend publishing it. This is
the authority an operator can grant against that derivation, and these tests are mostly
about what it still cannot do:

* **it opens a path; it softens nothing.** Protection stays exactly what the evidence made
  it and every blocker stays published, in the plan and in the store;
* **it is not a confirmation and a confirmation is not it.** Both are required, each is
  found by purpose, and the write boundary demands each through its own trigger — so the
  guarantee survives any mistake in the code above them;
* **only the one derived hazard reaches it.** `possible`, `unresolved`, an agent outside a
  host service, a second management-path unit in the closure, an unrecognised gap or any
  ordinary blocker leaves a plan `blocked`, and a blocked plan has nothing to grant;
* **it is exact-preview-bound and single-use**, and spending it happens atomically with the
  confirmation, in one transaction, once.

The policy branch is exercised directly as well as through a published plan, and the
persistence and boundary rules against seeded rows — which is the only way to prove the store
refuses what the code above it might one day get wrong.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from localplane.backend.db.repositories import (
    CONFIRMATION_PURPOSE_APPLY,
    CONFIRMATION_PURPOSE_SELF_IMPACT_OVERRIDE,
)
from localplane.backend.domain.guard import GuardAvailability, GuardPlan
from localplane.backend.domain.policy import (
    SELF_IMPACT_OVERRIDE_BLOCKER,
    assess_execution,
)
from localplane.backend.domain.protection import ProtectionStatus
from localplane.backend.domain.runs import (
    ExecutionAvailability,
    ExecutionEligibility,
    OperationType,
    PlanRefused,
    plan_digest,
)
from localplane.backend.domain.self_impact import SelfImpactStatus
from localplane.backend.domain.systemd_lifecycle import (
    SystemdServiceAction,
    assess_lifecycle_protection,
)
from localplane.backend.operations import OPERATIONS
from localplane.backend.systemd_operations import DEFINITIONS
from tests.test_self_impact import DOCKER_ENGINE, _assess, _hosted
from tests.test_systemd_lifecycle import _planning
# The estate fixtures come from the write-boundary suite, where the real agent, helper and
# simulated kernel that an ordinary apply needs are already assembled.
from tests.test_write_boundary import estate, ready  # noqa: F401 - pytest fixtures


def _no_guard() -> GuardPlan:
    return GuardPlan(
        availability=GuardAvailability.UNAVAILABLE,
        reason="operation_has_no_safe_automatic_inverse",
        window_s=0,
        prerequisites=(),
        unmet=(),
        guarantee="no connection guard is offered for systemd lifecycle actions",
    )


def _execution(context=None, *, available: bool = True, capability: bool = True):
    """What the policy makes of one self-impact plan.

    `availability` is a parameter so that the branch can be driven both ways: an override
    answers the self-impact hazard and nothing else, so a plan that is *also* missing its
    executor or its capability is blocked for a reason this authority does not address.
    """
    context = context if context is not None else _hosted()
    protection = assess_lifecycle_protection(context)
    return assess_execution(
        DEFINITIONS[SystemdServiceAction.RESTART],
        availability=(
            ExecutionAvailability.AVAILABLE if available
            else ExecutionAvailability.NOT_IMPLEMENTED
        ),
        provider="systemd",
        capability_declared=capability,
        ownership_block_reason=None,
        ownership_gaps=(),
        protection=protection,
        guard=_no_guard(),
        self_impact=_assess(context),
    )


# ------------------------------------------------------------------ the fourth eligibility


def test_the_one_derived_hazard_opens_a_fourth_path_and_removes_no_blocker():
    """Ordinary execution is still blocked, and the blockers still say why."""
    execution = _execution()

    assert execution.eligibility is ExecutionEligibility.SELF_IMPACT_OVERRIDE_REQUIRED
    assert execution.blockers == (SELF_IMPACT_OVERRIDE_BLOCKER,)
    assert SELF_IMPACT_OVERRIDE_BLOCKER == "protected:management_path"


def test_protection_is_untouched_by_the_path_being_open():
    """The verdict an operator is shown does not improve because a route exists."""
    context = _hosted()
    protection = assess_lifecycle_protection(context)

    assert _execution(context).eligibility is (
        ExecutionEligibility.SELF_IMPACT_OVERRIDE_REQUIRED
    )
    assert assess_lifecycle_protection(context) == protection
    assert protection.status is ProtectionStatus.PROTECTED


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"agent_unit_type": "scope"}, id="agent_in_a_container"),
        pytest.param({"gaps": ("systemd.effect_graph",), "complete": False},
                     id="incomplete_effect_graph"),
        pytest.param({"gaps": ("management_path.provider_owner:tailscale",)},
                     id="provider_owner_unresolved"),
        pytest.param({"gaps": ("a.gap.this.build.never.heard.of",)}, id="unknown_gap"),
        pytest.param({"units": ("target.service", DOCKER_ENGINE, "NetworkManager.service"),
                      "management": ("backend.scope", DOCKER_ENGINE, "NetworkManager.service")},
                     id="second_management_unit_in_closure"),
        pytest.param({"runtime_owner": None}, id="no_runtime_owner"),
    ],
)
def test_every_other_hazard_stays_blocked(overrides):
    """Default-deny: only the derived hazard reaches the fourth answer, everything else is
    `blocked` — including hazards nobody has thought of, which arrive as unknown gaps."""
    assert _execution(_hosted(**overrides)).eligibility is ExecutionEligibility.BLOCKED


def test_a_closure_that_misses_the_backend_needs_no_authority_at_all():
    """The other direction, and it is not a blocker: nothing to accept, so nothing to grant.

    A proven runtime owner outside the effect closure is `not_detected`, protection comes out
    `clear`, and the plan is ordinarily eligible. `blocked` here would be refusing a plan for
    a hazard that was established *not* to apply.
    """
    execution = _execution(_hosted(owner_in_closure=False))

    assert execution.eligibility is ExecutionEligibility.ELIGIBLE
    assert execution.blockers == ()
    assert _assess(_hosted(owner_in_closure=False)).override_eligible is False


def test_a_possible_backend_impact_is_never_offered_the_path():
    """Surfaced honestly and deliberately not authorised in this build."""
    context = _hosted(
        runtime_owner=None,
        complete=False,
        gaps=("management_path.runtime_owner_unproven",),
    )
    assert _assess(context).status is SelfImpactStatus.POSSIBLE
    assert _execution(context).eligibility is ExecutionEligibility.BLOCKED


@pytest.mark.parametrize(
    "kwargs", [{"available": False}, {"capability": False}], ids=["no_executor", "no_capability"]
)
def test_an_ordinary_blocker_is_not_something_this_authority_answers(kwargs):
    """`ordinary_only` non-empty means the override is not the only thing in the way.

    An operator cannot authorise past a missing executor or an undeclared capability, and
    being asked to would be being asked to accept a hazard that is not the one described.
    """
    execution = _execution(**kwargs)
    assert execution.eligibility is ExecutionEligibility.BLOCKED
    assert SELF_IMPACT_OVERRIDE_BLOCKER in execution.blockers


def test_a_published_plan_reaches_the_fourth_eligibility_and_says_why():
    """The derivation, the blockers and the eligibility agree, in a plan an operator sees."""
    plan = OPERATIONS[OperationType.SYSTEMD_SERVICE_RESTART].plan(
        _planning(SystemdServiceAction.RESTART, lifecycle=_hosted())
    )
    assert not isinstance(plan, PlanRefused)
    assert plan.self_impact.override_eligible is True
    assert plan.self_impact.status is SelfImpactStatus.PROVEN
    assert plan.execution.availability is ExecutionAvailability.AVAILABLE
    assert plan.execution.eligibility is ExecutionEligibility.SELF_IMPACT_OVERRIDE_REQUIRED
    assert plan.execution.blockers == (SELF_IMPACT_OVERRIDE_BLOCKER,)
    # And the confirmation the plan separately requires is still required and satisfiable.
    assert plan.confirmation.required is True
    assert plan.confirmation.satisfiable is True


def test_the_derivation_is_asked_as_well_as_the_blockers():
    """Two independent conditions, and the stricter one is the derivation.

    A plan whose blockers look right but whose derivation says the hazard is not the one
    this authority covers must not reach the fourth answer — otherwise the eligibility rule
    would be reconstructing the derivation from a string.
    """
    context = _hosted()
    protection = assess_lifecycle_protection(context)
    ineligible = replace(_assess(context), override_eligible=False)
    execution = assess_execution(
        DEFINITIONS[SystemdServiceAction.RESTART],
        availability=ExecutionAvailability.AVAILABLE,
        provider="systemd",
        capability_declared=True,
        ownership_block_reason=None,
        ownership_gaps=(),
        protection=protection,
        guard=_no_guard(),
        self_impact=ineligible,
    )
    assert execution.eligibility is ExecutionEligibility.BLOCKED


def test_an_operation_with_no_derivation_at_all_is_unaffected():
    """Every other operation in the build passes `None` and behaves exactly as before."""
    context = _hosted()
    execution = assess_execution(
        DEFINITIONS[SystemdServiceAction.RESTART],
        availability=ExecutionAvailability.AVAILABLE,
        provider="systemd",
        capability_declared=True,
        ownership_block_reason=None,
        ownership_gaps=(),
        protection=assess_lifecycle_protection(context),
        guard=_no_guard(),
    )
    assert execution.eligibility is ExecutionEligibility.BLOCKED


# ------------------------------------------------------------------ what the store refuses


def _seed(database, *, eligibility: str, self_impact: str | None, digest_version: int = 6):
    """One systemd preview and its Run, published under the given eligibility.

    Seeded rather than planned because the fixture controls the stored eligibility
    directly, instead of constructing the narrow scenario under which a planner would
    publish the fourth eligibility. What is being tested is the store's own rules, which
    have to hold whatever the code above them does.
    """
    from tests.test_runs import _seed_a_published_plan

    _seed_a_published_plan(database)
    row = dict(database.query("SELECT * FROM run_previews")[0])
    row.update({
        "preview_id": "prv_si", "preview_digest": "sha256:si",
        "digest_version": digest_version,
        "operation": "systemd.service.restart", "change_kind": "action",
        "action": "restart", "observed_state": "active",
        "expected_state": "active_with_new_invocation_id",
        "field": None, "value_type": None, "current_value": None, "desired_value": None,
        "intent_id": None, "intent_version": None, "intent_capability": None,
        "intent_provider": None, "drift_finding_id": None,
        "authorization_assessment": "{}", "lifecycle_context": "{}",
        "self_impact": self_impact,
        "execution_availability": "available", "execution_eligibility": eligibility,
    })
    columns = list(row)
    database.connection.execute(
        f"INSERT INTO run_previews ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        tuple(row[c] for c in columns),
    )
    database.connection.execute(
        "INSERT INTO runs (run_id, host_id, object_id, operation, state, preview_id, "
        "host_effect, created_at, cancelled_at) VALUES "
        "('run_si','h','o','systemd.service.restart','preview','prv_si','none','t',NULL)"
    )


ELIGIBLE_DOCUMENT = '{"subject": "localplane_backend_runtime", "status": "proven", ' \
    '"outage": "temporary_possible", "override_eligible": true, "detail": "d", ' \
    '"owner_unit_id": "docker.service", "envelope": "docker-direct-unix-v1", "reasons": []}'
INELIGIBLE_DOCUMENT = ELIGIBLE_DOCUMENT.replace("true", "false")


def test_the_store_refuses_the_fourth_eligibility_without_a_derivation_supporting_it(
    database,
):
    """A preview may say the override is what its execution rests on only where the
    document it froze says the override is possible. The store checks its own evidence
    rather than trusting whatever wrote both halves."""
    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction():
            _seed(database, eligibility="self_impact_override_required",
                  self_impact=INELIGIBLE_DOCUMENT)

    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction():
            _seed(database, eligibility="self_impact_override_required", self_impact=None)


def test_a_supported_preview_is_accepted(database):
    with database.transaction():
        _seed(database, eligibility="self_impact_override_required",
              self_impact=ELIGIBLE_DOCUMENT)
    stored = database.query_one("SELECT * FROM run_previews WHERE preview_id='prv_si'")
    assert stored["execution_eligibility"] == "self_impact_override_required"


def _grant(database, *, purpose: str, consumed: bool, preview_digest: str = "sha256:si",
           confirmation_id: str = "cnf_si", method: str = "acknowledge") -> None:
    database.connection.execute(
        "INSERT INTO run_confirmations (confirmation_id, run_id, purpose, preview_id, "
        "preview_digest, digest_version, required_method, method, typed_statement, policy, "
        "source, satisfied_at, consumed_at, consumed_by_attempt_id) VALUES "
        "(?, 'run_si', ?, 'prv_si', ?, 6, ?, ?, NULL, 'pol', 'unauthenticated_request', "
        "'t', ?, ?)",
        (confirmation_id, purpose, preview_digest, method, method,
         "t" if consumed else None, "att" if consumed else None),
    )


def _change(database) -> None:
    database.connection.execute(
        "INSERT INTO changes (change_id, run_id, preview_id, host_id, object_id, operation, "
        "change_kind, action, observed_state, expected_state, execution_correlation, "
        "created_at, apply_attempt_id) VALUES "
        "('chg_si','run_si','prv_si','h','o','systemd.service.restart','action','restart',"
        "'active','active_with_new_invocation_id','{}','t','apl_si')"
    )


def test_a_change_cannot_cross_the_boundary_without_the_override_consumed(database):
    """The smallest independent statement of the rule, and it does not consult the code.

    An apply confirmation alone is not enough for a plan whose published eligibility says
    its execution rests on the override — which is exactly the substitution the two
    authorities exist to prevent.
    """
    with database.transaction():
        _seed(database, eligibility="self_impact_override_required",
              self_impact=ELIGIBLE_DOCUMENT)
        _grant(database, purpose=CONFIRMATION_PURPOSE_APPLY, consumed=True)

    with pytest.raises(sqlite3.IntegrityError, match="self-impact override"):
        with database.transaction():
            _change(database)


def test_an_unconsumed_override_is_not_authority_either(database):
    """Granted is not spent. A Change may exist only once the authority has been used."""
    with database.transaction():
        _seed(database, eligibility="self_impact_override_required",
              self_impact=ELIGIBLE_DOCUMENT)
        _grant(database, purpose=CONFIRMATION_PURPOSE_APPLY, consumed=True)
        _grant(database, purpose=CONFIRMATION_PURPOSE_SELF_IMPACT_OVERRIDE, consumed=False,
               confirmation_id="cnf_ov")

    with pytest.raises(sqlite3.IntegrityError, match="self-impact override"):
        with database.transaction():
            _change(database)


def test_an_override_cannot_stand_in_for_the_confirmation(database):
    """The mirror image, guarded by the other trigger, which knows nothing about this one."""
    with database.transaction():
        _seed(database, eligibility="self_impact_override_required",
              self_impact=ELIGIBLE_DOCUMENT)
        _grant(database, purpose=CONFIRMATION_PURPOSE_SELF_IMPACT_OVERRIDE, consumed=True,
               confirmation_id="cnf_ov")

    with pytest.raises(sqlite3.IntegrityError, match="consumed confirmation"):
        with database.transaction():
            _change(database)


def test_both_consumed_authorities_let_a_systemd_change_exist(database):
    """And only then. Two triggers, both satisfied, one Change — and that is the whole gate.

    `changes.operation` admits the three systemd verbs since the executor arrived; what
    still refuses is a Change that arrives without both authorities spent.
    """
    with database.transaction():
        _seed(database, eligibility="self_impact_override_required",
              self_impact=ELIGIBLE_DOCUMENT)
        _grant(database, purpose=CONFIRMATION_PURPOSE_APPLY, consumed=True)
        _grant(database, purpose=CONFIRMATION_PURPOSE_SELF_IMPACT_OVERRIDE, consumed=True,
               confirmation_id="cnf_ov")
        _change(database)

    stored = database.query_one("SELECT * FROM changes WHERE change_id='chg_si'")
    assert stored["operation"] == "systemd.service.restart"
    assert stored["change_kind"] == "action"
    # An action carries no checkpoint and no rollback material, so it cannot claim one.
    assert stored["checkpoint_id"] is None
    assert stored["rollback_outcome"] is None


def test_a_plan_that_does_not_rest_on_the_override_needs_only_the_confirmation(database):
    """The new trigger fires for one kind of plan and is silent for every other.

    Demonstrated on the ordinary MTU plan, which is the kind of Change this build can
    actually record: its preview does not say the override is required, so the second
    trigger has nothing to say and one consumed confirmation is the whole requirement.
    """
    from tests.test_runs import _seed_a_published_plan

    with database.transaction():
        _seed_a_published_plan(database)
        database.connection.execute(
            "INSERT INTO run_confirmations (confirmation_id, run_id, purpose, preview_id, "
            "preview_digest, digest_version, required_method, method, typed_statement, "
            "policy, source, satisfied_at, consumed_at, consumed_by_attempt_id) VALUES "
            "('cnf_mtu','run_legacy','apply','prv_legacy','sha256:legacy',1,'acknowledge',"
            "'acknowledge',NULL,'pol','unauthenticated_request','t','t','att')")
        # Shaped as an action so that the checkpoint rule has nothing to say about it and
        # the only rules left standing are the two authority ones being tested.
        database.connection.execute(
            "INSERT INTO changes (change_id, run_id, preview_id, host_id, object_id, "
            "operation, change_kind, action, observed_state, expected_state, "
            "execution_correlation, created_at, apply_attempt_id) VALUES "
            "('chg_mtu','run_legacy','prv_legacy','h','o','docker.container.start','action',"
            "'start','exited','running','{}','t','apl_mtu')")

    assert database.query_one("SELECT * FROM changes WHERE change_id='chg_mtu'") is not None


def test_authority_cannot_accumulate(database):
    """One override per run, ever — a partial unique index, not a code path."""
    with database.transaction():
        _seed(database, eligibility="self_impact_override_required",
              self_impact=ELIGIBLE_DOCUMENT)
        _grant(database, purpose=CONFIRMATION_PURPOSE_SELF_IMPACT_OVERRIDE, consumed=True,
               confirmation_id="cnf_ov")

    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction():
            _grant(database, purpose=CONFIRMATION_PURPOSE_SELF_IMPACT_OVERRIDE,
                   consumed=False, confirmation_id="cnf_ov2")


def test_an_override_is_consumed_once(database):
    """The same single-use rule the other purposes get, from the same trigger."""
    with database.transaction():
        _seed(database, eligibility="self_impact_override_required",
              self_impact=ELIGIBLE_DOCUMENT)
        _grant(database, purpose=CONFIRMATION_PURPOSE_SELF_IMPACT_OVERRIDE, consumed=True,
               confirmation_id="cnf_ov")

    with pytest.raises(sqlite3.IntegrityError, match="single-use"):
        with database.transaction():
            database.connection.execute(
                "UPDATE run_confirmations SET consumed_at = 'later' "
                "WHERE confirmation_id = 'cnf_ov'")


def test_an_override_may_not_be_typed(database):
    """There is no second object to name and nothing to type that is not in the document."""
    with database.transaction():
        _seed(database, eligibility="self_impact_override_required",
              self_impact=ELIGIBLE_DOCUMENT)

    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction():
            database.connection.execute(
                "INSERT INTO run_confirmations (confirmation_id, run_id, purpose, preview_id, "
                "preview_digest, digest_version, required_method, method, typed_statement, "
                "policy, source, satisfied_at) VALUES "
                "('cnf_t','run_si','self_impact_override','prv_si','sha256:si',6,'typed',"
                "'typed','target.service','pol','unauthenticated_request','t')")


def test_an_override_must_name_the_preview_its_own_run_published(database):
    """Reused verbatim from the confirmation rule, because it is the same rule."""
    with database.transaction():
        _seed(database, eligibility="self_impact_override_required",
              self_impact=ELIGIBLE_DOCUMENT)

    with pytest.raises(sqlite3.IntegrityError, match="preview its own run published"):
        with database.transaction():
            database.connection.execute(
                "INSERT INTO run_confirmations (confirmation_id, run_id, purpose, preview_id, "
                "preview_digest, digest_version, required_method, method, typed_statement, "
                "policy, source, satisfied_at) VALUES "
                "('cnf_x','run_si','self_impact_override','prv_legacy','sha256:legacy',1,"
                "'acknowledge','acknowledge',NULL,'pol','unauthenticated_request','t')")


# ------------------------------------------------------------------- what the service refuses


def _services(database):
    """A Change service over a seeded store. No executors: nothing here dispatches."""
    from localplane.backend.changes import ChangeService
    from localplane.backend.operations import OPERATIONS as ALL_OPERATIONS
    from localplane.backend.runs import RunService

    runs = RunService(database, 60.0, ALL_OPERATIONS)
    return runs, ChangeService(database, runs, {})


def _unknown_path():
    from localplane.backend.domain.protection import ManagementPathVerdict

    return ManagementPathVerdict(resource_id=None, reason="management_path_unresolved")


def test_a_plan_that_does_not_rest_on_the_override_has_none_to_grant(database):
    """Read from the published preview: a plan nobody showed this hazard to cannot acquire
    authority over it, and a merely `blocked` plan least of all."""
    from localplane.backend.runs import RunRefused

    with database.transaction():
        _seed(database, eligibility="blocked", self_impact=ELIGIBLE_DOCUMENT)
    runs, changes = _services(database)

    with pytest.raises(RunRefused) as raised:
        changes.grant_self_impact_override(
            runs.get("run_si"), preview_id="prv_si", acknowledge=True,
            expected_preview_digest=None, management_path=_unknown_path())
    assert raised.value.code == "self_impact_override_not_applicable"
    assert database.query("SELECT * FROM run_confirmations") == []


@pytest.mark.parametrize(
    "kwargs, code",
    [
        ({"preview_id": "prv_legacy"}, "confirmation_preview_mismatch"),
        ({"expected_preview_digest": "sha256:other"}, "preview_digest_mismatch"),
        ({"acknowledge": False}, "confirmation_not_acknowledged"),
    ],
)
def test_a_grant_names_the_exact_preview_and_digest_or_is_refused(database, kwargs, code):
    """Bound to one immutable document, and nothing is recorded when it is not this one."""
    from localplane.backend.runs import RunRefused

    with database.transaction():
        _seed(database, eligibility="self_impact_override_required",
              self_impact=ELIGIBLE_DOCUMENT)
    runs, changes = _services(database)
    body = {"preview_id": "prv_si", "acknowledge": True,
            "expected_preview_digest": None, "management_path": _unknown_path()}
    body.update(kwargs)

    with pytest.raises(RunRefused) as raised:
        changes.grant_self_impact_override(runs.get("run_si"), **body)
    assert raised.value.code == code
    assert database.query("SELECT * FROM run_confirmations") == []


def test_a_grant_is_still_refused_when_the_plan_no_longer_holds(database):
    """Re-derived against this request's evidence, exactly as an apply re-derives its gates.

    The seeded plan cannot be re-planned at all here, which is the strongest form of "no
    longer holds": authority is not granted over a document current truth cannot reproduce.
    """
    from localplane.backend.runs import RunRefused

    with database.transaction():
        _seed(database, eligibility="self_impact_override_required",
              self_impact=ELIGIBLE_DOCUMENT)
    runs, changes = _services(database)

    with pytest.raises(RunRefused) as raised:
        changes.grant_self_impact_override(
            runs.get("run_si"), preview_id="prv_si", acknowledge=True,
            expected_preview_digest="sha256:si", management_path=_unknown_path())
    assert raised.value.code == "preview_stale"
    assert database.query("SELECT * FROM run_confirmations") == []


def test_spending_an_override_that_was_never_granted_is_refused(database):
    """The apply path asks for it by purpose, and an apply confirmation is not it."""
    from localplane.backend.runs import RunRefused

    with database.transaction():
        _seed(database, eligibility="self_impact_override_required",
              self_impact=ELIGIBLE_DOCUMENT)
        _grant(database, purpose=CONFIRMATION_PURPOSE_APPLY, consumed=False)
    runs, changes = _services(database)

    with pytest.raises(RunRefused) as raised:
        with database.transaction():
            changes._consume_self_impact_override(runs.get("run_si"), "att_1")
    assert raised.value.code == "self_impact_override_required"


def test_spending_an_override_twice_is_refused(database):
    """Single-use in the service as well as in the store, and by the same guard the
    confirmation uses: the UPDATE carries its own `consumed_at IS NULL` condition."""
    from localplane.backend.runs import RunRefused

    with database.transaction():
        _seed(database, eligibility="self_impact_override_required",
              self_impact=ELIGIBLE_DOCUMENT)
        _grant(database, purpose=CONFIRMATION_PURPOSE_SELF_IMPACT_OVERRIDE, consumed=False,
               confirmation_id="cnf_ov")
    runs, changes = _services(database)

    with database.transaction():
        changes._consume_self_impact_override(runs.get("run_si"), "att_1")
    spent = database.query_one("SELECT * FROM run_confirmations WHERE confirmation_id='cnf_ov'")
    assert spent["consumed_by_attempt_id"] == "att_1"

    with pytest.raises(RunRefused) as raised:
        with database.transaction():
            changes._consume_self_impact_override(runs.get("run_si"), "att_2")
    assert raised.value.code == "self_impact_override_already_consumed"


def test_an_override_granted_against_another_digest_cannot_be_spent(database):
    """What was authorised has to be what is about to run."""
    from localplane.backend.runs import RunRefused

    with database.transaction():
        _seed(database, eligibility="self_impact_override_required",
              self_impact=ELIGIBLE_DOCUMENT)
        _grant(database, purpose=CONFIRMATION_PURPOSE_SELF_IMPACT_OVERRIDE, consumed=False,
               confirmation_id="cnf_ov", preview_digest="sha256:something_else")
    runs, changes = _services(database)

    with pytest.raises(RunRefused) as raised:
        with database.transaction():
            changes._consume_self_impact_override(runs.get("run_si"), "att_1")
    assert raised.value.code == "preview_digest_mismatch"


def test_an_ordinary_run_cannot_be_given_one_through_the_normal_confirmation(ready):
    """Confirming records an `apply` row and only ever an `apply` row."""
    run = ready.plan("eth0").run
    ready.confirm(run)

    purposes = [r["purpose"] for r in ready.rows("run_confirmations")]
    assert purposes == [CONFIRMATION_PURPOSE_APPLY]
    assert ready.changes.self_impact_override_for(run.run_id) is None


def test_an_ordinary_run_is_refused_an_override_it_was_never_offered(ready):
    """The estate's MTU plan is ordinarily eligible, so there is no hazard to accept."""
    from localplane.backend.runs import RunRefused

    run = ready.plan("eth0").run
    with pytest.raises(RunRefused) as raised:
        ready.changes.grant_self_impact_override(
            run, preview_id=run.preview.preview_id, acknowledge=True,
            expected_preview_digest=None, management_path=ready.management_path)
    assert raised.value.code == "self_impact_override_not_applicable"
    assert ready.rows("run_confirmations") == []


def test_the_digest_of_an_eligible_plan_differs_from_the_same_plan_blocked():
    """Eligibility is in the canonical document at every version, so opening the path
    changes the plan's identity — an authority granted against one cannot be read as
    granted against the other."""
    open_path = _execution()
    blocked = _execution(available=False)
    assert open_path.eligibility is not blocked.eligibility

    plan = OPERATIONS[OperationType.SYSTEMD_SERVICE_RESTART].plan(
        _planning(SystemdServiceAction.RESTART, lifecycle=_hosted())
    )
    assert plan_digest(replace(plan, execution=open_path)) != plan_digest(
        replace(plan, execution=blocked)
    )
