"""Port definitions — the seams the TDD suite tests at.

These are the only boundaries the rest of the system depends on:

* ``Store`` — data access. Implemented by ``MemoryStore`` (dev/tests)
  and the Postgres adapter (production). Everything above the store
  is storage-agnostic.
* ``Embedder`` — turns text into a fixed-dimension vector. Implemented
  by an OpenAI-compatible client; faked in unit tests.
* ``Authenticator`` — maps an API key to a credential. Implemented by
  a Postgres-backed credential table in production; faked in tests.

Tests live at these seams and nowhere inside them (TDD skill).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from hivemind.domain.entry import (
    Entry,
    EntryDraft,
    EntryFilters,
    embeddable_text,
)
from hivemind.domain.feedback import Feedback, FeedbackCounts


@runtime_checkable
class Store(Protocol):
    """Read/write access to the memory pool."""

    async def create_entry(
        self,
        draft: EntryDraft,
        embedding: list[float] | None = None,
        embedding_model: str | None = None,
    ) -> Entry:
        """Insert a new entry and flip any ``draft.supersedes`` targets
        to the ``superseded`` state (SPEC.md §4.1). Returns the stored
        Entry (with server-assigned ``id``/``created_at``).

        ``embedding_model`` (SPEC.md §7) records which model produced
        ``embedding`` so the vector's provenance is traceable per entry.
        """
        ...

    async def get_entry(self, entry_id: str) -> Entry | None: ...

    async def get_entries(self, entry_ids: list[str]) -> dict[str, Entry]:
        """Fetch full entries by ID (missing IDs are simply absent)."""
        ...

    async def list_entries(
        self,
        filters: EntryFilters,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Entry]: ...

    async def withdraw_entry(self, entry_id: str, reason: str | None, by_user: str) -> Entry:
        """Flip an entry to ``withdrawn`` (SPEC.md §4.1). Only active
        entries can be withdrawn; ``by_user`` is the API-layer audit of
        who withdrew it (v1 persists the reason, not the withdrawer —
        SPEC.md §4.1 lists ``withdrawn_reason`` only). Raises
        ``KeyError`` if unknown, ``ValueError`` if not active.
        """
        ...

    async def search_keyword(self, query: str, filters: EntryFilters, limit: int) -> list[str]:
        """Ranked entry IDs by keyword relevance (descending)."""
        ...

    async def search_vector(
        self, embedding: list[float], filters: EntryFilters, limit: int
    ) -> list[str]:
        """Ranked entry IDs by embedding similarity (descending)."""
        ...

    async def record_feedback(self, feedback: Feedback) -> None:
        """Upsert a feedback verdict (one row per entry+user+agent)."""
        ...

    async def feedback_counts(self, entry_id: str) -> FeedbackCounts:
        """(helpful, stale, wrong) counts for one entry."""
        ...

    async def quality_counts(self, entry_ids: list[str]) -> dict[str, FeedbackCounts]:
        """Batched feedback counts for a set of entries."""
        ...


@runtime_checkable
class Embedder(Protocol):
    """Embedding provider: text in, fixed-dimension vector out."""

    @property
    def dimension(self) -> int:
        """The fixed vector dimension (deploy-time decision, ADR 0005)."""
        ...

    @property
    def model_name(self) -> str:
        """The embedding model identifier (recorded per entry, ADR 0005)."""
        ...

    async def embed_text(self, text: str) -> list[float]:
        """Embed a query or a standalone text."""
        ...

    async def embed_entry(self, draft: EntryDraft) -> list[float]:
        """Embed the text an entry is indexed by (SPEC.md §7)."""
        ...

    def entry_embeddable_text(self, draft: EntryDraft) -> str:
        """The exact text an entry is embedded from."""
        ...


@runtime_checkable
class Authenticator(Protocol):
    """Credential check for the flat org pool (SPEC.md §8.1)."""

    async def verify(self, key: str) -> Credential | None:
        """Resolve a key to its credential, or ``None`` if unknown."""
        ...


@dataclass(frozen=True, slots=True)
class Credential:
    """A verified caller: who acts, under which agent, with which rights.

    ``agent_id`` is set for agent-scoped sub-keys (the key binds the
    agent); for plain user keys it is ``None`` and the agent self-reports
    its instance ID per request (SPEC.md §8.1).
    """

    user_id: str
    agent_id: str | None = None
    is_admin: bool = False

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        kind = "admin" if self.is_admin else "user"
        return f"Credential({kind}, user={self.user_id!r}, agent={self.agent_id!r})"


def entry_embeddable_text(draft: EntryDraft) -> str:
    """The text an entry is embedded from (SPEC.md §7)."""
    return embeddable_text(draft.summary, draft.body)
