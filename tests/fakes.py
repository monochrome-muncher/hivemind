"""Shared test fakes at the port seams (TDD).

Every fake implements a *port* (``Store``, ``Embedder``, ``clock``),
never a concrete class, so tests sit at the seam. The clock is
caller-controlled so time-dependent logic (decay, tie-breaks) is fully
deterministic.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from hivemind.config import SearchConfig
from hivemind.domain.entry import EntryDraft, embeddable_text
from hivemind.memstore import MemoryStore
from hivemind.ports import entry_embeddable_text

FIXED_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


class FixedClock:
    """A clock that returns a fixed, advanceable time."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance_days(self, days: float) -> None:
        self._now += timedelta(days=days)

    def set(self, now: datetime) -> None:
        self._now = now


class FakeEmbedder:
    """Deterministic token-hash embedder (4 dims).

    Overlapping tokens produce similar vectors; identical text produces
    identical vectors — enough semantics to test the vector stream
    without a real embedding service.
    """

    def __init__(self, dimension: int = 4) -> None:
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def model_name(self) -> str:
        return "fake-embedder"

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self._dimension
        for token in text.lower().split():
            digest = hashlib.sha256(token.encode()).digest()
            for i in range(self._dimension):
                byte = digest[(i * 3) % 32]
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


def make_clock(start: datetime | None = None) -> FixedClock:
    return FixedClock(start or FIXED_NOW)


def make_embedder() -> FakeEmbedder:
    return FakeEmbedder()


def make_store(clock: FixedClock | None = None) -> MemoryStore:
    return MemoryStore(clock or make_clock())


def make_search_config() -> SearchConfig:
    return SearchConfig(candidate_top_k=10, default_limit=5, half_life_days=30.0)


def make_entry_clocks(n: int) -> list[FixedClock]:
    """n independent clocks all starting at FIXED_NOW."""
    return [FixedClock(FIXED_NOW) for _ in range(n)]
