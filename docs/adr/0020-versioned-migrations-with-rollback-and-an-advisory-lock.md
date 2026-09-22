# Versioned migrations with rollback, guarded by a Postgres advisory lock

**Supersedes ADR 0013** (forward-migration path: idempotent re-apply +
`schema_migrations` tracking).

ADR 0013 made the DDL forward path an idempotent re-apply of a single
`schema.sql` (every statement guarded with `IF NOT EXISTS` /
`OR REPLACE`), plus a `schema_migrations` version marker and a
*reserved* `migrations/` directory for the occasional backfill. Three
facts have since changed the ground it stood on:

1. **The declarative re-apply cannot express a change to an existing
   object.** `CREATE TABLE IF NOT EXISTS` is a no-op on a live pool, so
   editing a `CHECK` constraint inside it changes a *fresh* deployment
   and silently leaves an *upgraded* one on the old constraint. The
   schema has seven such constraints, and `api_keys.kind` has already
   drifted (it still permits the `user` key kind that ADR 0012
   retired). The same blindness covers type changes, renames, and drops.
2. **The `migrations/` directory 0013 reserved was never built.** Its
   Consequences read as though the mechanism shipped; no such directory
   exists and `migrate` never applied ordered scripts. The declarative
   half was implemented and the incremental half was not, so backfills
   have had no home.
3. **The deployment is no longer effectively single-node.** ADR 0013
   scoped its "brief DDL locks are tolerable" argument to the
   single-node deployment of ADR 0007. The target is now GitLab
   AutoDevOps, which deploys one runner type per project — so
   `hivemind-api` and `hivemind-mcp-http` become two independently
   released projects against one database, each scaling to 2–3
   replicas. Up to six pods now race the entrypoint-owned migration
   (ADR 0018), and a migrating pod holds its locks while a sibling
   replica serves live traffic.

ADR 0007 (a single Postgres *node*) is unchanged by this: what changed
is the number of *application* replicas in front of it.

## Decision

1. **Ordered, versioned migrations replace the idempotent re-apply.**
   The schema is a chain of migrations under
   `src/hivemind/store/migrations/`, run by
   [yoyo-migrations](https://ollycope.com/software/yoyo/). Raw SQL
   files, no ORM and no model layer — the same hand-written SQL posture
   `PgStore` already has. `0001.initial-schema` is the current
   `schema.sql`, written as a Python migration so the pgvector
   dimension stays parameterised (ADR 0005); every later migration is
   plain SQL.
2. **Every migration ships a rollback.** Migrations carry a
   `.rollback.sql` companion, so an incremental change can be reversed
   without restoring the pool. **A rollback that would destroy data is
   not written** — reversing a populated column drop or a backfill is a
   restore from backup (`docs/ops-runbook.md`), not a migration. Under
   ADR 0001 (entries are immutable, nothing is hard-deleted) that is the
   only honest boundary: yoyo will run a destructive rollback without
   complaint, so the discipline has to be recorded rather than assumed.
3. **Mutual exclusion is a Postgres advisory lock, NOT yoyo's lock
   table.** `migrate` takes `pg_advisory_lock` on a dedicated
   connection and calls `apply_migrations` without `backend.lock()`
   (yoyo does not lock internally; locking is the caller's job).
   Rationale below — this is the least obvious part of this ADR.
4. **`schema_migrations` is retired.** yoyo's `_yoyo_migration` table
   becomes the single record of what is applied; `current_schema_version()`
   reads the latest applied migration id from it. The ops/health surface
   (ROADMAP §3.3) keeps its field — only the implementation moves. Two
   markers for one fact is exactly the drift this ADR exists to remove.
5. **`schema.sql` becomes a generated, non-authoritative reference.**
   The chain is the source of truth; a CI job regenerates
   `schema.sql` from a migrated pool so a reviewer can still read the
   whole storage model in one file and diff it across changes. It is
   never applied and never hand-edited.
6. **New index migrations use `CREATE INDEX CONCURRENTLY`** with
   yoyo's `-- transactional: false` directive (concurrent index builds
   cannot run inside a transaction). This closes the multi-node concern
   ADR 0013 deferred. `0001` is exempt: it runs against an empty
   database, where `CONCURRENTLY` is strictly slower and gains nothing.
7. **Migrations stay entrypoint-owned (ADR 0018 is unchanged).** The
   advisory lock is what makes that safe under replicas. AutoDevOps
   deploys through its bundled `auto-deploy-app` chart, which offers no
   clean hook for a migration Job or an initContainer — so an
   entrypoint pre-step remains the one mechanism that works identically
   under AutoDevOps, the kustomize manifests, docker-compose, and a bare
   `docker run`.
8. **Schema changes follow expand-and-contract.** See Consequences —
   this is a requirement of the two-project topology, not a style
   preference.

### Why an advisory lock and not `backend.lock()`

yoyo's lock is a single-row mutex (`yoyo_lock`, primary key on
`locked`), acquired by polling an `INSERT` with a **10-second default
timeout** and released by a `DELETE` in a `finally` block.

