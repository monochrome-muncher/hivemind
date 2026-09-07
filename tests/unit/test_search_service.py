"""Unit tests for the search pipeline (SPEC.md §6).

Seams under test: ``SearchService.search`` and the supersession
invariant, all driven through the ports (MemoryStore + FakeEmbedder)
so no implementation detail leaks in.
"""

from __future__ import annotations

from datetime import timedelta

from hivemind.config import SearchConfig
from hivemind.domain.entry import EntryDraft, EntryFilters, Kind
from hivemind.domain.feedback import Verdict
from hivemind.memstore import MemoryStore
from hivemind.services.search import SearchService
from tests.fakes import FIXED_NOW, make_clock

T0 = FIXED_NOW
HALF_LIFE = 30.0


def make_service(store: MemoryStore, embedder, config: SearchConfig) -> SearchService:
    return SearchService(store, embedder, config, now_fn=lambda: T0)


async def create(
    store: MemoryStore,
    summary: str,
    *,
    clock=None,
    **kwargs,
):
    draft = EntryDraft(
        kind=kwargs.pop("kind", Kind.INSIGHT),
        summary=summary,
        author=kwargs.pop("author", "alice"),
        agent=kwargs.pop("agent", "agent-1"),
        **kwargs,
    )
    return await store.create_entry(draft, [0.1, 0.1, 0.1, 0.1])


class TestSearchBasics:
    async def test_returns_compact_hits_without_body(self, embedder, search_config) -> None:
        clock = make_clock()
        store = MemoryStore(clock)
        await create(store, "Auth uses JWT", clock=clock)
        service = make_service(store, embedder, search_config)

        hits = await service.search("JWT auth")
        assert len(hits) == 1
        hit = hits[0]
        assert hit.summary == "Auth uses JWT"
        assert hit.kind is Kind.INSIGHT
        assert hit.score > 0.0
        # Progressive disclosure: the compact hit carries no body.
        assert not hasattr(hit, "body")

    async def test_empty_pool_returns_empty(self, embedder, search_config) -> None:
        store = MemoryStore(make_clock())
        service = make_service(store, embedder, search_config)
        assert await service.search("anything at all") == []

    async def test_limit_is_respected(self, embedder, search_config) -> None:
        store = MemoryStore(make_clock())
        for i in range(5):
            await create(store, f"note number {i} about cohorts")
        service = make_service(store, embedder, search_config)
        hits = await service.search("cohorts note", limit=2)
        assert len(hits) == 2


class TestDualStreamFusion:
    async def test_embedded_match_outranks_keyword_only_match(
        self, embedder, search_config
    ) -> None:
        """An entry that matches the query *semantically* (vector stream)
        and by keyword beats an entry that only shares keywords via a
        long unrelated body (SPEC.md §6.2 fusion)."""
        clock = make_clock()
        store = MemoryStore(clock)
        both = await create(store, "JWT token expiry")
        clock.advance_days(1)
        await create(
            store,
            "JWT token expiry",
            body="zzz qqq unrelated filler words that dilute the embedding",
        )
        service = make_service(store, embedder, search_config)
        hits = await service.search("JWT token expiry")
        assert hits[0].entry_id == both.id


class TestSupersessionInvariant:
    async def test_successor_outranks_predecessor_when_included(
        self, embedder, search_config
    ) -> None:
        """SPEC.md §6.3: the successor always ranks above the entry it
        superseded, even when the predecessor's raw score is higher."""
        clock = make_clock()
        store = MemoryStore(clock)
        old = await create(
            store,
            "Auth service uses JWT with 15-minute token expiry",
            importance=5,  # high raw score
            occurred_at=T0,
        )
        clock.advance_days(1)
        new = await create(
            store,
            "Auth service uses JWT with 30-minute token expiry",
            importance=1,  # low raw score (decayed by occurred_at)
            occurred_at=T0 - timedelta(days=60),
            supersedes=(old.id,),
        )
        service = make_service(store, embedder, search_config)

        hits = await service.search("JWT token expiry", filters=EntryFilters(include_inactive=True))
        ids = [h.entry_id for h in hits]
        assert new.id in ids and old.id in ids
        assert ids.index(new.id) < ids.index(old.id)

    async def test_superseded_entries_hidden_by_default(self, embedder, search_config) -> None:
        """SPEC.md §6.3: superseded entries are hidden unless
        include_inactive (SPEC.md §4.1: hidden by default)."""
        clock = make_clock()
        store = MemoryStore(clock)
        old = await create(store, "Token lifetime 15 minutes", occurred_at=T0)
        clock.advance_days(1)
        new = await create(
            store,
            "Token lifetime 30 minutes",
            supersedes=(old.id,),
            occurred_at=T0 - timedelta(days=1),
        )
        service = make_service(store, embedder, search_config)

        hits = await service.search("token lifetime")
        ids = [h.entry_id for h in hits]
        assert old.id not in ids
        assert new.id in ids


class TestDecayAndQuality:
    async def test_fresher_entry_outranks_stale_one(self, embedder, search_config) -> None:
        """SPEC.md §6.4: at equal importance and quality, recency wins."""
        store = MemoryStore(make_clock())
        await create(store, "weekly cohorts", occurred_at=T0 - timedelta(days=90))
        fresh = await create(store, "weekly cohorts", occurred_at=T0)
        service = make_service(store, embedder, search_config)
        hits = await service.search("weekly cohorts")
        assert hits[0].entry_id == fresh.id

    async def test_wrong_feedback_sinks_an_entry(self, embedder, search_config) -> None:
        """SPEC.md §4.2/§6.4: an entry reported `wrong` drops in rank."""
        store = MemoryStore(make_clock())
        target = await create(store, "churn uses daily cohorts", occurred_at=T0, importance=5)
        anchor = await create(store, "other churn note", occurred_at=T0, importance=3)
        service = make_service(store, embedder, search_config)

        before = [h.entry_id for h in await service.search("churn")]
        # Two independent 'wrong' reports: quality = 1.0 - 0.5 = 0.5.
        for user in ("u1", "u2"):
            await store.record_feedback(_feedback(target.id, user))
        after = [h.entry_id for h in await service.search("churn")]
        assert before.index(target.id) < before.index(anchor.id)
        assert after.index(target.id) > after.index(anchor.id)


def _feedback(entry_id: str, user: str):
    from hivemind.domain.feedback import Feedback

    return Feedback(
        entry_id=entry_id,
        user=user,
        agent="agent-x",
        verdict=Verdict.WRONG,
    )
