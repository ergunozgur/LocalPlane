-- Revising what LocalPlane intends for an object it already manages.
--
-- The operator response this makes possible is the second truthful answer to drift:
-- "the runtime value is the one I actually want — record that instead". The host is not
-- touched. What moves is LocalPlane's own claim.
--
-- Two changes:
--
--   intent_revisions   the durable event: this version exists because it was revised
--   findings           one more resolution, because a drift that ended this way did not
--                      end the way the two existing resolutions describe
--
-- and nothing added to `objects`, and nothing added to `intents`. Both omissions are
-- deliberate and both are explained below.
--
-- WHY THIS IS NOT A MANAGEMENT TRANSITION
--
-- Adopt is observed → managed and release is managed → observed. A revision starts and
-- ends at `managed`: the object's management state does not move, and recording a
-- managed → managed row in `management_transitions` would put an event in a table whose
-- CHECK enumerates the only two legal transitions, describing a movement that did not
-- happen. So revision gets its own event, and `management_transitions` stays exactly as
-- 0002 defined it.
--
-- WHY THE VERSION CHAIN IS NOT DUPLICATED HERE
--
-- Everything a revision could restate is already recorded on the intent it produced:
-- `intents.supersedes` is the version it replaced, `intents.version` orders the chain,
-- `intents.created_at` is when, `intents.observation_id` is the evidence it was made
-- against, and `objects.active_intent_id` is the single fact saying which version is in
-- force. This table adds the one thing none of them can say — *which* revision semantics
-- the operator chose — plus the event time that lets revisions and transitions be
-- interleaved into one history, and the structural statement that no host write happened.

-- One row per revision. Append-only, like every other event in this store.
--
-- `kind` is the distinction that must survive: `revise` is the operator supplying a new
-- desired value, `adopt_runtime` is the operator declaring that what is currently
-- observed is what was wanted. They produce the same kind of row in `intents` and they
-- are not the same act — one is a statement about the future, the other is a statement
-- about the present — and a reader who cannot tell them apart afterwards cannot audit
-- either of them.
--
-- `intent_id` is UNIQUE: a version is produced by at most one revision. Together with
-- `management_transitions`, that gives the invariant LocalPlane enforces — every
-- intent version is explained by exactly one event, an adopt or a revision, and there is
-- no third way for one to appear.
--
-- `host_effect` is CHECKed to a single value for the same reason it is in 0002: the store
-- must be structurally incapable of recording a host write on the management path. When
-- LocalPlane writes to a host that will be a Change, in a table that does not exist yet,
-- with obligations this one does not have.
--
-- There is no actor column, no reason text and no field list. There is no authentication
-- in this build so an actor would always read 'system'; a free-text reason is a claim
-- nothing could check; and which fields moved is the difference between two intent
-- versions that are both stored in full, which makes it derivable rather than storable.
CREATE TABLE intent_revisions (
    revision_id TEXT NOT NULL PRIMARY KEY,
    object_id   TEXT NOT NULL REFERENCES objects(object_id),
    host_id     TEXT NOT NULL REFERENCES hosts(host_id),
    kind        TEXT NOT NULL CHECK (kind IN ('revise', 'adopt_runtime')),
    -- The version this revision produced. Not the one it replaced: that is
    -- `intents.supersedes`, and saying it twice would let the two disagree.
    intent_id   TEXT NOT NULL UNIQUE REFERENCES intents(intent_id),
    host_effect TEXT NOT NULL CHECK (host_effect = 'none'),
    occurred_at TEXT NOT NULL
) STRICT;

CREATE INDEX idx_intent_revisions_object_time
    ON intent_revisions(object_id, occurred_at DESC);

-- WHY `intents` IS NOT ALTERED
--
-- The natural place for the revision semantics would be `intents.origin`, which 0002
-- CHECKed to the single value an intent could have had then. SQLite cannot widen a CHECK
-- constraint; the only way is to rebuild the table, and `intents` is referenced by
-- `intent_fields`, `findings`, `ownership_findings`, `management_transitions` and
-- `objects`, and is read by two triggers. Rebuilding it inside a migration means either
-- turning foreign keys off — which cannot be done inside a transaction, so the migration
-- would stop being atomic — or letting ALTER TABLE rewrite five other tables' REFERENCES
-- clauses to point at a temporary name. Neither is worth doing to widen an enumeration,
-- and both would rewrite every historical intent row to change nothing about it.
--
-- So `intents.origin` keeps the only value it can hold, and it is no longer the answer to
-- "how did this version come to exist". The events are: a version named by an `adopt` row
-- in `management_transitions` was adopted, and a version named by a row here was revised.
-- The repository derives the origin it reports from those, so the frozen column is never
-- read by anything above the store, and there is exactly one place the answer lives.

