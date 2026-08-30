"""The backend self-impact derivation: honest about the hazard, and authorising nothing.

LocalPlane may run inside Docker, so a lifecycle operation may legitimately target the
infrastructure hosting LocalPlane's own backend. LocalPlane derives and publishes what that
means for one plan. It does not create authority, and these tests are mostly about what it
still refuses:

* **protection never moves.** A plan carrying a self-impact section is exactly as protected
  as the same plan without one, and `override_eligible` can never be true where protection
  is not `protected`;
* **one topology is eligible and it is spelled out.** Proven `docker-direct-unix-v1`,
  nothing else in the closure from the management path, an agent proven to be a normal
  host-side `.service` outside it, and no gap anywhere;
* **`possible` is surfaced and never authorised**, which is this build's deliberate
  narrowing rather than an oversight;
* **anything unrecognised is ineligible**, because eligibility is built as a list of
  refusals rather than a chain of permissions.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from localplane.backend.domain.protection import ProtectionReason, ProtectionStatus
from localplane.backend.domain.runs import (
    PLAN_DIGEST_VERSION,
    ExecutionEligibility,
    OperationType,
    PlanRefused,
    canonical_plan,
    plan_digest,
)
from localplane.backend.domain.self_impact import (
    SELF_IMPACT_SUBJECT_BACKEND_RUNTIME,
    BackendSelfImpactAssessment,
    SelfImpactOutage,
    SelfImpactStatus,
    assess_backend_self_impact,
)
from localplane.backend.domain.systemd_lifecycle import (
    RuntimeOwnerStatus,
    SystemdServiceAction,
    assess_lifecycle_protection,
)
from localplane.backend.operations import OPERATIONS
from localplane.protocol.docker_runtime_owner import (
    DOCKER_DIRECT_UNIX_CONTRACT,
    RUNTIME_OWNER_UMBRELLA_GAP,
    DockerRuntimeOwnerGap,
)
from tests.test_systemd_lifecycle import _context, _facts, _planning, _runtime_owner


DOCKER_ENGINE = "docker-engine.service"
BACKEND_SCOPE = "backend.scope"
AGENT = "agent-holder.service"


def _hosted(
    action: SystemdServiceAction = SystemdServiceAction.RESTART,
    *,
    owner_in_closure: bool = True,
    **overrides,
):
    """The exact supported topology: backend in Docker, agent a host service.

    The effect closure of acting on the Engine's unit contains that unit and nothing else
    from the management path; the container scope is deliberately *not* in it, because
    systemd publishes no edge from a runtime daemon to the scopes it owns — which is the
    whole reason the runtime-owner correlation had to exist.
    """
    units = (
        ("target.service", DOCKER_ENGINE) if owner_in_closure else ("target.service",)
    )
    values = {
        "units": units,
        "management": (BACKEND_SCOPE, DOCKER_ENGINE),
        "agent": AGENT,
        "agent_unit_type": "service",
        "connection_unit": BACKEND_SCOPE,
        "connection_unit_type": "scope",
        "runtime_owner": _runtime_owner(),
        "facts": _facts(active="active"),
    }
    values.update(overrides)
    return _context(action, **values)


def _assess(context):
    return assess_backend_self_impact(context, assess_lifecycle_protection(context))


# --------------------------------------------------------------- what the status means


def test_the_supported_docker_topology_is_proven_and_names_the_unit_at_risk():
    """The whole chain, end to end: this connection reaches a backend in a container whose
    Engine is owned by a unit this operation's closure contains."""
    assessment = _assess(_hosted())

    assert assessment.subject == SELF_IMPACT_SUBJECT_BACKEND_RUNTIME
    assert assessment.status is SelfImpactStatus.PROVEN
    assert assessment.detail == "effect_closure_contains_proven_backend_runtime_owner"
    assert assessment.owner_unit_id == DOCKER_ENGINE
    assert assessment.envelope == DOCKER_DIRECT_UNIX_CONTRACT
    assert assessment.override_eligible is True
    assert assessment.reasons == ()


def test_a_proven_owner_outside_the_closure_is_not_detected_rather_than_unknown():
    """`not_detected` is earned. The owner is proven and this closure provably misses it."""
    assessment = _assess(_hosted(owner_in_closure=False))

    assert assessment.status is SelfImpactStatus.NOT_DETECTED
    assert assessment.outage is SelfImpactOutage.NONE
    assert assessment.override_eligible is False
    assert "self_impact_status_not_proven:not_detected" in assessment.reasons


