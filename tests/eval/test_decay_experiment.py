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

Run ``uv run pytest tests/eval/test_decay_experiment.py -s`` to print the
measured tables (reproduced in
``.superpowers/sdd/tier4-provenance-and-decay/task-2-report.md``).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from hivemind.domain.entry import EntryDraft, Kind
from hivemind.memstore import MemoryStore
from hivemind.retrieval.eval import aggregate_report, evaluate_query
from hivemind.services.governance import WriteService
from hivemind.services.search import SearchService
from tests.eval.golden import golden_corpus, golden_queries
from tests.eval.temporal import TEMPORAL_K, LabelledQuery, temporal_corpus, temporal_queries
from tests.fakes import FIXED_NOW, FixedClock, make_clock, make_embedder, make_search_config

K = TEMPORAL_K

# The two half-lives the experiment sweeps. DECAY_OFF is large enough that
# the recency factor is *exactly* 1.0 for the oldest entry in the corpus,
# so variant B is "no recency term" rather than "a very long half-life" —
# see ``test_decay_off_is_exactly_neutral``. It matters: at 1e9 the factor
# still varies in the 7th decimal, which is enough to break ties in the
# fused score and quietly reorder results.
DECAY_ON = 30.0
DECAY_OFF = 1.0e20

# A variant is a rule mapping a query to the half-life it is scored under.
HalfLifeRule = Callable[[LabelledQuery], float]

VARIANTS: dict[str, HalfLifeRule] = {
    "A_always_on": lambda _q: DECAY_ON,
    "B_off": lambda _q: DECAY_OFF,
    "C_gated": lambda q: DECAY_ON if q.temporal else DECAY_OFF,
}

# The slices reported, in report order: the §4.4 axis first, then the
# competition-pattern diagnostic, then the whole set.
SLICES = ("non_temporal", "temporal", "old_exact", "currency_pair", "timeless", "all")

QueryMetrics = dict[str, Any]


def _slices_of(labelled: LabelledQuery) -> tuple[str, ...]:
    """Which report slices a query belongs to (a query is in three)."""
    return ("temporal" if labelled.temporal else "non_temporal", labelled.pattern, "all")


async def _seed() -> tuple[MemoryStore, list[str], FixedClock]:
    """Seed the age-varied corpus; return the store, entry ids and the "now".

    The store's clock advances one second per write, so ``created_at``
    order equals corpus order and the store's overlap tie-break is
    deterministic (entry ids are uuid4, so leaving ties to the id would not
    be). The search clock stays pinned at ``FIXED_NOW``; entry ages come
    from the explicit ``occurred_at`` each draft carries.
    """
    store_clock = make_clock()
    now_clock = make_clock()
    store = MemoryStore(store_clock)
    write_service = WriteService(store, make_embedder())

    entry_ids: list[str] = []
    for aged in temporal_corpus():
        draft = EntryDraft(
            kind=Kind(aged.kind),
            summary=aged.summary,
            author="eval",
            agent="eval-agent",
            tags=aged.tags,
            occurred_at=FIXED_NOW - timedelta(days=aged.age_days),
        )
        entry = await write_service.write(draft)
        entry_ids.append(entry.id)
        store_clock.advance_days(1.0 / 86_400.0)
    return store, entry_ids, now_clock


async def _rank(
    store: MemoryStore,
    now_clock: FixedClock,
    query: str,
    half_life_days: float,
) -> list[str]:
    """The ranked entry ids for one query under one half-life."""
    service = SearchService(
        store,
        make_embedder(),
        make_search_config(candidate_top_k=20, default_limit=K, half_life_days=half_life_days),
        now_fn=now_clock,
    )
    hits = await service.search(query, limit=K)
    return [hit.entry_id for hit in hits]


async def run_variant(rule: HalfLifeRule) -> dict[str, dict[str, QueryMetrics]]:
    """Run every labelled query under one variant, bucketed into the slices."""
    store, entry_ids, now_clock = await _seed()
    slices: dict[str, dict[str, QueryMetrics]] = {name: {} for name in SLICES}
    for labelled in temporal_queries():
        ranked = await _rank(store, now_clock, labelled.query, rule(labelled))
        relevant = {entry_ids[i]: 1 for i in labelled.relevant}
        metrics = evaluate_query(ranked, relevant, K)
        for slice_name in _slices_of(labelled):
            slices[slice_name][labelled.query] = metrics
    return slices


async def measure_all() -> dict[str, dict[str, dict[str, float]]]:
    """``{variant: {slice: aggregate_report}}`` for all three variants."""
    out: dict[str, dict[str, dict[str, float]]] = {}
    for name, rule in VARIANTS.items():
        slices = await run_variant(rule)
        out[name] = {
            slice_name: aggregate_report(per_query, K) for slice_name, per_query in slices.items()
        }
    return out


def render_table(measured: dict[str, dict[str, dict[str, float]]]) -> str:
    """The measured numbers as a Markdown table (pasted into the report)."""
    lines = [
        f"| variant | slice | queries | hit@{K} | MRR | nDCG@{K} |",
        "|---|---|---|---|---|---|",
    ]
    for variant, slices in measured.items():
        for slice_name in SLICES:
            report = slices[slice_name]
            lines.append(
                f"| {variant} | {slice_name} | {int(report['queries'])} | "
                f"{report['hit_at_k']:.3f} | {report['mrr']:.3f} | {report[f'ndcg_at_{K}']:.3f} |"
            )
    return "\n".join(lines)


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
            assert await _rank(store, clock, query, DECAY_ON) == await _rank(
                store, clock, query, DECAY_OFF
            ), f"golden query '{query}' is decay-sensitive — the golden set is no longer blind"

    async def test_the_fixture_actually_exercises_decay(self) -> None:
        """Unlike the golden set, this corpus makes the recency term bite.

        If turning decay off changed no ranking, the fixture would be as
        blind to §6.4 as the golden set is and every number below would be
        meaningless. This asserts it is not.
        """
        store, _entry_ids, now_clock = await _seed()
        differing = [
            labelled.query
            for labelled in temporal_queries()
            if await _rank(store, now_clock, labelled.query, DECAY_ON)
            != await _rank(store, now_clock, labelled.query, DECAY_OFF)
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

    async def test_gated_variant_is_exactly_the_per_slice_mix(self) -> None:
        """C is A on temporal queries and B on the rest — by construction.

        Asserting it keeps the reported C numbers honest: C adds no
        retrieval signal of its own, it only picks which of A and B to use
        per query, using an oracle label no production code has.
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
        store, entry_ids, now_clock = await _seed()
        old_exact = [q for q in temporal_queries() if q.pattern == "old_exact"]
        assert {case.temporal for case in old_exact} == {True, False}, (
            "the old_exact pattern must be crossed with both query kinds"
        )

        for case in old_exact:
            ranked = await _rank(store, now_clock, case.query, VARIANTS["C_gated"](case))
            relevant_id = entry_ids[case.relevant[0]]
            if case.temporal:
                assert relevant_id not in ranked, (
                    "the gated variant unexpectedly recovered the temporal "
                    "old-exact case; the §4.4 conclusion needs rechecking"
                )
            else:
                assert ranked and ranked[0] == relevant_id

    async def test_report_table_is_emitted(self) -> None:
        """Print the measured table and assert it is complete — every
        variant x slice cell present, with the expected query counts."""
        measured = await measure_all()
        table = render_table(measured)
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