-- FINDINGS: ONE MORE WAY A DRIFT CAN END
--
-- 0002 gives a drift finding two resolutions, and neither of them can
-- describe what happens when an operator revises intent to match the runtime:
--
--   observed_matches_intent  says an observation showed the value agreeing again, and
--                            names the observation that proved it. Used here it would
--                            read as though the host had come back — the machine moved,
--                            the problem fixed itself. Nothing about the host moved.
--   intent_released          says there is no intent left to disagree with. There is; it
--                            is a new version, and LocalPlane is still answerable for it.
--
-- `intent_revised` says the true thing: the disagreement ended because LocalPlane changed
-- what it wants, not because anything was applied. The existing CHECK on
-- `resolved_by_observation_id` is kept exactly as it was, which is what stops this
-- resolution from naming an observation as its proof — there was no remediation for an
-- observation to have proven.
--
-- `findings` is rebuilt rather than altered because widening a CHECK needs a rebuild, and
-- unlike `intents` this one is safe: no table references `findings`, no trigger reads it,
-- and its three indexes are recreated below. Every row is copied column for column.
ALTER TABLE findings RENAME TO findings_0002;

CREATE TABLE findings (
    finding_id      TEXT NOT NULL PRIMARY KEY,
    finding_key     TEXT NOT NULL,
    host_id         TEXT NOT NULL REFERENCES hosts(host_id),
    object_id       TEXT NOT NULL REFERENCES objects(object_id),
    finding_type    TEXT NOT NULL CHECK (finding_type IN ('network.interface.drift')),
    subject         TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('open', 'resolved')),

    intent_id       TEXT NOT NULL REFERENCES intents(intent_id),
    intended_type   TEXT NOT NULL CHECK (intended_type IN ('boolean', 'integer')),
    intended_value  INTEGER NOT NULL,
    observed_type   TEXT CHECK (observed_type IS NULL OR observed_type IN ('boolean', 'integer')),
    observed_value  INTEGER,
    comparison      TEXT NOT NULL CHECK (comparison IN ('differs', 'unknown')),
    reason          TEXT NOT NULL,
    observation_id  TEXT REFERENCES observations(observation_id),
    sweep_id        TEXT REFERENCES observation_sweeps(sweep_id),

    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    updated_at      TEXT NOT NULL,

    resolved_at     TEXT,
    resolution      TEXT CHECK (
        resolution IS NULL
        OR resolution IN ('observed_matches_intent', 'intent_revised', 'intent_released')
    ),
    resolved_by_observation_id TEXT REFERENCES observations(observation_id),

    CHECK ((status = 'resolved') = (resolved_at IS NOT NULL)),
    CHECK ((status = 'resolved') = (resolution IS NOT NULL)),
    CHECK ((observed_value IS NULL) = (observed_type IS NULL)),
    CHECK ((comparison = 'unknown') = (observed_value IS NULL)),
    -- Unchanged from 0002, and load-bearing for the new resolution: only a resolution an
    -- observation proved may name one, so `intent_revised` structurally cannot claim the
    -- runtime was read and found to have been put right.
    CHECK (resolved_by_observation_id IS NULL OR resolution = 'observed_matches_intent')
) STRICT;

INSERT INTO findings (
    finding_id, finding_key, host_id, object_id, finding_type, subject, status,
    intent_id, intended_type, intended_value, observed_type, observed_value,
    comparison, reason, observation_id, sweep_id,
    first_seen_at, last_seen_at, updated_at,
    resolved_at, resolution, resolved_by_observation_id
)
SELECT
    finding_id, finding_key, host_id, object_id, finding_type, subject, status,
    intent_id, intended_type, intended_value, observed_type, observed_value,
    comparison, reason, observation_id, sweep_id,
    first_seen_at, last_seen_at, updated_at,
    resolved_at, resolution, resolved_by_observation_id
FROM findings_0002;

DROP TABLE findings_0002;

CREATE UNIQUE INDEX idx_findings_one_open_per_key ON findings(finding_key) WHERE status = 'open';

CREATE INDEX idx_findings_object ON findings(object_id, status, first_seen_at DESC);

CREATE INDEX idx_findings_host ON findings(host_id, status, first_seen_at DESC);
