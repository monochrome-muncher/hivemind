"""Measure the narrow importance factor with a real embedder (ADR 0088).

``SearchConfig.narrow_importance`` replaces the importance factor
``0.5 + 0.1 * importance`` (0.6..1.0) with ``1 + 0.05 * (importance - 3)``
(0.9..1.1). It is off by default until this has been run with the embedder a
deployment uses. For each domain fixture (the sixteen ``EXTENDED_DOMAINS``
of ``tests/eval/corpus``, each alone as one fleet's pool, and all together)
it seeds the pool once per **importance assignment** and runs every
answerable query with the factor off and on:

* ``equal``: every entry importance 3. Both factors give 1.0 at 3, so off
  and on must rank identically (a sanity check of the machinery).
* ``random``: each entry gets a seeded random importance 1..5 independent of
  relevance: importance as noise, the realistic case of agents that rate
  nearly everything high or at random.
* ``aligned``: each query's answer entries get importance 4 or 5 (seeded),
  every other entry a random 1..5: the best case for the wide factor, where
  authors rate the useful entries higher.

Importance is derived from a SHA-256 of the assignment, a seed and the
entry's summary, so it is deterministic, independent of position, and the
same entry has the same importance in its domain and in the pooled fixture.
A per-domain fixture and the pooled one give an entry different ages (the
age follows the entry's position), so compare off against on within a
fixture, not across fixtures. Under ``aligned`` every answer entry gets 4 or
5, including answers that act as distractors for other queries.
Ages use the same deterministic 0 to 400 day spread as the ranking
measurement; there is no feedback, so the importance factor is the only
thing that differs between the off and on runs.

It reports hit@1, hit@5 and MRR per fixture and for the pooled one, off and
on, and the change. Set ``EVAL_IMPORTANCE_JSON`` to a path to also write
the rows as JSON.

Set ``EVAL_QUERY_INSTRUCTION`` to a literal instruction string to run the
whole measurement with ``SearchConfig.query_instruction`` on (ADR 0097): the
search embeds the wrapped query, (entries are embedded as before; this module embeds no query itself). Unset or empty is off; each report states
the instruction in use.

Run it against the deployment's embedder (the ``HIVEMIND_EMBEDDING_*``
settings, e.g. ``make vllm`` locally): ``make measure-importance``.
``tests/eval/test_importance_measurement.py`` runs it on the hash embedder,
which checks the machinery only; its numbers mean nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import timedelta
from pathlib import Path

from hivemind.config import SearchConfig
from hivemind.domain.entry import EntryDraft, ImportanceSource, Kind
from hivemind.memstore import MemoryStore
from hivemind.ports import Embedder, Store
from hivemind.retrieval.eval import hit_at_k, reciprocal_rank
from hivemind.services.search import SearchService
from hivemind.services.write import WriteService
from tests.eval.domains import Domain
from tests.eval.ranking import entry_age_days
from tests.eval.threshold import Fixture, combined_fixture, domain_fixture
from tests.fakes import FIXED_NOW, make_clock

K = 5
SEED = 20260601
ASSIGNMENTS = ("equal", "random", "aligned")
JSON_ENV = "EVAL_IMPORTANCE_JSON"


@dataclass(frozen=True, slots=True)
class Row:
    """One importance assignment over one fixture, with the factor off or on."""

    fixture: str
    assignment: str
    narrow: bool
    queries: int
    hit_at_1: float
    hit_at_5: float
    mrr: float


def _draw(label: str, summary: str, seed: int) -> int:
    """A deterministic number from the label, seed and summary."""
    return int.from_bytes(hashlib.sha256(f"{seed}:{label}:{summary}".encode()).digest()[:8], "big")


def importances(fixture: Fixture, assignment: str, seed: int = SEED) -> list[int]:
    """The importance (1..5) of each entry of ``fixture`` under ``assignment``."""
    if assignment not in ASSIGNMENTS:
        raise ValueError(f"unknown assignment {assignment!r}")
    if assignment == "equal":
        return [3] * len(fixture.entries)
    answers = {i for _query, relevant in fixture.answerable for i in relevant}
    values = []
    for index, entry in enumerate(fixture.entries):
        summary = entry[1]
        if assignment == "aligned" and index in answers:
            values.append(4 + _draw("answer", summary, seed) % 2)
        else:
            values.append(1 + _draw("noise", summary, seed) % 5)
    return values


async def seed_pool(
    embedder: Embedder, store: Store, fixture: Fixture, assignment: str
) -> list[str]:
    """Write the fixture's entries with their importance and spread ages.

    Returns the entry ids in fixture order. ``store`` must be empty.
    """
    writer = WriteService(store, embedder)
    ids: list[str] = []
    values = importances(fixture, assignment)
    for index, (kind, summary, tags, body) in enumerate(fixture.entries):
        importance = values[index]
        draft = EntryDraft(
            kind=Kind(kind),
            summary=summary,
            tags=list(tags),
            body=body,
            author="eval",
            agent="eval",
            occurred_at=FIXED_NOW - timedelta(days=entry_age_days(index)),
            importance=importance,
            importance_source=(
                ImportanceSource.CALLER if importance != 3 else ImportanceSource.DEFAULT
            ),
        )
        ids.append((await writer.write(draft)).id)
    return ids


async def measure(
    embedder: Embedder,
    fixture: Fixture,
    *,
    base: SearchConfig | None = None,
    query_instruction: str = "",
) -> list[Row]:
    """Every assignment over ``fixture``, each scored with the factor off, then on."""
    base = replace(
        base or SearchConfig(),
        default_limit=K,
        query_instruction=query_instruction,
        vector_min_similarity=None,  # measure the factor, not the cut-off (ADR 0100)
    )
    rows = []
    for assignment in ASSIGNMENTS:
        store = MemoryStore(make_clock())
        ids = await seed_pool(embedder, store, fixture, assignment)
        for narrow in (False, True):
            service = SearchService(
                store,
                embedder,
                replace(base, narrow_importance=narrow),
                now_fn=lambda: FIXED_NOW,
            )
            first = five = rr = 0.0
            for query, relevant_indices in fixture.answerable:
                relevant = {ids[i]: 1 for i in relevant_indices}
                ranked = [h.entry_id for h in await service.search(query, limit=K)]
                first += hit_at_k(ranked, relevant, 1)
                five += hit_at_k(ranked, relevant, K)
                rr += reciprocal_rank(ranked, relevant)
            n = len(fixture.answerable)
            rows.append(Row(fixture.name, assignment, narrow, n, first / n, five / n, rr / n))
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


def report(rows: Sequence[Row], model: str | None = None, query_instruction: str = "") -> str:
    """The measurement as Markdown: one line per fixture and assignment."""
    shown = f"`{query_instruction}`" if query_instruction else "none"
    lines = []
    if model:
        lines += [f"Model: `{model}`. Cells are hit@1 / hit@5 / MRR.", ""]
    lines += [f"Query instruction: {shown}", ""]
    lines += [
        "| fixture | importance | queries | narrow off | narrow on | change (hit@1 / hit@5 / MRR) |",
        "|---|---|---|---|---|---|",
    ]
    by_key: dict[tuple[str, str], dict[bool, Row]] = {}
    for row in rows:
        by_key.setdefault((row.fixture, row.assignment), {})[row.narrow] = row
    for (fixture, assignment), pair in by_key.items():
        off, on = pair[False], pair[True]
        lines.append(
            f"| {fixture} | {assignment} | {off.queries} "
            f"| {off.hit_at_1:.3f} / {off.hit_at_5:.3f} / {off.mrr:.3f} "
            f"| {on.hit_at_1:.3f} / {on.hit_at_5:.3f} / {on.mrr:.3f} "
            f"| {on.hit_at_1 - off.hit_at_1:+.3f} / {on.hit_at_5 - off.hit_at_5:+.3f} "
            f"/ {on.mrr - off.mrr:+.3f} |"
        )
    return "\n".join(lines)


def to_json(rows: Sequence[Row], model: str | None = None, query_instruction: str = "") -> str:
    payload = {
        "model": model,
        "query_instruction": query_instruction,
        "k": K,
        "rows": [asdict(r) for r in rows],
    }
    return json.dumps(payload, indent=2)


async def _main() -> None:
    from hivemind.config import endpoint_label, load_settings
    from hivemind.embeddings import EmbeddingError, build_embedder
    from tests.eval.threshold import CachingEmbedder

    settings = load_settings()
    query_instruction = os.environ.get("EVAL_QUERY_INSTRUCTION", "").strip()
    try:
        embedder = CachingEmbedder(build_embedder(settings))
        rows: list[Row] = []
        for fixture in fixtures():
            rows += await measure(embedder, fixture, query_instruction=query_instruction)
    except EmbeddingError as exc:
        where = endpoint_label(settings.embedding_endpoint)
        raise SystemExit(f"embedder at {where} unavailable ({exc}); start it (make vllm)") from None
    print(report(rows, embedder.model_name, query_instruction))
    if path := os.environ.get(JSON_ENV):
        Path(path).write_text(to_json(rows, embedder.model_name, query_instruction) + "\n")


if __name__ == "__main__":
    asyncio.run(_main())
