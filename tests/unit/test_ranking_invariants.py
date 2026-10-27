"""Ranking invariants every ranking change must pass (ADR 0070).

They run against the SHIPPED ``SearchConfig()`` defaults, so a change that
moves a default, or adds a social signal outside the cap, fails here.

* I1  A newly written unrated entry under about 1.7 days old is never
      outranked by an entry 10 or more days old, at equal match and
      importance, however much feedback the old one has.
* I2  Ranking inputs are numbers and dates only: no author, agent label,
      pin, link or rater identity reaches the score.
* I3  A confirmation date never reaches the score: "last confirmed" is
      display only (ADR 0103; ``tests/unit/test_last_confirmed.py`` pins it
      through the service).
* I5  The upward multiplier from all social signals never exceeds
      ``social_max``; penalties are not capped.
* I6  With the narrow importance factor on (ADR 0088), one importance step
      (e.g. 5 against 3) stays below the peer cap at the default
      ``social_max``, so it does not outweigh peer confirmation at the same
      match and age.

Ranking has no per-kind recency profile and no confirmation clock (both
removed, ADR 0103): every kind ages by ``half_life_days``. "No decay" is
not offered as a setting, because it would break I1; only per search via the
explicit ``ignore_age`` hint (ADR 0073).
"""

from __future__ import annotations

import inspect
import math
from datetime import timedelta

import pytest

from hivemind.config import SearchConfig
from hivemind.domain.entry import EntryDraft, Kind
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.memstore import MemoryStore
from hivemind.retrieval import scoring
from hivemind.retrieval.scoring import SOCIAL_MAX, entry_score, feedback_quality
from hivemind.services.search import SearchService
from tests.fakes import FIXED_NOW, make_clock, make_embedder

CONFIG = SearchConfig()
NARROW = SearchConfig(narrow_importance=True)
WIDE = SearchConfig(narrow_importance=False)
NOW = FIXED_NOW
FRESH_DAYS = (0.0, 0.5, 1.0, 1.69)
OLD_DAYS = (10.0, 11.0, 30.0, 400.0)
FEEDBACK_MIXES = (
    *((h, 0, 0) for h in (0, 1, 2, 3, 5, 10, 50, 100, 1000)),
    (1000, 1, 0),
    (1000, 0, 1),
    (500, 5, 5),
    (3, 1, 0),
)


def shipped_score(
    *,
    age_days: float,
    counts: tuple[int, int, int] = (0, 0, 0),
    fused: float = 1.0,
    importance: int = 3,
    config: SearchConfig = CONFIG,
) -> float:
    """The score SearchService computes, for one entry, through the pure functions."""
    quality = feedback_quality(*counts, **config.quality_kwargs())
    return entry_score(
        fused,
        importance,
        NOW - timedelta(days=age_days),
        quality,
        NOW,
        config.half_life_days,
        recency_floor=config.recency_floor,
        social_max=config.social_max,
        narrow_importance=config.narrow_importance,
    )