def test_an_unproven_container_runtime_owner_is_possible_and_never_authorised():
    """The narrowing this build made on purpose.

    The backend runs in a container and which unit owns that runtime was not established,
    so LocalPlane cannot rule out that this operation takes it away. That is reported —
    and it is not offered a way around, because deciding which incomplete correlations are
    safe enough to authorise past is a question with no evidence behind it yet.
    """
    context = _hosted(
        runtime_owner=_runtime_owner(
            status=RuntimeOwnerStatus.INCOMPLETE,
            attestation_fingerprint=None,
            endpoint=None,
            container_id=None,
            container_started_at=None,
            engine_id=None,
            direct_transport_verified=False,
            peer_service_main_verified=False,
            owner_unit_id=None,
            owner_invocation_id=None,
            execution_cgroup_relation=None,
            gaps=(str(DockerRuntimeOwnerGap.ATTESTATION_UNAVAILABLE),),
        ),
        complete=False,
        gaps=(
            RUNTIME_OWNER_UMBRELLA_GAP,
            str(DockerRuntimeOwnerGap.ATTESTATION_UNAVAILABLE),
        ),
    )
    assessment = _assess(context)

    assert assessment.status is SelfImpactStatus.POSSIBLE
    assert assessment.detail == "backend_container_runtime_owner_unproven"
    assert assessment.owner_unit_id is None
    assert assessment.envelope is None
    assert assessment.override_eligible is False
    assert "self_impact_status_not_proven:possible" in assessment.reasons


def test_nothing_established_about_the_backend_is_unresolved_not_a_negative():
    """The default answer is "we did not establish it", never "it is fine"."""
    assessment = _assess(_context(SystemdServiceAction.RESTART))

    assert assessment.status is SelfImpactStatus.UNRESOLVED
    assert assessment.detail == "backend_runtime_containment_not_established"
    assert assessment.override_eligible is False


def test_a_backend_running_as_a_host_service_is_assessed_from_the_effect_graph():
    """No container in sight: the backend's own unit is in the graph and answers directly.

    Proven self-impact, and still not eligible — the one envelope an authority could cover
    is the Docker one, and a host-service backend is a different situation this build does
    not offer a way around.
    """
    inside = _assess(
        _context(
            SystemdServiceAction.STOP,
            units=("target.service", "backend.service"),
            management=("backend.service",),
            connection_unit="backend.service",
            connection_unit_type="service",
            agent_unit_type="service",
        )
    )
    assert inside.status is SelfImpactStatus.PROVEN
    assert inside.detail == "effect_closure_contains_backend_service_unit"
    assert inside.owner_unit_id == "backend.service"
    assert inside.override_eligible is False
    assert "runtime_owner_outside_supported_self_impact_envelope" in inside.reasons

    outside = _assess(
        _context(
            SystemdServiceAction.STOP,
            connection_unit="backend.service",
            connection_unit_type="service",
            agent_unit_type="service",
        )
    )
    assert outside.status is SelfImpactStatus.NOT_DETECTED


@pytest.mark.parametrize(
    "action, outage",
    [
        (SystemdServiceAction.RESTART, SelfImpactOutage.TEMPORARY_POSSIBLE),
        (SystemdServiceAction.STOP, SelfImpactOutage.INDEFINITE_POSSIBLE),
    ],
)
def test_restart_and_stop_describe_different_outages(action, outage):
    """A restart may interrupt LocalPlane and it may come back; a stop may simply end it.

    Neither promises a return: nothing here has verified a second path to this host.
    """
    facts = _facts(active="active")
    assert _assess(_hosted(action, facts=facts)).outage is outage


# ------------------------------------------------------------- what protection never does


def test_the_assessment_moves_no_protection_verdict_in_either_direction():
    """The one rule this derivation exists under: `protected` and `unknown` stay."""
    for context in (_hosted(), _hosted(owner_in_closure=False), _context(SystemdServiceAction.STOP)):
        before = assess_lifecycle_protection(context)
        assessment = assess_backend_self_impact(context, before)
        after = assess_lifecycle_protection(context)
        assert before == after
        assert assessment is not before  # a separate document, not a mutation


def test_an_eligible_plan_is_still_protected_and_still_blocked_for_an_ordinary_apply():
    """Eligibility opens one path. It softens no verdict and removes no blocker.

    The plan is protected, says exactly why, and publishes that blocker unchanged. What the
    derivation adds is that this one hazard has a second route — which an operator has to
    open deliberately, with an authority separate from the confirmation the plan also
    requires. Ordinary execution remains blocked and always will.
    """
    plan = OPERATIONS[OperationType.SYSTEMD_SERVICE_RESTART].plan(
        _planning(SystemdServiceAction.RESTART, lifecycle=_hosted())
    )
    assert not isinstance(plan, PlanRefused)
    assert plan.self_impact.override_eligible is True
    assert plan.protection.status is ProtectionStatus.PROTECTED
    assert plan.protection.reasons == (ProtectionReason.MANAGEMENT_PATH,)
    assert plan.execution.blockers == ("protected:management_path",)
    assert plan.execution.eligibility is ExecutionEligibility.SELF_IMPACT_OVERRIDE_REQUIRED
    assert plan.execution.eligibility is not ExecutionEligibility.ELIGIBLE


