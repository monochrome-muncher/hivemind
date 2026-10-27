-- Rollback (ADR 0020): the 0020 schema has no representation for an
-- `agent.cleanup` / `agent.cleanup_undo` audit row, so those rows are removed
-- before the narrower CHECK is restored, and the cleanup record goes with
-- its table. This loses the cleanup history 0022 started recording. The
-- memories a cleanup withdrew or restored stay as they are, and the ratings
-- it set aside count again, because the mark that hid them is dropped with
-- the column (the rows are kept).
DELETE FROM audit_log WHERE action IN ('agent.cleanup', 'agent.cleanup_undo');

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
        'agent_key.replace'
    ));

DROP INDEX IF EXISTS agent_cleanups_agent_idx;
DROP TABLE IF EXISTS agent_cleanups;
ALTER TABLE feedbacks DROP COLUMN IF EXISTS set_aside_by;
