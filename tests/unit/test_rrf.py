"""Unit tests for reciprocal rank fusion (RRF).

Expected values are hand-computed from the RRF definition:

    fused(d) = sum_i  w_i / (k + rank_i(d))

where rank is 1-based and d must appear in list i to contribute.
"""

import math

from hivemind.retrieval.rrf import rrf_fuse


def test_fusion_of_two_ranked_lists_matches_hand_computed_scores():
    a_ids, b_ids = ["a", "b", "c"], ["c", "a", "d"]
    fused = rrf_fuse([a_ids, b_ids], k=60, weights=[0.5, 0.5])

    # a: 0.5/61 + 0.5/62 ; b: 0.5/62 ; c: 0.5/63 + 0.5/61 ; d: 0.5/63
    assert fused == {
        "a": 0.5 / 61 + 0.5 / 62,
        "b": 0.5 / 62,
        "c": 0.5 / 63 + 0.5 / 61,
        "d": 0.5 / 63,
    }
    # Fused ranking: a > c > b > d
    assert sorted(fused, key=fused.get, reverse=True) == ["a", "c", "b", "d"]


def test_item_in_only_one_list_contributes_once():
    fused = rrf_fuse([["x", "y"], ["z"]], k=10, weights=[1.0, 1.0])
    assert fused["x"] == 1.0 / 11
    assert fused["y"] == 1.0 / 12
    assert fused["z"] == 1.0 / 11
    assert set(fused) == {"x", "y", "z"}


def test_weights_scale_contribution():
    fused = rrf_fuse([["a", "b"]], k=10, weights=[2.0])
    assert fused["a"] == 2.0 / 11
    assert fused["b"] == 2.0 / 12


def test_empty_input_is_empty():
    assert rrf_fuse([], k=60) == {}
    assert rrf_fuse([[], []], k=60, weights=[0.5, 0.5]) == {}


def test_duplicate_ids_in_one_list_are_allowed():
    """A list may contain the same id twice; each occurrence contributes at its own rank."""
    fused = rrf_fuse([["a", "a"]], k=10, weights=[1.0])
    assert fused["a"] == 1.0 / 11 + 1.0 / 12


def test_larger_k_damps_rank_differences():
    """With a very large k, ranks barely matter: scores flatten toward 1/k."""
    fused = rrf_fuse([["a", "b"]], k=10_000, weights=[1.0])
    assert math.isclose(fused["a"], 1.0 / 10_001, rel_tol=1e-9)
    assert fused["a"] > fused["b"]
    ratio = fused["a"] / fused["b"]
    assert ratio < 1.01  # nearly equal


def test_weights_must_match_list_count():
    import pytest

    with pytest.raises(ValueError):
        rrf_fuse([["a"]], k=60, weights=[0.5, 0.5])
