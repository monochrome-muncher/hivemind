"""Decay-aware entry scoring and feedback-quality computation.

Both functions are pure and parameterized by their time/quality inputs
so callers (and tests) pin down exactly which "now" and which feedback
counts are being used. Formulas follow SPEC.md §4.2 and §6.4.
"""

from __future__ import annotations

import math
from datetime import datetime

# Constants for the feedback quality formula (SPEC.md §4.2, ADR 0063).
# Defaults are config-overridable at the service layer; the pure function
# takes them explicitly so it has no hidden state. Each weight is where
# quality heads when every report gives that verdict (before the prior).
HELPFUL_WEIGHT = 0.2
STALE_WEIGHT = 0.4
WRONG_WEIGHT = 1.0
QUALITY_PRIOR = 2.0
QUALITY_MIN = 0.5
QUALITY_MAX = 1.2
# Ceiling on the combined upward effect of all social signals (ADR 0070).
SOCIAL_MAX = 1.2

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
) -> float:
    """Bounded quality multiplier derived from an entry's feedback.

    Consensus share (ADR 0063), with ``n = helpful + stale + wrong``::

        quality = clamp(1 + (wh*h - ws*s - ww*w) / (n + prior), min, max)

    Each weight is where quality heads when every report gives that
    verdict (helpful 1.2, stale 0.6, wrong 0 with the defaults, the
    last clamped to ``min_quality``). ``prior`` counts imaginary neutral
    reports, so one report cannot swing the result far and more reports
    move it toward the weight of the majority verdict. The sum is a
    share, not a running total, so helpful reports cannot be banked to
    cancel later wrong ones. No feedback -> exactly 1.0 (neutral). The
    result is clamped to [min_quality, max_quality].
    """
    n = helpful + stale + wrong
    score = 1.0 + (helpful * helpful_weight - stale * stale_weight - wrong * wrong_weight) / (
        n + prior
    )
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
    social_max: float | None = None,
    confirmed_at: datetime | None = None,
    narrow_importance: bool = False,
) -> float:
    """Decay-aware final score for one entry (SPEC.md §6.4).

    final = fused
          * (0.5 + 0.1 * importance)     # or 1 + 0.05 * (importance - 3), see narrow_importance
          * max(recency_floor, 0.5 ** (age_days / half_life_days))
          * min(social_max, quality * r_conf / r_occ)

    where ``r_occ`` is the recency factor above and ``r_conf`` is the same
    factor computed from the newer of ``occurred_at`` and ``confirmed_at``
    (see below); without a confirmation the ratio is 1 and the last factor
    is ``min(social_max, quality)``.

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

    ``social_max`` (ADR 0070) caps the upward effect of the social
    signals, today only feedback ``quality``: ``social = min(social_max,
    quality)``. Penalties (``quality < 1``) pass through unchanged, and
    ``None`` means no cap. The service ships ``1.2``
    (``SearchConfig.social_max``), equal to the default ``quality_max``,
    so the cap changes no ranking at the shipped defaults.

    ``confirmed_at`` (ADR 0071) is the entry's "last confirmed" date
    (``FeedbackSignals.last_confirmed_at``), passed only when the service's
    ``recency_from_confirmation`` setting is on; ``None`` is bit-identical
    to the form above. A confirmation newer than ``occurred_at`` restarts
    the recency clock for the upside only: the social factor becomes
    ``min(social_max, quality * r_conf / r_occ)``, so the gain is at most
    ``1 / recency_floor`` before the cap and the cap keeps it inside the
    shared social budget. A confirmation at or before ``occurred_at`` adds
    nothing. A future confirmation date clamps to age zero, like a future
    ``occurred_at``. If ``r_occ`` underflows to 0 (unbounded form, extreme
    age) the ratio is taken as 1 rather than dividing by zero. Only the
    date matters, never how many confirmations there were.

    ``narrow_importance`` (ADR 0088) swaps the importance factor for
    ``1 + 0.05 * (importance - 3)`` (0.9..1.1, one step about 1.05x), so
    one importance step stays below ``social_max``; ``False`` is
    bit-identical to the form above. The setting ``HIVEMIND_NARROW_IMPORTANCE``
    is on by default and passes ``True``; this function's own default is off.
    """
    age_days = (now - occurred_at).total_seconds() / 86_400.0
    if age_days < 0:
        age_days = 0.0
    if narrow_importance:
        importance_factor = 1.0 + _NARROW_IMPORTANCE_STEP * (importance - _NARROW_IMPORTANCE_MID)
    else:
        importance_factor = _IMPORTANCE_BASE + _IMPORTANCE_STEP * importance
    # Half-life decay: 0.5 ** (age / half_life). Computed via exp/log so
    # the result is a plain float (no complex-number branch).
    recency = math.exp(math.log(0.5) * (age_days / half_life_days))
    if recency_floor is not None and recency_floor > recency:
        recency = recency_floor
    if confirmed_at is not None and confirmed_at > occurred_at and recency > 0.0:
        conf_age = max(0.0, (now - confirmed_at).total_seconds() / 86_400.0)
        conf_recency = math.exp(math.log(0.5) * (conf_age / half_life_days))
        if recency_floor is not None and recency_floor > conf_recency:
            conf_recency = recency_floor
        # The gain is at most 1/floor; without a floor, an underflowed
        # recency must not turn the ratio into inf.
        ratio = conf_recency / recency
        if math.isfinite(ratio):
            quality = quality * ratio
    social = quality if social_max is None else min(social_max, quality)
    return fused * importance_factor * recency * social
