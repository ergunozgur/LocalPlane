-- Docker as a real subsystem: containers as objects, and lifecycle actions as Runs.
--
-- Four tables are rebuilt and nothing new is created. That is the shape of LocalPlane's
-- persistence: LocalPlane needs no Docker tables, because Docker is authoritative for
-- Docker's runtime state and mirroring it here would produce a second copy that is wrong
-- between sweeps. What it needs is for its *existing* vocabulary — object identity, the
-- typed operation set, and what a plan may say it would do — to admit a kind of resource
-- and a kind of change that were not network interfaces and not field reconciliation.
--
-- localplane:foreign-keys=off
--
-- **Why the directive.** SQLite cannot widen a CHECK in place, and `objects` is referenced
-- by ten tables, one of them circularly (`intents.object_id` → `objects`, and
-- `objects.active_intent_id` → `intents`). The two routes 0004 and 0006 recorded were both
-- re-tested against SQLite 3.45 before this file was written, and both still fail:
--
--   * `PRAGMA defer_foreign_keys=ON` — `DROP TABLE` performs an implicit delete whose
--     violation counter is still set at COMMIT, so the transaction cannot commit;
--   * `PRAGMA legacy_alter_table=ON` — reports as set inside a transaction and has no
--     effect, so `ALTER TABLE ... RENAME` still rewrites every child's REFERENCES clause.
--
-- The remaining route is the one SQLite documents for this case, and it needs the pragma
-- outside the transaction — which is what the directive above asks the migration engine
-- for. **Atomicity is not what is given up.** Every statement below still runs inside one
-- `BEGIN IMMEDIATE` together with the row that records this migration, and the engine runs
-- `PRAGMA foreign_key_check` *inside* that transaction before committing, rolling back if
-- it reports anything. This migration is therefore verified more strictly than an ordinary
-- one, which is only ever checked row by row as it writes.
--
-- Nothing is dropped that is not immediately recreated, every rebuild copies column for
-- column, and no historical migration is touched.

------------------------------------------------------------------------------------------
-- 0 — every trigger and index that names a table this migration rebuilds
------------------------------------------------------------------------------------------
--
-- Dropped first, and all of them together, because SQLite reparses the schema on `DROP
-- TABLE` and a trigger whose body names a table that has just gone is a parse error — even
-- when the trigger is about to be recreated against the new one. 0006 learned the same
-- ordering for the same reason.
--
-- `run_confirmations_match_the_runs_preview` is in this list although its table is not
-- rebuilt: it reads `runs`, which is.

DROP TRIGGER objects_active_intent_belongs_to_object_insert;

DROP TRIGGER objects_active_intent_belongs_to_object_update;

DROP INDEX idx_objects_host_kind;

DROP TRIGGER run_previews_are_immutable;

DROP TRIGGER runs_intent_belongs_to_object;

DROP TRIGGER runs_observation_belongs_to_object;

DROP TRIGGER runs_identity_is_immutable;

DROP TRIGGER runs_protection_evidence_belongs_to_host;

DROP INDEX idx_runs_host_created;

DROP INDEX idx_runs_object_created;

DROP TRIGGER changes_identity_is_immutable;

DROP TRIGGER changes_require_a_consumed_confirmation;

DROP INDEX idx_changes_host_created;

DROP INDEX idx_changes_object_created;

DROP TRIGGER run_confirmations_match_the_runs_preview;

DROP TRIGGER run_events_are_append_only;

DROP TRIGGER run_events_are_not_deleted;

DROP INDEX idx_run_events_run;

------------------------------------------------------------------------------------------
-- 1 — `objects`: identity may now rest on a provider-assigned id
------------------------------------------------------------------------------------------
--
-- `identity_basis` enumerated three values and all three are facts about a network
-- interface: a burned-in MAC, a bus address, a kernel name. A Docker container has none of
-- them. Its identity is the id the daemon assigned it, which is immutable for the life of
-- the container, unique on the host, and stronger than any name — and calling that a
-- `kernel_name` would be a falsehood stored in the column whose entire job is to say what
-- the identity rests on.
--
-- `provider_id` is the general form: the identity the system that manages a resource
-- assigned to it and guarantees stable. It is what makes `objects` a table about resources
-- rather than a table about links.

