-- ADR 0143: an admin can put a fleet in an `explore` or `consolidate` phase
-- until a date. `phase` and `phase_until` are NULL for a fleet with no phase;
-- a phase whose `phase_until` has passed counts as none (nothing clears it).
--
-- The audit CHECK is widened the 0014 ... 0032 way, for `fleet.phase_set` and
-- `fleet.phase_clear`.
--
-- ADR 0020 expand-and-contract: two nullable columns with no default (a
-- catalogue change that rewrites nothing) and a widened CHECK. An older pod
-- ignores the columns and never writes the actions. Nothing is backfilled.
SET LOCAL lock_timeout = '5s';
ALTER TABLE fleets ADD COLUMN IF NOT EXISTS phase text;
ALTER TABLE fleets ADD COLUMN IF NOT EXISTS phase_until timestamptz;
ALTER TABLE fleets DROP CONSTRAINT IF EXISTS fleets_phase_check;
ALTER TABLE fleets
    ADD CONSTRAINT fleets_phase_check
    CHECK (phase IN ('explore', 'consolidate'));
ALTER TABLE fleets DROP CONSTRAINT IF EXISTS fleets_phase_pair_check;
ALTER TABLE fleets
    ADD CONSTRAINT fleets_phase_pair_check
    CHECK ((phase IS NULL) = (phase_until IS NULL));
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
        'entry.redact',
        'fleet.phase_set',
        'fleet.phase_clear'
    ));