def test_eligibility_is_impossible_wherever_protection_is_not_protected():
    """A `clear` or `unknown` verdict can never carry this authority's shape."""
    for context in (
        _hosted(owner_in_closure=False),
        _hosted(complete=False, gaps=("systemd.effect_graph",)),
    ):
        assessment = _assess(context)
        protection = assess_lifecycle_protection(context)
        if protection.status is not ProtectionStatus.PROTECTED:
            assert assessment.override_eligible is False


# -------------------------------------------------------------- what is never overrideable


@pytest.mark.parametrize(
    "overrides, reason",
    [
        pytest.param(
            {"agent_unit_type": "scope"},
            "localplane_agent_not_proven_outside_closure_as_host_service",
            id="agent_in_a_container_scope"),
        pytest.param(
            {"agent_unit_type": "slice"},
            "localplane_agent_not_proven_outside_closure_as_host_service",
            id="agent_in_a_slice"),
        pytest.param(
            {"agent_unit_type": None},
            "localplane_agent_not_proven_outside_closure_as_host_service",
            id="agent_containment_type_unknown"),
        pytest.param(
            {"agent": None, "agent_unit_type": "service", "complete": False,
             "gaps": ("localplane_agent.containing_unit",)},
            "localplane_agent_not_proven_outside_closure_as_host_service",
            id="agent_unresolved"),
        pytest.param(
            {"units": ("target.service", DOCKER_ENGINE, AGENT)},
            "localplane_agent_not_proven_outside_closure_as_host_service",
            id="agent_inside_the_effect_closure"),
        pytest.param(
            {"units": ("target.service", DOCKER_ENGINE, "NetworkManager.service"),
             "management": (BACKEND_SCOPE, DOCKER_ENGINE, "NetworkManager.service")},
            "effect_closure_touches_management_path_beyond_backend_runtime",
            id="closure_reaches_another_management_unit"),
        pytest.param(
            {"complete": False, "gaps": ("systemd.effect_graph",)},
            "lifecycle_context_incomplete",
            id="incomplete_effect_graph"),
        pytest.param(
            {"gaps": ("management_path.provider_owner:tailscale",)},
            "lifecycle_context_carries_unresolved_gaps",
            id="provider_owner_uncertainty"),
        pytest.param(
            {"gaps": ("something.nobody.has.implemented.yet",)},
            "lifecycle_context_carries_unresolved_gaps",
            id="an_unknown_future_gap"),
        pytest.param(
            {"runtime_owner": None},
            "runtime_owner_outside_supported_self_impact_envelope",
            id="no_runtime_owner_correlation"),
    ],
)
def test_only_the_exact_supported_hazard_is_ever_overrideable(overrides, reason):
    """Every one of these is a hazard an authority about LocalPlane disappearing does not
    speak to, and every one of them comes out ineligible with a typed reason."""
    assessment = _assess(_hosted(**overrides))

    assert assessment.override_eligible is False
    assert reason in assessment.reasons
    assert assessment.envelope is None


def test_a_runtime_owner_from_an_unknown_contract_cannot_be_constructed_or_trusted():
    """The envelope is a version, not a shape. An unknown one is refused at construction,
    which is what keeps a future correlation method from inheriting this authority."""
    with pytest.raises(ValueError):
        _runtime_owner(contract_version="docker-direct-unix-v2")
    with pytest.raises(ValueError):
        _runtime_owner(method_version=2)


def test_the_agent_reason_and_the_context_facts_must_agree():
    """Context facts alone are not enough: the protection roll-up has to call it clear too."""
    context = _hosted()
    protection = assess_lifecycle_protection(context)
    agent_unknown = replace(
        protection,
        assessed=tuple(
            replace(entry, status=ProtectionStatus.UNKNOWN)
            if entry.reason is ProtectionReason.LOCALPLANE_AGENT
            else entry
            for entry in protection.assessed
        ),
    )
    assessment = assess_backend_self_impact(context, agent_unknown)

    assert assessment.override_eligible is False
    assert "localplane_agent_protection_reason_not_clear" in assessment.reasons


# ----------------------------------------------------------------------------- identity


