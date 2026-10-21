"""Measure the Qwen3 query instruction with a real embedder (ADR 0097).

``SearchConfig.query_instruction`` wraps the search query sent to the vector
stream as ``Instruct: {task}\\nQuery:{query}``; entries are never embedded
with it. It is on by default (``DEFAULT_QUERY_INSTRUCTION``, the generic
instruction measured here); re-run this with the embedder a deployment uses. For each domain fixture (the sixteen ``EXTENDED_DOMAINS`` of
``tests/eval/corpus``, each alone as one fleet's pool, and all together) it
seeds the pool once and runs every answerable query with:

* no instruction (the baseline);
* the generic instruction ``GENERIC_INSTRUCTION``;
* each extra instruction in ``EVAL_QUERY_INSTRUCTIONS``, a JSON list of
  strings, for trying variants.

It reports hit@1, hit@5 and MRR per fixture and for the pooled one, and the
change against no instruction. For the pooled fixture it also reports, per
instruction, the mean and the 10th percentile of the cosine similarity
between a query and its best answer entry: the instruction shifts query
vectors, so ``HIVEMIND_VECTOR_MIN_SIMILARITY`` (ADR 0062; 0.45 is the
default, ADR 0100) may need re-checking. Set ``EVAL_QUERY_INSTRUCTION_JSON`` to
a path to also write the rows as JSON.

Entries are aged the same way as in the other measurements and there is no
feedback, so the instruction is the only thing that differs between runs.
Run it against the deployment's embedder (the ``HIVEMIND_EMBEDDING_*``
settings, e.g. ``make vllm`` locally): ``make measure-query-instruction``.
``tests/eval/test_query_instruction_measurement.py`` runs it on the hash
embedder, which checks the machinery only; its numbers mean nothing.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import timedelta
from pathlib import Path

from hivemind.config import DEFAULT_QUERY_INSTRUCTION, SearchConfig
from hivemind.domain.entry import EntryDraft, Kind
from hivemind.memstore import MemoryStore, cosine_similarity
from hivemind.ports import Embedder
from hivemind.retrieval.eval import hit_at_k, reciprocal_rank
from hivemind.services.search import SearchService
from hivemind.services.write import WriteService
from tests.eval.confirmation import entry_age_days
from tests.eval.domains import Domain
from tests.eval.threshold import Entry, Fixture, combined_fixture, domain_fixture
from tests.fakes import FIXED_NOW, make_clock

K = 5
GENERIC_INSTRUCTION = DEFAULT_QUERY_INSTRUCTION
EXTRA_ENV = "EVAL_QUERY_INSTRUCTIONS"
JSON_ENV = "EVAL_QUERY_INSTRUCTION_JSON"
NONE_LABEL = "none"


@dataclass(frozen=True, slots=True)
class Row:
    """One instruction over one fixture (``instruction`` is "" for none)."""

    fixture: str
    instruction: str
    queries: int
    hit_at_1: float
    hit_at_5: float
    mrr: float
    answer_similarity_mean: float
    answer_similarity_p10: float


def extra_instructions(raw: str | None) -> list[str]:
    """The instructions in ``EVAL_QUERY_INSTRUCTIONS`` (a JSON list of strings)."""
    if not raw or not raw.strip():
        return []
    value = json.loads(raw)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{EXTRA_ENV} must be a JSON list of strings")
    return value


def instructions(extra: Sequence[str] = ()) -> list[str]:
    """No instruction, the generic one, then the extras (duplicates dropped)."""
    out = ["", GENERIC_INSTRUCTION]
    for item in extra:
        if item not in out:
            out.append(item)
    return out


def percentile(values: Sequence[float], fraction: float) -> float:
    """The ``fraction`` quantile of ``values`` by linear interpolation."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _draft(entry: Entry, index: int) -> EntryDraft:
    kind, summary, tags, body = entry
    return EntryDraft(
        kind=Kind(kind),
        summary=summary,
        tags=list(tags),
        body=body,
        author="eval",
        agent="eval",
        occurred_at=FIXED_NOW - timedelta(days=entry_age_days(index)),
    )


