"""Hivemind domain entities (SPEC.md §4).

Pure data, no I/O: this layer is shared by every adapter (in-memory
store, Postgres store, API schemas, MCP tools). Identity is a UUID
string so the domain stays storage-agnostic.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def new_entry_id() -> str:
    """A fresh entry ID (uuid4, stored as a plain string in the domain)."""
    return str(uuid.uuid4())


# SPEC.md §4.1: the summary is a short blurb (≤ ~280 chars), not a body.
_SUMMARY_MAX_CHARS = 280

# ADR 0016 / SPEC §13: a machine-extracted entity name is a bounded
# display string (the extractor validates before it is stored).
_ENTITY_NAME_MAX_CHARS = 128


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Kind(StrEnum):
    """The category of an entry (SPEC.md §4.1)."""

    FACT = "fact"
    INSIGHT = "insight"
    DECISION = "decision"


class EntryState(StrEnum):
    """Lifecycle state of an entry (SPEC.md §4.1)."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"


class ImportanceSource(StrEnum):
    """How an entry's ``importance`` was set (ROADMAP §4.5).

    ``importance`` feeds retrieval scoring (SPEC §6.4); a field nobody
    sets is a dead ranking input. Recorded server-side so an operator
    can tell whether agents are actually setting it or every entry
    rides the default — never client-settable itself.
    """

    CALLER = "caller"
    DEFAULT = "default"


class EntityKind(StrEnum):
    """The closed type vocabulary of an extracted entity (ADR 0016, SPEC §13).

    A fixed six-value set over *open* names: the extractor may only pick
    from these kinds; the names themselves are open vocabulary.
    """

    PERSON = "person"
    ORGANIZATION = "organization"
    SYSTEM = "system"
    SERVICE = "service"
    ARTIFACT = "artifact"
    CONCEPT = "concept"


class SourceType(StrEnum):
    """What a source reference points at."""

    PATH = "path"
    URL = "url"
    SESSION = "session"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class Source:
    """A provenance pointer to where an entry came from (file, URL, session)."""

    type: SourceType
    ref: str


@dataclass(frozen=True, slots=True)
class ExtractedEntity:
    """A machine-extracted entity facet of an entry (ADR 0016, SPEC §13).

    ``name`` is open vocabulary (trimmed, non-empty, bounded); ``kind``
    is the closed ``EntityKind`` vocabulary. Set once at write time by
    the extractor, never mutated (ADR 0001).
    """

    name: str
    kind: EntityKind

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not name:
            raise ValueError("entity name must be non-empty")
        if len(name) > _ENTITY_NAME_MAX_CHARS:
            raise ValueError(
                f"entity name must be at most {_ENTITY_NAME_MAX_CHARS} characters (got {len(name)})"
            )
        object.__setattr__(self, "name", name)


@dataclass(frozen=True, slots=True)
class EntryDraft:
    """A write request: everything the writer supplies for a new entry.

    ``occurred_at`` defaults to now but is backdatable (the "memory
    date"); ``created_at`` is assigned by the store at insert time.
    """

    kind: Kind
    summary: str
    author: str
    agent: str
    body: str | None = None
    payload: dict[str, Any] | None = None
    sources: tuple[Source, ...] = ()
    tags: tuple[str, ...] = ()
    occurred_at: datetime | None = None
    importance: int = 3
    importance_source: ImportanceSource = ImportanceSource.DEFAULT
    scope: str = "org"
    fleet_id: str | None = None
    supersedes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 1 <= self.importance <= 5:
            raise ValueError(f"importance must be 1..5, got {self.importance}")
        if not self.summary.strip():
            raise ValueError("summary must be non-empty")
        if len(self.summary) > _SUMMARY_MAX_CHARS:
            raise ValueError(
                f"summary must be at most {_SUMMARY_MAX_CHARS} characters (got {len(self.summary)})"
            )
        if not self.author.strip():
            raise ValueError("author must be non-empty")
        if not self.agent.strip():
            raise ValueError("agent must be non-empty")

    def resolved_occurred_at(self) -> datetime:
        """The entry's memory date: the supplied value, or now."""
        return self.occurred_at or _utcnow()

    @property
    def supersedes_ids(self) -> tuple[str, ...]:
        return self.supersedes


