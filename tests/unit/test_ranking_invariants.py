"""Ranking invariants every ranking change must pass (ADR 0070).

They run against the SHIPPED ``SearchConfig()`` defaults, so a change that
moves a default, or adds a social signal outside the cap, fails here.

* I1  A newly written unrated entry under about 1.7 days old is never
      outranked by an entry 10 or more days old, at equal match and
      importance, however much feedback the old one has.
* I2  Ranking inputs are numbers and dates only: no author, agent label,
      pin, link or rater identity reaches the score.
* I3  A confirmation's gain depends only on the newest confirmation date,
      not on how many confirmations there were (ADR 0071).
* I4  A later stale or wrong report cancels a confirmation's gain
      (ADR 0071).
* I5  The upward multiplier from all social signals never exceeds
      ``social_max``; penalties are not capped.
* I6  With the narrow importance factor on (ADR 0088), one importance step
      (e.g. 5 against 3) stays below the peer cap at the default
      ``social_max``, so it does not outweigh peer confirmation at the same
      match and age.

I1 and I5 also run with ``recency_from_confirmation`` on (ADR 0071), where
a confirmation can restart the recency clock for the upside only.

With a recency profile per kind (ADR 0072) I1 holds per kind, with that
kind's half-life ``h`` in place of 30: a new unrated entry younger than
``h * log2(1 / (social_max * floor))`` is never outranked by an entry of
the same kind older than ``h * log2(1 / floor)``. I3, I4 and I5 run with a
profile on as well. "No decay" is not offered as a setting, because it would break I1; only per search via the explicit ``ignore_age`` hint (ADR 0073).
I1 also holds across kinds: a new entry of kind A under A's bound (recency
at least 0.96) is never outranked by an entry of any kind past its own
kind's floor age (at most 0.8 x 1.2 = 0.96).
"""

from __future__ import annotations

import inspect
import math
from dataclasses import replace
from datetime import datetime, timedelta

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
CONFIRMING = SearchConfig(recency_from_confirmation=True)
NARROW = SearchConfig(narrow_importance=True)
WIDE = SearchConfig(narrow_importance=False)
PROFILES = {
    "insight=180,decision=365": ((Kind.INSIGHT, 180.0), (Kind.DECISION, 365.0)),
    "insight=90,decision=180": ((Kind.INSIGHT, 90.0), (Kind.DECISION, 180.0)),
}
PROFILED = SearchConfig(recency_by_kind=PROFILES["insight=180,decision=365"])
PROFILED_CONFIRMING = replace(PROFILED, recency_from_confirmation=True)
# Plausible half-lives for any kind, from a week to ten years.
HALF_LIVES = (7.0, 30.0, 90.0, 180.0, 365.0, 1000.0, 3650.0)
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
    confirmed_ago_days: float | None = None,
    kind: Kind = Kind.FACT,
) -> float:
    """The score SearchService computes, for one entry, through the pure functions.

    ``confirmed_ago_days`` is the entry's "last confirmed" date; like the
    service it reaches the score only when ``config.recency_from_confirmation``.
    """
    quality = feedback_quality(*counts, **config.quality_kwargs())
    confirmed_at = (
        NOW - timedelta(days=confirmed_ago_days)
        if confirmed_ago_days is not None and config.recency_from_confirmation
        else None
    )
    return entry_score(
        fused,
        importance,
        NOW - timedelta(days=age_days),
        quality,
        NOW,
        config.half_life_for(kind),
        recency_floor=config.recency_floor,
        social_max=config.social_max,
        confirmed_at=confirmed_at,
        narrow_importance=config.narrow_importance,
    )


