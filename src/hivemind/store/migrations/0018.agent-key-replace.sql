-- ADR 0108: an admin can replace an active agent's key in one step. The audit
-- CHECK is widened the 0017 way for one new action, `agent_key.replace`.
--
-- ADR 0020 expand-and-contract: a widened CHECK is additive. An older sibling
-- pod never writes the new action. The ALTER takes a brief lock; give up
-- rather than queue behind a long read (the migration then fails and is
-- retried).
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
        'agent_key.replace'
    ));
