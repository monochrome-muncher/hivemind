-- ADR 0124: the admin can put one withdrawn memory back. The restore is
-- audited as `entry.restore` (target: the entry id; detail: author, the
-- reason it was withdrawn with, the restore reason).
--
-- The audit CHECK is widened the 0014 ... 0029 way, for `entry.restore` (the list
-- keeps 0029's `agent.owner_set`).
--
-- A partial index serves the agent history (ADR 0123): `entry.restore` names the
-- memory's author in `detail.author`, as `entry.withdraw` does, and the subject
-- query needs an index on every arm to stay a BitmapOr. Not `CONCURRENTLY`
-- (that would make the whole file non-transactional, and the CHECK change
-- below wants its lock timeout): the index is partial on an action that has no
-- rows yet, so the build is one quick scan of `audit_log`.
--
-- ADR 0020 expand-and-contract: a widened CHECK and a new index are additive;
-- an older pod never writes the action.
SET LOCAL lock_timeout = '5s';
CREATE INDEX IF NOT EXISTS audit_log_restore_author_idx
    ON audit_log ((detail->>'author'), occurred_at)
    WHERE action = 'entry.restore';

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
