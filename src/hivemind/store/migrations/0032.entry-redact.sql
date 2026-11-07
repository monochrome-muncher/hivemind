-- ADR 0125: an admin can redact a withdrawn memory that holds a secret or
-- personal data. `redacted_at` marks a memory whose text, embedding and
-- keyword index were wiped; NULL for every other memory.
--
-- The audit CHECK is widened the 0014 ... 0029 way, for `entry.redact`.
--
-- ADR 0020 expand-and-contract: one nullable column with no default (a
-- catalogue change that rewrites nothing) and a widened CHECK. An older pod
-- ignores the column and never writes the action. Nothing is backfilled: no
-- memory is redacted until an admin does it.
SET LOCAL lock_timeout = '5s';
ALTER TABLE entries ADD COLUMN IF NOT EXISTS redacted_at timestamptz;
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
        'entry.redact'
    ));
