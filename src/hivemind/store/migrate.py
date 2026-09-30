"""Migration runner: apply the ordered migration chain (ADR 0020).

The schema is a chain of ordered migrations under ``migrations/``,
applied by `yoyo <https://ollycope.com/software/yoyo/>`_. ``migrate``
applies every migration not yet recorded in yoyo's ``_yoyo_migration``
table, so it is safe to run on every process start — an up-to-date pool
is a no-op — and the image entrypoint does exactly that (ADR 0018).

**Concurrency (ADR 0020).** ``migrate`` holds a Postgres *advisory*
lock for the whole run, so many replicas may start at once and exactly
one migrates. The lock is session-scoped: Postgres releases it when the
connection drops, so a pod killed mid-migration (OOM, eviction, a
liveness probe) releases it by dying. yoyo's own ``backend.lock()`` is
deliberately NOT used — it is a table row deleted in a ``finally``, with
no TTL and no stale detection, so a killed pod wedges every later pod
until a human runs ``yoyo break-lock``.

The lock is acquired by **polling** ``pg_try_advisory_lock``, not by
blocking inside ``pg_advisory_lock`` — a blocking waiter deadlocks
against ``CREATE INDEX CONCURRENTLY`` in a way Postgres cannot detect.
See ``_acquire_migration_lock``; this matters from migration ``0004``
(ADR 0025) onward.

**Rollback.** ``rollback`` reverses the most recently applied
migration(s) via their ``.rollback.sql`` companions. Rollbacks are
written for *structural* changes only: reversing a populated column drop
or a backfill is a restore from backup (``docs/ops-runbook.md``), never
a migration. ``0001.initial-schema`` has no rollback at all — reversing
it would drop every entry in the pool.

**The dim guard (ADR 0015)** runs *after* the lock is taken and before
any DDL: a pool provisioned at a different embedding dimension than the
configured one is a deployment error (ADR 0005), and failing before any
DDL beats the confusing ``DataError`` the first vector write would
otherwise produce. (Under the lock so that two replicas racing a cold
pool at different dims cannot both pass it.) The same pre-flight checks
pgvector >= 0.8 and ``dim <= 2000`` (the HNSW limit), and after the chain
an INVALID ``entries_embedding_hnsw_idx`` (a crashed ``CONCURRENTLY``
build, which ``IF NOT EXISTS`` would otherwise skip silently) fails the
run with the runbook remedy.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from pathlib import Path

import asyncpg

from hivemind.config import load_settings
from hivemind.store import migration_context

logger = logging.getLogger(__name__)

# pgvector's `hnsw.iterative_scan` (used by every vector search) exists
# from 0.8.0; HNSW cannot index more than 2000 dimensions.
_MIN_PGVECTOR = (0, 8, 0)
_MAX_HNSW_DIM = 2000
_HNSW_INDEX = "entries_embedding_hnsw_idx"

_MIGRATIONS_DIR = Path(__file__).with_name("migrations")

# The advisory-lock key (ADR 0020). Arbitrary but FIXED: every process
# that migrates this pool must take the same key. The value is ASCII
# "HIVEMIND" read as a big-endian int64 (fits in a signed bigint), which
# makes it self-identifying in `pg_locks`.
MIGRATION_LOCK_KEY = 0x484956454D494E44

# How long a migrator sleeps between attempts at the lock. Small enough
# that a fast migration is not held up noticeably, large enough that six
# replicas polling do not busy-spin against the pool.
_LOCK_POLL_SECONDS = 0.25


def _yoyo_dsn(dsn: str) -> str:
    """Rewrite an asyncpg DSN to the psycopg3 form yoyo expects.

    The service speaks ``postgresql://`` (asyncpg) everywhere; yoyo
    selects its driver from the scheme, and ``postgresql+psycopg://``
    is the psycopg3 backend. Anything already carrying an explicit
    ``+driver`` is left alone.
    """
    for prefix in ("postgresql://", "postgres://"):
        if dsn.startswith(prefix):
            return "postgresql+psycopg://" + dsn[len(prefix) :]
    return dsn


def _apply_chain(dsn: str, dim: int) -> None:
    """Apply every outstanding migration (synchronous; yoyo is sync).

    Called inside ``asyncio.to_thread`` while the caller holds the
    advisory lock. ``backend.lock()`` is intentionally not used — see
    the module docstring.
    """
    from yoyo import get_backend, read_migrations

    migration_context.embedding_dim = dim
    backend = get_backend(_yoyo_dsn(dsn))
    try:
        migrations = read_migrations(str(_MIGRATIONS_DIR))
        backend.apply_migrations(backend.to_apply(migrations))
    finally:
        _close_backend(backend)


def _rollback_chain(dsn: str, dim: int, count: int) -> list[str]:
    """Roll back the ``count`` most recently applied migrations.

    Returns the ids rolled back, most recent first. A migration with no
    ``.rollback.sql`` has no rollback steps, so yoyo unmarks it without
    running DDL — which is why ``0001`` must never be rolled back for
    real (the module docstring says so, and ``rollback`` refuses it).
    """
    from yoyo import get_backend, read_migrations

    migration_context.embedding_dim = dim
    backend = get_backend(_yoyo_dsn(dsn))
    try:
        migrations = read_migrations(str(_MIGRATIONS_DIR))
        applied = list(backend.to_rollback(migrations))[:count]
        if any(m.id.startswith("0001.") for m in applied):
            raise RuntimeError(
                "refusing to roll back 0001.initial-schema: it would drop every "
                "entry in the pool (ADR 0020 — a rollback that destroys data is "
                "not written). Reset the pool (`make pg-reset`) or restore from "
                "backup instead (docs/ops-runbook.md)."
            )
        ids = [m.id for m in applied]
        if ids:
            backend.rollback_migrations(applied)
        return ids
    finally:
        _close_backend(backend)


def _close_backend(backend: object) -> None:
    """Close the connection a yoyo backend opened in its constructor.

    yoyo's ``DatabaseBackend`` has no ``close()``; without this each
    migrate/rollback call leaks one connection for the life of the
    process (harmless for the one-shot entrypoint, not for anything
    long-lived — the integration suite exhausted ``max_connections``)."""
    connection = getattr(backend, "connection", None)
    if connection is not None:
        connection.close()


async def _acquire_migration_lock(conn: asyncpg.Connection) -> None:
    """Take the migration advisory lock by **polling**, never by blocking
    inside ``pg_advisory_lock``.

    This is not a style choice — a blocking wait deadlocks against
    ``CREATE INDEX CONCURRENTLY`` (ADR 0020 §6, first used by migration
    ``0004``), and the deadlock is invisible to Postgres:

    * the winning migrator holds the lock on *this* asyncpg connection
      and then runs the chain on a **separate** psycopg connection
      (``_apply_chain`` in a worker thread);
    * ``CREATE INDEX CONCURRENTLY`` waits for every transaction older
      than itself to finish, and a sibling parked inside
      ``SELECT pg_advisory_lock(...)`` is exactly that — one long-running
      statement, holding a virtual xid for as long as it waits;
    * that sibling is waiting on the lock the winner holds, so it never
      finishes, so the index build never finishes, so the lock is never
      released.

    Postgres's deadlock detector cannot break it: the lock *holder* is
    idle, not waiting, so the cycle is closed only through application
    logic and never appears in the wait graph. Measured: six migrators
    racing a cold pool hang indefinitely at ``deadlock_timeout = 1s``.

    Polling makes each waiter's transaction short, so the index build's
    wait set drains instead of stalling. The lock itself is unchanged —
    still session-scoped, so a pod killed mid-migration still releases it
    by dying, which is the property ADR 0020 chose it for. The wait is
    still unbounded, exactly as the blocking form was: the orchestrator's
    ``startupProbe`` is the timeout, not a number invented here.
    """
    while not await conn.fetchval("SELECT pg_try_advisory_lock($1)", MIGRATION_LOCK_KEY):
        await asyncio.sleep(_LOCK_POLL_SECONDS)


async def _with_migration_lock(dsn: str, dim: int, work: str, count: int = 0) -> list[str]:
    """Run a chain operation under the advisory lock, after the guards."""
    conn = await asyncpg.connect(dsn)
    try:
        await _acquire_migration_lock(conn)
        try:
            # ADR 0015: check the pool's provisioned dim AFTER taking the
            # lock (two replicas at different dims on a cold pool must not
            # both pass) and BEFORE applying anything, so a mismatched
            # pool fails loudly and leaves no partial state.
            actual = await _existing_embedding_dim(conn)
            if actual is not None:
                msg = dim_mismatch_message(actual, dim)
                if msg is not None:
                    raise RuntimeError(msg)
            if work == "apply":
                msg = pgvector_requirement_message(await _pgvector_version(conn), dim)
                if msg is not None:
                    raise RuntimeError(msg)
                await asyncio.to_thread(_apply_chain, dsn, dim)
                try:
                    await _require_valid_hnsw_index(conn)
                except (asyncpg.InterfaceError, asyncpg.PostgresConnectionError) as exc:
                    raise RuntimeError(
                        "the migration lock connection was lost during the run, so the "
                        "advisory lock was released early and another replica may have "
                        "run the chain concurrently. Re-run `hivemind-migrate` to confirm "
                        "the head."
                    ) from exc
                return []
            return await asyncio.to_thread(_rollback_chain, dsn, dim, count)
        finally:
            await _release_lock(conn)
    finally:
        if not conn.is_closed():
            await conn.close()


async def _release_lock(conn: asyncpg.Connection) -> None:
    """Best-effort explicit unlock. Closing the connection releases the
    lock regardless (a dead session cannot hold one); if the lock
    connection itself dropped mid-run, the lock was already lost — say so
    instead of letting an ``InterfaceError`` mask the migration's result."""
    try:
        await conn.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_KEY)
    except asyncpg.InterfaceError, asyncpg.PostgresConnectionError, OSError:
        logger.warning(
            "the migration lock connection was lost during the run; the advisory "
            "lock was released early, so another replica may have run the chain "
            "concurrently. Re-run `hivemind-migrate` to confirm the head."
        )


