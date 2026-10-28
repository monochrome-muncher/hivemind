-- ADR 0113: retire an agent with a successor. A row says "retired_agent hands
-- every memory it wrote to successor": the successor then owns them (reads,
-- supersedes, withdraws, cannot rate them) while `entries.author` stays the
-- original name. Nothing is rewritten; the key check resolves the chain.
--
-- `retired_agent` is the primary key: an agent hands down to one successor at
-- a time (several agents may hand down to one successor). An undo deletes the
-- row (the audit log keeps the history); the agent can then be retired again.
-- No FKs, like `pins`, `entry_links` and `fleet_merges`: tooling that truncates
-- `agents` (older releases' test suites, ADR 0020's backward-compat check)
-- keeps working. The index serves the key check, which walks the chain from
-- successor to retired agent.
--
-- The audit CHECK is widened the 0014, 0017, 0020, 0022 and 0023 way, for `agent.retire` and
-- `agent.retire_undo`.
--
-- ADR 0020 expand-and-contract: a new table and a widened CHECK are additive.
-- An older sibling pod never reads the table, so for the one-rollout window it
-- gives a successor no ownership (it fails closed: less access, never more),
-- and it does not refuse activating a retired agent. Do not retire an agent
-- during a rollout. To roll the migration back, deploy the previous release
-- first (this release reads `successions` in every key check).
SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS successions (
    retired_agent text        PRIMARY KEY,
    successor     text        NOT NULL,
    by            text        NOT NULL,
    at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT successions_not_self CHECK (retired_agent <> successor)
);
CREATE INDEX IF NOT EXISTS successions_successor_idx ON successions (successor);

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
        'agent.retire_undo'
    ));
