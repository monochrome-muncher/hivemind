-- Rollback (ADR 0020): the 0027 schema cannot hold an `agent.owner_set` row, so
-- those rows are removed before the narrower CHECK is restored.
-- `hivemind-migrate` refuses this rollback while such rows exist unless
-- `--discard-audit-rows` is given (ADR 0119). The owner aliases themselves stay
-- as corrected: they live on the agent row, which this migration never touches.
DELETE FROM audit_log WHERE action = 'agent.owner_set';

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
        'agent.retire_undo',
        'agent_key.lock',
        'schema.rollback'
    ));
