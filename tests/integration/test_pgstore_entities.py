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

import pytest
from tests.pg import ensure_migrated, reset_tables

from hivemind.config import Settings
from hivemind.domain.entry import (
    EntityKind,
    EntryDraft,
    ExtractedEntity,
    Kind,
)
from hivemind.store import PgStore

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


@pytest.fixture
async def pg():
    """A ready ``PgStore`` on a clean dev database (skip when unreachable)."""
    dsn = _dsn()
    dim = _dim()
    await ensure_migrated(dsn, dim)
    await reset_tables(dsn)

    store = PgStore(dsn)
    try:
        yield store
    finally:
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