class TestI1FreshUnratedBeatsOldFavourite:
    def test_arithmetic_of_the_shipped_config(self) -> None:
        """I1: the best old entry (social_max x floor) sits below the recency
        of a 1.7-day-old entry, so raising the cap or the floor out of
        agreement fails here."""
        assert CONFIG.recency_floor is not None
        # Past ~9.7 days the floor is the old entry's recency.
        assert 0.5 ** (10.0 / CONFIG.half_life_days) < CONFIG.recency_floor
        assert CONFIG.social_max * CONFIG.recency_floor < 0.5 ** (1.7 / CONFIG.half_life_days)

    @pytest.mark.parametrize("fresh_age", FRESH_DAYS)
    @pytest.mark.parametrize("old_age", OLD_DAYS)
    @pytest.mark.parametrize("counts", FEEDBACK_MIXES)
    @pytest.mark.parametrize("config", [CONFIG, CONFIRMING], ids=["off", "confirmation-on"])
    def test_old_entry_never_outranks_a_fresh_unrated_one(
        self, fresh_age: float, old_age: float, counts: tuple[int, int, int], config: SearchConfig
    ) -> None:
        """I1: at equal fused score and importance, fresh unrated >= old, any feedback.

        With ``recency_from_confirmation`` on, the old entry is also confirmed
        a minute ago (the best case for the gain): the cap still holds it at
        0.8 x 1.2 = 0.96, below a fresh entry's recency.
        """
        for importance in (1, 3, 5):
            fresh = shipped_score(age_days=fresh_age, importance=importance, config=config)
            old = shipped_score(
                age_days=old_age,
                counts=counts,
                importance=importance,
                config=config,
                confirmed_ago_days=1.0 / 1440.0,
            )
            assert fresh >= old

    @pytest.mark.parametrize("config", [CONFIG, CONFIRMING], ids=["off", "confirmation-on"])
    async def test_service_ratio_of_an_old_favourite_stays_below_a_new_entry(
        self, config: SearchConfig
    ) -> None:
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

        hits = await _service(store, config).search("gamma rollout note", limit=10)
        by_id = {h.entry_id: h.score for h in hits}
        fused = await _fused_by_id(store, "gamma rollout note")
        assert set(by_id) == {old.id, new.id}
        assert by_id[old.id] / fused[old.id] <= by_id[new.id] / fused[new.id]


