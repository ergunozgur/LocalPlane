-- Management-path evidence, and the protection judgement made from it.
--
-- One new table, and one table rebuild that this migration exists to make reviewable.
--
--   management_path_observations  raw evidence: how a connection reached LocalPlane
--   run_previews                  rebuilt, so a plan can record that its target is the
--                                 management path — which 0005 forbade, correctly
--   runs                          rebuilt only to re-point its foreign key
--
-- WHAT THE NEW TABLE IS, AND WHAT IT IS NOT
--
-- It holds evidence, not a conclusion. Two addresses — the peer of the connection and the
-- local endpoint it terminated on, both read from the accepted socket and never from a
-- header — and what the kernel said when asked which route it would use to reach that
-- peer. It deliberately does *not* hold "the management path is eth0".
--
-- A stored conclusion would be wrong the moment either half of it moved: the addresses on
-- an interface change, an interface disappears, a route is replaced. The verdict is
-- therefore derived at read time from this evidence plus the newest observation of each
-- object, exactly as ownership is derived from `provider_observations`. What is kept is
-- what made the verdict believable, so a preview published today stays answerable
-- tomorrow.
--
-- WHY THERE IS NO CALLER-SUPPLIED ADDRESS ANYWHERE ON THIS PATH
--
-- Every value in a row here comes from the server side of a real connection. There is no
-- endpoint that accepts a peer address, a local address, an interface name or a claim that
-- a particular object is the operator's path, and there is no column one could be written
-- to. `X-Forwarded-For`, `X-Real-IP` and `Forwarded` are read by nothing. A management
-- path proven from something a caller wrote is not proven at all — it is a request that
-- has been believed — and the 2026-07-22 lockout is what believing one costs.
--
-- WHY A ROW EXISTS ONLY FOR A USABLE TRANSPORT
--
-- A request from loopback, from a reverse proxy on this host, or over a transport whose
-- endpoints cannot be read, establishes nothing. Recording it would create a row that
-- looks like evidence, sorts as the newest evidence, and proves nothing — and the whole
-- risk in this area is a record being taken for proof it never was. So the refusal is
-- returned and nothing is written. What *is* recorded is a usable transport whose route
-- lookup failed, because "we asked and this is what happened" is evidence, and the CHECKs
-- below make such a row say why and carry no route facts.

-- One observation of how a connection reached LocalPlane.
--
-- Immutable: a trigger refuses every UPDATE. Evidence that can be rewritten is not
-- evidence, and a published preview points at exactly this row to say what its protection
-- judgement was made from. Deletion is not blocked by a trigger — it is blocked where it
-- matters, by the foreign key from `run_previews.protection_evidence_id`, so a row a plan
-- rests on cannot be removed while the plan exists, and rows nothing rests on can one day
-- be pruned without a schema change.
CREATE TABLE management_path_observations (
    observation_id TEXT NOT NULL PRIMARY KEY,
    host_id        TEXT NOT NULL REFERENCES hosts(host_id),
    observed_at    TEXT NOT NULL,
    -- Which agent answered the route query. Nullable: the transport half of this evidence
    -- is the backend's own and does not need an agent to exist.
    agent_instance_id TEXT REFERENCES agent_instances(agent_instance_id),

    -- TRANSPORT EVIDENCE. The two ends of the accepted connection, as the kernel reported
    -- them to the server — `transport_peer` rather than "actor", "user" or "session",
    -- because there is no authentication in this product and nobody on the other end of
    -- this socket has been identified. Its value is precisely that a caller cannot choose
    -- it, not that it says who anyone is.
    --
    -- Both are stored in their canonical text form, IPv4-mapped IPv6 already reduced to
    -- IPv4, so that comparing one against the addresses an interface carries is a
    -- comparison of the same thing.
    transport_peer_address TEXT NOT NULL,
    transport_peer_family  TEXT NOT NULL CHECK (transport_peer_family IN ('inet', 'inet6')),
    local_endpoint_address TEXT NOT NULL,
    local_endpoint_family  TEXT NOT NULL CHECK (local_endpoint_family IN ('inet', 'inet6')),

    -- WHERE THE ROUTE EVIDENCE CAME FROM. Named the same way an observation names its
    -- source, so "which capability, which provider, which method" is answerable here too.
    capability       TEXT NOT NULL,
    provider         TEXT NOT NULL,
    provider_version TEXT NOT NULL,
    method           TEXT NOT NULL,

    -- ROUTE EVIDENCE. Kernel facts, and nothing derived from them. `route_oif_index` is an
    -- interface *index*: correlating it with a LocalPlane object is a judgement and is done
    -- in the backend, against the same observations everything else is derived from.
    route_status TEXT NOT NULL
        CHECK (route_status IN ('resolved', 'unreachable', 'failed', 'unavailable')),
    route_reason TEXT,
    route_family TEXT CHECK (route_family IS NULL OR route_family IN ('inet', 'inet6')),
    route_destination               TEXT,
    route_destination_prefix_length INTEGER,
    route_preferred_source TEXT,
    route_gateway          TEXT,
    route_oif_index        INTEGER,
    route_table            INTEGER,
    route_type             TEXT,
    route_scope            TEXT,
    route_protocol         TEXT,
    route_priority         INTEGER,
    -- The structured errno the kernel answered with, when it answered with one. JSON
    -- because it is read whole and nothing queries into it.
    route_error TEXT,

    -- The two ends of one connection are the same family, or this is not one connection.
    CHECK (transport_peer_family = local_endpoint_family),
    -- A lookup that did not resolve must say why, and must carry no route facts. The same
    -- rule `provider_observations` applies to a failed provider reading, for the same
    -- reason: a failure that carries data invites the data to be read as an answer.
    CHECK (route_status = 'resolved' OR route_reason IS NOT NULL),
    CHECK (route_status = 'resolved' OR route_oif_index IS NULL),
    CHECK (route_status = 'resolved' OR route_preferred_source IS NULL),
    CHECK (route_status = 'resolved' OR route_gateway IS NULL),
    CHECK (route_status = 'resolved' OR route_destination IS NULL)
) STRICT;

