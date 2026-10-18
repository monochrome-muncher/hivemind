"""Measure a value for the search similarity threshold (ADR 0062).

The threshold depends on the embedding model, so it is measured with the
model a deployment runs, not with the 4-dimension hash embedder the other
evals use. A ``Fixture`` is a pool of entries, queries they answer, and
off-topic queries none of them answers. ``scenarios()`` gives the
multi-domain set (``domains.py``: five kinds of work, 160 entries), each
domain alone as one fleet's pool would be, and all of them together, plus
the golden and age-varied corpora (``golden.py``, ``temporal.py``). For
each, and a sweep of thresholds, it reports:

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
import os
from dataclasses import dataclass, field, replace

from hivemind.domain.entry import EntryDraft, Kind
from hivemind.memstore import MemoryStore, cosine_similarity
from hivemind.ports import Embedder, Store
from hivemind.retrieval.eval import hit_at_k
from hivemind.services.governance import WriteService
from hivemind.services.search import SearchService
from tests.eval.domains import CROSS_DOMAIN, DOMAINS, Domain
from tests.eval.golden import golden_corpus, golden_queries
from tests.eval.temporal import temporal_corpus, temporal_queries
from tests.fakes import FIXED_NOW, make_clock, make_search_config

K = 5
SWEEP = tuple(round(0.05 * i, 2) for i in range(0, 19))  # 0.00 .. 0.90
# Kept below the lowest relevant similarity (see ``suggest``).
MARGIN = 0.05

# Queries no entry of the golden or age-varied corpus answers. Most share the corpora's
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


Entry = tuple[str, str, tuple[str, ...], str | None]  # kind, summary, tags, body


@dataclass(frozen=True, slots=True)
class Fixture:
    """A pool to measure on: entries, queries with the indices of the
    entries that answer them, and queries no entry answers."""

    name: str
    entries: tuple[Entry, ...]
    answerable: tuple[tuple[str, tuple[int, ...]], ...]
    off_topic: tuple[str, ...]


def golden_fixture() -> Fixture:
    """The golden and age-varied corpora with ``OFF_TOPIC_QUERIES``."""
    entries: list[Entry] = [(k, s, tuple(t), None) for k, s, t in golden_corpus()]
    entries += [(e.kind, e.summary, e.tags, None) for e in temporal_corpus()]
    offset = len(golden_corpus())
    answerable = [(q, tuple(rel)) for q, rel in golden_queries()]
    answerable += [(q.query, tuple(offset + i for i in q.relevant)) for q in temporal_queries()]
    return Fixture("golden + age-varied", tuple(entries), tuple(answerable), OFF_TOPIC_QUERIES)


def domain_fixture(domain: Domain, all_domains: tuple[Domain, ...] = DOMAINS) -> Fixture:
    """One domain's pool alone, as one fleet's would be. Off-topic: the
    domain's unanswered queries, then every other domain's queries (answered
    elsewhere, not here)."""
    others = [d for d in all_domains if d is not domain]
    answerable = list(domain.answerable)
    answerable += [(q, (i,)) for q, name, i in CROSS_DOMAIN if name == domain.name]
    off_topic = list(domain.unanswered)
    for other in others:
        off_topic += [q for q, _ in other.answerable] + list(other.unanswered)
    off_topic += [q for q, name, _ in CROSS_DOMAIN if name != domain.name]
    return Fixture(domain.name, domain.entries, tuple(answerable), tuple(off_topic))


def combined_fixture(
    all_domains: tuple[Domain, ...] = DOMAINS, name: str = "all domains together"
) -> Fixture:
    """Every domain in one pool, as an org-wide reader sees it."""
    entries: list[Entry] = []
    answerable: list[tuple[str, tuple[int, ...]]] = []
    offsets = {}
    for domain in all_domains:
        offsets[domain.name] = len(entries)
        answerable += [(q, tuple(len(entries) + i for i in rel)) for q, rel in domain.answerable]
        entries += domain.entries
    answerable += [(q, (offsets[name] + i,)) for q, name, i in CROSS_DOMAIN]
    off_topic = tuple(q for d in all_domains for q in d.unanswered)
    return Fixture(name, tuple(entries), tuple(answerable), off_topic)


def scenarios() -> list[Fixture]:
    """What ``make measure-threshold`` ran on the five-domain study set."""
    return [
        combined_fixture(),
        *(domain_fixture(d) for d in DOMAINS),
        golden_fixture(),
    ]


def extended_scenarios() -> list[Fixture]:
    """The same scenarios over the expanded sixteen-domain corpus
    (``tests/eval/corpus``), plus the golden and age-varied corpora."""
    from tests.eval.corpus import EXTENDED_DOMAINS

    return [
        combined_fixture(EXTENDED_DOMAINS, "all sixteen domains together"),
        *(domain_fixture(d, EXTENDED_DOMAINS) for d in EXTENDED_DOMAINS),
        golden_fixture(),
    ]


def extended_scenarios_specific() -> list[Fixture]:
    """The extended scenarios with the answerable queries replaced by their
    specific (de-vagued) variants (``tests/eval/corpus/specific_queries``), so
    the sweep measures what a refined agent retry — a query that names the
    system and setting — scores against the entries. The off-topic queries
    and the entries are unchanged; only the answerable query text differs."""
    from tests.eval.corpus import EXTENDED_DOMAINS
    from tests.eval.corpus.specific_queries import SPECIFIC_ANSWERABLE

    def _spec_domain(domain: Domain) -> Fixture:
        base = domain_fixture(domain, EXTENDED_DOMAINS)
        spec = SPECIFIC_ANSWERABLE[domain.name]
        answerable = [(spec[i], rel) for i, (_q, rel) in enumerate(domain.answerable)]
        answerable += list(base.answerable[len(domain.answerable) :])  # cross-domain stays
        return Fixture(
            base.name + " (specific queries)", base.entries, tuple(answerable), base.off_topic
        )

    def _spec_combined() -> Fixture:
        base = combined_fixture(EXTENDED_DOMAINS, "all sixteen domains together")
        answerable = list(base.answerable)
        i = 0
        for domain in EXTENDED_DOMAINS:
            spec = SPECIFIC_ANSWERABLE[domain.name]
            for _j in range(len(domain.answerable)):
                answerable[i] = (spec[_j], answerable[i][1])
                i += 1
        # the trailing cross-domain queries (i onwards) stay as written
        return Fixture(
            base.name + " (specific queries)", base.entries, tuple(answerable), base.off_topic
        )

    return [
        _spec_combined(),
        *(_spec_domain(d) for d in EXTENDED_DOMAINS),
        golden_fixture(),
    ]


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
    fixture: str
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


@dataclass(frozen=True, slots=True)
class EmbedCache:
    """Caches embeddings by text so one run of many scenarios embeds each
    entry and query once. The entry key is the summary, which the corpus
    keeps unique; the query key is the query itself."""

    embedder: Embedder
    by_key: dict[str, list[float]] = field(default_factory=dict)

    async def entry(self, draft: EntryDraft) -> list[float]:
        key = "e\x00" + draft.summary
        vector = self.by_key.get(key)
        if vector is None:
            vector = await self.embedder.embed_entry(draft)
            self.by_key[key] = vector
        return vector

    async def text(self, text: str) -> list[float]:
        key = "q\x00" + text
        vector = self.by_key.get(key)
        if vector is None:
            vector = await self.embedder.embed_text(text)
            self.by_key[key] = vector
        return vector


class CachingEmbedder:
    """An ``Embedder`` that embeds each distinct text once, delegating to a
    wrapped embedder. Lets ``SearchService.search`` (which embeds the query on
    every call) reuse vectors across the whole sweep and across scenarios, so
    one run of many fixtures embeds each entry and query a single time."""

    _wrapped: Embedder
    _by_text: dict[str, list[float]]

    def __init__(self, wrapped: Embedder) -> None:
        self._wrapped = wrapped
        self._by_text = {}

    @property
    def dimension(self) -> int:
        return self._wrapped.dimension

    @property
    def model_name(self) -> str:
        return self._wrapped.model_name

    def entry_embeddable_text(self, draft: EntryDraft) -> str:
        return self._wrapped.entry_embeddable_text(draft)

    async def embed_text(self, text: str) -> list[float]:
        vector = self._by_text.get(text)
        if vector is None:
            vector = await self._wrapped.embed_text(text)
            self._by_text[text] = vector
        return vector

    async def embed_entry(self, draft: EntryDraft) -> list[float]:
        key = "e\x00" + self.entry_embeddable_text(draft)
        vector = self._by_text.get(key)
        if vector is None:
            vector = await self._wrapped.embed_entry(draft)
            self._by_text[key] = vector
        return vector


async def _seed(
    embedder: Embedder, store: Store, entries: tuple[Entry, ...], cache: EmbedCache | None = None
) -> list[tuple[str, list[float]]]:
    """Write the entries; return (id, stored vector) in fixture order."""
    writer = WriteService(store, embedder)
    stored = []
    for kind, summary, tags, body in entries:
        draft = EntryDraft(
            kind=Kind(kind),
            summary=summary,
            tags=tuple(tags),
            body=body,
            author="eval",
            agent="eval",
        )
        vector = (
            await cache.entry(draft) if cache is not None else await embedder.embed_entry(draft)
        )
        stored.append(((await writer.write(draft)).id, vector))
    return stored


async def measure(
    embedder: Embedder,
    fixture: Fixture | None = None,
    sweep: tuple[float, ...] = SWEEP,
    *,
    store: Store | None = None,
    cache: EmbedCache | None = None,
) -> Measurement:
    """Run the sweep on ``fixture`` (the golden one by default) with
    ``embedder``; see the module docstring.

    ``store`` must be empty; a ``MemoryStore`` by default. A scratch
    ``PgStore`` gives Postgres's keyword matching (never a real pool: the
    corpus is written into it). ``cache`` shares embeddings across the
    scenarios of one run."""
    fixture = fixture or golden_fixture()
    store = store or MemoryStore(make_clock())
    seeded = await _seed(embedder, store, fixture.entries, cache)
    ids = [eid for eid, _ in seeded]
    stored = dict(seeded)
    answerable = [(q, {ids[i] for i in rel}) for q, rel in fixture.answerable]

    async def similarities(query: str) -> dict[str, float]:
        vector = await cache.text(query) if cache is not None else await embedder.embed_text(query)
        return {eid: cosine_similarity(vector, emb) for eid, emb in stored.items()}

    relevant_sims = []
    for query, relevant in answerable:
        sims = await similarities(query)
        relevant_sims.append(max(sims[eid] for eid in relevant))
    off_sims = [max((await similarities(query)).values()) for query in fixture.off_topic]

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
        for query in fixture.off_topic:
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
        fixture=fixture.name,
        answerable=len(answerable),
        off_topic=len(fixture.off_topic),
        relevant_similarities=relevant_sims,
        off_topic_similarities=off_sims,
        rows=rows,
    )


def report(m: Measurement) -> str:
    """The measurement as Markdown, ready for docs/retrieval-experiments.md."""
    lines = [
        f"### {m.fixture}",
        "",
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


def _measure_fixture(
    fixture: Fixture,
    endpoint: str,
    model: str,
    dim: int,
    api_key: str,
) -> str:
    """Measure one fixture in its own process: the search work is CPU-bound
    (pure-Python cosine) and GIL-bound, so true parallelism needs one process
    per fixture. Each process builds its own embedder against the shared vLLM
    server and caches its embeddings."""
    from hivemind.config import Settings
    from hivemind.embeddings import build_embedder

    settings = Settings(
        embedding_endpoint=endpoint,
        embedding_model=model,
        embedding_dim=dim,
        embedding_api_key=api_key,
    )
    embedder = CachingEmbedder(build_embedder(settings))
    return report(asyncio.run(measure(embedder, fixture)))


def _main() -> None:
    from concurrent.futures import ProcessPoolExecutor

    from hivemind.config import endpoint_label, load_settings
    from hivemind.embeddings import EmbeddingError

    settings = load_settings()
    query_style = os.environ.get("EVAL_QUERY_STYLE", "vague").strip().lower()
    fixtures = extended_scenarios_specific() if query_style == "specific" else extended_scenarios()
    try:
        with ProcessPoolExecutor(max_workers=8) as pool:
            futures = [
                pool.submit(
                    _measure_fixture,
                    f,
                    str(settings.embedding_endpoint),
                    settings.embedding_model,
                    settings.embedding_dim,
                    settings.embedding_api_key,
                )
                for f in fixtures
            ]
            results = [f.result() for f in futures]
        print("\n\n".join(results))
    except EmbeddingError as exc:
        where = endpoint_label(settings.embedding_endpoint)
        raise SystemExit(f"embedder at {where} unavailable ({exc}); start it (make vllm)") from None


if __name__ == "__main__":
    _main()
