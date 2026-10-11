-- Rollback (ADR 0020): the 0013 schema has no representation for a pin
-- row, so the `entry.pin` / `entry.unpin` rows (the only ones that can
-- carry `agent_key`) are removed before the narrower CHECKs are restored.
-- This loses exactly the pin history 0014 started recording; the pins
-- themselves are untouched.
DELETE FROM audit_log WHERE action IN ('entry.pin', 'entry.unpin') OR actor_kind = 'agent_key';

ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_actor_kind_check;
ALTER TABLE audit_log
    ADD CONSTRAINT audit_log_actor_kind_check
    CHECK (actor_kind IN ('admin_key', 'cli', 'org_key'));

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
        'agent_key.issue'
    ));
