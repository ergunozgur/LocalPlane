-- Slice 11B Part B / step 2 of the self-impact vertical: one narrowly typed authority.
--
-- Step 1 derived whether a plan could interrupt the LocalPlane backend publishing it and
-- whether that exact hazard is the shape an authority could cover.  This adds the authority
-- itself, and it adds **no new place to keep truth**.  The self-impact document is already
-- frozen in `run_previews` and bound by digest version 6; a grant that copied its status,
-- subject, outage or owner unit into columns of its own would be a second copy of a fact
-- that cannot disagree with the first while it is immutable, and the first time they did
-- disagree nothing could say which was right.
--
-- So the grant is a `run_confirmations` row with a third purpose.  Every binding it needs
-- already exists on that table and is already enforced there: the run, the preview its run
-- published, the digest and digest version that preview carries, and single-use consumption
-- with the attempt that spent it.  What is added is the purpose value, one partial unique
-- index so authority cannot accumulate, and one trigger on `changes`.
--
-- **The two authorities are separate and neither substitutes for the other.**  Two triggers
-- now guard the boundary independently: `changes_require_a_consumed_confirmation` has always
-- demanded a consumed `apply` row, and `changes_requiring_a_self_impact_override_have_one`
-- demands a consumed `self_impact_override` row from any Change whose published preview says
-- the override is what its execution rests on.  Both fire on the same INSERT.
--
-- `run_previews` gains the fourth execution eligibility and one CHECK tying it to the
-- derivation: a preview may say the override is required only where the document it stored
-- says the override is possible.  The store refuses an eligibility its own evidence does not
-- support rather than trusting the code that wrote both.
--
-- No executor, no dispatch and no systemd write exists in this migration or this build.
--
-- localplane:foreign-keys=off

DROP TRIGGER run_previews_are_immutable;
DROP TRIGGER runs_intent_belongs_to_object;
DROP TRIGGER runs_observation_belongs_to_object;
DROP TRIGGER runs_protection_evidence_belongs_to_host;
DROP TRIGGER run_confirmations_match_the_runs_preview;
DROP TRIGGER run_confirmations_are_consumed_once;
DROP TRIGGER run_guards_require_the_typed_authority;
DROP TRIGGER changes_require_a_consumed_confirmation;
DROP TRIGGER recovery_mutation_names_the_authority_spent_on_it_on_insert;
DROP TRIGGER recovery_mutation_names_the_authority_spent_on_it_on_update;

------------------------------------------------------------------------------------------
-- 1 — `run_previews`: a fourth eligibility, and it has to be earned
------------------------------------------------------------------------------------------

