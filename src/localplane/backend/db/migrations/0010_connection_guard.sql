-- The connection guard: a change to the object carrying the operator's own path, made
-- survivable rather than refused.
--
-- Until this migration a target proven to carry the management path was blocked outright,
-- and `run_checkpoints.protection_management_path` was CHECKed to the single value
-- `not_on_management_path` so that a build which decided otherwise could not write the row.
-- That constraint said the right thing for a build with no guard. It says the wrong thing
-- now, because the property worth enforcing was never "never arm for the path" — it was
-- **never cross the write boundary against the path with nothing holding a reversal**.
--
-- So the CHECK widens to the two *proven* relations and the statement moves to where it
-- belongs: a trigger on `changes` that refuses a Change whose checkpoint says
-- `on_management_path` unless an armed guard exists for the same run and the same
-- checkpoint. `unknown` remains structurally unstorable on both tables, which is the half
-- of the old rule that must not move an inch — a guard armed against a hazard nobody has
-- established would be dispatching a change LocalPlane cannot justify and then a reversal
-- it cannot justify either.
--
-- localplane:foreign-keys=off
--
-- **Why the directive.** SQLite cannot widen a CHECK in place, and four of the five tables
-- rebuilt here are referenced by others: `runs` by six, `run_previews` by four,
-- `run_checkpoints` by `changes` and by the new `run_guards`, and `run_confirmations` by
-- `run_guards`. The two routes 0004, 0006 and 0008 recorded were re-tested and both still
-- fail — `PRAGMA defer_foreign_keys` leaves the DROP's violation counter set at COMMIT, and
-- `PRAGMA legacy_alter_table` has no effect inside a transaction. The remaining route needs
-- the pragma outside the transaction, which is what the directive asks for.
--
-- **Atomicity is not what is given up.** Every statement below runs inside one `BEGIN
-- IMMEDIATE` together with the row recording this migration, and the engine runs `PRAGMA
-- foreign_key_check` *inside* that transaction before committing. This is the second
-- migration in the repository to declare the directive; 0008 is the first, and a test names
-- both rather than counting them.
--
-- Six things change:
--
--   1. `run_guards` (new) — one row per guarded Run: what was armed, by which agent
--      instance, until when, and — appended exactly once — what became of it;
--   2. `run_checkpoints` rebuilt — the CHECK described above;
--   3. `runs` rebuilt — `guarded` becomes a reachable state, paired by CHECK to the only
--      host effect it can honestly carry;
--   4. `run_previews` rebuilt — `execution_eligibility` gains `guarded`, and the guard
--      section a plan publishes is stored so the digest stays recomputable from the row;
--   5. `run_confirmations` rebuilt — a `typed_statement` column, because a typed
--      confirmation is a thing an operator wrote and a record that only says "typed" has
--      lost the evidence;
--   6. `run_events` rebuilt — six members, because the transcript's vocabulary is closed.
--
-- Four triggers whose *own* tables are not rebuilt are dropped and recreated verbatim,
-- because their bodies read tables that are: the two on `changes` and the two 0009 put on
-- `change_recovery_attempts`. Section 6 adds one genuinely new trigger to `changes`, and it
-- is the one this whole migration exists for.
--
-- `changes` is **not** rebuilt. A guard's reversal is a rollback — it restores the value the
-- checkpoint holds, through the same privileged path, with its own three-valued outcome —
-- so it is recorded in the rollback columns that already exist and mean exactly that. A
-- second set of columns saying the same thing about the same event would be the second
-- source of truth this build keeps refusing to create.
--
-- No historical migration is touched.

------------------------------------------------------------------------------------------
-- 0 — every trigger and index naming a table this migration rebuilds
------------------------------------------------------------------------------------------
--
-- Dropped first and together, because SQLite reparses the schema on `DROP TABLE` and a
-- trigger whose body names a table that has just gone is a parse error — even one about to
-- be recreated against the new table. 0006, 0008 and 0009 learned the same ordering.
--
-- The two `changes` triggers and the two on `change_recovery_attempts` are here although
-- neither of those tables is rebuilt: their bodies read `run_confirmations` and
-- `run_checkpoints`, which are. Both pairs are recreated verbatim in section 8.

DROP TRIGGER recovery_mutation_names_the_authority_spent_on_it_on_insert;