async def migrate(dsn: str, dim: int = 1024) -> None:
    """Apply every outstanding migration to the database at ``dsn``.

    Safe to re-run: an up-to-date pool applies nothing. Safe to run
    concurrently: the advisory lock serialises replicas.

    Raises:
        RuntimeError: when the pool's existing ``entries.embedding``
            column is at a *different* dimension than ``dim`` (ADR 0015).
            The message names both dims and both remediations.
    """
    await _with_migration_lock(dsn, dim, "apply")


async def rollback(dsn: str, dim: int = 1024, count: int = 1) -> list[str]:
    """Roll back the ``count`` most recently applied migrations.

    Returns the migration ids rolled back, most recent first. Refuses to
    roll back ``0001.initial-schema`` (ADR 0020: a rollback that would
    destroy data is not written).
    """
    if count < 1:
        raise ValueError(f"count must be at least 1, got {count}")
    return await _with_migration_lock(dsn, dim, "rollback", count)


def dim_mismatch_message(actual_dim: int, configured_dim: int) -> str | None:
    """The dim-mismatch error (ADR 0015), or ``None`` when the pool's dim
    matches the configured dim.

    The message names *both* dims and offers *both* remediations —
    reset the pool (fresh migrate) or point ``HIVEMIND_EMBEDDING_DIM``
    at the existing pool — so the operator never has to guess which pool
    is "the" one. A dim change is a pool reset, never in-place
    (ADR 0005).
    """
    if actual_dim == configured_dim:
        return None
    return (
        f"embedding dim mismatch: the pool's vector column is {actual_dim}-dim, "
        f"but the configured dim is {configured_dim} (HIVEMIND_EMBEDDING_DIM). "
        f"The dim is baked in at deploy time (ADR 0005) and cannot change "
        f"in place. Either reset the pool (`make pg-reset` then `make migrate`) "
        f"or set HIVEMIND_EMBEDDING_DIM={actual_dim} to match the existing pool."
    )