class TestI1PerKind:
    """I1 generalised (ADR 0072): each kind's own half-life, the same floor."""

    def test_the_cap_times_the_floor_is_below_one(self) -> None:
        """Needed for I1 to be satisfiable at all: an old entry at the cap must
        be below a brand-new unrated one (recency 1.0)."""
        assert CONFIG.recency_floor is not None
        assert CONFIG.social_max * CONFIG.recency_floor < 1.0

    @pytest.mark.parametrize("half_life", HALF_LIVES)
    def test_the_two_ages_scale_with_the_half_life(self, half_life: float) -> None:
        """The fresh bound h*log2(1/(cap*floor)) is below the floor age
        h*log2(1/floor), and the shipped 30-day values are about 1.7 and 9.7 days."""
        assert CONFIG.recency_floor is not None
        fresh = _fresh_bound(half_life)
        floor_age = _floor_age(half_life)
        assert 0.0 < fresh < floor_age
        assert 0.5 ** (fresh / half_life) == pytest.approx(CONFIG.social_max * CONFIG.recency_floor)
        assert 0.5 ** (floor_age / half_life) == pytest.approx(CONFIG.recency_floor)
        if half_life == 30.0:
            assert 1.7 < fresh < 1.8 and 9.6 < floor_age < 9.7

    @pytest.mark.parametrize("half_life", HALF_LIVES)
    @pytest.mark.parametrize("counts", FEEDBACK_MIXES)
    @pytest.mark.parametrize("confirming", [False, True], ids=["off", "confirmation-on"])
    def test_a_new_unrated_entry_beats_an_old_one_of_the_same_kind(
        self, half_life: float, counts: tuple[int, int, int], confirming: bool
    ) -> None:
        """For every kind of a profile that gives it ``half_life``, fresh
        unrated >= old with any feedback, at equal fused score and importance."""
        config = SearchConfig(
            recency_by_kind=((Kind.INSIGHT, half_life),), recency_from_confirmation=confirming
        )
        bound = _fresh_bound(half_life)
        for fresh_age in (0.0, 0.5 * bound, 0.999 * bound):
            for old_age in (_floor_age(half_life) * m for m in (1.0, 1.1, 3.0, 50.0)):
                for importance in (1, 3, 5):
                    fresh = shipped_score(
                        age_days=fresh_age,
                        importance=importance,
                        config=config,
                        kind=Kind.INSIGHT,
                    )
                    old = shipped_score(
                        age_days=old_age,
                        counts=counts,
                        importance=importance,
                        config=config,
                        confirmed_ago_days=1.0 / 1440.0,
                        kind=Kind.INSIGHT,
                    )
                    assert fresh >= old

    @pytest.mark.parametrize("profile", PROFILES)
    @pytest.mark.parametrize("kind", list(Kind))
    @pytest.mark.parametrize("counts", FEEDBACK_MIXES)
    def test_every_kind_of_the_configured_profiles(
        self, profile: str, kind: Kind, counts: tuple[int, int, int]
    ) -> None:
        config = replace(
            SearchConfig(recency_by_kind=PROFILES[profile]), recency_from_confirmation=True
        )
        half_life = config.half_life_for(kind)
        fresh = shipped_score(age_days=0.999 * _fresh_bound(half_life), config=config, kind=kind)
        old = shipped_score(
            age_days=_floor_age(half_life),
            counts=counts,
            config=config,
            confirmed_ago_days=1.0 / 1440.0,
            kind=kind,
        )
        assert fresh >= old

    @pytest.mark.parametrize("profile", PROFILES)
    @pytest.mark.parametrize("new_kind", list(Kind))
    @pytest.mark.parametrize("old_kind", list(Kind))
    @pytest.mark.parametrize("counts", FEEDBACK_MIXES)
    def test_across_kinds(
        self, profile: str, new_kind: Kind, old_kind: Kind, counts: tuple[int, int, int]
    ) -> None:
        """A new entry of kind A under A's bound beats any entry past its own
        kind's floor age, whatever the two kinds are."""
        config = replace(
            SearchConfig(recency_by_kind=PROFILES[profile]), recency_from_confirmation=True
        )
        fresh = shipped_score(
            age_days=0.999 * _fresh_bound(config.half_life_for(new_kind)),
            config=config,
            kind=new_kind,
        )
        old = shipped_score(
            age_days=_floor_age(config.half_life_for(old_kind)),
            counts=counts,
            config=config,
            confirmed_ago_days=1.0 / 1440.0,
            kind=old_kind,
        )
        assert fresh >= old

    async def test_service_ratio_of_an_old_favourite_stays_below_a_new_entry(self) -> None:
        """Through SearchService with a profile on: two decisions, the old one
        past its (365-day) floor age with 200 helpful reports."""
        store = MemoryStore(make_clock())
        old = await store.create_entry(
            _draft("gamma rollout note", age_days=3000, kind=Kind.DECISION), [0.1] * 4
        )
        new = await store.create_entry(
            _draft("gamma rollout note", age_days=1, kind=Kind.DECISION), [0.1] * 4
        )
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
        hits = await _service(store, PROFILED_CONFIRMING).search("gamma rollout note", limit=10)
        by_id = {h.entry_id: h.score for h in hits}
        fused = await _fused_by_id(store, "gamma rollout note")
        assert by_id[old.id] / fused[old.id] <= by_id[new.id] / fused[new.id]


