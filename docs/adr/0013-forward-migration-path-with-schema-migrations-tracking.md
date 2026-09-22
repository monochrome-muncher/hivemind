# Forward-migration path: idempotent re-apply + schema_migrations tracking

> **Status: superseded by [ADR 0020](0020-versioned-migrations-with-rollback-and-an-advisory-lock.md).**
> The idempotent `schema.sql` re-apply, the `schema_migrations` table,
> and the reserved `migrations/` directory below are all retired. The
> text is kept verbatim for the audit trail — do not follow it.

`make migrate` re-applies the single `schema.sql` (every DDL statement
guarded with `IF NOT EXISTS` / `OR REPLACE`). That is already an
idempotent **catch-up**: running it on an existing deployment creates any
missing object without touching existing data. But it has two gaps for a
**forward-migration** story (a schema change that lands *after* the pool
has data):

1. **No tracking.** There is no record of *which* schema a deployment is
   on, so an operator cannot tell whether a live pool is up to date.
2. **No explicit data-migration path.** A change that needs a
   *backfill* (not just DDL) has no home — `schema.sql` is pure DDL.

**Decision: the forward-migration path is (a) the idempotent `schema.sql`
re-apply as the DDL forward path, (b) a `schema_migrations` version
marker that records the applied schema generation, and (c) a reserved,
ordered `migrations/` directory for the occasional data migration
(backfill) that idempotent DDL cannot express.** For single-node
Postgres (ADR 0007) brief DDL locks during `CREATE INDEX` are
tolerable; `CREATE INDEX CONCURRENTLY` is noted as a future
multi-node production concern, not a v1 requirement.

This ADR complements ADR 0007 (single Postgres node, docker-compose
self-hosted): the migration story assumes the single-node deployment, so
"zero-downtime" means "no pool rewrite and no data loss," not "no
brief lock."

## Decision

* **DDL forward path = idempotent re-apply.** `schema.sql` remains the
  single source of truth (the full, guarded schema). `make migrate`
  re-applies it on every start; on a live pool this creates any missing
  object (idempotent, `IF NOT EXISTS`) and is a no-op on already-present
  objects. A new column / index lands by adding the guarded DDL to
  `schema.sql` — no rewrite, no data loss.
* **`schema_migrations` tracking (new).** A `schema_migrations` table
  records the applied schema generation (a single upsert of the current
  `SCHEMA_VERSION` after a successful migrate). Operators (and the
  health / metrics surface) can now read `current_schema_version` to tell
  whether a deployment is up to date. The schema version is a small
  integer bumped with each schema generation.
* **Ordered `migrations/` for data migrations (reserved).** A
  `migrations/` directory is reserved for the *occasional* data
  migration (a backfill that idempotent DDL cannot express). Such a
  migration is an ordered, idempotent script (re-running it is a no-op)
  that `make migrate` applies in numeric order *after* the schema
  catch-up, recorded in `schema_migrations`. Tier 2 (ADRs 0011-0012)
  was pure DDL — no backfill — so it rides the `schema.sql` path and
  ships no data-migration script yet; the mechanism is in place for the
  first real backfill.
* **Online-safe DDL.** All DDL stays guarded (`ADD COLUMN IF NOT EXISTS`
  is a fast metadata-only change; index creation is guarded). For the
  single-node deployment (ADR 0007) a brief lock is acceptable; `CREATE
  INDEX CONCURRENTLY` (no table lock) is the future multi-node
  production upgrade path.

## Consequences

* `make migrate` stays safe to run on every start (idempotent) and now
  *reports* the applied schema generation (`schema_migrations`), so a
  live pool can be checked for drift.
* A schema change = (1) add the guarded DDL to `schema.sql` + bump
  `SCHEMA_VERSION`, or (2) add an ordered, idempotent script to
  `migrations/` for a data backfill. Either way, no pool rewrite.
* The `schema_migrations` counter (applied version) is one of the §3.3
  usage/ops counters (ROADMAP §3.3).