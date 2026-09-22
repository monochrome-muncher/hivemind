"""The retrieval eval runner + CI gate (ROADMAP §1.1).

Seam under test: the retrieval pipeline (``SearchService``) measured on a
fixed, deterministic golden set (``tests/eval/golden.py``). The corpus is
seeded into a ``MemoryStore`` (no Postgres, no network) on top of
``tests/fakes.py``; each golden query runs through ``SearchService.search``
and the retrieval metrics (hit@k / MRR / nDCG, ``hivemind.retrieval.eval``)
are computed against the expected-relevant entries.

The **CI gate** pins a threshold on the aggregate metrics: a future
retrieval change that *regresses* the pipeline (a lower hit@k / MRR /
nDCG than the pinned bar) fails the suite — retrieval quality becomes
measured, not vibes. The per-query results are also asserted so a single
broken golden query cannot hide behind an average.
"""

from __future__ import annotations

from hivemind.config import SearchConfig
from hivemind.domain.entry import EntryDraft, Kind
from hivemind.memstore import MemoryStore
from hivemind.retrieval.eval import aggregate_report, evaluate_query
from hivemind.services.governance import WriteService
from hivemind.services.search import SearchService
from tests.eval.golden import golden_corpus, golden_queries
from tests.fakes import make_clock, make_embedder, make_search_config

# What the service ships (ADR 0022). The gate runs the shipped pipeline.
_SHIPPED_FLOOR = SearchConfig().recency_floor

# The pinned gate (ROADMAP §1.1: "a CI gate pinning a few golden queries so
# future retrieval changes are measured, not vibes"). These thresholds
# encode the *expected* quality of a healthy hybrid pipeline on the golden
# set (a relevant entry is strongly related to its query, so a good pipeline
# ranks it near the top). A regression that drops below the bar fails.
K = 5
MIN_HIT_AT_K = 0.875  # >= 7 of 8 golden queries hit the top-K (all 8 today)
MIN_MRR = 0.625  # floor: the relevant entry is usually at or near rank 1 (a regression below it fails; an improvement passes)
MIN_NDCG = 0.70  # floor: a healthy pipeline ranks the relevant entry very high


def _seed_corpus(clock) -> tuple[MemoryStore, WriteService]:
    """Seed the golden corpus into a fresh MemoryStore; return the store
    and its WriteService (the corpus is seeded in ``_run_eval``)."""
    store = MemoryStore(clock)
    embedder = make_embedder()
    write_service = WriteService(store, embedder)
    return store, write_service


async def _run_eval(
    golden_k: int = K, *, recency_floor: float | None = _SHIPPED_FLOOR
) -> tuple[dict[str, dict], dict[str, float]]:
    """Run the golden query set through SearchService; return the
    per-query metrics + the aggregate report.

    ``recency_floor`` defaults to whatever the service ships (ADR 0022),
    so the gate always measures the shipped pipeline; it is a parameter
    only so ``test_the_shipped_recency_floor_cannot_move_this_gate`` can
    run the same corpus without it.
    """
    clock = make_clock()
    store, write_service = _seed_corpus(clock)

    # Seed the golden corpus (deterministic ids, in corpus order).
    entry_ids: list[str] = []
    for kind, summary, tags in golden_corpus():
        draft = EntryDraft(
            kind=Kind(kind),
            summary=summary,
            author="eval",
            agent="eval-agent",
            tags=tuple(tags),
        )
        entry = await write_service.write(draft)
        entry_ids.append(entry.id)

    embedder = make_embedder()
    search_service = SearchService(
        store, embedder, make_search_config(recency_floor=recency_floor), now_fn=clock
    )

    # Run each golden query; measure against its expected-relevant entries.
    per_query: dict[str, dict] = {}
    for query, relevant_indices in golden_queries():
        hits = await search_service.search(query, limit=golden_k)
        ranked_ids = [h.entry_id for h in hits]
        relevant = {entry_ids[i]: 1 for i in relevant_indices}
        per_query[query] = evaluate_query(ranked_ids, relevant, golden_k)
    report = aggregate_report(per_query, golden_k)
    return per_query, report


class TestRetrievalEvalGate:
    """The CI gate: the retrieval pipeline must clear the pinned bar on the
    golden set (a regression that drops below it fails the suite)."""

    async def test_hit_at_k_clears_the_bar(self) -> None:
        _per_query, report = await _run_eval()
        assert report["hit_at_k"] >= MIN_HIT_AT_K, (
            f"hit@{K} regressed: {report['hit_at_k']:.3f} < {MIN_HIT_AT_K}"
        )

    async def test_mrr_clears_the_bar(self) -> None:
        _per_query, report = await _run_eval()
        assert report["mrr"] >= MIN_MRR, f"MRR regressed: {report['mrr']:.3f} < {MIN_MRR}"

    async def test_ndcg_clears_the_bar(self) -> None:
        _per_query, report = await _run_eval()
        assert report[f"ndcg_at_{K}"] >= MIN_NDCG, (
            f"nDCG@{K} regressed: {report[f'ndcg_at_{K}']:.3f} < {MIN_NDCG}"
        )

    async def test_every_golden_query_hits_at_top_k(self) -> None:
        """The golden queries are *strongly* related to their relevant
        entry, so each must rank it in the top-K (a single broken query
        is caught, not hidden by the average)."""
        per_query, _report = await _run_eval()
        for query, metrics in per_query.items():
            assert metrics["hit_at_k"], (
                f"golden query '{query}' did not rank its relevant entry in the top-{K}"
            )

    async def test_aggregate_report_is_well_formed(self) -> None:
        _per_query, report = await _run_eval()
        assert set(report) >= {"hit_at_k", "mrr", f"ndcg_at_{K}", "queries"}
        assert report["queries"] == 8  # the golden set size

    async def test_the_shipped_recency_floor_cannot_move_this_gate(self) -> None:
        """ADR 0022 flipped ``recency_floor`` on by default. The pinned
        thresholds above were NOT re-pinned to accommodate it, and they
        did not need to be: every golden entry is seeded without an
        ``occurred_at``, so all eight share one timestamp, every
        candidate's recency factor is 1.0, and a *lower* bound on a
        factor that is already 1.0 changes nothing.

        The equality is asserted rather than argued — if the golden
        corpus ever gains age spread, this test fails and says that the
        gate's numbers are no longer floor-independent, instead of the
        thresholds quietly absorbing a scoring change.
        """
        floored_per_query, floored = await _run_eval()
        unfloored_per_query, unfloored = await _run_eval(recency_floor=None)
        assert _SHIPPED_FLOOR == 0.8
        assert floored == unfloored
        assert floored_per_query == unfloored_per_query
