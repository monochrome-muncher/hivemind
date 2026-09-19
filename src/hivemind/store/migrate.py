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
"""

from __future__ import annotations

import asyncio
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


async def migrate(dsn: str, dim: int = 1536) -> None:
    """Apply the idempotent schema to the database at ``dsn``.

    Each top-level statement is executed individually, so the migration
    is safe to re-run and the plpgsql trigger function (a dollar-quoted
    block containing ``;``) is handled correctly. After the schema is
    applied, the schema generation (``SCHEMA_VERSION``, ADR 0013) is
    recorded in ``schema_migrations`` (a single upsert), so a live pool
    can be checked for drift.
    """
    conn = await asyncpg.connect(dsn)
    try:
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
    asyncio.run(migrate(settings.database_url, settings.embedding_dim))
