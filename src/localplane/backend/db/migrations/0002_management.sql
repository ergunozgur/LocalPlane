-- The durable model for management: retained intent, and the claims it produces.
--
-- Four concerns, four tables:
--
--   intents                 what LocalPlane retains as the desired state of an object
--   intent_fields           the individual controlled values, one row per field
--   management_transitions  adopt and release, recorded as the events they are
--   findings                LocalPlane's interpreted, evidence-backed claims
--
-- and one column added to `objects`:
--
--   active_intent_id        which retained intent is in force right now
--
-- Nothing in this migration lets LocalPlane write to the host. Adopt records what was
-- already true; release forgets it. `management_transitions.host_effect` is CHECKed to a
-- single value so the schema itself cannot record a host write here — when LocalPlane
-- writes to a host that will be a Change, a different record with different obligations.
--
-- Two states are made structurally impossible, in the schema rather than in code:
--
--   managed with no active intent      — the CHECK on objects
--   not managed while intent is in force — the same CHECK, read the other way
--
-- and a third, "the active intent belongs to a different object", by the two triggers at
-- the end. A CHECK constraint cannot look at another table, and this invariant has to
-- hold against any writer, so a trigger is the tool that fits.
--
-- Reconciliation is not stored. It is the comparison of the active intent against the
-- newest compatible observation, computed when it is asked for, exactly as freshness is.
-- A stored reconciliation column would be a second copy of a fact that changes when a new
-- observation lands, and keeping the two in step is the duplicated-truth bug this schema
-- exists to avoid.

-- An intent is what LocalPlane believes an object should look like. Rows are immutable
-- and append-only: adopting again writes a new version rather than rewriting one, so the
-- history of what was intended, and on what evidence, survives.
--
-- There is deliberately no `status` or `retired_at` column. Whether an intent is in force
-- is one fact, and it lives in exactly one place — `objects.active_intent_id`. When that
-- pointer moves, the moment is recorded in management_transitions.
CREATE TABLE intents (
    intent_id        TEXT    NOT NULL PRIMARY KEY,
    object_id        TEXT    NOT NULL REFERENCES objects(object_id),
    host_id          TEXT    NOT NULL REFERENCES hosts(host_id),
    version          INTEGER NOT NULL CHECK (version >= 1),
    supersedes       TEXT    REFERENCES intents(intent_id),
    -- The shape of the captured field set. An intent written by a build that understood a
    -- different set of fields must be recognisable as such rather than silently compared.
    schema_version   INTEGER NOT NULL CHECK (schema_version >= 1),
    origin           TEXT    NOT NULL CHECK (origin IN ('adopt')),
    -- The observation this intent was captured from, and the contract that produced it.
    -- Reconciliation refuses to compare against an observation from a different provider,
    -- because the same field name from a different source is not the same fact.
    capability       TEXT    NOT NULL,
    provider         TEXT    NOT NULL,
    provider_version TEXT    NOT NULL,
    observation_id   TEXT    NOT NULL REFERENCES observations(observation_id),
    sweep_id         TEXT    NOT NULL REFERENCES observation_sweeps(sweep_id),
    observed_at      TEXT    NOT NULL,
    created_at       TEXT    NOT NULL,
    UNIQUE (object_id, version)
) STRICT;

CREATE INDEX idx_intents_object_version ON intents(object_id, version DESC);

-- One row per controlled field. Relational rather than a document, because "which fields
-- does LocalPlane control" is a set that is queried, compared and counted.
--
-- Both fields adoptable at this schema version encode as integers, so one typed value column
-- carries them with `value_type` saying how to read it. A text-valued field would get its
-- own column in its own migration — which is the right moment to decide what comparing
-- one means.
CREATE TABLE intent_fields (
    intent_id  TEXT    NOT NULL REFERENCES intents(intent_id),
    field      TEXT    NOT NULL,
    value_type TEXT    NOT NULL CHECK (value_type IN ('boolean', 'integer')),
    value      INTEGER NOT NULL,
    CHECK (value_type <> 'boolean' OR value IN (0, 1)),
    PRIMARY KEY (intent_id, field)
) STRICT;

-- Adopt and release. Append-only.
--
-- This table exists because release leaves no other trace: the intent row it retires stays
-- exactly as it was, and without this a caller could see that an intent exists and that
-- the object is observed, but not when or how the two stopped being connected.
--
-- The CHECK enumerates the only two legal transitions. There is no actor column: this
-- build has no authentication, and a column that always read 'system' would be an
-- attribution LocalPlane has not earned.
CREATE TABLE management_transitions (
    transition_id  TEXT NOT NULL PRIMARY KEY,
    object_id      TEXT NOT NULL REFERENCES objects(object_id),
    host_id        TEXT NOT NULL REFERENCES hosts(host_id),
    transition     TEXT NOT NULL CHECK (transition IN ('adopt', 'release')),
    from_state     TEXT NOT NULL,
    to_state       TEXT NOT NULL,
    intent_id      TEXT NOT NULL REFERENCES intents(intent_id),
    -- The evidence adopt captured from. Release consults no observation and stores none.
    observation_id TEXT REFERENCES observations(observation_id),
    host_effect    TEXT NOT NULL CHECK (host_effect = 'none'),
    occurred_at    TEXT NOT NULL,
    CHECK (
        (transition = 'adopt'   AND from_state = 'observed' AND to_state = 'managed'
                                AND observation_id IS NOT NULL)
     OR (transition = 'release' AND from_state = 'managed'  AND to_state = 'observed'
                                AND observation_id IS NULL)
    )
) STRICT;