CREATE TABLE run_previews_rebuilt (
    preview_id     TEXT    NOT NULL PRIMARY KEY,
    preview_digest TEXT    NOT NULL,
    digest_version INTEGER NOT NULL CHECK (digest_version >= 1),
    operation      TEXT    NOT NULL CHECK (operation IN (
        'network.interface.reconcile_mtu',
        'docker.container.start', 'docker.container.stop', 'docker.container.restart',
        'systemd.service.start', 'systemd.service.stop', 'systemd.service.restart'
    )),

    change_kind    TEXT    NOT NULL CHECK (change_kind IN ('field', 'action')),
    field          TEXT,
    value_type     TEXT CHECK (value_type IS NULL OR value_type IN ('boolean', 'integer')),
    current_value  INTEGER,
    desired_value  INTEGER,
    action         TEXT,
    observed_state TEXT,
    expected_state TEXT,

    intent_id         TEXT REFERENCES intents(intent_id),
    intent_version    INTEGER CHECK (intent_version IS NULL OR intent_version >= 1),
    intent_capability TEXT,
    intent_provider   TEXT,

    observation_id   TEXT NOT NULL REFERENCES observations(observation_id),
    sweep_id         TEXT NOT NULL REFERENCES observation_sweeps(sweep_id),
    observed_at      TEXT NOT NULL,
    drift_finding_id TEXT REFERENCES findings(finding_id),

    ownership_state     TEXT NOT NULL,
    ownership_reason    TEXT NOT NULL,
    ownership_claims    TEXT NOT NULL DEFAULT '[]',
    ownership_gaps      TEXT NOT NULL DEFAULT '[]',
    provider_readings   TEXT NOT NULL DEFAULT '{}',

    protection_status TEXT NOT NULL
        CHECK (protection_status IN ('protected', 'clear', 'unknown')),
    protection_reasons    TEXT NOT NULL DEFAULT '[]',
    protection_unresolved TEXT NOT NULL DEFAULT '[]',
    protection_management_path TEXT NOT NULL
        CHECK (protection_management_path IN
               ('on_management_path', 'not_on_management_path', 'unknown')),
    protection_reason           TEXT NOT NULL,
    protection_missing_evidence TEXT NOT NULL DEFAULT '[]',
    protection_evidence_id TEXT REFERENCES management_path_observations(observation_id),
    protection_evidence_observed_at TEXT,
    protection_assessments TEXT NOT NULL DEFAULT '[]',

    authorization_assessment TEXT,
    lifecycle_context TEXT,
    self_impact TEXT,

    risk_tier    TEXT NOT NULL CHECK (risk_tier IN ('low', 'medium', 'high')),
    risk_factors TEXT NOT NULL DEFAULT '[]',

    confirmation_required     INTEGER NOT NULL CHECK (confirmation_required IN (0, 1)),
    confirmation_method       TEXT NOT NULL
        CHECK (confirmation_method IN ('none', 'acknowledge', 'typed')),
    confirmation_source       TEXT NOT NULL CHECK (confirmation_source IN ('policy', 'operation')),
    confirmation_reasons      TEXT NOT NULL DEFAULT '[]',
    confirmation_policy       TEXT NOT NULL,
    confirmation_token_issued INTEGER NOT NULL CHECK (confirmation_token_issued = 0),

    execution_availability TEXT NOT NULL
        CHECK (execution_availability IN ('available', 'unavailable', 'not_implemented')),
    execution_eligibility  TEXT NOT NULL
        CHECK (execution_eligibility IN (
            'eligible', 'guarded', 'blocked', 'self_impact_override_required'
        )),
    execution_blockers     TEXT NOT NULL DEFAULT '[]',
    execution_provider     TEXT,
    required_capability    TEXT NOT NULL,
    capability_declared    INTEGER NOT NULL CHECK (capability_declared IN (0, 1)),

    guard_availability  TEXT NOT NULL DEFAULT 'unavailable'
        CHECK (guard_availability IN ('available', 'unavailable')),
    guard_reason        TEXT NOT NULL DEFAULT 'guard_not_required',
    guard_window_s      INTEGER NOT NULL DEFAULT 0 CHECK (guard_window_s >= 0),
    guard_prerequisites TEXT NOT NULL DEFAULT '[]',
    guard_unmet         TEXT NOT NULL DEFAULT '[]',
    guard_guarantee     TEXT NOT NULL DEFAULT '',
    guard_armed         INTEGER NOT NULL DEFAULT 0 CHECK (guard_armed = 0),

    recovery_mode              TEXT NOT NULL
        CHECK (recovery_mode IN ('auto', 'operator', 'none')),
    recovery_rollback_possible INTEGER NOT NULL CHECK (recovery_rollback_possible IN (0, 1)),
    recovery_armed             INTEGER NOT NULL CHECK (recovery_armed = 0),
    recovery_guarantee         TEXT NOT NULL,
    recovery_reason            TEXT NOT NULL,

    verification_capability TEXT NOT NULL,
    verification_provider   TEXT NOT NULL,
    verification_condition  TEXT NOT NULL,
    verification_executed   INTEGER NOT NULL CHECK (verification_executed = 0),
    published_at TEXT NOT NULL,

    CHECK (change_kind <> 'field' OR (
        field IS NOT NULL AND value_type IS NOT NULL
        AND current_value IS NOT NULL AND desired_value IS NOT NULL
        AND current_value <> desired_value
        AND (value_type <> 'boolean' OR (current_value IN (0, 1) AND desired_value IN (0, 1)))
        AND intent_id IS NOT NULL AND intent_version IS NOT NULL
        AND intent_capability IS NOT NULL AND intent_provider IS NOT NULL
        AND action IS NULL AND observed_state IS NULL AND expected_state IS NULL
    )),
    CHECK (change_kind <> 'action' OR (
        action IS NOT NULL AND observed_state IS NOT NULL AND expected_state IS NOT NULL
        AND field IS NULL AND value_type IS NULL
        AND current_value IS NULL AND desired_value IS NULL
        AND intent_id IS NULL AND intent_version IS NULL
        AND intent_capability IS NULL AND intent_provider IS NULL
        AND drift_finding_id IS NULL
    )),
    CHECK (protection_evidence_id IS NULL OR protection_evidence_observed_at IS NOT NULL),
    CHECK (execution_eligibility <> 'guarded' OR (
        guard_availability = 'available'
        AND protection_management_path = 'on_management_path'
    )),
    CHECK (guard_availability <> 'available'
           OR protection_management_path = 'on_management_path'),
    CHECK (guard_availability <> 'available' OR guard_window_s > 0),
    CHECK (
        operation NOT LIKE 'systemd.service.%'
        OR (change_kind = 'action' AND action IN ('start', 'stop', 'restart')
            AND authorization_assessment IS NOT NULL AND lifecycle_context IS NOT NULL)
    ),
    -- A preview carries the derivation exactly when its canonical form has somewhere to put
    -- it. Version 6 is where the self-impact section enters the document, so every systemd
    -- plan published under it must have one, and a plan published under an earlier form
    -- never had one and does not acquire one by being migrated past.
    CHECK (
        digest_version < 6
        OR operation NOT LIKE 'systemd.service.%'
        OR self_impact IS NOT NULL
    ),
    CHECK (
        operation LIKE 'systemd.service.%'
        OR (authorization_assessment IS NULL AND lifecycle_context IS NULL
            AND self_impact IS NULL)
    ),
    CHECK (operation <> 'systemd.service.start' OR action = 'start'),
    CHECK (operation <> 'systemd.service.stop' OR action = 'stop'),
    CHECK (operation <> 'systemd.service.restart' OR action = 'restart'),
    -- The fourth eligibility has to be earned by the plan's own stored derivation. A preview
    -- may say the override is what its execution rests on only where the document it froze
    -- says the override is possible at all — so the store refuses an eligibility its own
    -- evidence does not support, rather than trusting the code that wrote both halves.
    -- `possible` and `unresolved` derivations carry `override_eligible: false` and are
    -- refused here by the same clause that refuses a plan with no derivation at all.
    CHECK (
        execution_eligibility <> 'self_impact_override_required'
        OR (
            operation LIKE 'systemd.service.%'
            AND digest_version >= 6
            AND self_impact IS NOT NULL
            AND json_extract(self_impact, '$.override_eligible') = 1
        )
    )
) STRICT;

