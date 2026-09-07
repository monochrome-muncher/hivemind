"""Unit tests for the decay-aware rescore and feedback quality formulas.

Formulas (SPEC.md §6.4, §4.2):

    final = fused
          * (0.5 + 0.1 * importance)
          * 0.5 ** (age_days / half_life_days)
          * quality

    quality = clamp(1.0 + 0.05*h - 0.10*s - 0.25*w, 0.5, 1.2)
"""

from datetime import UTC, datetime, timedelta

import pytest

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
