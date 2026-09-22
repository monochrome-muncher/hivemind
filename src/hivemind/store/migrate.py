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

**Rollback.** ``rollback`` reverses the most recently applied
migration(s) via their ``.rollback.sql`` companions. Rollbacks are
written for *structural* changes only: reversing a populated column drop
or a backfill is a restore from backup (``docs/ops-runbook.md``), never
a migration. ``0001.initial-schema`` has no rollback at all — reversing
it would drop every entry in the pool.

**The dim guard (ADR 0015)** runs *before* the lock is taken: a pool
provisioned at a different embedding dimension than the configured one
is a deployment error (ADR 0005), and failing before any DDL beats the
confusing ``DataError`` the first vector write would otherwise produce.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import asyncpg

from hivemind.config import load_settings
from hivemind.store import migration_context

_MIGRATIONS_DIR = Path(__file__).with_name("migrations")

# The advisory-lock key (ADR 0020). Arbitrary but FIXED: every process
# that migrates this pool must take the same key. The value is ASCII
# "HIVEMIND" read as a big-endian int64 (fits in a signed bigint), which
# makes it self-identifying in `pg_locks`.
MIGRATION_LOCK_KEY = 0x484956454D494E44


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
    migrations = read_migrations(str(_MIGRATIONS_DIR))
    backend.apply_migrations(backend.to_apply(migrations))


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


async def _with_migration_lock(dsn: str, dim: int, work: str, count: int = 0) -> list[str]:
    """Run a chain operation under the advisory lock, after the dim guard."""
    conn = await asyncpg.connect(dsn)
    try:
        # ADR 0015: check the pool's provisioned dim BEFORE taking the
        # lock or applying anything, so a mismatched pool fails loudly
        # with an actionable message and leaves no partial state.
        actual = await _existing_embedding_dim(conn)
        if actual is not None:
            msg = dim_mismatch_message(actual, dim)
            if msg is not None:
                raise RuntimeError(msg)
        await conn.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK_KEY)
        try:
            if work == "apply":
                await asyncio.to_thread(_apply_chain, dsn, dim)
                return []
            return await asyncio.to_thread(_rollback_chain, dsn, dim, count)
        finally:
            # Best-effort explicit unlock; closing the connection below
            # releases it regardless (that is the point of an advisory
            # lock — a dead session cannot hold one).
            await conn.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_KEY)
    finally:
        await conn.close()


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
