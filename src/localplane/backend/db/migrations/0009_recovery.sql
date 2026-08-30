-- Recovery completion: a way out of `recovery_required` that does not rewrite what happened.
--
-- Until this migration a Change that ended `recovery_required` held its object's write lock
-- with no endpoint that released it, and the only escape was a human editing this database.
-- Retry and resolution are the two ways out, and both are
-- acts that happen *after* an uncertain execution, so neither may be recorded by moving the
-- original Change's answer.
--
-- **The original record is not touched, and that is the whole shape of this file.** `changes`
-- is not rebuilt, no column of it is widened, and no row of it is updated by anything this
-- migration adds. `result` keeps saying `recovery_required`, `mutation_outcome` keeps saying what
-- the original dispatch produced, and `recovery_reason` keeps saying why LocalPlane could not
-- prove a safe end state. A recovery attempt is a **later, separate event**, and it gets its
-- own append-only row.
--
-- Three things change:
--
--   1. `change_recovery_attempts` (new) — one append-only row per recovery action, retry or
--      resolution, carrying the fresh evidence it rested on, whatever new write it made, and
--      whether it released the hold;
--   2. `run_confirmations` rebuilt — a `purpose` column, so a retry that must write again can
--      be authorised by the confirmation machinery that already exists instead of by a second
--      one built beside it. `UNIQUE (run_id)` becomes two partial unique indexes, which is a
--      restatement of the old guarantee plus one for the new purpose, not a relaxation;
--   3. `run_events` rebuilt — ten members, because the transcript's vocabulary is closed and
--      every member is a fact.
--
-- **No `foreign-keys=off` directive, and it is not needed.** Nothing references
-- `run_confirmations` or `run_events`; both are leaves. `DROP TABLE` on a leaf performs an
-- implicit delete that violates nothing, so foreign keys stay enforced for every statement
-- here and the migration keeps its atomicity without the escape 0008 had to declare. A test
-- asserts exactly one migration in this repository declares it, and this is not that one.
--
-- No historical migration is touched.

------------------------------------------------------------------------------------------
-- 0 — every trigger and index naming a table this migration rebuilds
------------------------------------------------------------------------------------------
--
-- Dropped first and together, because SQLite reparses the schema on `DROP TABLE` and a
-- trigger whose body names a table that has just gone is a parse error — even one about to
-- be recreated against the new table. 0006 and 0008 learned the same ordering.
--
-- `changes_require_a_consumed_confirmation` is in this list although its own table is not
-- rebuilt: its body reads `run_confirmations`, which is.

DROP TRIGGER changes_require_a_consumed_confirmation;

DROP TRIGGER run_confirmations_match_the_runs_preview;

DROP TRIGGER run_confirmations_are_consumed_once;

DROP TRIGGER run_events_are_append_only;

DROP TRIGGER run_events_are_not_deleted;

DROP INDEX idx_run_events_run;

------------------------------------------------------------------------------------------
-- 1 — `run_confirmations`: one table, two purposes, and the same single-use rule for both
------------------------------------------------------------------------------------------
--
-- A retry that dispatches a new mutation is a **new write attempt**, and this build's rule is
-- that a write attempt needs operator authority that has not been used before. The
-- confirmation that authorised the original apply was consumed by it; reusing it would make
-- one confirmation authorise two writes, which is exactly the property 0007 made structural.
--
-- The alternative to this column was a second confirmation table with its own single-use
-- rule, its own immutability trigger and its own idea of what "consumed" means — two places
-- for the most safety-critical rule in the product to drift apart. So the existing table
-- grows a discriminator instead, and every rule it already enforces goes on applying
-- unchanged to both kinds.
--
-- `UNIQUE (run_id)` cannot survive, because a run may now carry an apply confirmation and,
-- later, one or more recovery confirmations. It is replaced by two **partial** unique
-- indexes:
--
--   * one apply confirmation per run — byte-for-byte the guarantee 0007 stated, scoped to the
--     rows it was ever about;
--   * one *outstanding* recovery confirmation per run — a second cannot be created while one
--     is waiting to be used, and a new one becomes possible only once the previous has been
--     consumed by an attempt. Authority is therefore never accumulated.
--
-- What is **not** relaxed: the trigger refusing a confirmation whose preview is not its own
-- run's, the trigger refusing every update to a consumed row, `source` CHECKed to the one
-- value that is true, and the consuming UPDATE's own `consumed_at IS NULL` guard.

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
    policy          TEXT NOT NULL,
    source          TEXT NOT NULL CHECK (source = 'unauthenticated_request'),
    satisfied_at    TEXT NOT NULL,
    consumed_at     TEXT,
    consumed_by_attempt_id TEXT
) STRICT;

