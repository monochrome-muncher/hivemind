"""Unit tests for the retrieval quality metrics (ROADMAP §1.1).

Seam under test: the pure metrics in ``hivemind.retrieval.eval`` —
hit@k, MRR (reciprocal rank), DCG/nDCG. These are the signals the eval
runner + CI gate watch, so their correctness is pinned against
hand-computed values (no I/O: ranked ids + a relevance map only).
"""

from __future__ import annotations

import math

from hivemind.retrieval.eval import (
    aggregate_report,
    dcg_at_k,
    evaluate_query,
    hit_at_k,
    ndcg_at_k,
    reciprocal_rank,
)

# A fixed ranking + the set of relevant entries.
RANKED = ["a", "b", "c", "d", "e"]
RELEVANT = {"c": 1, "e": 1}  # c at position 3, e at position 5


def test_hit_at_k_positive() -> None:
    # 'c' (relevant) is in the top-3.
    assert hit_at_k(RANKED, RELEVANT, 3) is True


def test_hit_at_k_negative() -> None:
    # No relevant entry ('c' is 3rd, 'e' is 5th) in the top-1.
    assert hit_at_k(RANKED, RELEVANT, 1) is False


def test_hit_at_k_zero_is_false() -> None:
    assert hit_at_k(RANKED, RELEVANT, 0) is False


def test_hit_at_k_ignores_non_relevant() -> None:
    # Only 'a' is relevant; top-1 is 'a' -> hit.
    assert hit_at_k(RANKED, {"a": 1}, 1) is True
    assert hit_at_k(RANKED, {"z": 1}, 5) is False  # 'z' never ranked


def test_reciprocal_rank_first_position() -> None:
    # The relevant entry is 1st -> 1/1.
    assert reciprocal_rank(RANKED, {"a": 1}) == 1.0


def test_reciprocal_rank_second_position() -> None:
    # The relevant entry is 3rd ('c') -> 1/3.
    assert reciprocal_rank(RANKED, {"c": 1}) == 1 / 3


def test_reciprocal_rank_none_relevant() -> None:
    # No relevant entry is ranked -> 0.0.
    assert reciprocal_rank(RANKED, {"z": 1}) == 0.0


def test_dcg_at_k_known_value() -> None:
    # 'c' at position 3 (1/log2(4)=0.5) + 'e' at position 5 (1/log2(6)).
    expected = 1 / math.log2(4) + 1 / math.log2(6)  # 0.5 + 0.386853...
    assert dcg_at_k(RANKED, RELEVANT, 5) == expected


def test_dcg_at_k_truncates_to_k() -> None:
    # Only 'c' (position 3) is in the top-3; 'e' (position 5) is cut.
    assert dcg_at_k(RANKED, RELEVANT, 3) == 1 / 2


def test_ndcg_perfect_ranking_is_one() -> None:
    # All relevant entries at the very top -> nDCG = 1.0.
    assert ndcg_at_k(["c", "e", "a", "b", "d"], RELEVANT, 5) == 1.0


def test_ndcg_no_relevant_is_zero() -> None:
    assert ndcg_at_k(RANKED, {"z": 1}, 5) == 0.0


def test_ndcg_partial_is_between_zero_and_one() -> None:
    # c and e are not at the top -> nDCG < 1 but > 0.
    value = ndcg_at_k(RANKED, RELEVANT, 5)
    assert 0.0 < value < 1.0


def test_evaluate_query_combines_metrics() -> None:
    result = evaluate_query(RANKED, RELEVANT, 5)
    assert result["hit_at_k"] is True
    assert result["reciprocal_rank"] == 1 / 3  # 'c' is 3rd
    assert 0.0 < result["ndcg_at_k"] < 1.0


def test_aggregate_report_averages_over_queries() -> None:
    # Two queries: one perfect (rank 1), one with 'c' at rank 3.
    per_query = {
        "q1": evaluate_query(RANKED, {"a": 1}, 5),  # 'a' at rank 1
        "q2": evaluate_query(RANKED, {"c": 1}, 5),  # 'c' at rank 3
    }
    report = aggregate_report(per_query, 5)
    assert report["queries"] == 2
    assert report["hit_at_k"] == 1.0  # both have a relevant hit in top-5
    assert report["mrr"] == (1.0 + 1 / 3) / 2
    assert report["queries"] == 2


def test_aggregate_report_empty_is_zero() -> None:
    report = aggregate_report({}, 5)
    assert report["hit_at_k"] == 0.0
    assert report["mrr"] == 0.0
    assert report["ndcg_at_5"] == 0.0