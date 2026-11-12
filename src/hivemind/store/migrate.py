"""Migration runner: apply the ordered migration chain (ADR 0020).

Applies via yoyo every migration not yet recorded; an up-to-date pool is a no-op, so the entrypoint
runs it on every start (ADR 0018).

**Concurrency.** One session-scoped Postgres advisory lock is held for the whole run, so one replica
migrates and a killed pod releases it by dying. yoyo's ``backend.lock()`` is NOT used: a table row
with no stale detection would wedge every later pod. The lock is taken by polling.

**Rollback** runs ``.rollback.sql`` files (structural changes only; data loss is a restore,
``docs/ops-runbook.md``); ``0001`` is never rolled back. **Rollback guard** (ADR 0119): some frozen
rollbacks ``DELETE FROM audit_log`` and ``0006``'s revives revoked agents' keys, so under the lock,
before any DDL, ``rollback`` counts that impact and refuses unless ``discard_audit_rows`` /
``revive_revoked_keys`` / ``unlock_locked_keys`` is given. A rollback leaving ``0027`` applied
writes a ``schema.rollback`` audit row; one undoing it logs a WARNING.

**Pre-flight**, under the lock before any DDL: the dim guard (ADR 0015; under the lock so replicas
at different dims cannot both pass), pgvector >= 0.8, ``dim <= 2000``. After the chain, an INVALID
HNSW index (a crashed ``CONCURRENTLY`` build) fails the run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from hivemind.config import load_settings
from hivemind.domain.audit import ActorKind, AuditAction
from hivemind.store import migration_context

logger = logging.getLogger(__name__)

# `hnsw.iterative_scan` (every vector search) needs pgvector 0.8.0; HNSW indexes at most 2000
# dimensions.
_MIN_PGVECTOR = (0, 8, 0)
_MAX_HNSW_DIM = 2000
_HNSW_INDEX = "entries_embedding_hnsw_idx"

_MIGRATIONS_DIR = Path(__file__).with_name("migrations")

# Advisory-lock key (ADR 0020): fixed; ASCII "HIVEMIND" as an int64, recognisable in `pg_locks`.
MIGRATION_LOCK_KEY = 0x484956454D494E44

_SCHEMA_ROLLBACK_MIGRATION = "0027.schema-rollback-audit"
_REVOKED_AGENTS_MIGRATION = "0006.agent-revoked-status"
_KEY_LOCK_MIGRATION = "0025.key-lock"


@dataclass(frozen=True, slots=True)
class AuditDeletion:
    """Audit rows one ``.rollback.sql`` deletes: by ``actions`` or ``actor_kinds``."""

    actions: tuple[str, ...]
    actor_kinds: tuple[str, ...] = ()


# Audit rows each shipped rollback deletes (ADR 0119); ``tests/unit/test_migration_chain.py``
# cross-checks it against the SQL. A new rollback that deletes audit rows needs an entry.
ROLLBACK_AUDIT_DELETES: dict[str, AuditDeletion] = {
    "0009.audit-register": AuditDeletion(("agent.register",), ("org_key",)),
    "0014.pin-audit": AuditDeletion(("entry.pin", "entry.unpin"), ("agent_key",)),
    "0017.fleet-merge": AuditDeletion(("fleet.merge", "fleet.unmerge")),
    "0019.fleet-rename": AuditDeletion(("fleet.rename",)),
    "0020.agent-key-replace": AuditDeletion(("agent_key.replace",)),
    "0022.agent-cleanup": AuditDeletion(("agent.cleanup", "agent.cleanup_undo")),
    "0023.fleet-move": AuditDeletion(("fleet.move_agents", "fleet.move_undo")),
    "0024.agent-successions": AuditDeletion(("agent.retire", "agent.retire_undo")),
    "0025.key-lock": AuditDeletion(("agent_key.lock",)),
    _SCHEMA_ROLLBACK_MIGRATION: AuditDeletion(("schema.rollback",)),
    "0029.agent-owner-set": AuditDeletion(("agent.owner_set",)),
    "0032.entry-redact": AuditDeletion(("entry.redact",)),
    "0031.entry-restore": AuditDeletion(("entry.restore",)),
    "0033.fleet-phase": AuditDeletion(("fleet.phase_set", "fleet.phase_clear")),
}

_COUNT_AUDIT_ROWS = (
    "SELECT count(*) FROM audit_log WHERE action = ANY($1::text[]) OR actor_kind = ANY($2::text[])"
)
_COUNT_LOCKED_KEYS = "SELECT count(*) FROM credentials WHERE locked_at IS NOT NULL"
_COUNT_REVOKED_KEYS = (
    "SELECT count(*) FROM credentials c JOIN agents a ON a.name = c.agent_name "
    "WHERE c.kind = 'agent' AND a.status = 'revoked'"
)
_INSERT_ROLLBACK_AUDIT = (
    "INSERT INTO audit_log (actor_kind, actor, action, target, detail) "
    "VALUES ($1, $2, $3, $4, $5::jsonb)"
)


class RollbackRefused(RuntimeError):
    """A rollback that would erase audit rows or revive keys lacked its flag (ADR 0119)."""


@dataclass(frozen=True, slots=True)
class RollbackImpact:
    """What a rollback chain would destroy or revive, counted before any DDL."""

    audit_rows: dict[str, int]  # migration id -> rows its rollback deletes (non-zero only)
    audit_rows_total: int  # distinct rows across the whole chain
    revived_keys: int  # key rows of revoked agents that 0006's rollback revives
    unlocked_keys: int = 0  # locked key rows that 0025's rollback makes usable again


_LOCK_POLL_SECONDS = 0.25


def _yoyo_dsn(dsn: str) -> str:
    """Rewrite an asyncpg DSN to yoyo's ``postgresql+psycopg://``; an explicit ``+driver`` stays."""
    for prefix in ("postgresql://", "postgres://"):
        if dsn.startswith(prefix):
            return "postgresql+psycopg://" + dsn[len(prefix) :]
    return dsn


