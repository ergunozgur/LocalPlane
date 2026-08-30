-- Slice 11B Part B / step 1 of the self-impact vertical: derive and store, authorise nothing.
--
-- The immutable Run preview gains one generic JSON document: the backend self-impact
-- assessment for a systemd service action.  It answers whether executing that plan could
-- interrupt the LocalPlane backend that published it, what kind of outage that would be, and
-- whether the exact hazard is one a narrowly typed operator authority could later be issued
-- against.
--
-- **It authorises nothing and this migration adds no authority.**  There is no grant table,
-- no confirmation purpose, no new execution eligibility and no endpoint.  `override_eligible`
-- is a derivation stored beside the plan it was derived from, so that whatever later work
-- issues authority against is a document an operator could already read.  Protection is
-- untouched: the columns recording it keep exactly the values they had, and nothing here can
-- move a `protected` or an `unknown`.
--
-- The column follows `authorization_assessment` and `lifecycle_context` in both shape and
-- rule: present for the three closed systemd service actions, absent for everything else, and
-- CHECKed both ways rather than left to the code that writes it.
--
-- localplane:foreign-keys=off

-- The three triggers on `runs` name `run_previews` in their bodies, and SQLite resolves a
-- trigger body while dropping a table it mentions. They are dropped and recreated verbatim
-- around the rebuild; none of their rules changes here.
DROP TRIGGER run_previews_are_immutable;
DROP TRIGGER runs_intent_belongs_to_object;
DROP TRIGGER runs_observation_belongs_to_object;
DROP TRIGGER runs_protection_evidence_belongs_to_host;

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
        CHECK (execution_eligibility IN ('eligible', 'guarded', 'blocked')),
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
    CHECK (operation <> 'systemd.service.restart' OR action = 'restart')
) STRICT;


-- Every existing preview keeps every value it had. `self_impact` is NULL for all of them,
-- including systemd ones published under digest version 5: no build before this one derived
-- the assessment, and inventing one now for a plan whose author never saw it would put a
-- judgement into a document that is immutable precisely so it cannot acquire opinions.
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
    authorization_assessment, lifecycle_context, NULL,
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
