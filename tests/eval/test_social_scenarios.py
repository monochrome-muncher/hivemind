"""Social scenarios S1-S4 as baselines (ADR 0070).

They pin today's direction and arithmetic through ``feedback_quality`` and
``entry_score`` with the shipped config, not a quality gate. Each docstring
names the later change meant to flip the outcome; that change edits
``EXPECTED_WINNER`` in ``tests/eval/social.py`` on purpose.

Scores include the importance factor and the recency factor. In S1-S3 both
candidates sit past the recency floor, so the floor (0.8) is shared and the
figures quoted in the docs, importance x social factor, are the scores
divided by it.
"""

from __future__ import annotations

import pytest

from hivemind.config import SearchConfig
from tests.eval.social import (
    EXPECTED_WINNER,
    S1,
    S2,
    S3,
    S4,
    SCENARIOS,
    fused_at,
    score,
    winner,
)

FLOOR = SearchConfig().recency_floor
assert FLOOR is not None


def _relative(scenario) -> tuple[float, float]:
    """Both scores with the shared recency floor divided out (S1-S3)."""
    return score(scenario.first) / FLOOR, score(scenario.second) / FLOOR


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_the_winner_today(scenario) -> None:
    """Each scenario's winner matches EXPECTED_WINNER (flip it deliberately)."""
    assert winner(scenario) == EXPECTED_WINNER[scenario.id]


def test_s1_old_favourite_beats_a_newer_unlinked_correction() -> None:
    """S1: both are past the recency floor, so 3 helpful on the 400-day entry
    decide it: 0.8 x 1.12 = 0.896 vs 0.80. Meant to flip with version
    grouping / supersession."""
    first, second = _relative(S1)
    assert first == pytest.approx(0.896)
    assert second == pytest.approx(0.80)


def test_s2_a_recheck_yesterday_barely_separates_old_facts() -> None:
    """S2: one helpful, however recent, is worth 1.0667 on a two-year-old fact:
    0.853 vs 0.80. Meant to become clear with the confirmation date as the
    recency clock."""
    first, second = _relative(S2)
    assert first == pytest.approx(0.8 * (1 + 0.2 / 3))
    assert round(first, 3) == 0.853
    assert second == pytest.approx(0.80)


def test_s3_a_fresh_stale_does_not_sink_an_entry_below_an_unrated_one() -> None:
    """S3: 4 helpful and 1 fresh stale still leave quality at 1.057: 0.846 vs
    0.80, so the flagged entry wins. Meant to flip with the reserve 'helpful
    evaporation' change."""
    first, second = _relative(S3)
    assert first == pytest.approx(0.8 * (1 + 0.4 / 7))
    assert round(first, 3) == 0.846
    assert second == pytest.approx(0.80)


def test_s4_importance_5_at_rank_16_beats_importance_3_at_rank_1() -> None:
    """S4: fused 0.013158 (16th in both streams) x 1.0 vs 0.016393 (1st in both)
    x 0.8 = 0.013115, so the loose match wins on importance alone. Meant to
    flip with a narrowed importance factor."""
    loose = fused_at(16, 16)
    exact = fused_at(1, 1)
    assert loose == pytest.approx(0.013158, abs=5e-7)
    assert exact * 0.8 == pytest.approx(0.013115, abs=5e-7)
    # Both are 5 days old, so recency is the same factor and the scores
    # compare as fused x importance factor.
    assert score(S4.first) > score(S4.second)
    assert score(S4.first) / score(S4.second) == pytest.approx(loose / (exact * 0.8))
