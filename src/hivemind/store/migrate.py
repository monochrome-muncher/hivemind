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
    block containing ``;``) is handled correctly.
    """
    conn = await asyncpg.connect(dsn)
    try:
        for statement in _split_statements(_schema_sql(dim)):
            await conn.execute(statement)
    finally:
        await conn.close()


def main() -> None:
    """Console entry point (``hivemind-migrate``): migrate from settings."""
    settings = Settings()
    asyncio.run(migrate(settings.database_url, settings.embedding_dim))