INSERT INTO run_previews_rebuilt (
    preview_id, preview_digest, digest_version, operation, change_kind,
    field, value_type, current_value, desired_value, action, observed_state, expected_state,
    intent_id, intent_version, intent_capability, intent_provider,
    observation_id, sweep_id, observed_at, drift_finding_id,
    ownership_state, ownership_reason, ownership_claims, ownership_gaps, provider_readings,
    protection_status, protection_reasons, protection_unresolved, protection_management_path,
    protection_reason, protection_missing_evidence, protection_evidence_id,
    protection_evidence_observed_at, protection_assessments,
    authorization_assessment, lifecycle_context, self_impact,
    risk_tier, risk_factors,
    confirmation_required, confirmation_method, confirmation_source, confirmation_reasons,
    confirmation_policy, confirmation_token_issued,
    execution_availability, execution_eligibility, execution_blockers, execution_provider,
    required_capability, capability_declared,
    guard_availability, guard_reason, guard_window_s, guard_prerequisites, guard_unmet,
    guard_guarantee, guard_armed,
    recovery_mode, recovery_rollback_possible, recovery_armed, recovery_guarantee,
    recovery_reason, verification_capability, verification_provider,
    verification_condition, verification_executed, published_at
)
SELECT
    preview_id, preview_digest, digest_version, operation, change_kind,
    field, value_type, current_value, desired_value, action, observed_state, expected_state,
    intent_id, intent_version, intent_capability, intent_provider,
    observation_id, sweep_id, observed_at, drift_finding_id,
    ownership_state, ownership_reason, ownership_claims, ownership_gaps, provider_readings,
    protection_status, protection_reasons, protection_unresolved, protection_management_path,
    protection_reason, protection_missing_evidence, protection_evidence_id,
    protection_evidence_observed_at, protection_assessments,
    authorization_assessment, lifecycle_context, self_impact,
    risk_tier, risk_factors,
    confirmation_required, confirmation_method, confirmation_source, confirmation_reasons,
    confirmation_policy, confirmation_token_issued,
    execution_availability, execution_eligibility, execution_blockers, execution_provider,
    required_capability, capability_declared,
    guard_availability, guard_reason, guard_window_s, guard_prerequisites, guard_unmet,
    guard_guarantee, guard_armed,
    recovery_mode, recovery_rollback_possible, recovery_armed, recovery_guarantee,
    recovery_reason, verification_capability, verification_provider,
    verification_condition, verification_executed, published_at