-- Every existing row authorised an apply, because that is the only thing a confirmation could
-- authorise before this migration. This is a restatement, not a decision about history.
INSERT INTO run_confirmations_rebuilt (
    confirmation_id, run_id, purpose, preview_id, preview_digest, digest_version,
    required_method, method, policy, source, satisfied_at, consumed_at, consumed_by_attempt_id
)
SELECT
    confirmation_id, run_id, 'apply', preview_id, preview_digest, digest_version,
    required_method, method, policy, source, satisfied_at, consumed_at, consumed_by_attempt_id
FROM run_confirmations;

DROP TABLE run_confirmations;

ALTER TABLE run_confirmations_rebuilt RENAME TO run_confirmations;

CREATE UNIQUE INDEX one_apply_confirmation_per_run
    ON run_confirmations (run_id) WHERE purpose = 'apply';

CREATE UNIQUE INDEX one_outstanding_recovery_confirmation_per_run
    ON run_confirmations (run_id) WHERE purpose = 'recovery_retry' AND consumed_at IS NULL;

------------------------------------------------------------------------------------------
-- 2 — `change_recovery_attempts`: what happened *after* the Change could not be settled
------------------------------------------------------------------------------------------
--
-- One row per recovery action against one Change, numbered, append-only, never deleted. The
-- table exists because the alternatives are both falsehoods: updating the Change would make
-- it say something other than what happened, and creating a second Change would claim the
-- Run crossed the boundary twice.
--
-- **Two kinds, and the CHECKs keep them apart.**
--
--   * `retry` re-attempts the *original* operation's required end state. It always begins by
--     asking whether a fresh reading already proves that end state — because a retry that
--     writes when nothing needed writing is a host mutation nobody asked for — and it
--     dispatches a new mutation only if it cannot be proven, the operation is still
--     applicable, and fresh operator authority exists.
--   * `resolve` is a person saying they have dealt with the situation. It performs **no host
--     mutation**, and that is a CHECK rather than a code path: every mutation column must be
--     null, `host_effect` must be `none`, and no confirmation may be named. A build that
--     decided a resolution could write would fail to store the row.
--
-- **What the row may not claim.** `proven` requires the reading that proved it. `verified`
-- requires a write that was acknowledged *and* a reading that proved the result. `refused`
-- requires that nothing was dispatched and nothing could have been written. And a new
-- mutation attempt requires a confirmation — `mutation_attempt_id IS NULL OR confirmation_id
-- IS NOT NULL` is the structural form of "a retry that writes is a new write attempt".
--
-- **The hold is released in exactly three ways and no others**, and `releases_hold` is
-- CHECKed to them. A partial unique index allows one releasing row per Change, so a retry
-- that completes and a resolution cannot both release the same hold, and two resolutions
-- cannot either.