class TestI2NumbersAndDatesOnly:
    def test_entry_score_takes_only_the_allowlisted_inputs(self) -> None:
        """I2: a new parameter (an author, a count of links, a rater record)
        must be added to this allowlist on purpose, in review. ``confirmed_at``
        is a date: how many confirmations there were never reaches the score."""
        assert set(inspect.signature(entry_score).parameters) == {
            "fused",
            "importance",
            "occurred_at",
            "quality",
            "now",
            "half_life_days",
            "recency_floor",
            "social_max",
            "confirmed_at",
            "narrow_importance",
        }

    async def test_the_kind_reaches_the_score_only_as_a_half_life(self) -> None:
        """I2 with a profile (ADR 0072): two kinds given the same half-life
        score alike, so the kind is nothing but a number."""
        same = SearchConfig(recency_by_kind=((Kind.FACT, 120.0), (Kind.DECISION, 120.0)))
        facts = shipped_score(age_days=77.0, config=same, kind=Kind.FACT)
        decisions = shipped_score(age_days=77.0, config=same, kind=Kind.DECISION)
        assert facts == decisions

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


class TestI3GainDependsOnlyOnTheNewestDate:
    def test_pure_gain_is_a_function_of_the_date(self) -> None:
        """I3: with the same newest date and the same quality, the score is the
        same whatever else is true; a newer date gives at least as much."""
        args = (1.0, 3, NOW - timedelta(days=400), 1.0, NOW, 30.0)
        kw = {"recency_floor": 0.8, "social_max": 3.0}
        week = entry_score(*args, **kw, confirmed_at=NOW - timedelta(days=7))
        day = entry_score(*args, **kw, confirmed_at=NOW - timedelta(days=1))
        assert day > week > entry_score(*args, **kw)

    async def test_one_confirmation_and_five_gain_alike_through_the_service(self) -> None:
        """I3 through SearchService: one confirmation and five, same newest
        date. Quality differs (1 vs 5 helpful), so the claim is about the
        gain: the score with the setting on divided by the score with it off
        is identical, under a cap high enough not to hide it."""
        for ago in (1.0 / 1440.0, 1.0, 5.0, 8.0):
            gains = []
            for confirmations in (1, 5):
                on = await _confirmed_score(
                    recency_from_confirmation=True,
                    confirmations=[ago] + [ago + 3.0] * (confirmations - 1),
                    social_max=3.0,
                )
                off = await _confirmed_score(
                    recency_from_confirmation=False,
                    confirmations=[ago] + [ago + 3.0] * (confirmations - 1),
                    social_max=3.0,
                )
                gains.append(on / off)
            assert gains[0] == pytest.approx(gains[1])
            assert gains[0] > 1.0

    async def test_at_the_shipped_cap_the_number_of_recent_confirmations_is_invisible(
        self,
    ) -> None:
        """I3: a recent confirmation puts a two-year-old entry at the cap, so
        one and five confirmations score exactly the same."""
        scores = [
            await _confirmed_score(
                recency_from_confirmation=True,
                confirmations=[1.0] + [4.0] * (n - 1),
                social_max=CONFIG.social_max,
            )
            for n in (1, 5)
        ]
        assert scores[0] == scores[1]


class TestI4ALaterFlagCancelsTheGain:
    @pytest.mark.parametrize("verdict", [Verdict.STALE, Verdict.WRONG])
    async def test_a_later_report_removes_the_confirmation_gain(self, verdict: Verdict) -> None:
        """I4 through SearchService: a stale or wrong report newer than the
        confirmation leaves the setting on and off scoring the same."""
        flagged_on = await _confirmed_score(
            recency_from_confirmation=True,
            confirmations=[2.0],
            flags=[(verdict, 1.0)],
        )
        flagged_off = await _confirmed_score(
            recency_from_confirmation=False,
            confirmations=[2.0],
            flags=[(verdict, 1.0)],
        )
        assert flagged_on == flagged_off

    async def test_an_older_report_does_not_cancel_it(self) -> None:
        """I4's converse: a flag older than the confirmation leaves the gain."""
        on = await _confirmed_score(
            recency_from_confirmation=True, confirmations=[1.0], flags=[(Verdict.STALE, 3.0)]
        )
        off = await _confirmed_score(
            recency_from_confirmation=False, confirmations=[1.0], flags=[(Verdict.STALE, 3.0)]
        )
        assert on > off

    async def test_a_tie_is_no_confirmation(self) -> None:
        on = await _confirmed_score(
            recency_from_confirmation=True, confirmations=[2.0], flags=[(Verdict.STALE, 2.0)]
        )
        off = await _confirmed_score(
            recency_from_confirmation=False, confirmations=[2.0], flags=[(Verdict.STALE, 2.0)]
        )
        assert on == off


