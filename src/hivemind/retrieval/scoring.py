"""Decay-aware entry scoring and feedback quality (SPEC §4.2, §6.4). Pure: "now" and the counts are
arguments."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

# Feedback quality defaults (ADR 0063), overridable at the service layer. Each weight is where
# quality heads when every report gives that verdict.
HELPFUL_WEIGHT = 0.2
STALE_WEIGHT = 0.4
WRONG_WEIGHT = 1.0
QUALITY_PRIOR = 2.0
QUALITY_MIN = 0.5
QUALITY_MAX = 1.2
# Ceiling on the combined upward effect of all social signals (ADR 0070).
SOCIAL_MAX = 1.2
# What a stale or wrong report counts as once a confirmation is newer than all (ADR 0106).
CONFIRMED_FLAG_SCALE = 0.5

# Importance factor: 0.5 + 0.1 * importance (importance in 1..5 -> 0.6..1.0).
_IMPORTANCE_BASE = 0.5
_IMPORTANCE_STEP = 0.1
# Narrow importance factor (ADR 0088): 1 + 0.05 * (importance - 3) -> 0.9..1.1.
_NARROW_IMPORTANCE_STEP = 0.05
_NARROW_IMPORTANCE_MID = 3


def feedback_quality(
    helpful: int,
    stale: int,
    wrong: int,
    *,
    helpful_weight: float = HELPFUL_WEIGHT,
    stale_weight: float = STALE_WEIGHT,
    wrong_weight: float = WRONG_WEIGHT,
    prior: float = QUALITY_PRIOR,
    min_quality: float = QUALITY_MIN,
    max_quality: float = QUALITY_MAX,
    flag_scale: float = 1.0,
) -> float:
    """Bounded quality multiplier from feedback (ADR 0063), ``n = helpful + stale + wrong``::

        quality = clamp(1 + (wh*h - ws*s - ww*w) / (n + prior), min, max)

    A share, not a running total, so helpful reports cannot be banked against later wrong ones.
    No feedback gives exactly 1.0. ``flag_scale`` scales each stale/wrong report, in the share
    and in ``n``: ``CONFIRMED_FLAG_SCALE`` when a confirmation is newer than every flag (ADR 0106).
    """
    s, w = stale * flag_scale, wrong * flag_scale
    n = helpful + s + w
    score = 1.0 + (helpful * helpful_weight - s * stale_weight - w * wrong_weight) / (n + prior)
    return max(min_quality, min(max_quality, score))


@dataclass(frozen=True, slots=True)
class ScoreParts:
    """The factors of one entry's score (ADR 0137): ``score`` is their product, in this order."""

    fused: float
    importance: int
    importance_factor: float
    age_days: float
    half_life_days: float
    decay: float
    recency_floor: float | None
    recency_factor: float
    quality: float
    social_max: float | None
    social_factor: float
    score: float

    @property
    def floor_applied(self) -> bool:
        return self.recency_factor > self.decay

    @property
    def cap_applied(self) -> bool:
        return self.social_factor < self.quality


def score_parts(
    fused: float,
    importance: int,
    occurred_at: datetime,
    quality: float,
    now: datetime,
    half_life_days: float,
    *,
    recency_floor: float | None = None,
    social_max: float | None = None,
    narrow_importance: bool,
) -> ScoreParts:
    """Decay-aware final score for one entry (SPEC §6.4), with the factors it multiplies.

    final = fused
          * (0.5 + 0.1 * importance)     # or 1 + 0.05 * (importance - 3), see narrow_importance
          * max(recency_floor, 0.5 ** (age_days / half_life_days))
          * min(social_max, quality)

    ``age_days`` runs from ``occurred_at`` (the memory date), clamped at zero for the future.
    ``recency_floor`` (ADR 0022) bounds the recency factor; ``None`` is unbounded and
    ``SearchConfig`` validates and ships 0.8. ``social_max`` (ADR 0070) caps only the upward
    effect of ``quality``; ``None`` is no cap. ``narrow_importance`` (ADR 0088) selects the
    1 + 0.05 * (importance - 3) form; it is required so a caller states its form.
    """
    age_days = max(0.0, (now - occurred_at).total_seconds() / 86_400.0)
    if narrow_importance:
        importance_factor = 1.0 + _NARROW_IMPORTANCE_STEP * (importance - _NARROW_IMPORTANCE_MID)
    else:
        importance_factor = _IMPORTANCE_BASE + _IMPORTANCE_STEP * importance
    # exp/log, not ``0.5 ** x``, which has a complex-number branch.
    decay = math.exp(math.log(0.5) * (age_days / half_life_days))
    recency = decay if recency_floor is None else max(recency_floor, decay)
    social = quality if social_max is None else min(social_max, quality)
    return ScoreParts(
        fused=fused,
        importance=importance,
        importance_factor=importance_factor,
        age_days=age_days,
        half_life_days=half_life_days,
        decay=decay,
        recency_floor=recency_floor,
        recency_factor=recency,
        quality=quality,
        social_max=social_max,
        social_factor=social,
        score=fused * importance_factor * recency * social,
    )


def entry_score(
    fused: float,
    importance: int,
    occurred_at: datetime,
    quality: float,
    now: datetime,
    half_life_days: float,
    *,
    recency_floor: float | None = None,
    social_max: float | None = None,
    narrow_importance: bool,
) -> float:
    """The final score of ``score_parts``: the one formula ranking uses."""
    return score_parts(
        fused,
        importance,
        occurred_at,
        quality,
        now,
        half_life_days,
        recency_floor=recency_floor,
        social_max=social_max,
        narrow_importance=narrow_importance,
    ).score
