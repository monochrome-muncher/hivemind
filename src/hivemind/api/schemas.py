"""Pydantic schemas for the Hivemind REST surface (SPEC.md §5.1).

They mirror the domain and service results without leaking internals
(embeddings, raw config) across the wire.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field, field_validator

from hivemind.domain.access import Agent, Fleet, Standing
from hivemind.domain.audit import AuditRecord
from hivemind.domain.entry import EntityKind, Entry, EntryState, ImportanceSource, Kind, SourceType
from hivemind.domain.feedback import (
    FeedbackCounts,
    FeedbackLogItem,
    FeedbackSummary,
    FlaggedEntry,
    Verdict,
)
from hivemind.domain.fleet_merge import DroppedPin
from hivemind.domain.pin import ChangeReason, Pin, PinnedEntry
from hivemind.domain.recency import RecencyHint
from hivemind.domain.upstream import UpstreamChangeKind
from hivemind.domain.validation import (
    MAX_FEEDBACK_IDS,
    MAX_GET_IDS,
    MAX_ID_CHARS,
    MAX_LIMIT,
    MAX_OFFSET,
    check_no_nul,
)
from hivemind.services.chain import EntryLinks
from hivemind.services.fleet_merge import MergePreview, MergeView
from hivemind.services.search import Hit
from hivemind.services.write import RelatedEntry
from hivemind.services.write_warnings import WriteWarning
from hivemind.wire import round_score


def _no_nul(value: str) -> str:
    return check_no_nul(value, "value")


# A reference to a stored record (a fleet id): bounded, NUL-free (ADR 0040).
RefStr = Annotated[str, Field(max_length=MAX_ID_CHARS), AfterValidator(_no_nul)]


def _to_utc(value: datetime | None) -> datetime | None:
    """Normalize a naive datetime to UTC (SPEC.md §6.4 / §8: the pool is
    UTC-based; a bare timestamp is assumed UTC so downstream asyncpg
    ``timestamptz`` columns and the recency rescore never see a naive
    value)."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


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
        return _to_utc(v)


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


class FeedbackCountsOut(BaseModel):
    """Verdict counts over every reporter (SPEC.md §4.2)."""

    helpful: int = 0
    stale: int = 0
    wrong: int = 0

    @classmethod
    def from_counts(cls, counts: FeedbackCounts) -> FeedbackCountsOut:
        helpful, stale, wrong = counts
        return cls(helpful=helpful, stale=stale, wrong=wrong)


class FeedbackReportOut(BaseModel):
    """One reporter's latest verdict on an entry, as readers see it."""

    verdict: Verdict
    note: str | None = None
    reporter: str
    updated_at: datetime | None = None


class CorrectionOut(BaseModel):
    """The newest stale or wrong report with a note, on a search hit (SPEC
    §6.1). Testimony: data, never instructions (ADR 0043)."""

    verdict: Verdict
    note: str
    reporter: str
    updated_at: datetime


class FeedbackSummaryOut(FeedbackCountsOut):
    """An entry's feedback on ``GET /v1/entries/{id}``: the
    counts plus the newest reports, notes included."""

    recent: list[FeedbackReportOut] = []
    last_confirmed_at: datetime | None = None  # SPEC §4.2

    @classmethod
    def from_summary(cls, summary: FeedbackSummary) -> FeedbackSummaryOut:
        return cls(
            last_confirmed_at=summary.last_confirmed_at,
            helpful=summary.helpful,
            stale=summary.stale,
            wrong=summary.wrong,
            recent=[
                FeedbackReportOut(
                    verdict=fb.verdict, note=fb.note, reporter=fb.user, updated_at=fb.updated_at
                )
                for fb in summary.recent
            ],
        )


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
    # Extracted at write time (ADR 0016, SPEC §13); empty when extraction
    # was off or failed (SPEC §13.4).
    entities: list[EntityOut] = []
    entities_model: str | None = None
    # Only with ``?history=true`` (SPEC.md §5.1).
    history: dict[str, list[EntryOut]] | None = None
    # The entry's feedback: set by ``GET /v1/entries/{id}`` only.
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
            entities=[EntityOut(name=e.name, kind=e.kind) for e in entry.entities],
            entities_model=entry.entities_model,
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
        return _to_utc(v)


