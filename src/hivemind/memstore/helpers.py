"""Pure helpers shared by the in-memory store's areas."""

from __future__ import annotations

import math
import uuid
from dataclasses import replace
from datetime import UTC, datetime

from hivemind.domain.entry import Entry
from hivemind.domain.feedback import Feedback, FeedbackCounts, Verdict
from hivemind.domain.validation import canonical_uuid, is_valid_uuid

# Sort key for a feedback row that has no timestamp (fixtures only).
EPOCH = datetime.min.replace(tzinfo=UTC)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity; 0.0 for zero-norm or dimension-mismatched vectors."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def all_pairs_cosine(
    ids: list[str], vectors: dict[str, list[float]]
) -> dict[tuple[str, str], float]:
    """Cosine of every pair among ``ids`` (in that order; sorted, a pair is
    keyed ``(a, b)`` with ``a < b``), from ``vectors``."""
    return {
        (a, b): cosine_similarity(vectors[a], vectors[b])
        for i, a in enumerate(ids)
        for b in ids[i + 1 :]
    }


def canon(value: str) -> str | None:
    """``value`` as Postgres reads a ``uuid``, or ``None`` if the column would refuse it."""
    return canonical_uuid(value) if is_valid_uuid(value) else None


def canonical_or_same(value: str) -> str:
    """``value`` in the spelling ids are stored in, or unchanged when it is no uuid."""
    return str(uuid.UUID(value)) if is_valid_uuid(value) else value


def copy_entry(entry: Entry) -> Entry:
    """Return a fresh Entry with copied containers (defensive copy)."""
    return replace(
        entry,
        payload=dict(entry.payload) if entry.payload is not None else None,
        sources=tuple(entry.sources),
        tags=tuple(entry.tags),
        embedding=tuple(entry.embedding) if entry.embedding is not None else None,
        entities=tuple(entry.entities),
    )


def count_for(feedback: dict[tuple[str, str, str], Feedback], entry_id: str) -> FeedbackCounts:
    helpful = stale = wrong = 0
    for fb in feedback.values():
        if fb.entry_id != entry_id:
            continue
        if fb.verdict is Verdict.HELPFUL:
            helpful += 1
        elif fb.verdict is Verdict.STALE:
            stale += 1
        elif fb.verdict is Verdict.WRONG:
            wrong += 1
    return (helpful, stale, wrong)
