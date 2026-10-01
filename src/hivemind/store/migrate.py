"""Migration runner: apply the ordered migration chain (ADR 0020).

``migrate`` applies, via yoyo, every migration under ``migrations/`` not
yet recorded in ``_yoyo_migration``; an up-to-date pool is a no-op, so
the entrypoint runs it on every start (ADR 0018).

**Concurrency.** A session-scoped Postgres advisory lock is held for the
whole run, so exactly one replica migrates and a pod killed mid-run
releases it by dying. yoyo's ``backend.lock()`` is NOT used: it is a
table row with no stale detection, so a killed pod would wedge every
later one. The lock is taken by polling (see ``_acquire_migration_lock``).

**Rollback** reverses the latest migration(s) via ``.rollback.sql``
files, written for structural changes only; data loss is a restore from
backup (``docs/ops-runbook.md``). ``0001.initial-schema`` is never
rolled back.

**Pre-flight**, under the lock and before any DDL: the dim guard (ADR
0015, so two replicas at different dims cannot both pass), pgvector >=
0.8 and ``dim <= 2000``. After the chain, an INVALID HNSW index (a
crashed ``CONCURRENTLY`` build) fails the run.
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

# The advisory-lock key (ADR 0020): arbitrary but FIXED. ASCII "HIVEMIND"
# as a big-endian int64, so it is recognisable in `pg_locks`.
MIGRATION_LOCK_KEY = 0x484956454D494E44

# Poll interval for the lock: short delay, no busy-spin.
_LOCK_POLL_SECONDS = 0.25


def _yoyo_dsn(dsn: str) -> str:
    """Rewrite an asyncpg DSN to yoyo's psycopg3 scheme
    (``postgresql+psycopg://``); an explicit ``+driver`` is left alone.
    """
    for prefix in ("postgresql://", "postgres://"):
        if dsn.startswith(prefix):
            return "postgresql+psycopg://" + dsn[len(prefix) :]
    return dsn


def _apply_chain(dsn: str, dim: int) -> None:
    """Apply every outstanding migration (sync; run in a thread while the
    caller holds the advisory lock).
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

    Returns the ids rolled back, most recent first. yoyo would unmark a
    migration with no ``.rollback.sql`` without running DDL, hence the
    explicit ``0001`` refusal.
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

    yoyo's ``DatabaseBackend`` has no ``close()``, so each call would
    otherwise leak a connection for the life of the process."""
    connection = getattr(backend, "connection", None)
    if connection is not None:
        connection.close()


async def _acquire_migration_lock(conn: asyncpg.Connection) -> None:
    """Take the migration advisory lock by **polling**, never by blocking
    inside ``pg_advisory_lock``.

    A blocking wait deadlocks against ``CREATE INDEX CONCURRENTLY`` (ADR
    0020 §6) invisibly to Postgres: the winner holds the lock on this
    connection and runs the chain on a separate one; the index build
    waits for every older transaction, including a sibling parked in
    ``pg_advisory_lock``, which in turn waits on the winner. The holder
    is idle, so the cycle never shows in the wait graph.

    Polling keeps each waiter's transaction short. The wait stays
    unbounded; the orchestrator's ``startupProbe`` is the timeout.
    """
    while not await conn.fetchval("SELECT pg_try_advisory_lock($1)", MIGRATION_LOCK_KEY):
        await asyncio.sleep(_LOCK_POLL_SECONDS)


async def _with_migration_lock(dsn: str, dim: int, work: str, count: int = 0) -> list[str]:
    """Run a chain operation under the advisory lock, after the guards."""
    conn = await asyncpg.connect(dsn)
    try:
        await _acquire_migration_lock(conn)
        try:
            # Dim guard (ADR 0015): after the lock, before any DDL.
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
                if not await _lock_still_held(conn):
                    raise RuntimeError(_LOCK_LOST_MESSAGE)
                await _require_valid_hnsw_index(dsn)
                return []
            return await asyncio.to_thread(_rollback_chain, dsn, dim, count)
        finally:
            await _release_lock(conn)
    finally:
        if not conn.is_closed():
            try:
                await conn.close()
            except Exception:
                conn.terminate()  # a broken session cannot close cleanly; drop it


_LOCK_LOST_MESSAGE = (
    "the migration lock connection was lost during the run, so the advisory lock "
    "was released early and another replica may have run the chain concurrently. "
    "Re-run `hivemind-migrate` to confirm the head."
)


async def _lock_still_held(conn: asyncpg.Connection) -> bool:
    """Whether the lock connection is alive and still holds the lock.

    Any failure counts as lost: asyncpg reports a dead session as one of
    several exception types depending on timing."""
    try:
        held = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
            "AND pid = pg_backend_pid() AND granted)"
        )
    except Exception:
        return False
    return bool(held)


async def _release_lock(conn: asyncpg.Connection) -> None:
    """Best-effort unlock (closing the connection releases it anyway). A
    dead connection means the lock was already lost: warn rather than
    mask the migration's result."""
    try:
        await conn.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_KEY)
    except Exception:  # any dead-session shape (see _lock_still_held)
        logger.warning(
            "the migration lock connection was lost during the run; the advisory "
            "lock was released early, so another replica may have run the chain "
            "concurrently. Re-run `hivemind-migrate` to confirm the head."
        )


