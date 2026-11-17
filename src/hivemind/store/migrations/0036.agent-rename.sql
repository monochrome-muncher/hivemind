-- ADR 0159: an admin renames an agent. The name is the identity and is copied as
-- plain text into other tables, so the rename rewrites those copies in one
-- transaction (no schema change for that). What the schema adds:
--
-- `agent_former_names` keeps every name an agent has left, so no new agent can
-- take it (the audit log and the history keep the old names). `agent` is the
-- agent's CURRENT name, rewritten on every later rename; the unique index on
-- `lower(name)` is the case-insensitive reservation (ADR 0045).
--
-- The audit CHECK is widened the 0014 ... 0035 way, for `agent.rename`.
--
-- ADR 0020 expand-and-contract: a new table and a widened CHECK are additive;
-- an older pod never writes the action and never reads the table.
SET LOCAL lock_timeout = '5s';
CREATE TABLE IF NOT EXISTS agent_former_names (
    name       text        NOT NULL,
    agent      text        NOT NULL,
    renamed_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS agent_former_names_name_key
    ON agent_former_names (lower(name));
CREATE INDEX IF NOT EXISTS agent_former_names_agent_idx
    ON agent_former_names (agent);
ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_action_check;
ALTER TABLE audit_log
    ADD CONSTRAINT audit_log_action_check
    CHECK (action IN (
        'agent.register',
        'agent.activate',
        'agent.trust_level_set',
        'agent.home_fleet_set',
        'agent.revoke',
        'fleet.create',
        'org_key.rotate',
        'entry.withdraw',
        'admin_key.issue',
        'admin_key.revoke',
        'agent_key.issue',
        'entry.pin',
        'entry.unpin',
        'fleet.merge',
        'fleet.unmerge',
        'fleet.rename',
        'agent_key.replace',
        'agent.cleanup',
        'agent.cleanup_undo',
        'fleet.move_agents',
        'fleet.move_undo',
        'agent.retire',
        'agent.retire_undo',
        'agent_key.lock',
        'schema.rollback',
        'agent.owner_set',
        'entry.restore',
        'entry.redact',
        'fleet.phase_set',
        'fleet.phase_clear',
        'agent.role_set',
        'agent.role_clear',
        'entry.move_to_self',
        'agent.rename'
    ));
