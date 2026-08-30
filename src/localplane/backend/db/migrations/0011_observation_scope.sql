-- Distinguish an estate inventory from a targeted observation.
--
-- Both are append-only observation sweeps, but only an inventory sweep establishes the
-- completeness boundary for an estate.  A targeted read may refresh one object's facts
-- without implying that every other known object was absent from that read.  This is a
-- generic observation invariant, not systemd-specific storage and not a runtime mirror.

ALTER TABLE observation_sweeps
    ADD COLUMN scope TEXT NOT NULL DEFAULT 'inventory'
        CHECK (scope IN ('inventory', 'targeted'));

CREATE INDEX idx_sweeps_host_capability_scope_received
    ON observation_sweeps(host_id, capability, scope, received_at DESC);