CREATE TABLE objects_rebuilt (
    object_id           TEXT NOT NULL PRIMARY KEY,
    host_id             TEXT NOT NULL REFERENCES hosts(host_id),
    kind                TEXT NOT NULL,
    identity_basis      TEXT NOT NULL CHECK (identity_basis IN
                            ('permanent_mac', 'device_path', 'kernel_name', 'provider_id')),
    identity_value      TEXT NOT NULL,
    identity_confidence TEXT NOT NULL CHECK (identity_confidence IN ('high', 'low')),
    display_name        TEXT NOT NULL,
    management_state    TEXT NOT NULL CHECK (management_state IN ('observe_only', 'observed', 'managed')),
    management_reason   TEXT NOT NULL,
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    active_intent_id    TEXT REFERENCES intents(intent_id)
        CHECK ((management_state = 'managed') = (active_intent_id IS NOT NULL)),
    UNIQUE (host_id, kind, identity_basis, identity_value)
) STRICT;

INSERT INTO objects_rebuilt (
    object_id, host_id, kind, identity_basis, identity_value, identity_confidence,
    display_name, management_state, management_reason, first_seen_at, last_seen_at,
    active_intent_id
)
SELECT
    object_id, host_id, kind, identity_basis, identity_value, identity_confidence,
    display_name, management_state, management_reason, first_seen_at, last_seen_at,
    active_intent_id
FROM objects;

DROP TABLE objects;

ALTER TABLE objects_rebuilt RENAME TO objects;

------------------------------------------------------------------------------------------
-- 2 — `run_previews`: a plan may describe an action, not only a field change
------------------------------------------------------------------------------------------
--
-- Every column of the published plan was written for one shape of change: one controlled
-- field, an integer-encoded current and desired value that must differ, and a retained
-- intent that supplied the desired one. That is exactly what reconciling an MTU is, and it
-- is not what starting a container is.
--
--   * an **action** has no desired *value*. It has a verb, the lifecycle state the
--     resource was observed in, and the state that must hold afterwards for the action to
--     have worked. `restart` proves the point: its expected state is `running` and so is
--     the state it was observed in, which the `current_value <> desired_value` CHECK
--     forbids and rightly so — the two are not a disagreement to reconcile;
--   * an action has **no intent**. LocalPlane retains no desired state for a container,
--     nothing drifts, and there is no version chain. `intent_id` was NOT NULL because
--     there was no other kind of plan; it is now paired to the change kind instead.
--
-- `change_kind` is the discriminator, and every column of each half is CHECKed to be
-- present for its own kind and absent for the other — so a row cannot be half a field
-- change and half an action, and a reader never has to guess which columns mean anything.

