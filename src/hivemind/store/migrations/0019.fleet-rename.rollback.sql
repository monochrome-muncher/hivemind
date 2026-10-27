-- Rollback (ADR 0020): the 0017 schema has no representation for a
-- `fleet.rename` audit row, so those rows are removed before the narrower
-- CHECK is restored. This loses the rename history 0019 started recording;
-- renamed fleets keep their new names.
DELETE FROM audit_log WHERE action = 'fleet.rename';

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
        'fleet.unmerge'
    ));
