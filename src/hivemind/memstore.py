"""In-memory Store implementation (SPEC.md §4).

A faithful, dependency-free reference implementation of the ``Store``
port: used by the unit/integration suite (without a live database)
and by local dev mode. Postgres-specific adapters live elsewhere and
implement the same port.

Determinism notes (for test stability):
* Keyword scoring: number of *distinct* query tokens present in the
  entry's embeddable text, tie-broken by ``created_at`` then ``id``.
* Vector scoring: cosine similarity, tie-broken by ``created_at`` then
  ``id``.
* ``created_at`` is caller-supplied via ``clock`` (defaults to now).
"""

from __future__ import annotations

import math
import threading
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from hivemind.domain.access import (
    Agent,
    AgentStatus,
    Fleet,
    TrustLevel,
    Visibility,
    entry_is_visible,
)
from hivemind.domain.entry import (
    Entry,
    EntryDraft,
    EntryFilters,
    EntryState,
    ExtractedEntity,
    embeddable_text,
    new_entry_id,
)
from hivemind.domain.feedback import (
    Feedback,
    FeedbackCounts,
    Verdict,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _tokens(text: str) -> set[str]:
    return set(text.lower().split())


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


class MemoryStore:
    """Thread-safe in-memory Store (a ``dict`` under a lock)."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock: Callable[[], datetime] = clock or _utcnow
        self._lock = threading.RLock()
        self._entries: dict[str, Entry] = {}
        self._feedback: dict[tuple[str, str, str], Feedback] = {}
        self._fleets: dict[str, Fleet] = {}
        self._agents: dict[str, Agent] = {}

    # -- write path -------------------------------------------------------

    async def create_entry(
        self,
        draft: EntryDraft,
        embedding: list[float] | None = None,
        embedding_model: str | None = None,
        entities: tuple[ExtractedEntity, ...] = (),
        entities_model: str | None = None,
    ) -> Entry:
        """Insert a new entry, flipping any ``draft.supersedes`` targets to
        the ``superseded`` state (SPEC.md §4.1, §7).

        ``embedding`` / ``embedding_model`` are recorded on the entry so
        the model that produced the vector is traceable (SPEC.md §7).
        """
        now = self._clock()
        entry = Entry(
            id=new_entry_id(),
            kind=draft.kind,
            summary=draft.summary,
            author=draft.author,
            agent=draft.agent,
            occurred_at=draft.resolved_occurred_at(),
            created_at=now,
            body=draft.body,
            payload=dict(draft.payload) if draft.payload is not None else None,
            sources=tuple(draft.sources),
            tags=tuple(draft.tags),
            importance=draft.importance,
            importance_source=draft.importance_source,
            scope=draft.scope,
            fleet_id=draft.fleet_id,
            embedding=tuple(embedding) if embedding is not None else None,
            embedding_model=embedding_model,
            entities=tuple(entities),  # ADR 0016: machine-extracted facets
            entities_model=entities_model,
        )
        with self._lock:
            self._entries[entry.id] = entry
            # Explicit supersessions: flip the targets (SPEC.md §4.1).
            for target_id in draft.supersedes:
                target = self._entries.get(target_id)
                if target is not None and target.state is EntryState.ACTIVE:
                    self._entries[target_id] = _with_state(
                        target,
                        EntryState.SUPERSEDED,
                        superseded_by=entry.id,
                    )
        return entry

    async def withdraw_entry(self, entry_id: str, reason: str | None, by_user: str) -> Entry:
        with self._lock:
            entry = self._entries.get(entry_id)
            if entry is None:
                raise KeyError(entry_id)
            if entry.state is not EntryState.ACTIVE:
                raise ValueError(f"entry {entry_id} is not active (state={entry.state.value})")
            updated = _with_state(
                entry,
                EntryState.WITHDRAWN,
                withdrawn_reason=reason,
            )
            self._entries[entry_id] = updated
            return updated

    async def record_feedback(self, feedback: Feedback) -> None:
        with self._lock:
            self._feedback[(feedback.entry_id, feedback.user, feedback.agent)] = feedback

    # -- read path --------------------------------------------------------

    async def get_entry(self, entry_id: str) -> Entry | None:
        with self._lock:
            entry = self._entries.get(entry_id)
            return _copy(entry) if entry is not None else None

    async def get_entries(self, entry_ids: list[str]) -> dict[str, Entry]:
        with self._lock:
            result: dict[str, Entry] = {}
            for eid in entry_ids:
                entry = self._entries.get(eid)
                if entry is not None:
                    result[eid] = _copy(entry)
            return result

    async def list_entries(
        self,
        filters: EntryFilters,
        limit: int = 20,
        offset: int = 0,
        *,
        visibility: Visibility | None = None,
    ) -> list[Entry]:
        with self._lock:
            matches = [
                _copy(entry)
                for entry in self._entries.values()
                if filters.matches(_copy(entry)) and self._visible(entry, visibility)
            ]
            # Most-recent-first (SPEC.md §11.4), tie-broken by id DESC to
            # match PgStore's `ORDER BY created_at DESC, id DESC` exactly.
            matches.sort(key=lambda e: (e.created_at, e.id), reverse=True)
            return matches[offset : offset + limit]

    async def count_entries(
        self, filters: EntryFilters, *, visibility: Visibility | None = None
    ) -> int:
        """Count entries matching ``filters`` (the minimal usage-counters
        surface, ROADMAP §3.3) — a cheap count, not a full fetch."""
        with self._lock:
            return sum(
                1
                for entry in self._entries.values()
                if filters.matches(_copy(entry)) and self._visible(entry, visibility)
            )

    async def search_keyword(
        self, query: str, filters: EntryFilters, limit: int, *, visibility: Visibility | None = None
    ) -> list[str]:
        query_tokens = _tokens(query)
        if not query_tokens:
            return []
        with self._lock:
            rows: list[tuple[float, datetime, str]] = []
            for entry in self._entries.values():
                if not filters.matches(entry) or not self._visible(entry, visibility):
                    continue
                # The keyword haystack is the same bounded text the entry
                # is embedded from (default prefix-token budget, ADR 0021):
                # the reference store has no Settings, and the two streams
                # should see the same text.
                haystack = _tokens(embeddable_text(entry.summary, entry.body)) | {
                    tag.lower() for tag in entry.tags
                }
                overlap = len(query_tokens & haystack)
                if overlap == 0:
                    continue
                rows.append((float(overlap), entry.created_at, entry.id))
            rows.sort(key=lambda r: (-r[0], r[1], r[2]))
            return [eid for _, _, eid in rows[:limit]]

    async def search_vector(
        self,
        vector: list[float],
        filters: EntryFilters,
        limit: int,
        *,
        visibility: Visibility | None = None,
    ) -> list[str]:
        with self._lock:
            rows: list[tuple[float, datetime, str]] = []
            for entry in self._entries.values():
                if not filters.matches(entry) or not self._visible(entry, visibility):
                    continue
                if entry.embedding is None:
                    continue
                sim = cosine_similarity(vector, list(entry.embedding))
                if sim <= 0.0:
                    continue
                rows.append((sim, entry.created_at, entry.id))
            rows.sort(key=lambda r: (-r[0], r[1], r[2]))
            return [eid for _, _, eid in rows[:limit]]

    def _visible(self, entry: Entry, visibility: Visibility | None) -> bool:
        """Whether ``entry`` passes the optional visibility filter.

        ``visibility is None`` → v1 flat-pool behavior (no filter);
        otherwise apply the trust-level matrix (ADR 0011).
        """
        return visibility is None or entry_is_visible(entry, visibility)

    async def feedback_counts(self, entry_id: str) -> FeedbackCounts:
        with self._lock:
            return _count_for(self._feedback, entry_id)

    async def quality_counts(self, entry_ids: list[str]) -> dict[str, FeedbackCounts]:
        with self._lock:
            return {
                eid: _count_for(self._feedback, eid) for eid in entry_ids if eid in self._entries
            }

    async def health_check(self) -> bool:
        """In-memory pool: always healthy (ADR 0019)."""
        return True

    # -- fleets (ADR 0011) --------------------------------------------------

    async def create_fleet(self, name: str) -> Fleet:
        """Create a named fleet (ADR 0011). ``ValueError`` if the name exists."""
        with self._lock:
            for existing in self._fleets.values():
                if existing.name == name:
                    raise ValueError(f"fleet already exists: {name!r}")
            fleet = Fleet(id=str(uuid.uuid4()), name=name, created_at=self._clock())
            self._fleets[fleet.id] = fleet
            return fleet

    async def list_fleets(self) -> list[Fleet]:
        with self._lock:
            return list(self._fleets.values())

    async def get_fleet(self, fleet_id: str) -> Fleet | None:
        with self._lock:
            return self._fleets.get(fleet_id)

    # -- agent registration / activation (ADR 0012) --------------------------

    async def register_agent(self, name: str, owner_alias: str | None = None) -> Agent:
        """Register (or re-register) an agent (ADR 0012). Idempotent: an
        existing record is returned unchanged (only ``owner_alias`` is
        back-filled if newly supplied); a new record is ``pending``.
        """
        with self._lock:
            existing = self._agents.get(name)
            if existing is not None:
                if owner_alias is not None and existing.owner_alias is None:
                    existing = replace(existing, owner_alias=owner_alias)
                    self._agents[name] = existing
                return existing
            agent = Agent(
                name=name,
                status=AgentStatus.PENDING,
                trust_level=TrustLevel.UNTRUSTED,
                owner_alias=owner_alias,
                created_at=self._clock(),
            )
            self._agents[name] = agent
            return agent

    async def get_agent(self, name: str) -> Agent | None:
        with self._lock:
            return self._agents.get(name)

    async def list_agents(self) -> list[Agent]:
        with self._lock:
            return list(self._agents.values())

    async def activate_agent(
        self, name: str, *, trust_level: TrustLevel, home_fleet_id: str
    ) -> Agent:
        """Activate a pending agent: set trust level + home fleet, flip to
        ``active`` (ADR 0012). ``KeyError`` if unknown."""
        with self._lock:
            agent = self._agents.get(name)
            if agent is None:
                raise KeyError(f"unknown agent: {name}")
            activated = replace(
                agent,
                status=AgentStatus.ACTIVE,
                trust_level=trust_level,
                home_fleet_id=home_fleet_id,
                activated_at=self._clock(),
            )
            self._agents[name] = activated
            return activated

    async def set_agent_trust_level(self, name: str, level: TrustLevel) -> Agent:
        """Promote/demote an agent's trust level (ADR 0011). ``KeyError`` if
        unknown. Demotion to level 0 is *dormant* (still active, no access).
        """
        with self._lock:
            agent = self._agents.get(name)
            if agent is None:
                raise KeyError(f"unknown agent: {name}")
            updated = replace(agent, trust_level=level)
            self._agents[name] = updated
            return updated

    async def set_agent_home_fleet(self, name: str, fleet_id: str) -> Agent:
        """Re-parent an agent to a new home fleet (ADR 0011). The agent's
        earlier ``fleet``-scoped entries stay in the fleet they were written
        into (never re-parented). ``KeyError`` if unknown.
        """
        with self._lock:
            agent = self._agents.get(name)
            if agent is None:
                raise KeyError(f"unknown agent: {name}")
            updated = replace(agent, home_fleet_id=fleet_id)
            self._agents[name] = updated
            return updated


def _count_for(feedback: dict[tuple[str, str, str], Feedback], entry_id: str) -> FeedbackCounts:
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


def _with_state(
    entry: Entry,
    state: EntryState,
    *,
    superseded_by: str | None = None,
    withdrawn_reason: str | None = None,
) -> Entry:
    return Entry(
        id=entry.id,
        kind=entry.kind,
        summary=entry.summary,
        author=entry.author,
        agent=entry.agent,
        occurred_at=entry.occurred_at,
        created_at=entry.created_at,
        body=entry.body,
        payload=entry.payload,
        sources=entry.sources,
        tags=entry.tags,
        importance=entry.importance,
        importance_source=entry.importance_source,
        scope=entry.scope,
        fleet_id=entry.fleet_id,
        embedding=entry.embedding,
        embedding_model=entry.embedding_model,
        state=state,
        superseded_by=superseded_by or entry.superseded_by,
        withdrawn_reason=withdrawn_reason or entry.withdrawn_reason,
        entities=entry.entities,  # ADR 0016
        entities_model=entry.entities_model,
    )


def _copy(entry: Entry) -> Entry:
    """Return a fresh Entry with copied containers (defensive copy)."""
    return Entry(
        id=entry.id,
        kind=entry.kind,
        summary=entry.summary,
        author=entry.author,
        agent=entry.agent,
        occurred_at=entry.occurred_at,
        created_at=entry.created_at,
        body=entry.body,
        payload=dict(entry.payload) if entry.payload is not None else None,
        sources=tuple(entry.sources),
        tags=tuple(entry.tags),
        importance=entry.importance,
        importance_source=entry.importance_source,
        scope=entry.scope,
        fleet_id=entry.fleet_id,
        embedding=tuple(entry.embedding) if entry.embedding is not None else None,
        embedding_model=entry.embedding_model,
        state=entry.state,
        superseded_by=entry.superseded_by,
        withdrawn_reason=entry.withdrawn_reason,
        entities=tuple(entry.entities),  # ADR 0016
        entities_model=entry.entities_model,
    )


# EntryDraft is used only for the create_entry signature type.
_ = EntryDraft