CREATE TABLE run_previews_rebuilt (
    preview_id     TEXT    NOT NULL PRIMARY KEY,
    preview_digest TEXT    NOT NULL,
    digest_version INTEGER NOT NULL CHECK (digest_version >= 1),
    operation      TEXT    NOT NULL CHECK (operation IN (
        'network.interface.reconcile_mtu',
        'docker.container.start', 'docker.container.stop', 'docker.container.restart'
    )),

    -- WHAT would change, in one of two shapes.
    change_kind    TEXT    NOT NULL CHECK (change_kind IN ('field', 'action')),

    field          TEXT,
    value_type     TEXT    CHECK (value_type IS NULL OR value_type IN ('boolean', 'integer')),
    current_value  INTEGER,
    desired_value  INTEGER,

    action         TEXT,
    observed_state TEXT,
    expected_state TEXT,

    -- The retained intent a field change reconciles towards. An action has none, and the
    -- CHECK below says so rather than leaving four nullable columns to be read hopefully.
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
    protection_evidence_id TEXT
        REFERENCES management_path_observations(observation_id),
    protection_evidence_observed_at TEXT,

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
        CHECK (execution_eligibility IN ('eligible', 'blocked')),
    execution_blockers     TEXT NOT NULL DEFAULT '[]',
    execution_provider     TEXT,
    required_capability    TEXT NOT NULL,
    capability_declared    INTEGER NOT NULL CHECK (capability_declared IN (0, 1)),

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

    -- A field change: both ends present, typed, different, and an intent behind them.
    CHECK (change_kind <> 'field' OR (
        field IS NOT NULL AND value_type IS NOT NULL
        AND current_value IS NOT NULL AND desired_value IS NOT NULL
        AND current_value <> desired_value
        AND (value_type <> 'boolean' OR (current_value IN (0, 1) AND desired_value IN (0, 1)))
        AND intent_id IS NOT NULL AND intent_version IS NOT NULL
        AND intent_capability IS NOT NULL AND intent_provider IS NOT NULL
        AND action IS NULL AND observed_state IS NULL AND expected_state IS NULL
    )),
    -- An action: a verb, the state it was observed in and the state it must produce, and
    -- structurally no value, no intent and no drift finding. There is nothing for an
    -- action to have drifted from.
    CHECK (change_kind <> 'action' OR (
        action IS NOT NULL AND observed_state IS NOT NULL AND expected_state IS NOT NULL
        AND field IS NULL AND value_type IS NULL
        AND current_value IS NULL AND desired_value IS NULL
        AND intent_id IS NULL AND intent_version IS NULL
        AND intent_capability IS NULL AND intent_provider IS NULL
        AND drift_finding_id IS NULL
    )),
    CHECK (protection_evidence_id IS NULL OR protection_evidence_observed_at IS NOT NULL)
) STRICT;

INSERT INTO run_previews_rebuilt (
    preview_id, preview_digest, digest_version, operation, change_kind,
    field, value_type, current_value, desired_value,
    action, observed_state, expected_state,
    intent_id, intent_version, intent_capability, intent_provider,
    observation_id, sweep_id, observed_at, drift_finding_id,
    ownership_state, ownership_reason, ownership_claims, ownership_gaps, provider_readings,
    protection_status, protection_reasons, protection_unresolved, protection_management_path,
    protection_reason, protection_missing_evidence, protection_evidence_id,
    protection_evidence_observed_at,
    risk_tier, risk_factors,
    confirmation_required, confirmation_method, confirmation_source, confirmation_reasons,
    confirmation_policy, confirmation_token_issued,
    execution_availability, execution_eligibility, execution_blockers, execution_provider,
    required_capability, capability_declared,
    recovery_mode, recovery_rollback_possible, recovery_armed, recovery_guarantee,
    recovery_reason,
    verification_capability, verification_provider, verification_condition,
    verification_executed, published_at
)
SELECT
    preview_id, preview_digest, digest_version, operation, 'field',
    field, value_type, current_value, desired_value,
    NULL, NULL, NULL,
    intent_id, intent_version, intent_capability, intent_provider,
    observation_id, sweep_id, observed_at, drift_finding_id,
    ownership_state, ownership_reason, ownership_claims, ownership_gaps, provider_readings,
    protection_status, protection_reasons, protection_unresolved, protection_management_path,
    protection_reason, protection_missing_evidence, protection_evidence_id,
    protection_evidence_observed_at,
    risk_tier, risk_factors,
    confirmation_required, confirmation_method, confirmation_source, confirmation_reasons,
    confirmation_policy, confirmation_token_issued,
    execution_availability, execution_eligibility, execution_blockers, execution_provider,
    required_capability, capability_declared,
    recovery_mode, recovery_rollback_possible, recovery_armed, recovery_guarantee,
    recovery_reason,
    verification_capability, verification_provider, verification_condition,
    verification_executed, published_at
FROM run_previews;

DROP TABLE run_previews;

ALTER TABLE run_previews_rebuilt RENAME TO run_previews;