@dataclass(frozen=True, slots=True)
class Entry:
    """A stored unit of memory (SPEC.md §4.1).

    Immutable by design (ADR 0001): corrections happen only via
    supersession (a new entry) or withdrawal (a state flip), never by
    editing an existing entry's fields.
    """

    id: str
    kind: Kind
    summary: str
    author: str
    agent: str
    occurred_at: datetime
    created_at: datetime
    body: str | None = None
    payload: dict[str, Any] | None = None
    sources: tuple[Source, ...] = ()
    tags: tuple[str, ...] = ()
    importance: int = 3
    importance_source: ImportanceSource = ImportanceSource.DEFAULT
    scope: str = "org"
    fleet_id: str | None = None
    embedding: tuple[float, ...] | None = None
    embedding_model: str | None = None
    state: EntryState = EntryState.ACTIVE
    superseded_by: str | None = None
    withdrawn_reason: str | None = None
    # Machine-extracted entity facets (ADR 0016, SPEC §13): set once at
    # write time by the extractor, never mutated (ADR 0001).
    entities: tuple[ExtractedEntity, ...] = ()
    entities_model: str | None = None


@dataclass(frozen=True, slots=True)
class EntryFilters:
    """Filter set shared by search and list (SPEC.md §5.3).

    ``tags`` is AND-semantics: an entry must carry every listed tag.
    ``entities`` (ADR 0016, SPEC §13) is AND-semantics over extracted
    entity *names*, matched case-insensitively: an entry must have an
    extracted entity whose lower-cased name equals each lower-cased
    filter name.
    ``include_inactive`` surfaces superseded/withdrawn entries; by
    default only ``active`` entries are visible (SPEC.md §6.3).
    """

    kind: Kind | None = None
    tags: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    importance_source: ImportanceSource | None = None
    scope: str | None = None
    fleet_id: str | None = None
    author: str | None = None
    agent: str | None = None
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    include_inactive: bool = False

    def matches(self, entry: Entry) -> bool:
        """Pure filter evaluation — shared by every store adapter."""
        if not self.include_inactive and entry.state is not EntryState.ACTIVE:
            return False
        if self.kind is not None and entry.kind is not self.kind:
            return False
        if (
            self.importance_source is not None
            and entry.importance_source is not self.importance_source
        ):
            return False
        if self.scope is not None and entry.scope != self.scope:
            return False
        if self.fleet_id is not None and entry.fleet_id != self.fleet_id:
            return False
        if self.author is not None and entry.author != self.author:
            return False
        if self.agent is not None and entry.agent != self.agent:
            return False
        if self.occurred_from is not None and entry.occurred_at < self.occurred_from:
            return False
        if self.occurred_to is not None and entry.occurred_at > self.occurred_to:
            return False
        if self.created_from is not None and entry.created_at < self.created_from:
            return False
        if self.created_to is not None and entry.created_at > self.created_to:
            return False
        entry_tags = set(entry.tags)
        if any(tag not in entry_tags for tag in self.tags):
            return False
        if self.entities:
            have = {e.name.lower() for e in entry.entities}
            if any(name.lower() not in have for name in self.entities):
                return False
        return True


def embeddable_text(
    summary: str,
    body: str | None,
    prefix_chars: int = 2048,
) -> str:
    """The text an entry is embedded from (SPEC.md §7): the summary plus a
    bounded prefix of the body (~512 tokens ≈ 2048 chars in v1)."""
    text = summary
    if body:
        text = f"{text}\n{body[:prefix_chars]}"
    return text


__all__ = [
    "EntityKind",
    "Entry",
    "EntryDraft",
    "EntryFilters",
    "EntryState",
    "ExtractedEntity",
    "ImportanceSource",
    "Kind",
    "Source",
    "SourceType",
    "embeddable_text",
    "new_entry_id",
]
