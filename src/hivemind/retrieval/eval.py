"""Retrieval quality metrics (ROADMAP §1.1: hit@k, MRR, nDCG).

Pure functions over ranked entry ids and a per-query relevance map ``entry_id -> grade``
(0/1 for the golden set; graded for nDCG). Relevant means grade > 0. They feed the eval
runner and CI gate.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence


def _unique(ranked_ids: Sequence[str]) -> list[str]:
    """``ranked_ids`` without repeats (first wins), so a duplicate neither earns gain twice
    (nDCG > 1) nor occupies a rank."""
    return list(dict.fromkeys(ranked_ids))


def _top_k(ranked_ids: Sequence[str], k: int) -> list[str]:
    """The first ``k`` distinct ranked ids."""
    return _unique(ranked_ids)[: max(k, 0)]


def hit_at_k(ranked_ids: Sequence[str], relevant: Mapping[str, int], k: int) -> bool:
    """Whether any relevant entry (grade > 0) appears in the top-``k``."""
    if k <= 0:
        return False
    relevant_ids = {eid for eid, grade in relevant.items() if grade > 0}
    return any(eid in relevant_ids for eid in _top_k(ranked_ids, k))


def reciprocal_rank(ranked_ids: Sequence[str], relevant: Mapping[str, int]) -> float:
    """``1/rank`` of the first relevant entry (1-based), ``0.0`` if none: the per-query MRR
    summand."""
    relevant_ids = {eid for eid, grade in relevant.items() if grade > 0}
    for rank, eid in enumerate(_unique(ranked_ids), start=1):
        if eid in relevant_ids:
            return 1.0 / rank
    return 0.0


def dcg_at_k(ranked_ids: Sequence[str], relevant: Mapping[str, int], k: int) -> float:
    """``DCG@k = sum_{i=1..k} grade_i / log2(i + 1)``; entries not in ``relevant`` grade 0."""
    if k <= 0:
        return 0.0
    total = 0.0
    for position, eid in enumerate(_top_k(ranked_ids, k), start=1):
        grade = relevant.get(eid, 0)
        if grade > 0:
            total += grade / math.log2(position + 1)
    return total


def ndcg_at_k(ranked_ids: Sequence[str], relevant: Mapping[str, int], k: int) -> float:
    """``DCG@k / IDCG@k`` (ideal: highest grades first); ``0.0`` when nothing is relevant."""
    if k <= 0:
        return 0.0
    ideal_grades = sorted((grade for grade in relevant.values() if grade > 0), reverse=True)
    ideal = min(len(ideal_grades), k)
    ideal_dcg = sum(
        grade / math.log2(position + 1)
        for position, grade in enumerate(ideal_grades[:ideal], start=1)
    )
    if ideal_dcg == 0.0:
        return 0.0
    return dcg_at_k(ranked_ids, relevant, k) / ideal_dcg


def evaluate_query(
    ranked_ids: Sequence[str],
    relevant: Mapping[str, int],
    k: int,
) -> dict[str, float | bool]:
    """Per-query metrics for ``aggregate_report``."""
    return {
        "hit_at_k": hit_at_k(ranked_ids, relevant, k),
        "reciprocal_rank": reciprocal_rank(ranked_ids, relevant),
        "ndcg_at_k": ndcg_at_k(ranked_ids, relevant, k),
    }


def aggregate_report(per_query: Mapping[str, dict[str, float | bool]], k: int) -> dict[str, float]:
    """Mean hit@k, MRR and nDCG@k over ``per_query`` (query label to ``evaluate_query`` result)."""
    if not per_query:
        return {"hit_at_k": 0.0, "mrr": 0.0, f"ndcg_at_{k}": 0.0, "queries": 0.0}
    n = len(per_query)
    hits = sum(1 for q in per_query.values() if q["hit_at_k"])
    mrr = sum(q["reciprocal_rank"] for q in per_query.values()) / n
    ndcg = sum(q["ndcg_at_k"] for q in per_query.values()) / n
    return {
        "hit_at_k": hits / n,
        "mrr": mrr,
        f"ndcg_at_{k}": ndcg,
        "queries": float(n),
    }