DROP TRIGGER recovery_mutation_names_the_authority_spent_on_it_on_update;

DROP TRIGGER changes_require_a_consumed_confirmation;

DROP TRIGGER changes_checkpoint_belongs_to_run;

DROP TRIGGER run_confirmations_match_the_runs_preview;

DROP TRIGGER run_confirmations_are_consumed_once;

DROP TRIGGER run_checkpoints_are_immutable;

DROP TRIGGER run_previews_are_immutable;

DROP TRIGGER runs_intent_belongs_to_object;

DROP TRIGGER runs_observation_belongs_to_object;

DROP TRIGGER runs_identity_is_immutable;

DROP TRIGGER runs_protection_evidence_belongs_to_host;

DROP TRIGGER run_events_are_append_only;

DROP TRIGGER run_events_are_not_deleted;

DROP INDEX idx_run_events_run;

DROP INDEX idx_runs_host_created;

DROP INDEX idx_runs_object_created;

DROP INDEX one_apply_confirmation_per_run;

DROP INDEX one_outstanding_recovery_confirmation_per_run;

------------------------------------------------------------------------------------------
-- 1 — `run_previews`: a plan may now say that guarded execution is the only write path
------------------------------------------------------------------------------------------
--
-- `execution_eligibility` gains a third value. It is a third value rather than a flavour of
-- one of the first two because there are three situations: the ordinary path is open, the
-- ordinary path is closed and the guarded one is open, and nothing is open. Collapsing the
-- middle case into `eligible` would say an ordinary apply may proceed against the object
-- carrying the operator's connection; collapsing it into `blocked` would say the work
-- cannot be done at all.
--
-- The guard section is stored in full for the reason every other section is: the digest
-- covers it, and a digest that had to be re-derived through today's planner would be
-- repeatable rather than content-addressed.

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
        CHECK (execution_eligibility IN ('eligible', 'guarded', 'blocked')),
    execution_blockers     TEXT NOT NULL DEFAULT '[]',
    execution_provider     TEXT,
    required_capability    TEXT NOT NULL,
    capability_declared    INTEGER NOT NULL CHECK (capability_declared IN (0, 1)),

    -- THE CONNECTION GUARD, as the plan describes it. Nothing here is armed, and the CHECK
    -- says so in the same way `recovery_armed` does: a published plan states what arming
    -- would establish, and a document that could claim a live guard would be a document an
    -- operator could read as protection that does not exist yet.
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
    CHECK (protection_evidence_id IS NULL OR protection_evidence_observed_at IS NOT NULL),

    -- Guarded eligibility means exactly one thing, and the store says so rather than
    -- trusting the planner: the guard is available, and the target is proven to be the
    -- management path. A plan that claimed the guarded path against a target proven *not*
    -- to be the path would be offering a mechanism against no hazard; one that claimed it
    -- against an unresolved relation is the case this whole product exists to refuse.
    CHECK (execution_eligibility <> 'guarded' OR (
        guard_availability = 'available'
        AND protection_management_path = 'on_management_path'
    )),
    -- And an available guard is only ever offered for that same proven relation.
    CHECK (guard_availability <> 'available'
           OR protection_management_path = 'on_management_path'),
    CHECK (guard_availability <> 'available' OR guard_window_s > 0)
) STRICT;

-- Every existing preview is restated with no guard offered, which is a restatement rather
-- than a decision about history: no build before this one could arm one, so `unavailable`
-- with `guard_not_required` is exactly what those plans said.
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
    guard_availability, guard_reason, guard_window_s, guard_prerequisites, guard_unmet,
    guard_guarantee, guard_armed,
    recovery_mode, recovery_rollback_possible, recovery_armed, recovery_guarantee,
    recovery_reason,
    verification_capability, verification_provider, verification_condition,
    verification_executed, published_at
)
SELECT
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
    'unavailable', 'guard_not_required', 0, '[]', '[]', '', 0,
    recovery_mode, recovery_rollback_possible, recovery_armed, recovery_guarantee,
    recovery_reason,
    verification_capability, verification_provider, verification_condition,
    verification_executed, published_at
FROM run_previews;

DROP TABLE run_previews;

ALTER TABLE run_previews_rebuilt RENAME TO run_previews;

CREATE TRIGGER run_previews_are_immutable
BEFORE UPDATE ON run_previews
BEGIN
    SELECT RAISE(ABORT, 'a published preview is immutable; plan again instead of rewriting');
