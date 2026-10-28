-- Rollback (ADR 0020): the 0022 schema has no representation for a
-- `fleet.move_agents` / `fleet.move_undo` audit row or a move record, so
-- those audit rows are removed before the narrower CHECK is restored, and
-- the move records go (with their entry lists) before the `kind` column
-- does. This loses the move history 0023 started recording; the entries and
-- agents a move re-filed stay where they are, and merge records are kept.
DELETE FROM audit_log WHERE action IN ('fleet.move_agents', 'fleet.move_undo');

SET LOCAL lock_timeout = '5s';
DELETE FROM fleet_merge_entries
 WHERE merge_id IN (SELECT id FROM fleet_merges WHERE kind = 'move');
DELETE FROM fleet_merges WHERE kind = 'move';
ALTER TABLE fleet_merges DROP COLUMN IF EXISTS kind;

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
        'agent.cleanup_undo'
    ));
