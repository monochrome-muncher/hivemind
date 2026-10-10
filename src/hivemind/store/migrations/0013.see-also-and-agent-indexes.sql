-- transactional: false
--
-- Two read-path indexes; performance only, no behaviour change.
--
-- 1. `entry_links_to_created_idx` — the incoming "see also" reads
--    (`SELECT_LINKS_IN` / `SELECT_LINKS_IN_MANY`: `WHERE to_id = $1 ORDER BY
--    created_at DESC, from_id LIMIT $2`, limit MAX_LINKED_FROM = 20). The key
--    matches the WHERE and ORDER BY exactly and carries `from_id`, so the
--    read is an index-only scan that stops after 20 rows. The single-column
--    `entry_links_to_idx` (0011) had to sort every link to the entry: with
--    one entry linked 10,000 times (50k links) 2.9-5.2 ms -> 0.06 ms; 1,000
--    incoming 0.7 -> 0.03 ms; the two-id batch read 3.6-5.6 -> 0.1 ms.
--    2.9 MB per 50k links. `entry_links_to_idx` is then fully redundant
--    (the new index leads with `to_id`; nothing else filters on it) and is
--    dropped. A not-yet-upgraded sibling pod runs the same queries and the
--    planner uses the new index, so the drop is safe (ADR 0020).
--
-- 2. `entries_agent_created_idx` — `GET /v1/entries?agent=` had no index,
--    the pathology 0008 fixed for `author`: the planner walks
--    `entries_created_at_id_idx` and filters. At 200k entries an agent whose
--    1,500 entries are the oldest: 46 ms (198,502 rows filtered out) ->
--    0.06 ms; with an L2 visibility clause 35 -> 0.06 ms; a common agent
--    (~660 rows) 1.0 -> 0.1 ms. Keyword search with an agent filter still
--    uses the GIN index. ~11 MB per 200k entries; the write cost is noise
--    next to the per-write embedder call (0008 measured ~18% on a 20k bulk
--    insert for a similar index).
--
-- `CONCURRENTLY` (hence `-- transactional: false`, ADR 0020 §6 / ADR 0025)
-- so nothing takes a write lock while sibling replicas serve traffic;
-- `IF NOT EXISTS` because a cancelled concurrent build leaves an INVALID
-- index behind that a plain re-run would collide with (reindex or drop it:
-- docs/ops-runbook.md).
--
-- Expand-and-contract (ADR 0020): the new indexes are invisible to an older
-- sibling; the one drop removes an index that has become redundant, and the
-- rollback recreates it.
CREATE INDEX CONCURRENTLY IF NOT EXISTS entry_links_to_created_idx
    ON entry_links (to_id, created_at DESC, from_id);

DROP INDEX CONCURRENTLY IF EXISTS entry_links_to_idx;

CREATE INDEX CONCURRENTLY IF NOT EXISTS entries_agent_created_idx
    ON entries (agent, created_at DESC, id DESC);