FROM run_previews;

DROP TABLE run_previews;
ALTER TABLE run_previews_rebuilt RENAME TO run_previews;

CREATE TRIGGER run_previews_are_immutable
BEFORE UPDATE ON run_previews
BEGIN
    SELECT RAISE(ABORT, 'a published preview is immutable; plan again instead of rewriting');
END;

------------------------------------------------------------------------------------------
-- 2 — `run_confirmations`: a third purpose, and not one new column
------------------------------------------------------------------------------------------
--
-- Everything this authority has to be bound to is already a column here and already
-- enforced here. What it is for is the purpose; which Run it belongs to is `run_id`; which
-- immutable document it was granted against is `preview_id`, checked by trigger against the
-- preview that Run actually published; what that document said is `preview_digest` and
-- `digest_version`; and that it may be spent once is `consumed_at` plus the trigger that
-- refuses a second consumption.
--
-- The self-impact status, subject, outage and owner unit are deliberately *not* copied. They
-- are frozen in `run_previews.self_impact`, they are bound into `preview_digest` by digest
-- version 6, and the preview is immutable — so a copy here could only ever agree, until the
-- day it did not and nothing could say which was right.
--
-- The method is `acknowledge`. The typed method belongs to a guarded apply, where what an
-- operator writes is evidence about *which object* they looked at; here the object, the
-- hazard and the outage are all in a document this row names by digest, and asking a client
-- to type any of them back would turn copied display text into authority.

