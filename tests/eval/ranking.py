"""Measure ranking variants against a real embedder.

The variants are the per-search recency hint (ADR 0073: ``ignore_age`` and
``prefer_recent``, each applied to every query because the domain fixtures do
not label time sense; the labelled oracle run is
``tests/eval/test_recency_hint.py``) against the shipped ranking. The hint is
on by default (ADR 0101) and acts only when a query carries it. (The
confirmation clock of ADR 0071 and the recency profile per kind of ADR 0072
were measured here and removed, ADR 0103.) For each domain fixture
(``domains.py``: five kinds of work, 32 entries each) and for all of them
pooled, it:

* seeds the pool with entries aged 0 to 400 days (a golden-ratio spread,
  so the ages are deterministic and independent of the text), so the
  recency floor matters;
* sprinkles a ``helpful`` report with a note on a deterministic 20% of the
  entries (unchanged from the recorded measurements);
* runs every answerable query once per **variant** (a named set of
  ``SearchConfig`` overrides and a hint) over the same seeded pool, and
  reports hit@5 and MRR per variant, with the change against the first
  variant.

A new ranking change adds a ``Variant`` to ``VARIANTS`` and is measured over
the same fixtures.

The pass rule from the study (docs/retrieval-experiments.md): no hit@5 or
MRR drop beyond 0.01 in any domain.

Run it against the deployment's embedder (the ``HIVEMIND_EMBEDDING_*``
settings, e.g. ``make vllm`` locally): ``make measure-ranking``.
``tests/eval/test_ranking_measurement.py`` runs it on the hash embedder, which
checks the machinery only; its numbers mean nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

from hivemind.config import SearchConfig
from hivemind.domain.entry import EntryDraft, Kind
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.domain.recency import RecencyHint
from hivemind.memstore import MemoryStore
from hivemind.ports import Embedder, Store
from hivemind.retrieval.eval import hit_at_k, reciprocal_rank
from hivemind.services.governance import WriteService
from hivemind.services.search import SearchService
from tests.eval.domains import DOMAINS
from tests.eval.threshold import Fixture, combined_fixture, domain_fixture
from tests.fakes import FIXED_NOW, make_clock

K = 5
FRACTION = 0.2
MAX_AGE_DAYS = 400.0
# Confirmations are dated this many days before the fixed clock, at most.
CONFIRM_MIN_DAYS = 0.5
CONFIRM_MAX_DAYS = 5.0
# 1 / golden ratio: i * PHI mod 1 spreads any prefix of 0, 1, 2, ... evenly.
_PHI = 0.6180339887498949
# The study's pass rule: a drop beyond this in hit@5 or MRR is a failure.
MAX_DROP = 0.01


@dataclass(frozen=True, slots=True)
class Variant:
    """A named set of ``SearchConfig`` overrides, measured over the same pool."""

    name: str
    overrides: Mapping[str, Any]
    # The per-search hint sent with every query of the variant (ADR 0073).
    recency: RecencyHint = RecencyHint.DEFAULT


# The first variant is the baseline the others are compared with.
VARIANTS: tuple[Variant, ...] = (
    Variant("shipped (no hint)", {}),
    Variant(
        "recency hint: ignore_age (all queries)",
        {"recency_hint_enabled": True},
        RecencyHint.IGNORE_AGE,
    ),
    Variant(
        "recency hint: prefer_recent (all queries)",
        {"recency_hint_enabled": True},
        RecencyHint.PREFER_RECENT,
    ),
)


@dataclass(frozen=True, slots=True)
class Row:
    """One variant over one fixture."""

    fixture: str
    variant: str
    queries: int
    hit_at_k: float
    mrr: float


def entry_age_days(index: int) -> float:
    """Entry ``index``'s age, 0 to 400 days: deterministic, text-independent."""
    return round(MAX_AGE_DAYS * ((index * _PHI) % 1.0), 1)


def confirmation_ago_days(ordinal: int) -> float:
    """How long before the clock the ``ordinal``-th confirmation was made."""
    return round(
        CONFIRM_MIN_DAYS + (CONFIRM_MAX_DAYS - CONFIRM_MIN_DAYS) * ((ordinal * _PHI) % 1.0), 2
    )


def pick_fraction(summaries: Sequence[str], fraction: float = FRACTION) -> list[int]:
    """The indices of a deterministic ``fraction`` of the entries: the ones
    whose summary has the smallest SHA-256, so the choice is independent of
    position, order and the embedder, and stable under reruns."""
    count = round(len(summaries) * fraction)
    ranked = sorted(
        range(len(summaries)), key=lambda i: hashlib.sha256(summaries[i].encode()).hexdigest()
    )
    return sorted(ranked[:count])


