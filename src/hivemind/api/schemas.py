"""Pydantic schemas for the Hivemind REST surface (SPEC.md §5.1).

Request/response models are plain and small: they mirror the domain
entities (Entry, EntryDraft, EntryFilters, Feedback) and the service
results (Hit) without leaking implementation details (embeddings,
raw config) across the wire.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from hivemind.domain.entry import Entry, Kind, SourceType
from hivemind.domain.feedback import Verdict
from hivemind.services.search import Hit


class SourceModel(BaseModel):
    """A provenance pointer (SPEC.md §4.1: sources)."""

    type: SourceType
    ref: str


class CreateEntryRequest(BaseModel):
    """Write request: everything the writer supplies for a new entry.

    ``author`` is never client-supplied — it is stamped from the
    credential (SPEC.md §8.1). ``agent`` only matters for plain user
    keys: with an agent sub-key the server fills it in.
    """

    kind: Kind
    summary: str
    body: str | None = None
    payload: dict[str, Any] | None = None
    sources: list[SourceModel] = []
    tags: list[str] = []
    occurred_at: datetime | None = None
    importance: int = 3
    scope: str = "org"
    supersedes: list[str] = []
    agent: str | None = None


class SourceOut(BaseModel):
    """A provenance pointer as serialized in an entry."""

    type: SourceType
    ref: str


class EntryOut(BaseModel):
    """A full entry (SPEC.md §4.1). Embeddings are internal and are
    never serialized across the wire."""

    id: str
    kind: Kind
    summary: str
    body: str | None = None
    payload: dict[str, Any] | None = None
    sources: list[SourceOut] = []
    tags: list[str] = []
    occurred_at: datetime
    created_at: datetime
    author: str
    agent: str
    importance: int
    scope: str
    state: str
    superseded_by: str | None = None
    withdrawn_reason: str | None = None

    @classmethod
    def from_entry(cls, entry: Entry) -> EntryOut:
        """Serialize a stored entry (the get/withdraw/withdraw responses)."""
        return cls(
            id=entry.id,
            kind=entry.kind,
            summary=entry.summary,
            body=entry.body,
            payload=entry.payload,
            sources=[SourceOut(type=s.type, ref=s.ref) for s in entry.sources],
            tags=list(entry.tags),
            occurred_at=entry.occurred_at,
            created_at=entry.created_at,
            author=entry.author,
            agent=entry.agent,
            importance=entry.importance,
            scope=entry.scope,
            state=entry.state.value,
            superseded_by=entry.superseded_by,
            withdrawn_reason=entry.withdrawn_reason,
        )


class HitOut(BaseModel):
    """A compact search hit (SPEC.md §6.1: progressive disclosure).

    Deliberately carries no ``body``: agents scan compact hits and
    open the interesting ones via the get endpoint.
    """

    entry_id: str
    kind: Kind
    summary: str
    tags: list[str] = []
    author: str
    agent: str
    occurred_at: datetime
    score: float

    @classmethod
    def from_hit(cls, hit: Hit) -> HitOut:
        """Serialize a service-layer hit (the search response)."""
        return cls(
            entry_id=hit.entry_id,
            kind=hit.kind,
            summary=hit.summary,
            tags=list(hit.tags),
            author=hit.author,
            agent=hit.agent,
            occurred_at=hit.occurred_at,
            score=hit.score,
        )


class SearchRequest(BaseModel):
    """A hybrid search with filters (SPEC.md §5.1, §5.3)."""

    query: str
    kind: Kind | None = None
    tags: list[str] = []
    scope: str | None = None
    author: str | None = None
    agent: str | None = None
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None
    include_inactive: bool = False
    limit: int | None = None


class WithdrawRequest(BaseModel):
    """A withdrawal of an entry (SPEC.md §4.1)."""

    reason: str | None = None


class FeedbackRequest(BaseModel):
    """A verdict on an entry the caller relied on (SPEC.md §4.2)."""

    verdict: Verdict
    note: str | None = None


class FeedbackOut(BaseModel):
    """The result of a feedback upsert: the stored verdict + the
    entry's refreshed quality multiplier (SPEC.md §4.2)."""

    entry_id: str
    verdict: Verdict
    quality: float


class HealthOut(BaseModel):
    """Liveness/readiness (SPEC.md §5.1)."""

    status: str


class ErrorBody(BaseModel):
    """The error envelope: one consistent JSON body for every failure."""

    code: str
    message: str
