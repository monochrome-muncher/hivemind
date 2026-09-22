"""The §4.4 and §4.6-(a) experiments on the age-varied fixture.

Two questions, one fixture. **§4.4:** should the SPEC §6.4 recency term
stay unconditional, or be *gated* on the query's time sense? (Measured,
rejected — variants A/B/C below.) **§4.6 direction (a):** should the
recency factor be *floored*, so the one unbounded factor in
``entry_score``'s product can no longer dominate RRF's compressed fused
range? (Variant D, the floor sweep at the bottom of this module.)

Seam under test: the same one the §1.1 gate uses — ``SearchService`` over a
``MemoryStore``, measured with ``hivemind.retrieval.eval`` — but on the
age-varied fixture (``tests/eval/temporal.py``) instead of the golden set,
because the golden set seeds every entry at one timestamp and therefore
cannot measure decay at all.

The §4.4 variants are realised through ``SearchConfig.half_life_days``
alone — no production code change, which is the point of running the
experiment *before* deciding:

* **A. always-on** — today's behaviour, a 30-day half-life on every query.
* **B. off** — a half-life so long that ``0.5 ** (age / half_life)`` is
  exactly ``1.0`` in float64 for every entry in the corpus, so the recency
  factor is a constant and cancels out of the ranking entirely.
* **C. gated** — A's config for queries labelled temporal, B's for the
  rest. This is an **oracle** gate: it reads the fixture's hand-written
  ``temporal`` label, not a classifier. Its numbers are the ceiling a
  perfect query-intent classifier could reach, not what a keyword-list
  heuristic would deliver.
* **D. floored** — A's half-life, with ``SearchConfig.recency_floor``
  bounding the recency factor below (``max(floor, 0.5 ** (age/hl))``).
  A and B are its two limits: ``floor -> 0`` is A, ``floor = 1`` is B.
  Unlike A/B/C this needed a knob at the ``entry_score`` seam, but the
  knob's default is ``None`` = off, so the shipped pipeline is A.

The fused RRF score is identical across every variant (same store, same
streams, same fusion), so every difference between them is attributable
to the recency factor and nothing else. That is the asymmetry that makes
this sweep more trustworthy than the ``rrf_k`` one: A, B and D are a
*bounded rebalance of the same scores from the same embedder*, and the
arithmetic (below) predicts the direction and roughly where the
transition sits before a single query is run.

Reported two ways: by the **query's time sense** (temporal vs
non-temporal — the axis §4.4 proposes gating on) and by the **corpus
competition pattern** the query lands in. The second split is what
actually explains the first.

This module does not modify ``tests/eval/golden.py`` or the pinned §1.1
gate; those keep running unchanged beside it.

The seeding and ranking machinery is shared with the §4.6 ``rrf_k``
sweep (``tests/eval/temporal_runner.py``), so the two experiments cannot
drift apart on how the fixture is loaded or scored.

Run ``uv run pytest tests/eval/test_decay_experiment.py -s`` to print the
measured tables: the §4.4 A/B/C table (reproduced in ROADMAP §4.4 and in
``.superpowers/sdd/tier4-provenance-and-decay/task-2-report.md``) and the
§4.6-(a) floor table (reproduced in ROADMAP §4.6).
"""

from __future__ import annotations

import pytest

from hivemind.domain.entry import EntryDraft, Kind
from hivemind.memstore import MemoryStore
from hivemind.services.governance import WriteService
from tests.eval.golden import golden_corpus, golden_queries
from tests.eval.temporal import temporal_corpus, temporal_queries
from tests.eval.temporal_runner import (
    DECAY_OFF,
    DECAY_ON,
    FUSED_RANGE_AT_DEFAULT_K,
    SLICES,
    HalfLifeRule,
    K,
    SliceReports,
    aggregate_slices,
    measure_slices,
    ranked_ids,
    render_table,
    seed_temporal_corpus,
)
from tests.fakes import make_clock, make_embedder, make_search_config

VARIANTS: dict[str, HalfLifeRule] = {
    "A_always_on": lambda _q: DECAY_ON,
    "B_off": lambda _q: DECAY_OFF,
    "C_gated": lambda q: DECAY_ON if q.temporal else DECAY_OFF,
}

