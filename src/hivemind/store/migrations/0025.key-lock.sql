-- ADR 0117: an agent's own key can be locked from the agent portal ("this key
-- leaked"). `locked_at` is NULL for a usable key; set, the key fails every
-- request with `key_locked` while its agent stays `active` (ratings, memories,
-- pins, trust and fleet untouched). A replaced key (ADR 0108) is a new row, so
-- it carries no lock.
--
-- The audit CHECK is widened the 0014 ... 0024 way, for `agent_key.lock`.
--
-- ADR 0020 expand-and-contract: one nullable column with no default (a
-- catalogue change that rewrites nothing) and a widened CHECK are additive.
-- An older sibling pod ignores the column, so for the one-rollout window it
-- still accepts a key that was locked in that time: do not lock a key during a
-- rollout. Nothing else reads the column.
SET LOCAL lock_timeout = '5s';
ALTER TABLE credentials ADD COLUMN IF NOT EXISTS locked_at timestamptz;

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
        'agent_key.lock'
    ));