------------------------------------------------------------------------------------------
-- 3 — `runs`: the operation vocabulary, and a plan that names no intent
------------------------------------------------------------------------------------------
--
-- Only two things move. `operation` admits the three Docker lifecycle actions, and the
-- trigger that requires a Run's preview to cite an intent retained for the Run's object now
-- applies only where there is an intent to cite. It has not been relaxed: for a field
-- change it is exactly the rule it was, and for an action there is structurally no
-- `intent_id` on the preview at all — the `run_previews` CHECK above guarantees it — so
-- there is nothing the trigger could be protecting against.
--
-- The state vocabulary is untouched. An action moves through the same twelve states, and
-- `guarded` and `draft` remain unreachable for the same reasons.

CREATE TABLE runs_rebuilt (
    run_id      TEXT NOT NULL PRIMARY KEY,
    host_id     TEXT NOT NULL REFERENCES hosts(host_id),
    object_id   TEXT NOT NULL REFERENCES objects(object_id),
    operation   TEXT NOT NULL CHECK (operation IN (
        'network.interface.reconcile_mtu',
        'docker.container.start', 'docker.container.stop', 'docker.container.restart'
    )),
    state       TEXT NOT NULL CHECK (state IN (
        'preview', 'awaiting_confirmation', 'arming', 'applying', 'verifying',
        'succeeded', 'failed', 'rolling_back', 'rollback_verifying', 'rolled_back',
        'recovery_required', 'cancelled'
    )),
    preview_id  TEXT NOT NULL UNIQUE REFERENCES run_previews(preview_id),
    host_effect TEXT NOT NULL DEFAULT 'none'
        CHECK (host_effect IN ('none', 'written', 'write_unknown')),
    created_at   TEXT NOT NULL,
    cancelled_at TEXT,
    finished_at  TEXT,
    CHECK ((state = 'cancelled') = (cancelled_at IS NOT NULL)),
    CHECK (host_effect = 'none' OR state IN (
        'applying', 'verifying', 'succeeded', 'rolling_back', 'rollback_verifying',
        'rolled_back', 'recovery_required'
    )),
    CHECK (state <> 'succeeded' OR host_effect = 'written'),
    CHECK (state <> 'failed' OR host_effect = 'none'),
    CHECK (state <> 'rolled_back' OR host_effect IN ('written', 'write_unknown')),
    CHECK (state <> 'recovery_required' OR host_effect IN ('written', 'write_unknown'))
) STRICT;

INSERT INTO runs_rebuilt (
    run_id, host_id, object_id, operation, state, preview_id, host_effect,
    created_at, cancelled_at, finished_at
)
SELECT
    run_id, host_id, object_id, operation, state, preview_id, host_effect,
    created_at, cancelled_at, finished_at
FROM runs;

DROP TABLE runs;

ALTER TABLE runs_rebuilt RENAME TO runs;

------------------------------------------------------------------------------------------
-- 4 — `changes`: an action's record, and the absence of a rollback
------------------------------------------------------------------------------------------
--
-- The same discriminator, plus two structural statements:
--
--
--   * **an action writes no checkpoint.** `checkpoint_id` was NOT NULL because every write
--     LocalPlane could perform was a scalar field whose previous value was verified and
--     could be written back. Starting a container has no inverse LocalPlane may perform:
--     issuing `stop` because `start` could not be verified is not a restoration, it is a
--     second change nobody asked for, and it can make the situation worse. So there is no
--     checkpoint, `recovery_mode` is `none`, and the row cannot claim otherwise:
--     `rollback_required`, `rollback_attempt_id` and every rollback column are CHECKed
--     absent for an action, and `rolled_back` is not an ending an action can reach;
--   * **the execution correlation lives here.** For a field change it is on the checkpoint,
--     where the rollback material is. An action has no checkpoint, and the stable material
--     that identifies the target — and the lifecycle evidence a verification is judged
--     against — has to be durable before the dispatch marker is written, so it is written
--     at the boundary with everything else the Change was about.
--
-- `verification_observed_state` is a separate column from `verification_observed_value`
-- rather than a widening of it. A lifecycle state is not an integer, STRICT tables mean it
-- cannot pretend to be one, and one column holding two types is how a reader ends up
-- guessing which it is looking at.