class TestI1FreshUnratedBeatsOldFavourite:
    @pytest.mark.parametrize("fresh_age", FRESH_DAYS)
    @pytest.mark.parametrize("old_age", OLD_DAYS)
    @pytest.mark.parametrize("counts", FEEDBACK_MIXES)
    def test_old_entry_never_outranks_a_fresh_unrated_one(
        self, fresh_age: float, old_age: float, counts: tuple[int, int, int]
    ) -> None:
        """I1: at equal fused score and importance, fresh unrated >= old, any feedback."""
        for importance in (1, 3, 5):
            fresh = shipped_score(age_days=fresh_age, importance=importance)
            old = shipped_score(age_days=old_age, counts=counts, importance=importance)
            assert fresh >= old

    def test_the_two_ages_of_the_shipped_half_life(self) -> None:
        """SPEC §6.4's numbers: a new unrated entry younger than about 1.7 days
        beats any entry past the floor age of about 9.7 days."""
        assert CONFIG.recency_floor is not None and CONFIG.half_life_days == 30.0
        fresh = CONFIG.half_life_days * math.log2(1.0 / (CONFIG.social_max * CONFIG.recency_floor))
        floor_age = CONFIG.half_life_days * math.log2(1.0 / CONFIG.recency_floor)
        assert 0.0 < fresh < floor_age
        assert 0.5 ** (fresh / 30.0) == pytest.approx(CONFIG.social_max * CONFIG.recency_floor)
        assert 0.5 ** (floor_age / 30.0) == pytest.approx(CONFIG.recency_floor)
        assert 1.7 < fresh < 1.8 and 9.6 < floor_age < 9.7

    async def test_service_ratio_of_an_old_favourite_stays_below_a_new_entry(self) -> None:
        """I1 through SearchService: with many helpful verdicts from other
        users, the old entry's score/fused ratio is at most the new one's.

        Fused scores cannot be made equal through real streams, so the ratio
        (what everything after fusion contributes) is compared; it is read
        from the arguments ``entry_score`` is called with.
        """
        store = MemoryStore(make_clock())
        old = await store.create_entry(_draft("gamma rollout note", age_days=45), [0.1] * 4)
        new = await store.create_entry(_draft("gamma rollout note", age_days=1), [0.1] * 4)
        for i in range(200):
            await store.record_feedback(
                Feedback(
                    old.id,
                    f"rater-{i}",
                    "a",
                    Verdict.HELPFUL,
                    note="re-checked",
                    updated_at=NOW - timedelta(minutes=1),
                )
            )

        hits = await _service(store).search("gamma rollout note", limit=10)
        by_id = {h.entry_id: h.score for h in hits}
        fused = await _fused_by_id(store, "gamma rollout note")
        assert set(by_id) == {old.id, new.id}
        assert by_id[old.id] / fused[old.id] < by_id[new.id] / fused[new.id]
        assert [h.entry_id for h in hits] == [new.id, old.id]


class TestI2NumbersAndDatesOnly:
    def test_entry_score_takes_only_the_allowlisted_inputs(self) -> None:
        """I2: a new parameter (an author, a count of links, a rater record)
        must be added to this allowlist on purpose, in review. There is no
        confirmation input: "last confirmed" is display only (ADR 0103)."""
        assert set(inspect.signature(entry_score).parameters) == {
            "fused",
            "importance",
            "occurred_at",
            "quality",
            "now",
            "half_life_days",
            "recency_floor",
            "social_max",
            "narrow_importance",
        }

    async def test_the_kind_does_not_reach_the_score(self) -> None:
        """I2: two kinds at the same age, with the same match, score alike."""
        store = MemoryStore(make_clock())
        a = await store.create_entry(
            _draft("zeta cutover", age_days=100, kind=Kind.DECISION), [0.1] * 4
        )
        b = await store.create_entry(_draft("zeta cutover", age_days=100), [0.1] * 4)
        by_id = {h.entry_id: h.score for h in await _service(store).search("zeta cutover")}
        fused = await _fused_by_id(store, "zeta cutover")
        assert by_id[a.id] / fused[a.id] == pytest.approx(by_id[b.id] / fused[b.id])

    async def test_authors_agents_pins_links_and_raters_do_not_change_scores(self) -> None:
        """I2: two pools identical except for author names, agent labels, pins,
        see_also links and who the raters are score and order identically."""
        plain = await _pool(
            authors=("alice", "alice", "alice"),
            agents=("a1", "a1", "a1"),
            raters=("r1", "r2", "r3"),
            pins=False,
            links=False,
        )
        dressed = await _pool(
            authors=("zed", "yan", "xia"),
            agents=("bot-9", "bot-8", "bot-7"),
            raters=("q1", "q2", "q3"),
            pins=True,
            links=True,
        )
        assert len(plain) == 3
        assert plain == dressed


