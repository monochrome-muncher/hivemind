"""Unit tests for MemoryStore (the reference Store implementation).

What both adapters share lives in ``tests/integration/test_store_contract.py``;
this file keeps what only the in-memory store pins: its keyword ranking, the
id tie-break under a fixed clock, and the draft's own validation.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from hivemind.domain.entry import (
    EntryDraft,
    EntryFilters,
    EntryState,
    ImportanceSource,
    Kind,
)
from hivemind.memstore import MemoryStore
from tests.fakes import FIXED_NOW, make_clock

UTC = FIXED_NOW.tzinfo


def draft(
    summary: str,
    *,
    author: str = "alice",
    agent: str = "claude-code",
    kind: Kind = Kind.FACT,
    body: str | None = None,
    importance: int = 3,
    tags: tuple[str, ...] = (),
    supersedes: tuple[str, ...] = (),
    occurred_at=None,
) -> EntryDraft:
    return EntryDraft(
        kind=kind,
        summary=summary,
        author=author,
        agent=agent,
        body=body,
        importance=importance,
        tags=tags,
        supersedes=supersedes,
        occurred_at=occurred_at,
    )


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore(make_clock())


async def test_create_entry_assigns_ids_and_created_at(store: MemoryStore) -> None:
    entry = await store.create_entry(draft("Auth service uses JWT"), [0.1, 0.2, 0.3])
    assert entry.id
    assert entry.created_at == FIXED_NOW
    assert entry.state is EntryState.ACTIVE
    assert entry.embedding == (0.1, 0.2, 0.3)


async def test_keyword_search_ranks_by_token_overlap(store: MemoryStore) -> None:
    jwt = await store.create_entry(draft("JWT tokens expire after 15 minutes", tags=("auth",)))
    other = await store.create_entry(draft("Cookies are HTTP only"))
    await store.create_entry(draft("Unrelated churn model"))

    results = await store.search_keyword("jwt token", EntryFilters(), limit=10)
    assert jwt.id in results
    assert other.id not in results
    # The jwt entry must rank first (only entry with both tokens).
    assert results[0] == jwt.id


async def test_keyword_search_respects_filters(store: MemoryStore) -> None:
    decision = await store.create_entry(
        draft("We decided to keep weekly cohorts", kind=Kind.DECISION)
    )
    fact = await store.create_entry(draft("Weekly cohorts are used"))
    results = await store.search_keyword(
        "weekly cohorts", EntryFilters(kind=Kind.DECISION), limit=10
    )
    assert decision.id in results
    assert fact.id not in results


async def test_occurred_range_filter(store: MemoryStore) -> None:
    t0 = FIXED_NOW
    early = await store.create_entry(draft("early", occurred_at=t0 - timedelta(days=10)))
    late = await store.create_entry(draft("late", occurred_at=t0 + timedelta(days=10)))
    mid_cut = t0
    in_range = await store.list_entries(
        EntryFilters(occurred_from=mid_cut, occurred_to=mid_cut), limit=10
    )
    assert [e.id for e in in_range] == []  # neither early(-10d) nor late(+10d)
    from_to = await store.list_entries(
        EntryFilters(occurred_from=t0 - timedelta(days=11), occurred_to=t0 + timedelta(days=11)),
        limit=10,
    )
    assert {e.id for e in from_to} == {early.id, late.id}


async def test_list_entries_tie_breaks_by_id_desc(store: MemoryStore) -> None:
    """Same ``created_at`` (fixed clock) -> order by id DESC (matches PgStore)."""
    a = await store.create_entry(draft("a"))
    b = await store.create_entry(draft("b"))
    c = await store.create_entry(draft("c"))
    listed = await store.list_entries(EntryFilters(), limit=10)
    assert [e.id for e in listed] == sorted([a.id, b.id, c.id], reverse=True)


def test_summary_over_280_chars_is_rejected() -> None:
    """SPEC.md §4.1: summary is a short blurb (~280 chars, not a body)."""
    with pytest.raises(ValueError, match="280"):
        draft("x" * 281)


def test_default_importance_source_requires_default_importance() -> None:
    """A draft can't claim ``importance_source=default`` while also
    supplying a non-default ``importance`` — that combination is a lie
    about provenance (ROADMAP §4.5): ``default`` means "the caller didn't
    set it", so it must carry the actual default value (3)."""
    with pytest.raises(ValueError, match="importance_source=default"):
        EntryDraft(
            kind=Kind.FACT,
            summary="claims default provenance but isn't",
            author="alice",
            agent="claude-code",
            importance=5,
            importance_source=ImportanceSource.DEFAULT,
        )


