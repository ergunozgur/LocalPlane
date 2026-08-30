-- The durable model for ownership: what the providers said, and the safety claims that
-- follow when they contradict something LocalPlane has taken responsibility for.
--
-- Two tables:
--
--   provider_observations  what Docker, NetworkManager and Tailscale said, per sweep
--   ownership_findings     a managed object proven to be run by something else
--
-- and nothing added to `objects`. That omission is the central decision in this migration
-- and it is deliberate.
--
-- WHY OWNERSHIP IS NOT A COLUMN
--
-- Ownership is derived, exactly as freshness and reconciliation are. It is a function of
-- two things that already exist here — the newest observation of an interface and the
-- newest reading from each provider — and storing the result would create a second copy of
-- a fact that changes whenever either input changes. That copy would then need to be
-- recomputed on every sweep, would be stale between them, and would be the thing an
-- operator sees while the evidence underneath it says something else.
--
-- What *is* stored is the evidence. That is what makes the derivation auditable rather
-- than merely repeatable: an intent records the observation it was captured from, that
-- observation records its sweep, and the provider readings of that sweep are here. What
-- LocalPlane knew at the moment it agreed to manage an object can be reconstructed exactly,
-- without having written down a conclusion that might since have been improved.
--
-- WHAT IS NOT HERE
--
-- No management state changes. Nothing in this migration can move an object between
-- observe_only, observed and managed: those three remain the whole of the management axis,
-- and a provider's opinion is not a transition. An object proven to be Docker's stays
-- exactly where the operator left it, and what changes is that adoption is refused and —
-- if it was already managed — that the conflict is recorded below.

-- One row per provider per sweep. Append-only, like observations, and for the same reason:
-- what a provider said at a moment is not something later evidence may rewrite.
--
-- `records` is JSON because the providers answer with different shapes — a list of Docker
-- networks, a device table, a daemon's own state — and because nothing queries into them
-- in SQL. They are read whole, by the derivation, in Python. The shapes are normalized and
-- documented by each provider; what was dropped from the provider's full output is
-- declared in `detail` rather than left for a reader to discover.
CREATE TABLE provider_observations (
    provider_observation_id TEXT NOT NULL PRIMARY KEY,
    sweep_id         TEXT NOT NULL REFERENCES observation_sweeps(sweep_id),
    host_id          TEXT NOT NULL REFERENCES hosts(host_id),
    -- Which system was asked, and which of its surfaces answered. Both, because one
    -- provider may grow a second source and a reader must be able to tell them apart.
    provider         TEXT NOT NULL,
    source           TEXT NOT NULL,
    -- 'absent' is not a failure and must never be read as one: a daemon that is not
    -- installed owns nothing on this host, which is a conclusion. 'unavailable' and
    -- 'error' are the gaps.
    status           TEXT NOT NULL CHECK (status IN ('ok', 'absent', 'unavailable', 'error')),
    reason           TEXT,
    method           TEXT NOT NULL,
    provider_version TEXT,
    observed_at      TEXT NOT NULL,
    received_at      TEXT NOT NULL,
    records          TEXT NOT NULL DEFAULT '[]',
    detail           TEXT NOT NULL DEFAULT '{}',
    -- A reading that did not succeed must say why...
    CHECK (status = 'ok' OR reason IS NOT NULL),
    -- ...and must carry no records. Enforced here rather than in the writer so that a
    -- failed consultation cannot smuggle half an answer past any code path at all.
    CHECK (status = 'ok' OR records = '[]'),
    UNIQUE (sweep_id, provider, source)
) STRICT;

CREATE INDEX idx_provider_observations_newest
    ON provider_observations(host_id, provider, observed_at DESC);

-- A managed object that another system is demonstrably running.
--
-- This is a finding in the same sense as drift, and it keeps the same lifecycle: a
-- deterministic `finding_key` so re-observing the same conflict updates one row, a
-- per-episode `finding_id` so a conflict that resolved and recurred keeps both histories,
-- `last_seen_at` moving only when the claim was proven again, and resolution rather than
-- deletion.
--
-- It is a separate table from `findings` because its evidence is a different kind of
-- thing. A drift finding's evidence is one typed scalar comparison against one controlled
-- field, and every column of it is about that; an ownership conflict's evidence is a claim
-- about a relation, an owner and the source that established it. Forcing the second into
-- the first's columns would mean an intended_value that means nothing, and the CHECKs that
-- make drift evidence coherent would have to be loosened until they stopped saying
-- anything. The 0002 migration said as much when it defined them.
--
-- There is no severity here either, for the same reason there is none there.
CREATE TABLE ownership_findings (
    finding_id      TEXT NOT NULL PRIMARY KEY,
    finding_key     TEXT NOT NULL,
    host_id         TEXT NOT NULL REFERENCES hosts(host_id),
    object_id       TEXT NOT NULL REFERENCES objects(object_id),
    finding_type    TEXT NOT NULL CHECK (finding_type IN ('network.interface.ownership_conflict')),
    -- The relation in conflict: what the other system is doing to this object.
    subject         TEXT NOT NULL CHECK (subject IN ('created_by', 'configured_by')),
    status          TEXT NOT NULL CHECK (status IN ('open', 'resolved')),

    -- The intent this conflicts with. NOT NULL because the conflict is only meaningful for
    -- a managed object: LocalPlane holding intent for something somebody else is running
    -- is the whole of the claim. An observed object owned by Docker is not a finding, it
    -- is just a fact, and it is reported as one.
    intent_id       TEXT NOT NULL REFERENCES intents(intent_id),

    -- evidence: the claim, and where it came from
    owner_provider  TEXT NOT NULL,
    owner_instance  TEXT,
    owner_label     TEXT,
    confidence      TEXT NOT NULL CHECK (confidence IN ('confirmed', 'corroborated')),
    evidence_source TEXT NOT NULL,
    reason          TEXT NOT NULL,
    provider_observation_id TEXT REFERENCES provider_observations(provider_observation_id),
    observation_id  TEXT REFERENCES observations(observation_id),
    sweep_id        TEXT REFERENCES observation_sweeps(sweep_id),

    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    updated_at      TEXT NOT NULL,

    resolved_at     TEXT,
    -- 'owner_no_longer_claims' requires the sources to have been consulted and to have
    -- stopped claiming the object. A provider that could not be read proves nothing and
    -- resolves nothing — the finding stays open and `updated_at` moves without
    -- `last_seen_at`, which is exactly how a drift finding says the same thing.
    resolution      TEXT CHECK (
        resolution IS NULL OR resolution IN ('owner_no_longer_claims', 'intent_released')
    ),
    resolved_by_provider_observation_id TEXT
        REFERENCES provider_observations(provider_observation_id),

    CHECK ((status = 'resolved') = (resolved_at IS NOT NULL)),
    CHECK ((status = 'resolved') = (resolution IS NOT NULL)),
    CHECK (
        resolved_by_provider_observation_id IS NULL
        OR resolution = 'owner_no_longer_claims'
    )
) STRICT;

CREATE UNIQUE INDEX idx_ownership_findings_one_open_per_key
    ON ownership_findings(finding_key) WHERE status = 'open';
CREATE INDEX idx_ownership_findings_object
    ON ownership_findings(object_id, status, first_seen_at DESC);
CREATE INDEX idx_ownership_findings_host
    ON ownership_findings(host_id, status, first_seen_at DESC);
