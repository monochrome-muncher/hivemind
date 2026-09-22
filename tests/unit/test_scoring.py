"""Unit tests for the decay-aware rescore and feedback quality formulas.

Formulas (SPEC.md §6.4, §4.2):

    final = fused
          * (0.5 + 0.1 * importance)
          * max(recency_floor, 0.5 ** (age_days / half_life_days))
          * quality

    quality = clamp(1.0 + 0.05*h - 0.10*s - 0.25*w, 0.5, 1.2)

The floor is ADR 0022 (shipped at 0.8, in ``SearchConfig``). The pure
function keeps ``recency_floor=None`` as its own default — "the numbers
I was handed", the pre-ADR-0022 unbounded form — so the tests below can
drive both sides of the same formula.
"""

from datetime import UTC, datetime, timedelta

import pytest

from hivemind.config import SearchConfig
from hivemind.retrieval.scoring import entry_score, feedback_quality

NOW = datetime(2026, 6, 1, tzinfo=UTC)


class TestEntryScore:
    def test_fresh_importance_three_entry(self):
        occurred = NOW  # age 0
        assert entry_score(
            fused=1.0, importance=3, occurred_at=occurred, quality=1.0, now=NOW, half_life_days=30.0
        ) == pytest.approx(0.8)

    def test_fresh_max_importance(self):
        # importance 5 -> factor 1.0, age 0 -> 1.0, quality 1.0
        assert entry_score(1.0, 5, NOW, 1.0, NOW, 30.0) == pytest.approx(1.0)

    def test_one_half_life_of_decay_halves_score(self):
        occurred = NOW - timedelta(days=30)
        assert entry_score(1.0, 5, occurred, 1.0, NOW, 30.0) == pytest.approx(0.5)

    def test_two_half_lives_quarter_score(self):
        occurred = NOW - timedelta(days=60)
        # 2.0 * (0.5 + 0.1*1) * 0.5**2 * 0.5 = 2.0 * 0.6 * 0.25 * 0.5
        assert entry_score(2.0, 1, occurred, 0.5, NOW, 30.0) == pytest.approx(0.15)

    def test_futures_are_treated_as_age_zero(self):
        occurred = NOW + timedelta(days=10)
        assert entry_score(1.0, 3, occurred, 1.0, NOW, 30.0) == pytest.approx(0.8)

    def test_quality_multiplies_score(self):
        occurred = NOW
        base = entry_score(1.0, 3, occurred, 1.0, NOW, 30.0)
        lowered = entry_score(1.0, 3, occurred, 0.5, NOW, 30.0)
        assert lowered == pytest.approx(base * 0.5)


