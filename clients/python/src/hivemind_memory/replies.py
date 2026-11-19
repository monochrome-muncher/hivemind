"""The shapes of the tools' replies (SPEC §5.2), as the server sends them.

Keys the server leaves out when they have nothing to say sit in a ``total=False`` part. A reply
may carry keys a newer server added; they are there at runtime even when not listed here.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

EntryKind = Literal["fact", "insight", "decision"]
Verdict = Literal["helpful", "stale", "wrong"]
Recency = Literal["default", "prefer_recent", "ignore_age"]
EntryState = Literal["active", "superseded", "withdrawn"]


class Source(TypedDict):
    type: Literal["path", "url", "session", "other"]
    ref: str


class Entity(TypedDict):
    name: str
    kind: str


class FeedbackCounts(TypedDict):
    helpful: int
    stale: int
    wrong: int


class Warning(TypedDict):
    code: str
    message: str


class Entry(TypedDict):
    id: str
    kind: EntryKind
    summary: str
    author: str
    agent: str | None
    body: str | None
    payload: dict[str, Any] | None
    sources: list[Source]
    tags: list[str]
    importance: int
    importance_source: str
    scope: str
    fleet_id: str | None
    state: EntryState
    superseded_by: str | None
    withdrawn_reason: str | None
    withdrawn_at: str | None
    entities: list[Entity]
    entities_model: str | None
    occurred_at: str
    created_at: str


class RecentFeedback(TypedDict):
    verdict: Verdict
    note: str | None
    reporter: str
    updated_at: str | None


class _Feedback(FeedbackCounts):
    recent: list[RecentFeedback]


class Feedback(_Feedback, total=False):
    last_confirmed_at: str


class Link(TypedDict):
    id: str
    kind: EntryKind
    summary: str
    author: str
    state: EntryState
    fleet_id: str | None


class _EntryDetail(Entry):
    feedback: Feedback
    see_also: list[Link]
    linked_from: list[Link]


class EntryDetail(_EntryDetail, total=False):
    """``get`` of one id; ``successors`` and ``superseded`` come with ``include_history``."""

    successors: list[Entry]
    superseded: list[Entry]


class Entries(TypedDict):
    """``get`` of a list of ids: the readable ones, then the ids that named nothing readable."""

    entries: list[EntryDetail]
    not_found: list[str]


class Correction(TypedDict):
    verdict: Verdict
    note: str | None
    reporter: str
    updated_at: str


class SimilarHit(TypedDict):
    id: str
    occurred_at: str


class UpstreamChange(TypedDict):
    id: str
    change: str


class _Hit(TypedDict):
    id: str
    kind: EntryKind
    summary: str
    tags: list[str]
    author: str
    agent: str | None
    occurred_at: str
    score: float
    match: str
    scope: str
    fleet_id: str | None


class Hit(_Hit, total=False):
    """A compact search hit, without the body: ``get`` it to read it."""

    similarity: float
    feedback: FeedbackCounts
    last_confirmed_at: str
    contested: bool
    correction: Correction
    supersedes: list[str]
    state: EntryState
    similar: list[SimilarHit]
    upstream_changed: list[UpstreamChange]


class _SearchReply(TypedDict):
    count: int
    hits: list[Hit]


class SearchReply(_SearchReply, total=False):
    """``note`` explains a ``degraded`` (keyword-only) search or a ``closest: "loose"`` one."""

    recency: Recency
    degraded: str
    closest: str
    note: str


class _ListItem(TypedDict):
    id: str
    kind: EntryKind
    summary: str
    tags: list[str]
    author: str
    agent: str | None
    occurred_at: str
    created_at: str
    scope: str
    fleet_id: str | None
    state: EntryState
    superseded_by: str | None


class ListItem(_ListItem, total=False):
    """With ``include_body`` an item is a whole :class:`Entry`; ``feedback`` comes with
    ``relied_on_changed``."""

    body: str | None
    feedback: FeedbackCounts


class ListReply(TypedDict):
    count: int
    entries: list[ListItem]


class RelatedEntry(TypedDict):
    id: str
    kind: EntryKind
    summary: str
    author: str
    scope: str
    fleet_id: str | None
    occurred_at: str
    similarity: float
    tags: list[str]
    feedback: FeedbackCounts
    same_author: bool


class WriteReply(Entry):
    """The stored entry, its nearest ``related`` entries and advisory ``warnings``."""

    related: list[RelatedEntry]
    warnings: list[Warning]


class CheckReply(TypedDict):
    """``write`` with ``check=True``: what a write would report, with nothing stored."""

    checked: Literal[True]
    related: list[RelatedEntry]
    warnings: list[Warning]


class StoredFeedback(TypedDict):
    entry_id: str
    user: str
    agent: str | None
    verdict: Verdict
    note: str | None
    updated_at: str | None


class _FeedbackReply(TypedDict):
    entry_id: str
    verdict: Verdict
    note: str | None
    quality: float
    feedback: StoredFeedback


class FeedbackReply(_FeedbackReply, total=False):
    """``fix`` says how to supersede the entry when the rater may."""

    fix: str


class FeedbackResult(TypedDict):
    entry_id: str
    quality: float


class BatchFix(TypedDict):
    may_supersede: list[str]
    how: str


class _BatchFeedbackReply(TypedDict):
    verdict: Verdict
    note: str | None
    results: list[FeedbackResult]


class BatchFeedbackReply(_BatchFeedbackReply, total=False):
    fix: BatchFix


class FleetPhase(TypedDict):
    phase: str
    until: str
    hint: str


class Pin(TypedDict):
    id: str
    pinned_id: str
    kind: EntryKind
    summary: str
    author: str
    state: EntryState
    pinned_by: str
    pinned_at: str
    changed_since_pin: bool
    change_reason: str | None


class _PinnedReply(TypedDict):
    fleet_id: str | None
    pins: list[Pin]


class PinnedReply(_PinnedReply, total=False):
    fleet_phase: FleetPhase


class _PinReply(TypedDict):
    entry_id: str
    pinned: bool


class PinReply(_PinReply, total=False):
    """Pinning adds ``fleet_id``, ``pinned_by`` and ``pinned_at``; unpinning adds ``removed``."""

    fleet_id: str
    pinned_by: str
    pinned_at: str
    removed: bool


class RegisterReply(TypedDict):
    name: str
    status: str
    trust_level: int
    home_fleet_id: str | None
    already_registered: bool
    message: str


class _WhoAmI(TypedDict):
    key_kind: str
    name: str | None
    status: str | None
    trust_level: int
    trust_level_name: str
    home_fleet_id: str | None
    home_fleet_name: str | None
    can_read: list[str]
    can_write_scopes: list[str]
    inherits_from: list[str]
    key_last_used_at: str | None


class WhoAmI(_WhoAmI, total=False):
    fleet_phase: FleetPhase
    role: str
    role_hint: str