class WithdrawRequest(BaseModel):
    """A withdrawal of an entry (SPEC.md §4.1)."""

    reason: str | None = None


class FeedbackRequest(BaseModel):
    """A verdict on an entry the caller relied on (SPEC.md §4.2)."""

    verdict: Verdict
    note: str | None = None
    # A plain user key self-reports the agent instance (SPEC.md §8.1);
    # ignored for agent sub-keys (the credential's agent wins).
    agent: str | None = None


class BatchFeedbackRequest(BaseModel):
    """One verdict on several entries at once."""

    entry_ids: list[str] = Field(min_length=1, max_length=MAX_FEEDBACK_IDS)
    verdict: Verdict
    note: str | None = None
    agent: str | None = None


class PinOut(BaseModel):
    """A pin: which entry is pinned to which fleet, by whom."""

    entry_id: str
    fleet_id: str
    pinned_by: str
    pinned_at: datetime

    @classmethod
    def from_pin(cls, pin: Pin) -> PinOut:
        return cls(
            entry_id=pin.entry_id,
            fleet_id=pin.fleet_id,
            pinned_by=pin.pinned_by,
            pinned_at=pin.pinned_at,
        )


class UnpinOut(BaseModel):
    """The answer to an unpin: whether the entry was pinned."""

    entry_id: str
    removed: bool


class PinnedOut(BaseModel):
    """One pinned entry as the reader sees it: ``id`` is the
    current version, ``pinned_id`` the version that was pinned."""

    id: str
    pinned_id: str
    kind: Kind
    summary: str
    author: str
    state: EntryState
    pinned_by: str
    pinned_at: datetime
    # ``change_reason`` is ``superseded`` or ``flagged`` when set.
    changed_since_pin: bool
    change_reason: ChangeReason | None

    @classmethod
    def from_pinned(cls, pinned: PinnedEntry) -> PinnedOut:
        return cls(
            id=pinned.entry.id,
            pinned_id=pinned.pin.entry_id,
            kind=pinned.entry.kind,
            summary=pinned.entry.summary,
            author=pinned.entry.author,
            state=pinned.entry.state,
            pinned_by=pinned.pin.pinned_by,
            pinned_at=pinned.pin.pinned_at,
            changed_since_pin=pinned.changed_since_pin,
            change_reason=pinned.change_reason,
        )


class PinsOut(BaseModel):
    """A fleet's pinned entries, newest pin first."""

    fleet_id: str | None
    pins: list[PinnedOut]


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


# --- Access-control (ADRs 0011-0012, SPEC §12) -----------------------------


class RegisterAgentRequest(BaseModel):
    """Register an agent (ADR 0012): the name is the org-unique identity
    (it becomes the entry ``author``); ``owner_alias`` is an optional
    owner contact for the org key holder (out-of-band delivery)."""

    name: str
    owner_alias: str | None = None


class ActivateAgentRequest(BaseModel):
    """Activate a pending agent: set its trust level + home
    fleet; the agent key is issued once. The trust level defaults to
    `lurker` (1) (SPEC §5.1). Out-of-range values are a 422, not a 500."""

    trust_level: int = Field(default=1, ge=0, le=3)
    home_fleet_id: RefStr


class UpdateAgentRequest(BaseModel):
    """Change an agent's trust level and/or home fleet (the SPEC §5.1
    ``PATCH /v1/admin/agents/{name}`` body; ADR 0011). At least one
    field must be supplied. Out-of-range trust levels are a 422, not a 500."""

    trust_level: int | None = Field(default=None, ge=0, le=3)
    home_fleet_id: RefStr | None = None


class CreateFleetRequest(BaseModel):
    """Create a named fleet."""

    name: str


class RenameFleetRequest(BaseModel):
    """Give a fleet a new name (ADR 0109)."""

    name: str


class AgentOut(BaseModel):
    """A registered agent."""

    name: str
    status: str
    trust_level: int
    home_fleet_id: str | None
    owner_alias: str | None = None
    created_at: str | None = None
    activated_at: str | None = None

    @classmethod
    def from_agent(cls, agent: Agent) -> AgentOut:
        return cls(
            name=agent.name,
            status=agent.status.value,
            trust_level=agent.trust_level.value,
            home_fleet_id=agent.home_fleet_id,
            owner_alias=agent.owner_alias,
            created_at=agent.created_at.isoformat() if agent.created_at else None,
            activated_at=agent.activated_at.isoformat() if agent.activated_at else None,
        )