END;

------------------------------------------------------------------------------------------
-- 2 — `runs`: `guarded` is a state this build can reach
------------------------------------------------------------------------------------------
--
-- It sits between `verifying` and `succeeded` and it means one thing: the change was
-- written and verified against the object carrying the operator's own connection, a
-- host-side guard is holding a reversal, and the outstanding question is whether the
-- operator can still reach LocalPlane at all.
--
-- The pairing CHECK is the important half. `guarded` requires `host_effect = 'written'` —
-- it is only reached after a mutation that was acknowledged *and* proved, so a Run claiming
-- to be guarded while claiming nothing was written, or while claiming the write is unknown,
-- is unstorable. An unknown write is not guarded; it is restored.

CREATE TABLE runs_rebuilt (
    run_id      TEXT NOT NULL PRIMARY KEY,
    host_id     TEXT NOT NULL REFERENCES hosts(host_id),
    object_id   TEXT NOT NULL REFERENCES objects(object_id),
    operation   TEXT NOT NULL CHECK (operation IN (
        'network.interface.reconcile_mtu',
        'docker.container.start', 'docker.container.stop', 'docker.container.restart'
    )),
    state       TEXT NOT NULL CHECK (state IN (
        'preview', 'awaiting_confirmation', 'arming', 'applying', 'verifying', 'guarded',
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
        'applying', 'verifying', 'guarded', 'succeeded', 'rolling_back',
        'rollback_verifying', 'rolled_back', 'recovery_required'
    )),
    CHECK (state <> 'succeeded' OR host_effect = 'written'),
    CHECK (state <> 'guarded' OR host_effect = 'written'),
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

CREATE INDEX idx_runs_host_created ON runs(host_id, created_at DESC);

CREATE INDEX idx_runs_object_created ON runs(object_id, created_at DESC);

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

------------------------------------------------------------------------------------------
-- 3 — `run_confirmations`: what an operator typed is evidence, so it is stored
------------------------------------------------------------------------------------------
--
-- A guarded apply requires the `typed` method the policy has demanded for a management-path
-- target since 0005, and which nothing could satisfy until now. The operator types
-- the name of the thing at risk; here it is the object's display name under the identity
-- this build holds, and the string is **stored as the operator's statement** rather than
-- merely compared and discarded — the same decision `recoveryResolve` made in 0009, for the
-- same reason. A record that says only "typed" has kept the ceremony and lost the evidence.
--
-- The two CHECKs are an equivalence: a typed confirmation carries a statement, and an
-- acknowledgement carries none. Nothing else about this table moves — the purposes, the two
-- partial unique indexes, the `source` CHECK, both triggers and the absence of any token
-- are recreated exactly.

CREATE TABLE run_confirmations_rebuilt (
    confirmation_id TEXT NOT NULL PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(run_id),

    -- What this authority is for. An apply confirmation authorises crossing the write
    -- boundary once; a recovery confirmation authorises one recovery retry to dispatch a new
    -- mutation for a Change that already crossed it and could not prove what it left behind.
    -- They are the same act — a person saying yes to a write that may happen — against two
    -- different documents, and a column saying which is what keeps one from being read as
    -- the other.
    purpose         TEXT NOT NULL CHECK (purpose IN ('apply', 'recovery_retry')),

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

    CHECK ((method = 'typed') = (typed_statement IS NOT NULL))
) STRICT;

INSERT INTO run_confirmations_rebuilt (
    confirmation_id, run_id, purpose, preview_id, preview_digest, digest_version,
    required_method, method, typed_statement, policy, source, satisfied_at, consumed_at,
    consumed_by_attempt_id
)
SELECT
    confirmation_id, run_id, purpose, preview_id, preview_digest, digest_version,
    required_method, method, NULL, policy, source, satisfied_at, consumed_at,
    consumed_by_attempt_id
FROM run_confirmations;

DROP TABLE run_confirmations;

ALTER TABLE run_confirmations_rebuilt RENAME TO run_confirmations;

CREATE UNIQUE INDEX one_apply_confirmation_per_run
    ON run_confirmations (run_id) WHERE purpose = 'apply';

CREATE UNIQUE INDEX one_outstanding_recovery_confirmation_per_run
    ON run_confirmations (run_id) WHERE purpose = 'recovery_retry' AND consumed_at IS NULL;

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
-- 4 — `run_checkpoints`: armed for a proven relation, either one
------------------------------------------------------------------------------------------
--
-- The single-value CHECK becomes a two-value one and `unknown` stays unstorable. What the
-- old constraint actually protected now lives on `changes` (section 6) and is stronger: a
-- checkpoint for a management-path target may exist, and a Change against one may not,
-- unless a guard is armed and holding.

CREATE TABLE run_checkpoints_rebuilt (
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
        CHECK (protection_management_path IN
               ('not_on_management_path', 'on_management_path')),
    protection_evidence_id TEXT REFERENCES management_path_observations(observation_id),

    execution_correlation TEXT NOT NULL DEFAULT '{}',
    armed_at TEXT NOT NULL,

    CHECK (before_value <> desired_value)
) STRICT;

INSERT INTO run_checkpoints_rebuilt (
    checkpoint_id, run_id, preview_id, host_id, object_id, intent_id, intent_version,
    field, value_type, before_value, desired_value, observation_id, observed_at,
    protection_management_path, protection_evidence_id, execution_correlation, armed_at
)
SELECT
    checkpoint_id, run_id, preview_id, host_id, object_id, intent_id, intent_version,
    field, value_type, before_value, desired_value, observation_id, observed_at,
    protection_management_path, protection_evidence_id, execution_correlation, armed_at
FROM run_checkpoints;

DROP TABLE run_checkpoints;

ALTER TABLE run_checkpoints_rebuilt RENAME TO run_checkpoints;

CREATE TRIGGER run_checkpoints_are_immutable
BEFORE UPDATE ON run_checkpoints
BEGIN
    SELECT RAISE(ABORT, 'a checkpoint is the material recovery rests on and is immutable');
END;

------------------------------------------------------------------------------------------
-- 5 — `run_guards`: what is holding a guarded change, and what became of it
------------------------------------------------------------------------------------------
--
-- One row per guarded Run. It is written in two moves and settled once, and the two moves
-- are the crash window made visible rather than closed:
--
--   1. inserted with `arm_began_at` and no `armed_at` — LocalPlane has asked the host side
--      to hold a guard and does not yet know whether it will. **Nothing may be dispatched
--      from here**, and the trigger on `changes` enforces that rather than trusting code;
--   2. updated with `armed_at`, the holder's instance id and the deadline it stated. Only
--      now is a Change permitted against this target;
--   3. settled exactly once, with what the guard actually did.
--
-- **The deadline is the holder's, recorded rather than recomputed.** `expires_at` is what
-- the agent said, on the agent's clock, because that is the clock the reversal will run on.
-- A deadline the backend calculated for itself would be a second answer to a question only
-- one component can answer, and the two would differ by exactly the skew between a
-- container and its host.
--
-- **Facts with a home elsewhere are not restated here.** What the reversal would write, what
-- it would write it to, and the identity material it addresses are all on the checkpoint
-- this row points at; the operation, the target and the plan are on the Run and the preview.
-- What is unique to this row is the guard: who is holding it, until when, and how it ended.
CREATE TABLE run_guards (
    guard_id      TEXT NOT NULL PRIMARY KEY,
    run_id        TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
    preview_id    TEXT NOT NULL REFERENCES run_previews(preview_id),
    checkpoint_id TEXT NOT NULL UNIQUE REFERENCES run_checkpoints(checkpoint_id),
    host_id       TEXT NOT NULL REFERENCES hosts(host_id),
    object_id     TEXT NOT NULL REFERENCES objects(object_id),

    -- WHY there is a guard at all, and it is a single value: a guard exists for a target
    -- *proven* to carry the management path of the request that armed it. Not "unknown",
    -- which would be a mechanism against an unestablished hazard, and not "not on the
    -- path", which would be a mechanism against no hazard.
    protection_management_path TEXT NOT NULL
        CHECK (protection_management_path = 'on_management_path'),
    protection_evidence_id TEXT NOT NULL
        REFERENCES management_path_observations(observation_id),

    -- The typed authority this guarded execution was permitted by. NOT NULL, because a
    -- guarded change is the one case where the policy has always demanded `typed` and
    -- nothing could satisfy it; a guard armed without one would be this build quietly
    -- lowering its own bar.
    confirmation_id TEXT NOT NULL REFERENCES run_confirmations(confirmation_id),

    window_s     INTEGER NOT NULL CHECK (window_s > 0),
    arm_began_at TEXT NOT NULL,
    armed_at     TEXT,
    expires_at   TEXT,
    holder_id    TEXT,

    -- The attempt id the reversal will carry, minted here and handed to the holder, so the
    -- privileged helper's duplicate-dispatch memory recognises it and so the record can
    -- name the write before it happens.
    reversal_attempt_id TEXT NOT NULL UNIQUE,

    -- THE CONNECTION PROOF. A request arrived whose own transport re-established the
    -- management path over this very object. It is the only evidence that keeps a guarded
    -- change, and it is a *different observation* from the one that armed the guard —
    -- taken after the write, over the path the write could have destroyed.
    kept_at          TEXT,
    kept_evidence_id TEXT REFERENCES management_path_observations(observation_id),

    -- THE SETTLEMENT, written once.
    settled_at    TEXT,
    settled_phase TEXT
        CHECK (settled_phase IS NULL
               OR settled_phase IN ('disarmed', 'fired', 'lost', 'unreachable')),
    settled_reason TEXT,
    fired_at       TEXT,
    reversal_outcome TEXT
        CHECK (reversal_outcome IS NULL
               OR reversal_outcome IN ('not_written', 'written', 'write_unknown')),
    reversal_reason TEXT,
    settled_detail  TEXT NOT NULL DEFAULT '{}',

    -- Armed means the holder answered. Three columns move together or not at all, because
    -- a row that named a deadline without a holder, or a holder without a deadline, would
    -- be describing a guard nobody is holding.
    CHECK ((armed_at IS NOT NULL) = (expires_at IS NOT NULL)),
    CHECK ((armed_at IS NOT NULL) = (holder_id IS NOT NULL)),

    -- Settled means a phase, and a phase means settled.
    CHECK ((settled_at IS NOT NULL) = (settled_phase IS NOT NULL)),

    -- A guard that fired attempted a reversal and says when; one that did not, did not.
    -- Stated as equivalences, because a one-way constraint lets a row claim an ending its
    -- own columns contradict.
    CHECK ((settled_phase = 'fired') = (fired_at IS NOT NULL)),
    CHECK ((settled_phase = 'fired') = (reversal_outcome IS NOT NULL)),

    -- A guard that never confirmed its arming cannot have fired: the holder that would
    -- have dispatched the reversal never said it had one.
    CHECK (settled_phase <> 'fired' OR armed_at IS NOT NULL),

    -- The connection proof belongs to a guard that was released because of it. A kept
    -- guard is a disarmed guard, and a disarmed guard that claims no proof is a release
    -- nobody justified.
    CHECK ((kept_at IS NOT NULL) = (kept_evidence_id IS NOT NULL)),
    CHECK (kept_at IS NULL OR settled_phase = 'disarmed'),
    CHECK (kept_at IS NULL OR armed_at IS NOT NULL)
) STRICT;

CREATE INDEX idx_run_guards_object ON run_guards(object_id, arm_began_at DESC);

-- What a guard was *about* never moves. Only what became of it does — the same division
-- `changes` makes, for the same reason.
CREATE TRIGGER run_guards_identity_is_immutable
BEFORE UPDATE OF guard_id, run_id, preview_id, checkpoint_id, host_id, object_id,
                 protection_management_path, protection_evidence_id, confirmation_id,
                 window_s, arm_began_at, reversal_attempt_id
ON run_guards
BEGIN
    SELECT RAISE(ABORT, 'what a connection guard was armed for is immutable');
END;

-- A settled guard is history.
CREATE TRIGGER run_guards_settle_once
BEFORE UPDATE ON run_guards
WHEN OLD.settled_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'a connection guard settles once; what it did is what it did');
END;

