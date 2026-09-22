"""Pydantic schemas for the Hivemind REST surface (SPEC.md §5.1).

Request/response models are plain and small: they mirror the domain
entities (Entry, EntryDraft, EntryFilters, Feedback) and the service
results (Hit) without leaking implementation details (embeddings,
raw config) across the wire.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, field_validator

from hivemind.domain.access import Agent, Fleet
from hivemind.domain.entry import EntityKind, Entry, ImportanceSource, Kind, SourceType
from hivemind.domain.feedback import Verdict
from hivemind.services.search import Hit


def _to_utc(value: datetime | None) -> datetime | None:
    """Normalize a naive datetime to UTC (SPEC.md §6.4 / §8: the pool is
    UTC-based; a bare timestamp is assumed UTC so downstream asyncpg
    ``timestamptz`` columns and the recency rescore never see a naive
    value)."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


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
    # Omitted -> the default (3) with ``importance_source=default``;
    # supplied -> that value with ``importance_source=caller``
    # (ROADMAP §4.5). The validated 1..5 range still applies when supplied
    # (``EntryDraft.__post_init__``).
    importance: int | None = None
    # ADR 0011: an omitted scope defaults to the highest scope the caller's
    # trust level permits (L1 -> self; L2/L3 -> fleet; legacy/admin -> org);
    # an explicit out-of-permission scope is rejected (403).
    scope: str | None = None
    supersedes: list[str] = []
    agent: str | None = None

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
    """A machine-extracted entity facet (ADR 0016, SPEC §13).

    ``name`` is open vocabulary (the extractor's output); ``kind`` is the
    closed six-value ``EntityKind`` vocabulary (display-only in v1 —
    filters match on names, not kinds).
    """

    name: str
    kind: EntityKind


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
    # Server-derived, not client-settable (ROADMAP §4.5): whether the
    # writer supplied ``importance`` or it fell out of the default.
    importance_source: ImportanceSource
    scope: str
    fleet_id: str | None = None
    state: str
    superseded_by: str | None = None
    withdrawn_reason: str | None = None
    # Machine-extracted entity facets (ADR 0016, SPEC §13): set once at
    # write time, never mutated (ADR 0001). ``entities_model`` records
    # which extractor model produced them (provenance, symmetric with
    # the internal ``embedding_model``); both are absent when extraction
    # was off or failed (best-effort, SPEC §13.4).
    entities: list[EntityOut] = []
    entities_model: str | None = None
    # The supersession chain (SPEC.md §5.1 ``?history=true``): optional,
    # populated only when the caller asks for it.
    history: dict[str, list[EntryOut]] | None = None

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
    # ADR 0016 / SPEC §13: filter by machine-extracted entity names
    # (AND-semantics, case-insensitive — the store layer matches them;
    # kinds are display-only, not filterable in v1).
    entities: list[str] = []
    scope: str | None = None
    author: str | None = None
    agent: str | None = None
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    include_inactive: bool = False
    limit: int | None = None
    offset: int | None = None

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
    """Activate a pending agent (ADR 0012): set its trust level + home
    fleet; the agent key is issued once. The trust level defaults to
    `lurker` (1) (SPEC §5.1)."""

    trust_level: int = 1
    home_fleet_id: str


class UpdateAgentRequest(BaseModel):
    """Change an agent's trust level and/or home fleet (the SPEC §5.1
    ``PATCH /v1/admin/agents/{name}`` body; ADR 0011). At least one
    field must be supplied."""

    trust_level: int | None = None
    home_fleet_id: str | None = None


class CreateFleetRequest(BaseModel):
    """Create a named fleet (ADR 0011)."""

    name: str


class AgentOut(BaseModel):
    """A registered agent (ADR 0012)."""

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


class FleetOut(BaseModel):
    """A fleet (ADR 0011)."""

    id: str
    name: str
    created_at: str

    @classmethod
    def from_fleet(cls, fleet: Fleet) -> FleetOut:
        return cls(id=fleet.id, name=fleet.name, created_at=fleet.created_at.isoformat())


class KeyIssuedOut(BaseModel):
    """A freshly issued key (returned **once**, ADR 0012)."""

    key: str

    note: str = "store this key now; it is shown only once"


# --- Usage counters (ROADMAP §3.3, Tier 3) ----------------------------------


class EntriesMetrics(BaseModel):
    """Entry usage counters (ROADMAP §3.3)."""

    total: int
    active: int
    inactive: int
    by_scope: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    # ROADMAP §4.5: is anyone actually setting ``importance``, or is
    # every entry riding the default?
    by_importance_source: dict[str, int] = {}


class FleetsMetrics(BaseModel):
    """Fleet usage counters (writes per fleet, the §12 counter)."""

    total: int
    writes_by_fleet: dict[str, int] = {}


class AgentsMetrics(BaseModel):
    """Agent usage counters (the §12 counters: trust-level distribution,
    pending-agent count, ADR 0012)."""

    total: int
    pending: int
    active: int
    by_trust_level: dict[str, int] = {}


class MetricsOut(BaseModel):
    """The minimal usage-counters report (ROADMAP §3.3, admin-gated)."""

    entries: EntriesMetrics
    fleets: FleetsMetrics
    agents: AgentsMetrics