CREATE TABLE changes_rebuilt (
    change_id     TEXT NOT NULL PRIMARY KEY,
    run_id        TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
    preview_id    TEXT NOT NULL REFERENCES run_previews(preview_id),
    checkpoint_id TEXT REFERENCES run_checkpoints(checkpoint_id),
    host_id       TEXT NOT NULL REFERENCES hosts(host_id),
    object_id     TEXT NOT NULL REFERENCES objects(object_id),
    operation     TEXT NOT NULL CHECK (operation IN (
        'network.interface.reconcile_mtu',
        'docker.container.start', 'docker.container.stop', 'docker.container.restart'
    )),

    change_kind   TEXT NOT NULL CHECK (change_kind IN ('field', 'action')),

    field         TEXT,
    value_type    TEXT CHECK (value_type IS NULL OR value_type IN ('boolean', 'integer')),
    before_value  INTEGER,
    desired_value INTEGER,

    action         TEXT,
    observed_state TEXT,
    expected_state TEXT,

    -- The stable material the executor used to reach this target, and the evidence a
    -- verification is judged against. Opaque to the engine on purpose: what identifies a
    -- target is the concrete operation's business.
    execution_correlation TEXT NOT NULL DEFAULT '{}',

    -- The write-boundary time: when LocalPlane stopped deciding and started acting.
    created_at TEXT NOT NULL,

    apply_attempt_id  TEXT NOT NULL UNIQUE,
    dispatch_began_at TEXT,
    mutation_outcome  TEXT
        CHECK (mutation_outcome IS NULL
               OR mutation_outcome IN ('not_written', 'written', 'write_unknown')),
    mutation_reason   TEXT,
    mutation_provider TEXT,
    mutation_method   TEXT,
    mutation_detail   TEXT NOT NULL DEFAULT '{}',
    settled_at        TEXT,

    host_effect TEXT NOT NULL DEFAULT 'none'
        CHECK (host_effect IN ('none', 'written', 'write_unknown')),

    verification_outcome TEXT NOT NULL DEFAULT 'not_attempted' CHECK (verification_outcome IN (
        'not_attempted', 'verified', 'mismatch', 'value_unreadable',
        'observation_unavailable', 'source_incompatible', 'target_absent'
    )),
    verification_observation_id TEXT REFERENCES observations(observation_id),
    verification_observed_value INTEGER,
    verification_observed_state TEXT,
    verification_reason TEXT,

    rollback_required   INTEGER NOT NULL DEFAULT 0 CHECK (rollback_required IN (0, 1)),
    rollback_attempt_id TEXT UNIQUE,
    rollback_dispatch_began_at TEXT,
    rollback_outcome    TEXT
        CHECK (rollback_outcome IS NULL
               OR rollback_outcome IN ('not_written', 'written', 'write_unknown')),
    rollback_reason     TEXT,
    rollback_detail     TEXT NOT NULL DEFAULT '{}',
    rollback_verification_outcome TEXT NOT NULL DEFAULT 'not_attempted'
        CHECK (rollback_verification_outcome IN (
            'not_attempted', 'verified', 'mismatch', 'value_unreadable',
            'observation_unavailable', 'source_incompatible', 'target_absent'
        )),
    rollback_verification_observation_id TEXT REFERENCES observations(observation_id),
    rollback_verification_observed_value INTEGER,

    result TEXT NOT NULL DEFAULT 'in_flight' CHECK (result IN (
        'in_flight', 'succeeded', 'failed', 'rolled_back', 'recovery_required'
    )),
    recovery_reason TEXT,
    finished_at     TEXT,

    -- A field change is what it always was, and it still carries the checkpoint that would
    -- restore it.
    CHECK (change_kind <> 'field' OR (
        field IS NOT NULL AND value_type IS NOT NULL
        AND before_value IS NOT NULL AND desired_value IS NOT NULL
        AND before_value <> desired_value
        AND checkpoint_id IS NOT NULL
        AND action IS NULL AND observed_state IS NULL AND expected_state IS NULL
        AND verification_observed_state IS NULL
    )),
    -- An action has no checkpoint, no rollback, and no ending that claims one.
    CHECK (change_kind <> 'action' OR (
        action IS NOT NULL AND observed_state IS NOT NULL AND expected_state IS NOT NULL
        AND field IS NULL AND value_type IS NULL
        AND before_value IS NULL AND desired_value IS NULL
        AND checkpoint_id IS NULL
        AND verification_observed_value IS NULL
        AND rollback_required = 0
        AND rollback_attempt_id IS NULL AND rollback_dispatch_began_at IS NULL
        AND rollback_outcome IS NULL AND rollback_reason IS NULL
        AND rollback_verification_outcome = 'not_attempted'
        AND rollback_verification_observation_id IS NULL
        AND rollback_verification_observed_value IS NULL
        AND result <> 'rolled_back'
    )),

    CHECK (
        mutation_outcome IS NULL
        OR mutation_outcome = 'not_written'
        OR dispatch_began_at IS NOT NULL
    ),

    CHECK (
        (mutation_outcome IS NULL AND host_effect = 'none')
        OR (mutation_outcome = 'not_written' AND host_effect = 'none')
        OR (mutation_outcome = 'written' AND host_effect = 'written')
        OR (mutation_outcome = 'write_unknown' AND host_effect = 'write_unknown')
    ),

    CHECK (result <> 'failed' OR (mutation_outcome = 'not_written' AND host_effect = 'none')),
    CHECK (result <> 'succeeded'
           OR (mutation_outcome = 'written' AND verification_outcome = 'verified')),
    CHECK (result <> 'rolled_back' OR rollback_verification_outcome = 'verified'),
    CHECK (result <> 'recovery_required' OR recovery_reason IS NOT NULL),

    CHECK (verification_outcome <> 'verified' OR verification_observation_id IS NOT NULL),
    CHECK (rollback_verification_outcome <> 'verified'
           OR rollback_verification_observation_id IS NOT NULL)
) STRICT;

