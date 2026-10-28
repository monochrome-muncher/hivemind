-- Rollback (ADR 0020): the 0023 schema has no representation for an
-- `agent.retire` / `agent.retire_undo` audit row, so those rows are removed
-- before the narrower CHECK is restored, and the successions go with their
-- table. A successor loses the memories it inherited (only an admin reads
-- them then); retired agents stay revoked. Deploy the previous release first,
-- then roll back: this release reads `successions` in every key check, so
-- running it against the dropped table would fail every agent request.
DELETE FROM audit_log WHERE action IN ('agent.retire', 'agent.retire_undo');

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
        'fleet.move_undo'
    ));

DROP TABLE IF EXISTS successions;
