-- Rollback (ADR 0020): the 0017 schema has no representation for an
-- `agent_key.replace` audit row, so those rows are removed before the
-- narrower CHECK is restored. The replacement keys themselves stay valid.
DELETE FROM audit_log WHERE action = 'agent_key.replace';

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