class RegisteredAgentOut(AgentOut):
    """The answer to ``POST /v1/agents``: the agent record plus
    whether this caller had already registered the name (same alias) and
    what its status means for the caller (pending / active → ask the admin
    for the key / revoked)."""

    already_registered: bool
    message: str


class FleetOut(BaseModel):
    """A fleet."""

    id: str
    name: str
    created_at: str
    # Set on a fleet merged into another (ADR 0107); listed only with
    # ``?include_merged=true``.
    merged_into: str | None = None
    merged_at: str | None = None

    @classmethod
    def from_fleet(cls, fleet: Fleet) -> FleetOut:
        return cls(
            id=fleet.id,
            name=fleet.name,
            created_at=fleet.created_at.isoformat(),
            merged_into=fleet.merged_into,
            merged_at=fleet.merged_at.isoformat() if fleet.merged_at is not None else None,
        )


# --- Fleet merge (ADR 0107) ---------------------------------------------------


class FleetMergeRequest(BaseModel):
    """``POST /v1/admin/fleets/{source_id}/merge``: name exactly one of
    ``target_fleet_id`` and ``new_fleet_name``. ``dry_run`` is required, so a
    merge is never started by leaving it out."""

    target_fleet_id: RefStr | None = None
    new_fleet_name: str | None = None
    keep_pin_ids: list[RefStr] | None = Field(default=None, max_length=50)
    # Every pin the preview showed: the merge is refused (409) when the pins
    # of the two fleets are not exactly these, so none is kept or dropped unseen.
    expected_pin_ids: list[RefStr] | None = Field(default=None, max_length=200)
    dry_run: bool


class MergeSideOut(BaseModel):
    """One fleet of a previewed merge; ``id`` is ``None`` for a new target."""

    id: str | None
    name: str
    new: bool
    agents: int
    entries: dict[str, int]
    pins: int


class MergePinOut(BaseModel):
    """A pin of either fleet; ``default_kept`` marks the newest ten."""

    entry_id: str
    fleet_id: str
    pinned_by: str
    pinned_at: datetime
    kind: Kind | None
    summary: str | None
    state: EntryState | None
    default_kept: bool


class MergeDuplicateOut(BaseModel):
    """A likely duplicate pair: an active entry of the smaller fleet and its
    nearest active entry in the other fleet."""

    entry_id: str
    entry_summary: str | None
    match_id: str
    match_summary: str | None
    similarity: float


class FleetMergePreviewOut(BaseModel):
    """The ``dry_run`` answer: nothing was changed."""

    dry_run: Literal[True] = True
    source: MergeSideOut
    target: MergeSideOut
    pins: list[MergePinOut]
    pin_limit: int
    needs_pin_choice: bool
    duplicates: list[MergeDuplicateOut]
    duplicates_looked_up: int
    duplicates_truncated: bool

    @classmethod
    def from_preview(cls, preview: MergePreview) -> FleetMergePreviewOut:
        def side(item: Any, new: bool) -> MergeSideOut:
            return MergeSideOut(
                id=item.fleet.id if item.fleet is not None else None,
                name=item.name,
                new=new,
                agents=item.facts.agents,
                entries=dict(item.facts.entries),
                pins=item.pins,
            )

        return cls(
            source=side(preview.source, False),
            target=side(preview.target, preview.target.fleet is None),
            pins=[
                MergePinOut(
                    entry_id=p.pin.entry_id,
                    fleet_id=p.pin.fleet_id,
                    pinned_by=p.pin.pinned_by,
                    pinned_at=p.pin.pinned_at,
                    kind=p.entry.kind if p.entry is not None else None,
                    summary=p.entry.summary if p.entry is not None else None,
                    state=p.entry.state if p.entry is not None else None,
                    default_kept=p.default_kept,
                )
                for p in preview.pins
            ],
            pin_limit=preview.pin_limit,
            needs_pin_choice=preview.needs_pin_choice,
            duplicates=[
                MergeDuplicateOut(
                    entry_id=d.entry_id,
                    entry_summary=d.entry.summary if d.entry is not None else None,
                    match_id=d.match_id,
                    match_summary=d.match.summary if d.match is not None else None,
                    similarity=round(d.similarity, 4),
                )
                for d in preview.duplicates
            ],
            duplicates_looked_up=preview.duplicates_looked_up,
            duplicates_truncated=preview.duplicates_truncated,
        )


