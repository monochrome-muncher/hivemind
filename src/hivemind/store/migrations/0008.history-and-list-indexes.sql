-- transactional: false
--
-- Two missing indexes that made two read paths O(pool) (review findings
-- PERF-1 / STORE-2 / STORE-3).
--
-- 1. `entries_superseded_by_idx` — the reverse supersession link. The
--    `?history` / `hive_get(history=true)` walk asks "which entries were
--    superseded BY these ids?" (`WHERE superseded_by = ANY($1)`,
--    `Store.list_predecessors`). With no index on `superseded_by` the only
--    way to answer was to page the whole table (one query per 200 rows,
--    108 s on 200k rows). Partial: only superseded rows carry a link, so
--    the index holds the chains and nothing else.
--
-- 2. `entries_created_at_id_idx` — the default list order. `list_entries`
--    is `ORDER BY created_at DESC, id DESC LIMIT/OFFSET`; with no index
--    Postgres sorted every matching row for each page (90 ms at 200k rows,
--    0.05 ms with the index, which stops after LIMIT rows). The key matches
--    the ORDER BY exactly (same direction, same tie-break column), and is
--    deliberately NOT partial on `state = 'active'` so the
--    `include_inactive` listing uses it too.
--
-- `CONCURRENTLY` (hence `-- transactional: false`, ADR 0020 §6 / ADR
-- 0025) so the builds take no write lock on `entries` while sibling
-- replicas serve traffic; `IF NOT EXISTS` because a cancelled concurrent
-- build leaves an INVALID index behind that a plain re-run would collide
-- with (reindex or drop it — docs/ops-runbook.md).
--
-- Ordering: this file declares no `-- depends:` (nor does any other in
-- the chain). yoyo orders by migration id, so 0008 applies after 0006
-- today and after 0007 once it lands; the indexes touch nothing 0007
-- touches, so either application order would also be safe.
--
-- Additive-only (ADR 0020 expand-and-contract): an index is invisible to
-- a not-yet-upgraded sibling project.
CREATE INDEX CONCURRENTLY IF NOT EXISTS entries_superseded_by_idx
    ON entries (superseded_by)
    WHERE superseded_by IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS entries_created_at_id_idx
    ON entries (created_at DESC, id DESC);
