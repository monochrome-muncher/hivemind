"""The §4.4 experiment: should the SPEC §6.4 recency term stay unconditional?

Seam under test: the same one the §1.1 gate uses — ``SearchService`` over a
``MemoryStore``, measured with ``hivemind.retrieval.eval`` — but on the
age-varied fixture (``tests/eval/temporal.py``) instead of the golden set,
because the golden set seeds every entry at one timestamp and therefore
cannot measure decay at all.

Three variants, all realised through ``SearchConfig.half_life_days`` alone
— no production code change, which is the point of running the experiment
*before* deciding:

* **A. always-on** — today's behaviour, a 30-day half-life on every query.
* **B. off** — a half-life so long that ``0.5 ** (age / half_life)`` is
  exactly ``1.0`` in float64 for every entry in the corpus, so the recency
  factor is a constant and cancels out of the ranking entirely.
* **C. gated** — A's config for queries labelled temporal, B's for the
  rest. This is an **oracle** gate: it reads the fixture's hand-written
  ``temporal`` label, not a classifier. Its numbers are the ceiling a
  perfect query-intent classifier could reach, not what a keyword-list
  heuristic would deliver.

The fused RRF score is identical across all three variants (same store,
same streams, same fusion), so every difference between them is
attributable to the recency factor and nothing else.

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
measured tables (reproduced in
``.superpowers/sdd/tier4-provenance-and-decay/task-2-report.md``).
"""

from __future__ import annotations

from hivemind.domain.entry import EntryDraft, Kind
from hivemind.memstore import MemoryStore
from hivemind.services.governance import WriteService
from tests.eval.golden import golden_corpus, golden_queries
from tests.eval.temporal import temporal_corpus, temporal_queries
from tests.eval.temporal_runner import (
    DECAY_OFF,
    DECAY_ON,
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
from tests.fakes import make_clock, make_embedder

VARIANTS: dict[str, HalfLifeRule] = {
    "A_always_on": lambda _q: DECAY_ON,
    "B_off": lambda _q: DECAY_OFF,
    "C_gated": lambda q: DECAY_ON if q.temporal else DECAY_OFF,
}


async def measure_all() -> dict[str, SliceReports]:
    """``{variant: {slice: aggregate_report}}`` for all three variants."""
    return {name: aggregate_slices(await measure_slices(rule)) for name, rule in VARIANTS.items()}


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
