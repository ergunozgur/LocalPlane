-- The Change Engine's pre-write foundation: a Run, and the immutable plan it published.
--
-- Two tables:
--
--   run_previews  the plan that was published, exactly as it was published
--   runs          the operator's request, its target, and what became of it
--
-- and nothing added to any existing table. Every omission below is deliberate.
--
-- THERE IS NO `changes` TABLE, AND THAT IS THE POINT
--
-- A Run is not a Change. A Run is somebody asking what it would take to reconcile an
-- object and whether doing so would be safe; a Change is the record that LocalPlane wrote
-- to a host. The two are separated by the first possible write boundary, and this build
-- does not reach it: there is no mutating agent method, no privileged helper, no executor.
--
-- So there is no `changes` table, and no `change_id` column pointing at one. An empty
-- table named `changes` would say that LocalPlane has a place to record host writes and
-- has not used it yet, which is a promise. Having none says the true thing: this build
-- cannot write, so it cannot have written. The work that crosses the boundary brings the
-- record with it, with the obligations that come with it — a confirmation that was
-- actually satisfied, an apply transcript, a verification result, a guard — and adds the
-- link from the Run at the moment the first write is attempted.
--
-- `runs.host_effect` is CHECKed to 'none' for the same reason it is on
-- `management_transitions` and `intent_revisions`: three tables now record events on
-- LocalPlane's own records, and not one of them is structurally capable of claiming a
-- host write.
--
-- THERE IS NO `run_events` TABLE
--
-- A run state history is worth having when there are states to have a history of. There
-- are two here, and the row already answers every question a reader would ask of an event
-- log: when the Run was created (`created_at`), which plan it published (`preview_id`,
-- and the preview is immutable), whether it was cancelled (`state`, `cancelled_at`), and
-- whether host execution ever began — which `host_effect` and the state CHECK answer
-- structurally, and the answer is no. A table restating those would be a second copy of
-- facts that already have one home. When `arming` becomes reachable there will be
-- transitions worth recording per se, and that is the migration that should add it.
--
-- WHY THE PLAN IS A SEPARATE TABLE FROM THE RUN
--
-- Because one of them changes and the other must not. A Run is cancelled; a published
-- plan is what an operator was shown, and rewriting it would make the record of a decision
-- into a record of the current opinion. Splitting them lets the plan be immutable against
-- any writer — enforced by a trigger below, not by whoever remembered — while the Run
-- keeps the small mutable lifecycle it needs.
--
-- The pointer runs one way: the Run names its preview, the preview names no Run. That
-- makes a preview publishable before the Run that publishes it (they land in one
-- transaction either way), and it keeps the immutable table free of a column that would
-- have to be filled in afterwards.

