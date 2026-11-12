-- Rollback (ADR 0020): drops the role column, so every agent loses its role,
-- and removes the `agent.role_set` and `agent.role_clear` audit rows the 0033
-- CHECK cannot hold (`hivemind-migrate` refuses this unless
-- `--discard-audit-rows` is given, ADR 0119).
DELETE FROM audit_log WHERE action IN ('agent.role_set', 'agent.role_clear');

SET LOCAL lock_timeout = '5s';
ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_role_check;
ALTER TABLE agents DROP COLUMN IF EXISTS role;
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
        'fleet.phase_clear'
    ));
