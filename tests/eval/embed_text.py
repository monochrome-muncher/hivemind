"""Measure what goes into the embedded text with a real embedder (ROADMAP §4.2(a)).

Today an entry is embedded from ``summary`` + a bounded ``body`` prefix
(``embeddable_text``, SPEC §7). This measures whether adding the other fields
an entry carries — its ``kind`` and its ``tags`` (the corpus's proxy for the
extracted entity names, ADR 0016) — to that text helps retrieval or just adds
noise to one shared vector. Chunking (several vectors per entry) is a separate
decision (SPEC §4.1 commits to one embedding per entry) and is out of scope.

For each domain fixture (the sixteen ``EXTENDED_DOMAINS`` of
``tests/eval/corpus``, each alone as one fleet's pool, and all together) it
seeds the pool once per text variant and runs every answerable query with:

* ``baseline`` — ``summary`` + bounded ``body`` (today's text);
* ``+kind`` — the kind label added to the summary;
* ``+tags`` — the tags added to the summary (the entity-name proxy);
* ``+kind+tags`` — both.

The query side is unchanged (no query instruction, no cut-off), so the entry
text is the only thing that differs between runs. It reports hit@1, hit@5 and
MRR per fixture and for the pooled one, and the change against baseline. Set
``EVAL_EMBED_TEXT_VARIANTS`` to a JSON list to try other compositions (a label
is any string; the known ones are built in, an unknown one falls back to
baseline text), and ``EVAL_EMBED_TEXT_JSON`` to a path to also write the rows
as JSON.

Entries are aged the same way as the other measurements and there is no
feedback. Run it against the deployment's embedder (the ``HIVEMIND_EMBEDDING_*``
settings, e.g. ``make vllm`` locally): ``make measure-embed-text``.
``tests/eval/test_embed_text_measurement.py`` runs it on the hash embedder,
which checks the machinery only; its numbers mean nothing.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import timedelta

from hivemind.config import SearchConfig
from hivemind.domain.entry import EntryDraft, Kind, embeddable_text
from hivemind.memstore import MemoryStore
from hivemind.ports import Embedder
from hivemind.retrieval.eval import hit_at_k, reciprocal_rank
from hivemind.services.search import SearchService
from hivemind.services.write import WriteService
from tests.eval.domains import Domain
from tests.eval.ranking import entry_age_days
from tests.eval.threshold import Entry, Fixture, combined_fixture, domain_fixture
from tests.fakes import FIXED_NOW, make_clock

K = 5
BASELINE = "baseline"
VARIANTS = (BASELINE, "+kind", "+tags", "+kind+tags")
EXTRA_ENV = "EVAL_EMBED_TEXT_VARIANTS"
JSON_ENV = "EVAL_EMBED_TEXT_JSON"


@dataclass(frozen=True, slots=True)
class Row:
    """One text variant over one fixture (``variant`` is ``baseline`` for none)."""

    fixture: str
    variant: str
    queries: int
    hit_at_1: float
    hit_at_5: float
    mrr: float


def variant_text(
    kind: str, summary: str, tags: Sequence[str], body: str | None, variant: str
) -> str:
    """The entry text for ``variant``: ``summary`` (+ kind / tags) whole, plus
    the same bounded ``body`` prefix ``embeddable_text`` gives baseline.
    """
    parts = [summary]
    if variant in ("+kind", "+kind+tags"):
        parts.append(f"kind: {kind}")
    if variant in ("+tags", "+kind+tags") and tags:
        parts.append(f"tags: {', '.join(tags)}")
    return embeddable_text("\n".join(parts), body)


class VariantEmbedder:
    """An embedder that embeds entries from a variant text (ROADMAP §4.2(a)).

    Queries are embedded unchanged (delegated to ``inner``); only the entry
    text differs, so the entry text is the only thing that varies between
    runs.
    """

    def __init__(self, inner: Embedder, variant: str) -> None:
        self._inner = inner
        self._variant = variant

    @property
    def dimension(self) -> int:
        return self._inner.dimension

    @property
    def model_name(self) -> str:
        return self._inner.model_name

    async def embed_text(self, text: str) -> list[float]:
        return await self._inner.embed_text(text)

    def entry_embeddable_text(self, draft: EntryDraft) -> str:
        return variant_text(
            draft.kind.value, draft.summary, tuple(draft.tags), draft.body, self._variant
        )

    async def embed_entry(self, draft: EntryDraft) -> list[float]:
        return await self._inner.embed_text(self.entry_embeddable_text(draft))


def _draft(entry: Entry, index: int) -> EntryDraft:
    kind, summary, tags, body = entry
    return EntryDraft(
        kind=Kind(kind),
        summary=summary,
        tags=tuple(tags),
        body=body,
        author="eval",
        agent="eval",
        occurred_at=FIXED_NOW - timedelta(days=entry_age_days(index)),
    )


async def measure(
    embedder: Embedder,
    fixture: Fixture,
    variants: Sequence[str] = VARIANTS,
    *,
    base: SearchConfig | None = None,
) -> list[Row]:
    """``fixture`` seeded once per variant, then every answerable query."""
    base = replace(
        base or SearchConfig(),
        default_limit=K,
        query_instruction="",
        vector_min_similarity=None,  # compare variants without the cut-off (ADR 0100)
    )
    rows = []
    for variant in variants:
        store = MemoryStore(make_clock())
        writer = WriteService(store, VariantEmbedder(embedder, variant))
        drafts = [_draft(entry, i) for i, entry in enumerate(fixture.entries)]
        ids = [(await writer.write(draft)).id for draft in drafts]
        service = SearchService(store, embedder, base, now_fn=lambda: FIXED_NOW)
        first = five = rr = 0.0
        for query, relevant_indices in fixture.answerable:
            relevant = {ids[i]: 1 for i in relevant_indices}
            ranked = [h.entry_id for h in await service.search(query, limit=K)]
            first += hit_at_k(ranked, relevant, 1)
            five += hit_at_k(ranked, relevant, K)
            rr += reciprocal_rank(ranked, relevant)
        n = len(fixture.answerable)
        rows.append(Row(fixture.name, variant, n, first / n, five / n, rr / n))
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


def extra_variants(raw: str | None) -> list[str]:
    """``EVAL_EMBED_TEXT_VARIANTS`` as a JSON list of extra labels (none)."""
    if raw is None or not raw.strip():
        return []
    try:
        variants = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{EXTRA_ENV} must be a JSON list of strings") from exc
    if not isinstance(variants, list) or not all(isinstance(v, str) for v in variants):
        raise ValueError(f"{EXTRA_ENV} must be a JSON list of strings")
    return variants


def all_variants(extra: Sequence[str] = ()) -> tuple[str, ...]:
    """The built-in variants, then each extra not already present."""
    seen = list(VARIANTS)
    return tuple(seen + [v for v in extra if v not in seen])


def report(rows: Sequence[Row], model: str | None = None) -> str:
    """The measurement as Markdown: one line per fixture and variant, with the
    change against that fixture's baseline."""
    lines = []
    if model:
        lines += [f"Model: `{model}`. Cells are hit@1 / hit@5 / MRR.", ""]
    lines += [
        "| fixture | embedded text | queries | hit@1 / hit@5 / MRR "
        "| change vs baseline (hit@1 / hit@5 / MRR) |",
        "|---|---|---|---|---|",
    ]
    baseline: dict[str, Row] = {r.fixture: r for r in rows if r.variant == BASELINE}
    for row in rows:
        base = baseline.get(row.fixture)
        if row.variant == BASELINE or base is None:
            change = "baseline"
        else:
            change = (
                f"{row.hit_at_1 - base.hit_at_1:+.3f} / "
                f"{row.hit_at_5 - base.hit_at_5:+.3f} / "
                f"{row.mrr - base.mrr:+.3f}"
            )
        lines.append(
            f"| {row.fixture} | `{row.variant}` | {row.queries} "
            f"| {row.hit_at_1:.3f} / {row.hit_at_5:.3f} / {row.mrr:.3f} | {change} |"
        )
    return "\n".join(lines)


def to_json(rows: Sequence[Row], model: str | None = None) -> str:
    return json.dumps({"model": model, "k": K, "rows": [asdict(r) for r in rows]}, indent=2)


async def _main() -> None:
    from hivemind.config import endpoint_label, load_settings
    from hivemind.embeddings import EmbeddingError, build_embedder
    from tests.eval.threshold import CachingEmbedder

    settings = load_settings()
    variants = all_variants(extra_variants(os.environ.get(EXTRA_ENV)))
    try:
        embedder = CachingEmbedder(build_embedder(settings))
        rows: list[Row] = []
        for fixture in fixtures():
            rows += await measure(embedder, fixture, variants)
    except EmbeddingError as exc:
        where = endpoint_label(settings.embedding_endpoint)
        raise SystemExit(f"embedder at {where} unavailable ({exc}); start it (make vllm)") from None
    print(report(rows, embedder.model_name))
    if path := os.environ.get(JSON_ENV):
        from pathlib import Path

        Path(path).write_text(to_json(rows, embedder.model_name) + "\n")


if __name__ == "__main__":
    asyncio.run(_main())