INSERT INTO changes_rebuilt (
    change_id, run_id, preview_id, checkpoint_id, host_id, object_id, operation, change_kind,
    field, value_type, before_value, desired_value,
    action, observed_state, expected_state, execution_correlation,
    created_at, apply_attempt_id, dispatch_began_at, mutation_outcome, mutation_reason,
    mutation_provider, mutation_method, mutation_detail, settled_at, host_effect,
    verification_outcome, verification_observation_id, verification_observed_value,
    verification_observed_state, verification_reason,
    rollback_required, rollback_attempt_id, rollback_dispatch_began_at, rollback_outcome,
    rollback_reason, rollback_detail, rollback_verification_outcome,
    rollback_verification_observation_id, rollback_verification_observed_value,
    result, recovery_reason, finished_at
)
SELECT
    c.change_id, c.run_id, c.preview_id, c.checkpoint_id, c.host_id, c.object_id,
    c.operation, 'field',
    c.field, c.value_type, c.before_value, c.desired_value,
    NULL, NULL, NULL,
    -- Restated, not invented: the correlation a pre-0008 change used is on its checkpoint,
    -- which is where the executor read it from and where it still is. Copying it here keeps
    -- one shape for every row rather than two ways of finding the same fact.
    COALESCE((SELECT k.execution_correlation FROM run_checkpoints k
              WHERE k.checkpoint_id = c.checkpoint_id), '{}'),
    c.created_at, c.apply_attempt_id, c.dispatch_began_at, c.mutation_outcome,
    c.mutation_reason, c.mutation_provider, c.mutation_method, c.mutation_detail,
    c.settled_at, c.host_effect,
    c.verification_outcome, c.verification_observation_id, c.verification_observed_value,
    NULL, c.verification_reason,
    c.rollback_required, c.rollback_attempt_id, c.rollback_dispatch_began_at,
    c.rollback_outcome, c.rollback_reason, c.rollback_detail,
    c.rollback_verification_outcome, c.rollback_verification_observation_id,
    c.rollback_verification_observed_value,
    c.result, c.recovery_reason, c.finished_at
