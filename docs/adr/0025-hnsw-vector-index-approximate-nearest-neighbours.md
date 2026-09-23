# An HNSW index on `entries.embedding`: approximate, not exact, nearest neighbours

**Amends ADR 0006** (hybrid RRF retrieval) on how the vector stream is
served. It does not change what the vector stream *is* — cosine
distance over one embedding per entry — only how Postgres finds it.

## Context

`PgStore.search_vector` (SPEC §6.2) issues

```sql
SELECT id FROM entries WHERE <filters> ORDER BY embedding <=> $n LIMIT $m
```

and until now there was **no index on `entries.embedding` anywhere in
the migration chain**. Postgres therefore answered every vector query
with a sequential scan that computed the true cosine distance for every
row in the pool.

Three facts make that a problem rather than a simplification:

1. **The cost is per-row and the row is large.** At the ADR 0015 default
   of 1024 dimensions an embedding is ~4KB. Every vector query reads and
   distance-computes the whole column.
2. **It grows without bound.** ADR 0007's scale target is ~10k entries a
   day; ROADMAP Tier 5 revises the org's ambition upward to 300+
   agents. Entries are append-only and nothing is hard-deleted
   (ADR 0001), so the scan only ever gets longer. Measured on the dev
   pool: 21k entries is already a ~60ms sequential scan against ~1.6ms
   for the same query on an HNSW index.
3. **Replicas multiply it against one node.** ADR 0020 already records
   the move to two independently released projects at 2–3 replicas each
   against **one** Postgres node (ADR 0007). Every replica's vector
   query is another concurrent full-table scan of the same table.

## Decision

**Add an HNSW index over `vector_cosine_ops` and accept approximate
nearest-neighbour search.**

1. **Migration `0004.hnsw-vector-index`** creates
   `entries_embedding_hnsw_idx` on `entries USING hnsw (embedding
   vector_cosine_ops) WITH (m = 16, ef_construction = 64)`.
   `vector_cosine_ops` because `<=>` is the operator the query already
   uses; an index on a different operator class would simply never be
   chosen. `CONCURRENTLY`, with yoyo's `-- transactional: false`
   (ADR 0020 §6), so the build takes no write lock on `entries` while
   sibling replicas serve traffic. The rollback is a
   `DROP INDEX CONCURRENTLY IF EXISTS` — an index holds no information
   the table does not, so reversing it is always legal.
2. **`m = 16`, `ef_construction = 64` are pgvector's defaults**, stated
   explicitly rather than inherited. There is no corpus to tune them
   against; an unstated default is indistinguishable from a considered
   one.
3. **Query time is `hnsw.iterative_scan = strict_order` and
   `hnsw.ef_search = 40`**, applied with `SET LOCAL` inside
   `search_vector`'s transaction (`VECTOR_SEARCH_SETTINGS` in
   `store/pgstore.py`). `ef_search = 40` is again pgvector's default,
   unchanged and explicit.
4. **The advisory lock is now acquired by polling** `pg_try_advisory_lock`
   instead of blocking inside `pg_advisory_lock` (`migrate.py`,
   `_acquire_migration_lock`). This is not an optional tidy-up — see the
   next section; without it, this migration hangs every deployment
   forever.
5. **This is a move from exact to approximate.** Recall is no longer
   100% by construction, and our own measurement says it is materially
   below 100% on the fixture we can measure. That is stated in SPEC §6.2
   and in the migration's own header, not only here.

### Why HNSW and not IVFFlat

IVFFlat **trains on the data**: it clusters the existing vectors into
lists, so an IVFFlat index built on an empty table is meaningless and
must be rebuilt once rows exist. That does not fit a migration chain.
Migration `0001` provisions a fresh, empty pool, so every chain-built
index is built on zero rows; an IVFFlat migration would have to ship as
"create it now, and separately remember to `REINDEX` it later, by hand,
once the pool has data" — a manual step with no home in the chain, no
rollback story, and a silent failure mode (nobody runs it; retrieval
quietly degrades). HNSW builds a valid, incrementally-maintained graph
from zero rows, so the migration is the whole story.

IVFFlat is also the weaker structure for this workload on pgvector's own
guidance (better build time and memory, worse query performance and
recall), and its recall is sensitive to `lists` — a parameter that would
have to be re-chosen as the pool grows, i.e. another migration each
time.

### `CREATE INDEX CONCURRENTLY` deadlocks the ADR 0020 advisory lock

ADR 0020 mandates both a `pg_advisory_lock` around the whole migration
run (decision 3) and `CREATE INDEX CONCURRENTLY` for index migrations
(decision 6). `0004` is the first migration to use decision 6, and the
two turn out to deadlock:

- the winning migrator holds the advisory lock on its own asyncpg
  connection, then applies the chain on a **separate** psycopg
  connection (`_apply_chain`, in a worker thread);
- `CREATE INDEX CONCURRENTLY` waits for every transaction older than
  itself to finish. A sibling replica parked inside
  `SELECT pg_advisory_lock(...)` is exactly that — one long-running
  statement holding a virtual xid for the whole wait;
