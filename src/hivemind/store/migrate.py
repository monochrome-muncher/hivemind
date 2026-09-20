"""Migration runner: apply the idempotent schema (ADR 0007).

``migrate`` is idempotent — every DDL statement is guarded
(``IF NOT EXISTS`` / ``OR REPLACE`` / ``DROP ... IF EXISTS``) — so it is
safe to run on every service start and on every developer machine. The
embedding dimension is a deploy-time decision (ADR 0005) baked into the
``vector(:dim)`` column; a dimension change is an operator migration,
not an online feature (see SPEC.md §7).

The schema is a single ``schema.sql`` file (the source of truth) that
contains several top-level statements, including a dollar-quoted
plpgsql trigger function. ``migrate`` splits that file into individual
statements (respecting ``;`` inside ``$$`` blocks and comments) and
executes them one by one, because asyncpg cannot run a multi-statement
batch in a single ``execute`` call.

The dim-mismatch guard (ADR 0015): a pool provisioned at a *different*
embedding dimension than the configured one is a deployment error, not
an in-place change (ADR 0005). ``migrate`` checks the existing
column's dimension (``current_embedding_dim``) before applying any
statement and fails loudly with an actionable message — never a silent
no-op that later surfaces as a confusing ``DataError`` on the first
vector write.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import asyncpg

from hivemind.config import Settings

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# The applied schema generation (ADR 0013): a small marker recorded in
# ``schema_migrations`` after each successful migrate, so an operator can
# tell whether a live pool is up to date. **Bump this on every schema.sql
# change** (each schema generation gets a new version).
SCHEMA_VERSION = "5"


def _schema_sql(dim: int) -> str:
    """Load schema.sql with the embedding dimension substituted in."""
    return _SCHEMA_PATH.read_text().replace(":dim", str(dim))


def _split_statements(sql: str) -> list[str]:
    """Split a DDL script into top-level statements.

    A ``;`` ends a statement unless it appears inside a ``$$``
    dollar-quoted block (the plpgsql trigger body) or inside a comment.
    """
    parts: list[str] = []
    buf: list[str] = []
    in_dollar = False
    in_line_comment = False
    in_block_comment = False
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if in_line_comment:
            buf.append(c)
            if c == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            buf.append(c)
            if c == "*" and nxt == "/":
                buf.append(nxt)
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_dollar:
            buf.append(c)
            if c == "$" and nxt == "$":
                buf.append(nxt)
                in_dollar = False
                i += 2
                continue
            i += 1
            continue
        # normal mode
        if c == "-" and nxt == "-":
            in_line_comment = True
            buf.append(c)
            i += 1
            continue
        if c == "/" and nxt == "*":
            in_block_comment = True
            buf.append(c)
            i += 1
            continue
        if c == "$" and nxt == "$":
            in_dollar = True
            buf.append(c)
            i += 1
            continue
        if c == ";":
            stmt = "".join(buf).strip()
            if stmt:
                parts.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


async def migrate(dsn: str, dim: int = 1024) -> None:
    """Apply the idempotent schema to the database at ``dsn``.

    Each top-level statement is executed individually, so the migration
    is safe to re-run and the plpgsql trigger function (a dollar-quoted
    block containing ``;``) is handled correctly. After the schema is
    applied, the schema generation (``SCHEMA_VERSION``, ADR 0013) is
    recorded in ``schema_migrations`` (a single upsert), so a live pool
    can be checked for drift.

    Raises:
        RuntimeError: when the pool's existing ``entries.embedding``
            column is at a *different* dimension than ``dim`` (ADR 0015).
            The message names both dims and both remediations (reset the
            pool, or point ``HIVEMIND_EMBEDDING_DIM`` at the pool's dim).
            Failing here — before any schema statement — beats the
            confusing ``DataError`` (``expected 512 dimensions, not
            1024``) the first vector write would otherwise produce.
    """
    conn = await asyncpg.connect(dsn)
    try:
        # The dim-mismatch guard (ADR 0015): check the existing column's
        # dimension before applying anything, so a mismatched pool fails
        # loudly at migrate time with an actionable message.
        actual = await _existing_embedding_dim(conn)
        if actual is not None:
            msg = dim_mismatch_message(actual, dim)
            if msg is not None:
                raise RuntimeError(msg)
        for statement in _split_statements(_schema_sql(dim)):
            await conn.execute(statement)
        # Record the applied schema generation (ADR 0013 forward-migration
        # tracking): a single upsert, so re-running migrate is idempotent.
        await conn.execute(
            "INSERT INTO schema_migrations (version) VALUES ($1) "
            "ON CONFLICT (version) DO UPDATE SET applied_at = now()",
            SCHEMA_VERSION,
        )
    finally:
        await conn.close()


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
    """The applied schema generation (ADR 0013), or ``None`` if the pool
    has never been migrated (the ``schema_migrations`` table is absent
    or empty). Used by the ops / health surface to check for drift.
    """
    conn = await asyncpg.connect(dsn)
    try:
        try:
            row = await conn.fetchrow(
                "SELECT version FROM schema_migrations ORDER BY applied_at DESC LIMIT 1"
            )
        except asyncpg.exceptions.UndefinedTableError:
            return None  # pre-ADR-0013 pool (no schema_migrations table)
        return row["version"] if row is not None else None
    finally:
        await conn.close()


def main() -> None:
    """Console entry point (``hivemind-migrate``): migrate from settings."""
    settings = Settings()
    try:
        asyncio.run(migrate(settings.database_url, settings.embedding_dim))
    except RuntimeError as exc:
        # A dim mismatch (ADR 0015) is a deployment error, not a crash —
        # print the actionable message and exit non-zero instead of a
        # traceback.
        print(f"migrate error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
