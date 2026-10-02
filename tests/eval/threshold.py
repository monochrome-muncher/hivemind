"""Measure a value for the search similarity threshold (ADR 0062).

The threshold depends on the embedding model, so it is measured with the
model a deployment runs, not with the 4-dimension hash embedder the other
evals use. This module seeds the golden corpus and the age-varied corpus
(``golden.py``, ``temporal.py``: 24 entries, 18 answerable queries) plus a
set of off-topic queries that no entry answers, then reports, for a sweep
of thresholds:

* how many answerable queries keep their relevant entry in the vector
  stream, and the search's hit@5;
* how many off-topic queries get an empty vector stream, and an empty
  search (the keyword stream still runs). The default ``MemoryStore``
  matches any shared word, stop words included, so Postgres comes back
  empty at least as often; ``measure(..., store=PgStore(...))`` on a
  scratch pool gives Postgres's own keyword matching.

Run it against the deployment's embedder (the ``HIVEMIND_EMBEDDING_*``
settings, e.g. ``make vllm`` locally): ``make measure-threshold``.
``tests/eval/test_threshold_measurement.py`` runs it on the hash embedder,
which checks the machinery only; its numbers mean nothing.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, replace

from hivemind.domain.entry import EntryDraft, Kind
from hivemind.memstore import MemoryStore, cosine_similarity
from hivemind.ports import Embedder, Store
from hivemind.retrieval.eval import hit_at_k
from hivemind.services.governance import WriteService
from hivemind.services.search import SearchService
from tests.eval.golden import golden_corpus, golden_queries
from tests.eval.temporal import temporal_corpus, temporal_queries
from tests.fakes import FIXED_NOW, make_clock, make_search_config

K = 5
SWEEP = tuple(round(0.05 * i, 2) for i in range(0, 19))  # 0.00 .. 0.90
# Kept below the lowest relevant similarity (see ``suggest``).
MARGIN = 0.05

# Queries no entry of either corpus answers. Most share the corpora's
# domain (ops, auth, data, deploys) so they are hard negatives, written
# the way agents search: a few content words.
OFF_TOPIC_QUERIES: tuple[str, ...] = (
    "kubernetes horizontal pod autoscaler cpu target",
    "tls certificate rotation on the load balancer",
    "frontend javascript bundle size budget",
    "on-call escalation policy after hours",
    "terraform state locking in s3",
    "structured logging library for the go services",
    "kafka consumer group lag alert",
    "gdpr retention period for customer emails",
    "redis cache eviction policy",
    "sourdough bread recipe",
    "office wifi password reset",
    "quarterly sales targets for emea",
)


@dataclass(frozen=True, slots=True)
class Row:
    """One threshold of the sweep."""

    threshold: float
    answers_kept: int  # answerable queries whose relevant entry stays in the vector stream
    hit_at_k: float  # mean hit@K of the answerable queries, through SearchService
    vector_empty: int  # off-topic queries with an empty vector stream
    search_empty: int  # off-topic queries with an empty search


@dataclass(frozen=True, slots=True)
class Measurement:
    model: str
    answerable: int
    off_topic: int
    relevant_similarities: list[float]  # per answerable query, its best relevant entry
    off_topic_similarities: list[float]  # per off-topic query, its nearest entry
    rows: list[Row]

    @property
    def suggested(self) -> float:
        return suggest(self.relevant_similarities)


def suggest(relevant_similarities: list[float], margin: float = MARGIN) -> float:
    """The highest 0.05 step that is at least ``margin`` below the lowest
    relevant similarity, so every answerable query keeps its answer: a
    missed answer costs more than an unrelated hit. Never below 0."""
    lowest = min(relevant_similarities)
    return max(0.0, math.floor((lowest - margin) * 20 + 1e-9) / 20)


async def _seed(embedder: Embedder, store: Store) -> dict[str, list[float]]:
    """Write both corpora; return each entry's id -> stored vector, in seed order."""
    writer = WriteService(store, embedder)
    stored = {}
    corpus = [(kind, summary, list(tags)) for kind, summary, tags in golden_corpus()]
    corpus += [(e.kind, e.summary, list(e.tags)) for e in temporal_corpus()]
    for kind, summary, tags in corpus:
        draft = EntryDraft(kind=Kind(kind), summary=summary, tags=tags, author="eval", agent="eval")
        stored[(await writer.write(draft)).id] = await embedder.embed_entry(draft)
    return stored