class TestI5UpwardMultiplierIsCapped:
    @pytest.mark.parametrize("helpful", [0, 1, 2, 5, 20, 100, 1000])
    def test_social_factor_never_exceeds_social_max(self, helpful: int) -> None:
        """I5: the product of the quality term never exceeds social_max."""
        # recency is 1 at age 0, importance 3 gives 1.0 (narrow default), fused 1.0.
        score = shipped_score(age_days=0.0, counts=(helpful, 0, 0), importance=3)
        assert score <= CONFIG.social_max

    @pytest.mark.parametrize("helpful", [0, 1, 5, 1000])
    @pytest.mark.parametrize("age", [0.0, 3.0, 9.0, 10.0, 45.0, 400.0, 5000.0])
    @pytest.mark.parametrize("confirmed_ago", [0.0, 1.0 / 1440.0, 1.0, 8.0, 30.0, 399.0])
    @pytest.mark.parametrize("floor", [0.8, 0.5, None])
    def test_confirmation_never_lifts_the_multiplier_past_social_max(
        self, helpful: int, age: float, confirmed_ago: float, floor: float | None
    ) -> None:
        """I5 with the setting on: score / recency, the social multiplier, is
        at most social_max however recent the confirmation (importance 3, fused 1)."""
        if confirmed_ago > age:
            pytest.skip("a confirmation older than the memory date adds nothing")
        config = SearchConfig(recency_from_confirmation=True, recency_floor=floor)
        score = shipped_score(
            age_days=age,
            counts=(helpful, 0, 0),
            importance=3,
            config=config,
            confirmed_ago_days=confirmed_ago,
        )
        recency = max(floor or 0.0, 0.5 ** (age / config.half_life_days))
        assert score / recency <= config.social_max * (1 + 1e-12)

    def test_a_penalty_times_a_gain_can_stay_below_one(self) -> None:
        """Intended: min caps only the upside, so quality 0.667 (one wrong) x gain 1.22 is 0.81."""
        score = shipped_score(
            age_days=730.0,
            counts=(0, 0, 1),
            importance=5,
            config=CONFIRMING,
            confirmed_ago_days=1.0,
        )
        assert score / 0.8 < 1.0

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
            entry_score(1.0, 5, NOW, 0.5, NOW, 30.0, recency_floor=0.8, social_max=SOCIAL_MAX)
            == 0.5
        )

    def test_the_cap_is_a_ceiling_on_the_pure_function(self) -> None:
        """I5: with an explicit cap, any quality above it is cut to it."""
        capped = entry_score(1.0, 5, NOW, 2.0, NOW, 30.0, social_max=1.2)
        assert capped == pytest.approx(1.2)
        assert entry_score(1.0, 5, NOW, 2.0, NOW, 30.0) == pytest.approx(2.0)

    def test_the_shipped_cap_equals_the_shipped_quality_ceiling(self) -> None:
        """I5: social_max == quality_max by default, which is why the cap changes no ranking."""
        assert SOCIAL_MAX == CONFIG.social_max == CONFIG.quality_max == scoring.QUALITY_MAX


