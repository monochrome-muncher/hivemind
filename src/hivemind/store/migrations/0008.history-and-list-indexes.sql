-- transactional: false
--
-- Three missing indexes that made read paths O(pool) (review findings
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
-- 3. `entries_author_created_idx` — the same order, per author. Index (2) alone
--    makes a selective filter WORSE when the matching rows are old: the
--    planner trusts (2) to find LIMIT rows early and walks it (82 ms for an
--    author whose 1.5k entries are the oldest of 200k, 227 ms vs 2.9 ms at
--    1M in the verifier's run). With this index the same query is an index
--    range scan that stops after LIMIT rows (0.075 ms). Cost: ~10 MB per
--    200k rows and ~18% on a 20k-row bulk insert (397 -> 469 ms), noise next
--    to the per-write embedder call. `entries_author_idx` is kept
--    (expand-only, ADR 0020); it is now redundant for ordered lists but
--    still serves unordered author filters and counts.
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

CREATE INDEX CONCURRENTLY IF NOT EXISTS entries_author_created_idx
    ON entries (author, created_at DESC, id DESC);
