-- ADR 0109: an admin can rename a fleet. No table or column changes: a
-- fleet keeps its id, so pins, counters and entries are untouched. The audit
-- CHECK is widened the 0017 way for the new action `fleet.rename`.
--
-- ADR 0020 expand-and-contract: a widened CHECK is additive. An older
-- sibling pod never writes the action, and the rename route is served only
-- by a pod that has this migration's code. The ALTER takes a brief lock;
-- give up rather than queue behind a long read (the migration then fails
-- and is retried).
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
        'fleet.rename'
    ));
