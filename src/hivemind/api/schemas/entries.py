"""Entry schemas: sources, entities, entries, links, related entries, hits, search, withdraw, restore and redact."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from hivemind.api.schemas.common import to_utc
from hivemind.api.schemas.feedback import CorrectionOut, FeedbackCountsOut, FeedbackSummaryOut
from hivemind.domain.entry import EntityKind, Entry, EntryState, ImportanceSource, Kind, SourceType
from hivemind.domain.recency import RecencyHint
from hivemind.domain.redaction import RedactOutcome
from hivemind.domain.upstream import UpstreamChangeKind
from hivemind.domain.validation import MAX_AGENT_NAME_CHARS, MAX_GET_IDS, MAX_LIMIT, MAX_OFFSET
from hivemind.services.chain import EntryLinks
from hivemind.services.read import EntryDetail
from hivemind.services.search import Hit
from hivemind.services.write import RelatedEntry
from hivemind.services.write_warnings import WriteWarning
from hivemind.wire import round_score


class SourceModel(BaseModel):
    """A provenance pointer (SPEC.md §4.1: sources). An empty ``ref`` is
    a dead pointer, so both surfaces require a non-blank string."""

    type: SourceType
    ref: str = Field(min_length=1)

    @field_validator("ref")
    @classmethod
    def _ref_must_not_be_blank(cls, ref: str) -> str:
        if not ref.strip():
            raise ValueError("source 'ref' must be a non-blank string")
        return ref


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
    # Omitted -> 3 with ``importance_source=default``; supplied (1..5) ->
    # ``caller`` (ROADMAP §4.5).
    importance: int | None = None
    # Omitted -> the highest scope the trust level permits (ADR 0011); out
    # of permission -> 403; not a vocabulary value -> 422.
    scope: Literal["self", "fleet", "org"] | None = None
    supersedes: list[str] = []
    agent: str | None = None
    # "See also" links: up to 5 entries the writer can read.
    see_also: list[str] = []

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_utc(cls, v: datetime | None) -> datetime | None:
        """Naive ``occurred_at`` is assumed UTC (SPEC.md §6.4)."""
        return to_utc(v)


class SourceOut(BaseModel):
    """A provenance pointer as serialized in an entry."""

    type: SourceType
    ref: str


class EntityOut(BaseModel):
    """A machine-extracted entity facet (SPEC §13).

    ``name`` is open vocabulary (the extractor's output); ``kind`` is the
    closed six-value ``EntityKind`` vocabulary (display-only in v1 —
    filters match on names, not kinds).
    """

    name: str
    kind: EntityKind


class RelatedOut(BaseModel):
    """A nearby existing entry in a write's response."""

    id: str
    kind: Kind
    summary: str
    author: str
    scope: str
    fleet_id: str | None = None
    occurred_at: datetime
    similarity: float
    tags: list[str] = []
    feedback: FeedbackCountsOut = FeedbackCountsOut()
    same_author: bool = False

    @classmethod
    def from_related(cls, related: RelatedEntry) -> RelatedOut:
        entry = related.entry
        return cls(
            id=entry.id,
            kind=entry.kind,
            summary=entry.summary,
            author=entry.author,
            scope=entry.scope,
            fleet_id=entry.fleet_id,
            occurred_at=entry.occurred_at,
            similarity=round(related.similarity, 3),
            tags=list(entry.tags),
            feedback=FeedbackCountsOut.from_counts(related.feedback),
            same_author=related.same_author,
        )


class WarningOut(BaseModel):
    """An advisory note on a write (SPEC.md §4.1): never blocks it."""

    code: str
    message: str

    @classmethod
    def from_warning(cls, warning: WriteWarning) -> WarningOut:
        return cls(code=warning.code, message=warning.message)


class CheckOut(BaseModel):
    """``POST /v1/entries?check=true``: what a write would report, with
    nothing stored."""

    checked: bool = True
    related: list[RelatedOut] = []
    warnings: list[WarningOut] = []


class LinkOut(BaseModel):
    """A linked entry on a read: compact, with its state."""

    id: str
    kind: Kind
    summary: str
    author: str
    state: EntryState
    fleet_id: str | None = None

    @classmethod
    def from_entry(cls, entry: Entry) -> LinkOut:
        return cls(
            id=entry.id,
            kind=entry.kind,
            summary=entry.summary,
            author=entry.author,
            state=entry.state,
            fleet_id=entry.fleet_id,
        )


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
    # Server-derived (ROADMAP §4.5).
    importance_source: ImportanceSource
    scope: str
    fleet_id: str | None = None
    state: str
    superseded_by: str | None = None
    withdrawn_reason: str | None = None
    withdrawn_at: datetime | None = None
    # Extracted at write time (ADR 0016, SPEC §13); empty when extraction
    # was off or failed (SPEC §13.4).
    entities: list[EntityOut] = []
    entities_model: str | None = None
    # Only with ``?history=true`` (SPEC.md §5.1).
    history: dict[str, list[EntryOut]] | None = None
    # The entry's feedback: set by ``GET /v1/entries/{id}``; the admin and portal lists set the
    # counts alone.
    feedback: FeedbackSummaryOut | None = None
    # The nearest existing entries the writer can read: set by
    # ``POST /v1/entries`` only.
    related: list[RelatedOut] | None = None
    # Advisory notes on the write (SPEC.md §4.1): set by ``POST /v1/entries`` only.
    warnings: list[WarningOut] | None = None
    # "See also" links both ways, limited to what the reader may see
    # Set by the reads by id only.
    see_also: list[LinkOut] | None = None
    linked_from: list[LinkOut] | None = None

    def set_links(self, links: EntryLinks) -> None:
        self.see_also = [LinkOut.from_entry(e) for e in links.see_also]
        self.linked_from = [LinkOut.from_entry(e) for e in links.linked_from]

    @classmethod
    def from_detail(cls, detail: EntryDetail) -> EntryOut:
        """An entry as a read by id answers it: with its feedback and links."""
        out = cls.from_entry(detail.entry)
        out.feedback = FeedbackSummaryOut.from_summary(detail.feedback)
        out.set_links(detail.links)
        return out

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
            importance_source=entry.importance_source,
            scope=entry.scope,
            fleet_id=entry.fleet_id,
            state=entry.state.value,
            superseded_by=entry.superseded_by,
            withdrawn_reason=entry.withdrawn_reason,
            withdrawn_at=entry.withdrawn_at,
            entities=[EntityOut(name=e.name, kind=e.kind) for e in entry.entities],
            entities_model=entry.entities_model,
        )