class DroppedPinOut(BaseModel):
    """A pin a merge unpinned (or an undo could not restore)."""

    fleet_id: str
    entry_id: str
    pinned_by: str
    pinned_at: datetime

    @classmethod
    def from_dropped(cls, pin: DroppedPin) -> DroppedPinOut:
        return cls(
            fleet_id=pin.fleet_id,
            entry_id=pin.entry_id,
            pinned_by=pin.pinned_by,
            pinned_at=pin.pinned_at,
        )


class FleetMergeOut(BaseModel):
    """A merge that happened."""

    dry_run: Literal[False] = False
    merge_id: str
    source_fleet_id: str
    target: FleetOut
    target_created: bool
    entries_moved: int
    agents_moved: list[str]
    pins_kept: int
    pins_dropped: list[DroppedPinOut]


class FleetUnmergeOut(BaseModel):
    """An undone merge. Search counters stay summed."""

    merge_id: str
    source_fleet_id: str
    target_fleet_id: str
    entries_restored: int
    agents_restored: list[str]
    pins_returned: int
    pins_restored: list[DroppedPinOut]
    pins_skipped: list[DroppedPinOut]


class FleetMergeRecordOut(BaseModel):
    """One recorded merge; ``undoable`` says whether an undo is allowed now
    (``undo_blocked`` says why not)."""

    id: str
    source_fleet_id: str
    source_fleet_name: str | None
    target_fleet_id: str
    target_fleet_name: str | None
    merged_by: str
    merged_at: str
    moved_agents: list[str]
    dropped_pins: int
    undone_at: str | None
    undoable: bool
    undo_blocked: str | None

    @classmethod
    def from_view(cls, view: MergeView) -> FleetMergeRecordOut:
        r = view.record
        return cls(
            id=r.id,
            source_fleet_id=r.source_fleet_id,
            source_fleet_name=view.source_name,
            target_fleet_id=r.target_fleet_id,
            target_fleet_name=view.target_name,
            merged_by=r.merged_by,
            merged_at=r.merged_at.isoformat(),
            moved_agents=list(r.moved_agents),
            dropped_pins=len(r.dropped_pins),
            undone_at=r.undone_at.isoformat() if r.undone_at is not None else None,
            undoable=view.undoable,
            undo_blocked=view.blocked,
        )


class WhoamiOut(BaseModel):
    """The calling key's standing (``GET /v1/whoami``)."""

    key_kind: str
    name: str
    status: str | None
    trust_level: int
    trust_level_name: str
    home_fleet_id: str | None
    home_fleet_name: str | None
    can_read: list[str]
    can_write_scopes: list[str]

    @classmethod
    def from_standing(cls, standing: Standing) -> WhoamiOut:
        return cls.model_validate(standing.as_dict())


class KeyIssuedOut(BaseModel):
    """A freshly issued key (returned **once**, ADR 0012)."""

    key: str

    note: str = "store this key now; it is shown only once"


class AuditRecordOut(BaseModel):
    """One audit-log row (ADR 0027, SPEC §12.5). Never carries a raw key:
    key-producing actions record a key fingerprint or nothing."""

    id: str
    occurred_at: str
    actor_kind: str
    actor: str
    action: str
    target: str | None
    detail: dict[str, Any]

    @classmethod
    def from_record(cls, record: AuditRecord) -> AuditRecordOut:
        return cls(
            id=record.id,
            occurred_at=record.occurred_at.isoformat(),
            actor_kind=record.actor_kind.value,
            actor=record.actor,
            action=record.action.value,
            target=record.target,
            detail=dict(record.detail),
        )


# --- Usage counters (ROADMAP §3.3, Tier 3) ----------------------------------


class EntriesMetrics(BaseModel):
    """Entry usage counters (ROADMAP §3.3)."""

    total: int
    active: int
    inactive: int
    by_scope: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    # ROADMAP §4.5.
    by_importance_source: dict[str, int] = {}
    # Author -> kind counts (ROADMAP §4.5), keyed by the agent roster, so
    # an author with no entries maps to {}; zero counts are omitted.
    by_author_kind: dict[str, dict[str, int]] = {}


