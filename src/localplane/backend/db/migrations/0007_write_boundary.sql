-- The write boundary: confirmation, arming, the Change, and the transcript.
--
-- This is the migration that lets LocalPlane record having written to a host. Every previous
-- migration was structurally incapable of it — `management_transitions`, `intent_revisions`
-- and `runs` all CHECKed `host_effect` to the single value 'none' — and exactly one of those
-- three loosens here, by exactly two further values, and one new table gains the same three.
--
--   run_confirmations   an operator's confirmation: durable, bound, single-use
--   run_checkpoints     the rollback material, written before anything can be written
--   changes             the durable record that LocalPlane entered a host-write path
--   run_events          the append-only transcript of one Run's lifecycle
--   object_write_locks  durable serialisation: one mutating Run per object and field
--   run_previews        rebuilt, so a plan may say execution is available and eligible
--   runs                rebuilt, so the lifecycle may go past 'preview' and record an effect
--
-- WHAT A CHANGE IS
--
-- The record that LocalPlane *entered the path on which a host write may occur*. Not that one
-- happened — that is `mutation_outcome`, which has three values — and not that a plan exists,
-- that somebody confirmed it, or that a checkpoint was armed. A Change may therefore exist
-- with 'not_written': the boundary was crossed and the privileged helper refused before the
-- mutating message existed. What may *not* exist is a Change whose result is 'failed' after
-- anything other than a proven-empty write, and a CHECK below says so.
--
-- THE CRASH WINDOW IS A COLUMN
--
-- `dispatch_began_at` is written and committed *before* the mutating request is sent, and
-- `mutation_outcome` after the answer comes back. Between those two commits a process death
-- leaves a row that says dispatch began and does not say what happened, and the only honest
-- reading of that row is `write_unknown`.
--
-- WHICH INVARIANTS ARE IN THE SCHEMA, AND WHICH ARE NOT
--
-- The CHECKs here are the ones that make a *false durable record* impossible: the closed
-- vocabularies, the pairing between an outcome and what may be claimed about the host, and
-- the three words that must be earned — 'failed', 'succeeded' and 'rolled_back'. Bookkeeping
-- that merely restates the application's own state machine (that a settled row has a settled
-- time, that a rollback attempt implies a rollback was required) is enforced where it is
-- decided, in `backend/changes.py`, and is not duplicated here. A schema that reproduced the
-- whole state machine would be a second implementation of it to keep in step.

-- ---------------------------------------------------------------------------------------
-- THE REBUILD
--
-- 0005 CHECKed `execution_availability` to 'not_implemented', `execution_eligibility` to
-- 'blocked' and `execution_provider` to NULL, because there was no execution code, no
-- mutating agent method and no privileged helper. All three exist now, so all three CHECKs
-- are false statements about the build and have to widen — to closed vocabularies, not to
-- anything. SQLite cannot widen a CHECK in place, so `run_previews` is rebuilt and `runs` —
-- its only child — is rebuilt with it, in the order 0006 established and verified: drop the
-- triggers and indexes, build both tables with the child referencing the new parent, copy,
-- drop child then parent, rename parent then child, put the indexes and triggers back. No
-- PRAGMA is touched, `foreign_keys` stays ON, and the migration keeps its atomicity.
--
-- FOUR OF 0005's SEVEN CHECKS ARE UNCHANGED, AND THAT IS DELIBERATE. `recovery_armed = 0`,
-- `verification_executed = 0` and `confirmation_token_issued = 0` are still true of a
-- *preview*: it is published before anything is armed and before anything is verified, and
-- this build still issues no token of any kind — a confirmation is a row naming a Run and a
-- plan, not a bearer value. Arming and verification now happen, on the Change, in their own
-- tables. Keeping these three is what stops the immutable document acquiring claims that
-- belong to the execution of it.

DROP TRIGGER run_previews_are_immutable;

DROP TRIGGER runs_intent_belongs_to_object;

DROP TRIGGER runs_observation_belongs_to_object;

