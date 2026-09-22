"""Seeding + ranking for the age-varied fixture, shared by its experiments.

``tests/eval/temporal.py`` is the fixture (the aged corpus and the
labelled queries). This module is the *machinery* the experiments over it
share: seed the corpus into a ``MemoryStore``, rank one query under one
``SearchConfig``, bucket each query's metrics into the report slices.

Two experiment modules use it, over three sweeps, and must not drift
apart:

* ``test_decay_experiment.py`` — ROADMAP §4.4, sweeps
  ``SearchConfig.half_life_days`` (is the recency term worth gating?).
* ``test_rrf_k_experiment.py`` — ROADMAP §4.6, sweeps
  ``SearchConfig.rrf_k`` with decay left ON (does widening the fused
  range recover what the always-on recency term buries?).
* ``test_decay_experiment.py`` again — ROADMAP §4.6 direction (a),
  sweeps ``SearchConfig.recency_floor`` with the 30-day half-life left
  ON (does *bounding* the recency factor recover it instead?).

Every sweep is realised through ``SearchConfig`` alone — no behaviour
change to production code, which is the point of running them *before*
deciding anything. ``recency_floor`` needed a new (default-off) knob at
the ``entry_score`` seam; its default is ``None``, so the shipped
pipeline is untouched.
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
from tests.eval.temporal import TEMPORAL_K, LabelledQuery, temporal_corpus, temporal_queries
from tests.fakes import FIXED_NOW, FixedClock, make_clock, make_embedder, make_search_config

K = TEMPORAL_K

# The two half-lives the experiments sweep. DECAY_OFF is large enough that
# the recency factor is *exactly* 1.0 for the oldest entry in the corpus,
# so "off" is "no recency term" rather than "a very long half-life" — see
# ``test_decay_off_is_exactly_neutral``. It matters: at 1e9 the factor
# still varies in the 7th decimal, which is enough to break ties in the
# fused score and quietly reorder results.
DECAY_ON = 30.0
DECAY_OFF = 1.0e20

# SPEC §6.2's rank-damping constant, and the candidate pool the sweeps
# run with. ``candidate_top_k=20`` is the production default; the fake's
# default is 10 (used by the §1.1 gate, tests/fakes.py), so this fixture
# runs under a slightly different config than the gate. Harmless here:
# every comparison below varies one knob with the pool size held fixed.
DEFAULT_RRF_K = 60
CANDIDATE_TOP_K = 20

# The fused RRF range at the defaults above: ``2(k + candidates)/(k + 1)``
# (derived and asserted against ``rrf_fuse`` itself in
# ``test_rrf_k_experiment.fused_range``). A recency floor ``f`` bounds the
# recency factor's range at ``1/f``, so match quality can outrank age only
# once ``1/f < FUSED_RANGE_AT_DEFAULT_K``, i.e. ``f > 0.381``.
FUSED_RANGE_AT_DEFAULT_K = 2 * (DEFAULT_RRF_K + CANDIDATE_TOP_K) / (DEFAULT_RRF_K + 1)

# A variant is a rule mapping a query to the half-life it is scored under.
HalfLifeRule = Callable[[LabelledQuery], float]

# The slices reported, in report order: the §4.4 axis first, then the
# competition-pattern diagnostic, then the whole set.
SLICES = ("non_temporal", "temporal", "old_exact", "currency_pair", "timeless", "all")

QueryMetrics = dict[str, Any]
SliceReports = dict[str, dict[str, float]]


def slices_of(labelled: LabelledQuery) -> tuple[str, ...]:
    """Which report slices a query belongs to (a query is in three)."""
    return ("temporal" if labelled.temporal else "non_temporal", labelled.pattern, "all")


async def seed_temporal_corpus() -> tuple[MemoryStore, list[str], FixedClock]:
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


async def ranked_ids(
    store: MemoryStore,
    now_clock: FixedClock,
    query: str,
    half_life_days: float,
    *,
    rrf_k: int = DEFAULT_RRF_K,
    recency_floor: float | None = None,
) -> list[str]:
    """The ranked entry ids for one query under one search config."""
    service = SearchService(
        store,
        make_embedder(),
        make_search_config(
            candidate_top_k=CANDIDATE_TOP_K,
            default_limit=K,
            half_life_days=half_life_days,
            rrf_k=rrf_k,
            recency_floor=recency_floor,
        ),
        now_fn=now_clock,
    )
    hits = await service.search(query, limit=K)
    return [hit.entry_id for hit in hits]


async def measure_slices(
    half_life: HalfLifeRule,
    *,
    rrf_k: int = DEFAULT_RRF_K,
    recency_floor: float | None = None,
) -> dict[str, dict[str, QueryMetrics]]:
    """Run every labelled query under one config, bucketed into the slices."""
    store, entry_ids, now_clock = await seed_temporal_corpus()
    slices: dict[str, dict[str, QueryMetrics]] = {name: {} for name in SLICES}
    for labelled in temporal_queries():
        ranked = await ranked_ids(
            store,
            now_clock,
            labelled.query,
            half_life(labelled),
            rrf_k=rrf_k,
            recency_floor=recency_floor,
        )
        relevant = {entry_ids[i]: 1 for i in labelled.relevant}
        metrics = evaluate_query(ranked, relevant, K)
        for slice_name in slices_of(labelled):
            slices[slice_name][labelled.query] = metrics
    return slices


def aggregate_slices(slices: dict[str, dict[str, QueryMetrics]]) -> SliceReports:
    """``{slice: aggregate_report}`` for one measured config."""
    return {name: aggregate_report(per_query, K) for name, per_query in slices.items()}


def render_table(measured: dict[str, SliceReports], first_column: str) -> str:
    """The measured numbers as a Markdown table (pasted into the ROADMAP)."""
    lines = [
        f"| {first_column} | slice | queries | hit@{K} | MRR | nDCG@{K} |",
        "|---|---|---|---|---|---|",
    ]
    for row_label, slices in measured.items():
        for slice_name in SLICES:
            report = slices[slice_name]
            lines.append(
                f"| {row_label} | {slice_name} | {int(report['queries'])} | "
                f"{report['hit_at_k']:.3f} | {report['mrr']:.3f} | {report[f'ndcg_at_{K}']:.3f} |"
            )
    return "\n".join(lines)