class TestI5UpwardMultiplierIsCapped:
    @pytest.mark.parametrize("helpful", [0, 1, 5, 1000])
    @pytest.mark.parametrize("age", [0.0, 3.0, 9.0, 10.0, 45.0, 400.0, 5000.0])
    @pytest.mark.parametrize("floor", [0.8, 0.5, None])
    def test_the_social_multiplier_never_exceeds_social_max_at_any_age(
        self, helpful: int, age: float, floor: float | None
    ) -> None:
        """I5: score / recency, the social multiplier, is at most social_max
        (importance 3, fused 1), at any age and floor."""
        config = SearchConfig(recency_floor=floor)
        score = shipped_score(age_days=age, counts=(helpful, 0, 0), importance=3, config=config)
        recency = max(floor or 0.0, 0.5 ** (age / config.half_life_days))
        assert score / recency <= config.social_max * (1 + 1e-12)

    def test_a_larger_quality_max_still_stops_at_social_max(self) -> None:
        """I5: quality_max 2.0 (helpful reports heading to 2.0) is capped at 1.2."""
        wide = SearchConfig(quality_max=2.0, quality_helpful_weight=1.0)
        assert feedback_quality(100, 0, 0, **wide.quality_kwargs()) > 1.9
        score = shipped_score(age_days=0.0, counts=(100, 0, 0), importance=3, config=wide)
        assert score == pytest.approx(wide.social_max)

    @pytest.mark.parametrize("counts", [(0, 0, 1), (0, 5, 0), (0, 0, 1000), (1, 1, 1), (0, 1, 0)])
    def test_penalties_pass_through_uncapped(self, counts: tuple[int, int, int]) -> None:
        """I5: a quality below 1 is the multiplier as it is; the cap bounds only the upside."""
        quality = feedback_quality(*counts, **CONFIG.quality_kwargs())
        assert quality < 1.0
        assert shipped_score(age_days=0.0, counts=counts, importance=3) == quality
        assert (
            entry_score(
                1.0,
                5,
                NOW,
                0.5,
                NOW,
                30.0,
                recency_floor=0.8,
                social_max=SOCIAL_MAX,
                narrow_importance=False,
            )
            == 0.5
        )

    def test_the_cap_is_a_ceiling_on_the_pure_function(self) -> None:
        """I5: with an explicit cap, any quality above it is cut to it."""
        capped = entry_score(1.0, 5, NOW, 2.0, NOW, 30.0, social_max=1.2, narrow_importance=False)
        assert capped == pytest.approx(1.2)
        assert entry_score(1.0, 5, NOW, 2.0, NOW, 30.0, narrow_importance=False) == pytest.approx(
            2.0
        )

    def test_the_shipped_cap_equals_the_shipped_quality_ceiling(self) -> None:
        """I5: social_max == quality_max by default, which is why the cap changes no ranking."""
        assert SOCIAL_MAX == CONFIG.social_max == CONFIG.quality_max == scoring.QUALITY_MAX


class TestBitIdentity:
    def test_the_cap_changes_no_score_at_the_shipped_defaults(self) -> None:
        """The capped form equals the uncapped form (bit for bit) at quality_max 1.2."""
        for fused in (0.0164, 0.0082, 0.5, 1.0):
            for importance in range(1, 6):
                for age in (0.0, 1.0, 9.0, 10.0, 45.0, 400.0, 5000.0):
                    for counts in (*FEEDBACK_MIXES, (0, 0, 3), (0, 4, 0), (2, 2, 2)):
                        quality = feedback_quality(*counts, **CONFIG.quality_kwargs())
                        args = (
                            fused,
                            importance,
                            NOW - timedelta(days=age),
                            quality,
                            NOW,
                            CONFIG.half_life_days,
                        )
                        narrow = CONFIG.narrow_importance
                        uncapped = entry_score(
                            *args, recency_floor=CONFIG.recency_floor, narrow_importance=narrow
                        )
                        capped = entry_score(
                            *args,
                            recency_floor=CONFIG.recency_floor,
                            social_max=1.2,
                            narrow_importance=narrow,
                        )
                        assert capped == uncapped
                        assert math.isfinite(capped)


# --- helpers -----------------------------------------------------------------


def _draft(summary: str, *, age_days: float, **kwargs: object) -> EntryDraft:
    kind = kwargs.pop("kind", Kind.INSIGHT)
    return EntryDraft(
        kind=kind,  # type: ignore[arg-type]
        summary=summary,
        author=str(kwargs.pop("author", "alice")),
        agent=str(kwargs.pop("agent", "agent-1")),
        occurred_at=NOW - timedelta(days=age_days),
        **kwargs,  # type: ignore[arg-type]
    )


def _service(store: MemoryStore, config: SearchConfig = CONFIG) -> SearchService:
    return SearchService(store, make_embedder(), config, now_fn=lambda: NOW)