CREATE TABLE change_recovery_attempts (
    attempt_id TEXT NOT NULL PRIMARY KEY,
    change_id  TEXT NOT NULL REFERENCES changes(change_id),
    run_id     TEXT NOT NULL REFERENCES runs(run_id),
    host_id    TEXT NOT NULL REFERENCES hosts(host_id),
    object_id  TEXT NOT NULL REFERENCES objects(object_id),
    sequence   INTEGER NOT NULL CHECK (sequence >= 1),

    kind       TEXT NOT NULL CHECK (kind IN ('retry', 'resolve')),
    started_at TEXT NOT NULL,

    -- The management-path evidence belonging to *this* operator request, never the one the
    -- original Run rested on. Recovery happens later, over a different connection, and
    -- evidence about where an operator was reaching LocalPlane from an hour ago is not
    -- evidence about where they are reaching it from now.
    protection_management_path TEXT NOT NULL CHECK (
        protection_management_path IN
            ('on_management_path', 'not_on_management_path', 'unknown')
    ),
    protection_evidence_id TEXT REFERENCES management_path_observations(observation_id),

    -- The fresh reading this attempt rested on, taken through the ordinary observation path,
    -- and what the *operation* made of it. `verified` here means the reading proved the
    -- original operation's required end state — which for a retry is the whole question, and
    -- for a resolution is a fact recorded beside the human's decision and never instead of it.
    evidence_outcome TEXT NOT NULL DEFAULT 'not_attempted' CHECK (evidence_outcome IN (
        'not_attempted', 'verified', 'mismatch', 'value_unreadable',
        'observation_unavailable', 'source_incompatible', 'target_absent'
    )),
    evidence_observation_id TEXT REFERENCES observations(observation_id),
    evidence_observed_value INTEGER,
    evidence_observed_state TEXT,
    evidence_reason         TEXT,

    -- A new write attempt, where one happened. Retries only.
    confirmation_id       TEXT REFERENCES run_confirmations(confirmation_id),
    execution_correlation TEXT,
    mutation_attempt_id   TEXT UNIQUE,
    dispatch_began_at     TEXT,
    mutation_outcome      TEXT CHECK (
        mutation_outcome IS NULL
        OR mutation_outcome IN ('not_written', 'written', 'write_unknown')
    ),
    mutation_reason   TEXT,
    mutation_provider TEXT,
    mutation_method   TEXT,
    mutation_detail   TEXT NOT NULL DEFAULT '{}',

    -- What *this attempt* did to the host. The Change's own `host_effect` still answers what
    -- the original attempt did, and the two are deliberately separate columns on separate
    -- rows: a Change that wrote once and whose retry wrote again has two host effects, and
    -- collapsing them into one would lose which was which.
    host_effect TEXT NOT NULL DEFAULT 'none'
        CHECK (host_effect IN ('none', 'written', 'write_unknown')),

    -- The reading taken *after* that write. Distinct from `evidence_*`, which was taken
    -- before it: "the end state was already there" and "the end state is there because we
    -- wrote it again" are different findings and a single column could not tell them apart.
    verification_outcome TEXT NOT NULL DEFAULT 'not_attempted' CHECK (verification_outcome IN (
        'not_attempted', 'verified', 'mismatch', 'value_unreadable',
        'observation_unavailable', 'source_incompatible', 'target_absent'
    )),
    verification_observation_id TEXT REFERENCES observations(observation_id),
    verification_observed_value INTEGER,
    verification_observed_state TEXT,
    verification_reason         TEXT,

    outcome TEXT NOT NULL DEFAULT 'in_flight' CHECK (outcome IN (
        'in_flight',
        'proven',        -- a fresh reading proved the original end state; nothing was written
        'verified',      -- a new mutation was written and a fresh reading proved the result
        'not_written',   -- a new mutation was dispatched and provably wrote nothing
        'write_unknown', -- a new mutation may have landed and nothing can say
        'not_proven',    -- written, and the reading afterwards did not prove the end state
        'refused',       -- nothing was dispatched; provably no new host effect
        'interrupted',   -- the process died during the attempt; settled on restart
        'resolved'       -- a person released the hold
    )),
    refusal_code  TEXT,
    releases_hold INTEGER NOT NULL DEFAULT 0 CHECK (releases_hold IN (0, 1)),
    finished_at   TEXT,

    -- A resolution's own two facts: what the operator typed to make the act deliberate, and
    -- whatever they wanted recorded beside it. The operator types the held domain's
    -- name for the same reason — an escape from a safety hold should not be one accidental
    -- click — and it records the statement as activity rather than as a change.
    operator_statement TEXT,
    note               TEXT,

    UNIQUE (change_id, sequence),

    -- A resolution performs no host mutation. Structural, not a code path nobody took.
    CHECK (kind <> 'resolve' OR (
        confirmation_id IS NULL
        AND execution_correlation IS NULL
        AND mutation_attempt_id IS NULL
        AND dispatch_began_at IS NULL
        AND mutation_outcome IS NULL
        AND mutation_reason IS NULL
        AND mutation_provider IS NULL
        AND mutation_method IS NULL
        AND host_effect = 'none'
        AND verification_outcome = 'not_attempted'
        AND verification_observation_id IS NULL
        AND verification_observed_value IS NULL
        AND verification_observed_state IS NULL
        AND outcome IN ('resolved')
        AND operator_statement IS NOT NULL
    )),
    -- A retry is not a person's statement and does not carry one.
    CHECK (kind <> 'retry' OR (operator_statement IS NULL AND note IS NULL)),

    -- `resolved` is a resolution's ending and nothing else's. The clause above says a
    -- resolution ends `resolved`; this says the converse, so the two cannot come apart and a
    -- retry cannot borrow the word for an ending it did not reach.
    CHECK (outcome <> 'resolved' OR kind = 'resolve'),

    -- THE PHASES OF A WRITE ATTEMPT MOVE TOGETHER OR NOT AT ALL.
    --
    -- Authority, the identifier of the attempt it authorised, the material used to reach the
    -- target and the dispatch marker are written in one transaction, before the provider is
    -- called. Any row where one of them exists without the others describes a sequence that
    -- did not happen, so none of them is allowed to appear alone.
    --
    -- The crash window is unchanged and is the reason the marker is in this set rather than
    -- after it: a row may say a dispatch began and not say what became of it. What it may not
    -- say is that something became of a dispatch that never began.
    CHECK ((mutation_attempt_id IS NULL) = (confirmation_id IS NULL)),
    CHECK ((mutation_attempt_id IS NULL) = (dispatch_began_at IS NULL)),
    CHECK ((mutation_attempt_id IS NULL) = (execution_correlation IS NULL)),
    CHECK (mutation_outcome IS NULL OR mutation_attempt_id IS NOT NULL),
    CHECK (verification_outcome = 'not_attempted' OR mutation_outcome IS NOT NULL),

    CHECK (
        (mutation_outcome IS NULL AND host_effect = 'none')
        OR (mutation_outcome = 'not_written' AND host_effect = 'none')
        OR (mutation_outcome = 'written' AND host_effect = 'written')
        OR (mutation_outcome = 'write_unknown' AND host_effect = 'write_unknown')
    ),

    -- THE OUTCOME AND WHAT PRODUCED IT AGREE, IN BOTH DIRECTIONS WHERE THE DOMAIN SAYS SO.
    --
    -- One-way constraints let a row claim an ending its own columns contradict. `not_written`
    -- describes a dispatch the execution path proved wrote nothing, so the row must say that;
    -- `write_unknown` describes one that may have landed, so the row must say that too; and
    -- `not_proven` describes a write that happened and a reading afterwards that did not
    -- prove it — which needs the write *and* a verification that was attempted and did not
    -- succeed. Nothing else may wear any of the three.
    CHECK (outcome <> 'proven' OR (
        evidence_outcome = 'verified' AND evidence_observation_id IS NOT NULL
        AND mutation_outcome IS NULL AND host_effect = 'none'
    )),
    CHECK (outcome <> 'verified' OR (
        mutation_outcome = 'written'
        AND verification_outcome = 'verified' AND verification_observation_id IS NOT NULL
    )),
    CHECK (outcome <> 'not_written' OR mutation_outcome = 'not_written'),
    CHECK (outcome <> 'write_unknown' OR mutation_outcome = 'write_unknown'),
    CHECK (outcome <> 'not_proven' OR (
        mutation_outcome = 'written'
        AND verification_outcome <> 'not_attempted'
        AND verification_outcome <> 'verified'
    )),
    CHECK (outcome <> 'refused' OR (
        mutation_attempt_id IS NULL
        AND mutation_outcome IS NULL AND host_effect = 'none' AND refusal_code IS NOT NULL
    )),

    -- The hold is released by exactly three outcomes: not "at most", not "at least". Stated
    -- as an equivalence, because a `proven`, `verified` or `resolved` attempt that did *not*
    -- release is as impossible as a `refused` one that did, and one-way phrasing permitted
    -- the first.
    CHECK (releases_hold = (
        CASE WHEN outcome IN ('proven', 'verified', 'resolved') THEN 1 ELSE 0 END
    )),

    -- An attempt is finished or it is not, and the two columns must agree.
    CHECK ((outcome = 'in_flight') = (finished_at IS NULL))
) STRICT;

