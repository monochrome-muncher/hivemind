-- Rollback (ADR 0020): the 0024 schema has no representation for an
-- `agent_key.lock` audit row, so those rows are removed before the narrower
-- CHECK is restored. A locked key becomes usable again (the lock is the
-- column); an admin who wants it dead replaces the key (ADR 0108).
DELETE FROM audit_log WHERE action = 'agent_key.lock';

SET LOCAL lock_timeout = '5s';
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
        'agent.retire_undo'
    ));


ALTER TABLE credentials DROP COLUMN IF EXISTS locked_at;