CREATE TABLE run_confirmations_rebuilt (
    confirmation_id TEXT NOT NULL PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(run_id),

    -- What this authority is for. An apply confirmation authorises crossing the write
    -- boundary once; a recovery confirmation authorises one recovery retry to dispatch a new
    -- mutation for a Change that already crossed it and could not prove what it left behind;
    -- a self-impact override answers the one further question an apply confirmation does not
    -- ask, which is whether the operator accepts that this operation may take LocalPlane
    -- itself away. They are the same act — a person saying yes to a write that may happen —
    -- against different documents and different hazards, and a column saying which is what
    -- keeps one from being read as the other.
    purpose         TEXT NOT NULL
        CHECK (purpose IN ('apply', 'recovery_retry', 'self_impact_override')),

    preview_id      TEXT NOT NULL REFERENCES run_previews(preview_id),
    preview_digest  TEXT NOT NULL,
    digest_version  INTEGER NOT NULL CHECK (digest_version >= 1),
    required_method TEXT NOT NULL CHECK (required_method IN ('acknowledge', 'typed')),
    method          TEXT NOT NULL CHECK (method IN ('acknowledge', 'typed')),
    typed_statement TEXT,
    policy          TEXT NOT NULL,
    source          TEXT NOT NULL CHECK (source = 'unauthenticated_request'),
    satisfied_at    TEXT NOT NULL,
    consumed_at     TEXT,
    consumed_by_attempt_id TEXT,

    CHECK ((method = 'typed') = (typed_statement IS NOT NULL)),
    -- An override is an acknowledgement of a hazard the preview already states. There is no
    -- second object for an operator to name, and nothing they could type that is not already
    -- in the document this row is bound to by digest.
    CHECK (purpose <> 'self_impact_override'
           OR (method = 'acknowledge' AND required_method = 'acknowledge'))
) STRICT;

INSERT INTO run_confirmations_rebuilt (
    confirmation_id, run_id, purpose, preview_id, preview_digest, digest_version,
    required_method, method, typed_statement, policy, source, satisfied_at, consumed_at,
    consumed_by_attempt_id
)
SELECT
    confirmation_id, run_id, purpose, preview_id, preview_digest, digest_version,
    required_method, method, typed_statement, policy, source, satisfied_at, consumed_at,
    consumed_by_attempt_id
FROM run_confirmations;

DROP TABLE run_confirmations;

ALTER TABLE run_confirmations_rebuilt RENAME TO run_confirmations;

CREATE UNIQUE INDEX one_apply_confirmation_per_run
    ON run_confirmations (run_id) WHERE purpose = 'apply';

CREATE UNIQUE INDEX one_outstanding_recovery_confirmation_per_run
    ON run_confirmations (run_id) WHERE purpose = 'recovery_retry' AND consumed_at IS NULL;

-- One override per Run, ever. Like the apply confirmation and unlike the recovery one: a Run
-- publishes exactly one immutable preview, so there is exactly one document this authority
-- could be granted against, and a second row could only be a second answer to a question
-- already answered. Authority cannot accumulate and be spent all at once.
CREATE UNIQUE INDEX one_self_impact_override_per_run
    ON run_confirmations (run_id) WHERE purpose = 'self_impact_override';

CREATE TRIGGER run_confirmations_match_the_runs_preview
BEFORE INSERT ON run_confirmations
WHEN NOT EXISTS (
    SELECT 1 FROM runs r WHERE r.run_id = NEW.run_id AND r.preview_id = NEW.preview_id
)
BEGIN
    SELECT RAISE(ABORT, 'a confirmation must name the preview its own run published');
END;

CREATE TRIGGER run_confirmations_are_consumed_once
BEFORE UPDATE ON run_confirmations
WHEN OLD.consumed_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'a confirmation is single-use and this one has been consumed');
END;

------------------------------------------------------------------------------------------
-- 3 — the boundary: two authorities, two independent triggers
------------------------------------------------------------------------------------------
--
-- The first is recreated verbatim. The second is new and is the smallest statement of the
-- rule: a Change whose published preview says its execution rests on the override may not
-- come into existence without a consumed override naming that same Run and that same
-- preview. Neither trigger knows about the other, so neither authority can stand in for the
-- other by any path through the code above.

CREATE TRIGGER changes_require_a_consumed_confirmation
BEFORE INSERT ON changes
WHEN NOT EXISTS (
    SELECT 1 FROM run_confirmations c
    WHERE c.run_id = NEW.run_id AND c.preview_id = NEW.preview_id
      AND c.purpose = 'apply'
      AND c.consumed_at IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, 'a change requires a consumed confirmation for its run and plan');
END;

