"""Exact vs. HNSW on the golden set — the ADR 0025 measurement.

**A measurement, not a gate** (the `tests/eval/` experiment idiom, moved
to Postgres). Run it with ``-s`` to see the tables.

The ROADMAP §1.1 harness runs the golden set against ``MemoryStore`` —
no Postgres, no index, no ``<=>`` operator — so it is structurally
incapable of saying anything about migration ``0004``. This module is
the Postgres-backed counterpart: the *same* golden corpus and the *same*
golden queries (``tests/eval/golden.py``, untouched), seeded into a real
``PgStore`` and run through the real ``SearchService`` twice —

* **exact**: the pre-0004 behaviour, a sequential scan computing every
  row's true cosine distance;
* **hnsw**: migration ``0004``'s index under the shipped session
  settings (``hnsw.iterative_scan = strict_order``,
  ``hnsw.ef_search = 40``).

ADR 0025 moves the vector stream from **exact to approximate** nearest
neighbours. That is a behaviour change, not a pure speed win, and this
is the evidence about it.

**What it measured: the two do NOT agree on this fixture.** The exact
arm scores 1.000 on hit@5, MRR and nDCG@5 every run. The HNSW arm scores
0.375-0.625, and the vector stream recovers 0.69-0.85 of the exact
top-10, worsening as the corpus grows (0.850 at 2k distractors, 0.700 at
20k).

The miss is **graph reachability, not candidate budget**. The
``ef_search`` sweep plateaus below parity — 0.688 -> 0.938 from 40 to
800 at 20k, still short of 1.000 — and a direct check found the index
returning the 1st and 3rd-through-11th nearest neighbours while omitting
the 2nd entirely (d = 0.838 against a cloud at 0.89-0.95). A larger
candidate budget cannot reach a vertex with no in-edges, which is why
nothing here is tuned and the shipped ``ef_search`` stays at pgvector's
default.

ADR 0025 carries the numbers, the caveat that this fixture is close to
the worst case for a graph index (uniformly distributed
high-dimensional hash vectors, relevant entries as isolated outliers —
real embeddings cluster, which is what HNSW navigates by), and the
re-measure trigger on a real corpus.

So the equality assertions this module would like to make are not
written: they were measured to be false, and a permanently-red test
teaches nothing. What *is* asserted is what held on every run: the two
arms really execute different plans, the exact arm reproduces the golden
ordering (so a failure here is the harness, not the index), the HNSW arm
never beats ground truth, and — the property ``strict_order`` was chosen
for — the filtered vector stream is never starved.

**How the two arms are forced.** Not by dropping and recreating the
index (that mutates a shipped schema object mid-suite and leaves the
pool wrong if a test dies), but by appending one planner GUC to the
shipped ``VECTOR_SEARCH_SETTINGS`` — ``enable_indexscan = off`` for the
exact arm, ``enable_seqscan = off`` for the HNSW arm. Everything else
about the query is byte-identical to production, and
``test_the_two_arms_really_run_different_plans`` proves via ``EXPLAIN``
that each arm got the plan it claims. Forcing is necessary because
**Postgres does not choose the HNSW index at small table sizes**:
measured on the dev pool, it keeps the sequential scan at ~21k rows and
switches at ~31k (pgvector's HNSW cost estimate has a high *startup*
cost that a small table's seq-scan total never exceeds) — even though
the index scan runs ~25x faster there.

**Not deterministic.** HNSW's level assignment is randomised and not
seeded, so every build is a different graph and the HNSW column moves
run to run. The exact column does not.

**Corpus.** The golden 8, interleaved into ``FILLER`` distractors so the
index has a real graph to get lost in (8 rows would be vacuous — HNSW
would return everything) and so insertion order is not adversarial by
construction. ``VECTOR_INDEX_EVAL_FILLER`` overrides the count —
deliberately *not* a ``HIVEMIND_*`` name, so ADR 0024's unknown-variable
check never sees it.

Skips cleanly when Postgres is unreachable (the ``tests/integration/``
convention).
"""

from __future__ import annotations

import hashlib
import os
import random
import statistics

import asyncpg
import pytest
from tests.eval.golden import golden_corpus, golden_queries
from tests.fakes import make_search_config

from hivemind.config import Settings
from hivemind.domain.entry import EntryDraft, EntryFilters, Kind, embeddable_text
from hivemind.ports import entry_embeddable_text
from hivemind.retrieval.eval import aggregate_report, evaluate_query
from hivemind.services.governance import WriteService
from hivemind.services.search import SearchService
from hivemind.store import PgStore
from hivemind.store import pgstore as pgstore_module
from hivemind.store.migrate import migrate

INDEX_NAME = "entries_embedding_hnsw_idx"

