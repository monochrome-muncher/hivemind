"""Reciprocal Rank Fusion (RRF) over ranked result lists.

RRF definition (Cormack et al., 2009):

    fused(d) = sum_i  w_i / (k + rank_i(d))

where ``rank_i`` is the 1-based rank of d in ranked list i, and d only
contributes from lists where it appears.

RRF is chosen over score-averaging because the two retrieval streams
(keyword rank and vector cosine distance) produce incommensurable
scores; ranks are the only quantity comparable across them. Weights
and k are plain arguments so fusion tuning stays a config change
(see SPEC.md §6.2 and docs/adr/0006).
"""

from __future__ import annotations

from collections.abc import Sequence


def rrf_fuse(
    ranked_lists: Sequence[Sequence[str]],
    k: int = 60,
    weights: Sequence[float] | None = None,
) -> dict[str, float]:
    """Fuse one or more ranked ID lists into a single fused-score mapping.

    Args:
        ranked_lists: Each inner sequence is a list of entry IDs in
            descending relevance (rank 0-based in the input, 1-based in
            the formula). Duplicates within a list are allowed and each
            occurrence contributes at its own rank.
        k: Rank-damping constant (default 60, the classic RRF value).
        weights: Per-list fusion weights. ``None`` means uniform weights
            of 1.0. Must match ``len(ranked_lists)`` when given.

    Returns:
        Mapping of entry ID to fused score, where higher is better.
        IDs appearing in no list are absent.
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