class AdminEntryOut(EntryOut):
    """An entry as the admin panel reads it (ADR 0125): ``EntryOut`` plus
    whether and when an admin redacted it. Agents never get these fields, so
    the agent-facing entry shape (``tests/unit/test_wire_contract.py``) is
    unchanged."""

    redacted: bool = False
    redacted_at: datetime | None = None
    history: dict[str, list[AdminEntryOut]] | None = None  # type: ignore[assignment]

    @classmethod
    def from_entry(cls, entry: Entry) -> AdminEntryOut:
        out = super().from_entry(entry)
        assert isinstance(out, AdminEntryOut)
        out.redacted = entry.redacted_at is not None
        out.redacted_at = entry.redacted_at
        return out


class RedactRequest(BaseModel):
    """A redaction of a withdrawn memory (ADR 0125): ``confirm`` is the
    memory's id typed back."""

    # Both default to empty so the admin check answers first (403 before
    # any 422); the service refuses an empty one.
    reason: str = ""
    confirm: str = ""


class RedactOut(BaseModel):
    """What a redaction left behind for the admin to review: the feedback
    notes (flag notes among them) other agents wrote on the memory, which may
    quote the wiped text and were not edited."""

    entry: AdminEntryOut
    feedback_notes: int
    flag_notes: int
    withdraw_reasons: int

    @classmethod
    def from_outcome(cls, outcome: RedactOutcome) -> RedactOut:
        return cls(
            entry=AdminEntryOut.from_entry(outcome.entry),
            feedback_notes=outcome.feedback_notes,
            flag_notes=outcome.flag_notes,
            withdraw_reasons=outcome.withdraw_reasons,
        )


class GetEntriesRequest(BaseModel):
    """Read several entries by id at once."""

    entry_ids: list[str] = Field(min_length=1, max_length=MAX_GET_IDS)