def _apply_chain(dsn: str, dim: int) -> None:
    """Apply every outstanding migration (sync; the caller holds the advisory lock)."""
    from yoyo import get_backend, read_migrations

    migration_context.embedding_dim = dim
    backend = get_backend(_yoyo_dsn(dsn))
    try:
        migrations = read_migrations(str(_MIGRATIONS_DIR))
        backend.apply_migrations(backend.to_apply(migrations))
    finally:
        _close_backend(backend)


def _rollback_plan(dsn: str, count: int) -> list[str]:
    """Ids the ``count`` latest migrations would roll back, newest first (read-only). Refuses
    ``0001``: with no ``.rollback.sql`` yoyo would unmark it without running DDL."""
    from yoyo import get_backend, read_migrations

    backend = get_backend(_yoyo_dsn(dsn))
    try:
        migrations = read_migrations(str(_MIGRATIONS_DIR))
        ids = [m.id for m in list(backend.to_rollback(migrations))[:count]]
    finally:
        _close_backend(backend)
    if any(i.startswith("0001.") for i in ids):
        raise RuntimeError(
            "refusing to roll back 0001.initial-schema: it would drop every "
            "entry in the pool (ADR 0020 — a rollback that destroys data is "
            "not written). Reset the pool (`make pg-reset`) or restore from "
            "backup instead (docs/ops-runbook.md)."
        )
    return ids


