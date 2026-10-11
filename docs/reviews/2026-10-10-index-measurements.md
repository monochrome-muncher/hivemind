# Index measurements, 2026-10-10 (main ec13225, v2.4.0)

> A review kept for reference. Findings, line numbers and PR states are as of the commit it names, so the code may have moved on. [SPEC.md](../../SPEC.md) defines behaviour and the [ADRs](../adr/README.md) record decisions.

Local Postgres 16.15 + pgvector, scratch db `hm_bench`: 200k entries (300 agent labels, one "old" agent and author owning the oldest 1,500 rows), 50k see-also links (one target linked 10,000 times, 50 targets 1,000 times each). Times are warm-cache EXPLAIN ANALYZE execution times.

## Added (migration 0013)

| Query (src/hivemind/store/pgstore.py) | Before | After | Index |
|---|---|---|---|
| Incoming links, `SELECT_LINKS_IN` (:126), entry with 10k links | 2.9-5.2 ms, bitmap scan + top-N sort over 10k rows | 0.06 ms, index-only scan, 20 rows | `entry_links (to_id, created_at DESC, from_id)` |
| Same, entry with 1k links | 0.7 ms | 0.03 ms | same |
| Batch incoming, `SELECT_LINKS_IN_MANY` (:137), 2 ids | 3.6-5.6 ms | 0.1 ms | same |
| `GET /v1/entries?agent=` (list_entries :670), agent with the 1.5k oldest rows | 46 ms (198,502 rows filtered) | 0.06 ms | `entries (agent, created_at DESC, id DESC)` |
| Same, with an L2 visibility clause | 35 ms | 0.06 ms | same |
| Same, common agent (~660 rows) | 1.0 ms | 0.1 ms | same |

The old `entry_links_to_idx (to_id)` is dropped: the new index leads with `to_id` and serves every `to_id` lookup. Sizes: 2.9 MB per 50k links; 11 MB per 200k entries.

## Checked, no index added

- Outgoing links (`from_id`, primary key): at most 5 per writing entry.
- Pins (primary key, capped per fleet), feedbacks (primary key per entry), audit log (admin events only, small), agents `lower(name)` lookup (registration only, small table).
- Keyword search with an agent filter keeps using the GIN index after the new index.
- Rare **old tag** filter on the list (`tags @> ...`): 60 ms at 200k, because the planner walks the created_at index assuming matches are spread over time. The estimate is right (411 vs 300 rows); a btree cannot combine array containment with the order, so no index fixes it. Left alone.
- `USAGE_COUNTS` (pgstore.py:101), run on every `/metrics` scrape: a full seq scan + hash aggregate, 116 ms at 200k entries. An index will not help; a short-lived cache in services/metrics.py would. Not done here (not an index).