CREATE TRIGGER run_guards_are_not_deleted
BEFORE DELETE ON run_guards
BEGIN
    SELECT RAISE(ABORT, 'a connection guard is a record of what protected a change');
END;

-- The authority must be the typed one this Run's own operator gave for this very plan.
-- Four things, each closing a different forgery: it belongs to this Run, it is an apply
-- confirmation rather than a recovery grant, it was actually typed, and it names the plan
-- this guard's Run published.
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

-- A guard may only cite evidence about its own host. A foreign key says an observation
-- exists; it does not say it is an observation of the connection to *this* host, and
-- evidence about another host is not weaker proof that this target carries the path — it is
-- none at all, and it would be the most convincing-looking falsehood the record could hold.
CREATE TRIGGER run_guards_cite_their_own_evidence_on_insert
BEFORE INSERT ON run_guards
WHEN EXISTS (
    SELECT 1 FROM management_path_observations m
    WHERE m.observation_id = NEW.protection_evidence_id AND m.host_id <> NEW.host_id
)
BEGIN
    SELECT RAISE(ABORT, 'a guard''s management-path evidence must be of its own host');
END;

CREATE TRIGGER run_guards_keep_evidence_is_their_own
BEFORE UPDATE OF kept_evidence_id ON run_guards
WHEN NEW.kept_evidence_id IS NOT NULL
 AND EXISTS (
     SELECT 1 FROM management_path_observations m
     WHERE m.observation_id = NEW.kept_evidence_id AND m.host_id <> NEW.host_id
 )
