-- Slice 11B Part B, final step: a Change may now be about a systemd service action.
--
-- The last structural thing standing between an accepted plan and an executed one.  Every
-- other piece is already here: the planner, the protection model, the self-impact derivation
-- and its single-use override, the closed agent dispatch and the write boundary.  What has
-- been true until now is that `changes.operation` admitted no systemd operation, so no Change
-- could exist for one — which was exactly right while there was no executor, and is what this
-- migration ends.
--
-- **It widens one CHECK and changes nothing else.**  Every invariant on this table is carried
-- forward verbatim: `failed` still requires a proven `not_written`, `succeeded` still requires
-- `written` plus a verification that proved it, a Change still requires a consumed apply
-- confirmation, a Change whose plan rests on the self-impact override still requires that
-- override consumed, a management-path checkpoint still requires an armed guard, and identity
-- is still immutable.  An action still carries no checkpoint, no rollback columns and no
-- intent, which is what makes a systemd lifecycle Change structurally incapable of claiming a
-- rollback it does not have.
--
-- No self-impact fact is copied here.  It is frozen in the preview this Change names and
-- bound by digest version 6; a second copy could only ever agree with it until the day it did
-- not.
--
-- localplane:foreign-keys=off

DROP TRIGGER changes_identity_is_immutable;
DROP TRIGGER changes_checkpoint_belongs_to_run;
DROP TRIGGER changes_on_the_management_path_require_an_armed_guard;
DROP TRIGGER changes_require_a_consumed_confirmation;
DROP TRIGGER changes_requiring_a_self_impact_override_have_one;
DROP TRIGGER recovery_attempts_belong_to_a_recovery_hold;
DROP INDEX idx_changes_host_created;
DROP INDEX idx_changes_object_created;

CREATE TABLE changes_rebuilt (
    change_id     TEXT NOT NULL PRIMARY KEY,
    run_id        TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
    preview_id    TEXT NOT NULL REFERENCES run_previews(preview_id),
    checkpoint_id TEXT REFERENCES run_checkpoints(checkpoint_id),
    host_id       TEXT NOT NULL REFERENCES hosts(host_id),
    object_id     TEXT NOT NULL REFERENCES objects(object_id),
    operation     TEXT NOT NULL CHECK (operation IN (
        'network.interface.reconcile_mtu',
        'docker.container.start', 'docker.container.stop', 'docker.container.restart',
        'systemd.service.start', 'systemd.service.stop', 'systemd.service.restart'
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
) STRICT
;

-- Every existing Change keeps every value it had; only the vocabulary its operation is
-- checked against is wider.
INSERT INTO changes_rebuilt (change_id, run_id, preview_id, checkpoint_id, host_id, object_id, operation, change_kind, field, value_type, before_value, desired_value, action, observed_state, expected_state, execution_correlation, created_at, apply_attempt_id, dispatch_began_at, mutation_outcome, mutation_reason, mutation_provider, mutation_method, mutation_detail, settled_at, host_effect, verification_outcome, verification_observation_id, verification_observed_value, verification_observed_state, verification_reason, rollback_required, rollback_attempt_id, rollback_dispatch_began_at, rollback_outcome, rollback_reason, rollback_detail, rollback_verification_outcome, rollback_verification_observation_id, rollback_verification_observed_value, result, recovery_reason, finished_at)
SELECT change_id, run_id, preview_id, checkpoint_id, host_id, object_id, operation, change_kind, field, value_type, before_value, desired_value, action, observed_state, expected_state, execution_correlation, created_at, apply_attempt_id, dispatch_began_at, mutation_outcome, mutation_reason, mutation_provider, mutation_method, mutation_detail, settled_at, host_effect, verification_outcome, verification_observation_id, verification_observed_value, verification_observed_state, verification_reason, rollback_required, rollback_attempt_id, rollback_dispatch_began_at, rollback_outcome, rollback_reason, rollback_detail, rollback_verification_outcome, rollback_verification_observation_id, rollback_verification_observed_value, result, recovery_reason, finished_at FROM changes;

DROP TABLE changes;
ALTER TABLE changes_rebuilt RENAME TO changes;

CREATE INDEX idx_changes_host_created ON changes(host_id, created_at DESC);
CREATE INDEX idx_changes_object_created ON changes(object_id, created_at DESC);

-- Recreated verbatim; only the table beneath them moved. Each is the same rule it was:
-- identity is immutable, a checkpoint belongs to its own run, a management-path change
-- needs an armed guard, a Change needs its consumed apply confirmation, one whose plan
-- rests on the self-impact override needs that consumed too, and a recovery attempt
-- belongs to a Change that actually ended in recovery_required.

CREATE TRIGGER changes_identity_is_immutable
BEFORE UPDATE OF change_id, run_id, preview_id, checkpoint_id, host_id, object_id,
                 operation, change_kind, field, value_type, before_value, desired_value,
                 action, observed_state, expected_state, execution_correlation, created_at,
                 apply_attempt_id
ON changes
BEGIN
    SELECT RAISE(ABORT, 'what a change was about is immutable; only what became of it moves');
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
