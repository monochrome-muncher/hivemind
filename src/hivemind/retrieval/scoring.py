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
    *,
    recency_floor: float | None = None,
) -> float:
    """Decay-aware final score for one entry (SPEC.md §6.4).

    final = fused
          * (0.5 + 0.1 * importance)
          * max(recency_floor, 0.5 ** (age_days / half_life_days))
          * quality

    ``age_days`` is derived from the entry's *occurrence* time (the
    "memory date"), not its ingest time, so a backdated entry decays
    from when the observation happened. Future occurrence times are
    clamped to age zero.

    ``recency_floor`` (ADR 0022) bounds the one factor in the product
    that is unbounded below. With a floor ``f`` in ``(0, 1]`` the
    recency factor spans at most ``1/f`` across any candidate list
    instead of ``2 ** (age spread / half_life)``. Once ``1/f`` is
    narrower than the fused RRF range (``2(k+20)/(k+1)`` = 2.6230x at
    the §6.2 defaults, i.e. ``f > 0.381``), match quality is the sort
    key and recency the tie-break — SPEC §6.4's stated intent. The
    service ships ``f = 0.8`` (``SearchConfig.recency_floor``); this
    parameter keeps ``None`` — the pre-ADR-0022 unbounded form — as its
    own default, because the value is a configuration decision and this
    stays a pure function of the numbers it is handed. ``SearchConfig``
    validates it; this function does not.
    """
    age_days = (now - occurred_at).total_seconds() / 86_400.0
    if age_days < 0:
        age_days = 0.0
    importance_factor = _IMPORTANCE_BASE + _IMPORTANCE_STEP * importance
    # Half-life decay: 0.5 ** (age / half_life). Computed via exp/log so
    # the result is a plain float (no complex-number branch).
    recency = math.exp(math.log(0.5) * (age_days / half_life_days))
    if recency_floor is not None and recency_floor > recency:
        recency = recency_floor
    return fused * importance_factor * recency * quality