CREATE INDEX idx_recovery_attempts_change
    ON change_recovery_attempts (change_id, sequence);

-- One recovery action at a time per Change. Two concurrent retries cannot both dispatch,
-- and a resolution cannot slip past one that is in flight, because the second insert fails
-- on this index rather than on a check somebody remembered to write.
CREATE UNIQUE INDEX one_recovery_attempt_in_flight_per_change
    ON change_recovery_attempts (change_id) WHERE outcome = 'in_flight';

-- One release per hold, ever. A retry that completes and a resolution cannot both release
-- the same hold, and two resolutions cannot either.
CREATE UNIQUE INDEX one_release_per_recovery_hold
    ON change_recovery_attempts (change_id) WHERE releases_hold = 1;

-- Only a Change that actually ended in recovery may have a recovery attempt. A CHECK cannot
-- read another table, so this is a trigger.
CREATE TRIGGER recovery_attempts_belong_to_a_recovery_hold
BEFORE INSERT ON change_recovery_attempts
WHEN NOT EXISTS (
    SELECT 1 FROM changes c
    WHERE c.change_id = NEW.change_id
      AND c.run_id = NEW.run_id
      AND c.host_id = NEW.host_id
      AND c.object_id = NEW.object_id
      AND c.result = 'recovery_required'
)
BEGIN
    SELECT RAISE(ABORT, 'a recovery attempt belongs to its own change, which must have ended in recovery_required');
