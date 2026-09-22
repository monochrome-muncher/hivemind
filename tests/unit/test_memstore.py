"""Unit tests for MemoryStore (the reference Store implementation).

The in-memory store is a *reference implementation of the Store port*:
these tests pin its observable behavior so the Postgres adapter can be
tested against the same expectations.
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
from hivemind.domain.feedback import Feedback, Verdict
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


async def test_create_entry_with_supersedes_flips_targets(
    store: MemoryStore,
) -> None:
    old = await store.create_entry(draft("Token lifetime is 15 minutes"))
    new = await store.create_entry(draft("Token lifetime is 30 minutes", supersedes=(old.id,)))
    reloaded_old = await store.get_entry(old.id)
    reloaded_new = await store.get_entry(new.id)
    assert reloaded_old is not None
    assert reloaded_old.state is EntryState.SUPERSEDED
    assert reloaded_old.superseded_by == new.id
    assert reloaded_new is not None
    assert reloaded_new.state is EntryState.ACTIVE


async def test_supersede_unknown_target_is_ignored(store: MemoryStore) -> None:
    entry = await store.create_entry(draft("Something", supersedes=("no-such-id",)))
    assert entry.state is EntryState.ACTIVE


async def test_supersede_of_superseded_entry_leaves_it_as_is(
    store: MemoryStore,
) -> None:
    a = await store.create_entry(draft("v1"))
    b = await store.create_entry(draft("v2", supersedes=(a.id,)))
    await store.create_entry(draft("v3", supersedes=(a.id, b.id)))
    reloaded_b = await store.get_entry(b.id)
    assert reloaded_b is not None
    # b was superseded by c; c's create flipped b (still active at that
    # moment) — a had already been flipped by b.
    assert reloaded_b.state is EntryState.SUPERSEDED


async def test_withdraw_entry_sets_state_and_reason(store: MemoryStore) -> None:
    entry = await store.create_entry(draft("Retract this"))
    withdrawn = await store.withdraw_entry(entry.id, "turned out to be wrong", by_user="alice")
    assert withdrawn.state is EntryState.WITHDRAWN
    assert withdrawn.withdrawn_reason == "turned out to be wrong"
    reloaded = await store.get_entry(entry.id)
    assert reloaded is not None
    assert reloaded.state is EntryState.WITHDRAWN


async def test_withdraw_inactive_entry_raises(store: MemoryStore) -> None:
    entry = await store.create_entry(draft("Once active"))
    await store.withdraw_entry(entry.id, None, by_user="alice")
    with pytest.raises(ValueError):
        await store.withdraw_entry(entry.id, "again", by_user="alice")


async def test_withdraw_unknown_entry_raises(store: MemoryStore) -> None:
    with pytest.raises(KeyError):
        await store.withdraw_entry("nope", None, by_user="alice")


async def test_feedback_counts_aggregate_verdicts(store: MemoryStore) -> None:
    entry = await store.create_entry(draft("A fact"))

    async def give(verdict: Verdict, agent: str) -> None:
        await store.record_feedback(
            Feedback(
                entry_id=entry.id,
                user="bob",
                agent=agent,
                verdict=verdict,
            )
        )

    await give(Verdict.HELPFUL, "agent-1")
    await give(Verdict.STALE, "agent-2")
    await give(Verdict.WRONG, "agent-3")
    assert await store.feedback_counts(entry.id) == (1, 1, 1)


async def test_feedback_upsert_same_reporter_overrides(
    store: MemoryStore,
) -> None:
    entry = await store.create_entry(draft("A fact"))

    async def give(verdict: Verdict) -> None:
        await store.record_feedback(
            Feedback(entry_id=entry.id, user="bob", agent="agent-1", verdict=verdict)
        )

    await give(Verdict.HELPFUL)
    await give(Verdict.WRONG)  # same reporter, new verdict
    assert await store.feedback_counts(entry.id) == (0, 0, 1)


async def test_quality_counts_is_batched(store: MemoryStore) -> None:
    a = await store.create_entry(draft("fact a"))
    b = await store.create_entry(draft("fact b"))
    await store.record_feedback(
        Feedback(entry_id=a.id, user="bob", agent="a1", verdict=Verdict.HELPFUL)
    )
    counts = await store.quality_counts([a.id, b.id, "missing"])
    assert counts[a.id] == (1, 0, 0)
    assert counts[b.id] == (0, 0, 0)
    assert "missing" not in counts


async def test_get_entries_returns_only_known_ids(store: MemoryStore) -> None:
    e = await store.create_entry(draft("one"))
    got = await store.get_entries([e.id, "missing"])
    assert list(got) == [e.id]


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


async def test_vector_search_ranks_by_cosine_similarity(store: MemoryStore) -> None:
    close = await store.create_entry(draft("Auth uses JWT"), [0.9, 0.1, 0.0, 0.0])
    # Slightly positive dot with the query -> small-but-positive cosine,
    # ranked below `close`. (Exactly-orthogonal vectors score 0 and are
    # excluded, which is the intended behavior.)
    far = await store.create_entry(draft("Cookies"), [0.1, 0.1, 0.9, 0.1])
    unembedded = await store.create_entry(draft("No vector"), None)  # type: ignore[arg-type]

    results = await store.search_vector([0.95, 0.05, 0.0, 0.0], EntryFilters(), limit=10)
    assert results[0] == close.id
    assert unembedded.id not in results
    assert far.id in results
    assert results.index(close.id) < results.index(far.id)


async def test_list_entries_filters_and_pagination(store: MemoryStore) -> None:
    alice = await store.create_entry(draft("alice's fact", author="alice", tags=("x",)))
    bob = await store.create_entry(draft("bob's decision", author="bob", kind=Kind.DECISION))
    bob2 = await store.create_entry(draft("bob's fact", author="bob", tags=("x", "y")))

    all_x = await store.list_entries(EntryFilters(tags=("x",)), limit=10)
    assert {e.id for e in all_x} == {alice.id, bob2.id}

    bob_only = await store.list_entries(EntryFilters(author="bob", kind=Kind.DECISION), limit=10)
    assert [e.id for e in bob_only] == [bob.id]

    page = await store.list_entries(EntryFilters(), limit=1, offset=1)
    assert len(page) == 1


async def test_list_entries_hides_inactive_by_default(store: MemoryStore) -> None:
    old = await store.create_entry(draft("v1"))
    new = await store.create_entry(draft("v2", supersedes=(old.id,)))
    visible = await store.list_entries(EntryFilters(), limit=10)
    assert [e.id for e in visible] == [new.id]

    everything = await store.list_entries(EntryFilters(include_inactive=True), limit=10)
    assert {e.id for e in everything} == {old.id, new.id}


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


async def test_list_entries_orders_newest_first() -> None:
    """SPEC.md §11.4: most-recent-first is the assumed default list order."""
    from tests.fakes import make_clock

    clock = make_clock()
    store = MemoryStore(clock)
    first = await store.create_entry(draft("first"))
    clock.advance_days(1)
    second = await store.create_entry(draft("second"))
    clock.advance_days(1)
    third = await store.create_entry(draft("third"))
    listed = await store.list_entries(EntryFilters(), limit=10)
    assert [e.id for e in listed] == [third.id, second.id, first.id]


async def test_list_entries_tie_breaks_by_id_desc(store: MemoryStore) -> None:
    """Same ``created_at`` (fixed clock) -> order by id DESC (matches PgStore)."""
    a = await store.create_entry(draft("a"))
    b = await store.create_entry(draft("b"))
    c = await store.create_entry(draft("c"))
    listed = await store.list_entries(EntryFilters(), limit=10)
    assert [e.id for e in listed] == sorted([a.id, b.id, c.id], reverse=True)


async def test_create_entry_records_embedding_model(store: MemoryStore) -> None:
    """SPEC.md §7: the store records the embedding model per entry."""
    entry = await store.create_entry(
        draft("Uses embeddings"), [0.1, 0.2], embedding_model="fake-embedder"
    )
    assert entry.embedding_model == "fake-embedder"
    reloaded = await store.get_entry(entry.id)
    assert reloaded is not None
    assert reloaded.embedding_model == "fake-embedder"


def test_summary_over_280_chars_is_rejected() -> None:
    """SPEC.md §4.1: summary is a short blurb (~280 chars, not a body)."""
    with pytest.raises(ValueError, match="280"):
        draft("x" * 281)


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