class EntriesOut(BaseModel):
    """The readable entries, in the order asked, and the ids that name
    nothing the caller may read (ADR 0055)."""

    entries: list[EntryOut]
    not_found: list[str]


class SimilarOut(BaseModel):
    """Another member of a hit's version group."""

    id: str
    occurred_at: datetime


class UpstreamChangeOut(BaseModel):
    """A changed ``see_also`` target of a hit."""

    id: str
    change: UpstreamChangeKind


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
    score: float  # 4 significant figures (SPEC §6.1)
    scope: str = "fleet"
    fleet_id: str | None = None  # a foreign entry: != the reader's home fleet (ADR 0036)
    feedback: FeedbackCountsOut | None = None  # null when all three counts are zero
    last_confirmed_at: datetime | None = None  # SPEC §4.2
    # SPEC §6.1, display only; null when empty (state: unless include_inactive).
    contested: bool | None = None
    correction: CorrectionOut | None = None
    supersedes: int | None = None
    state: EntryState | None = None
    similar: list[SimilarOut] | None = None  # null when none
    upstream_changed: list[UpstreamChangeOut] | None = None  # null when none

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
            score=round_score(hit.score),
            scope=hit.scope,
            fleet_id=hit.fleet_id,
            feedback=FeedbackCountsOut.from_counts(hit.feedback) if any(hit.feedback) else None,
            last_confirmed_at=hit.last_confirmed_at,
            contested=True if hit.contested else None,
            correction=(
                CorrectionOut(
                    verdict=hit.correction.verdict,
                    note=hit.correction.note,
                    reporter=hit.correction.reporter,
                    updated_at=hit.correction.updated_at,
                )
                if hit.correction
                else None
            ),
            supersedes=hit.supersedes or None,
            state=hit.state,
            similar=(
                [SimilarOut(id=m.entry_id, occurred_at=m.occurred_at) for m in hit.similar]
                if hit.similar
                else None
            ),
            upstream_changed=(
                [UpstreamChangeOut(id=c.id, change=c.change) for c in hit.upstream_changed]
                if hit.upstream_changed
                else None
            ),
        )


class SearchRequest(BaseModel):
    """A hybrid search with filters (SPEC.md §5.1, §5.3)."""

    query: str
    kind: Kind | None = None
    tags: list[str] = []
    # Extracted entity names: AND, case-insensitive (ADR 0016, SPEC §13).
    entities: list[str] = []
    scope: str | None = None
    author: str | None = None
    agent: str | None = None
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    include_inactive: bool = False
    # Only entries reported stale or wrong at least once.
    flagged: bool = False
    # Opt-in diversity re-order of the ranked results.
    explore: bool = False
    # ADR 0073: how age should weigh in; honoured only when the server's
    # recency_hint_enabled is on. Send the same value on every page.
    recency: RecencyHint | None = None
    # SPEC §5.3 / ADR 0040: 1..100 and 0..10000 (a search also reaches at
    # most 2 x candidate_top_k ranked hits).
    limit: int | None = Field(default=None, ge=1, le=MAX_LIMIT)
    offset: int | None = Field(default=None, ge=0, le=MAX_OFFSET)

    @field_validator("occurred_from", "occurred_to", "created_from", "created_to")
    @classmethod
    def _utc_ranges(cls, v: datetime | None) -> datetime | None:
        """Naive range bounds are assumed UTC (SPEC.md §5.3, §6.4)."""
        return to_utc(v)


class WithdrawRequest(BaseModel):
    """A withdrawal of an entry (SPEC.md §4.1)."""

    reason: str | None = None


class RestoreRequest(BaseModel):
    """Putting a withdrawn memory back (ADR 0124). The reason is required; it
    defaults to empty so the service refuses a missing one after the admin
    check (a non-admin gets 403, never 422)."""

    reason: str = ""


class FleetSearchRequest(SearchRequest):
    """A search plus a fleet filter: the admin search and the portal search."""

    fleet_id: str | None = None


class AdminSearchRequest(FleetSearchRequest):
    """``POST /v1/admin/search``: the fleet search, optionally run as one agent (``as_agent``,
    ADR 0136). Admin only: the portal's request has no such field."""

    as_agent: str | None = Field(default=None, max_length=MAX_AGENT_NAME_CHARS)
