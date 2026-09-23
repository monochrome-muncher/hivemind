-- ADR 0027, SPEC §12.5: the audit log of admin-surface actions — who
-- activated, promoted, demoted, re-parented or revoked an agent, created
-- a fleet, rotated the org key, issued or revoked a key, or withdrew
-- another agent's entry as an admin.
--
-- Insert-only: nothing in the codebase updates or deletes a row. No
-- foreign keys, deliberately: `target` is a bare string, so a revoked
-- agent's history outlives whatever later happens to its record, and a
-- key fingerprint never points at a `credentials` row that may be gone.
--
-- `actor_kind` says how far to trust `actor`: `admin_key` rows come from
-- the app admin surface (a verified admin key, recorded by fingerprint);
-- `cli` rows come from `hivemind-keys`, whose `--actor` is operator-typed
-- and UNVERIFIED. No column ever holds a raw API key (ADR 0027).
--
-- Both enum-shaped columns get a NAMED CHECK, the pattern every other
-- enum-shaped column in the schema uses (0003).
--
-- Additive-only (ADR 0020 expand-and-contract): a new table is invisible
-- to a not-yet-upgraded sibling project.
CREATE TABLE IF NOT EXISTS audit_log (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    actor_kind  text        NOT NULL,
    actor       text        NOT NULL,
    action      text        NOT NULL,
    target      text,
    detail      jsonb       NOT NULL DEFAULT '{}',
    CONSTRAINT audit_log_actor_kind_check
        CHECK (actor_kind IN ('admin_key', 'cli')),
    CONSTRAINT audit_log_action_check
        CHECK (action IN (
            'agent.activate',
            'agent.trust_level_set',
            'agent.home_fleet_set',
            'agent.revoke',
            'fleet.create',
            'org_key.rotate',
            'entry.withdraw',
            'admin_key.issue',
            'admin_key.revoke',
            'agent_key.issue'
        ))
);

-- The read surface (GET /v1/admin/audit-log) is newest-first, filterable
-- by actor. Plain (not CONCURRENTLY) index builds: the table is created
-- empty in this same transaction, so there is nothing to lock out.
CREATE INDEX IF NOT EXISTS audit_log_occurred_at_idx ON audit_log (occurred_at);
CREATE INDEX IF NOT EXISTS audit_log_actor_idx ON audit_log (actor);