def _rollback_chain(dsn: str, dim: int, count: int) -> list[str]:
    """Roll back the ``count`` latest migrations; returns their ids, newest first."""
    from yoyo import get_backend, read_migrations

    migration_context.embedding_dim = dim
    ids = _rollback_plan(dsn, count)
    backend = get_backend(_yoyo_dsn(dsn))
    try:
        migrations = read_migrations(str(_MIGRATIONS_DIR))
        applied = list(backend.to_rollback(migrations))[: len(ids)]
        if [m.id for m in applied] != ids:
            raise RuntimeError("the applied chain changed between the plan and the rollback")
        if ids:
            backend.rollback_migrations(applied)
        return ids
    finally:
        _close_backend(backend)


async def rollback_impact(conn: asyncpg.Connection, ids: list[str]) -> RollbackImpact:
    """Count, read-only, the audit rows the rollbacks of ``ids`` would delete and the keys ``0006``
    would revive."""
    per_migration: dict[str, int] = {}
    actions: set[str] = set()
    kinds: set[str] = set()
    for migration_id in ids:
        deletion = ROLLBACK_AUDIT_DELETES.get(migration_id)
        if deletion is None:
            continue
        actions.update(deletion.actions)
        kinds.update(deletion.actor_kinds)
        n = int(
            await conn.fetchval(
                _COUNT_AUDIT_ROWS, list(deletion.actions), list(deletion.actor_kinds)
            )
        )
        if n:
            per_migration[migration_id] = n
    total = 0
    if per_migration:
        total = int(await conn.fetchval(_COUNT_AUDIT_ROWS, sorted(actions), sorted(kinds)))
    revived = 0
    if _REVOKED_AGENTS_MIGRATION in ids:
        revived = int(await conn.fetchval(_COUNT_REVOKED_KEYS))
    unlocked = 0
    if _KEY_LOCK_MIGRATION in ids:
        unlocked = int(await conn.fetchval(_COUNT_LOCKED_KEYS))
    return RollbackImpact(per_migration, total, revived, unlocked)


def rollback_refusal_message(
    impact: RollbackImpact,
    *,
    discard_audit_rows: bool,
    revive_revoked_keys: bool,
    unlock_locked_keys: bool = False,
) -> str | None:
    """The refusal naming what the rollback would destroy, or ``None`` if each non-zero count has
    its flag (ADR 0119)."""
    parts: list[str] = []
    if impact.audit_rows and not discard_audit_rows:
        lines = []
        for migration_id, n in impact.audit_rows.items():
            deletion = ROLLBACK_AUDIT_DELETES[migration_id]
            what = ", ".join(deletion.actions)
            if deletion.actor_kinds:
                what += " or actor_kind " + ", ".join(deletion.actor_kinds)
            lines.append(f"  {migration_id}: {n} audit row(s) ({what})")
        parts.append(
            "this rollback would permanently delete audit rows (ADR 0027, 0119):\n"
            + "\n".join(lines)
            + "\nDump the table first (`pg_dump --table=audit_log`, docs/ops-runbook.md), "
            "then pass --discard-audit-rows to proceed."
        )
    if impact.revived_keys and not revive_revoked_keys:
        parts.append(
            f"rolling back {_REVOKED_AGENTS_MIGRATION} would set revoked agents back to "
            f"active, reviving {impact.revived_keys} credential row(s) they still hold "
            "(ADR 0028, 0039). Delete those keys first (docs/ops-runbook.md, dead "
            "credentials), or pass --revive-revoked-keys to proceed."
        )
    if impact.unlocked_keys and not unlock_locked_keys:
        parts.append(
            f"rolling back {_KEY_LOCK_MIGRATION} drops the lock mark, so {impact.unlocked_keys} "
            "locked key(s) would be usable again (ADR 0117, 0119). Replace those keys first "
            "(docs/ops-runbook.md), or pass --unlock-locked-keys to proceed."
        )
    return "\n".join(parts) or None