def test_digest_v6_binds_the_assessment_while_v5_stays_recomputable():
    """A plan that may interrupt LocalPlane and one that provably does not are different
    documents to review — under version 6, which is where the section exists."""
    assert PLAN_DIGEST_VERSION == 6
    proven = OPERATIONS[OperationType.SYSTEMD_SERVICE_RESTART].plan(
        _planning(SystemdServiceAction.RESTART, lifecycle=_hosted())
    )
    clear = OPERATIONS[OperationType.SYSTEMD_SERVICE_RESTART].plan(
        _planning(
            SystemdServiceAction.RESTART, lifecycle=_hosted(owner_in_closure=False)
        )
    )

    assert plan_digest(proven, 6) != plan_digest(clear, 6)
    assert canonical_plan(proven, 5).get("self_impact") is None
    assert canonical_plan(proven, 6)["self_impact"]["status"] == "proven"
    assert canonical_plan(proven, 6)["self_impact"]["override_eligible"] is True

    # The eligibility itself is part of the identity: a plan whose answer to "could this be
    # authorised" changed is not the same document, whatever else stayed the same.
    ineligible = replace(
        proven, self_impact=replace(proven.self_impact, override_eligible=False)
    )
    assert plan_digest(ineligible, 6) != plan_digest(proven, 6)
    assert plan_digest(ineligible, 5) == plan_digest(proven, 5)


def test_v6_binds_which_unit_holds_the_backend_and_which_holds_the_agent():
    """The facts the derivation rests on are hashed, not merely reported.

    Both are already inside `management_units` and `agent_unit`; what version 6 adds is
    *which member is which* and what kind of containment each is — and a safety judgement
    resting on a fact the digest could not see move would be one nothing detects going bad.
    """
    plan = OPERATIONS[OperationType.SYSTEMD_SERVICE_RESTART].plan(
        _planning(SystemdServiceAction.RESTART, lifecycle=_hosted())
    )
    for field in ("connection_unit", "connection_unit_type", "agent_unit_type"):
        moved = replace(
            plan, lifecycle_context=replace(plan.lifecycle_context, **{field: "moved"})
        )
        assert plan_digest(moved, 6) != plan_digest(plan, 6), field
        assert plan_digest(moved, 5) == plan_digest(plan, 5), field

    context = canonical_plan(plan, 6)["lifecycle_context"]
    assert context["connection_unit"] == BACKEND_SCOPE
    assert context["connection_unit_type"] == "scope"
    assert context["agent_unit_type"] == "service"
    assert canonical_plan(plan, 5)["lifecycle_context"].get("connection_unit") is None


def test_the_assessment_round_trips_through_its_persisted_shape():
    assessment = _assess(_hosted())
    assert BackendSelfImpactAssessment.from_dict(assessment.as_dict()) == assessment

    with pytest.raises(ValueError):
        BackendSelfImpactAssessment.from_dict(
            {**assessment.as_dict(), "override_eligible": "yes"}
        )
    with pytest.raises(ValueError):
        BackendSelfImpactAssessment.from_dict(
            {**assessment.as_dict(), "reasons": "not_a_list"}
        )


# -------------------------------------------------------------------- what the store refuses


def _preview_row(database, **overrides) -> None:
    row = dict(database.query("SELECT * FROM run_previews")[0])
    row.update(overrides)
    columns = list(row)
    database.connection.execute(
        f"INSERT INTO run_previews ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        tuple(row[c] for c in columns),
    )


def test_the_store_refuses_a_current_systemd_preview_with_no_assessment(database):
    """A version 6 systemd plan without the derivation is not a document this build makes,
    and the store says so rather than trusting the code that writes it."""
    from tests.test_runs import _seed_a_published_plan

    with database.transaction():
        _seed_a_published_plan(database)

    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction():
            _preview_row(
                database,
                preview_id="prv_systemd_no_self_impact",
                operation="systemd.service.restart",
                digest_version=6,
                change_kind="action",
                action="restart",
                observed_state="active",
                expected_state="active_with_new_invocation_id",
                field=None, value_type=None, current_value=None, desired_value=None,
                intent_id=None, intent_version=None, intent_capability=None,
                intent_provider=None, drift_finding_id=None,
                authorization_assessment="{}", lifecycle_context="{}", self_impact=None,
            )


def test_the_store_refuses_an_assessment_on_a_plan_that_is_not_a_systemd_action(database):
    """Nothing else has a backend runtime to be assessed against."""
    from tests.test_runs import _seed_a_published_plan

    with database.transaction():
        _seed_a_published_plan(database)

    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction():
            _preview_row(
                database, preview_id="prv_mtu_with_self_impact", self_impact="{}"
            )
