"""Hivemind domain entities (SPEC.md §4).

Pure data, no I/O: this layer is shared by every adapter (in-memory
store, Postgres store, API schemas, MCP tools). Identity is a UUID
string so the domain stays storage-agnostic.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from hivemind.domain.validation import (
    MAX_BODY_CHARS,
    MAX_FILTER_VALUE_CHARS,
    MAX_ID_CHARS,
    MAX_SEE_ALSO,
    MAX_SOURCE_REF_CHARS,
    MAX_SOURCES,
    MAX_SUPERSEDES,
    InvalidInput,
    check_datetime,
    check_filter_values,
    check_id,
    check_no_nul,
    check_payload,
    check_tags,
    check_text,
)


def new_entry_id() -> str:
    """A fresh entry ID (uuid4, stored as a plain string in the domain)."""
    return str(uuid.uuid4())


# SPEC.md §4.1: the summary is a short blurb (≤ ~280 chars), not a body.
_SUMMARY_MAX_CHARS = 280

# ADR 0016 / SPEC §13: extracted entity names are bounded display strings.
_ENTITY_NAME_MAX_CHARS = 128

# Embedded-text body budget in whitespace-delimited words, never tokenizer
# tokens (ADR 0021). It bounds dilution and cost, not a model limit. The
# single default for ``Settings``, the embedder and the extractor.
DEFAULT_PREFIX_TOKENS = 2000

# A "prefix token" is one run of non-whitespace characters (ADR 0021).
_WORD_RE = re.compile(r"\S+")

# Character ceiling beside the word budget (ADR 0040): a body with no
# whitespace (base64, minified JSON, CJK) is "one word" of any length.
EMBED_BODY_MAX_CHARS = 20_000
# The ceiling scales with a raised word budget so the knob is not
# silently capped.
EMBED_CHARS_PER_PREFIX_TOKEN = 10


def embed_body_char_ceiling(prefix_tokens: int) -> int:
    return max(EMBED_BODY_MAX_CHARS, prefix_tokens * EMBED_CHARS_PER_PREFIX_TOKEN)


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
    """How an entry's ``importance`` was set (ROADMAP §4.5), recorded
    server-side so operators can see whether agents set it at all."""

    CALLER = "caller"
    DEFAULT = "default"


class EntityKind(StrEnum):
    """The closed kind vocabulary of an extracted entity (ADR 0016, SPEC
    §13); names are open vocabulary."""

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
    """A machine-extracted entity facet (ADR 0016, SPEC §13); ``name`` is
    trimmed, non-empty and bounded."""

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
    # "See also" links to other entries (ADR 0057): stored beside the
    # entry, not on it, and readable from both ends.
    see_also: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Normalise raw strings ("fact") to enum members: ``matches``
        # compares by identity and serialisers read ``.value``.
        object.__setattr__(self, "kind", Kind(self.kind))
        object.__setattr__(self, "importance_source", ImportanceSource(self.importance_source))
        if not 1 <= self.importance <= 5:
            raise ValueError(f"importance must be 1..5, got {self.importance}")
        if self.importance_source is ImportanceSource.DEFAULT and self.importance != 3:
            raise ValueError(
                "importance_source=default requires the default importance (3), "
                f"got importance={self.importance}"
            )
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
        self._validate_bounds()

    def _validate_bounds(self) -> None:
        """NUL + size bounds (ADR 0040): runs at construction, so every
        surface rejects the same inputs before the embedder is called."""
        check_no_nul(self.summary, "summary")
        # author/agent can be a legacy registered name (unbounded before
        # ADR 0040), so only what Postgres cannot store is refused here.
        check_no_nul(self.author, "author")
        check_no_nul(self.agent, "agent")
        check_datetime(self.occurred_at, "occurred_at")
        check_text(self.body, "body", MAX_BODY_CHARS)
        check_tags(self.tags)
        check_payload(self.payload)
        if len(self.sources) > MAX_SOURCES:
            raise InvalidInput(f"at most {MAX_SOURCES} sources (got {len(self.sources)})")
        for source in self.sources:
            check_text(source.ref, "source ref", MAX_SOURCE_REF_CHARS)
        if len(self.supersedes) > MAX_SUPERSEDES:
            raise InvalidInput(f"at most {MAX_SUPERSEDES} supersedes targets")
        for target in self.supersedes:
            check_id(target, "supersedes id")
        if len(self.see_also) > MAX_SEE_ALSO:
            raise InvalidInput(f"at most {MAX_SEE_ALSO} see_also entries")
        for target in self.see_also:
            check_id(target, "see_also id")
        check_text(self.scope, "scope", MAX_ID_CHARS)
        if self.fleet_id is not None:
            check_text(self.fleet_id, "fleet_id", MAX_ID_CHARS)

    def resolved_occurred_at(self) -> datetime:
        """The entry's memory date: the supplied value, or now."""
        return self.occurred_at or _utcnow()

    @property
    def supersedes_ids(self) -> tuple[str, ...]:
        return self.supersedes


