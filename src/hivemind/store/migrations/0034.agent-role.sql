-- ADR 0144: an admin can mark an agent as a scout. `role` is NULL for an agent
-- with no role; it only adds a hint to the agent's `hive_whoami`.
--
-- The audit CHECK is widened the 0014 ... 0033 way, for `agent.role_set` and
-- `agent.role_clear`.
--
-- ADR 0020 expand-and-contract: one nullable column with no default (a
-- catalogue change that rewrites nothing) and a widened CHECK. An older pod
-- ignores the column and never writes the actions. Nothing is backfilled.
SET LOCAL lock_timeout = '5s';
ALTER TABLE agents ADD COLUMN IF NOT EXISTS role text;
ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_role_check;
ALTER TABLE agents
    ADD CONSTRAINT agents_role_check
    CHECK (role IN ('scout'));
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
        'agent.role_clear'
    ));
