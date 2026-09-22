"""The §4.6 experiment: can `rrf_k` recover what the recency term buries?

ROADMAP §4.6 records an arithmetic mismatch: `entry_score` multiplies the
fused RRF score by `0.5 ** (age_days / half_life_days)`, and at the SPEC
§6.2 defaults the *whole* fused range is narrower than a couple of
half-lives — so age, not match quality, is the sort key. §4.4 measured
one proposed fix (gate the term on query time sense) and rejected it.

This module measures the **other** side of the same mismatch, and the
cheap one: `rrf_k`. It is a plain `SearchConfig` value — no SPEC change,
no ADR, no new code — and lowering it *widens* the fused range:

    best  = w_kw/(k+1) + w_vec/(k+1) = 1/(k+1)      (rank 1 in both streams)
    worst = w_kw/(k+20)              = 0.5/(k+20)   (rank 20 in one stream)
    range = 2(k + 20) / (k + 1)

The question, stated so it can come back "no": **does lowering `rrf_k`
recover the recall that the unconditional recency term destroys, without
touching `scoring.py`?**

Variant A (decay ON, the 30-day half-life this service ships) is held
fixed and `rrf_k` is swept over {60, 20, 10, 5}. Everything else — corpus,
queries, streams, `candidate_top_k` — is shared with the §4.4 experiment
via `tests/eval/temporal_runner.py`, so the two cannot drift apart.

**What this fixture can and cannot establish.** The *arithmetic* tests
here are fixture-independent: they are properties of `rrf_fuse` and of
the decay formula, and they hold for any corpus, synthetic or real. The
*ranking* results are not: 10 hand-written queries over 16 entries,
scored by a 4-dimension hash embedder (`tests/fakes.FakeEmbedder`), can
show a direction but cannot justify changing a shipped default. So this
module does **not** change `SearchConfig.rrf_k`, and the §1.1 gate
(`tests/eval/golden.py`, `tests/eval/test_eval_gate.py`) is untouched.

Run `uv run pytest tests/eval/test_rrf_k_experiment.py -s` to print the
measured table (reproduced in ROADMAP §4.6).
"""

from __future__ import annotations

import math
from itertools import pairwise

import pytest

from hivemind.config import SearchConfig
from hivemind.retrieval.rrf import rrf_fuse
from tests.eval.temporal import temporal_corpus, temporal_queries
from tests.eval.temporal_runner import (
    CANDIDATE_TOP_K,
    DECAY_OFF,
    DECAY_ON,
    SLICES,
    K,
    SliceReports,
    aggregate_slices,
    measure_slices,
    ranked_ids,
    render_table,
    seed_temporal_corpus,
)
from tests.fakes import make_search_config

# The sweep. 60 is today's default (SPEC §6.2); 5 is about as low as RRF
# is ever tuned in the literature.
RRF_KS = (60, 20, 10, 5)

# Decay is left ON for the whole sweep: the question is whether `rrf_k`
# can compete with the recency term, not what happens without it.
ALWAYS_ON = DECAY_ON


def fused_range(k: int, *, candidates: int = CANDIDATE_TOP_K) -> float:
    """The best-to-worst fused-score ratio across a candidate list, measured
    from ``rrf_fuse`` itself rather than from the formula in the docstring.

    Best case: an entry ranked 1 in *both* streams. Worst retained case: an
    entry ranked last in *one* stream only. With the SPEC §6.2 weights
    (0.5/0.5) this is ``2(k + candidates) / (k + 1)``.
    """
    keyword = [f"e{i}" for i in range(candidates)]
    vector = list(keyword)
    fused = rrf_fuse([keyword, vector], k=k, weights=[0.5, 0.5])
    best = fused["e0"]
    # The worst retained candidate appears in one stream only, at the last
    # retained rank — fuse a single list to get exactly that contribution.
    worst = rrf_fuse([keyword], k=k, weights=[0.5])[f"e{candidates - 1}"]
    return best / worst


