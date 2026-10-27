-- ADR 0112: move some agents, with the fleet memories they wrote, to another
-- fleet. A move is recorded in the merge tables (0017) so one undo rule and
-- one history list cover both: `fleet_merges.kind` says which it was
-- ('merge', the default every existing row gets, or 'move'). A move never
-- marks its source fleet as merged. `fleet_merge_entries` holds the entry
-- ids a move re-filed, and `moved_agents` the agents.
--
-- The audit CHECK is widened the 0017 way for two new actions,
-- `fleet.move_agents` and `fleet.move_undo`.
--
-- ADR 0020 expand-and-contract: a NOT NULL column with a constant default
-- (no table rewrite) and a widened CHECK are additive. An older sibling pod
-- never reads these tables, and it never writes the new audit actions. The
-- move's lock guard needs this release on every pod, like the merge's: an
-- older pod's entry insert does not check the author's home fleet, so do
-- not move agents during a rollout. The ALTERs take brief locks; give up
-- rather than queue behind a long read (the migration then fails and is
-- retried).
SET LOCAL lock_timeout = '5s';
ALTER TABLE fleet_merges
    ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'merge'
    CHECK (kind IN ('merge', 'move'));

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
        'fleet.move_agents',
        'fleet.move_undo'
    ));