CREATE TRIGGER changes_requiring_a_self_impact_override_have_one
BEFORE INSERT ON changes
WHEN EXISTS (
    SELECT 1 FROM run_previews p
    WHERE p.preview_id = NEW.preview_id
      AND p.execution_eligibility = 'self_impact_override_required'
)
AND NOT EXISTS (
    SELECT 1 FROM run_confirmations c
    WHERE c.run_id = NEW.run_id AND c.preview_id = NEW.preview_id
      AND c.purpose = 'self_impact_override'
      AND c.consumed_at IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT,
        'a change whose plan rests on a self-impact override requires that override consumed');
END;

-- Recreated verbatim; only the table beneath them moved.

CREATE TRIGGER run_guards_require_the_typed_authority
BEFORE INSERT ON run_guards
WHEN NOT EXISTS (
    SELECT 1 FROM run_confirmations c
    WHERE c.confirmation_id = NEW.confirmation_id
      AND c.run_id = NEW.run_id
      AND c.preview_id = NEW.preview_id
      AND c.purpose = 'apply'
      AND c.method = 'typed'
)
BEGIN
    SELECT RAISE(ABORT, 'a connection guard requires this run''s own typed apply confirmation');
END;

CREATE TRIGGER recovery_mutation_names_the_authority_spent_on_it_on_insert
BEFORE INSERT ON change_recovery_attempts
WHEN NEW.mutation_attempt_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM run_confirmations c
    WHERE c.confirmation_id = NEW.confirmation_id
      AND c.run_id = NEW.run_id
      AND c.purpose = 'recovery_retry'
      AND c.consumed_at IS NOT NULL
      AND c.consumed_by_attempt_id = NEW.mutation_attempt_id
)
BEGIN
    SELECT RAISE(ABORT, 'a recovery mutation must name the consumed recovery confirmation spent on it');
END;

CREATE TRIGGER recovery_mutation_names_the_authority_spent_on_it_on_update
BEFORE UPDATE ON change_recovery_attempts
WHEN NEW.mutation_attempt_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM run_confirmations c
    WHERE c.confirmation_id = NEW.confirmation_id
      AND c.run_id = NEW.run_id
      AND c.purpose = 'recovery_retry'
      AND c.consumed_at IS NOT NULL
      AND c.consumed_by_attempt_id = NEW.mutation_attempt_id
)
BEGIN
    SELECT RAISE(ABORT, 'a recovery mutation must name the consumed recovery confirmation spent on it');
END;

CREATE TRIGGER runs_intent_belongs_to_object
BEFORE INSERT ON runs
WHEN EXISTS (
    SELECT 1 FROM run_previews p
    WHERE p.preview_id = NEW.preview_id AND p.intent_id IS NOT NULL
)
AND NOT EXISTS (
    SELECT 1 FROM run_previews p
    JOIN intents i ON i.intent_id = p.intent_id
    WHERE p.preview_id = NEW.preview_id AND i.object_id = NEW.object_id
)
BEGIN
    SELECT RAISE(ABORT, 'the preview''s intent must be retained for this run''s object');
END;

CREATE TRIGGER runs_observation_belongs_to_object
BEFORE INSERT ON runs
WHEN NOT EXISTS (
    SELECT 1 FROM run_previews p
    JOIN observations o ON o.observation_id = p.observation_id
    WHERE p.preview_id = NEW.preview_id AND o.object_id = NEW.object_id
)
BEGIN
    SELECT RAISE(ABORT, 'the preview''s observation must be of this run''s object');
END;

CREATE TRIGGER runs_protection_evidence_belongs_to_host
BEFORE INSERT ON runs
WHEN EXISTS (
    SELECT 1 FROM run_previews p
    JOIN management_path_observations m ON m.observation_id = p.protection_evidence_id
    WHERE p.preview_id = NEW.preview_id AND m.host_id <> NEW.host_id
)
BEGIN
    SELECT RAISE(ABORT, 'the preview''s management-path evidence must be of this run''s host');
END;
