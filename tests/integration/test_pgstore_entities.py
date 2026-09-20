"""Postgres-backed entity-extraction tests (ADR 0016, SPEC §13).

Runs against the dev Postgres (docker, ``pgvector/pgvector:pg16``) like
the rest of the integration suite: self-contained fixture (skip
cleanly when the DB is unreachable, ``migrate`` + ``TRUNCATE`` before
each test) so ``uv run pytest`` stays green on a machine without a
database.

Covers the storage seam: the ``entities jsonb`` / ``entity_names
text[]`` / ``entities_model`` columns round-trip, and the
AND + case-insensitive filter clause (``entity_names @> ?``) behaves
like the ``tags`` pattern.
"""

from __future__ import annotations

from datetime import UTC, datetime

import asyncpg
import pytest

from hivemind.config import Settings
from hivemind.domain.entry import (
    EntityKind,
    EntryDraft,
    EntryFilters,
    ExtractedEntity,
    Kind,
)
from hivemind.store import PgStore
from hivemind.store.migrate import migrate

FIXED_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


# --- helpers (mirroring test_pgstore.py's self-contained fixture) ---------------


def _dsn() -> str:
    return Settings().database_url


def _dim() -> int:
    return Settings().embedding_dim


def make_draft(summary: str, body: str | None = None) -> EntryDraft:
    return EntryDraft(
        kind=Kind.FACT,
        summary=summary,
        author="alice",
        agent="claude-code",
        body=body,
    )


async def _truncate(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("TRUNCATE credentials, feedbacks, entries")
    finally:
        await conn.close()


async def _vector_extension_available(dsn: str) -> bool:
    conn = await asyncpg.connect(dsn)
    try:
        count = await conn.fetchval("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")
        return count > 0
    finally:
        await conn.close()


@pytest.fixture
async def pg():
    """A ready ``PgStore`` on a clean dev database (skip when unreachable)."""
    dsn = _dsn()
    dim = _dim()
    try:
        probe = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # connection refused / timeout / auth
        pytest.skip(f"Postgres unreachable at {dsn}: {exc}")
    await probe.close()

    if not await _vector_extension_available(dsn):
        pytest.skip("the 'vector' extension is not available on the dev database")

    await migrate(dsn, dim)
    await _truncate(dsn)

    store = PgStore(dsn)
    try:
        yield store
    finally:
        await _truncate(dsn)
        await store.close()


# --- the storage seam -----------------------------------------------------------


async def test_pgstore_roundtrips_facets_and_provenance(pg) -> None:
    entities = (
        ExtractedEntity(name="Postgres", kind=EntityKind.SYSTEM),
        ExtractedEntity(name="auth-service", kind=EntityKind.SERVICE),
    )
    entry = await pg.create_entry(
        make_draft("Postgres connection pool exhausted"),
        entities=entities,
        entities_model="qwen3.8-27b",
    )
    stored = await pg.get_entry(entry.id)
    assert stored is not None
    assert stored.entities == entities
    assert stored.entities_model == "qwen3.8-27b"


async def test_pgstore_entry_without_extraction_carries_defaults(pg) -> None:
    entry = await pg.create_entry(make_draft("etcd cluster formed"))
    stored = await pg.get_entry(entry.id)
    assert stored is not None
    assert stored.entities == ()
    assert stored.entities_model is None


async def test_pgstore_entity_filter_is_and_case_insensitive(pg) -> None:
    a = await pg.create_entry(
        make_draft("Postgres pool exhausted"),
        entities=(
            ExtractedEntity(name="Postgres", kind=EntityKind.SYSTEM),
            ExtractedEntity(name="auth-service", kind=EntityKind.SERVICE),
        ),
    )
    b = await pg.create_entry(
        make_draft("Kubernetes autoscaler misconfigured"),
        entities=(ExtractedEntity(name="Kubernetes", kind=EntityKind.SYSTEM),),
    )
    # AND-semantics: both names required.
    assert await pg.list_entries(EntryFilters(entities=("postgres", "auth-service"))) == [a]
    assert await pg.list_entries(EntryFilters(entities=("postgres", "etcd"))) == []
    # Case-insensitive: filter names are lower-cased against the column.
    assert await pg.list_entries(EntryFilters(entities=("POSTGRES",))) == [a]
    assert await pg.list_entries(EntryFilters(entities=("KUBERNETES",))) == [b]
    # search_keyword respects the filter too (the same WHERE builder).
    assert await pg.search_keyword("pool", EntryFilters(entities=("postgres",)), 10) == [a.id]
    assert await pg.search_keyword("pool", EntryFilters(entities=("kubernetes",)), 10) == []