# The shipped settings, captured at import so an arm always composes
# against the production value and never against another arm's patch.
BASE_SETTINGS = pgstore_module.VECTOR_SEARCH_SETTINGS

K = 5  # the golden set's k, mirroring tests/eval/test_eval_gate.py
FILLER = int(os.environ.get("VECTOR_INDEX_EVAL_FILLER", "2000"))

# The two arms, as a suffix appended to BASE_SETTINGS. The HNSW arm has to
# disable the *sort* as well as the sequential scan: with only
# `enable_seqscan = off`, Postgres satisfies the query at ~20k rows by
# scanning `entries_state_idx` and sorting — which is still EXACT, so the
# two arms would silently become one. Nothing but an ordered HNSW scan can
# produce the ORDER BY without a Sort node.
EXACT_ARM = "SET LOCAL enable_indexscan = off; SET LOCAL enable_bitmapscan = off;"
HNSW_ARM = "SET LOCAL enable_seqscan = off; SET LOCAL enable_sort = off;"

# The distractors' vocabulary: unrelated to any golden query's terms, so a
# distractor is never accidentally relevant.
_VOCABULARY = [
    "alpha",
    "bravo",
    "cirrus",
    "delta",
    "ember",
    "falcon",
    "gamut",
    "harbor",
    "ingot",
    "jasper",
    "kelvin",
    "lumen",
    "marlin",
    "nimbus",
    "onyx",
    "pylon",
    "quartz",
    "raster",
    "sable",
    "tundra",
    "umber",
    "vertex",
    "willow",
    "xenon",
    "yarrow",
    "zephyr",
    "basalt",
    "cinder",
    "dune",
    "eddy",
    "flint",
    "granite",
    "hollow",
    "iris",
    "juniper",
    "kestrel",
    "larch",
    "mesa",
    "nettle",
    "opal",
    "peregrine",
    "quill",
    "rookery",
    "saffron",
    "thistle",
    "vellum",
    "wicket",
]


