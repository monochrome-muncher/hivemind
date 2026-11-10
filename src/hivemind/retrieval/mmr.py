"""Maximal marginal relevance (ADR 0078), used only by the opt-in ``explore`` search."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def mmr_order(
    ids: Sequence[str],
    relevance: Mapping[str, float],
    similarity: Mapping[tuple[str, str], float],
    lam: float,
) -> list[str]:
    """``ids`` re-ordered greedily by ``lam * rel - (1 - lam) * max_sim``: ``rel`` scaled to [0, 1]
    by the best, ``max_sim`` the highest similarity (floored at 0) to a picked id.
    ``similarity`` is keyed ``(a, b)`` with ``a < b``; a missing pair is 0. Ties keep ``ids``
    order; nothing is dropped."""
    remaining = list(dict.fromkeys(ids))
    top = max((relevance[i] for i in remaining), default=0.0)
    scale = top if top > 0 else 1.0
    picked: list[str] = []
    # Kept incrementally so the pass is O(n^2).
    closest = dict.fromkeys(remaining, 0.0)
    while remaining:
        best = max(
            remaining,
            key=lambda i: lam * (relevance[i] / scale) - (1.0 - lam) * closest[i],
        )
        picked.append(best)
        remaining.remove(best)
        for other in remaining:
            pair = (best, other) if best < other else (other, best)
            sim = similarity.get(pair, 0.0)
            if sim > closest[other]:
                closest[other] = sim
    return picked
