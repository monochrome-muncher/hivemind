-- Rollback (ADR 0020): drops the redaction marker and removes the
-- `entry.redact` audit rows the 0031 CHECK cannot hold (`hivemind-migrate`
-- refuses this unless `--discard-audit-rows` is given, ADR 0119).
--
-- This does NOT bring any redacted text back: the text, the embedding and
-- the keyword index are gone for good (ADR 0125). The memories stay
-- withdrawn with their stub, and a withdrawal reason of `[redacted]` keeps
-- an older release's cleanup undo from reactivating them.
DELETE FROM audit_log WHERE action = 'entry.redact';
DROP INDEX IF EXISTS audit_log_redact_author_idx;

SET LOCAL lock_timeout = '5s';
ALTER TABLE entries DROP COLUMN IF EXISTS redacted_at;
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
        'entry.restore'
    ));