def half_lives_of(ratio: float) -> float:
    """How many half-lives of age a given fused-score ratio can outrank."""
    return math.log2(ratio)


async def measure_sweep() -> dict[str, SliceReports]:
    """``{"k=60": {slice: aggregate_report}, ...}`` with decay held ON."""
    return {
        f"k={k}": aggregate_slices(await measure_slices(lambda _q: ALWAYS_ON, rrf_k=k))
        for k in RRF_KS
    }


class TestFusedRangeArithmetic:
    """Fixture-independent: properties of ``rrf_fuse`` and of the §6.4
    recency factor. These hold for any corpus, synthetic or real — they are
    the part of this experiment that does not depend on a 4-dimension hash
    embedder being a good stand-in for a real one."""

    @pytest.mark.parametrize("k", RRF_KS)
    def test_fused_range_matches_the_closed_form(self, k: int) -> None:
        """The ROADMAP §4.6 table's arithmetic, checked against the real
        fusion code rather than restated."""
        expected = 2 * (k + CANDIDATE_TOP_K) / (k + 1)
        assert fused_range(k) == pytest.approx(expected)

    def test_lowering_rrf_k_widens_the_fused_range(self) -> None:
        """The premise of the whole experiment: a lower ``k`` gives match
        quality more room to compete with age."""
        ranges = [fused_range(k) for k in RRF_KS]
        assert ranges == sorted(ranges), (
            f"expected the fused range to widen as k falls; got {ranges} for {RRF_KS}"
        )
        assert fused_range(60) == pytest.approx(2.6230, abs=1e-4)
        assert fused_range(10) == pytest.approx(5.4545, abs=1e-4)

    def test_the_rrf_k_lever_is_bounded_at_about_five_half_lives(self) -> None:
        """**The finding that decides §4.6's `rrf_k` question.**

        ``2(k + 20)/(k + 1)`` is bounded above by **40x** (its limit as
        ``k`` -> 0), so the entire `rrf_k` lever — the whole range from
        today's default down to a degenerate ``k`` nobody would ship — is
        worth at most ``log2(40)`` = ~5.3 half-lives, ~160 days at the
        30-day default.

        The fixture's ``old_exact`` entries are 420 and 380 days old: 14
        and 12.7 half-lives. No value of ``k`` can close that, and that is
        arithmetic, not a property of this corpus.
        """
        ceiling = 2 * (0 + CANDIDATE_TOP_K) / (0 + 1)
        assert ceiling == 40.0
        assert half_lives_of(ceiling) == pytest.approx(5.32, abs=0.01)

        old_exact_ages = [
            temporal_corpus()[case.relevant[0]].age_days
            for case in temporal_queries()
            if case.pattern == "old_exact"
        ]
        assert old_exact_ages, "the fixture must still carry old_exact cases"
        for age_days in old_exact_ages:
            assert age_days / DECAY_ON > half_lives_of(ceiling), (
                f"an old_exact entry at {age_days}d is within the rrf_k lever's reach; "
                "the §4.6 conclusion needs rechecking"
            )