END;

-- THE EVIDENCE A RECOVERY RESTS ON MUST BE EVIDENCE ABOUT THE THING BEING RECOVERED.
--
-- A foreign key only says the observation exists. It does not say it is an observation of
-- *this* object on *this* host — and a reading of some other object is not weaker evidence
-- for this one, it is no evidence at all. The whole point of `proven` is that a reading
-- established the end state; a reading of a different object establishing a different
-- object's state would be the most convincing-looking falsehood the record could hold.
--
-- The same for the management-path evidence, and for the same reason `run_previews` already
-- has `runs_protection_evidence_belongs_to_host`.
CREATE TRIGGER recovery_attempts_cite_their_own_evidence_on_insert
BEFORE INSERT ON change_recovery_attempts
WHEN (NEW.evidence_observation_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM observations o WHERE o.observation_id = NEW.evidence_observation_id
          AND o.object_id = NEW.object_id AND o.host_id = NEW.host_id))
  OR (NEW.verification_observation_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM observations o WHERE o.observation_id = NEW.verification_observation_id
          AND o.object_id = NEW.object_id AND o.host_id = NEW.host_id))
  OR (NEW.protection_evidence_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM management_path_observations m
        WHERE m.observation_id = NEW.protection_evidence_id AND m.host_id = NEW.host_id))
BEGIN
    SELECT RAISE(ABORT, 'a recovery attempt may only cite evidence about its own object and host');
END;

CREATE TRIGGER recovery_attempts_cite_their_own_evidence_on_update
BEFORE UPDATE ON change_recovery_attempts
WHEN (NEW.evidence_observation_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM observations o WHERE o.observation_id = NEW.evidence_observation_id
          AND o.object_id = NEW.object_id AND o.host_id = NEW.host_id))
  OR (NEW.verification_observation_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM observations o WHERE o.observation_id = NEW.verification_observation_id
          AND o.object_id = NEW.object_id AND o.host_id = NEW.host_id))
BEGIN
    SELECT RAISE(ABORT, 'a recovery attempt may only cite evidence about its own object and host');
END;

-- A RECOVERY MUTATION MAY ONLY NAME THE AUTHORITY THAT WAS SPENT ON IT.
--
-- The service consumes a fresh `recovery_retry` confirmation and records the attempt id it
-- was consumed by, in one transaction, before the provider is called. That is the correct
-- sequence; this is what makes it the *only* storable one. A CHECK cannot read another
-- table, so it is a trigger, and it fires on UPDATE as well as INSERT because the attempt
-- row exists before its mutation fields do.
--
-- Four things are required of the named confirmation, and each closes a different forgery:
-- it belongs to this Run, so authority cannot be borrowed from another Change; its purpose
-- is `recovery_retry`, so the confirmation the original apply consumed cannot be reused;
-- it is consumed, so an outstanding grant cannot be spent twice; and it was consumed *by
-- this very attempt*, which is what stops one consumed grant from covering a second write.
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

