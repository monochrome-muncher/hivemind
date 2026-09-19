"""Retrieval quality metrics (ROADMAP §1.1: hit@k, MRR, nDCG).

Pure functions over a ranked list of entry ids + a per-query relevance
map, so the retrieval pipeline can be measured — not guessed at — as
the ~10 spec-default config knobs are calibrated. No I/O: these run on
the ranked ids ``SearchService`` returns and the golden expected-relevant
set, and feed the eval runner + CI gate.

Relevance is a mapping ``entry_id -> grade`` (0/1 for the golden set;
graded is supported for nDCG). A "relevant" hit is any entry with a
grade > 0.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence


def _top_k(ranked_ids: Sequence[str], k: int) -> list[str]:
    """The first ``k`` ranked ids (or fewer if the list is shorter)."""
    return list(ranked_ids[: max(k, 0)])


def hit_at_k(ranked_ids: Sequence[str], relevant: Mapping[str, int], k: int) -> bool:
    """Whether any relevant entry (grade > 0) appears in the top-``k``."""
    if k <= 0:
        return False
    relevant_ids = {eid for eid, grade in relevant.items() if grade > 0}
    return any(eid in relevant_ids for eid in _top_k(ranked_ids, k))


def reciprocal_rank(ranked_ids: Sequence[str], relevant: Mapping[str, int]) -> float:
    """The reciprocal rank of the first relevant entry (1-based).

    ``1/rank`` where rank is the 1-based position of the first
    relevant hit; ``0.0`` when no relevant entry is ranked at all. This
    is the per-query MRR summand (averaged over queries -> MRR).
    """
    relevant_ids = {eid for eid, grade in relevant.items() if grade > 0}
    for rank, eid in enumerate(ranked_ids, start=1):
        if eid in relevant_ids:
            return 1.0 / rank
    return 0.0


def dcg_at_k(ranked_ids: Sequence[str], relevant: Mapping[str, int], k: int) -> float:
    """Discounted cumulative gain at ``k`` (binary or graded relevance).

    ``DCG@k = sum_{i=1..k} grade_i / log2(i + 1)`` (0-based discount on
    the 1-based position). Entries not in ``relevant`` grade 0.
    """
    if k <= 0:
        return 0.0
    total = 0.0
    for position, eid in enumerate(_top_k(ranked_ids, k), start=1):
        grade = relevant.get(eid, 0)
        if grade > 0:
            total += grade / math.log2(position + 1)
    return total


def ndcg_at_k(ranked_ids: Sequence[str], relevant: Mapping[str, int], k: int) -> float:
    """Normalized DCG at ``k`` (1.0 = perfect ranking of the relevant).

    ``nDCG@k = DCG@k / IDCG@k``; the ideal DCG places every relevant
    entry at the top (highest grades first). ``0.0`` when nothing is
    relevant (IDCG is 0).
    """
    if k <= 0:
        return 0.0
    ideal_grades = sorted((grade for grade in relevant.values() if grade > 0), reverse=True)
    ideal = min(len(ideal_grades), k)
    ideal_dcg = sum(grade / math.log2(position + 1) for position, grade in enumerate(ideal_grades[:ideal], start=1))
    if ideal_dcg == 0.0:
        return 0.0
    return dcg_at_k(ranked_ids, relevant, k) / ideal_dcg


def evaluate_query(
    ranked_ids: Sequence[str],
    relevant: Mapping[str, int],
    k: int,
) -> dict[str, float | bool]:
    """Per-query metrics (the building block of the aggregate report)."""
    return {
        "hit_at_k": hit_at_k(ranked_ids, relevant, k),
        "reciprocal_rank": reciprocal_rank(ranked_ids, relevant),
        "ndcg_at_k": ndcg_at_k(ranked_ids, relevant, k),
    }


def aggregate_report(per_query: Mapping[str, dict[str, float | bool]], k: int) -> dict[str, float]:
    """Aggregate the per-query metrics over a query set (averaged).

    ``per_query`` maps a query (label) to its ``evaluate_query`` result.
    Reports mean ``hit@k`` (fraction of queries with a relevant hit in
    the top-``k``), mean ``MRR``, and mean ``nDCG@k`` — the three
    signals the eval runner + CI gate watch.
    """
    if not per_query:
        return {"hit_at_k": 0.0, "mrr": 0.0, f"ndcg_at_{k}": 0.0}
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