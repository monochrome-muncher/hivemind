"""Social scenarios S1-S4 as baselines (ADR 0070).

They pin today's direction and arithmetic through ``feedback_quality`` and
``entry_score`` with the shipped config, not a quality gate. Each docstring
names the later change meant to flip the outcome; that change edits
``EXPECTED_WINNER`` in ``tests/eval/social.py`` on purpose.

Scores include the importance factor and the recency factor. In S1-S3 both
candidates have importance 3, which the narrowed factor (ADR 0088) leaves at
1.0, and sit past the recency floor, so each score is the floor (0.8) times
the social factor.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.eval.social import (
    CONFIG,
    EXPECTED_WINNER,
    S1,
    S2,
    S3,
    S4,
    S5,
    SCENARIOS,
    fused_at,
    score,
    winner,
)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_the_winner_today(scenario) -> None:
    """Each scenario's winner matches EXPECTED_WINNER (flip it deliberately)."""
    assert winner(scenario) == EXPECTED_WINNER[scenario.id]


def test_s1_old_favourite_beats_a_newer_unlinked_correction() -> None:
    """S1: both are past the recency floor, so 3 helpful on the 400-day entry
    decide it: 0.8 x 1.12 = 0.896 vs 0.80. Meant to flip with supersession
    of the older version."""
    assert score(S1.first) == pytest.approx(0.896)
    assert score(S1.second) == pytest.approx(0.80)


def test_s2_a_recheck_yesterday_barely_separates_old_facts() -> None:
    """S2: one helpful, however recent, is worth 1.0667 on a two-year-old fact:
    0.853 vs 0.80. The confirmation date does not restart the recency clock
    (ADR 0103)."""
    first = score(S2.first)
    assert first == pytest.approx(0.8 * (1 + 0.2 / 3))
    assert round(first, 3) == 0.853
    assert score(S2.second) == pytest.approx(0.80)


def test_s3_a_fresh_stale_does_not_sink_an_entry_below_an_unrated_one() -> None:
    """S3: 4 helpful and 1 fresh stale still leave quality at 1.057: 0.846 vs
    0.80, so the flagged entry wins. Meant to flip with the reserve 'helpful
    evaporation' change."""
    first = score(S3.first)
    assert first == pytest.approx(0.8 * (1 + 0.4 / 7))
    assert round(first, 3) == 0.846
    assert score(S3.second) == pytest.approx(0.80)


def test_s4_importance_5_at_rank_16_loses_to_importance_3_at_rank_1() -> None:
    """S4: fused 0.013158 (16th in both streams) x 1.10 = 0.014474 vs 0.016393
    (1st in both) x 1.00, so the exact match wins. The wide importance factor
    ADR 0088 replaced gave the loose match the win, 0.013158 vs 0.013115."""
    loose = fused_at(16, 16)
    exact = fused_at(1, 1)
    assert loose == pytest.approx(0.013158, abs=5e-7)
    assert loose * 1.10 == pytest.approx(0.014474, abs=5e-7)
    assert exact == pytest.approx(0.016393, abs=5e-7)
    # Both are 5 days old, so recency is the same factor and the scores
    # compare as fused x importance factor.
    assert score(S4.second) > score(S4.first)
    assert score(S4.second) / score(S4.first) == pytest.approx(exact / (loose * 1.10))


def test_s4_flips_back_with_the_wide_importance_factor() -> None:
    """The flip comes from ADR 0088's narrow factor, not from elsewhere."""
    assert winner(S4, replace(CONFIG, narrow_importance=False)) == "important_loose_match"


def test_s5_three_confirmations_answer_one_wrong() -> None:
    """S5: one wrong, then 3 noted helpful. The confirmation is newer than the
    flag, so it counts half (ADR 0106): quality 1 + (0.6 - 0.5) / 5.5 = 1.018,
    0.815 vs 0.80. Counted in full it is 1 + (0.6 - 1.0) / 6 = 0.933, 0.747,
    and five helpful reports are needed to cancel one wrong."""
    assert score(S5.first) == pytest.approx(0.8 * (1 + 0.1 / 5.5))
    assert score(S5.second) == pytest.approx(0.80)
    full = replace(CONFIG, confirmation_halves_flags=False)
    assert score(S5.first, full) == pytest.approx(0.8 * (1 - 0.4 / 6))
    assert winner(S5, full) == "unrated_rival"
