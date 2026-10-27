-- ADR 0107: merge one fleet into another. A merged fleet keeps its row and
-- is marked with the fleet it went into (`merged_into`) and when
-- (`merged_at`); it takes no new members or memories.
--
-- Two new tables record a merge so an undo can reverse exactly what moved:
-- `fleet_merges` (one row per merge: the agents moved, the pins dropped to
-- fit the briefing cap, and when it was undone) and `fleet_merge_entries`
-- (the ids of the entries re-filed). Like `pins` and `entry_links` they
-- have no FKs, so tooling that truncates `entries` or `fleets` (older
-- releases' test suites, ADR 0020's backward-compat check) keeps working,
-- and the hot `entries` table gains no column.
--
-- The audit CHECK is widened the 0014 way for two new actions,
-- `fleet.merge` and `fleet.unmerge`.
--
-- ADR 0020 expand-and-contract: two nullable columns, two new tables and a
-- widened CHECK are additive. An older sibling pod never reads the tables
-- and ignores the columns; it would list a merged fleet as a normal one
-- and could assign an agent to it for the one-rollout window (the same
-- window ADR 0028 accepted for `revoked`). Such a pod also skips the
-- merge's row-lock guard, so its entry insert or pin can still land in a
-- merged source fleet, and an agent it assigns to a merged fleet gets
-- 409 `fleet_merged` until moved. Do not merge during a rollout. The ALTERs take brief locks;
-- give up rather than queue behind a long read (the migration then fails
-- and is retried).
SET LOCAL lock_timeout = '5s';
ALTER TABLE fleets ADD COLUMN IF NOT EXISTS merged_into uuid;
ALTER TABLE fleets ADD COLUMN IF NOT EXISTS merged_at timestamptz;

CREATE TABLE IF NOT EXISTS fleet_merges (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    source_fleet_id uuid        NOT NULL,
    target_fleet_id uuid        NOT NULL,
    merged_by       text        NOT NULL,
    merged_at       timestamptz NOT NULL DEFAULT now(),
    moved_agents    text[]      NOT NULL DEFAULT '{}',
    dropped_pins    jsonb       NOT NULL DEFAULT '[]',
    undone_at       timestamptz
);

CREATE TABLE IF NOT EXISTS fleet_merge_entries (
    merge_id uuid NOT NULL,
    entry_id uuid NOT NULL,
    PRIMARY KEY (merge_id, entry_id)
);

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
        'fleet.unmerge'
    ));