FROM changes c;

DROP TABLE changes;

ALTER TABLE changes_rebuilt RENAME TO changes;

------------------------------------------------------------------------------------------
-- 5 — `run_events`: two steps an action takes that a field change does not
------------------------------------------------------------------------------------------
--
-- The transcript's vocabulary is closed and every member is a fact, so a step that happens
-- needs a name of its own rather than the nearest existing one. An action's preparation is
-- not arming: arming means establishing recovery, and an action has none to establish. What
-- it does instead is identify the target — which can fail, before the boundary, leaving a
-- Run that ended with nothing written and a transcript that should say why.
--
-- Reusing `arming_started` for it would produce a history reading "arming started" followed
-- by a boundary crossing with no checkpoint between them, which is what an interrupted
-- arming looks like. Two names cost a rebuild of a leaf table; a transcript that misreports
-- a step costs an operator the one record they have.

CREATE TABLE run_events_rebuilt (
    event_id  TEXT NOT NULL PRIMARY KEY,
    run_id    TEXT NOT NULL REFERENCES runs(run_id),
    change_id TEXT REFERENCES changes(change_id),
    sequence  INTEGER NOT NULL CHECK (sequence >= 1),
    event     TEXT NOT NULL CHECK (event IN (
        'run_planned', 'run_cancelled',
        'confirmation_satisfied', 'confirmation_required', 'confirmation_consumed',
        'apply_refused',
        'arming_started', 'checkpoint_written', 'arming_failed',
        'target_correlated', 'target_correlation_failed',
        'write_boundary_crossed', 'mutation_dispatched', 'mutation_result',
        'verification_started', 'verification_result',
        'rollback_started', 'rollback_mutation_dispatched', 'rollback_mutation_result',
        'rollback_verification_started', 'rollback_verification_result',
        'recovery_required', 'run_finished'
    )),
    state_from  TEXT,
    state_to    TEXT,
    occurred_at TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '{}',
    UNIQUE (run_id, sequence)
) STRICT;

INSERT INTO run_events_rebuilt (
    event_id, run_id, change_id, sequence, event, state_from, state_to, occurred_at, detail
)
SELECT
    event_id, run_id, change_id, sequence, event, state_from, state_to, occurred_at, detail
FROM run_events;

DROP TABLE run_events;

ALTER TABLE run_events_rebuilt RENAME TO run_events;

------------------------------------------------------------------------------------------
-- 6 — the indexes and triggers, recreated
------------------------------------------------------------------------------------------
--
-- Verbatim apart from the three changes argued for above: `runs_intent_belongs_to_object`
-- now applies only where the preview names an intent, `changes_identity_is_immutable`
-- freezes the action columns and the execution correlation as well, and
-- `changes_checkpoint_belongs_to_run` is new.

CREATE TRIGGER objects_active_intent_belongs_to_object_insert
BEFORE INSERT ON objects
WHEN NEW.active_intent_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM intents
     WHERE intent_id = NEW.active_intent_id AND object_id = NEW.object_id
 )
BEGIN
    SELECT RAISE(ABORT, 'active_intent_id must reference an intent retained for this object');
END;

CREATE TRIGGER objects_active_intent_belongs_to_object_update
BEFORE UPDATE OF active_intent_id ON objects
WHEN NEW.active_intent_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM intents
     WHERE intent_id = NEW.active_intent_id AND object_id = NEW.object_id
 )
BEGIN
    SELECT RAISE(ABORT, 'active_intent_id must reference an intent retained for this object');
END;