async def measure(
    embedder: Embedder,
    fixture: Fixture,
    extra: Sequence[str] = (),
    *,
    base: SearchConfig | None = None,
) -> list[Row]:
    """``fixture`` seeded once, then every answerable query under each instruction."""
    base = replace(
        base or SearchConfig(),
        default_limit=K,
        query_instruction="",
        vector_min_similarity=None,  # compare instructions without the cut-off (ADR 0100)
    )
    store = MemoryStore(make_clock())
    writer = WriteService(store, embedder)
    drafts = [_draft(entry, i) for i, entry in enumerate(fixture.entries)]
    ids = [(await writer.write(draft)).id for draft in drafts]
    vectors = [await embedder.embed_entry(draft) for draft in drafts]

    rows = []
    for instruction in instructions(extra):
        config = replace(base, query_instruction=instruction)
        service = SearchService(store, embedder, config, now_fn=lambda: FIXED_NOW)
        first = five = rr = 0.0
        sims = []
        for query, relevant_indices in fixture.answerable:
            relevant = {ids[i]: 1 for i in relevant_indices}
            ranked = [h.entry_id for h in await service.search(query, limit=K)]
            first += hit_at_k(ranked, relevant, 1)
            five += hit_at_k(ranked, relevant, K)
            rr += reciprocal_rank(ranked, relevant)
            query_vector = await embedder.embed_text(config.embedded_query(query))
            sims.append(max(cosine_similarity(query_vector, vectors[i]) for i in relevant_indices))
        n = len(fixture.answerable)
        rows.append(
            Row(
                fixture.name,
                instruction,
                n,
                first / n,
                five / n,
                rr / n,
                sum(sims) / n,
                percentile(sims, 0.10),
            )
        )
    return rows


def fixtures(domains: Sequence[Domain] | None = None) -> list[Fixture]:
    """Pooled first, then each domain alone (the sixteen domains by default)."""
    if domains is None:
        from tests.eval.corpus import EXTENDED_DOMAINS

        domains = EXTENDED_DOMAINS
    pool = tuple(domains)
    return [
        combined_fixture(pool, f"all {len(pool)} domains together"),
        *(domain_fixture(d, pool) for d in pool),
    ]


def _label(instruction: str) -> str:
    if not instruction:
        return NONE_LABEL
    return "generic" if instruction == GENERIC_INSTRUCTION else f"`{instruction}`"


def report(rows: Sequence[Row], model: str | None = None) -> str:
    """The measurement as Markdown: one line per fixture and instruction, then
    the pooled answer similarity per instruction."""
    lines = []
    if model:
        lines += [f"Model: `{model}`. Cells are hit@1 / hit@5 / MRR.", ""]
    lines += [
        "| fixture | instruction | queries | hit@1 / hit@5 / MRR "
        "| change vs none (hit@1 / hit@5 / MRR) |",
        "|---|---|---|---|---|",
    ]
    baseline: dict[str, Row] = {r.fixture: r for r in rows if not r.instruction}
    for row in rows:
        base = baseline.get(row.fixture)
        change = (
            "baseline"
            if base is None or row is base
            else f"{row.hit_at_1 - base.hit_at_1:+.3f} / {row.hit_at_5 - base.hit_at_5:+.3f} "
            f"/ {row.mrr - base.mrr:+.3f}"
        )
        lines.append(
            f"| {row.fixture} | {_label(row.instruction)} | {row.queries} "
            f"| {row.hit_at_1:.3f} / {row.hit_at_5:.3f} / {row.mrr:.3f} | {change} |"
        )
    pooled = rows[0].fixture if rows else None
    lines += [
        "",
        f"Cosine similarity of the best answer to the query, `{pooled}` "
        "(check HIVEMIND_VECTOR_MIN_SIMILARITY against the 10th percentile):",
        "",
        "| instruction | mean | 10th percentile |",
        "|---|---|---|",
    ]
    for row in rows:
        if row.fixture == pooled:
            lines.append(
                f"| {_label(row.instruction)} | {row.answer_similarity_mean:.3f} "
                f"| {row.answer_similarity_p10:.3f} |"
            )
    return "\n".join(lines)


def to_json(rows: Sequence[Row], model: str | None = None) -> str:
    return json.dumps({"model": model, "k": K, "rows": [asdict(r) for r in rows]}, indent=2)


async def _main() -> None:
    from hivemind.config import endpoint_label, load_settings
    from hivemind.embeddings import EmbeddingError, build_embedder
    from tests.eval.threshold import CachingEmbedder

    settings = load_settings()
    extra = extra_instructions(os.environ.get(EXTRA_ENV))
    try:
        embedder = CachingEmbedder(build_embedder(settings))
        rows: list[Row] = []
        for fixture in fixtures():
            rows += await measure(embedder, fixture, extra)
    except EmbeddingError as exc:
        where = endpoint_label(settings.embedding_endpoint)
        raise SystemExit(f"embedder at {where} unavailable ({exc}); start it (make vllm)") from None
    print(report(rows, embedder.model_name))
    if path := os.environ.get(JSON_ENV):
        Path(path).write_text(to_json(rows, embedder.model_name) + "\n")


if __name__ == "__main__":
    asyncio.run(_main())
