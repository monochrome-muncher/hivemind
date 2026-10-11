"""``ENTRY_ID_REF`` (``domain/health.py``) is one pattern used twice: in
Python by the ``no_primary_source`` write warning, and in Postgres by the
fleet health grounding share (``HEALTH_SCALARS``, ``!~*``). Both regex
engines must classify every ref the same way."""

from __future__ import annotations

import asyncpg
import pytest

from hivemind.config import Settings
from hivemind.domain.health import ENTRY_ID_REF, is_entry_ref

ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
REFS = {
    ID: True,
    ID.upper(): True,
    ID.replace("-", ""): True,
    f"entry:{ID}": True,
    f"  {ID}  ": True,
    f"https://hivemind.example.com/v1/entries/{ID}": True,
    f"https://hivemind.example.com/v1/entries/{ID}/": True,
    "docs/runbook.md": False,
    f"https://github.com/org/repo/issues/{ID}": False,
    f"{ID} and a log line": False,
    "": False,
}


@pytest.mark.parametrize(("ref", "expected"), sorted(REFS.items()))
def test_python_classifies_entry_refs(ref: str, expected: bool) -> None:
    assert is_entry_ref(ref) is expected


async def test_postgres_agrees_with_python() -> None:
    dsn = Settings().database_url
    try:
        conn = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # connection refused / timeout / auth
        pytest.skip(f"Postgres unreachable at {dsn}: {exc}")
    try:
        for ref, expected in REFS.items():
            assert await conn.fetchval("SELECT $1::text ~* $2", ref, ENTRY_ID_REF) is expected, ref
    finally:
        await conn.close()