CREATE INDEX idx_transitions_object_time ON management_transitions(object_id, occurred_at DESC);

-- A finding is a claim LocalPlane is making, and it is not the same thing as the state it
-- was derived from. `reconciliation = drifted` is a comparison, recomputed from scratch
-- every time it is asked for. A finding is the durable record that LocalPlane noticed, when
-- it first noticed, what evidence it had, and how it ended.
--
-- Identity is split in two so that both requirements hold at once:
--
--   finding_key  deterministic — host, object, type and subject. The same disagreement
--                always maps to the same logical finding, so re-observing it updates one
--                row instead of appending another.
--   finding_id   one row per *episode*. A finding that resolved and later recurred is a
--                new episode with its own first_seen_at, and the resolved one is kept.
--
-- The unique index enforces at most one open finding per logical key. That is the whole
-- of the duplicate-storm defence, and it is in the schema rather than in a code path that
-- could be bypassed.
--
-- Evidence is relational, not a blob: a drift finding is scoped to exactly one controlled
-- field, so its evidence is one typed comparison and every part of it is a column. A
-- finding type whose evidence is not field-scoped will need its own representation, and
-- that is a decision for the migration that introduces it.
--
-- There is no severity column. Ranking an MTU disagreement against an admin-state one
-- needs a model of what the object is for, which LocalPlane does not have; a column that
-- always held the same value would imply a judgement that was never made.
--
-- There is no summary column either. A sentence describing this row is derived from the
-- typed columns when a caller asks for one, so it cannot fall out of step with them.
CREATE TABLE findings (
    finding_id      TEXT NOT NULL PRIMARY KEY,
    finding_key     TEXT NOT NULL,
    host_id         TEXT NOT NULL REFERENCES hosts(host_id),
    object_id       TEXT NOT NULL REFERENCES objects(object_id),
    finding_type    TEXT NOT NULL CHECK (finding_type IN ('network.interface.drift')),
    -- What within the object the claim is about. For drift, the controlled field name.
    subject         TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('open', 'resolved')),

    -- evidence: the last comparison that supported this claim
    intent_id       TEXT NOT NULL REFERENCES intents(intent_id),
    intended_type   TEXT NOT NULL CHECK (intended_type IN ('boolean', 'integer')),
    intended_value  INTEGER NOT NULL,
    observed_type   TEXT CHECK (observed_type IS NULL OR observed_type IN ('boolean', 'integer')),
    observed_value  INTEGER,
    comparison      TEXT NOT NULL CHECK (comparison IN ('differs', 'unknown')),
    reason          TEXT NOT NULL,
    observation_id  TEXT REFERENCES observations(observation_id),
    sweep_id        TEXT REFERENCES observation_sweeps(sweep_id),

    -- first_seen_at  when this episode opened — the drift was proven
    -- last_seen_at   the most recent observation that proved it again
    -- updated_at     the most recent evaluation that touched this row at all
    --
    -- last_seen_at < updated_at is a real and meaningful state: the finding is open, and
    -- the latest look could not confirm it. That is not grounds for closing it.
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    updated_at      TEXT NOT NULL,

    resolved_at     TEXT,
    resolution      TEXT CHECK (
        resolution IS NULL OR resolution IN ('observed_matches_intent', 'intent_released')
    ),
    resolved_by_observation_id TEXT REFERENCES observations(observation_id),

    CHECK ((status = 'resolved') = (resolved_at IS NOT NULL)),
    CHECK ((status = 'resolved') = (resolution IS NOT NULL)),
    CHECK ((observed_value IS NULL) = (observed_type IS NULL)),
    -- An unknown comparison has no observed value, and a differing one must have one.
    CHECK ((comparison = 'unknown') = (observed_value IS NULL)),
    -- Only a resolution proven by an observation may name one.
    CHECK (resolved_by_observation_id IS NULL OR resolution = 'observed_matches_intent')
) STRICT;

CREATE UNIQUE INDEX idx_findings_one_open_per_key ON findings(finding_key) WHERE status = 'open';
CREATE INDEX idx_findings_object ON findings(object_id, status, first_seen_at DESC);
CREATE INDEX idx_findings_host ON findings(host_id, status, first_seen_at DESC);

-- Which intent is in force. NULL for every object that is not managed, and that is not a
-- convention — the CHECK below makes the two inseparable in both directions.
ALTER TABLE objects ADD COLUMN active_intent_id TEXT
    REFERENCES intents(intent_id)
    CHECK ((management_state = 'managed') = (active_intent_id IS NOT NULL));

-- The active intent must be an intent retained for *this* object. A CHECK cannot read
-- another table, so this is a trigger; it holds for any writer, including SQL typed into
-- a shell, which is the point of putting it here rather than in the repository.
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
