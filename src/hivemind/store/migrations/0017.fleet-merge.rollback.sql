-- Rollback (ADR 0020): the 0016 schema has no representation for a
-- `fleet.merge` / `fleet.unmerge` audit row, so those rows are removed
-- before the narrower CHECK is restored, and the merge record goes with
-- its tables. This loses the merge history 0017 started recording; the
-- entries and agents a merge moved stay where they are, and merged fleets
-- become ordinary (empty) fleets again.
DELETE FROM audit_log WHERE action IN ('fleet.merge', 'fleet.unmerge');

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
        'entry.unpin'
    ));

DROP TABLE IF EXISTS fleet_merge_entries;
DROP TABLE IF EXISTS fleet_merges;
ALTER TABLE fleets DROP COLUMN IF EXISTS merged_at;
ALTER TABLE fleets DROP COLUMN IF EXISTS merged_into;