def _answerable(ids: list[str]) -> list[tuple[str, set[str]]]:
    offset = len(golden_corpus())
    queries = [(q, {ids[i] for i in rel}) for q, rel in golden_queries()]
    queries += [(q.query, {ids[offset + i] for i in q.relevant}) for q in temporal_queries()]
    return queries


async def measure(
    embedder: Embedder, sweep: tuple[float, ...] = SWEEP, *, store: Store | None = None
) -> Measurement:
    """Run the sweep with ``embedder``; see the module docstring.

    ``store`` must be empty; a ``MemoryStore`` by default. A scratch
    ``PgStore`` gives Postgres's keyword matching (never a real pool: the
    corpus is written into it)."""
    store = store or MemoryStore(make_clock())
    stored = await _seed(embedder, store)
    answerable = _answerable(list(stored))

    async def similarities(query: str) -> dict[str, float]:
        vector = await embedder.embed_text(query)
        return {eid: cosine_similarity(vector, emb) for eid, emb in stored.items()}

    relevant_sims = []
    for query, relevant in answerable:
        sims = await similarities(query)
        relevant_sims.append(max(sims[eid] for eid in relevant))
    off_sims = [max((await similarities(query)).values()) for query in OFF_TOPIC_QUERIES]

    rows = []
    base = make_search_config(candidate_top_k=20, default_limit=K)
    for threshold in sweep:
        config = replace(base, vector_min_similarity=threshold)
        service = SearchService(store, embedder, config, now_fn=lambda: FIXED_NOW)
        hits = 0.0
        for query, relevant in answerable:
            ranked = [h.entry_id for h in await service.search(query, limit=K)]
            hits += hit_at_k(ranked, dict.fromkeys(relevant, 1), K)
        search_empty = 0
        for query in OFF_TOPIC_QUERIES:
            search_empty += not await service.search(query, limit=K)
        rows.append(
            Row(
                threshold=threshold,
                answers_kept=sum(s > threshold for s in relevant_sims),
                hit_at_k=hits / len(answerable),
                vector_empty=sum(s <= threshold for s in off_sims),
                search_empty=search_empty,
            )
        )
    return Measurement(
        model=embedder.model_name,
        answerable=len(answerable),
        off_topic=len(OFF_TOPIC_QUERIES),
        relevant_similarities=relevant_sims,
        off_topic_similarities=off_sims,
        rows=rows,
    )


def report(m: Measurement) -> str:
    """The measurement as Markdown, ready for docs/retrieval-experiments.md."""
    lines = [
        f"Model: `{m.model}`. {m.answerable} answerable queries, {m.off_topic} off-topic.",
        "",
        "Lowest similarity of an answerable query to its relevant entry: "
        f"{min(m.relevant_similarities):.3f} (median {_median(m.relevant_similarities):.3f}).",
        "Highest similarity of an off-topic query to any entry: "
        f"{max(m.off_topic_similarities):.3f} (median {_median(m.off_topic_similarities):.3f}).",
        "",
        f"| threshold | answers kept in vector stream | hit@{K} "
        "| off-topic: vector stream empty | off-topic: search empty |",
        "|---|---|---|---|---|",
    ]
    for r in m.rows:
        lines.append(
            f"| {r.threshold:.2f} | {r.answers_kept}/{m.answerable} | {r.hit_at_k:.3f} "
            f"| {r.vector_empty}/{m.off_topic} | {r.search_empty}/{m.off_topic} |"
        )
    lines += ["", f"Suggested HIVEMIND_VECTOR_MIN_SIMILARITY: {m.suggested:.2f}"]
    return "\n".join(lines)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


async def _main() -> None:
    from hivemind.config import endpoint_label, load_settings
    from hivemind.embeddings import EmbeddingError, build_embedder

    settings = load_settings()
    try:
        print(report(await measure(build_embedder(settings))))
    except EmbeddingError as exc:
        where = endpoint_label(settings.embedding_endpoint)
        raise SystemExit(f"embedder at {where} unavailable ({exc}); start it (make vllm)") from None


if __name__ == "__main__":
    asyncio.run(_main())