BEGIN
    SELECT RAISE(ABORT, 'the proof that kept a guard must be of the guard''s own host');
END;

------------------------------------------------------------------------------------------
-- 6 — `changes`: the boundary is closed against an unguarded management-path write
------------------------------------------------------------------------------------------
--
-- The statement 0007 made with a CHECK on `run_checkpoints`, now made where it is actually
-- about: **a Change may not come into existence against a target proven to carry the
-- operator's own connection unless a guard is armed and holding a reversal for it.**
--
-- It is a strictly stronger rule than the one it replaces. The old CHECK stopped a
-- checkpoint being armed for the path; it could say nothing about a Change, about a guard,
-- or about whether the guard was actually confirmed by the component that would fire it.
-- This one reads the guard's own `armed_at`, which is only written when the host side has
-- answered — so a guard LocalPlane merely *asked* for does not open the boundary.
--
-- The other two triggers are recreated verbatim; only their tables moved beneath them.

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

CREATE TRIGGER changes_on_the_management_path_require_an_armed_guard
BEFORE INSERT ON changes
WHEN NEW.checkpoint_id IS NOT NULL
 AND EXISTS (
     SELECT 1 FROM run_checkpoints k
     WHERE k.checkpoint_id = NEW.checkpoint_id
       AND k.protection_management_path = 'on_management_path'
 )
 AND NOT EXISTS (
     SELECT 1 FROM run_guards g
     WHERE g.run_id = NEW.run_id
       AND g.checkpoint_id = NEW.checkpoint_id
       AND g.armed_at IS NOT NULL
       AND g.settled_at IS NULL
 )