DROP TRIGGER runs_identity_is_immutable;

DROP TRIGGER runs_protection_evidence_belongs_to_host;

DROP INDEX idx_runs_host_created;

DROP INDEX idx_runs_object_created;

CREATE TABLE run_previews_rebuilt (
    preview_id     TEXT    NOT NULL PRIMARY KEY,
    preview_digest TEXT    NOT NULL,
    digest_version INTEGER NOT NULL CHECK (digest_version >= 1),
    operation      TEXT    NOT NULL CHECK (operation IN ('network.interface.reconcile_mtu')),

    field          TEXT    NOT NULL,
    value_type     TEXT    NOT NULL CHECK (value_type IN ('boolean', 'integer')),
    current_value  INTEGER NOT NULL,
    desired_value  INTEGER NOT NULL,

    intent_id         TEXT NOT NULL REFERENCES intents(intent_id),
    intent_version    INTEGER NOT NULL CHECK (intent_version >= 1),
    intent_capability TEXT NOT NULL,
    intent_provider   TEXT NOT NULL,

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

    -- WIDENED, to closed vocabularies. `execution_provider` is no longer CHECKed NULL because
    -- one exists and it is not a plausible-sounding daemon: it is the privileged helper's
    -- fixed RTM_NEWLINK/IFLA_MTU message.
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

    CHECK (current_value <> desired_value),
    CHECK (value_type <> 'boolean' OR (current_value IN (0, 1) AND desired_value IN (0, 1))),
    CHECK (protection_evidence_id IS NULL OR protection_evidence_observed_at IS NOT NULL)
) STRICT;

-- Column for column: a preview published before execution existed keeps saying exactly what
-- it said, because that is what the operator was shown. What makes those Runs non-executable
-- is not a rewrite of their previews; it is that re-planning them now produces a different
-- digest, and that apply refuses a preview whose own published assessment says execution was
-- not available.
INSERT INTO run_previews_rebuilt SELECT * FROM run_previews;