-- The read this table exists for: the newest evidence matching *this* request's transport.
-- Peer and local endpoint are both in the key because either one differing means the
-- evidence belongs to a different connection and must not answer for this one.
CREATE INDEX idx_management_path_observations_match
    ON management_path_observations(
        host_id, transport_peer_address, local_endpoint_address, observed_at DESC
    );

CREATE TRIGGER management_path_observations_are_immutable
BEFORE UPDATE ON management_path_observations
BEGIN
    SELECT RAISE(ABORT, 'management-path evidence is immutable; observe again instead');
END;

-- ---------------------------------------------------------------------------------------
-- THE REBUILD
--
-- 0005 CHECKed `run_previews.protection_management_path` to the single value 'unknown',
-- and said the column would widen "in the migration that adds it, which is the review that
-- decision deserves". This is that migration. SQLite cannot widen a CHECK in place, so the
-- table is rebuilt — honestly, rather than by parking the new truth in a side table and
-- leaving two places to disagree about what a preview said.
--
-- `runs` is rebuilt too, and only for one reason: it is the sole child of `run_previews`,
-- and re-pointing a foreign key needs the table that holds it. Its columns, CHECKs and
-- semantics are reproduced exactly. In particular `state` still enumerates only 'preview'
-- and 'cancelled' — widening *that* is the write boundary's migration, not this one.
--
-- The order below is what makes it safe inside one transaction, and each step is load
-- bearing:
--
--   1. drop the triggers first, so no ALTER TABLE has to rewrite one;
--   2. build both new tables, the child referencing the new parent;
--   3. copy, so every row exists in the new tables before anything is dropped;
--   4. drop the child, then the parent — in that order, because dropping a parent with
--      live children is a foreign-key violation and deferring it does not help: SQLite's
--      deferred-violation counter is not decremented by re-creating the table, which is
--      what 0004 found when it tried the same thing on `intents`;
--   5. rename the parent, which rewrites the child's REFERENCES clause to match — the
--      modern ALTER TABLE behaviour, and here it is exactly what is wanted;
--   6. rename the child, which nothing references, so nothing is rewritten;
--   7. put the indexes and triggers back.
--
-- No PRAGMA is touched. `foreign_keys` stays ON and the migration keeps its atomicity.

DROP TRIGGER run_previews_are_immutable;

DROP TRIGGER runs_intent_belongs_to_object;

DROP TRIGGER runs_observation_belongs_to_object;

DROP TRIGGER runs_identity_is_immutable;

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

    -- PROTECTION, as it was assessed when the plan was published.
    --
    -- `protection_status` is the roll-up over every reason this build implements, and
    -- 'clear' means "every implemented reason was evaluated and none applies" — never
    -- "safe". `protection_reasons` names the reasons proven to apply and
    -- `protection_unresolved` the ones whose evidence could not be settled, so a second
    -- reason arriving later does not make the first one's answer harder to find.
    --
    -- `protection_management_path` is this target's relation to the operator's path:
    -- on it, proven not on it, or unknown. All three are storable now — which is the whole
    -- point of the rebuild — and nothing outside those three is.
    --
    -- `protection_evidence_id` binds the judgement to the exact observation it was made
    -- from. Bound, not hashed: a later observation proving the same path has confirmed the
    -- plan rather than changed it, and a digest over the row's identity would make every
    -- preview stale at the next refresh.
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

    execution_availability TEXT NOT NULL CHECK (execution_availability = 'not_implemented'),
    execution_eligibility  TEXT NOT NULL CHECK (execution_eligibility = 'blocked'),
    execution_blockers     TEXT NOT NULL DEFAULT '[]',
    execution_provider     TEXT CHECK (execution_provider IS NULL),
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
    -- A named piece of evidence must come with when it was observed. Half an answer about
    -- provenance is the kind that gets read as a whole one.
    CHECK (protection_evidence_id IS NULL OR protection_evidence_observed_at IS NOT NULL)
) STRICT;