CREATE TRIGGER run_previews_are_immutable
BEFORE UPDATE ON run_previews
BEGIN
    SELECT RAISE(ABORT, 'a published preview is immutable; plan again instead of rewriting');
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

CREATE TRIGGER runs_identity_is_immutable
BEFORE UPDATE OF run_id, host_id, object_id, operation, preview_id, created_at
ON runs
BEGIN
    SELECT RAISE(ABORT, 'a run''s operation, target and published plan are immutable');
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

CREATE TRIGGER run_confirmations_match_the_runs_preview
BEFORE INSERT ON run_confirmations
WHEN NOT EXISTS (
    SELECT 1 FROM runs r WHERE r.run_id = NEW.run_id AND r.preview_id = NEW.preview_id
)
BEGIN
    SELECT RAISE(ABORT, 'a confirmation must name the preview its own run published');
END;

CREATE TRIGGER changes_identity_is_immutable
BEFORE UPDATE OF change_id, run_id, preview_id, checkpoint_id, host_id, object_id,
                 operation, change_kind, field, value_type, before_value, desired_value,
                 action, observed_state, expected_state, execution_correlation, created_at,
                 apply_attempt_id
ON changes
BEGIN
    SELECT RAISE(ABORT, 'what a change was about is immutable; only what became of it moves');
END;



-- New, and it replaces something a column used to guarantee. While `checkpoint_id` was NOT
-- NULL and `run_checkpoints.run_id` was UNIQUE, a Change could not name a checkpoint armed
-- for a different Run without that Run already having one of its own. The column is now
-- nullable, so the rule is stated outright rather than left to be inferred from two other
-- constraints.
CREATE TRIGGER run_events_are_append_only
BEFORE UPDATE ON run_events
BEGIN
    SELECT RAISE(ABORT, 'the transcript is append-only; a recorded event is what happened');
END;

CREATE TRIGGER run_events_are_not_deleted
BEFORE DELETE ON run_events
BEGIN
    SELECT RAISE(ABORT, 'the transcript is append-only; events are not removed');
END;

CREATE TRIGGER changes_checkpoint_belongs_to_run
BEFORE INSERT ON changes
WHEN NEW.checkpoint_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM run_checkpoints k
     WHERE k.checkpoint_id = NEW.checkpoint_id AND k.run_id = NEW.run_id
 )
BEGIN
    SELECT RAISE(ABORT, 'a change''s checkpoint must be the one armed for its own run');
END;

CREATE INDEX idx_objects_host_kind ON objects(host_id, kind, display_name);

CREATE INDEX idx_runs_host_created ON runs(host_id, created_at DESC);

CREATE INDEX idx_runs_object_created ON runs(object_id, created_at DESC);

CREATE INDEX idx_changes_host_created ON changes(host_id, created_at DESC);

CREATE INDEX idx_changes_object_created ON changes(object_id, created_at DESC);

CREATE INDEX idx_run_events_run ON run_events(run_id, sequence);

-- Created last, and the order is deliberate: SQLite fires BEFORE INSERT triggers in reverse
-- creation order, so this is the rule a forged row is refused by first. Of the three that
-- guard an insert here it is the one worth reporting — a change with no consumed
-- confirmation is a change nobody authorised, which is a more fundamental thing to have
-- caught than a mismatched checkpoint.
--
-- Unchanged otherwise, and still universal: every operation this build can execute requires a
-- confirmation, so every Change requires a consumed one for its own Run and its own
-- published plan. The three Docker lifecycle actions are all rated at or above the tier the
-- policy confirms from, so none of them is an exception. An operation that genuinely
-- required none would need this trigger revisited in the migration that introduces it —
-- which is the note 0007 left, kept here because it is still the note that applies.
CREATE TRIGGER changes_require_a_consumed_confirmation
BEFORE INSERT ON changes
WHEN NOT EXISTS (
    SELECT 1 FROM run_confirmations c
    WHERE c.run_id = NEW.run_id AND c.preview_id = NEW.preview_id
      AND c.consumed_at IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, 'a change requires a consumed confirmation for its run and plan');
END;