async def seed(
    embedder: Embedder, store: Store, fixture: Fixture, *, confirm_fraction: float = FRACTION
) -> list[str]:
    """Write the fixture's entries with spread ages, then the helpful reports.

    A deterministic 20% of the entries get a ``helpful`` report with a note
    from a user who is not the author, as in every recorded measurement
    (ADR 0101's hint figures included); nothing ranks on their date any more
    (ADR 0103), but they still lift ``quality``, so the fixture stays the same.

    Returns the entry ids in fixture order. ``store`` must be empty.
    """
    writer = WriteService(store, embedder)
    ids: list[str] = []
    for index, (kind, summary, tags, body) in enumerate(fixture.entries):
        draft = EntryDraft(
            kind=Kind(kind),
            summary=summary,
            tags=list(tags),
            body=body,
            author="eval",
            agent="eval",
            occurred_at=FIXED_NOW - timedelta(days=entry_age_days(index)),
        )
        ids.append((await writer.write(draft)).id)
    picked = pick_fraction([e[1] for e in fixture.entries], confirm_fraction)
    for ordinal, index in enumerate(picked):
        await store.record_feedback(
            Feedback(
                ids[index],
                "eval-confirmer",
                "eval-confirmer-agent",
                Verdict.HELPFUL,
                note="re-checked against the live system",
                updated_at=FIXED_NOW - timedelta(days=confirmation_ago_days(ordinal)),
            )
        )
    return ids


def fixtures() -> list[Fixture]:
    """Pooled first, then each domain alone (what ``make measure-ranking`` runs)."""
    return [combined_fixture(), *(domain_fixture(d) for d in DOMAINS)]


async def measure(
    embedder: Embedder,
    fixture: Fixture,
    variants: Sequence[Variant] = VARIANTS,
    *,
    store: Store | None = None,
    base: SearchConfig | None = None,
) -> list[Row]:
    """Seed ``fixture`` once and run every variant's config over its answerable queries.

    ``store`` must be empty (a ``MemoryStore`` by default; a scratch
    ``PgStore`` works, never a real pool). ``base`` is the shipped
    ``SearchConfig`` by default, with the hint off and the similarity cut-off
    off (ADR 0100) so a variant is compared on ranking alone.
    """
    store = store or MemoryStore(make_clock())
    ids = await seed(embedder, store, fixture)
    # The hint is off in the baseline; the hint variants turn it on themselves.
    base = replace(
        base or replace(SearchConfig(), recency_hint_enabled=False),
        default_limit=K,
        vector_min_similarity=None,
    )
    rows = []
    for variant in variants:
        service = SearchService(
            store, embedder, replace(base, **variant.overrides), now_fn=lambda: FIXED_NOW
        )
        hits = rrs = 0.0
        for query, relevant_indices in fixture.answerable:
            relevant = {ids[i]: 1 for i in relevant_indices}
            ranked = [
                h.entry_id for h in await service.search(query, limit=K, recency=variant.recency)
            ]
            hits += hit_at_k(ranked, relevant, K)
            rrs += reciprocal_rank(ranked, relevant)
        n = len(fixture.answerable)
        rows.append(Row(fixture.name, variant.name, n, hits / n, rrs / n))
    return rows


def drops(rows: Sequence[Row]) -> list[tuple[str, str, float, float]]:
    """(fixture, variant, hit@5 change, MRR change) of every non-baseline row
    whose hit@5 or MRR fell more than ``MAX_DROP`` below the baseline's."""
    baseline: dict[str, Row] = {}
    bad = []
    for row in rows:
        first = baseline.setdefault(row.fixture, row)
        if first is row:
            continue
        d_hit, d_mrr = row.hit_at_k - first.hit_at_k, row.mrr - first.mrr
        if d_hit < -MAX_DROP - 1e-9 or d_mrr < -MAX_DROP - 1e-9:
            bad.append((row.fixture, row.variant, d_hit, d_mrr))
    return bad


def report(rows: Sequence[Row], model: str | None = None) -> str:
    """The measurement as Markdown, ready for docs/retrieval-experiments.md."""
    lines = []
    if model:
        lines += [f"Model: `{model}`. Changes are against the first variant.", ""]
    lines += [
        f"| fixture | variant | queries | hit@{K} | MRR | hit@{K} change | MRR change |",
        "|---|---|---|---|---|---|---|",
    ]
    base: dict[str, Row] = {}
    for row in rows:
        first = base.setdefault(row.fixture, row)
        if first is row:
            change = ("", "")
        else:
            change = (f"{row.hit_at_k - first.hit_at_k:+.3f}", f"{row.mrr - first.mrr:+.3f}")
        lines.append(
            f"| {row.fixture} | {row.variant} | {row.queries} | {row.hit_at_k:.3f} "
            f"| {row.mrr:.3f} | {change[0]} | {change[1]} |"
        )
    bad = drops(rows)
    lines.append("")
    if bad:
        lines.append(
            f"Pass rule (no drop beyond {MAX_DROP}): NOT MET in "
            + ", ".join(
                f"{fixture} ({variant}: hit@{K} {d_hit:+.3f}, MRR {d_mrr:+.3f})"
                for fixture, variant, d_hit, d_mrr in bad
            )
        )
    else:
        lines.append(f"Pass rule (no hit@{K} or MRR drop beyond {MAX_DROP}): met.")
    return "\n".join(lines)


async def _main() -> None:
    from hivemind.config import endpoint_label, load_settings
    from hivemind.embeddings import EmbeddingError, build_embedder

    settings = load_settings()
    try:
        embedder = build_embedder(settings)
        rows: list[Row] = []
        for fixture in fixtures():
            rows += await measure(embedder, fixture)
        print(report(rows, embedder.model_name))
    except EmbeddingError as exc:
        where = endpoint_label(settings.embedding_endpoint)
        raise SystemExit(f"embedder at {where} unavailable ({exc}); start it (make vllm)") from None


if __name__ == "__main__":
    asyncio.run(_main())