# The §4.6-(a) floor sweep, decay held ON. 0.2 sits below the threshold
# the arithmetic predicts, 0.381 is the threshold itself, and 0.9 is
# deliberately past the useful band — a floor that high is nearly "decay
# off" again, and the table shows it behaving like it.
FLOORS = (0.2, 0.381, 0.5, 0.7, 0.8, 0.9)

# The floor above which the recency range (1/floor) is narrower than the
# fused RRF range, i.e. where match quality — not age — becomes the sort
# key. Arithmetic on the two formulas, not a property of this fixture.
QUALITY_DOMINATES_ABOVE = 1.0 / FUSED_RANGE_AT_DEFAULT_K


async def measure_all() -> dict[str, SliceReports]:
    """``{variant: {slice: aggregate_report}}`` for all three variants."""
    return {name: aggregate_slices(await measure_slices(rule)) for name, rule in VARIANTS.items()}


async def measure_floors() -> dict[str, SliceReports]:
    """``{"D_floor=0.8": {slice: aggregate_report}, ...}``, decay held ON."""
    return {
        f"D_floor={floor}": aggregate_slices(
            await measure_slices(VARIANTS["A_always_on"], recency_floor=floor)
        )
        for floor in FLOORS
    }


class TestDecayExperiment:
    """ROADMAP §4.4 — measure, then decide."""

    async def test_the_golden_set_is_blind_to_decay(self) -> None:
        """Why this second fixture exists at all.

        ``test_eval_gate._run_eval`` seeds every golden entry with no
        ``occurred_at``, so all eight resolve to one timestamp, the recency
        factor is the same constant for every candidate and it cancels out
        of the ordering *exactly*. Turning decay off must therefore change
        no golden ranking at all. If this ever fails, the golden set has
        gained age variation and §4.4 could be measured on it directly.
        """
        clock = make_clock()
        store = MemoryStore(clock)
        write_service = WriteService(store, make_embedder())
        for kind, summary, tags in golden_corpus():
            await write_service.write(
                EntryDraft(
                    kind=Kind(kind),
                    summary=summary,
                    author="eval",
                    agent="eval-agent",
                    tags=tuple(tags),
                )
            )
        for query, _relevant in golden_queries():
            assert await ranked_ids(store, clock, query, DECAY_ON) == await ranked_ids(
                store, clock, query, DECAY_OFF
            ), f"golden query '{query}' is decay-sensitive — the golden set is no longer blind"

    async def test_the_fixture_actually_exercises_decay(self) -> None:
        """Unlike the golden set, this corpus makes the recency term bite.

        If turning decay off changed no ranking, the fixture would be as
        blind to §6.4 as the golden set is and every number below would be
        meaningless. This asserts it is not.
        """
        store, _entry_ids, now_clock = await seed_temporal_corpus()
        differing = [
            labelled.query
            for labelled in temporal_queries()
            if await ranked_ids(store, now_clock, labelled.query, DECAY_ON)
            != await ranked_ids(store, now_clock, labelled.query, DECAY_OFF)
        ]
        assert differing, "decay changed no ranking — the fixture cannot measure §4.4"

    async def test_decay_off_is_exactly_neutral(self) -> None:
        """Variant B must be "no recency term", not "a weak one"."""
        oldest_age = max(aged.age_days for aged in temporal_corpus())
        assert 0.5 ** (oldest_age / DECAY_OFF) == 1.0

    async def test_always_on_decay_costs_non_temporal_ranking(self) -> None:
        """The §4.4 hypothesis, measured: the unconditional term depresses
        ranking quality on queries that carry no time sense."""
        measured = await measure_all()
        always_on = measured["A_always_on"]["non_temporal"]
        off = measured["B_off"]["non_temporal"]
        assert off["hit_at_k"] > always_on["hit_at_k"]
        assert off["mrr"] > always_on["mrr"], (
            "expected the §4.4 regression on non-temporal queries; measured "
            f"A={always_on['mrr']:.3f} B={off['mrr']:.3f}"
        )
        assert off[f"ndcg_at_{K}"] > always_on[f"ndcg_at_{K}"]

    async def test_recency_earns_its_keep_only_on_currency_pairs(self) -> None:
        """Where the term does real work — and where it does not.

        On a currency pair the right answer genuinely *is* the newest entry
        on its topic and the stale entry has the higher lexical overlap, so
        without recency the pipeline has no signal at all: A beats B there.
        That is the *only* slice where A wins. On the temporal slice as a
        whole it does not, because one temporal query (T1) asks for the
        current state of something whose only entry is old.
        """
        measured = await measure_all()
        assert (
            measured["A_always_on"]["currency_pair"]["mrr"]
            > (measured["B_off"]["currency_pair"]["mrr"])
        )
        assert measured["A_always_on"]["temporal"]["mrr"] <= measured["B_off"]["temporal"]["mrr"], (
            "if always-on now wins the whole temporal slice, the §4.4 "
            "conclusion (time sense is the wrong gating axis) needs rechecking"
        )

    async def test_measure_all_is_deterministic_across_variants(self) -> None:
        """This is a determinism check, not a §4.4 finding.

        C is *defined* as A on temporal queries and B on the rest
        (``VARIANTS["C_gated"]``), and ``measure_slices`` buckets each query's
        metrics into its labelled slice — so
        ``measured["C_gated"]["temporal"] == measured["A_always_on"]["temporal"]``
        is true by construction, for any pipeline, correct or not; it is
        NOT evidence about retrieval quality (that's
        ``test_gating_does_not_fix_the_old_exact_failure`` and the MRR
        comparisons above).

        What this test actually exercises: ``measure_all`` reseeds a fresh
        store per variant (fresh ``FixedClock``, fresh writes) and the hash
        embedder + retrieval pipeline have no randomness, so two
        independently-seeded runs of the *same effective config* (A's
        temporal-query runs vs. C's temporal-query runs) must reproduce
        bit-identical aggregate metrics. A prior version of this test's
        docstring called the equality itself the finding; it was the
        wiring, not the finding, that was worth asserting — this version
        says so.
        """
        measured = await measure_all()
        assert measured["C_gated"]["temporal"] == measured["A_always_on"]["temporal"]
        assert measured["C_gated"]["non_temporal"] == measured["B_off"]["non_temporal"]

    async def test_gating_does_not_fix_the_old_exact_failure(self) -> None:
        """The finding that decides §4.4.

        The fixture puts the same competition — an old entry matching the
        query almost word for word against a recent entry that merely
        shares a phrase — behind a non-temporal query (entries 0/1) and a
        temporal one (entries 2/3). The gate leaves decay ON for the
        temporal one, so it stays buried: gating on the query's time sense
        does not address the mechanism, it only narrows where it shows up.
        """
        store, entry_ids, now_clock = await seed_temporal_corpus()
        old_exact = [q for q in temporal_queries() if q.pattern == "old_exact"]
        assert {case.temporal for case in old_exact} == {True, False}, (
            "the old_exact pattern must be crossed with both query kinds"
        )

        for case in old_exact:
            ranked = await ranked_ids(store, now_clock, case.query, VARIANTS["C_gated"](case))
            relevant_id = entry_ids[case.relevant[0]]
            if case.temporal:
                assert relevant_id not in ranked, (
                    "the gated variant unexpectedly recovered the temporal "
                    "old-exact case; the §4.4 conclusion needs rechecking"
                )
            else:
                assert ranked and ranked[0] == relevant_id

    async def test_report_table_covers_every_variant_and_slice_with_expected_counts(self) -> None:
        """Print the measured table and assert its *shape* is complete —
        every variant x slice cell present, with the fixture's expected
        query counts per slice. This checks fixture shape, not measured
        retrieval behavior (the MRR/hit@k assertions above do that)."""
        measured = await measure_all()
        table = render_table(measured, "variant")
        print("\n" + table)
        for variant in VARIANTS:
            for slice_name in SLICES:
                assert f"| {variant} | {slice_name} |" in table
        counts = {name: measured["A_always_on"][name]["queries"] for name in SLICES}
        assert counts == {
            "non_temporal": 6.0,
            "temporal": 4.0,
            "old_exact": 2.0,
            "currency_pair": 3.0,
            "timeless": 5.0,
            "all": 10.0,
        }