- that sibling is waiting on the lock the winner holds, so it never
  finishes, so the index build never finishes, so the lock is never
  released.

**Postgres cannot break this.** The lock *holder* is idle, not waiting,
so the cycle closes only through application logic and never appears in
the wait graph; `deadlock_timeout` fires and finds nothing. Measured on
the dev pool with six migrators racing a cold pool (the ADR 0020
scenario, and the shape of an ADR 0018 rollout): five waiters blocked on
the advisory lock, the build blocked on `virtualxid`, hung indefinitely.
`pg_blocking_pids` shows the chain; the lock holder is absent from it.

The fix is to make each waiter's transaction *short*: poll
`pg_try_advisory_lock` with a sleep between attempts, so the index
build's wait set drains instead of stalling. The lock is unchanged —
same key, still session-scoped, so a pod killed mid-migration still
releases it by dying, which is the property ADR 0020 chose an advisory
lock for. The wait is still unbounded, as the blocking form was; the
`startupProbe` remains the timeout.

`test_concurrent_migrators_are_serialised_by_the_advisory_lock` is what
caught it — before the change it hung there rather than failing, which
is the honest signature of this bug and is now written into its
docstring.

### Why `strict_order` specifically, and not `relaxed_order`

Without iterative scan, HNSW fetches `ef_search` candidates from the
index and applies the `WHERE` clause **afterwards**. Every real query
here is filtered: at minimum `state = 'active'`, plus the ADR 0011
visibility matrix for any non-admin reader. So a narrow-visibility
reader could get back far fewer than `candidate_top_k` (20) rows from
the vector stream — RRF would then fuse a full keyword list against a
stub vector list, and the vector half of the hybrid would quietly
weaken exactly for the readers who see the least. Iterative scan keeps
resuming the search until the limit is satisfied.

`strict_order` rather than `relaxed_order` because **RRF fuses on ranks,
not scores** (`src/hivemind/retrieval/rrf.py`: `w / (k + rank)`). The
distances never reach the fused result; the *positions* are the entire
input. `relaxed_order` explicitly returns results slightly out of
distance order in exchange for speed — i.e. it perturbs precisely and
only the quantity fusion consumes. The same reasoning is already on the
record in ROADMAP §4.1, where rank-not-score fusion is what bounds
BM25's upside. Paying for ordering we then discard would be wasteful;
discarding ordering we then depend on is worse.

## Evidence: the measurement, and what it actually says

`tests/integration/test_vector_index_recall.py` (Postgres-backed,
skips when the DB is down; run with `-s` for the tables). It seeds the
**same** golden corpus and golden queries as the ROADMAP §1.1 gate
(`tests/eval/golden.py`, untouched and un-re-pinned) into a real
`PgStore`, interleaved with distractor entries, and runs the real
`SearchService` twice: once forced onto a sequential scan (exact), once
forced onto the HNSW index under the shipped session settings.

**The result did not confirm parity.**

| metric | exact scan | HNSW `strict_order` | delta |
|---|---|---|---|
| hit@5 | 1.0000 | 0.6250 | −0.3750 |
| MRR | 1.0000 | 0.5625 | −0.4375 |
| nDCG@5 | 1.0000 | 0.5789 | −0.4211 |

*(8 golden + 2000 distractors. Vector-stream set-recall@10: 0.850.)*

| metric | exact scan | HNSW `strict_order` | delta |
|---|---|---|---|
| hit@5 | 1.0000 | 0.3750 | −0.6250 |
| MRR | 1.0000 | 0.3125 | −0.6875 |
| nDCG@5 | 1.0000 | 0.3289 | −0.6711 |

*(8 golden + 20000 distractors. Vector-stream set-recall@10: 0.700.)*

The exact column is 1.000 on every run; the HNSW column moves run to
run, because HNSW's level assignment is randomised and not seeded, so
every build is a different graph.

**It is not a candidate-budget problem, so it must not be "fixed" by
raising `ef_search`.** Sweeping the knob at the 20k corpus gives
set-recall@10 of 0.688 / 0.850 / 0.888 / 0.912 / 0.938 at `ef_search` of
40 / 100 / 200 / 400 / 800 — it climbs and plateaus *below* parity. A
direct check found the index returning the 1st and 3rd-through-11th
nearest neighbours while omitting the 2nd entirely (distance 0.838
against a cloud at 0.89–0.95) at twenty times the shipped `ef_search`.
Those are vertices the graph cannot reach from its entry point, and no
candidate budget reaches a vertex with no in-edges. `ef_search` stays at
40.

**Why this is still the right decision, stated plainly.** The fixture is
close to the worst case for a graph index, and the reader should weigh
it as such:

- The embedder is a hash of the entry's text. Its vectors are
  effectively uniform on the unit sphere in 1024 dimensions, with no
  cluster structure — and cluster structure is exactly what an HNSW
  graph navigates by. Real embeddings cluster strongly; this fixture
  has nothing to navigate.