BEGIN
    SELECT RAISE(ABORT,
        'a change to the object carrying the management path requires an armed connection guard');
END;

------------------------------------------------------------------------------------------
-- 7 — `run_events`: six more members, and every one of them is a fact
------------------------------------------------------------------------------------------
--
-- The guard steps could not borrow the apply path's names. `checkpoint_written` says the
-- material to restore is on disk; `guard_armed` says something on the host has undertaken
-- to use it without being asked again, which is a different and much larger claim.
-- `guard_connection_proved` is the one event in this vocabulary that records evidence
-- LocalPlane could not have gathered by looking at the host.

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
        'guard_armed', 'guard_arming_failed',
        'write_boundary_crossed', 'mutation_dispatched', 'mutation_result',
        'verification_started', 'verification_result',
        'rollback_started', 'rollback_mutation_dispatched', 'rollback_mutation_result',
        'rollback_verification_started', 'rollback_verification_result',
        'guard_hold_started', 'guard_connection_proved', 'guard_keep_refused',
        'guard_settled',
        'recovery_required', 'run_finished',
        'recovery_retry_started', 'recovery_evidence_result',
        'recovery_confirmation_satisfied', 'recovery_confirmation_consumed',
        'recovery_mutation_dispatched', 'recovery_mutation_result',
        'recovery_verification_result', 'recovery_retry_finished',
        'recovery_resolved', 'recovery_hold_released'
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

------------------------------------------------------------------------------------------
-- 8 — 0009's authority triggers, recreated against the rebuilt `run_confirmations`
------------------------------------------------------------------------------------------
--
-- Verbatim. Nothing about what a recovery mutation must name has changed; the table its
-- body reads moved out from under it, which is the only reason they appear here at all.

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