def test_entry_draft_importance_source_defaults_to_default() -> None:
    """ROADMAP §4.5: an ``EntryDraft`` that doesn't set ``importance_source``
    rides the ``default`` value (mirrors ``importance``'s own default)."""
    assert draft("unset").importance_source is ImportanceSource.DEFAULT


async def test_create_entry_defaults_importance_source(store: MemoryStore) -> None:
    """ROADMAP §4.5: a draft with no explicit provenance stores/reads back
    as ``default``."""
    entry = await store.create_entry(draft("rode the default"))
    assert entry.importance_source is ImportanceSource.DEFAULT
    reloaded = await store.get_entry(entry.id)
    assert reloaded is not None
    assert reloaded.importance_source is ImportanceSource.DEFAULT


async def test_create_entry_records_caller_importance_source(store: MemoryStore) -> None:
    """ROADMAP §4.5: a draft that explicitly marks ``caller`` provenance
    stores/reads back as ``caller``."""
    caller_draft = EntryDraft(
        kind=Kind.FACT,
        summary="caller set it",
        author="alice",
        agent="claude-code",
        importance=5,
        importance_source=ImportanceSource.CALLER,
    )
    entry = await store.create_entry(caller_draft)
    assert entry.importance == 5
    assert entry.importance_source is ImportanceSource.CALLER
    reloaded = await store.get_entry(entry.id)
    assert reloaded is not None
    assert reloaded.importance_source is ImportanceSource.CALLER


class TestEntryDraftNormalisesEnums:
    """``Kind`` and ``ImportanceSource`` are ``StrEnum``s, so a raw string
    compares EQUAL to the member but is not IDENTICAL to it.

    ``EntryFilters.matches`` compares both by identity, and the MCP
    serialiser reads ``entry.kind.value`` — so an un-normalised
    string-kind draft is silently invisible to a ``kind=`` filter (it
    under-counts ``by_kind``) and crashes the serialiser. The store
    adapters coerce on read; the draft closes the write side.
    """

    def test_string_kind_becomes_the_enum_member(self) -> None:
        draft = EntryDraft(kind="fact", summary="s", author="a", agent="b")  # type: ignore[arg-type]
        assert draft.kind is Kind.FACT
        assert draft.kind.value == "fact"  # the MCP serialiser's access

    def test_string_importance_source_becomes_the_enum_member(self) -> None:
        draft = EntryDraft(
            kind=Kind.FACT,
            summary="s",
            author="a",
            agent="b",
            importance=5,
            importance_source="caller",  # type: ignore[arg-type]
        )
        assert draft.importance_source is ImportanceSource.CALLER

    def test_an_unknown_kind_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError):
            EntryDraft(kind="nonsense", summary="s", author="a", agent="b")  # type: ignore[arg-type]

    async def test_a_string_kind_draft_is_countable_by_kind(self) -> None:
        """The regression this closes: before normalisation this count
        was 0, so ``MetricsService``'s ``by_kind`` under-reported every
        entry written through a string-kind draft."""
        store = MemoryStore(make_clock())
        await store.create_entry(
            EntryDraft(kind="fact", summary="s", author="alice", agent="a1"),  # type: ignore[arg-type]
            [0.1, 0.1, 0.1, 0.1],
        )
        assert await store.count_entries(EntryFilters(kind=Kind.FACT, include_inactive=True)) == 1