- The eight relevant entries are semantic outliers in a cloud of
  unrelated distractors, which is the topology that produces
  low-in-degree vertices in the first place.
- At these corpus sizes Postgres would not choose the index at all: the
  planner keeps the sequential scan to ~21k rows and switches at ~31k
  (pgvector's HNSW path has a high *startup* cost that a small table's
  seq-scan total never exceeds). The harness forces the plan; a
  production pool of this size would still be exact.

So the measurement establishes the *shape* of the risk — recall loss is
real, it grows with the corpus, and it is structural rather than
tunable — not its magnitude on this system's real embeddings. It is
recorded here rather than smoothed over precisely because it did not
come out the way the decision assumed.

**Re-measure trigger.** The first real corpus with real embeddings at
≥30k entries — the size at which the planner starts choosing the index
unprompted. Re-run this module against it. If recall is materially below
1.0 there too, the response is a **new ADR**, not a parameter tweak: the
options are raising `m` (a rebuild — it changes graph connectivity,
which is the thing at fault, unlike `ef_search`), re-ranking the HNSW
candidates exactly inside a larger `LIMIT`, or reverting the index
(migration `0004`'s rollback exists and is legal).

## Consequences

- **Vector search is approximate.** SPEC §6.2 now says so. Two entries
  with near-identical embeddings may swap places in the vector stream,
  and a true top-k entry may be missed. The keyword stream is
  unaffected, and RRF means a vector miss is survivable when the keyword
  stream also finds the entry — which is part of why the hybrid design
  absorbs this at all (ADR 0006).
- **Writes get slower.** Every insert maintains the graph. Writes are a
  few per second at peak (SPEC §8.2), so this is not a concern at v1
  scale, but it is not free either.
- **The index is large.** Roughly `m` links per vertex plus the vectors
  themselves — ~16MB per 20k entries at 1024 dimensions on the dev pool.
  It is the first index here whose size is comparable to the table's.
- **Reads get much faster where it matters.** ~60ms → ~1.6ms on a 21k-row
  pool for one vector query, and the gap widens with the pool. That is
  the whole point, and it is what makes multiple replicas against one
  Postgres node viable.
- **The session settings live with the query, not on the pool.**
  `SET LOCAL` inside `search_vector`'s transaction, not the `init`
  callback in `store/pool.py`. The org runs a **transaction-mode
  PgBouncer** (see `make_pool`'s docstring), where a pooled client
  connection is mapped to a server backend *per transaction* and
  `server_reset_query` (`DISCARD ALL`) wipes session state between them:
  a session-level `SET` at connection-open time would land on an
  arbitrary backend and be discarded before the search ever ran —
  silently, leaving the defaults in force and the filtered stream
  starving. `SET LOCAL` travels with its transaction to whichever
  backend executes the query, and unsets at commit, so it never leaks
  into the other queries sharing that pooled connection. `search_vector`
  gains an explicit transaction it did not previously need; that is the
  cost.
- **Every migration now polls for the advisory lock.** The change is in
  shared infrastructure, not in `0004`, so it applies to the whole chain
  from here on. Worst case a migrator waits one extra poll interval
  (250ms) after the lock frees — against a deadlock that never clears.
- **A cancelled concurrent build leaves an INVALID index.** That is
  inherent to `CREATE INDEX CONCURRENTLY`, which is why the migration
  uses `IF NOT EXISTS`: a plain re-run would collide with the leftover.
  The remedy is `REINDEX INDEX CONCURRENTLY` or a drop and re-run
  (`docs/ops-runbook.md`).
- **The ROADMAP §1.1 gate is untouched and was not re-pinned.** It runs
  against `MemoryStore`, which has no index and no `<=>` operator, so it
  is structurally blind to this change. That blindness is the reason
  this ADR needed its own Postgres-backed measurement.

## Alternatives considered

- **IVFFlat** — rejected; see above (must be built after rows exist,
  which a migration chain cannot express, plus weaker recall/latency and
  a `lists` parameter that ages with the pool).
- **No index; scale the single node instead** — rejected. It is the
  status quo, it degrades monotonically with a table that only grows,
  and the replica topology ADR 0020 already assumes multiplies the
  concurrent scans against one node.
- **A partial index on `WHERE state = 'active'`** — deferred, not
  rejected. Superseded and withdrawn entries are a small fraction today,
  so the saving is small, and `include_inactive` queries would lose the
  index entirely. Worth revisiting if the inactive fraction ever becomes
  large.
- **Setting the GUCs in `make_pool`'s `init` callback** — rejected;
  transaction-mode PgBouncer discards them (see Consequences). It would
  also apply them to `PgAuthenticator`'s pool, which runs no vector
  query at all.
- **`relaxed_order`, or no iterative scan** — rejected; both attack the
  ranks RRF fuses on. See above.
- **Tuning `ef_search` up until the measurement matched** — rejected,
  and worth naming as a rejected option rather than an unconsidered one.
  The sweep shows it does not reach parity, so it would have bought a
  green number and a slower query without fixing the defect.
