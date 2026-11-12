"""Measure a close-match bar for the search reply's loose note (ADR 0148).

For every fixture of the threshold measurement (``threshold.py``: each of the
sixteen domains alone as one fleet's pool, all of them together, the golden
set; vague and specific queries) it takes, per query, the similarity of the
best entry the vector stream could return, and reports how often a bar would
mark an answerable query loose and how often it would mark an unanswered one
loose. Unanswered queries split into the pool's own domain (hard) and the rest;
the all-domains pool counts every unanswered query as the rest.

The query instruction is on, as shipped. Run it against the deployment's
embedder (``HIVEMIND_EMBEDDING_*``): ``make measure-close-match``. Set
``EVAL_CACHE_DIR`` to keep vectors on disk between runs.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, replace

from hivemind.config import DEFAULT_QUERY_INSTRUCTION
from hivemind.domain.entry import EntryDraft, Kind
from hivemind.memstore import cosine_similarity
from hivemind.ports import Embedder
from tests.eval.domains import DOMAINS
from tests.eval.threshold import Fixture, extended_scenarios, extended_scenarios_specific
from tests.fakes import make_search_config

BARS = (0.50, 0.55, 0.60, 0.62, 0.65, 0.68, 0.70, 0.75)


@dataclass(frozen=True, slots=True)
class TopSimilarities:
    """Per query, the similarity of its nearest entry."""

    fixture: str
    answerable: list[float]
    unanswered_hard: list[float]
    unanswered_other: list[float]


async def measure(embedder: Embedder, fixture: Fixture) -> TopSimilarities:
    own = next((d.unanswered for d in DOMAINS if fixture.name.startswith(d.name)), ())
    config = replace(make_search_config(), query_instruction=DEFAULT_QUERY_INSTRUCTION)
    vectors = [
        await embedder.embed_entry(
            EntryDraft(
                kind=Kind(kind),
                summary=summary,
                tags=tuple(tags),
                body=body,
                author="eval",
                agent="eval",
            )
        )
        for kind, summary, tags, body in fixture.entries
    ]

    async def top(query: str) -> float:
        query_vector = await embedder.embed_text(config.embedded_query(query))
        return max(cosine_similarity(query_vector, v) for v in vectors)

    return TopSimilarities(
        fixture=fixture.name,
        answerable=[await top(q) for q, _ in fixture.answerable],
        unanswered_hard=[await top(q) for q in fixture.off_topic if q in own],
        unanswered_other=[await top(q) for q in fixture.off_topic if q not in own],
    )


def report(title: str, results: list[TopSimilarities]) -> str:
    answerable = [s for r in results for s in r.answerable]
    hard = [s for r in results for s in r.unanswered_hard]
    other = [s for r in results for s in r.unanswered_other]
    lines = [
        f"### {title}",
        "",
        f"{len(answerable)} answerable, {len(hard)} unanswered same-domain, {len(other)} other.",
        "",
        "| | median | 10th pct | 90th pct |",
        "|---|---|---|---|",
    ]
    for name, values in (
        ("answerable", answerable),
        ("unanswered, same domain", hard),
        ("unanswered, other", other),
    ):
        if values:
            lines.append(
                f"| {name} | {_pct(values, 0.5):.3f} | {_pct(values, 0.1):.3f} "
                f"| {_pct(values, 0.9):.3f} |"
            )
    lines += [
        "",
        "| bar | answerable marked loose | unanswered marked loose (same domain / other) |",
        "|---|---|---|",
    ]
    for bar in BARS:
        lines.append(
            f"| {bar:.2f} | {_share_below(answerable, bar)} "
            f"| {_share_below(hard, bar)} / {_share_below(other, bar)} |"
        )
    return "\n".join(lines)


def _pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[round(p * (len(ordered) - 1))]


def _share_below(values: list[float], bar: float) -> str:
    return f"{sum(v < bar for v in values) / len(values):.1%}" if values else "-"


def _groups() -> dict[str, list[Fixture]]:
    groups: dict[str, list[Fixture]] = {}
    for style, fixtures in (
        ("vague", extended_scenarios()),
        ("specific", extended_scenarios_specific()),
    ):
        for f in fixtures:
            pool = (
                "all domains in one pool"
                if f.name.startswith("all ")
                else ("golden" if f.name.startswith("golden") else "one-domain pools")
            )
            groups.setdefault(f"{pool}, {style} queries", []).append(f)
    return groups


async def _run() -> str:
    from hivemind.config import load_settings
    from hivemind.embeddings import build_embedder
    from tests.eval.embed_cache import DiskCachingEmbedder

    settings = load_settings()
    embedder: Embedder = build_embedder(settings)
    if cache_dir := os.environ.get("EVAL_CACHE_DIR"):
        embedder = DiskCachingEmbedder(embedder, cache_dir)
    sections = [f"Model: `{embedder.model_name}`, dim {embedder.dimension}."]
    for title, fixtures in _groups().items():
        sections.append(report(title, [await measure(embedder, f) for f in fixtures]))
    return "\n\n".join(sections)


if __name__ == "__main__":
    print(asyncio.run(_run()))