class FleetsMetrics(BaseModel):
    """Fleet usage counters (writes per fleet, the §12 counter)."""

    total: int
    writes_by_fleet: dict[str, int] = {}


class AgentsMetrics(BaseModel):
    """Agent usage counters (the §12 counters: trust-level distribution,
    pending-agent count)."""

    total: int
    pending: int
    active: int
    revoked: int = 0
    by_trust_level: dict[str, int] = {}


class FleetSearchMetrics(BaseModel):
    """Search counters for one fleet name."""

    total: int
    empty: int
    explore: int = 0


class SearchesMetrics(BaseModel):
    """Search counters in total and per fleet name (ADRs 0056, 0078).
    Searches from callers without a home fleet count in the totals only."""

    total: int
    empty: int
    explore: int = 0
    by_fleet: dict[str, FleetSearchMetrics] = {}


class FeedbackMetrics(BaseModel):
    """Flag counters: ``stale``/``wrong`` reports made while the
    entry was current, and how many were later confirmed by a supersession.
    The totals include every non-revoked reporter (the admin key too);
    ``by_agent`` is keyed by the registered roster and omits agents with no
    flags. Observation only: never an input to ranking."""

    flags: int = 0
    confirmed: int = 0
    by_agent: dict[str, dict[str, int]] = {}


class Share(BaseModel):
    """``count`` of ``total`` and their ratio (``None`` while ``total`` is 0)."""

    count: int = 0
    total: int = 0
    share: float | None = None


class AuthorShare(Share):
    authors: int = 0


class ReporterShare(Share):
    reporters: int = 0


class OwnerShare(Share):
    owners: int = 0


class SupersessionRate(BaseModel):
    supersessions: int = 0
    writes: int = 0
    per_100: float | None = None


class RivalAge(BaseModel):
    count: int = 0
    median_age_seconds: float | None = None


class RivalLifetime(BaseModel):
    """Active ``alternative`` entries, ``Conflict:`` entries and contested
    entries."""

    alternative: RivalAge = RivalAge()
    conflict: RivalAge = RivalAge()
    contested: RivalAge = RivalAge()


class LabelShare(Share):
    labels: int = 0


class TopicDispersion(BaseModel):
    """Mean pairwise cosine of a busy tag's entries, last 30 days against
    the 30 before; a positive ``change`` means converging (ADR 0075)."""

    tag: str
    recent_entries: int
    before_entries: int
    recent_cosine: float
    before_cosine: float
    change: float


class FleetHealthMetrics(BaseModel):
    """The per-fleet part of the health report (ADR 0075), keyed by entries'
    ``fleet_id``."""

    sourced_share: Share = Share()
    flagged_backlog_14d: int = 0
    orphaned_flagged: int = 0
    floating_gap: int = 0
    gap_tag_counts: dict[str, int] = {}


class HealthMetrics(BaseModel):
    """Fleet health metrics (ADR 0075): admin-only and observation-only,
    never read by search, ranking, scoring, quality or the briefing. The
    windows (90 days for the concentration and supersession figures, 14 and
    30 days for the backlog and orphan ages) are fixed by the ADR.
    Author withdrawals within an hour of the write are not measured: the
    schema records neither when nor by whom an author withdrew."""

    helpful_with_note_share: Share = Share()
    sourced_share: Share = Share()
    correction_latency_p50_seconds: float | None = None
    corrections_measured: int = 0
    flagged_backlog_14d: int = 0
    orphaned_flagged: int = 0
    floating_gap: int = 0
    top3_author_write_share: AuthorShare = AuthorShare()
    top3_reporter_verdict_share: ReporterShare = ReporterShare()
    sibling_verdict_share: Share = Share()
    top3_owner_verdict_share: OwnerShare = OwnerShare()
    synthesis_rate: Share = Share()
    self_to_fleet_promotions: int = 0
    gap_tag_counts: dict[str, int] = {}
    supersessions_per_100_writes: SupersessionRate = SupersessionRate()
    echo_rate: Share = Share()
    topic_dispersion: list[TopicDispersion] = []
    rival_lifetime: RivalLifetime = RivalLifetime()
    grounding_share: Share = Share()
    top3_agent_label_write_share: LabelShare = LabelShare()
    top3_agent_label_verdict_share: LabelShare = LabelShare()
    by_fleet: dict[str, FleetHealthMetrics] = {}