def pgvector_requirement_message(version: str | None, dim: int) -> str | None:
    """The pgvector pre-flight error, or ``None`` when satisfied.

    Requires pgvector >= 0.8 (``hnsw.iterative_scan``, SPEC §6.2 / ADR
    0025: on older servers migrate passes but every search fails at first
    use) and ``dim <= 2000`` (HNSW's limit: ``CREATE INDEX`` would fail
    in migration ``0004`` with a raw Postgres error). An unparseable
    version is not treated as a failure.
    """
    if dim > _MAX_HNSW_DIM:
        return (
            f"HIVEMIND_EMBEDDING_DIM={dim} exceeds the {_MAX_HNSW_DIM}-dimension limit of "
            f"pgvector's HNSW index (ADR 0025, migration 0004). Choose an embedding "
            f"model/dimension <= {_MAX_HNSW_DIM} (e.g. request a Matryoshka-truncated "
            f"size from the endpoint) and set HIVEMIND_EMBEDDING_DIM accordingly."
        )
    if version is None:
        return None
    match = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", version)
    if match is None:
        return None
    parsed = (int(match[1]), int(match[2]), int(match[3] or 0))
    if parsed < _MIN_PGVECTOR:
        return (
            f"pgvector {version} is too old: Hivemind needs pgvector >= 0.8 "
            f"(hnsw.iterative_scan, ADR 0025); without it every search fails. "
            f"Upgrade the server's pgvector package/image, then run "
            f"`ALTER EXTENSION vector UPDATE;` in this database and re-run migrate."
        )
    return None


