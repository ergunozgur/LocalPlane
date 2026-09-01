-- Widen confirmation attribution without inventing an actor or rewriting history.
-- localplane:foreign-keys=off

-- These safety triggers reference the table being rebuilt and must never observe its
-- temporary absence. They are recreated verbatim below inside the same transaction.
DROP TRIGGER changes_require_a_consumed_confirmation;
DROP TRIGGER changes_requiring_a_self_impact_override_have_one;
DROP TRIGGER run_guards_require_the_typed_authority;
DROP TRIGGER recovery_mutation_names_the_authority_spent_on_it_on_insert;
DROP TRIGGER recovery_mutation_names_the_authority_spent_on_it_on_update;

CREATE TABLE run_confirmations_rebuilt (
    confirmation_id TEXT NOT NULL PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    purpose         TEXT NOT NULL
        CHECK (purpose IN ('apply', 'recovery_retry', 'self_impact_override')),
    preview_id      TEXT NOT NULL REFERENCES run_previews(preview_id),
    preview_digest  TEXT NOT NULL,
    digest_version  INTEGER NOT NULL CHECK (digest_version >= 1),
    required_method TEXT NOT NULL CHECK (required_method IN ('acknowledge', 'typed')),
    method          TEXT NOT NULL CHECK (method IN ('acknowledge', 'typed')),
    typed_statement TEXT,
    policy          TEXT NOT NULL,
    source          TEXT NOT NULL
        CHECK (source IN ('unauthenticated_request', 'authenticated_request')),
    satisfied_at    TEXT NOT NULL,
    consumed_at     TEXT,
    consumed_by_attempt_id TEXT,

    CHECK ((method = 'typed') = (typed_statement IS NOT NULL)),
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
    SELECT RAISE(ABORT,
        'a recovery mutation must name the consumed recovery confirmation spent on it');
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
    SELECT RAISE(ABORT,
        'a recovery mutation must name the consumed recovery confirmation spent on it');
END;
