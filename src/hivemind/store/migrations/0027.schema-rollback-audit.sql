-- ADR 0119: a rollback is recorded. `hivemind-migrate --rollback` writes one
-- `schema.rollback` audit row (the migrations undone, the audit rows it
-- discarded) when the schema still has this migration after the rollback.
--
-- The audit CHECK is widened the 0014 ... 0025 way, for `schema.rollback`.
--
-- ADR 0020 expand-and-contract: a widened CHECK is additive; an older pod
-- never writes the action.
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
