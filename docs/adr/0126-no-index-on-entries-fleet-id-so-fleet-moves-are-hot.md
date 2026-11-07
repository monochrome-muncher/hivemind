# No index on `entries.fleet_id`, so fleet moves are HOT updates

Amends [ADR 0107](0107-fleet-merge.md) and [ADR 0112](0112-move-agents-with-their-memories.md) (the speed of re-filing entries). Builds on [ADR 0020](0020-versioned-migrations-with-rollback-and-an-advisory-lock.md) (expand-and-contract) and [ADR 0025](0025-hnsw-vector-index-approximate-nearest-neighbours.md) (the vector index).

Status: Accepted. Migration 0028. Measured 2026-11-07.

## Context

A fleet merge, its undo and a fleet move each run one `UPDATE entries SET fleet_id = ...` over every moved memory (`MOVE_ENTRIES`, `RESTORE_ENTRIES` and the move statement in `store/pgstore.py`). `entries.fleet_id` had its own btree index (`entries_fleet_idx`, migration 0001). Postgres cannot make an update heap-only (HOT) while an indexed column changes, so each moved row got a new version and a new entry in all 14 indexes on `entries`, the HNSW vector index among them. That index insert was about 97% of the time: 11.2 s to move 6,667 of 13,334 entries (random 1024-dim vectors, Postgres 16, pgvector 0.8.7), and about 12 ms per entry on the 200k-entry pool of ADR 0107's correction. The merge holds both fleet rows locked meanwhile, so that time is time fleet writes are refused.

Dropping the index is not enough on its own. A HOT update needs room for the new row version in the same page, and the table was packed full:

| Setup (6,667 entries moved) | Time | HOT updates |
|---|---|---|
| Index kept, fillfactor 100 (before) | 11.2 s | 0 |
| Index dropped, fillfactor 100 | 11.3 s | 45 |
| Index dropped, fillfactor 70 | 1.09 s | 6,145 |
| Index dropped, fillfactor 50 | 0.034 s | 6,667 |

At fillfactor 50 three merge-and-undo rounds in a row stayed fully HOT, because Postgres prunes the dead versions inside the page.

## Decision

1. **No index may cover `entries.fleet_id`** (as a key column, in an expression or in a predicate). Migration 0028 drops `entries_fleet_idx`.
2. **`entries` keeps `fillfactor = 50`**, set by the same migration, so a page has room for the moved version of each row.
3. Both are guarded by `tests/integration/test_fleet_move_hot_pg.py`: no index on `entries` mentions `fleet_id`, the table option is set, and the store's own merge, undo and move statements are HOT updates (counted in `pg_stat_xact_user_tables`). A change that needs a fleet index has to supersede this ADR and say what it does to merge speed.
4. Existing rows need no rewrite (see Consequences). `VACUUM FULL entries` is an optional runbook step, not part of the migration.
5. The merge preview looks up a small fleet exactly, not through the HNSW index (`EXACT_DUPLICATE_LOOKUP_MAX`, `domain/fleet_merge.py`).

## Consequences

- **Speed.** A merge, undo or move of a fleet whose memories sit on pages with room is 100 to 300 times faster (11.2 s to 34 ms in the bench, at 13k entries) and touches no index. The lock on the two fleets is held for the same fraction of that time.
- **A memory written before this release is slow on its first move only.** `fillfactor` applies to pages written from now on, and a row in a full page cannot be updated in place. Its first move therefore still re-inserts the vector (about 12 ms per memory on the 200k-entry pool, the figure in `admin/static/merge.js`, so that figure is now a ceiling for memories never moved since the upgrade). The new version lands on a page that respects the fillfactor, so every later move, and the undo of the first one, is HOT: the move back measured 10,000 of 10,000 HOT. No rewrite is needed.
- **`VACUUM FULL entries` is optional, and an outage.** It rebuilds every index under `ACCESS EXCLUSIVE`, the HNSW one most of all: 80 s at 200k rows with only 32-dimension vectors, many minutes at 1024 dimensions, and the 30 s command timeout makes the API effectively down meanwhile. A `VACUUM FULL` waiting for its lock also blocks every query queued behind it. It builds a new HNSW graph (ADR 0025). It is only worth it when that first merge would cost more than the outage. The runbook, section 7, has the settings (`lock_timeout`, `maintenance_work_mem`, disk).
- **Disk.** The main heap roughly doubles (10 MB to 21 MB at 13k entries). Vectors live in TOAST and are not affected, so the table with TOAST and indexes grows about 6% (188 MB to 199 MB).
- **Every read that filters on `fleet_id` alone scans the table, and the cost grows with the whole pool, not with the fleet.** Measured with EXPLAIN ANALYZE at 200k entries:
  - The agent portal's fleet view and CSV export (`GET /v1/portal/entries?fleet_id=`) and portal search with `fleet_id` (both through `_filter_conditions`): a 20-entry fleet's list went from 0.08 ms to 35 ms (parallel seq scan); a 1.9k-entry fleet at offset 500 from 7 ms to 34 ms; the count behind the list from 0.04 ms to 32 ms.
  - The admin memory list with a fleet filter and the merge preview counts: a seq scan as well (10 ms against 7 ms at 13k entries).
  - `MOVE_ENTRIES` itself is now a seq scan: 268 ms at 200k entries (the 34 ms above is at 13k).
  - The merge preview's duplicate scan, where a small fleet in a big pool went through a filtered HNSW scan: two 20-entry fleets 0.4 ms to 1,156 ms, and it could reach the 20 s timeout. The store now counts the fleet looked into and scans it exactly when it has at most `EXACT_DUPLICATE_LOOKUP_MAX` (5,000) active entries with a vector; the results are the same.
  - Agent searches never used the index (the visibility clause is an OR). The fleet move's planning query also filters by author, which has an index.
  - ROADMAP section 4.7 holds the re-measure trigger: the pool passes about 200k entries. The options then are a fleet-scoped side table or accepting slower merges.
- **Expand-and-contract holds.** An older sibling pod never needs the index; its queries get a seq scan where they used the index, and the storage parameter is invisible to it. The rollback recreates the index with `CREATE INDEX CONCURRENTLY` and resets the fillfactor; pages already written at 50 stay half empty until a `VACUUM FULL`. A cancelled rollback can leave an invalid `entries_fleet_idx`; drop it, since it blocks HOT updates (runbook, section 7).
- ADR 0107's and ADR 0112's speed notes describe the time before this ADR; each carries a pointer here.

## Considered options

- **Move in batches so writes keep flowing.** The write pause shrinks to the last batch, but the total time does not, a half-done merge is visible, and undo would need progress tracking. Rejected once the move itself is fast.
- **Alias the old fleet to the target at read time.** Merge time stops growing with the fleet, but it changes the visibility clause (security code) and every place that compares `entries.fleet_id`, and a fleet move would still rewrite rows. Rejected, as in ADR 0107.
- **Lock only the source fleet.** Halves who is paused and changes no speed. Rejected as too small to matter.
- **Fillfactor 70.** About 10 times faster instead of 300 times, for half the extra heap. Rejected: the heap is the small part of the table and the measurement at 50 stayed fully HOT.
