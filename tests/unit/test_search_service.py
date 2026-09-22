"""Unit tests for the search pipeline (SPEC.md §6).

Seams under test: ``SearchService.search`` and the supersession
invariant, all driven through the ports (MemoryStore + FakeEmbedder)
so no implementation detail leaks in.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from hivemind.config import SearchConfig, Settings
from hivemind.domain.entry import EntryDraft, EntryFilters, ImportanceSource, Kind
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
            importance_source=ImportanceSource.CALLER,
            occurred_at=T0,
        )
        clock.advance_days(1)
        new = await create(
            store,
            "Auth service uses JWT with 30-minute token expiry",
            importance=1,  # low raw score (decayed by occurred_at)
            importance_source=ImportanceSource.CALLER,
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
        target = await create(
            store,
            "churn uses daily cohorts",
            occurred_at=T0,
            importance=5,
            importance_source=ImportanceSource.CALLER,
        )
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


class TestRecencyFloorConfig:
    """ADR 0022 / SPEC §6.4: the floor is config, reaches the score, and
    ships **on** at 0.8.
    """

    def test_the_shipped_default_is_the_adr_0022_floor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The value ADR 0022 decided, pinned where it is actually read.

        It lives in two places — the ``SearchConfig`` the services take
        and the ``Settings`` the runners build one from — and a drift
        between them would mean the deployed service does not score the
        way the spec says. 0.8 is not a slider: per ADR 0022 the floor is
        a *band*, and 0.9 measured *worse* than switching decay off.

        Hermetic like ``test_config_env_file``: no inherited env var, no
        profile file (``Settings`` reads ``.env.local`` when present).
        """
        monkeypatch.delenv("HIVEMIND_RECENCY_FLOOR", raising=False)
        monkeypatch.chdir(tmp_path)
        assert SearchConfig().recency_floor == 0.8
        assert Settings().search_config().recency_floor == 0.8

    def test_an_operator_tunes_the_floor_through_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``HIVEMIND_RECENCY_FLOOR`` is the operator's knob (ADR 0022).

        Its settable range there is ``(0, 1]``; there is deliberately no
        env spelling for "no floor" (the unbounded form is the defect
        ADR 0022 fixes), so a bad value is a loud startup failure rather
        than a silent fall back to it.
        """
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HIVEMIND_RECENCY_FLOOR", "0.5")
        assert Settings().search_config().recency_floor == 0.5
        monkeypatch.setenv("HIVEMIND_RECENCY_FLOOR", "null")
        with pytest.raises(ValidationError):
            Settings()

    @pytest.mark.parametrize("bad", [0.0, -0.1, 1.5])
    def test_a_floor_outside_the_unit_interval_is_a_configuration_error(self, bad: float) -> None:
        """A floor of 0 is "no floor" spelled misleadingly and a floor above
        1 would *boost* aged entries — both are configuration mistakes, and
        they fail at construction rather than silently skewing rankings."""
        with pytest.raises(ValueError, match="recency_floor"):
            SearchConfig(recency_floor=bad)

    def test_one_is_allowed_and_means_no_decay_at_all(self) -> None:
        assert SearchConfig(recency_floor=1.0).recency_floor == 1.0

    async def test_the_floor_reaches_the_score_through_the_service(
        self, embedder, search_config
    ) -> None:
        """The threading test: a knob that never reaches the scorer would
        make every §4.6 measurement a constant, and would mean ADR 0022
        shipped a no-op (the bug ADR 0021 found on the prefix knob). An
        entry 300 days old is ten half-lives down, so the shipped 0.8
        floor must raise its score by orders of magnitude over the
        pre-ADR-0022 unbounded term.
        """
        store = MemoryStore(make_clock())
        old = await create(store, "weekly cohorts", occurred_at=T0 - timedelta(days=300))
        unfloored = await make_service(
            store, embedder, SearchConfig(**{**vars_of(search_config), "recency_floor": None})
        ).search("weekly cohorts")
        floored = await make_service(store, embedder, search_config).search("weekly cohorts")
        assert [h.entry_id for h in unfloored] == [old.id] == [h.entry_id for h in floored]
        assert floored[0].score > unfloored[0].score * 100

    async def test_a_tight_enough_floor_flips_an_old_exact_match_back_on_top(
        self, embedder, search_config
    ) -> None:
        """What the floor is *for*, and the honest bound on it.

        With the unbounded term, 300 days of age outranks any match-quality
        difference, so the fresh loose match wins. Raising the floor closes
        the gap monotonically, and a floor tight enough to make `1/floor`
        narrower than *this pair's* fused range flips it back. Note what
        that range is here: both entries appear in **both** streams at
        adjacent ranks, so the fused spread is ~1.02x (not the 2.62x
        best-vs-worst ceiling) — which is exactly why 0.8 is not enough and
        1.0 is. The eval fixture measures the interesting middle.
        """
        store = MemoryStore(make_clock())
        old_exact = await create(
            store, "weekly cohorts churn analysis", occurred_at=T0 - timedelta(days=300)
        )
        fresh_loose = await create(store, "weekly rollup", occurred_at=T0)
        query = "weekly cohorts churn analysis"

        async def ratio(floor: float | None) -> float:
            config = SearchConfig(**{**vars_of(search_config), "recency_floor": floor})
            scores = {
                h.entry_id: h.score
                for h in await make_service(store, embedder, config).search(query)
            }
            return scores[old_exact.id] / scores[fresh_loose.id]

        async def winner(floor: float | None) -> str:
            config = SearchConfig(**{**vars_of(search_config), "recency_floor": floor})
            return (await make_service(store, embedder, config).search(query))[0].entry_id

        assert await winner(None) == fresh_loose.id
        assert await winner(1.0) == old_exact.id
        ratios = [await ratio(f) for f in (None, 0.2, 0.5, 0.8, 1.0)]
        assert ratios == sorted(ratios), f"a higher floor must never hurt the older entry: {ratios}"


def vars_of(config: SearchConfig) -> dict:
    """``SearchConfig`` is slotted, so ``vars()`` does not work on it."""
    return {f.name: getattr(config, f.name) for f in fields(config)}


class TestPagination:
    async def test_offset_paginates(self, embedder, search_config) -> None:
        """SPEC.md §5.3: search is limit/offset paginated."""
        store = MemoryStore(make_clock())
        for i in range(5):
            await create(store, f"cohort note number {i}")
        service = make_service(store, embedder, search_config)
        full = await service.search("cohort note", limit=5)
        paged = await service.search("cohort note", limit=2, offset=1)
        assert [h.entry_id for h in paged] == [h.entry_id for h in full[1:3]]