async def migrate(dsn: str, dim: int = 1024) -> None:
    """Apply every outstanding migration; idempotent and safe to run
    concurrently.

    Raises:
        RuntimeError: on a pre-flight failure, e.g. a dim mismatch (ADR 0015).
    """
    await _with_migration_lock(dsn, dim, "apply")
    await _warn_dead_credentials(dsn)


async def _warn_dead_credentials(dsn: str) -> None:
    """One-time startup WARNING counting credential rows that can no longer
    authenticate (ADR 0039), so operators notice and delete them. Purely
    informational: never raises."""
    from hivemind.store.auth import COUNT_DEAD_CREDENTIALS

    try:
        conn = await asyncpg.connect(dsn)
        try:
            dead = int(await conn.fetchval(COUNT_DEAD_CREDENTIALS))
        finally:
            await conn.close()
    except Exception:
        return
    if dead:
        logger.warning(
            "%d credential row(s) are dead (pre-v2 user keys, name-less agent keys, or keys "
            "of agents that are not active) and will never authenticate (ADR 0039); they "
            "would live again on a rollback to an older release. Delete them - see "
            "docs/ops-runbook.md (dead credentials).",
            dead,
        )


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
    """The dim-mismatch error (ADR 0015) naming both dims and both
    remedies, or ``None`` when they match. A dim change is a pool reset,
    never in place (ADR 0005).
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

    Requires pgvector >= 0.8 (``hnsw.iterative_scan``, ADR 0025; older
    servers migrate fine but fail every search) and ``dim <= 2000``
    (HNSW's limit). An unparseable version passes.
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


async def _require_valid_hnsw_index(dsn: str) -> None:
    """After the chain: fail loudly if the HNSW index is INVALID. Runs on
    its own connection, independent of the lock connection's health."""
    conn = await asyncpg.connect(dsn)
    try:
        valid = await conn.fetchval(
            "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = $1",
            _HNSW_INDEX,
        )
    finally:
        await conn.close()
    if valid is None:
        return
    msg = invalid_index_message(bool(valid))
    if msg is not None:
        raise RuntimeError(msg)


async def _existing_embedding_dim(conn: asyncpg.Connection) -> int | None:
    """The ``entries.embedding`` dim from its typmod (works on an empty
    pool), or ``None`` when the pool has never been migrated.
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
    """The pool's provisioned embedding dim, or ``None`` if never migrated."""
    conn = await asyncpg.connect(dsn)
    try:
        return await _existing_embedding_dim(conn)
    finally:
        await conn.close()


async def current_schema_version(dsn: str) -> str | None:
    """The most recently applied migration id from yoyo's table (ADR
    0020), or ``None`` if the pool has never been migrated.
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
    """Console entry point: ``hivemind-migrate`` applies the chain;
    ``--rollback [N]`` reverses the N latest (default 1). Uses
    ``Settings.migration_dsn``.
    """
    settings = load_settings()
    argv = sys.argv[1:]
    try:
        if argv and argv[0] == "--rollback":
            count = int(argv[1]) if len(argv) > 1 else 1
            rolled = asyncio.run(rollback(settings.migration_dsn, settings.embedding_dim, count))
            for migration_id in rolled:
                print(f"rolled back {migration_id}")
            if not rolled:
                print("nothing to roll back")
            return
        asyncio.run(migrate(settings.migration_dsn, settings.embedding_dim))
    except (RuntimeError, ValueError) as exc:
        # A deployment error, not a crash: message, no traceback.
        print(f"migrate error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