-- What an attempt *was* is frozen; only what became of it moves.
CREATE TRIGGER recovery_attempts_identity_is_immutable
BEFORE UPDATE ON change_recovery_attempts
WHEN OLD.change_id <> NEW.change_id
  OR OLD.run_id <> NEW.run_id
  OR OLD.host_id <> NEW.host_id
  OR OLD.object_id <> NEW.object_id
  OR OLD.sequence <> NEW.sequence
  OR OLD.kind <> NEW.kind
  OR OLD.started_at <> NEW.started_at
  OR OLD.protection_management_path <> NEW.protection_management_path
  OR OLD.protection_evidence_id IS NOT NEW.protection_evidence_id
BEGIN
    SELECT RAISE(ABORT, 'what a recovery attempt was about is immutable');
END;

-- An attempt that has finished has finished. Its outcome is the record of what happened.
CREATE TRIGGER recovery_attempts_settle_once
BEFORE UPDATE ON change_recovery_attempts
WHEN OLD.outcome <> 'in_flight'
BEGIN
    SELECT RAISE(ABORT, 'a settled recovery attempt is what happened and does not move');
END;

CREATE TRIGGER recovery_attempts_are_not_deleted
BEFORE DELETE ON change_recovery_attempts
BEGIN
    SELECT RAISE(ABORT, 'recovery history is append-only; attempts are not removed');
END;

-- Only the Run that owns the hold may release it, and only while it still owns it. The lock
-- row is the hold, so the release is written while the row it releases is still there — which
-- is what makes "this attempt released this object" checkable afterwards rather than inferred
-- from an absence.
CREATE TRIGGER recovery_release_requires_the_hold_on_insert
BEFORE INSERT ON change_recovery_attempts
WHEN NEW.releases_hold = 1 AND NOT EXISTS (
    SELECT 1 FROM object_write_locks l
    WHERE l.run_id = NEW.run_id AND l.object_id = NEW.object_id
)
BEGIN
    SELECT RAISE(ABORT, 'only the run holding this object may release it');
END;

CREATE TRIGGER recovery_release_requires_the_hold_on_update
BEFORE UPDATE ON change_recovery_attempts
WHEN NEW.releases_hold = 1 AND OLD.releases_hold = 0 AND NOT EXISTS (
    SELECT 1 FROM object_write_locks l
    WHERE l.run_id = NEW.run_id AND l.object_id = NEW.object_id
)
BEGIN
    SELECT RAISE(ABORT, 'only the run holding this object may release it');
END;

------------------------------------------------------------------------------------------
-- 3 — `run_events`: the ten steps recovery takes
------------------------------------------------------------------------------------------
--
-- The transcript's vocabulary is closed and every member is a fact, so a step that happens
-- needs a name of its own rather than the nearest existing one. 0008 made the same argument
-- for two members and paid the same price: a rebuild of a leaf table.
--
-- The recovery steps could not borrow the apply path's names. `mutation_dispatched` on a Run
-- that finished an hour ago would read as a second apply of the same plan; `verification_
-- result` would read as the verification of the original write. A reader reconstructing what
-- happened has to be able to see that these events came after the Run ended, and the names
-- are how.
--
-- `recovery_hold_released` is separate from `recovery_retry_finished` and
-- `recovery_resolved` on purpose: the release is the fact an operator most needs to find, it
-- can arrive from either of two different actions, and an event that only ever appears
-- inside one of them would be a fact hidden behind a name that does not mention it.

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

------------------------------------------------------------------------------------------
-- 4 — the indexes and triggers, recreated
------------------------------------------------------------------------------------------
--
-- Verbatim apart from one change, argued for below.

CREATE INDEX idx_run_events_run ON run_events (run_id, sequence);

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

-- The one change: scoped to `purpose = 'apply'`. A recovery confirmation authorises a retry
-- of a Change that already exists; it must never be what lets a Change be inserted, and
-- saying so here costs one predicate and closes the question permanently.
--
-- Recreated **last**, and the order is deliberate: SQLite fires `BEFORE INSERT` triggers in
-- reverse creation order, so this is the rule a forged row is refused by first — and of the
-- rules guarding that insert, a change nobody authorised is the one worth reporting.
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