-- One published plan. Written once, never updated, never deleted while a Run names it.
--
-- Every column here is either part of the plan's content-addressed identity or evidence
-- for how it was reached. The identity — `preview_digest` — is a hash of the canonical
-- form of the former, so a future apply can prove the plan it is about to run is the plan
-- an operator confirmed, rather than assuming it. Everything the digest covers is stored,
-- so recomputing it needs the row and nothing else: a digest that has to be re-derived
-- through today's code is not content-addressed, it is merely repeatable.
--
-- Five CHECKs make five possible fabrications structurally impossible:
-- execution cannot be recorded as available, eligibility cannot be recorded as eligible,
-- recovery cannot be recorded as armed, the management path cannot be recorded as known,
-- and no confirmation token can be recorded as issued. None of them is a rule this code
-- has to remember; they are what the store will accept.
CREATE TABLE run_previews (
    preview_id     TEXT    NOT NULL PRIMARY KEY,
    -- 'sha256:<hex>'. Not UNIQUE: two operators planning the same reconciliation a second
    -- apart produce two truthful snapshots of one plan, and equal content having equal
    -- identity is what content-addressing means. What must not happen is one silently
    -- replacing the other, and that is prevented by their being separate rows.
    preview_digest TEXT    NOT NULL,
    digest_version INTEGER NOT NULL CHECK (digest_version >= 1),
    operation      TEXT    NOT NULL CHECK (operation IN ('network.interface.reconcile_mtu')),

    -- WHAT WOULD CHANGE. One controlled field, typed, with both ends stated. Scoped to a
    -- single field for the same reason a drift finding is: its evidence is one typed
    -- comparison and every part of it is a column. An operation that moves several fields
    -- at once needs its own representation, and that is a decision for the migration that
    -- introduces it.
    field          TEXT    NOT NULL,
    value_type     TEXT    NOT NULL CHECK (value_type IN ('boolean', 'integer')),
    -- The two ends are CHECKed against each other at the end of the table, where SQLite
    -- takes constraints that read more than one column.
    current_value  INTEGER NOT NULL,
    desired_value  INTEGER NOT NULL,

    -- WHY. The retained intent this plan takes its target value from, and the contract
    -- that intent is comparable under. The desired value is never supplied by a caller:
    -- a Run reconciles the runtime *to the active intent*, and an operator who wants a
    -- different value revises the intent first, through the endpoint that exists for it.
    intent_id         TEXT NOT NULL REFERENCES intents(intent_id),
    intent_version    INTEGER NOT NULL CHECK (intent_version >= 1),
    intent_capability TEXT NOT NULL,
    intent_provider   TEXT NOT NULL,

    -- EVIDENCE. The exact reading the current value came from, and the durable claim this
    -- plan answers, when there is one.
    observation_id   TEXT NOT NULL REFERENCES observations(observation_id),
    sweep_id         TEXT NOT NULL REFERENCES observation_sweeps(sweep_id),
    observed_at      TEXT NOT NULL,
    drift_finding_id TEXT REFERENCES findings(finding_id),

    -- OWNERSHIP, as assessed when the plan was published rather than re-derived on every
    -- read. A plan is a decision made against the evidence that existed when it was made;
    -- a preview whose ownership section improved overnight would be a different plan
    -- wearing the same identity. `provider_readings` is the audit trail — which reading
    -- each source was assessed from — and is JSON because it is read whole, in Python,
    -- and nothing queries into it.
    ownership_state     TEXT NOT NULL,
    ownership_reason    TEXT NOT NULL,
    ownership_claims    TEXT NOT NULL DEFAULT '[]',
    ownership_gaps      TEXT NOT NULL DEFAULT '[]',
    provider_readings   TEXT NOT NULL DEFAULT '{}',

    -- PROTECTION. Whether this object carries the path the operator is reaching LocalPlane
    -- over. CHECKed to 'unknown' because that is the only answer this build can produce
    -- honestly: it observes links, addresses and what three daemons claim, and none of
    -- those names a path to anybody. The column will widen when the evidence arrives, in
    -- the migration that adds it, which is the review that decision deserves.
    protection_management_path TEXT NOT NULL CHECK (protection_management_path = 'unknown'),
    protection_reason          TEXT NOT NULL,
    protection_missing_evidence TEXT NOT NULL DEFAULT '[]',

    -- RISK, derived from the evidence above and stored with the factors that decided it.
    risk_tier    TEXT NOT NULL CHECK (risk_tier IN ('low', 'medium', 'high')),
    risk_factors TEXT NOT NULL DEFAULT '[]',

    -- CONFIRMATION. What confirming this would take, and the policy sentence in force when
    -- it was published — stored, not derived, because a plan reviewed under one policy
    -- must not be silently re-read under a later one. `confirmation_token_issued` is
    -- CHECKed to 0: a blocked plan that could hand out a token could progress to an apply
    -- review, and every plan in this build is blocked.
    confirmation_required     INTEGER NOT NULL CHECK (confirmation_required IN (0, 1)),
    confirmation_method       TEXT NOT NULL
        CHECK (confirmation_method IN ('none', 'acknowledge', 'typed')),
    confirmation_source       TEXT NOT NULL CHECK (confirmation_source IN ('policy', 'operation')),
    confirmation_reasons      TEXT NOT NULL DEFAULT '[]',
    confirmation_policy       TEXT NOT NULL,
    confirmation_token_issued INTEGER NOT NULL CHECK (confirmation_token_issued = 0),

    -- EXECUTION. Both halves, because they are different questions: whether LocalPlane
    -- could run this at all, and whether this particular plan would be allowed to.
    -- `execution_provider` is CHECKed NULL — there is no write provider for anything, and
    -- naming a plausible one would be inventing an execution path.
    execution_availability TEXT NOT NULL CHECK (execution_availability = 'not_implemented'),
    execution_eligibility  TEXT NOT NULL CHECK (execution_eligibility = 'blocked'),
    execution_blockers     TEXT NOT NULL DEFAULT '[]',
    execution_provider     TEXT CHECK (execution_provider IS NULL),
    -- The capability an execution would need. Deliberately not in the protocol's
    -- capability vocabulary: that list describes what the agent can do, and adding a name
    -- to it because LocalPlane knows the concept is the assumption the protocol forbids.
    required_capability    TEXT NOT NULL,
    -- Whether the agent declared it. A runtime fact about what was probed, so no CHECK:
    -- an agent that grows the capability should be recorded as having grown it.
    capability_declared    INTEGER NOT NULL CHECK (capability_declared IN (0, 1)),

    -- RECOVERY. `recovery_armed` is CHECKed to 0. "A rollback exists in principle" and
    -- "recovery is established and will fire" are different claims, and
    -- that difference is the whole point of the write-boundary rules.
    recovery_mode              TEXT NOT NULL
        CHECK (recovery_mode IN ('auto', 'operator', 'none')),
    recovery_rollback_possible INTEGER NOT NULL CHECK (recovery_rollback_possible IN (0, 1)),
    recovery_armed             INTEGER NOT NULL CHECK (recovery_armed = 0),
    recovery_guarantee         TEXT NOT NULL,
    recovery_reason            TEXT NOT NULL,

    -- VERIFICATION. What a future verification would have to observe. `verification_executed`
    -- is CHECKed to 0: nothing was verified, and no row here may say otherwise.
    verification_capability TEXT NOT NULL,
    verification_provider   TEXT NOT NULL,
    verification_condition  TEXT NOT NULL,
    verification_executed   INTEGER NOT NULL CHECK (verification_executed = 0),

    published_at TEXT NOT NULL,

    -- A plan whose two ends agree is not a plan. Reconciling a value the host already has
    -- is work that would be reported as done without anything having been done, so it is
    -- refused before a Run exists and cannot be stored even if it were not.
    CHECK (current_value <> desired_value),
    CHECK (value_type <> 'boolean' OR (current_value IN (0, 1) AND desired_value IN (0, 1)))
) STRICT;