@dataclass(frozen=True, slots=True)
class Entry:
    """A stored unit of memory (SPEC.md §4.1).

    Immutable (ADR 0001): corrected only by supersession or withdrawal.
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
    # Set on the entry ``create_entry`` returns; ``PgStore`` reads leave it
    # ``None`` (nothing reads a stored vector back; search ranks it in SQL).
    embedding: tuple[float, ...] | None = None
    embedding_model: str | None = None
    state: EntryState = EntryState.ACTIVE
    superseded_by: str | None = None
    withdrawn_reason: str | None = None
    # Extracted at write time (ADR 0016, SPEC §13).
    entities: tuple[ExtractedEntity, ...] = ()
    entities_model: str | None = None


@dataclass(frozen=True, slots=True)
class UsageCount:
    """One grouped row of the usage counters (ROADMAP §3.3): how many
    entries share this (scope, kind, importance_source, author, fleet_id)
    and whether they are still ``active`` (any other state is inactive)."""

    scope: str
    kind: Kind
    importance_source: ImportanceSource
    author: str
    fleet_id: str | None
    active: bool
    count: int


@dataclass(frozen=True, slots=True)
class SearchCount:
    """Searches made from one home fleet (ADR 0056): how many first-page
    searches its agents ran and how many of them found nothing.
    ``fleet_id`` is ``None`` for callers without a home fleet (admin keys,
    fleetless agents)."""

    fleet_id: str | None
    searches: int
    empty: int


@dataclass(frozen=True, slots=True)
class EntryFilters:
    """Filter set shared by search and list (SPEC.md §5.3).

    ``tags`` and ``entities`` (names, case-insensitive; ADR 0016) are AND.
    Only ``active`` entries match unless ``include_inactive`` (SPEC.md §6.3).
    """

    kind: Kind | None = None
    tags: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    # Internal-only (MetricsService, ROADMAP §4.5): not a public SPEC §5.3
    # filter. Do not expose it on REST or MCP without updating SPEC §5.3.
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
    # At least one stale/wrong report (ADR 0054). Not evaluated by
    # ``matches`` (feedback is not on ``Entry``); each store applies it.
    flagged: bool = False

    def validate(self) -> None:
        """Bounds for filters built from **caller input** (ADR 0040).
        Service-built filters skip this, so a legacy stored value can never
        make an internal query fail."""
        check_filter_values(self.tags, "tags")
        check_filter_values(self.entities, "entities")
        for field, value in (
            ("scope", self.scope),
            ("fleet_id", self.fleet_id),
            ("author", self.author),
            ("agent", self.agent),
        ):
            check_text(value, field, MAX_FILTER_VALUE_CHARS)
        for field, moment in (
            ("occurred_from", self.occurred_from),
            ("occurred_to", self.occurred_to),
            ("created_from", self.created_from),
            ("created_to", self.created_to),
        ):
            check_datetime(moment, field)

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


def _body_prefix(body: str, prefix_tokens: int) -> str:
    """The first ``prefix_tokens`` whitespace-delimited words of ``body``,
    with the original spacing between them preserved (ADR 0021).

    Cut at the end of the last word in budget, so paragraph structure
    survives. A body within budget is returned unchanged.
    """
    if prefix_tokens < 0:
        raise ValueError(f"prefix_tokens must be non-negative, got {prefix_tokens}")
    cut = 0
    for words, match in enumerate(_WORD_RE.finditer(body), start=1):
        if words > prefix_tokens:
            return body[:cut]
        cut = match.end()
    return body


def embeddable_text(
    summary: str,
    body: str | None,
    prefix_tokens: int = DEFAULT_PREFIX_TOKENS,
) -> str:
    """The text an entry is embedded from (SPEC.md §7): the summary whole,
    plus a bounded prefix of the body.

    The bound is in whitespace-delimited words (ADR 0021). The extractor
    reads the same text (SPEC §13.1).
    """
    text = summary
    if body:
        prefix = _body_prefix(body, prefix_tokens)[: embed_body_char_ceiling(prefix_tokens)]
        text = f"{text}\n{prefix}"
    return text


__all__ = [
    "DEFAULT_PREFIX_TOKENS",
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
