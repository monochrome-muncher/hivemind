-- ADR 0145: the admin, or the agent from the portal, can move an active fleet
-- memory to `self`. The move is audited as `entry.move_to_self` (target: the
-- entry id; detail: author, fleet, reason and the earlier versions moved with
-- it).
--
-- The audit CHECK is widened the 0014 ... 0034 way, for `entry.move_to_self`.
--
-- A partial index serves the agent history (ADR 0123): the action names the
-- memory's author in `detail.author`, as `entry.withdraw` does, and the subject
-- query needs an index on every arm to stay a BitmapOr. Not `CONCURRENTLY` (the
-- CHECK change below wants its lock timeout): the index is partial on an action
-- that has no rows yet, so the build is one quick scan.
--
-- ADR 0020 expand-and-contract: a widened CHECK and a new index are additive;
-- an older pod never writes the action.
SET LOCAL lock_timeout = '5s';
CREATE INDEX IF NOT EXISTS audit_log_move_to_self_author_idx
    ON audit_log ((detail->>'author'), occurred_at)
    WHERE action = 'entry.move_to_self';
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
        'fleet.phase_clear',
        'agent.role_set',
        'agent.role_clear',
        'entry.move_to_self'
    ));
