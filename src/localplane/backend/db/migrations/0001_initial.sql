-- The minimum durable model for read-only observation.
--
-- Five concerns, five tables, and the boundaries between them are the point:
--
--   hosts               who this is
--   agent_instances     what reported it, and with how much privilege
--   agent_capabilities  what that agent could actually do, as probed
--   objects             identity and management stance — the parts that persist
--   observations        what was seen at a moment, and the evidence for it
--   observation_sweeps  the batch an observation arrived in, and whether it was whole
--
-- Nothing here caches a derived value. Health is stored because it is a judgement made
-- at a point in time about specific evidence and is part of the record; freshness is not
-- stored, because it changes without anything happening. Current state is a query
-- against the newest observation, not a column somebody has to remember to update.
--
-- Tables are STRICT so a type error is an error, and every enumerated column carries a
-- CHECK so an invalid state cannot be written even by code that has not read this file.

CREATE TABLE hosts (
    host_id             TEXT    NOT NULL PRIMARY KEY,
    identity_basis      TEXT    NOT NULL,
    identity_confidence TEXT    NOT NULL CHECK (identity_confidence IN ('high', 'low')),
    hostname            TEXT,
    configured_hostname TEXT,
    boot_id             TEXT,
    os_id               TEXT,
    os_version_id       TEXT,
    os_pretty_name      TEXT,
    kernel_name         TEXT,
    kernel_release      TEXT,
    architecture        TEXT,
    identity_gaps       TEXT    NOT NULL DEFAULT '[]',
    first_seen_at       TEXT    NOT NULL,
    last_seen_at        TEXT    NOT NULL
) STRICT;

-- One row per agent *process*. A restart is a new instance, and that is deliberate:
-- LocalPlane should be able to see that the thing reporting on this host was replaced.
CREATE TABLE agent_instances (
    agent_instance_id TEXT    NOT NULL PRIMARY KEY,
    host_id           TEXT    NOT NULL REFERENCES hosts(host_id),
    agent_version     TEXT    NOT NULL,
    protocol_version  TEXT    NOT NULL,
    transport         TEXT    NOT NULL,
    process_isolated  INTEGER NOT NULL CHECK (process_isolated IN (0, 1)),
    privilege         TEXT    NOT NULL CHECK (privilege IN ('unprivileged', 'root')),
    effective_uid     INTEGER,
    pid               INTEGER,
    started_at        TEXT    NOT NULL,
    first_contact_at  TEXT    NOT NULL,
    last_contact_at   TEXT    NOT NULL
) STRICT;

CREATE INDEX idx_agent_instances_host ON agent_instances(host_id, last_contact_at DESC);

-- Capabilities are recorded per agent instance because they are a property of what that
-- process probed, not of the host in the abstract. An absent row means the capability was
-- not reported, which the backend must treat exactly as it treats 'unavailable'.
CREATE TABLE agent_capabilities (
    agent_instance_id TEXT    NOT NULL REFERENCES agent_instances(agent_instance_id),
    capability        TEXT    NOT NULL,
    version           INTEGER NOT NULL,
    status            TEXT    NOT NULL CHECK (status IN ('available', 'degraded', 'unavailable')),
    mutating          INTEGER NOT NULL CHECK (mutating IN (0, 1)),
    summary           TEXT    NOT NULL,
    reason            TEXT,
    detail            TEXT    NOT NULL DEFAULT '{}',
    discovered_at     TEXT    NOT NULL,
    PRIMARY KEY (agent_instance_id, capability)
) STRICT;

-- The durable half of an object: who it is and what LocalPlane's stance towards it is.
-- Everything that changes minute to minute lives in observations instead.
CREATE TABLE objects (
    object_id           TEXT NOT NULL PRIMARY KEY,
    host_id             TEXT NOT NULL REFERENCES hosts(host_id),
    kind                TEXT NOT NULL,
    identity_basis      TEXT NOT NULL CHECK (identity_basis IN ('permanent_mac', 'device_path', 'kernel_name')),
    identity_value      TEXT NOT NULL,
    identity_confidence TEXT NOT NULL CHECK (identity_confidence IN ('high', 'low')),
    display_name        TEXT NOT NULL,
    management_state    TEXT NOT NULL CHECK (management_state IN ('observe_only', 'observed', 'managed')),
    management_reason   TEXT NOT NULL,
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    UNIQUE (host_id, kind, identity_basis, identity_value)
) STRICT;

CREATE INDEX idx_objects_host_kind ON objects(host_id, kind, display_name);

-- One row per ingested batch. It exists so that an empty result can be explained: a
-- caller can tell "nothing is there" from "the last sweep failed" without guessing.
CREATE TABLE observation_sweeps (
    sweep_id          TEXT    NOT NULL PRIMARY KEY,
    host_id           TEXT    NOT NULL REFERENCES hosts(host_id),
    agent_instance_id TEXT    REFERENCES agent_instances(agent_instance_id),
    capability        TEXT    NOT NULL,
    provider          TEXT    NOT NULL,
    provider_version  TEXT    NOT NULL,
    status            TEXT    NOT NULL CHECK (status IN ('ok', 'partial', 'failed')),
    started_at        TEXT    NOT NULL,
    completed_at      TEXT    NOT NULL,
    received_at       TEXT    NOT NULL,
    object_count      INTEGER NOT NULL,
    missing           TEXT    NOT NULL DEFAULT '[]',
    issues            TEXT    NOT NULL DEFAULT '[]'
) STRICT;

CREATE INDEX idx_sweeps_host_received ON observation_sweeps(host_id, received_at DESC);

-- Append-only. An observation is what was true at a moment and is never rewritten; the
-- newest row for an object is its current state.
CREATE TABLE observations (
    observation_id   TEXT NOT NULL PRIMARY KEY,
    sweep_id         TEXT NOT NULL REFERENCES observation_sweeps(sweep_id),
    host_id          TEXT NOT NULL REFERENCES hosts(host_id),
    object_id        TEXT NOT NULL REFERENCES objects(object_id),
    capability       TEXT NOT NULL,
    provider         TEXT NOT NULL,
    provider_version TEXT NOT NULL,
    method           TEXT NOT NULL,
    fidelity         TEXT NOT NULL CHECK (fidelity IN ('complete', 'partial', 'degraded')),
    observed_at      TEXT NOT NULL,
    received_at      TEXT NOT NULL,
    health_state     TEXT NOT NULL CHECK (health_state IN ('healthy', 'degraded', 'failed', 'inactive', 'unknown')),
    health_reason    TEXT NOT NULL,
    gaps             TEXT NOT NULL DEFAULT '[]',
    facts            TEXT NOT NULL,
    evidence         TEXT NOT NULL DEFAULT '{}'
) STRICT;

CREATE INDEX idx_observations_object_time ON observations(object_id, observed_at DESC);
CREATE INDEX idx_observations_sweep ON observations(sweep_id);