class TestRrfKSweep:
    """Measured on the age-varied fixture, decay held ON (variant A)."""

    async def test_lowering_rrf_k_does_not_recover_the_old_exact_cases(self) -> None:
        """**The answer to the question this experiment asks: no.**

        Always-on decay scores 0.000 hit@5 on the ``old_exact`` pattern
        (§4.4). Every swept ``rrf_k`` still scores 0.000 — the relevant
        entry does not enter the top-5 at any of them. `rrf_k` is not a
        substitute for fixing the *form* of the recency term.
        """
        measured = await measure_sweep()
        for label, slices in measured.items():
            assert slices["old_exact"]["hit_at_k"] == 0.0, (
                f"{label} recovered an old_exact case — §4.6's 'rrf_k cannot "
                "reach it' conclusion needs rechecking"
            )

    async def test_the_old_exact_cases_are_recoverable_at_all(self) -> None:
        """Guards the test above from passing vacuously: with the recency
        term neutralised (and ``rrf_k`` left at its default) the same two
        queries are answered perfectly, so 0.000 above is the recency
        term's doing and not a broken fixture or a hopeless query."""
        off = aggregate_slices(await measure_slices(lambda _q: DECAY_OFF))
        assert off["old_exact"]["hit_at_k"] == 1.0
        assert off["old_exact"]["mrr"] == 1.0

    async def test_lowering_rrf_k_does_improve_aggregate_ranking(self) -> None:
        """What `rrf_k` *does* buy here, so the verdict is not overstated:
        all-query MRR rises monotonically as ``k`` falls, entirely from the
        non-temporal / timeless side of the fixture.

        Fixture-dependent — a direction, not a number to ship.
        """
        measured = await measure_sweep()
        # RRF_KS runs high-to-low, so a rising MRR means "lower k ranks
        # better" — strictly, at every step of the sweep.
        mrrs = [measured[f"k={k}"]["all"]["mrr"] for k in RRF_KS]
        assert all(later > earlier for earlier, later in pairwise(mrrs)), (
            f"expected all-query MRR to rise strictly as k falls across {RRF_KS}; got {mrrs}"
        )
        assert measured["k=5"]["non_temporal"]["mrr"] > measured["k=60"]["non_temporal"]["mrr"]

    async def test_the_currency_pair_and_temporal_slices_are_unmoved(self) -> None:
        """Where the gain does *not* come from: the slices the recency term
        actually earns its keep on are bit-identical across the whole sweep.

        `rrf_k` is not trading temporal accuracy for non-temporal recall
        here — it is not reaching those queries at all.
        """
        measured = await measure_sweep()
        for slice_name in ("temporal", "currency_pair"):
            values = {measured[f"k={k}"][slice_name]["mrr"] for k in RRF_KS}
            assert len(values) == 1, (
                f"the {slice_name} slice moved across the rrf_k sweep ({values}); "
                "§4.6's table needs re-running"
            )

    async def test_report_table_covers_every_k_and_slice(self) -> None:
        """Print the measured table and assert its shape is complete — every
        ``k`` x slice cell present, with the fixture's expected query counts.
        Shape only; the findings are asserted above."""
        measured = await measure_sweep()
        table = render_table(measured, "rrf_k")
        print("\n" + table)
        for k in RRF_KS:
            for slice_name in SLICES:
                assert f"| k={k} | {slice_name} |" in table
        counts = {name: measured["k=60"][name]["queries"] for name in SLICES}
        assert counts == {
            "non_temporal": 6.0,
            "temporal": 4.0,
            "old_exact": 2.0,
            "currency_pair": 3.0,
            "timeless": 5.0,
            "all": 10.0,
        }

    async def test_rrf_k_is_reached_at_all_by_this_harness(self) -> None:
        """A knob that changes no ranking would make every number above a
        constant — exactly the bug ADR 0021 found on the *prefix* knob. This
        asserts the sweep is not measuring one."""
        store, _entry_ids, now_clock = await seed_temporal_corpus()
        differing = [
            labelled.query
            for labelled in temporal_queries()
            if await ranked_ids(store, now_clock, labelled.query, ALWAYS_ON, rrf_k=60)
            != await ranked_ids(store, now_clock, labelled.query, ALWAYS_ON, rrf_k=5)
        ]
        assert differing, "rrf_k changed no ranking — the sweep cannot measure §4.6"


class TestSweepHarness:
    def test_make_search_config_mirrors_the_production_rrf_k(self) -> None:
        """The sweep's baseline must be the shipped default, not a fake one:
        if ``SearchConfig.rrf_k`` ever changes, the ``k=60`` row stops being
        "today's behaviour" and this says so."""
        assert make_search_config().rrf_k == SearchConfig().rrf_k == 60

    def test_the_sweep_starts_from_todays_default(self) -> None:
        assert RRF_KS[0] == SearchConfig().rrf_k
        assert K == 5