async def _record_rollback(
    conn: asyncpg.Connection, rolled: list[str], discarded: int | None
) -> None:
    """Audit a finished rollback if ``0027`` is still applied, else log it."""
    from hivemind.store.keys import default_actor

    if not rolled:
        return
    applied = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM _yoyo_migration WHERE migration_id = $1)",
        _SCHEMA_ROLLBACK_MIGRATION,
    )
    detail: dict[str, object] = {"rolled_back": rolled}
    if discarded is not None:
        detail["discarded_audit_rows"] = discarded
    if applied:
        await conn.execute(
            _INSERT_ROLLBACK_AUDIT,
            ActorKind.CLI.value,
            default_actor(),
            AuditAction.SCHEMA_ROLLBACK.value,
            rolled[0],
            json.dumps(detail),
        )
    else:
        logger.warning(
            "schema rolled back (%s) past %s, so no audit row was written: %s",
            ", ".join(rolled),
            _SCHEMA_ROLLBACK_MIGRATION,
            json.dumps(detail),
        )


def _close_backend(backend: object) -> None:
    """Close the connection a yoyo backend opened; ``DatabaseBackend`` has no ``close()``."""
    connection = getattr(backend, "connection", None)
    if connection is not None:
        connection.close()


async def _acquire_migration_lock(conn: asyncpg.Connection) -> None:
    """Take the advisory lock by **polling**, never blocking in ``pg_advisory_lock``: a parked
    sibling would deadlock ``CREATE INDEX CONCURRENTLY`` invisibly to Postgres (ADR 0020 §6).
    The wait is unbounded; the orchestrator's ``startupProbe`` is the timeout."""
    while not await conn.fetchval("SELECT pg_try_advisory_lock($1)", MIGRATION_LOCK_KEY):
        await asyncio.sleep(_LOCK_POLL_SECONDS)


async def _with_migration_lock(
    dsn: str,
    dim: int,
    work: str,
    count: int = 0,
    *,
    discard_audit_rows: bool = False,
    revive_revoked_keys: bool = False,
    unlock_locked_keys: bool = False,
) -> list[str]:
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
            planned = await asyncio.to_thread(_rollback_plan, dsn, count)
            impact = await rollback_impact(conn, planned)
            refusal = rollback_refusal_message(
                impact,
                discard_audit_rows=discard_audit_rows,
                revive_revoked_keys=revive_revoked_keys,
                unlock_locked_keys=unlock_locked_keys,
            )
            if refusal is not None:
                raise RollbackRefused(refusal)
            rolled = await asyncio.to_thread(_rollback_chain, dsn, dim, count)
            if not await _lock_still_held(conn):
                raise RuntimeError(
                    _ROLLBACK_LOCK_LOST_MESSAGE.format(rolled=", ".join(rolled) or "none")
                )
            await _record_rollback(
                conn, rolled, impact.audit_rows_total if discard_audit_rows else None
            )
            return rolled
        finally:
            await _release_lock(conn, work)
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


_ROLLBACK_LOCK_LOST_MESSAGE = (
    "the migration lock connection was lost during the rollback (rolled back: "
    "{rolled}), so the advisory lock was released early and another replica may "
    "have applied the chain while it ran. Check the latest `migration_id` in "
    "`_yoyo_migration` before relying on the rollback."
)


async def _lock_still_held(conn: asyncpg.Connection) -> bool:
    """Whether the lock connection still holds the lock; any failure counts as lost (asyncpg
    reports a dead session as several exception types)."""
    try:
        held = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
            "AND pid = pg_backend_pid() AND granted)"
        )
    except Exception:
        return False
    return bool(held)


async def _release_lock(conn: asyncpg.Connection, work: str) -> None:
    """Best-effort unlock. A dead connection means the lock was lost: warn, never mask the result.
    After a rollback the warning must not advise re-applying, which would undo it."""
    try:
        await conn.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_KEY)
    except Exception:  # any dead-session shape (see _lock_still_held)
        if work == "apply":
            advice = "Re-run `hivemind-migrate` to confirm the head."
        else:
            advice = "Check the latest `migration_id` in `_yoyo_migration`."
        logger.warning(
            "the migration lock connection was lost during the %s; the advisory "
            "lock was released early, so another replica may have run the chain "
            "concurrently. %s",
            "run" if work == "apply" else "rollback",
            advice,
        )


