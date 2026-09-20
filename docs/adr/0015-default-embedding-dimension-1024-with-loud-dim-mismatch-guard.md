# Default embedding dimension 1024, with a loud dim-mismatch guard

The embedding dimension is a **deploy-time decision** (ADR 0005): it is
baked into the pool's `vector(:dim)` column at migration time, and it is
never an online setting. But two things about how that decision is
handled were wrong:

* The *code default* (what `HIVEMIND_EMBEDDING_DIM` falls back to) was
  **1536** — an implicit assumption that deployments target
  OpenAI's native dimension. The org's production target is 1024, and
  the dev pool runs at 512 (fast local vLLM embedding, `make vllm`).
* A pool provisioned at a *different* dim than the configured one
  failed in a confusing way: `migrate` silently no-ops (the schema is
  idempotent), and the first vector write then dies with
  `DataError: expected 512 dimensions, not 1536`. The ROADMAP 1.2
  dogfood (friction #8) found exactly this footgun — anyone running
  the suite or `hivemind-migrate` outside `make` (no exported env)
  against the 512 dev pool hit it.

## Decision

1. **The default dimension is 1024** (`Settings.embedding_dim`; this
   supersedes ADR 0005's "default 1536, matching
   `text-embedding-3-small`"). We never assume 1536 — that is one
   provider's native dim, not a contract. 1024 is the middle-ground
   dimension both OpenAI-compatible endpoints (the `dimensions`
   parameter) and self-hosted Matryoshka servers (vLLM) serve. The
   dev Makefile keeps pinning **512** for fast local vLLM embedding
   (a dev override, not the default).
2. **A dim mismatch is a loud error at migrate time, never a silent
   no-op.** `migrate` reads the existing `entries.embedding` column's
   dimension from the catalog (`current_embedding_dim` — pgvector
   stores the dim as the column's typmod) *before* applying any
   statement, and raises with an actionable message when it differs
   from the configured dim: **both dims named + both remediations**
   (reset the pool, or point `HIVEMIND_EMBEDDING_DIM` at the pool's
   dim). The `hivemind-migrate` CLI prints that message and exits
   non-zero instead of a traceback.

## Consequences

* Bare `uv run pytest` (integration) / `uv run hivemind-migrate`
  against a pool at a different dim now fails **at migrate time** with
  an actionable error — instead of a silent no-op followed by a
  confusing `DataError` on the first write (the dogfood friction #8
  footgun is closed).
* `make test` / `make migrate` (the Makefile exports 512) stay green
  against the 512 dev pool; a 1024 production deployment sets
  `HIVEMIND_EMBEDDING_DIM` (or simply keeps the new default) and
  provisions a fresh pool at 1024 (the §6 switch procedure is
  unchanged).
* A never-migrated pool has no `entries` column, so the guard is a
  no-op there — `migrate` provisions the schema at the configured dim
  exactly as before. The guard changes nothing for a matching dim
  (the idempotent re-apply is untouched).
* A dim mismatch can no longer be *missed*: the one place a dim is
  consumed (the vector column) is checked at the one place it is
  provisioned (migrate).