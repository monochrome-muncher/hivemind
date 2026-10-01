-- Rollback (ADR 0020): the pre-0009 schema has no representation for a
-- registration row, so the `agent.register` rows (the only ones that can
-- carry `org_key`) are removed before the narrow CHECKs are restored. This
-- loses exactly the registration history 0009 started recording; the
-- agents themselves are untouched.
DELETE FROM audit_log WHERE action = 'agent.register' OR actor_kind = 'org_key';

ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_actor_kind_check;
ALTER TABLE audit_log
    ADD CONSTRAINT audit_log_actor_kind_check
    CHECK (actor_kind IN ('admin_key', 'cli'));

ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_action_check;
ALTER TABLE audit_log
    ADD CONSTRAINT audit_log_action_check
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
    ));