class TestRecencyFloorSweep:
    """ROADMAP §4.6 direction (a) — bound the unbounded factor, then decide.

    The §4.6 ``rrf_k`` sweep failed because widening the *fused* range is
    capped at 40x (~5.3 half-lives) against entries 12.7 to 14 half-lives
    old. Flooring attacks the other side of the same mismatch and has no
    such cap: the recency range collapses to ``1/floor`` outright.
    """

    def test_the_floor_is_off_by_default_so_variant_a_is_what_ships(self) -> None:
        """The whole sweep is a measurement, not a change: the row labelled
        "today" must really be today."""
        assert make_search_config().recency_floor is None

    def test_the_arithmetic_predicts_where_match_quality_takes_over(self) -> None:
        """Fixture-independent, and stated *before* the numbers below.

        A floor ``f`` bounds the recency factor's range at ``1/f``. Match
        quality can only outrank age once that is narrower than the fused
        RRF range (2.6230x at the §6.2 defaults), i.e. ``f > 0.381``. That
        is a lower bound on where the effect can begin, not a promise about
        where it completes: two candidates that both appear in *both*
        streams span far less than the best-vs-worst 2.62x, so the floor
        that actually flips those needs to be higher.
        """
        assert pytest.approx(2.6230, abs=1e-4) == FUSED_RANGE_AT_DEFAULT_K
        assert pytest.approx(0.381, abs=1e-3) == QUALITY_DOMINATES_ABOVE
        assert min(FLOORS) < QUALITY_DOMINATES_ABOVE < max(FLOORS), (
            "the sweep must straddle the predicted threshold or it cannot test the prediction"
        )

    async def test_an_unset_floor_reproduces_variant_a_exactly(self) -> None:
        """The seam is behaviour-preserving at the eval level too, not just
        in the unit test: ``recency_floor=None`` must reproduce today's
        numbers bit for bit, or every comparison below is against a moved
        baseline."""
        unfloored = aggregate_slices(
            await measure_slices(VARIANTS["A_always_on"], recency_floor=None)
        )
        assert unfloored == (await measure_all())["A_always_on"]

    async def test_the_floor_is_reached_at_all_by_this_harness(self) -> None:
        """A knob that changes no ranking would make the whole table a
        constant — the bug ADR 0021 found on the prefix knob."""
        store, _entry_ids, now_clock = await seed_temporal_corpus()
        differing = [
            labelled.query
            for labelled in temporal_queries()
            if await ranked_ids(store, now_clock, labelled.query, DECAY_ON)
            != await ranked_ids(store, now_clock, labelled.query, DECAY_ON, recency_floor=0.8)
        ]
        assert differing, "the floor changed no ranking — the sweep cannot measure §4.6-(a)"

    async def test_a_floor_below_the_predicted_threshold_does_not_recover_old_exact(
        self,
    ) -> None:
        """The prediction's negative half, which is what makes it a
        prediction: at 0.2 and at 0.381 itself the ``old_exact`` MRR stays
        at 0.100 — the entry surfaces at rank 5 at best, not at rank 1."""
        measured = await measure_floors()
        for floor in (0.2, 0.381):
            assert measured[f"D_floor={floor}"]["old_exact"]["mrr"] < 0.2, (
                f"floor {floor} is below the predicted threshold "
                f"{QUALITY_DOMINATES_ABOVE:.3f} but recovered old_exact anyway; "
                "the §4.6 arithmetic needs rechecking"
            )

    async def test_a_floor_recovers_the_old_exact_cases_always_on_scores_zero(self) -> None:
        """**Half one of the discriminating question.**

        Always-on scores 0.000 hit@5 on ``old_exact`` (§4.4) and no value of
        ``rrf_k`` moved it (§4.6). A floor at or above 0.5 takes it to 1.000
        hit@5, and at 0.8 to a perfect 1.000 MRR — the relevant entry is
        rank 1 for both queries.
        """
        measured = await measure_floors()
        always_on = (await measure_all())["A_always_on"]["old_exact"]
        assert always_on["hit_at_k"] == 0.0
        for floor in (0.5, 0.7, 0.8):
            assert measured[f"D_floor={floor}"]["old_exact"]["hit_at_k"] == 1.0
        assert measured["D_floor=0.8"]["old_exact"]["mrr"] == 1.0

    async def test_the_same_floors_keep_the_currency_pairs_decay_off_loses(self) -> None:
        """**Half two of the discriminating question.**

        Turning decay off wins ``old_exact`` but pays for it on
        ``currency_pair``, where the stale entry has the higher lexical
        overlap and recency is the only signal: B drops to 0.611 MRR from
        A's 0.833. A floored variant keeps 0.778 there — it still decays,
        it just cannot decay *without limit*.
        """
        measured = await measure_floors()
        off = (await measure_all())["B_off"]["currency_pair"]["mrr"]
        assert off == pytest.approx(0.611, abs=1e-3)
        for floor in (0.5, 0.7, 0.8):
            assert measured[f"D_floor={floor}"]["currency_pair"]["mrr"] > off

    async def test_a_floor_beats_both_extremes_on_their_own_weak_slice(self) -> None:
        """**The finding that decides §4.6-(a).**

        The bar this experiment was written to test: a variant that beats
        *both* extremes on the slice each of them is weak at — A's 0.000
        ``old_exact`` and B's 0.611 ``currency_pair`` MRR. Neither the §4.4
        gate nor any ``rrf_k`` cleared it. Floors in the 0.5 to 0.8 band do, and 0.8 is
        the best of them: ``old_exact`` MRR 1.000 (= B, vs A's 0.000) with
        ``currency_pair`` MRR 0.778 (> B's 0.611).
        """
        measured = await measure_floors()
        baseline = await measure_all()
        winners = [
            label
            for label, slices in measured.items()
            if slices["old_exact"]["mrr"] > baseline["A_always_on"]["old_exact"]["mrr"]
            and slices["currency_pair"]["mrr"] > baseline["B_off"]["currency_pair"]["mrr"]
        ]
        assert winners, (
            "no floor beat both extremes on their own weak slice — the §4.6-(a) "
            "verdict in the ROADMAP needs rewriting"
        )
        assert "D_floor=0.8" in winners
        best = max(winners, key=lambda label: measured[label]["old_exact"]["mrr"])
        assert best == "D_floor=0.8"
        assert measured["D_floor=0.8"]["old_exact"]["mrr"] == 1.0
        assert measured["D_floor=0.8"]["currency_pair"]["mrr"] == pytest.approx(0.778, abs=1e-3)

    async def test_too_high_a_floor_gives_back_what_decay_off_gives_back(self) -> None:
        """The floor is **not** monotonically better, which is the reason to
        report a value rather than a direction.

        At 0.9 the recency range is 1.11x — narrower than the fused spread
        of almost any pair — so the variant is nearly B again, and it pays
        B's price: ``currency_pair`` MRR falls to 0.583, *below* decay-off's
        0.611 and well below A's 0.833. A floor is a band, not a slider.
        """
        measured = await measure_floors()
        baseline = await measure_all()
        high = measured["D_floor=0.9"]["currency_pair"]["mrr"]
        assert high < baseline["B_off"]["currency_pair"]["mrr"]
        assert high < measured["D_floor=0.8"]["currency_pair"]["mrr"]

    async def test_no_floor_loses_hit_at_k_against_either_extreme(self) -> None:
        """A sanity bound on the recommendation: whatever the floor costs,
        it is not recall. Every floor from 0.5 up answers all ten queries
        inside the top 5 — matching decay-off and doubling always-on."""
        measured = await measure_floors()
        for floor in (0.5, 0.7, 0.8, 0.9):
            assert measured[f"D_floor={floor}"]["all"]["hit_at_k"] == 1.0

    async def test_floor_report_table_covers_every_floor_and_slice(self) -> None:
        """Print the measured table and assert its shape is complete — every
        floor x slice cell present, with the fixture's expected query counts.
        Shape only; the findings are asserted above."""
        measured = await measure_floors()
        table = render_table(measured, "recency_floor")
        print("\n" + table)
        for floor in FLOORS:
            for slice_name in SLICES:
                assert f"| D_floor={floor} | {slice_name} |" in table
        counts = {name: measured["D_floor=0.8"][name]["queries"] for name in SLICES}
        assert counts == {
            "non_temporal": 6.0,
            "temporal": 4.0,
            "old_exact": 2.0,
            "currency_pair": 3.0,
            "timeless": 5.0,
            "all": 10.0,
        }
