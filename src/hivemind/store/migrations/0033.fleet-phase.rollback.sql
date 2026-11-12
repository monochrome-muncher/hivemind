-- Rollback (ADR 0020): drops the phase columns, so every fleet phase in force
-- ends, and removes the `fleet.phase_set` and `fleet.phase_clear` audit rows
-- the 0032 CHECK cannot hold (`hivemind-migrate` refuses this unless
-- `--discard-audit-rows` is given, ADR 0119).
DELETE FROM audit_log WHERE action IN ('fleet.phase_set', 'fleet.phase_clear');

SET LOCAL lock_timeout = '5s';
ALTER TABLE fleets DROP CONSTRAINT IF EXISTS fleets_phase_pair_check;
ALTER TABLE fleets DROP CONSTRAINT IF EXISTS fleets_phase_check;
ALTER TABLE fleets DROP COLUMN IF EXISTS phase_until;
ALTER TABLE fleets DROP COLUMN IF EXISTS phase;
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
        'schema.rollback',
        'agent.owner_set',
        'entry.restore',
        'entry.redact'
    ));