class AdminSearchRequest(SearchRequest):
    """``POST /v1/admin/search``: a search plus a fleet filter."""

    fleet_id: str | None = None


class PortalSearchRequest(AdminSearchRequest):
    """``POST /v1/portal/search`` (ADR 0095): the same search and fleet
    filter, over what the agent's key can read."""


class PortalWithdrawRequest(BaseModel):
    """A withdrawal from the agent portal (ADR 0095): the reason is required."""

    reason: str


class DayActivityOut(BaseModel):
    day: date
    writes: int
    helpful: int
    stale: int
    wrong: int


class ActivityTotalsOut(BaseModel):
    writes: int
    verdicts: int
    writers: int
    reporters: int


class ReceivedOut(BaseModel):
    helpful: int
    stale: int
    wrong: int


class AgentActivityOut(BaseModel):
    name: str
    registered: bool
    owner_alias: str | None
    home_fleet: str | None
    status: str | None
    entries: int
    active: int
    recent_writes: int
    last_write_at: datetime | None
    verdicts_given: int
    recent_verdicts: int
    last_verdict_at: datetime | None
    received: ReceivedOut


class FleetActivityOut(BaseModel):
    fleet_id: str | None
    name: str | None
    entries: int
    active: int
    recent_writes: int
    writers: int
    last_write_at: datetime | None
    agents: int


class ActivityOut(BaseModel):
    """The admin activity view: observation only."""

    days: int
    since: datetime
    daily: list[DayActivityOut]
    totals: ActivityTotalsOut
    agents: list[AgentActivityOut]
    fleets: list[FleetActivityOut]


class FeedbackLogEntryOut(BaseModel):
    """The entry a feedback log row rates, as a list row shows it."""

    id: str
    kind: Kind
    summary: str
    author: str
    scope: str
    fleet_id: str | None
    state: EntryState


class FeedbackLogItemOut(BaseModel):
    """One row of ``GET /v1/admin/feedback``: who gave which verdict on
    which entry. ``reporter`` is the registered name, ``agent`` the label
    the row was filed under."""

    reporter: str
    agent: str
    verdict: Verdict
    note: str | None
    updated_at: datetime | None
    entry: FeedbackLogEntryOut

    @classmethod
    def from_item(cls, item: FeedbackLogItem) -> FeedbackLogItemOut:
        fb = item.feedback
        return cls(
            reporter=fb.user,
            agent=fb.agent,
            verdict=fb.verdict,
            note=fb.note,
            updated_at=fb.updated_at,
            entry=FeedbackLogEntryOut(
                id=fb.entry_id,
                kind=item.entry_kind,
                summary=item.entry_summary,
                author=item.entry_author,
                scope=item.entry_scope,
                fleet_id=item.entry_fleet_id,
                state=item.entry_state,
            ),
        )


class FlaggedEntryOut(BaseModel):
    """One row of ``GET /v1/admin/flagged`` (ADR 0093): an active entry
    with a standing ``stale`` or ``wrong`` verdict. ``orphaned`` is true
    when its author is revoked or has not written for 30 days."""

    id: str
    kind: Kind
    summary: str
    author: str
    scope: str
    fleet_id: str | None
    first_flag_at: datetime
    last_flag_at: datetime
    helpful: int
    stale: int
    wrong: int
    orphaned: bool

    @classmethod
    def from_flagged(cls, item: FlaggedEntry) -> FlaggedEntryOut:
        return cls(
            id=item.entry_id,
            kind=item.kind,
            summary=item.summary,
            author=item.author,
            scope=item.scope,
            fleet_id=item.fleet_id,
            first_flag_at=item.first_flag_at,
            last_flag_at=item.last_flag_at,
            helpful=item.helpful,
            stale=item.stale,
            wrong=item.wrong,
            orphaned=item.orphaned,
        )


class MetricsOut(BaseModel):
    """The minimal usage-counters report (ROADMAP §3.3, admin-gated)."""

    entries: EntriesMetrics
    fleets: FleetsMetrics
    agents: AgentsMetrics
    searches: SearchesMetrics
    feedback: FeedbackMetrics = FeedbackMetrics()
    health: HealthMetrics | None = None
