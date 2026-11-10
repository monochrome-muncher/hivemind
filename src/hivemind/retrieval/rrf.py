"""Reciprocal Rank Fusion (Cormack et al., 2009; SPEC §6.2, ADR 0006):

    fused(d) = sum_i  w_i / (k + rank_i(d))

with ``rank_i`` the 1-based rank in list i; d counts only in lists where it appears. Ranks, not
scores, because keyword rank and vector distance are incommensurable.
"""

from __future__ import annotations

from collections.abc import Sequence


def rrf_fuse(
    ranked_lists: Sequence[Sequence[str]],
    k: int = 60,
    weights: Sequence[float] | None = None,
) -> dict[str, float]:
    """Fuse ranked ID lists (best first) into ``{id: fused score}``, higher is better.

    A duplicate within a list counts at each of its ranks. ``k`` is the damping constant;
    ``weights`` default to 1.0 and must match ``len(ranked_lists)``.
    """
    if weights is None:
        weights = [1.0] * len(ranked_lists)
    if len(weights) != len(ranked_lists):
        raise ValueError(f"got {len(weights)} weights for {len(ranked_lists)} ranked lists")

    fused: dict[str, float] = {}
    for weight, ranked in zip(weights, ranked_lists, strict=True):
        for rank_0, entry_id in enumerate(ranked):
            rank_1 = rank_0 + 1
            fused[entry_id] = fused.get(entry_id, 0.0) + weight / (k + rank_1)
    return fused
