-- ADR 0111: clean up after a bad agent. An admin withdraws the memories an
-- agent wrote in a time window, sets its ratings of that window aside, and
-- puts back the memories it superseded. An undo reverses exactly that.
--
-- `feedbacks.set_aside_by` marks a rating set aside by a cleanup (the
-- cleanup's id). The row is kept; every feedback read skips it, the way ADR
-- 0067 already skips a revoked agent's rows. The agent rating the same
-- memory again clears the mark.
--
-- `agent_cleanups` records one cleanup so an undo can restore what it
-- changed: the withdrawn entry ids, the restored entries with the memory
-- that had superseded each, the pins taken off the withdrawn entries and the
-- number of ratings set aside (the ratings themselves carry the mark). Like
-- `fleet_merges` it has no FKs, so tooling that truncates `entries` keeps
-- working, and the hot `entries` table gains no column.
--
-- The audit CHECK is widened the 0017 way for two new actions,
-- `agent.cleanup` and `agent.cleanup_undo`.
--
-- ADR 0020 expand-and-contract: one nullable column, one new table and a
-- widened CHECK are additive. An older sibling pod ignores the column, so
-- during a rolling deploy it still counts a set-aside rating and its
-- upsert does not clear the mark. Do not run a cleanup during a rollout.
SET LOCAL lock_timeout = '5s';
ALTER TABLE feedbacks ADD COLUMN IF NOT EXISTS set_aside_by uuid;

CREATE TABLE IF NOT EXISTS agent_cleanups (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    agent             text        NOT NULL,
    since             timestamptz NOT NULL,
    until             timestamptz NOT NULL,
    cleaned_by        text        NOT NULL,
    cleaned_at        timestamptz NOT NULL DEFAULT now(),
    withdrawn_ids     uuid[]      NOT NULL DEFAULT '{}',
    restored          jsonb       NOT NULL DEFAULT '[]',
    ratings_set_aside integer     NOT NULL DEFAULT 0,
    unpinned_pins     jsonb       NOT NULL DEFAULT '[]',
    revoked           boolean     NOT NULL DEFAULT false,
    undone_at         timestamptz
);

CREATE INDEX IF NOT EXISTS agent_cleanups_agent_idx ON agent_cleanups (agent, cleaned_at DESC);

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
        'agent.cleanup_undo'
    ));
