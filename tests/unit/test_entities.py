"""Entity-extraction domain + store seam tests (ADR 0016, SPEC §13).

Hermetic: the ``EntityKind`` / ``ExtractedEntity`` model, the
``EntryFilters.entities`` AND + case-insensitive matching, and the
``MemoryStore`` round-trip (create with facets → get → filter), all
without a database or a live LLM.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from hivemind.domain.entry import (
    EntityKind,
    Entry,
    EntryDraft,
    EntryFilters,
    ExtractedEntity,
    Kind,
)
from hivemind.memstore import MemoryStore

FIXED_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def make_draft(
    summary: str = "Postgres connection pool exhausted",
    body: str | None = None,
) -> EntryDraft:
    return EntryDraft(
        kind=Kind.FACT,
        summary=summary,
        author="alice",
        agent="claude-code",
        body=body,
    )


# --- the facet model ------------------------------------------------------------


def test_entity_kind_is_a_closed_six_value_vocabulary() -> None:
    assert {k.value for k in EntityKind} == {
        "person",
        "organization",
        "system",
        "service",
        "artifact",
        "concept",
    }


def test_extracted_entity_trims_and_bounds_the_name() -> None:
    entity = ExtractedEntity(name="  Postgres  ", kind=EntityKind.SYSTEM)
    assert entity.name == "Postgres"
    assert ExtractedEntity(name="x" * 128, kind=EntityKind.SYSTEM).name == "x" * 128
    with pytest.raises(ValueError):
        ExtractedEntity(name="   ", kind=EntityKind.SYSTEM)
    with pytest.raises(ValueError):
        ExtractedEntity(name="x" * 129, kind=EntityKind.SYSTEM)


# --- the entry + filter shape ---------------------------------------------------


def test_entry_filters_entities_are_and_semantics_case_insensitive() -> None:
    """The pure ``matches`` method (shared by every store adapter)."""
    entry = Entry(
        id="e1",
        kind=Kind.FACT,
        summary="Postgres pool exhausted",
        author="alice",
        agent="claude-code",
        occurred_at=FIXED_NOW,
        created_at=FIXED_NOW,
        entities=(
            ExtractedEntity(name="Postgres", kind=EntityKind.SYSTEM),
            ExtractedEntity(name="auth-service", kind=EntityKind.SERVICE),
        ),
    )
    # AND-semantics: every listed name must be present.
    assert EntryFilters(entities=("Postgres", "auth-service")).matches(entry)
    assert not EntryFilters(entities=("Postgres", "kubernetes")).matches(entry)
    # Case-insensitive: filter names are lower-cased before matching.
    assert EntryFilters(entities=("postgres",)).matches(entry)
    assert EntryFilters(entities=("AUTH-SERVICE",)).matches(entry)
    # No entity filter matches everything.
    assert EntryFilters().matches(entry)


# --- the store round-trip --------------------------------------------------------


def test_memstore_roundtrips_facets_and_provenance() -> None:
    async def run() -> None:
        store = MemoryStore(clock=lambda: FIXED_NOW)
        entities = (
            ExtractedEntity(name="vLLM", kind=EntityKind.SERVICE),
            ExtractedEntity(name="embeddings", kind=EntityKind.CONCEPT),
        )
        entry = await store.create_entry(
            make_draft(), entities=entities, entities_model="qwen3.8-27b"
        )
        stored = await store.get_entry(entry.id)
        assert stored is not None
        assert stored.entities == entities
        assert stored.entities_model == "qwen3.8-27b"
        # The filter surface sees the facets (AND + case-insensitive).
        assert await store.list_entries(EntryFilters(entities=("vllm",))) == [entry]
        assert await store.list_entries(EntryFilters(entities=("VLLM", "EMBEDDINGS")))
        assert await store.list_entries(EntryFilters(entities=("vllm", "etcd"))) == []

        # An entry created without extraction (extractor off / failed)
        # carries the defaults: no facets, no provenance.
        bare = await store.create_entry(make_draft(summary="etcd cluster formed"))
        assert bare.entities == ()
        assert bare.entities_model is None

    asyncio.run(run())