-- The Run: who was asked to do what to which object, and what became of the request.
--
-- `state` enumerates only what this build can truthfully produce. The complete run lifecycle
-- is fourteen states and lives in the code as the vocabulary it is; a store that could
-- hold 'succeeded' could hold the claim that a host was written, and no code path would be
-- needed to make that a lie. Widening this CHECK is what the write-boundary work does,
-- and needing a migration to do it is the point.
CREATE TABLE runs (
    run_id      TEXT NOT NULL PRIMARY KEY,
    host_id     TEXT NOT NULL REFERENCES hosts(host_id),
    object_id   TEXT NOT NULL REFERENCES objects(object_id),
    operation   TEXT NOT NULL CHECK (operation IN ('network.interface.reconcile_mtu')),
    state       TEXT NOT NULL CHECK (state IN ('preview', 'cancelled')),
    -- One plan per Run and one Run per plan. A Run re-pointed at a different plan would be
    -- the exact failure the immutable Run-plan identity prevents: the same identity presenting a
    -- different plan than the one that was reviewed. When inputs move, the answer is a new
    -- Run, not a rewritten one.
    preview_id  TEXT NOT NULL UNIQUE REFERENCES run_previews(preview_id),
    host_effect TEXT NOT NULL CHECK (host_effect = 'none'),
    created_at   TEXT NOT NULL,
    cancelled_at TEXT,
    CHECK ((state = 'cancelled') = (cancelled_at IS NOT NULL))
) STRICT;

CREATE INDEX idx_runs_host_created ON runs(host_id, created_at DESC);

CREATE INDEX idx_runs_object_created ON runs(object_id, created_at DESC);

-- A published plan is what somebody was shown. Rewriting it turns the record of a decision
-- into a record of the current opinion, and every staleness signal that depends on
-- comparing "what was published" with "what is true now" stops working the moment the
-- first half can move. A CHECK cannot express this and it has to hold against any writer,
-- including SQL typed into a shell, so it is a trigger.
--
-- Deletion is prevented differently: `runs.preview_id` is a NOT NULL foreign key, so a
-- published plan cannot be removed while the Run that published it exists, and nothing
-- deletes Runs.
CREATE TRIGGER run_previews_are_immutable
BEFORE UPDATE ON run_previews
BEGIN
    SELECT RAISE(ABORT, 'a published preview is immutable; plan again instead of rewriting');
END;

-- A plan must take its target value from an intent retained for the object the Run names.
-- A plan about eth0 citing eth1's intent would be internally coherent and completely
-- false, and no CHECK can look at another table.
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

-- And its current value must come from an observation of that same object.
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

-- What a Run is *about* never changes; only what became of it does. Cancelling sets
-- `state` and `cancelled_at`, and there is no other legal update on this table.
CREATE TRIGGER runs_identity_is_immutable
BEFORE UPDATE OF run_id, host_id, object_id, operation, preview_id, host_effect, created_at
ON runs
BEGIN
    SELECT RAISE(ABORT, 'a run''s operation, target and published plan are immutable');
END;