async def migrate(dsn: str, dim: int = 1024) -> None:
    """Apply every outstanding migration; idempotent, safe to run concurrently.
    ``RuntimeError`` on a pre-flight failure such as a dim mismatch (ADR 0015)."""
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


async def rollback(
    dsn: str,
    dim: int = 1024,
    count: int = 1,
    *,
    discard_audit_rows: bool = False,
    revive_revoked_keys: bool = False,
    unlock_locked_keys: bool = False,
) -> list[str]:
    """Roll back the ``count`` latest migrations; returns their ids, newest first. Refuses ``0001``
    (ADR 0020). ``RollbackRefused``, with nothing changed, unless the matching flag allows
    deleting audit rows, reviving revoked keys or unlocking locked keys (ADR 0119)."""
    if count < 1:
        raise ValueError(f"count must be at least 1, got {count}")
    return await _with_migration_lock(
        dsn,
        dim,
        "rollback",
        count,
        discard_audit_rows=discard_audit_rows,
        revive_revoked_keys=revive_revoked_keys,
        unlock_locked_keys=unlock_locked_keys,
    )


def dim_mismatch_message(actual_dim: int, configured_dim: int) -> str | None:
    """The dim-mismatch error naming both dims and both remedies (ADR 0015), or ``None``. A dim
    change is a pool reset, never in place (ADR 0005)."""
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
    """The pgvector pre-flight error, or ``None``: needs >= 0.8 (older servers migrate but fail
    every search, ADR 0025) and ``dim <= 2000``. An unparseable version passes."""
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
    """Fail loudly if the HNSW index is INVALID, on its own connection."""
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
    """The ``entries.embedding`` dim from its typmod (works on an empty pool), or ``None`` if never
    migrated."""
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
    """The latest applied migration id (ADR 0020), or ``None`` if never migrated."""
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
    """``hivemind-migrate`` applies the chain; ``--rollback N`` reverses the N latest, refusing
    unless ``--discard-audit-rows``, ``--revive-revoked-keys`` or ``--unlock-locked-keys``
    allows what it would do (ADR 0119). Uses ``Settings.migration_dsn``."""
    settings = load_settings()
    argv = sys.argv[1:]
    try:
        if argv and argv[0] == "--rollback":
            known = {"--discard-audit-rows", "--revive-revoked-keys", "--unlock-locked-keys"}
            rest = argv[1:]
            flags = [a for a in rest if a.startswith("--")]
            numbers = [a for a in rest if not a.startswith("--")]
            unknown = sorted(set(flags) - known)
            if unknown:
                raise ValueError(f"unknown option(s): {', '.join(unknown)}")
            if len(numbers) != 1:
                raise ValueError(
                    "--rollback takes exactly one number (how many migrations to undo), "
                    f"got {len(numbers)}"
                )
            count = int(numbers[0])
            rolled = asyncio.run(
                rollback(
                    settings.migration_dsn,
                    settings.embedding_dim,
                    count,
                    discard_audit_rows="--discard-audit-rows" in flags,
                    revive_revoked_keys="--revive-revoked-keys" in flags,
                    unlock_locked_keys="--unlock-locked-keys" in flags,
                )
            )
            for migration_id in rolled:
                print(f"rolled back {migration_id}")
            if not rolled:
                print("nothing to roll back")
            return
        if argv:
            raise ValueError(f"unexpected argument(s): {' '.join(argv)} (only --rollback [N])")
        asyncio.run(migrate(settings.migration_dsn, settings.embedding_dim))
    except (RuntimeError, ValueError) as exc:
        # A deployment error, not a crash: message, no traceback.
        print(f"migrate error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