-- The copy. Every existing column moves across unchanged; the four new ones are *restated*
-- from the relation the row already carries rather than decided afresh.
--
-- A pre-0006 preview was published when nothing could establish a management path, so its
-- relation is 'unknown' and the only branch below that can fire is the one that says:
-- protection unknown, no reason proven, the management-path reason unresolved, and no
-- evidence — which is precisely what the row already said in the columns it had. The other
-- branches are written out so that the mapping is visibly a translation and not a decision
-- about history. `digest_version` is untouched: those previews keep verifying against the
-- digest they were published with, under the canonical form they were published under.
INSERT INTO run_previews_rebuilt (
    preview_id, preview_digest, digest_version, operation,
    field, value_type, current_value, desired_value,
    intent_id, intent_version, intent_capability, intent_provider,
    observation_id, sweep_id, observed_at, drift_finding_id,
    ownership_state, ownership_reason, ownership_claims, ownership_gaps, provider_readings,
    protection_status, protection_reasons, protection_unresolved,
    protection_management_path, protection_reason, protection_missing_evidence,
    protection_evidence_id, protection_evidence_observed_at,
    risk_tier, risk_factors,
    confirmation_required, confirmation_method, confirmation_source, confirmation_reasons,
    confirmation_policy, confirmation_token_issued,
    execution_availability, execution_eligibility, execution_blockers, execution_provider,
    required_capability, capability_declared,
    recovery_mode, recovery_rollback_possible, recovery_armed, recovery_guarantee,
    recovery_reason,
    verification_capability, verification_provider, verification_condition,
    verification_executed,
    published_at
)
SELECT
    preview_id, preview_digest, digest_version, operation,
    field, value_type, current_value, desired_value,
    intent_id, intent_version, intent_capability, intent_provider,
    observation_id, sweep_id, observed_at, drift_finding_id,
    ownership_state, ownership_reason, ownership_claims, ownership_gaps, provider_readings,
    CASE protection_management_path
        WHEN 'on_management_path' THEN 'protected'
        WHEN 'not_on_management_path' THEN 'clear'
        ELSE 'unknown'
    END,
    CASE protection_management_path
        WHEN 'on_management_path' THEN '["management_path"]'
        ELSE '[]'
    END,
    CASE protection_management_path
        WHEN 'unknown' THEN '["management_path"]'
        ELSE '[]'
    END,
    protection_management_path, protection_reason, protection_missing_evidence,
    NULL, NULL,
    risk_tier, risk_factors,
    confirmation_required, confirmation_method, confirmation_source, confirmation_reasons,
    confirmation_policy, confirmation_token_issued,
    execution_availability, execution_eligibility, execution_blockers, execution_provider,
    required_capability, capability_declared,
    recovery_mode, recovery_rollback_possible, recovery_armed, recovery_guarantee,
    recovery_reason,
    verification_capability, verification_provider, verification_condition,
    verification_executed,
    published_at
FROM run_previews;

-- `runs`, reproduced exactly. The only difference is which table `preview_id` points at,
-- and after the renames below even that reads the same.
CREATE TABLE runs_rebuilt (
    run_id      TEXT NOT NULL PRIMARY KEY,
    host_id     TEXT NOT NULL REFERENCES hosts(host_id),
    object_id   TEXT NOT NULL REFERENCES objects(object_id),
    operation   TEXT NOT NULL CHECK (operation IN ('network.interface.reconcile_mtu')),
    state       TEXT NOT NULL CHECK (state IN ('preview', 'cancelled')),
    preview_id  TEXT NOT NULL UNIQUE REFERENCES run_previews_rebuilt(preview_id),
    host_effect TEXT NOT NULL CHECK (host_effect = 'none'),
    created_at   TEXT NOT NULL,
    cancelled_at TEXT,
    CHECK ((state = 'cancelled') = (cancelled_at IS NOT NULL))
) STRICT;

INSERT INTO runs_rebuilt (
    run_id, host_id, object_id, operation, state, preview_id, host_effect,
    created_at, cancelled_at
)
SELECT run_id, host_id, object_id, operation, state, preview_id, host_effect,
       created_at, cancelled_at
FROM runs;

DROP TABLE runs;

DROP TABLE run_previews;

ALTER TABLE run_previews_rebuilt RENAME TO run_previews;

ALTER TABLE runs_rebuilt RENAME TO runs;

CREATE INDEX idx_runs_host_created ON runs(host_id, created_at DESC);

CREATE INDEX idx_runs_object_created ON runs(object_id, created_at DESC);

-- The four triggers 0005 established, recreated against the rebuilt tables with their
-- wording unchanged. Their reasons have not changed either: a published plan is what
-- somebody was shown, a plan about one object may not cite another's intent or
-- observation, and a Run may not be re-pointed at a different plan.
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

CREATE TRIGGER runs_identity_is_immutable
BEFORE UPDATE OF run_id, host_id, object_id, operation, preview_id, host_effect, created_at
ON runs
BEGIN
    SELECT RAISE(ABORT, 'a run''s operation, target and published plan are immutable');
END;

-- And one new trigger: a plan's management-path evidence must be evidence about the host
-- the Run is on. A preview citing another host's connection would be internally coherent
-- and completely false, which is the same failure the two triggers above exist to prevent,
-- and no CHECK can look at another table.
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