-- `runs`, with two CHECKs widened and one column added.
--
-- `state` now enumerates the twelve states this build can truthfully reach. Two of the
-- fourteen are still absent: 'draft', because creating a Run *is* planning one, and
-- 'guarded', because a guarded change is a change to the object carrying the operator's own
-- connection and this build blocks those rather than arming a guard it does not have.
--
-- The four CHECKs at the end are the state machine's honesty where no code has to remember
-- it: a host effect may only be claimed at or past the boundary; 'succeeded' requires the
-- host to have been written; 'failed' requires that it provably was not — the rule that
-- `failed` is a lie after a possible write; and 'rolled_back' and 'recovery_required' require
-- that it may have been, because neither word means anything otherwise.
CREATE TABLE runs_rebuilt (
    run_id      TEXT NOT NULL PRIMARY KEY,
    host_id     TEXT NOT NULL REFERENCES hosts(host_id),
    object_id   TEXT NOT NULL REFERENCES objects(object_id),
    operation   TEXT NOT NULL CHECK (operation IN ('network.interface.reconcile_mtu')),
    state       TEXT NOT NULL CHECK (state IN (
        'preview', 'awaiting_confirmation', 'arming', 'applying', 'verifying',
        'succeeded', 'failed', 'rolling_back', 'rollback_verifying', 'rolled_back',
        'recovery_required', 'cancelled'
    )),
    preview_id  TEXT NOT NULL UNIQUE REFERENCES run_previews_rebuilt(preview_id),
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

-- One restatement in the copy, and it is not a decision about history: a Run cancelled before
-- this migration finished when it was cancelled, so `finished_at` comes from `cancelled_at`
-- rather than being left null.
INSERT INTO runs_rebuilt (
    run_id, host_id, object_id, operation, state, preview_id, host_effect,
    created_at, cancelled_at, finished_at
)
SELECT run_id, host_id, object_id, operation, state, preview_id, host_effect,
       created_at, cancelled_at, cancelled_at
FROM runs;

DROP TABLE runs;

DROP TABLE run_previews;

ALTER TABLE run_previews_rebuilt RENAME TO run_previews;

ALTER TABLE runs_rebuilt RENAME TO runs;

CREATE INDEX idx_runs_host_created ON runs(host_id, created_at DESC);

CREATE INDEX idx_runs_object_created ON runs(object_id, created_at DESC);

CREATE TRIGGER run_previews_are_immutable
BEFORE UPDATE ON run_previews
BEGIN
    SELECT RAISE(ABORT, 'a published preview is immutable; plan again instead of rewriting');
END;

CREATE TRIGGER runs_intent_belongs_to_object
BEFORE INSERT ON runs
WHEN NOT EXISTS (
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

-- `host_effect` has left this list, because it is now a fact that moves: a Run that crosses
-- the boundary records what became of the host. What a Run *is* stays exactly as immutable.
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

-- ---------------------------------------------------------------------------------------
-- CONFIRMATION
--
-- One row per Run, naming the Run *and* the preview. Both, deliberately: 0005 recorded that
-- two identical concurrent plans share one digest, so a confirmation keyed on a digest could
-- not tell which of them an operator confirmed. The digest is kept as evidence of *what* was
-- confirmed, never as the thing that authorises.
--
-- Single-use is structural: `run_id` is UNIQUE so a second confirmation cannot exist, and a
-- trigger refuses every update to a row that already has `consumed_at`.
--
-- THERE IS NO ACTOR, AND THAT IS THE TRUTH RATHER THAN AN OMISSION. LocalPlane has no
-- authentication. Recording a user id would be recording a fiction; `source` says exactly
-- what is known. A build that gains authentication adds a column beside this one and the
-- meaning of these rows does not change — they will still say that nobody was identified.
CREATE TABLE run_confirmations (
    confirmation_id TEXT NOT NULL PRIMARY KEY,
    run_id          TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
    preview_id      TEXT NOT NULL REFERENCES run_previews(preview_id),
    preview_digest  TEXT NOT NULL,
    digest_version  INTEGER NOT NULL CHECK (digest_version >= 1),
    required_method TEXT NOT NULL CHECK (required_method IN ('acknowledge', 'typed')),
    method          TEXT NOT NULL CHECK (method IN ('acknowledge', 'typed')),
    policy          TEXT NOT NULL,
    source          TEXT NOT NULL CHECK (source = 'unauthenticated_request'),
    satisfied_at    TEXT NOT NULL,
    consumed_at     TEXT,
    consumed_by_attempt_id TEXT
) STRICT;

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

-- ---------------------------------------------------------------------------------------
-- ARMING
--
-- The checkpoint is the durable rollback material, and it exists before the boundary or the
-- boundary is not crossed. "Recovery is armed" means this row is on disk — not that a running
-- process knows the previous value, which is what every build before this one could have
-- claimed and none of them did.
--
-- `execution_correlation` is JSON and opaque to the Change engine on purpose: it is the
-- concrete operation's business what identifies its target — an interface index and the
-- kernel's own name for it, here — and the engine that arms, dispatches and rolls back must
-- not learn what kind of thing it is moving.
--
-- THE CHECK THAT MATTERS MOST IS THE LAST ONE. A checkpoint may only be armed for a target
-- *proven not* to carry the operator's management path. Not "proven and judged fine", not
-- "unknown". Guarded mutation of the management path is a real product capability, but this
-- build does not have, so the store refuses to arm one at all — which makes the block
-- structural rather than a policy somebody could relax by editing an evaluator.
CREATE TABLE run_checkpoints (
    checkpoint_id TEXT NOT NULL PRIMARY KEY,
    run_id        TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
    preview_id    TEXT NOT NULL REFERENCES run_previews(preview_id),
    host_id       TEXT NOT NULL REFERENCES hosts(host_id),
    object_id     TEXT NOT NULL REFERENCES objects(object_id),

    intent_id      TEXT NOT NULL REFERENCES intents(intent_id),
    intent_version INTEGER NOT NULL CHECK (intent_version >= 1),

    field         TEXT NOT NULL,
    value_type    TEXT NOT NULL CHECK (value_type IN ('boolean', 'integer')),
    before_value  INTEGER NOT NULL,
    desired_value INTEGER NOT NULL,

    observation_id TEXT NOT NULL REFERENCES observations(observation_id),
    observed_at    TEXT NOT NULL,

    protection_management_path TEXT NOT NULL
        CHECK (protection_management_path = 'not_on_management_path'),
    protection_evidence_id TEXT REFERENCES management_path_observations(observation_id),

    execution_correlation TEXT NOT NULL DEFAULT '{}',
    armed_at TEXT NOT NULL,

    CHECK (before_value <> desired_value)
) STRICT;

CREATE TRIGGER run_checkpoints_are_immutable
BEFORE UPDATE ON run_checkpoints
BEGIN
    SELECT RAISE(ABORT, 'a checkpoint is the material recovery rests on and is immutable');
END;

-- ---------------------------------------------------------------------------------------
-- THE CHANGE
--
-- Created at the boundary with almost every column at its default, and updated three times at
-- most: the dispatch marker, the mutation result, and the settlement that ends it.
CREATE TABLE changes (
    change_id     TEXT NOT NULL PRIMARY KEY,
    run_id        TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
    preview_id    TEXT NOT NULL REFERENCES run_previews(preview_id),
    checkpoint_id TEXT NOT NULL REFERENCES run_checkpoints(checkpoint_id),
    host_id       TEXT NOT NULL REFERENCES hosts(host_id),
    object_id     TEXT NOT NULL REFERENCES objects(object_id),
    operation     TEXT NOT NULL CHECK (operation IN ('network.interface.reconcile_mtu')),

    field         TEXT NOT NULL,
    value_type    TEXT NOT NULL CHECK (value_type IN ('boolean', 'integer')),
    before_value  INTEGER NOT NULL,
    desired_value INTEGER NOT NULL,

    -- The write-boundary time: when LocalPlane stopped deciding and started acting.
    created_at TEXT NOT NULL,

    -- THE APPLY. `apply_attempt_id` is the stable identity of this dispatch, carried to the
    -- privileged helper so a retry is recognisable as the same attempt rather than dispatched
    -- twice. UNIQUE, because two Changes sharing one attempt id would make that meaningless.
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

    -- VERIFICATION: a fresh reading through the ordinary observation path, or the typed reason
    -- there is none. A proof must name the reading that proved it.
    verification_outcome TEXT NOT NULL DEFAULT 'not_attempted' CHECK (verification_outcome IN (
        'not_attempted', 'verified', 'mismatch', 'value_unreadable',
        'observation_unavailable', 'source_incompatible', 'target_absent'
    )),
    verification_observation_id TEXT REFERENCES observations(observation_id),
    verification_observed_value INTEGER,
    verification_reason TEXT,

    -- ROLLBACK, through the same privileged typed path. Its write has its own three-valued
    -- outcome, because an interrupted rollback is not a rollback and an acknowledged one is
    -- not a restoration until something reads it back.
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

    CHECK (before_value <> desired_value),

    -- A claim that the host was written — or may have been — requires a dispatch to have
    -- begun. `not_written` is exempt, and the exemption is the point: it is provable
    -- *because* nothing was sent, which is the state a death before the dispatch marker
    -- leaves behind and the one recovery records afterwards. The converse is the crash window
    -- and is deliberately allowed: dispatch began, nothing settled it.
    CHECK (
        mutation_outcome IS NULL
        OR mutation_outcome = 'not_written'
        OR dispatch_began_at IS NOT NULL
    ),

    -- The record may claim exactly what the outcome supports, and nothing more.
    CHECK (
        (mutation_outcome IS NULL AND host_effect = 'none')
        OR (mutation_outcome = 'not_written' AND host_effect = 'none')
        OR (mutation_outcome = 'written' AND host_effect = 'written')
        OR (mutation_outcome = 'write_unknown' AND host_effect = 'write_unknown')
    ),

    -- The three words that have to be earned. 'failed' is the most important: it is permitted
    -- only where nothing was written and that is provable.
    CHECK (result <> 'failed' OR (mutation_outcome = 'not_written' AND host_effect = 'none')),
    CHECK (result <> 'succeeded'
           OR (mutation_outcome = 'written' AND verification_outcome = 'verified')),
    CHECK (result <> 'rolled_back' OR rollback_verification_outcome = 'verified'),
    CHECK (result <> 'recovery_required' OR recovery_reason IS NOT NULL),

    -- And a proof names the reading that proved it.
    CHECK (verification_outcome <> 'verified' OR verification_observation_id IS NOT NULL),
    CHECK (rollback_verification_outcome <> 'verified'
           OR rollback_verification_observation_id IS NOT NULL)
) STRICT;

CREATE INDEX idx_changes_host_created ON changes(host_id, created_at DESC);

CREATE INDEX idx_changes_object_created ON changes(object_id, created_at DESC);

CREATE TRIGGER changes_identity_is_immutable
BEFORE UPDATE OF change_id, run_id, preview_id, checkpoint_id, host_id, object_id,
                 operation, field, value_type, before_value, desired_value, created_at,
                 apply_attempt_id
ON changes
BEGIN
    SELECT RAISE(ABORT, 'what a change was about is immutable; only what became of it moves');
END;

-- No Change without a consumed confirmation for the same Run and the same published plan.
-- Every operation this build can execute requires confirmation, so this holds universally
-- here; an operation that policy exempted would need this trigger revisited in the migration
-- that introduced it, which is exactly the review such an operation deserves.
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

-- ---------------------------------------------------------------------------------------
-- THE TRANSCRIPT
--
-- 0005 said: "when `arming` becomes reachable there will be transitions worth recording per
-- se, and that is the migration that should add it". This is that migration.
--
-- Append-only and typed. `event` is a closed vocabulary and a free-text log is not an
-- acceptable substitute: prose is for a reader, and what a machine branches on has to be a
-- name somebody chose. `change_id` is nullable because the transcript begins before the
-- boundary — a Run planned, confirmed and then cancelled has a complete history and no
-- Change.
CREATE TABLE run_events (
    event_id  TEXT NOT NULL PRIMARY KEY,
    run_id    TEXT NOT NULL REFERENCES runs(run_id),
    change_id TEXT REFERENCES changes(change_id),
    sequence  INTEGER NOT NULL CHECK (sequence >= 1),
    event     TEXT NOT NULL CHECK (event IN (
        'run_planned', 'run_cancelled',
        'confirmation_satisfied', 'confirmation_required', 'confirmation_consumed',
        'apply_refused',
        'arming_started', 'checkpoint_written', 'arming_failed',
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

CREATE INDEX idx_run_events_run ON run_events(run_id, sequence);

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

-- ---------------------------------------------------------------------------------------
-- SERIALISATION
--
-- One mutating Run at a time per object and controlled field, enforced by the database rather
-- than by a mutex in one process. A process-local lock would serialise one backend and
-- silently permit two; the point of putting it here is that the second attempt fails on a
-- primary key whoever is asking.
--
-- It is deliberately *not* released for a Run that ends in `recovery_required`: the object's
-- state is unproven, and letting a second change start against it would be building on a
-- foundation nobody has checked. That is a domain hold: here it outlives the run as a
-- row.
CREATE TABLE object_write_locks (
    lock_key    TEXT NOT NULL PRIMARY KEY,
    host_id     TEXT NOT NULL REFERENCES hosts(host_id),
    object_id   TEXT NOT NULL REFERENCES objects(object_id),
    field       TEXT NOT NULL,
    run_id      TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
    acquired_at TEXT NOT NULL,
    UNIQUE (object_id, field)
) STRICT;