class HashEmbedder:
    """``tests.fakes.FakeEmbedder``'s construction, widened to full rank.

    ``FakeEmbedder`` indexes its sha256 digest as ``digest[(i * 3) % 32]``,
    which repeats with period 32 — at 1024 dimensions that puts every
    vector on a 32-dimensional subspace, which is the wrong geometry to
    measure a high-dimensional ANN index in. This hashes ``(block, token)``
    per 32-dimension block instead, so all ``dimension`` coordinates are
    independent, while the token-overlap semantics (shared tokens ->
    similar vectors, identical text -> identical vectors) are unchanged.
    Deterministic; no network.
    """

    def __init__(self, dimension: int) -> None:
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def model_name(self) -> str:
        return "hash-embedder"

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self._dimension
        for token in text.lower().split():
            for block in range((self._dimension + 31) // 32):
                digest = hashlib.sha256(f"{block}:{token}".encode()).digest()
                for offset in range(32):
                    i = block * 32 + offset
                    if i >= self._dimension:
                        break
                    byte = digest[offset]
                    sign = 1.0 if byte % 2 == 0 else -1.0
                    vec[i] += sign * (byte / 255.0)
        norm = (sum(v * v for v in vec) ** 0.5) or 1.0
        return [v / norm for v in vec]

    async def embed_text(self, text: str) -> list[float]:
        return self._vector(text)

    async def embed_entry(self, draft: EntryDraft) -> list[float]:
        return self._vector(entry_embeddable_text(draft))

    def entry_embeddable_text(self, draft: EntryDraft) -> str:
        return embeddable_text(draft.summary, draft.body)


async def _truncate(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("TRUNCATE credentials, feedbacks, entries")
    finally:
        await conn.close()


async def _index_exists(dsn: str) -> bool:
    conn = await asyncpg.connect(dsn)
    try:
        return bool(await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", INDEX_NAME))
    finally:
        await conn.close()


async def _analyze(dsn: str) -> None:
    """Refresh planner statistics after seeding.

    Without it the planner still believes the table is empty and every
    plan here is an artefact of stale statistics rather than of the index.
    """
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("ANALYZE entries")
    finally:
        await conn.close()


def _seed_drafts() -> tuple[list[EntryDraft], list[int]]:
    """The seeding order: ``FILLER`` distractors with the golden corpus
    interleaved. Returns the drafts and the positions of the golden ones."""
    rng = random.Random(20251)
    drafts = [
        EntryDraft(
            kind=Kind.FACT,
            summary=f"note {i} " + " ".join(rng.sample(_VOCABULARY, 10)),
            author="eval",
            agent="eval-agent",
            tags=("distractor",),
        )
        for i in range(FILLER)
    ]
    golden = [
        EntryDraft(
            kind=Kind(kind),
            summary=summary,
            author="eval",
            agent="eval-agent",
            tags=tuple(tags),
        )
        for kind, summary, tags in golden_corpus()
    ]
    step = max(FILLER // len(golden), 1)
    positions: list[int] = []
    for j, draft in enumerate(golden):
        at = min(j * step + j, len(drafts))
        drafts.insert(at, draft)
        positions.append(at)
    return drafts, positions


@pytest.fixture
async def corpus():
    """A seeded ``PgStore``: the golden corpus interleaved with distractors.

    Yields ``(store, embedder, golden_ids)``, where ``golden_ids`` is the
    golden corpus's entry ids in *corpus* order, so a golden query's
    relevant indices map straight to ids.
    """
    settings = Settings()
    dsn, dim = settings.database_url, settings.embedding_dim
    try:
        probe = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # connection refused / timeout / auth
        pytest.skip(f"Postgres unreachable at {dsn}: {exc}")
    await probe.close()

    await migrate(dsn, dim)
    if not await _index_exists(dsn):
        pytest.skip(f"{INDEX_NAME} is absent — migration 0004 has not been applied")
    await _truncate(dsn)

    store = PgStore(dsn)
    embedder = HashEmbedder(dim)
    writer = WriteService(store, embedder)
    try:
        drafts, positions = _seed_drafts()
        ids = [(await writer.write(draft)).id for draft in drafts]
        await _analyze(dsn)
        yield store, embedder, [ids[at] for at in positions]
    finally:
        await _truncate(dsn)
        await store.close()


async def _run_arm(monkeypatch, store, embedder, golden_ids, arm: str):
    """Run the golden query set with ``arm``'s planner GUC appended to the
    shipped vector-search settings.

    Returns ``(per_query_metrics, aggregate_report, vector_stream)`` —
    the last being the raw ``search_vector`` ids per query, which is where
    an approximation actually bites (the end-to-end metrics fuse two
    streams, so a vector miss can hide behind the keyword stream).
    """
    monkeypatch.setattr(pgstore_module, "VECTOR_SEARCH_SETTINGS", BASE_SETTINGS + arm)
    config = make_search_config()
    service = SearchService(store, embedder, config)
    per_query: dict[str, dict] = {}
    vector_stream: dict[str, list[str]] = {}
    for query, relevant_indices in golden_queries():
        hits = await service.search(query, limit=K)
        relevant = {golden_ids[i]: 1 for i in relevant_indices}
        per_query[query] = evaluate_query([h.entry_id for h in hits], relevant, K)
        vector_stream[query] = await store.search_vector(
            await embedder.embed_text(query), EntryFilters(), config.candidate_top_k
        )
    return per_query, aggregate_report(per_query, K), vector_stream


def _set_recall(exact: dict[str, list[str]], approx: dict[str, list[str]]) -> float:
    """Mean fraction of the exact top-k ids the approximate stream also
    returned — the recall number, insensitive to re-ordering within the set."""
    return statistics.mean(
        len(set(exact[q]) & set(approx[q])) / max(len(exact[q]), 1) for q in exact
    )


def _render(rows: list[tuple[str, ...]]) -> str:
    widths = [max(len(r[c]) for r in rows) for c in range(len(rows[0]))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)) for r in rows)


def _metric_table(exact: dict[str, float], hnsw: dict[str, float]) -> str:
    rows: list[tuple[str, ...]] = [("metric", "exact scan", "HNSW strict_order", "delta")]
    for key in ("hit_at_k", "mrr", f"ndcg_at_{K}"):
        rows.append(
            (key, f"{exact[key]:.4f}", f"{hnsw[key]:.4f}", f"{hnsw[key] - exact[key]:+.4f}")
        )
    return _render(rows)


class TestExactVersusHnsw:
    """The ADR 0025 evidence."""

    async def test_measure_the_golden_set_under_both_plans(self, monkeypatch, corpus) -> None:
        """Report hit@k / MRR / nDCG for both arms, and the vector stream's
        set-recall.

        Equality is **not** asserted — it was measured to be false (see the
        module docstring). What is asserted is that the exact arm is sound
        (it reproduces the golden ordering, so a failure here indicts the
        harness rather than the index) and that the approximation never
        *beats* ground truth, which would mean the arms were mislabelled.
        """
        store, embedder, golden_ids = corpus
        _, exact, exact_stream = await _run_arm(monkeypatch, store, embedder, golden_ids, EXACT_ARM)
        _, hnsw, hnsw_stream = await _run_arm(monkeypatch, store, embedder, golden_ids, HNSW_ARM)

        top_k = make_search_config().candidate_top_k
        print(f"\nADR 0025 — golden set, {len(golden_ids)} golden + {FILLER} distractors\n")
        print(_metric_table(exact, hnsw))
        print(
            f"\nvector stream set-recall@{top_k}: "
            f"{_set_recall(exact_stream, hnsw_stream):.3f}  "
            f"(identical ordering on "
            f"{sum(1 for q in exact_stream if exact_stream[q] == hnsw_stream[q])}"
            f"/{len(exact_stream)} queries)\n"
        )
        for key in ("hit_at_k", "mrr", f"ndcg_at_{K}"):
            assert exact[key] == 1.0, (
                f"the exact arm did not reproduce the golden ordering ({key}="
                f"{exact[key]:.4f}). That is a harness fault, not an index one."
            )
            assert hnsw[key] <= exact[key], (
                f"the approximate arm beat the exact one on {key} — the arms are "
                f"mislabelled, or the 'exact' arm is not running a sequential scan."
            )

    async def test_strict_order_never_starves_the_filtered_vector_stream(
        self, monkeypatch, corpus
    ) -> None:
        """The property ``hnsw.iterative_scan = strict_order`` was chosen for.

        Every query here carries ``state = 'active'`` (and production adds
        the ADR 0011 visibility matrix on top). Without iterative scan HNSW
        fetches ``ef_search`` candidates and filters *afterwards*, so a
        narrow reader can get back far fewer than ``candidate_top_k`` rows
        and RRF fuses against a short second list. With it, the scan resumes
        until the limit is met.
        """
        store, embedder, golden_ids = corpus
        _, _, stream = await _run_arm(monkeypatch, store, embedder, golden_ids, HNSW_ARM)
        top_k = make_search_config().candidate_top_k
        short = {q: len(ids) for q, ids in stream.items() if len(ids) != top_k}
        assert not short, f"the HNSW vector stream came back short of {top_k}: {short}"

    async def test_raising_ef_search_does_not_buy_back_the_exact_ranking(
        self, monkeypatch, corpus
    ) -> None:
        """Why ``ef_search`` is left at pgvector's default.

        The obvious reaction to the recall gap above is to raise
        ``ef_search`` until it closes. It does not close: the misses are
        vertices the graph cannot reach from its entry point, and a larger
        candidate budget cannot reach a vertex with no in-edges. This sweeps
        the knob so the next reader sees that before reaching for it.
        """
        store, embedder, golden_ids = corpus
        _, _, exact_stream = await _run_arm(monkeypatch, store, embedder, golden_ids, EXACT_ARM)

        rows: list[tuple[str, ...]] = [("ef_search", "set-recall@top_k")]
        recalls: dict[int, float] = {}
        for ef_search in (40, 100, 200, 400, 800):
            arm = f"{HNSW_ARM} SET LOCAL hnsw.ef_search = {ef_search};"
            _, _, stream = await _run_arm(monkeypatch, store, embedder, golden_ids, arm)
            recalls[ef_search] = _set_recall(exact_stream, stream)
            rows.append((str(ef_search), f"{recalls[ef_search]:.3f}"))
        print("\nef_search sweep (the shipped value is 40)\n")
        print(_render(rows))
        print()
        assert set(recalls) == {40, 100, 200, 400, 800}


class TestTheArmsAreReal:
    async def test_the_two_arms_really_run_different_plans(self, corpus) -> None:
        """Without this, both arms could be the same plan and every
        comparison above would be a tautology."""
        _store, embedder, _golden_ids = corpus
        dsn = Settings().database_url
        vector = await embedder.embed_text("database connection pooling pgbouncer")
        sql = (
            "EXPLAIN SELECT id FROM entries WHERE state = 'active' AND embedding IS NOT NULL "
            "ORDER BY embedding <=> $1 LIMIT 20"
        )
        conn = await asyncpg.connect(dsn)
        try:
            from pgvector.asyncpg import register_vector

            await register_vector(conn)
            plans: dict[str, str] = {}
            for label, arm in (("exact", EXACT_ARM), ("hnsw", HNSW_ARM)):
                transaction = conn.transaction()
                await transaction.start()
                await conn.execute(BASE_SETTINGS + arm)
                rows = await conn.fetch(sql, vector)
                await transaction.commit()
                # Node names only: a plan line carries the whole 1024-dim
                # query vector, which makes an assertion message unreadable.
                plans[label] = "\n".join(
                    r[0].split("  (cost")[0].strip() for r in rows if "(cost=" in r[0]
                )
        finally:
            await conn.close()

        assert "Seq Scan on entries" in plans["exact"], plans["exact"]
        assert INDEX_NAME not in plans["exact"], plans["exact"]
        assert f"Index Scan using {INDEX_NAME}" in plans["hnsw"], plans["hnsw"]
