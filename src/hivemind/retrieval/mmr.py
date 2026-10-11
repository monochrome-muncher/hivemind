"""Maximal marginal relevance (ADR 0078): a greedy re-ordering that trades
relevance against similarity to what is already picked.

Pure and deterministic: no I/O, no clock. Used only by an opt-in ``explore``
search; the default ranking never calls it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def mmr_order(
    ids: Sequence[str],
    relevance: Mapping[str, float],
    similarity: Mapping[tuple[str, str], float],
    lam: float,
) -> list[str]:
    """``ids`` re-ordered by MMR: each step picks the id maximising
    ``lam * rel - (1 - lam) * max_sim`` where ``rel`` is its relevance
    scaled to [0, 1] by the best relevance and ``max_sim`` its highest
    similarity (floored at 0) to an id already picked.

    ``similarity`` is keyed ``(a, b)`` with ``a < b``; a missing pair counts
    as 0 (no penalty). Ties keep the order of ``ids``. With ``lam == 1`` the
    result is ``ids`` ordered by relevance (stable), and nothing is dropped
    at any ``lam``.
    """
    remaining = list(dict.fromkeys(ids))
    top = max((relevance[i] for i in remaining), default=0.0)
    scale = top if top > 0 else 1.0
    picked: list[str] = []
    # Highest similarity of each remaining id to any picked id, kept
    # incrementally so the whole pass is O(n^2).
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