class TestRecencyFloor:
    """ADR 0022: the lower bound on the recency factor.

    The service ships it at **0.8** (``SearchConfig.recency_floor``,
    pinned in ``test_search_service``); ``entry_score``'s own default
    stays ``None`` — the pre-ADR-0022 unbounded form — because the pure
    function is a function of the numbers it is handed and the value is
    a configuration decision. These tests pin the arithmetic of both.
    """

    def test_the_shipped_floor_keeps_recency_inside_rrfs_fused_range(self) -> None:
        """ADR 0022's condition, as arithmetic on the shipped config.

        A floor ``f`` bounds the recency factor's whole range at ``1/f``.
        Match quality can only be the sort key (and recency the
        tie-break — SPEC §6.4's stated intent) while that is narrower
        than the range the fused RRF score spans, ``2(k + candidates) /
        (k + 1)``. At the shipped values that is 1.25x against 2.6230x.
        Fixture-independent: it holds for any corpus and any age spread,
        and it fails if the floor is lowered or ``rrf_k`` raised out of
        agreement.
        """
        config = SearchConfig()
        floor = config.recency_floor
        assert floor is not None, "ADR 0022 ships a floor; None is the pre-0022 form"
        newest = entry_score(1.0, 3, NOW, 1.0, NOW, 30.0, recency_floor=floor)
        oldest = entry_score(
            1.0, 3, NOW - timedelta(days=100_000), 1.0, NOW, 30.0, recency_floor=floor
        )
        fused_range = 2 * (config.rrf_k + config.candidate_top_k) / (config.rrf_k + 1)
        assert newest / oldest == pytest.approx(1.25)
        assert newest / oldest < fused_range

    @pytest.mark.parametrize("age_days", [0.0, 1.0, 30.0, 90.0, 420.0, 500.0])
    def test_unset_floor_is_bit_for_bit_the_pre_adr_0022_term(self, age_days: float) -> None:
        """Passing no floor must be *identical* to the unbounded form, not
        merely close: an `approx` here would hide a rounding change in the
        hot loop. This is what every §4.4/§4.6 measurement is against."""
        occurred = NOW - timedelta(days=age_days)
        explicit_none = entry_score(1.0, 3, occurred, 1.0, NOW, 30.0, recency_floor=None)
        assert entry_score(1.0, 3, occurred, 1.0, NOW, 30.0) == explicit_none

    def test_floor_clamps_a_very_old_entry(self) -> None:
        """500 days is 16.7 half-lives: the unfloored factor is ~1e-5, so
        the floored score is the floor's, not the decay's."""
        occurred = NOW - timedelta(days=500)
        unfloored = entry_score(1.0, 5, occurred, 1.0, NOW, 30.0)
        floored = entry_score(1.0, 5, occurred, 1.0, NOW, 30.0, recency_floor=0.5)
        assert unfloored < 1e-4
        assert floored == pytest.approx(0.5)  # 1.0 fused * 1.0 importance * 0.5 floor

    def test_floor_never_raises_the_factor_above_an_undecayed_entry(self) -> None:
        """The floor is a lower bound, never a boost: for any floor in
        (0, 1] and any age, a floored score stays at or below the score the
        same entry would get at age zero."""
        undecayed = entry_score(1.0, 3, NOW, 1.0, NOW, 30.0)
        for floor in (0.05, 0.381, 0.5, 0.7, 0.8, 1.0):
            for age_days in (0.0, 1.0, 30.0, 500.0):
                occurred = NOW - timedelta(days=age_days)
                assert entry_score(1.0, 3, occurred, 1.0, NOW, 30.0, recency_floor=floor) <= (
                    undecayed + 1e-12
                )

    def test_a_floor_below_the_decay_factor_changes_nothing(self) -> None:
        """A young entry is above the floor, so the floor does not touch it
        — which is what makes the term a tie-break rather than a constant."""
        occurred = NOW - timedelta(days=10)  # 0.5 ** (1/3) = 0.794 > 0.5
        assert entry_score(1.0, 3, occurred, 1.0, NOW, 30.0, recency_floor=0.5) == entry_score(
            1.0, 3, occurred, 1.0, NOW, 30.0
        )

    def test_the_floor_bounds_the_recency_range_to_its_reciprocal(self) -> None:
        """Why a floor can work where `rrf_k` could not (ROADMAP §4.6): the
        recency factor's whole range collapses from unbounded to `1/floor`,
        which is a number the fused RRF range (2.62x at the §6.2 defaults)
        can beat once `floor > 1/2.62 = 0.381`."""
        floor = 0.5
        newest = entry_score(1.0, 3, NOW, 1.0, NOW, 30.0, recency_floor=floor)
        oldest = entry_score(
            1.0, 3, NOW - timedelta(days=100_000), 1.0, NOW, 30.0, recency_floor=floor
        )
        assert newest / oldest == pytest.approx(1.0 / floor)


class TestFeedbackQuality:
    def test_no_feedback_is_neutral(self):
        assert feedback_quality(helpful=0, stale=0, wrong=0) == pytest.approx(1.0)

    def test_mixed_verdicts(self):
        # 1.0 + 0.05*2 - 0.10*1 - 0.25*1 = 0.75
        assert feedback_quality(helpful=2, stale=1, wrong=1) == pytest.approx(0.75)

    def test_many_helpful_clamps_to_max(self):
        assert feedback_quality(helpful=20, stale=0, wrong=0) == pytest.approx(1.2)

    def test_many_wrong_clamps_to_min(self):
        assert feedback_quality(helpful=0, stale=0, wrong=3) == pytest.approx(0.5)

    def test_stale_uses_its_own_weight(self):
        # 1.0 - 0.10*2 = 0.8
        assert feedback_quality(helpful=0, stale=2, wrong=0) == pytest.approx(0.8)