class TestProfileRespectsI3I4I5:
    """I3, I4 and I5 with a recency profile on (ADR 0072)."""

    async def test_i3_one_confirmation_and_five_gain_alike(self) -> None:
        scores = [
            await _confirmed_score(
                recency_from_confirmation=True,
                confirmations=[1.0] + [4.0] * (n - 1),
                social_max=CONFIG.social_max,
                base=PROFILED,
                kind=Kind.DECISION,
                entry_age_days=3000.0,
            )
            for n in (1, 5)
        ]
        assert scores[0] == scores[1]

    @pytest.mark.parametrize("verdict", [Verdict.STALE, Verdict.WRONG])
    async def test_i4_a_later_report_removes_the_gain(self, verdict: Verdict) -> None:
        on, off = [
            await _confirmed_score(
                recency_from_confirmation=flag,
                confirmations=[2.0],
                flags=[(verdict, 1.0)],
                base=PROFILED,
                kind=Kind.INSIGHT,
                entry_age_days=3000.0,
            )
            for flag in (True, False)
        ]
        assert on == off
        kept = await _confirmed_score(
            recency_from_confirmation=True,
            confirmations=[1.0],
            base=PROFILED,
            kind=Kind.INSIGHT,
            entry_age_days=3000.0,
        )
        plain = await _confirmed_score(
            recency_from_confirmation=False,
            confirmations=[1.0],
            base=PROFILED,
            kind=Kind.INSIGHT,
            entry_age_days=3000.0,
        )
        assert kept > plain

    @pytest.mark.parametrize("profile", PROFILES)
    @pytest.mark.parametrize("kind", list(Kind))
    @pytest.mark.parametrize("helpful", [0, 1, 5, 1000])
    @pytest.mark.parametrize("age", [0.0, 3.0, 40.0, 400.0, 5000.0])
    @pytest.mark.parametrize("confirmed_ago", [0.0, 1.0 / 1440.0, 1.0, 30.0])
    def test_i5_the_multiplier_never_exceeds_social_max(
        self, profile: str, kind: Kind, helpful: int, age: float, confirmed_ago: float
    ) -> None:
        if confirmed_ago > age:
            pytest.skip("a confirmation older than the memory date adds nothing")
        config = replace(
            SearchConfig(recency_by_kind=PROFILES[profile]), recency_from_confirmation=True
        )
        score = shipped_score(
            age_days=age,
            counts=(helpful, 0, 0),
            importance=3,
            config=config,
            confirmed_ago_days=confirmed_ago,
            kind=kind,
        )
        assert config.recency_floor is not None
        recency = max(config.recency_floor, 0.5 ** (age / config.half_life_for(kind)))
        assert score / recency <= config.social_max * (1 + 1e-12)