async def _fused_by_id(store: MemoryStore, query: str) -> dict[str, float]:
    """The fused RRF score the service computes, from the same streams."""
    from hivemind.domain.entry import EntryFilters
    from hivemind.retrieval.rrf import rrf_fuse

    keyword = await store.search_keyword(query, EntryFilters(), CONFIG.candidate_top_k)
    vector = await store.search_vector(
        await make_embedder().embed_text(CONFIG.embedded_query(query)),
        EntryFilters(),
        CONFIG.candidate_top_k,
    )
    return rrf_fuse(
        [keyword, vector],
        k=CONFIG.rrf_k,
        weights=[CONFIG.weight_keyword, CONFIG.weight_vector],
    )


async def _pool(
    *,
    authors: tuple[str, str, str],
    agents: tuple[str, str, str],
    raters: tuple[str, str, str],
    pins: bool,
    links: bool,
) -> list[tuple[str, float]]:
    """Three entries (same text, dates, importance), rated alike by different
    users; return (summary, score) in result order."""
    # The store clock advances per write so created_at breaks keyword ties
    # deterministically (entry ids are random).
    clock = make_clock()
    store = MemoryStore(clock)
    summaries = ("alpha deploy runbook", "beta deploy runbook", "gamma deploy runbook")
    ages = (3.0, 40.0, 200.0)
    ids: list[str] = []
    for i, summary in enumerate(summaries):
        see_also = (ids[0],) if links and i > 0 else ()
        entry = await store.create_entry(
            _draft(
                summary,
                age_days=ages[i],
                author=authors[i],
                agent=agents[i],
                see_also=see_also,
                scope="fleet" if pins else "org",
                fleet_id="fleet-a" if pins else None,
            ),
            # Distinct vectors keep the vector stream's order deterministic.
            await make_embedder().embed_text(summary),
        )
        ids.append(entry.id)
        clock.advance_days(1.0 / 86_400.0)
    verdicts = ((Verdict.HELPFUL, Verdict.HELPFUL), (Verdict.STALE,), (Verdict.HELPFUL,))
    for i, entry_id in enumerate(ids):
        for j, verdict in enumerate(verdicts[i]):
            await store.record_feedback(
                Feedback(entry_id, f"{raters[j]}", authors[0] + "x", verdict)
            )
    if pins:
        for entry_id in ids:
            await store.pin_entry("fleet-a", entry_id, authors[0], 10)
    hits = await _service(store).search("deploy runbook", limit=10)
    names = dict(zip(ids, summaries, strict=True))
    return [(names[h.entry_id], h.score) for h in hits]


class TestI6NarrowImportanceStaysBelowTheSocialCap:
    """I6 (ADR 0088): the narrow factor is 0.9..1.1, one step about 1.05x."""

    @staticmethod
    def _factor(importance: int) -> float:
        return entry_score(1.0, importance, NOW, 1.0, NOW, 30.0, narrow_importance=True)

    def test_one_step_is_smaller_than_the_social_cap(self) -> None:
        for importance in range(1, 5):
            step = self._factor(importance + 1) / self._factor(importance)
            assert 1.0 < step < CONFIG.social_max
        # The widest step, importance 1 to 5, is 1.1 / 0.9 and still below
        # the wide factor's own 1.0 / 0.6.
        assert self._factor(5) / self._factor(1) == pytest.approx(1.1 / 0.9)

    def test_importance_five_does_not_beat_a_peer_confirmed_importance_three(self) -> None:
        """At the quality ceiling (1.2) and equal recency: 1.2 * 1.0 against 1.0 * 1.1."""
        for age_days in (0.0, 5.0, 100.0):
            five = shipped_score(age_days=age_days, importance=5, config=NARROW)
            # Enough helpful reports to reach the 1.2 ceiling (within 1e-3).
            three = shipped_score(
                age_days=age_days, importance=3, counts=(10_000, 0, 0), config=NARROW
            )
            assert three > five
            assert three / five == pytest.approx(1.2 / 1.1, rel=1e-3)

    def test_the_wide_factor_is_what_let_importance_outweigh_peers(self) -> None:
        five = shipped_score(age_days=0.0, importance=5, config=WIDE)
        three = shipped_score(age_days=0.0, importance=3, counts=(10_000, 0, 0), config=WIDE)
        assert five > three  # 1.0 against 0.8 * 1.2

    def test_narrow_is_the_default(self) -> None:
        assert CONFIG.narrow_importance is True
