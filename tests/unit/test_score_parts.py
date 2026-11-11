"""``score_parts``: the factors of an entry's score as ``entry_score`` multiplies them (ADR 0137).

Hand-computed like ``test_scoring.py``; ``entry_score`` is the product of these parts."""

from datetime import UTC, datetime, timedelta

import pytest

from hivemind.retrieval.scoring import ScoreParts, entry_score, score_parts

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def parts(**overrides: object) -> ScoreParts:
    args: dict[str, object] = {
        "fused": 0.02,
        "importance": 5,
        "occurred_at": NOW - timedelta(days=30),
        "quality": 1.5,
        "now": NOW,
        "half_life_days": 30.0,
        "recency_floor": 0.8,
        "social_max": 1.2,
        "narrow_importance": True,
    }
    return score_parts(**{**args, **overrides})  # type: ignore[arg-type]


def test_every_factor_is_reported_as_computed() -> None:
    p = parts()
    assert p.fused == 0.02
    assert p.importance == 5
    assert p.importance_factor == pytest.approx(1.1)
    assert p.age_days == pytest.approx(30.0)
    assert p.half_life_days == 30.0
    assert p.decay == pytest.approx(0.5)
    assert p.recency_floor == 0.8
    assert p.recency_factor == pytest.approx(0.8)
    assert p.quality == 1.5
    assert p.social_max == 1.2
    assert p.social_factor == pytest.approx(1.2)
    assert p.score == pytest.approx(0.02 * 1.1 * 0.8 * 1.2)


def test_the_floor_and_the_cap_report_whether_they_bound() -> None:
    bound = parts()
    assert bound.floor_applied and bound.cap_applied
    free = parts(occurred_at=NOW, quality=1.0)
    assert not free.floor_applied and not free.cap_applied
    assert free.recency_factor == 1.0 and free.social_factor == 1.0


def test_no_floor_and_no_cap_leave_the_raw_factors() -> None:
    p = parts(recency_floor=None, social_max=None)
    assert p.recency_factor == pytest.approx(0.5)
    assert p.social_factor == 1.5
    assert not p.floor_applied and not p.cap_applied


def test_a_future_memory_date_has_age_zero() -> None:
    assert parts(occurred_at=NOW + timedelta(days=3)).age_days == 0.0


def test_the_wide_importance_form() -> None:
    assert parts(narrow_importance=False, importance=1).importance_factor == pytest.approx(0.6)


@pytest.mark.parametrize("importance", [1, 3, 5])
@pytest.mark.parametrize("age", [0, 4, 45, 400])
@pytest.mark.parametrize("quality", [0.5, 1.0, 1.15, 1.2])
@pytest.mark.parametrize("narrow", [True, False])
def test_the_parts_multiply_to_the_score_entry_score_returns(
    importance: int, age: int, quality: float, narrow: bool
) -> None:
    kwargs = {
        "importance": importance,
        "occurred_at": NOW - timedelta(days=age),
        "quality": quality,
        "narrow_importance": narrow,
    }
    p = parts(**kwargs)
    assert p.score == p.fused * p.importance_factor * p.recency_factor * p.social_factor
    assert p.score == entry_score(
        0.02,
        importance,
        NOW - timedelta(days=age),
        quality,
        NOW,
        30.0,
        recency_floor=0.8,
        social_max=1.2,
        narrow_importance=narrow,
    )