class TestUnsetIsToday:
    def test_no_profile_is_one_curve_for_every_kind(self) -> None:
        assert CONFIG.recency_by_kind is None
        assert {CONFIG.half_life_for(k) for k in Kind} == {CONFIG.half_life_days}

    def test_an_empty_profile_is_none(self) -> None:
        assert SearchConfig(recency_by_kind=()).recency_by_kind is None
        assert SearchConfig(recency_by_kind=()) == CONFIG

    async def test_unset_scores_are_bit_identical_to_the_flat_half_life(self) -> None:
        """Every kind, age, importance and feedback mix: the service's score
        with no profile equals entry_score called with ``half_life_days``."""
        for kind in Kind:
            for age in (0.0, 1.0, 9.0, 45.0, 400.0, 5000.0):
                for counts in FEEDBACK_MIXES:
                    quality = feedback_quality(*counts, **CONFIG.quality_kwargs())
                    flat = entry_score(
                        0.0164,
                        4,
                        NOW - timedelta(days=age),
                        quality,
                        NOW,
                        CONFIG.half_life_days,
                        recency_floor=CONFIG.recency_floor,
                        social_max=CONFIG.social_max,
                        narrow_importance=CONFIG.narrow_importance,
                    )
                    assert flat == shipped_score(
                        age_days=age, counts=counts, fused=0.0164, importance=4, kind=kind
                    )

    async def test_a_service_without_a_profile_ranks_kinds_by_age_alone(self) -> None:
        store = MemoryStore(make_clock())
        a = await store.create_entry(
            _draft("zeta cutover", age_days=100, kind=Kind.DECISION), [0.1] * 4
        )
        b = await store.create_entry(_draft("zeta cutover", age_days=100), [0.1] * 4)
        by_id = {h.entry_id: h.score for h in await _service(store).search("zeta cutover")}
        fused = await _fused_by_id(store, "zeta cutover")
        assert by_id[a.id] / fused[a.id] == pytest.approx(by_id[b.id] / fused[b.id])

    async def test_with_a_profile_an_old_decision_can_outrank_a_same_age_fact(self) -> None:
        """The point of ADR 0072, and its cost: at equal match an old decision
        ages slower than a same-age fact (the floor still bounds the gap at 1/0.8)."""
        store = MemoryStore(make_clock())
        fact = await store.create_entry(
            _draft("zeta cutover", age_days=20, kind=Kind.FACT), [0.1] * 4
        )
        decision = await store.create_entry(
            _draft("zeta cutover", age_days=20, kind=Kind.DECISION), [0.1] * 4
        )
        hits = await _service(store, PROFILED).search("zeta cutover")
        fused = await _fused_by_id(store, "zeta cutover")
        ratio = {h.entry_id: h.score / fused[h.entry_id] for h in hits}
        assert ratio[decision.id] > ratio[fact.id]
        assert ratio[decision.id] / ratio[fact.id] <= 1 / 0.8 + 1e-12


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
                        uncapped = entry_score(*args, recency_floor=CONFIG.recency_floor)
                        capped = entry_score(
                            *args, recency_floor=CONFIG.recency_floor, social_max=1.2
                        )
                        assert capped == uncapped
                        assert math.isfinite(capped)


# --- helpers -----------------------------------------------------------------


def _fresh_bound(half_life: float) -> float:
    """The age under which a new unrated entry beats any same-kind entry past
    its floor age: where ``0.5 ** (age / h)`` equals ``social_max * floor``."""
    assert CONFIG.recency_floor is not None
    return half_life * math.log2(1.0 / (CONFIG.social_max * CONFIG.recency_floor))


def _floor_age(half_life: float) -> float:
    """The age from which the recency factor is the floor."""
    assert CONFIG.recency_floor is not None
    return half_life * math.log2(1.0 / CONFIG.recency_floor)


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


async def _confirmed_score(
    *,
    recency_from_confirmation: bool,
    confirmations: list[float],
    flags: list[tuple[Verdict, float]] | None = None,
    social_max: float = CONFIG.social_max,
    entry_age_days: float = 730.0,
    base: SearchConfig = CONFIG,
    kind: Kind = Kind.INSIGHT,
) -> float:
    """The search score of one two-year-old entry whose helpful-with-a-note
    reports are dated ``confirmations`` days ago and whose stale or wrong
    reports are dated ``flags`` days ago, by different users."""
    store = MemoryStore(make_clock())
    entry = await store.create_entry(
        _draft("delta failover note", age_days=entry_age_days, kind=kind), [0.1] * 4
    )

    def when(days: float) -> datetime:
        return NOW - timedelta(days=days)

    for i, ago in enumerate(confirmations):
        await store.record_feedback(
            Feedback(
                entry.id, f"c-{i}", "a", Verdict.HELPFUL, note="re-checked", updated_at=when(ago)
            )
        )
    for i, (verdict, ago) in enumerate(flags or []):
        await store.record_feedback(
            Feedback(entry.id, f"f-{i}", "a", verdict, note="seen", updated_at=when(ago))
        )
    config = replace(
        base, recency_from_confirmation=recency_from_confirmation, social_max=social_max
    )
    hits = await _service(store, config).search("delta failover note", limit=5)
    assert [h.entry_id for h in hits] == [entry.id]
    return hits[0].score


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
