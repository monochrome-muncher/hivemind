"""Decay-aware entry scoring and feedback-quality computation.

Both functions are pure and parameterized by their time/quality inputs
so callers (and tests) pin down exactly which "now" and which feedback
counts are being used. Formulas follow SPEC.md §4.2 and §6.4.
"""

from __future__ import annotations

import math
from datetime import datetime

# Weights for the feedback quality formula (SPEC.md §4.2). Defaults are
# config-overridable at the service layer; the pure function takes them
# explicitly so it has no hidden state.
HELPFUL_WEIGHT = 0.05
STALE_WEIGHT = 0.10
WRONG_WEIGHT = 0.25
QUALITY_MIN = 0.5
QUALITY_MAX = 1.2

# Importance factor: 0.5 + 0.1 * importance (importance in 1..5 -> 0.6..1.0).
_IMPORTANCE_BASE = 0.5
_IMPORTANCE_STEP = 0.1


def feedback_quality(
    helpful: int,
    stale: int,
    wrong: int,
    *,
    helpful_weight: float = HELPFUL_WEIGHT,
    stale_weight: float = STALE_WEIGHT,
    wrong_weight: float = WRONG_WEIGHT,
    min_quality: float = QUALITY_MIN,
    max_quality: float = QUALITY_MAX,
) -> float:
    """Bounded quality multiplier derived from an entry's feedback.

    No feedback -> 1.0 (neutral). The result is clamped to
    [min_quality, max_quality] so a single entry with extreme feedback
    cannot vanish or dominate.
    """
    score = 1.0 + helpful * helpful_weight - stale * stale_weight - wrong * wrong_weight
    return max(min_quality, min(max_quality, score))


def entry_score(
    fused: float,
    importance: int,
    occurred_at: datetime,
    quality: float,
    now: datetime,
    half_life_days: float,
) -> float:
    """Decay-aware final score for one entry (SPEC.md §6.4).

    final = fused
          * (0.5 + 0.1 * importance)
          * 0.5 ** (age_days / half_life_days)
          * quality

    ``age_days`` is derived from the entry's *occurrence* time (the
    "memory date"), not its ingest time, so a backdated entry decays
    from when the observation happened. Future occurrence times are
    clamped to age zero.
    """
    age_days = (now - occurred_at).total_seconds() / 86_400.0
    if age_days < 0:
        age_days = 0.0
    importance_factor = _IMPORTANCE_BASE + _IMPORTANCE_STEP * importance
    # Half-life decay: 0.5 ** (age / half_life). Computed via exp/log so
    # the result is a plain float (no complex-number branch).
    recency = math.exp(math.log(0.5) * (age_days / half_life_days))
    return fused * importance_factor * recency * quality