Release therefore only happens on a *graceful* exit. There is no TTL and
no stale-lock detection — `ctime` is written and never read. A pod that
is OOMKilled, node-evicted, or killed by a liveness probe mid-migration
leaves the row behind, and **every subsequent pod across both projects
then fails after 10 seconds and enters CrashLoopBackOff, indefinitely,
until a human runs `yoyo break-lock`.** On an air-gapped cluster that
means exec'ing into a crash-looping pod to clear a cluster-wide outage.
The diagnostic is no help either: `pid` is `os.getpid()`, which is
almost always `1` in a container, so the error names "process 1" without
identifying which pod — or even which project — wedged it.

`pg_advisory_lock` is session-scoped: Postgres releases it automatically
when the connection drops. A dead pod releases its lock by dying, so a
stale lock is structurally impossible. It needs no table, no new
dependency, and no runbook entry.

## Consequences

- **Expand-and-contract is mandatory.** Two independently released
  projects share one database, so a migration from one runs against the
  *other's* still-deployed code. Within a release, changes are additive
  only (new columns nullable or defaulted, new tables, new indexes). A
  removal takes two releases: release N stops reading and writing the
  column, release N+1 drops it — and both projects must be on ≥N before
  N+1 ships. In practice both projects are deployed together whenever a
  migration lands; releases that only change config need not be
  synchronised. The compatibility window below is the backstop for when
  that practice slips, not the expected state.
- **CI gains three migration checks**: applied migration files are
  immutable (checksum), the generated `schema.sql` is current, and the
  **previous three releases' test suites pass against a pool migrated
  to HEAD**. The last one enforces expand-and-contract by testing the
  property directly. A grep for `DROP COLUMN` / `ALTER` / `RENAME` was
  rejected: in correct expand-and-contract those verbs are routine (they
  *are* the contract phase), so such a check fires on correct work and
  trains reviewers to dismiss it.
- **Migrations ship inside the image.** `uv_build` includes everything
  under the module root, so `src/hivemind/store/migrations/` rides the
  wheel with no packaging configuration (the same way `schema.sql`
  already does). A derived image — e.g. the air-gapped high-side build
  that adds custom CA certificates — inherits them. That image must
  **add layers only and never set its own `ENTRYPOINT`**: overriding it
  skips the migration pre-step silently, the same trap ADR 0018 already
  documents for the GitLab key-bootstrap Job. The operational rule lives
  in DEPLOY.md §5.
- **A new migration requires a full image cycle** (build → mirror →
  high-side rebuild). There is no hot-fixing a migration in place, which
  is precisely why rollback (decision 2) has to work from the image
  already deployed.
- **Two new runtime dependencies**: `yoyo-migrations` and `psycopg`
  (synchronous, used only by the startup migration path; `asyncpg`
  remains the only driver on the request path). Both must exist in the
  build-side package index.
- **A `startupProbe` is required on both Deployments.** The migration
  runs before the server listens, and liveness probing starts
  `initialDelaySeconds` after *container* start — so a migration longer
  than roughly 35 seconds is killed by the orchestrator mid-flight. With
  the advisory lock that is survivable (the lock releases, the next pod
  retries); without a `startupProbe` it is a restart loop on any slow
  index build.
- **The ADR 0015 dim-mismatch guard runs before the lock is taken**, so
  a misprovisioned pool still fails loudly before any DDL — unchanged
  posture, earlier in the sequence.
- **ADR 0013 is superseded, not edited.** Its `schema_migrations` table
  and its `migrations/`-directory plan are both retired by this ADR; the
  document stays as written for the audit trail.

## Alternatives considered

- *Alembic*: rejected. Its principal value is `--autogenerate`, which
  diffs SQLAlchemy models against the live database — and there are no
  ORM models here. Without them it is an ordered-script runner with
  extra ceremony; with them it means adopting SQLAlchemy purely for
  migrations and maintaining a second description of a schema that
  `PgStore` already expresses in hand-written SQL.
- *Keep the declarative re-apply and harden it* (name every constraint,
  drop-and-recreate it on each run, add a fresh-vs-upgraded schema
  equivalence test): viable, and it was the right answer while the
  deployment was single-replica with no rollback requirement. Rejected
  because it still cannot express a backfill, still offers no reverse
  direction, and the multi-replica race needs a lock regardless — at
  which point the remaining distance to a real migration chain is small.
- *A Kubernetes Job or initContainer instead of the entrypoint*:
  rejected for the same reason ADR 0018 rejected it, now reinforced by
  AutoDevOps, whose chart provides no hook for either.
- *One project owning migrations exclusively, the other skipping the
  pre-step*: rejected — it reintroduces "a pod starts against an
  un-migrated database," which is the failure ADR 0018 deliberately
  eliminated, and it makes deploy order load-bearing.
- *Pinning a `MIN_COMPATIBLE_SCHEMA` that refuses to start when the
  database is ahead of the code*: rejected — it inverts the safety
  property. Refusing to start on a newer schema would make it impossible
  to deploy one project before the other, which is the exact
  independence the two-project topology exists to provide.