def invalid_index_message(is_valid: bool) -> str | None:
    """The INVALID-HNSW-index error (a crashed ``CONCURRENTLY`` build),
    or ``None`` when the index is valid."""
    if is_valid:
        return None
    return (
        f"index {_HNSW_INDEX} exists but is INVALID (a crashed CREATE INDEX "
        f"CONCURRENTLY, ADR 0025): vector search silently falls back to a "
        f"sequential scan. Repair it with `REINDEX INDEX CONCURRENTLY {_HNSW_INDEX};` "
        f"(or `DROP INDEX CONCURRENTLY IF EXISTS {_HNSW_INDEX};` and re-run migrate) — "
        f"see docs/ops-runbook.md."
    )


async def _pgvector_version(conn: asyncpg.Connection) -> str | None:
    """The installed pgvector version, else the version the server would
    install (``CREATE EXTENSION`` has not run yet on a cold pool)."""
    version = await conn.fetchval("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    if version is None:
        version = await conn.fetchval(
            "SELECT default_version FROM pg_available_extensions WHERE name = 'vector'"
        )
    return str(version) if version is not None else None


async def _require_valid_hnsw_index(conn: asyncpg.Connection) -> None:
    """After the chain: fail loudly if the HNSW index is INVALID."""
    valid = await conn.fetchval(
        "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = $1",
        _HNSW_INDEX,
    )
    if valid is None:
        return
    msg = invalid_index_message(bool(valid))
    if msg is not None:
        raise RuntimeError(msg)


async def _existing_embedding_dim(conn: asyncpg.Connection) -> int | None:
    """The dim of the existing ``entries.embedding`` column, or ``None``
    when the pool has never been migrated (no ``entries`` table yet).

    pgvector stores the dimension as the column's typmod
    (``vector(512)`` → ``atttypmod == 512``), so this reads
    ``pg_attribute`` — which works on an empty pool (no rows needed).
    """
    try:
        row = await conn.fetchrow(
            "SELECT atttypmod FROM pg_attribute "
            "WHERE attrelid = 'entries'::regclass AND attname = 'embedding'"
        )
    except asyncpg.exceptions.UndefinedTableError:
        return None  # never-migrated pool: no ``entries`` table yet
    return row["atttypmod"] if row is not None else None


async def current_embedding_dim(dsn: str) -> int | None:
    """The dim of the pool's existing ``entries.embedding`` column, or
    ``None`` for a never-migrated pool (no such column yet).

    Used by the dim-mismatch guard (ADR 0015) and by ops tooling that
    wants to report a pool's provisioned dimension.
    """
    conn = await asyncpg.connect(dsn)
    try:
        return await _existing_embedding_dim(conn)
    finally:
        await conn.close()


async def current_schema_version(dsn: str) -> str | None:
    """The most recently applied migration id (ADR 0020), or ``None`` if
    the pool has never been migrated (``_yoyo_migration`` absent/empty).

    Used by the ops / health surface to check for drift. The table is
    yoyo's; there is no separate ``schema_migrations`` marker.
    """
    conn = await asyncpg.connect(dsn)
    try:
        try:
            row = await conn.fetchrow(
                "SELECT migration_id FROM _yoyo_migration "
                "ORDER BY applied_at_utc DESC, migration_id DESC LIMIT 1"
            )
        except asyncpg.exceptions.UndefinedTableError:
            return None  # never-migrated pool
        return row["migration_id"] if row is not None else None
    finally:
        await conn.close()


def main() -> None:
    """Console entry point (``hivemind-migrate``): migrate from settings.

    ``hivemind-migrate`` applies the chain. ``hivemind-migrate --rollback
    [N]`` reverses the N most recent migrations (default 1) — an
    explicit flag, never the default, because a rollback is a deliberate
    operator act.
    """
    settings = load_settings()
    argv = sys.argv[1:]
    try:
        if argv and argv[0] == "--rollback":
            count = int(argv[1]) if len(argv) > 1 else 1
            rolled = asyncio.run(rollback(settings.database_url, settings.embedding_dim, count))
            for migration_id in rolled:
                print(f"rolled back {migration_id}")
            if not rolled:
                print("nothing to roll back")
            return
        asyncio.run(migrate(settings.database_url, settings.embedding_dim))
    except (RuntimeError, ValueError) as exc:
        # A dim mismatch (ADR 0015) or a refused rollback (ADR 0020) is a
        # deployment error, not a crash — print the actionable message
        # and exit non-zero instead of a traceback.
        print(f"migrate error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
