"""In-memory Store implementation (SPEC.md §4).

The dependency-free reference ``Store``, for tests and local dev mode.

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
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from hivemind.domain.access import (
    ACTIVATABLE,
    REVOCABLE,
    SCOPE_FLEET,
    SCOPE_ORG,
    SCOPE_SELF,
    Agent,
    AgentStatus,
    Fleet,
    FleetExists,
    FleetMerged,
    InvalidAgentStatus,
    TrustLevel,
    UnknownAgent,
    UnknownFleet,
    Visibility,
    entry_is_visible,
)
from hivemind.domain.activity import (
    Activity,
    AuthorActivity,
    DayActivity,
    ReceivedFeedback,
    ReporterActivity,
)
from hivemind.domain.audit import AuditEvent, AuditFilters, AuditRecord
from hivemind.domain.entry import (
    Entry,
    EntryDraft,
    EntryFilters,
    EntryState,
    ExtractedEntity,
    ImportanceSource,
    Kind,
    SearchCount,
    UsageCount,
    embeddable_text,
    new_entry_id,
)
from hivemind.domain.feedback import (
    ORPHAN_SILENCE_DAYS,
    Correction,
    Feedback,
    FeedbackCounts,
    FeedbackLogFilters,
    FeedbackLogItem,
    FeedbackSignals,
    FlagBacklogFilters,
    FlagCount,
    FlaggedEntry,
    Verdict,
    last_confirmed,
    truncate_note,
)
from hivemind.domain.fleet_merge import (
    DroppedPin,
    DuplicatePair,
    DuplicateScan,
    FleetFacts,
    FleetMergeRecord,
    FleetMergeRefused,
    MergeOutcome,
    UnknownFleetMerge,
    UnmergeOutcome,
    check_pins_unchanged,
    choose_pins,
    plan_pin_restore,
    undo_refusal,
)
from hivemind.domain.health import (
    DISPERSION_CAP,
    DISPERSION_MIN,
    DISPERSION_TAGS,
    ECHO_SAMPLE,
    ECHO_THRESHOLD,
    Convergence,
    FleetHealth,
    HealthCounts,
    TagDispersion,
    is_entry_ref,
    owner_key,
)
from hivemind.domain.pin import Pin, PinRefusal, PinWrite, pin_refusal
from hivemind.domain.upstream import UpstreamLink
from hivemind.domain.validation import InvalidInput, canonical_uuid, is_valid_uuid, utcnow
from hivemind.ports import SupersedeConflict

# Sort key for a feedback row that has no timestamp (fixtures only).
_EPOCH = datetime.min.replace(tzinfo=UTC)


_DAYS_90 = timedelta(days=90)
_DAYS_60 = timedelta(days=60)
_DAYS_30 = timedelta(days=30)


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


def _all_pairs_cosine(
    ids: list[str], vectors: dict[str, list[float]]
) -> dict[tuple[str, str], float]:
    """Cosine of every pair among ``ids`` (in that order; sorted, a pair is
    keyed ``(a, b)`` with ``a < b``), from ``vectors``."""
    return {
        (a, b): cosine_similarity(vectors[a], vectors[b])
        for i, a in enumerate(ids)
        for b in ids[i + 1 :]
    }


def _mean_pairwise_cosine(entries: list[Entry]) -> float:
    """Mean cosine over all pairs; the caller guarantees at least two."""
    vectors = {e.id: list(e.embedding or ()) for e in entries}
    pairs = _all_pairs_cosine([e.id for e in entries], vectors).values()
    return sum(pairs) / len(pairs)


class MemoryStore:
    """Thread-safe in-memory Store (a ``dict`` under a lock)."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock: Callable[[], datetime] = clock or utcnow
        self._lock = threading.RLock()
        self._entries: dict[str, Entry] = {}
        self._feedback: dict[tuple[str, str, str], Feedback] = {}
        self._withdrawn_at: dict[str, datetime] = {}  # ADR 0098; Postgres: entries.withdrawn_at
        self._fleets: dict[str, Fleet] = {}
        self._agents: dict[str, Agent] = {}
        # The agent's key facts (ADR 0110), kept apart from the agent record
        # and merged only into ``list_agents``, as the real adapter joins them.
        self._key_last_used: dict[str, datetime] = {}
        self._key_created: dict[str, datetime] = {}
        self._audit: list[AuditRecord] = []  # append-only (ADR 0027)
        self._pins: dict[tuple[str, str], Pin] = {}  # (fleet, entry), ADR 0058
        self._searches: dict[str | None, tuple[int, int, int]] = {}  # ADR 0056, 0078
        # (from, to) -> when the link was made, insertion-ordered (ADR 0057, 0081)
        self._links: dict[tuple[str, str], datetime] = {}
        # Fleet merges, oldest first, and the entry ids each moved (ADR 0107)
        self._merges: list[FleetMergeRecord] = []
        self._merge_entries: dict[str, list[str]] = {}

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
        ``superseded`` (SPEC.md §4.1, §7)."""
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
            self._refuse_merged(draft.fleet_id)
            # Explicit supersessions (SPEC.md §4.1), atomic (ADR 0034):
            # every target must be active, or nothing is written.
            wanted = list(dict.fromkeys(draft.supersedes))
            denied = [
                t
                for t in wanted
                if (e := self._entries.get(t)) is None or e.state is not EntryState.ACTIVE
            ]
            if denied:
                raise SupersedeConflict(denied)
            self._entries[entry.id] = entry
            for target_id in wanted:
                self._entries[target_id] = _with_state(
                    self._entries[target_id],
                    EntryState.SUPERSEDED,
                    superseded_by=entry.id,
                )
            # One write's links in id order, as PgStore reads them back
            # (same ``created_at``, then ``to_id``).
            for target_id in sorted(draft.see_also):
                if target_id in self._entries and target_id != entry.id:
                    self._links[(entry.id, target_id)] = now
        return entry

    async def entry_links(self, entry_id: str, limit: int) -> tuple[list[str], list[str]]:
        with self._lock:
            outgoing = [to for (frm, to) in self._links if frm == entry_id]
            incoming = [frm for (frm, to) in reversed(self._links) if to == entry_id]
        return outgoing, incoming[:limit]

    async def entry_links_many(
        self, entry_ids: list[str], limit: int
    ) -> dict[str, tuple[list[str], list[str]]]:
        return {eid: await self.entry_links(eid, limit) for eid in entry_ids}

    async def upstream_links(
        self, entry_ids: list[str], *, visibility: Visibility | None = None
    ) -> dict[str, list[UpstreamLink]]:
        wanted = {str(uuid.UUID(eid)) for eid in entry_ids if is_valid_uuid(eid)}
        out: dict[str, list[UpstreamLink]] = {}
        with self._lock:
            targets = {to for (frm, to) in self._links if frm in wanted}
            by_target: dict[str, list[Feedback]] = {}
            for fb in self._live_feedback().values():
                if fb.entry_id in targets:
                    by_target.setdefault(fb.entry_id, []).append(fb)
            for (frm, to), linked_at in self._links.items():
                target = self._entries.get(to)
                if frm not in wanted or target is None or not self._visible(target, visibility):
                    continue
                successor = (
                    self._entries.get(target.superseded_by) if target.superseded_by else None
                )
                rows = by_target.get(to, [])
                flags = [
                    fb.updated_at or _EPOCH for fb in rows if fb.verdict is not Verdict.HELPFUL
                ]
                noted = [
                    fb.updated_at
                    for fb in rows
                    if fb.verdict is Verdict.HELPFUL
                    and fb.note
                    and fb.note.strip()
                    and fb.updated_at is not None
                ]
                out.setdefault(frm, []).append(
                    UpstreamLink(
                        from_id=frm,
                        to_id=to,
                        linked_at=linked_at,
                        state=target.state,
                        superseded_at=successor.created_at if successor else None,
                        superseded_author=successor.author if successor else None,
                        newest_flag_at=max(flags, default=None),
                        newest_noted_helpful_at=max(noted, default=None),
                    )
                )
        for links in out.values():
            links.sort(key=lambda link: (link.linked_at, link.to_id))
        return out

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
            self._withdrawn_at[entry_id] = self._clock()
            return updated

    async def record_feedback(self, feedback: Feedback) -> Feedback:
        (stored,) = await self.record_feedback_many([feedback])
        return stored

    async def record_feedback_many(self, feedbacks: list[Feedback]) -> list[Feedback]:
        with self._lock:
            now = self._clock()  # one stamp per call, like a transaction's now()
            stored = [replace(fb, updated_at=fb.updated_at or now) for fb in feedbacks]
            for fb in stored:
                self._feedback[(fb.entry_id, fb.user, fb.agent)] = fb
            return stored

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

    async def pairwise_similarity(self, entry_ids: list[str]) -> dict[tuple[str, str], float]:
        with self._lock:
            vectors = {
                eid: list(entry.embedding)
                for eid in dict.fromkeys(entry_ids)
                if (entry := self._entries.get(eid)) is not None and entry.embedding is not None
            }
        return _all_pairs_cosine(sorted(vectors), vectors)

    async def list_predecessors(self, entry_ids: list[str]) -> list[Entry]:
        wanted = set(entry_ids)
        with self._lock:
            return [
                _copy(entry) for entry in self._entries.values() if entry.superseded_by in wanted
            ]

    async def pin_entry(
        self,
        fleet_id: str,
        entry_id: str,
        pinned_by: str,
        limit: int,
        replaces: Sequence[str] = (),
        agent_limit: int | None = None,
    ) -> PinWrite | PinRefusal:
        if not is_valid_uuid(entry_id):
            return PinRefusal.FLEET  # as on Postgres: nothing a malformed id can pin
        with self._lock:
            self._refuse_merged(fleet_id)
            fleet_pins = [p for (f, _), p in self._pins.items() if f == fleet_id]
            refusal = pin_refusal(fleet_pins, entry_id, pinned_by, limit, agent_limit, replaces)
            if refusal is not None:
                return refusal
            existing = self._pins.get((fleet_id, entry_id))
            replaced = [
                old
                for old in dict.fromkeys(replaces)
                if old != entry_id and self._pins.pop((fleet_id, old), None) is not None
            ]
            # Re-inserting moves the pin to the end: insertion order breaks
            # ``list_pins`` ties, as ``entry_id`` does on Postgres.
            self._pins.pop((fleet_id, entry_id), None)
            pin = Pin(fleet_id, entry_id, pinned_by, self._clock())
            self._pins[(fleet_id, entry_id)] = pin
            return PinWrite(pin, existing is None, tuple(replaced))

    async def unpin_entry(self, fleet_id: str, entry_id: str) -> bool:
        with self._lock:
            return self._pins.pop((fleet_id, entry_id), None) is not None

    async def list_pins(self, fleet_id: str) -> list[Pin]:
        with self._lock:
            pins = [p for (f, _), p in self._pins.items() if f == fleet_id]
        # Newest first; insertion order breaks ties (the clock may not move).
        order = {id(p): i for i, p in enumerate(pins)}
        return sorted(pins, key=lambda p: (p.pinned_at, order[id(p)]), reverse=True)

    async def usage_counts(self) -> list[UsageCount]:
        with self._lock:
            groups: dict[tuple[str, Kind, ImportanceSource, str, str | None, bool], int] = {}
            for e in self._entries.values():
                key = (
                    e.scope,
                    e.kind,
                    e.importance_source,
                    e.author,
                    e.fleet_id,
                    e.state is EntryState.ACTIVE,
                )
                groups[key] = groups.get(key, 0) + 1
            return [UsageCount(*key, count=n) for key, n in groups.items()]

    async def flag_counts(self) -> list[FlagCount]:
        with self._lock:
            now = self._clock()
            flags: dict[str, int] = {}
            confirmed: dict[str, int] = {}
            for fb in self._live_feedback().values():
                if fb.verdict is Verdict.HELPFUL:
                    continue
                entry = self._entries.get(fb.entry_id)
                if entry is None:
                    continue
                at = fb.updated_at or now
                successor = (
                    self._entries.get(entry.superseded_by)
                    if entry.state is EntryState.SUPERSEDED and entry.superseded_by
                    else None
                )
                if successor is not None and successor.created_at <= at:
                    continue  # already superseded when written: predicted nothing
                flags[fb.user] = flags.get(fb.user, 0) + 1
                if successor is not None:
                    confirmed[fb.user] = confirmed.get(fb.user, 0) + 1
            return [FlagCount(u, n, confirmed.get(u, 0)) for u, n in flags.items()]

    async def activity(self, since: datetime) -> Activity:
        """The same aggregates as ``PgStore`` (ADR 0080), in plain loops."""
        with self._lock:
            live = list(self._live_feedback().values())
            entries = list(self._entries.values())
            by_group: dict[tuple[str, str | None], list[Entry]] = {}
            for e in entries:
                by_group.setdefault((e.author, e.fleet_id), []).append(e)
            authors = tuple(
                AuthorActivity(
                    author=author,
                    fleet_id=fleet_id,
                    entries=len(rows),
                    active=sum(e.state is EntryState.ACTIVE for e in rows),
                    recent=sum(e.created_at >= since for e in rows),
                    last_write_at=max(e.created_at for e in rows),
                )
                for (author, fleet_id), rows in by_group.items()
            )
            by_reporter: dict[str, list[Feedback]] = {}
            for fb in live:
                by_reporter.setdefault(fb.user, []).append(fb)
            now = self._clock()
            reporters = tuple(
                ReporterActivity(
                    reporter=user,
                    verdicts=len(rows),
                    recent=sum((fb.updated_at or now) >= since for fb in rows),
                    helpful=sum(fb.verdict is Verdict.HELPFUL for fb in rows),
                    stale=sum(fb.verdict is Verdict.STALE for fb in rows),
                    wrong=sum(fb.verdict is Verdict.WRONG for fb in rows),
                    last_at=max(fb.updated_at or now for fb in rows),
                )
                for user, rows in by_reporter.items()
            )
            author_of = {e.id: e.author for e in entries}
            got: dict[str, Counter[Verdict]] = {}
            for fb in live:
                if fb.entry_id in author_of:
                    got.setdefault(author_of[fb.entry_id], Counter())[fb.verdict] += 1
            received = tuple(
                ReceivedFeedback(
                    author=author,
                    helpful=c[Verdict.HELPFUL],
                    stale=c[Verdict.STALE],
                    wrong=c[Verdict.WRONG],
                )
                for author, c in got.items()
            )
            per_day: dict[date, Counter[str]] = {}
            for e in entries:
                if e.created_at >= since:
                    per_day.setdefault(e.created_at.astimezone(UTC).date(), Counter())[
                        "writes"
                    ] += 1
            for fb in live:
                at = fb.updated_at or now
                if at >= since:
                    per_day.setdefault(at.astimezone(UTC).date(), Counter())[fb.verdict.value] += 1
            days = tuple(
                DayActivity(
                    day=day,
                    writes=c["writes"],
                    helpful=c["helpful"],
                    stale=c["stale"],
                    wrong=c["wrong"],
                )
                for day, c in sorted(per_day.items())
            )
            return Activity(authors=authors, reporters=reporters, received=received, days=days)

    async def fleet_health(self, now: datetime) -> FleetHealth:
        """The same aggregates as ``PgStore`` (ADR 0075), in plain loops."""
        with self._lock:
            live = list(self._live_feedback().values())
            revoked = {n for n, a in self._agents.items() if a.status is AgentStatus.REVOKED}
            flagged = [fb for fb in live if fb.verdict is not Verdict.HELPFUL]
            first_flag: dict[str, datetime] = {}
            for fb in flagged:
                at = fb.updated_at or now
                if fb.entry_id not in first_flag or at < first_flag[fb.entry_id]:
                    first_flag[fb.entry_id] = at
            fresh = {fb.entry_id for fb in live if now - _DAYS_90 <= (fb.updated_at or now) <= now}
            outgoing: Counter[str] = Counter(frm for frm, _ in self._links)
            last_write: dict[str, datetime] = {}
            for e in self._entries.values():
                if e.author not in last_write or e.created_at > last_write[e.author]:
                    last_write[e.author] = e.created_at
            rows: dict[str | None, dict[str, int]] = {}
            for e in self._entries.values():
                if e.state is not EntryState.ACTIVE:
                    continue
                c = rows.setdefault(e.fleet_id, Counter())
                if e.scope in (SCOPE_ORG, SCOPE_FLEET) and e.kind in (Kind.FACT, Kind.INSIGHT):
                    c["sourceable"] += 1
                    c["sourced"] += bool(e.sources)
                if e.kind is Kind.INSIGHT:
                    c["insights"] += 1
                    c["synthesizing"] += outgoing[e.id] >= 2
                flag_at = first_flag.get(e.id)
                if flag_at is not None:
                    c["backlog"] += flag_at < now - timedelta(days=14)
                    c["orphaned"] += e.author in revoked or last_write[e.author] < now - timedelta(
                        days=ORPHAN_SILENCE_DAYS
                    )
                c["floating"] += e.occurred_at < now - _DAYS_90 and e.id not in fresh
                c["anomaly"] += "anomaly" in e.tags
                c["open_question"] += "open-question" in e.tags
                c["dead_end"] += "dead-end" in e.tags
            since = now - _DAYS_90
            recent = [e for e in self._entries.values() if since <= e.created_at <= now]
            authors = Counter(e.author for e in recent)
            verdicts = Counter(fb.user for fb in live if since <= (fb.updated_at or now) <= now)
            in_window = [fb for fb in live if since <= (fb.updated_at or now) <= now]
            owner_of: dict[str, str | None] = {
                n: owner_key(a.owner_alias) for n, a in self._agents.items()
            }
            owners = Counter(
                f"o:{key}" if (key := owner_of.get(fb.user)) else f"r:{fb.user}" for fb in in_window
            )
            siblings = 0
            for fb in in_window:
                entry = self._entries.get(fb.entry_id)
                key = owner_of.get(fb.user)
                siblings += (
                    entry is not None
                    and key is not None
                    and entry.author != fb.user
                    and owner_of.get(entry.author) == key
                )
            latencies: list[float] = []
            supersessions = 0
            promoted: set[str] = set()
            predecessors: dict[str, list[Entry]] = {}
            for old in self._entries.values():
                if old.superseded_by is not None:
                    predecessors.setdefault(old.superseded_by, []).append(old)
            for succ in recent:
                for old in predecessors.get(succ.id, ()):
                    supersessions += 1
                    if old.scope == SCOPE_SELF and succ.scope == SCOPE_FLEET:
                        promoted.add(succ.id)
                    times = [
                        fb.updated_at or now
                        for fb in flagged
                        if fb.entry_id == old.id and (fb.updated_at or now) < succ.created_at
                    ]
                    if old.state is EntryState.SUPERSEDED and times:
                        latencies.append((succ.created_at - min(times)).total_seconds())
            helpful = [fb for fb in live if fb.verdict is Verdict.HELPFUL]
            since30 = now - _DAYS_30
            written30 = [e for e in self._entries.values() if since30 <= e.created_at <= now]
            synth = [
                e
                for e in written30
                if e.scope in (SCOPE_ORG, SCOPE_FLEET) and e.kind in (Kind.INSIGHT, Kind.DECISION)
            ]
            labeled30 = [e for e in written30 if e.agent.strip()]
            labels = Counter(e.agent for e in labeled30)
            label_verdicts = Counter(
                fb.agent
                for fb in live
                if since30 <= (fb.updated_at or now) <= now and fb.agent.strip()
            )
            by_entry: dict[str, set[Verdict]] = {}
            for fb in live:
                by_entry.setdefault(fb.entry_id, set()).add(fb.verdict)
            active = [e for e in self._entries.values() if e.state is EntryState.ACTIVE]

            def ages(match: Callable[[Entry], bool]) -> tuple[float, ...]:
                return tuple((now - e.created_at).total_seconds() for e in active if match(e))

            def contested(e: Entry) -> bool:
                seen = by_entry.get(e.id, set())
                return Verdict.HELPFUL in seen and len(seen) > 1

            return FleetHealth(
                by_fleet=tuple(
                    HealthCounts(
                        fleet_id=fleet_id,
                        sourceable=c["sourceable"],
                        sourced=c["sourced"],
                        insights=c["insights"],
                        synthesizing=c["synthesizing"],
                        flagged_backlog_14d=c["backlog"],
                        orphaned_flagged=c["orphaned"],
                        floating_gap=c["floating"],
                        gap_anomaly=c["anomaly"],
                        gap_open_question=c["open_question"],
                        gap_dead_end=c["dead_end"],
                    )
                    for fleet_id, c in rows.items()
                ),
                helpful=len(helpful),
                helpful_with_note=sum(1 for fb in helpful if fb.note and fb.note.strip()),
                correction_latencies=tuple(latencies),
                writes_90d=len(recent),
                top3_author_writes_90d=sum(n for _, n in authors.most_common(3)),
                authors_90d=len(authors),
                verdicts_90d=sum(verdicts.values()),
                top3_reporter_verdicts_90d=sum(n for _, n in verdicts.most_common(3)),
                reporters_90d=len(verdicts),
                sibling_verdicts_90d=siblings,
                top3_owner_verdicts_90d=sum(n for _, n in owners.most_common(3)),
                owners_90d=len(owners),
                supersessions_90d=supersessions,
                self_to_fleet_promotions_90d=len(promoted),
                alternative_ages=ages(lambda e: "alternative" in e.tags),
                conflict_ages=ages(lambda e: e.summary.startswith("Conflict:")),
                contested_ages=ages(contested),
                synthesis_writes_30d=len(synth),
                grounded_30d=sum(
                    1 for e in synth if any(not is_entry_ref(src.ref) for src in e.sources)
                ),
                writes_30d=len(labeled30),
                top3_label_writes_30d=sum(n for _, n in labels.most_common(3)),
                write_labels_30d=len(labels),
                verdicts_30d=sum(label_verdicts.values()),
                top3_label_verdicts_30d=sum(n for _, n in label_verdicts.most_common(3)),
                verdict_labels_30d=len(label_verdicts),
            )

    async def fleet_convergence(self, now: datetime) -> Convergence:
        """The same figures as ``PgStore`` (ADR 0075), with exact cosines."""
        with self._lock:
            window = [
                e
                for e in self._entries.values()
                if e.embedding is not None and now - _DAYS_60 <= e.created_at <= now
            ]
            newest = sorted(window, key=lambda e: (e.created_at, e.id), reverse=True)
            sampled = [e for e in newest if e.created_at >= now - _DAYS_30][:ECHO_SAMPLE]
            hits = 0
            for w in sampled:
                assert w.embedding is not None
                best = max(
                    (
                        cosine_similarity(list(w.embedding), list(e.embedding))
                        for e in self._entries.values()
                        if e.state is EntryState.ACTIVE
                        and e.embedding is not None
                        and (e.created_at, e.id) < (w.created_at, w.id)
                    ),
                    default=None,
                )
                hits += best is not None and best >= ECHO_THRESHOLD
            by_tag: dict[str, list[Entry]] = {}
            for e in newest:
                if e.scope not in (SCOPE_ORG, SCOPE_FLEET):
                    continue  # tags are read from org and fleet entries only
                for tag in set(e.tags):
                    by_tag.setdefault(tag, []).append(e)
            busy: list[tuple[int, str, list[Entry], list[Entry]]] = []
            for tag, entries in by_tag.items():
                recent = [e for e in entries if e.created_at >= now - _DAYS_30][:DISPERSION_CAP]
                before = [e for e in entries if e.created_at < now - _DAYS_30][:DISPERSION_CAP]
                n_recent = sum(1 for e in entries if e.created_at >= now - _DAYS_30)
                if min(n_recent, len(entries) - n_recent) >= DISPERSION_MIN:
                    busy.append((-len(entries), tag, recent, before))
            busy.sort(key=lambda b: b[:2])
            return Convergence(
                echo_sampled=len(sampled),
                echo_hits=hits,
                dispersion=tuple(
                    TagDispersion(
                        tag=tag,
                        recent_entries=len(recent),
                        before_entries=len(before),
                        recent_cosine=_mean_pairwise_cosine(recent),
                        before_cosine=_mean_pairwise_cosine(before),
                    )
                    for _, tag, recent, before in busy[:DISPERSION_TAGS]
                ),
            )

    async def record_search(
        self, fleet_id: str | None, *, empty: bool, explore: bool = False
    ) -> None:
        with self._lock:
            searches, misses, explored = self._searches.get(fleet_id, (0, 0, 0))
            self._searches[fleet_id] = (searches + 1, misses + int(empty), explored + int(explore))

    async def search_counts(self) -> list[SearchCount]:
        with self._lock:
            return [SearchCount(f, s, e, x) for f, (s, e, x) in self._searches.items()]

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
                if self._matches(entry, filters) and self._visible(entry, visibility)
            ]
            # Most-recent-first (SPEC.md §11.4), tie-broken by id DESC to
            # match PgStore's `ORDER BY created_at DESC, id DESC` exactly.
            matches.sort(key=lambda e: (e.created_at, e.id), reverse=True)
            return matches[offset : offset + limit]

    async def count_entries(
        self, filters: EntryFilters, *, visibility: Visibility | None = None
    ) -> int:
        """Count entries matching ``filters`` (ROADMAP §3.3)."""
        with self._lock:
            return sum(
                1
                for entry in self._entries.values()
                if self._matches(entry, filters) and self._visible(entry, visibility)
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
                if not self._matches(entry, filters) or not self._visible(entry, visibility):
                    continue
                # Same text the vector stream sees, at the default budget
                # (ADR 0021; this store has no Settings).
                haystack = _tokens(embeddable_text(entry.summary, entry.body)) | {
                    tag.lower() for tag in entry.tags
                }
                overlap = len(query_tokens & haystack)
                if overlap == 0:
                    continue
                rows.append((float(overlap), entry.created_at, entry.id))
            # Ties newest first, as PgStore's ``created_at DESC, id DESC``.
            rows.sort(key=lambda r: (r[1], r[2]), reverse=True)
            rows.sort(key=lambda r: -r[0])
            return [eid for _, _, eid in rows[:limit]]

    async def search_vector(
        self,
        vector: list[float],
        filters: EntryFilters,
        limit: int,
        *,
        visibility: Visibility | None = None,
        min_similarity: float = 0.0,
    ) -> list[str]:
        with self._lock:
            rows: list[tuple[float, datetime, str]] = []
            for entry in self._entries.values():
                if not self._matches(entry, filters) or not self._visible(entry, visibility):
                    continue
                if entry.embedding is None:
                    continue
                sim = cosine_similarity(vector, list(entry.embedding))
                if sim <= min_similarity:  # ADRs 0061, 0062
                    continue
                rows.append((sim, entry.created_at, entry.id))
            rows.sort(key=lambda r: (-r[0], r[1], r[2]))
            return [eid for _, _, eid in rows[:limit]]

    def _matches(self, entry: Entry, filters: EntryFilters) -> bool:
        """``filters.matches`` plus the one filter it cannot evaluate:
        ``flagged`` (ADR 0054) and ``relied_on_changed`` (ADR 0098), which
        read this store's feedback rows.
        Callers hold the lock."""
        if not filters.matches(_copy(entry)):
            return False
        if filters.flagged:
            _, stale, wrong = _count_for(self._live_feedback(), entry.id)
            if stale + wrong == 0:
                return False
        return not filters.relied_on_changed or self._relied_on_changed(entry, filters.reporter)

    def _relied_on_changed(self, entry: Entry, reporter: tuple[str, str] | None) -> bool:
        """ADR 0098, as PgStore's clause: the reporter's helpful row on
        ``entry``, and since its ``updated_at`` the entry was withdrawn, or
        superseded by another user's successor, or another user's live
        stale/wrong report is newer. A withdrawal with no recorded time
        counts, as in PgStore. Callers hold the lock."""
        if reporter is None:
            return False
        own = self._feedback.get((entry.id, *reporter))
        if own is None or own.verdict is not Verdict.HELPFUL or own.updated_at is None:
            return False
        since = own.updated_at
        if entry.state is EntryState.WITHDRAWN:
            at = self._withdrawn_at.get(entry.id)
            if at is None or at > since:
                return True
        if entry.state is EntryState.SUPERSEDED:
            successor = self._entries.get(entry.superseded_by or "")
            if (
                successor is not None
                and successor.author != reporter[0]
                and successor.created_at > since
            ):
                return True
        return any(
            fb.entry_id == entry.id
            and fb.verdict is not Verdict.HELPFUL
            and fb.user != reporter[0]
            and fb.updated_at is not None
            and fb.updated_at > since
            for fb in self._live_feedback().values()
        )

    async def similar_entries(
        self,
        embedding: list[float],
        limit: int,
        *,
        exclude_id: str | None = None,
        visibility: Visibility | None = None,
    ) -> list[tuple[str, float]]:
        active = EntryFilters()
        with self._lock:
            rows: list[tuple[float, datetime, str]] = []
            for entry in self._entries.values():
                if (exclude_id is not None and entry.id == exclude_id) or entry.embedding is None:
                    continue
                if not active.matches(entry) or not self._visible(entry, visibility):
                    continue
                sim = cosine_similarity(embedding, list(entry.embedding))
                rows.append((sim, entry.created_at, entry.id))
            rows.sort(key=lambda r: (-r[0], r[1], r[2]))
            return [(eid, sim) for sim, _, eid in rows[:limit]]

    def _visible(self, entry: Entry, visibility: Visibility | None) -> bool:
        """Whether ``entry`` passes the optional visibility filter (ADR 0011)."""
        return visibility is None or entry_is_visible(entry, visibility)

    def _live_feedback(self) -> dict[tuple[str, str, str], Feedback]:
        """The feedback rows whose reporter is not a revoked agent (ADR 0067).
        Callers hold the lock."""
        revoked = {n for n, a in self._agents.items() if a.status is AgentStatus.REVOKED}
        if not revoked:
            return self._feedback
        return {k: fb for k, fb in self._feedback.items() if fb.user not in revoked}

    async def feedback_counts(self, entry_id: str) -> FeedbackCounts:
        with self._lock:
            return _count_for(self._live_feedback(), entry_id)

    async def quality_counts(self, entry_ids: list[str]) -> dict[str, FeedbackCounts]:
        with self._lock:
            # Every well-formed id, known or not, as on PgStore.
            live = self._live_feedback()
            return {eid: _count_for(live, eid) for eid in entry_ids if is_valid_uuid(eid)}

    async def feedback_signals(
        self,
        entry_ids: list[str],
        *,
        count_supersedes: bool = False,
        visibility: Visibility | None = None,
    ) -> dict[str, FeedbackSignals]:
        with self._lock:
            live = self._live_feedback()
            out = {eid: _signals_for(live, eid) for eid in entry_ids if is_valid_uuid(eid)}
            if count_supersedes:
                counts: dict[str, int] = {}
                for entry in self._entries.values():
                    if entry.superseded_by in out and self._visible(entry, visibility):
                        counts[entry.superseded_by] = counts.get(entry.superseded_by, 0) + 1
                out = {eid: replace(s, supersedes=counts.get(eid, 0)) for eid, s in out.items()}
            return out

    async def list_feedback(self, entry_id: str, limit: int) -> list[Feedback]:
        with self._lock:
            rows = [fb for fb in self._live_feedback().values() if fb.entry_id == entry_id]
        # Informative rows (stale, wrong, or with a note) before bare helpful
        # ones (ADR 0064), each group newest first; ties (same timestamp)
        # break on the reporter, as on Postgres, so the order is deterministic.
        rows.sort(key=lambda fb: (fb.user, fb.agent))
        rows.sort(key=lambda fb: fb.updated_at or _EPOCH, reverse=True)
        rows.sort(key=lambda fb: fb.verdict is Verdict.HELPFUL and not fb.note)
        return rows[:limit]

    async def list_feedback_many(
        self, entry_ids: list[str], limit: int
    ) -> dict[str, list[Feedback]]:
        return {eid: await self.list_feedback(eid, limit) for eid in entry_ids}

    async def list_feedback_log(
        self,
        filters: FeedbackLogFilters,
        limit: int,
        offset: int = 0,
        *,
        visibility: Visibility | None = None,
    ) -> list[FeedbackLogItem]:
        def well_formed(value: str | None) -> str | None:
            # ``is_valid_uuid`` first: it refuses the braced and URN spellings
            # ``canonical_uuid`` would accept, as Postgres does here.
            return canonical_uuid(value) if value is not None and is_valid_uuid(value) else None

        entry_id = well_formed(filters.entry_id)
        fleet_id = well_formed(filters.fleet_id)
        if (filters.entry_id is not None and entry_id is None) or (
            filters.fleet_id is not None and fleet_id is None
        ):
            return []
        with self._lock:
            pairs = [
                (fb, entry)
                for fb in self._live_feedback().values()
                if (entry := self._entries.get(fb.entry_id)) is not None
                and (entry_id is None or entry.id == entry_id)
                and (fleet_id is None or entry.fleet_id == fleet_id)
                and (filters.reporter is None or fb.user == filters.reporter)
                and (filters.verdict is None or fb.verdict is filters.verdict)
                and (filters.author is None or entry.author == filters.author)
                and self._visible(entry, visibility)
            ]
        # Newest first, then entry id, reporter and agent, as on Postgres.
        pairs.sort(key=lambda p: (p[0].entry_id, p[0].user, p[0].agent))
        pairs.sort(key=lambda p: p[0].updated_at or _EPOCH, reverse=True)
        return [
            FeedbackLogItem(
                feedback=fb,
                entry_kind=entry.kind,
                entry_summary=entry.summary,
                entry_author=entry.author,
                entry_scope=entry.scope,
                entry_fleet_id=entry.fleet_id,
                entry_state=entry.state,
            )
            for fb, entry in pairs[offset : offset + limit]
        ]

    async def list_flag_backlog(
        self,
        filters: FlagBacklogFilters,
        now: datetime,
        limit: int,
        offset: int = 0,
        *,
        visibility: Visibility | None = None,
    ) -> list[FlaggedEntry]:
        fleet_id = filters.fleet_id
        if fleet_id is not None:
            if not is_valid_uuid(fleet_id):  # braced or URN spellings too, as on Postgres
                return []
            fleet_id = canonical_uuid(fleet_id)
        with self._lock:
            revoked = {n for n, a in self._agents.items() if a.status is AgentStatus.REVOKED}
            by_entry: dict[str, list[Feedback]] = {}
            for fb in self._live_feedback().values():
                by_entry.setdefault(fb.entry_id, []).append(fb)
            last_write: dict[str, datetime] = {}
            for e in self._entries.values():
                if e.author not in last_write or e.created_at > last_write[e.author]:
                    last_write[e.author] = e.created_at
            silent_since = now - timedelta(days=ORPHAN_SILENCE_DAYS)
            items: list[FlaggedEntry] = []
            for entry_id, rows in by_entry.items():
                entry = self._entries.get(entry_id)
                flags = [fb.updated_at or now for fb in rows if fb.verdict is not Verdict.HELPFUL]
                if (
                    entry is None
                    or not flags
                    or entry.state is not EntryState.ACTIVE
                    or (fleet_id is not None and entry.fleet_id != fleet_id)
                    or not self._visible(entry, visibility)
                ):
                    continue
                verdicts = Counter(fb.verdict for fb in rows)
                orphaned = entry.author in revoked or last_write[entry.author] < silent_since
                first = min(flags)
                if (filters.flagged_before is not None and first >= filters.flagged_before) or (
                    filters.orphaned and not orphaned
                ):
                    continue
                items.append(
                    FlaggedEntry(
                        entry_id=entry.id,
                        kind=entry.kind,
                        summary=entry.summary,
                        author=entry.author,
                        scope=entry.scope,
                        fleet_id=entry.fleet_id,
                        first_flag_at=first,
                        last_flag_at=max(flags),
                        helpful=verdicts[Verdict.HELPFUL],
                        stale=verdicts[Verdict.STALE],
                        wrong=verdicts[Verdict.WRONG],
                        orphaned=orphaned,
                    )
                )
        # Oldest first flag first, then entry id, as on Postgres.
        items.sort(key=lambda i: (i.first_flag_at, i.entry_id))
        return items[offset : offset + limit]

    async def health_check(self) -> bool:
        """In-memory pool: always healthy (ADR 0019)."""
        return True

    # -- fleets (ADR 0011) --------------------------------------------------

    async def create_fleet(self, name: str) -> Fleet:
        """Create a named fleet (ADR 0011). ``FleetExists`` if the name exists."""
        with self._lock:
            for existing in self._fleets.values():
                if existing.name == name:
                    raise FleetExists(f"fleet already exists: {name!r}")
            fleet = Fleet(id=str(uuid.uuid4()), name=name, created_at=self._clock())
            self._fleets[fleet.id] = fleet
            return fleet

    async def list_fleets(self) -> list[Fleet]:
        with self._lock:
            return list(self._fleets.values())

    async def get_fleet(self, fleet_id: str) -> Fleet | None:
        with self._lock:
            return self._fleets.get(fleet_id)

    async def rename_fleet(self, fleet_id: str, name: str) -> tuple[Fleet, Fleet]:
        """Rename a fleet (ADR 0109); see ``Store.rename_fleet``."""
        with self._lock:
            before = self._fleets.get(fleet_id)
            if before is None:
                raise UnknownFleet(f"unknown fleet: {fleet_id}")
            if before.name == name:
                return before, before
            if any(f.name == name for f in self._fleets.values()):
                raise FleetExists(f"fleet already exists: {name!r}")
            after = replace(before, name=name)
            self._fleets[fleet_id] = after
            return before, after

    def _refuse_merged(self, fleet_id: str | None) -> None:
        """``FleetMerged`` when ``fleet_id`` names a merged fleet (ADR 0107).
        Caller holds the lock. An id naming no fleet passes, as on Postgres."""
        fleet = self._fleets.get(fleet_id) if fleet_id is not None else None
        if fleet is not None and fleet.merged_into is not None:
            target = self._fleets.get(fleet.merged_into)
            raise FleetMerged(fleet.id, fleet.merged_into, target.name if target else None)

    # -- fleet merge (ADR 0107) ---------------------------------------------

    async def fleet_facts(self, fleet_id: str) -> FleetFacts:
        with self._lock:
            agents = sum(1 for a in self._agents.values() if a.home_fleet_id == fleet_id)
            states = Counter(
                e.state.value for e in self._entries.values() if e.fleet_id == fleet_id
            )
        return FleetFacts(agents=agents, entries=dict(states))

    async def duplicate_pairs(
        self, fleet_id: str, other_id: str, threshold: float, cap: int
    ) -> DuplicateScan:
        with self._lock:
            active = [
                e
                for e in self._entries.values()
                if e.state is EntryState.ACTIVE and e.embedding is not None
            ]
            mine = sorted(
                (e for e in active if e.fleet_id == fleet_id),
                key=lambda e: (-e.created_at.timestamp(), e.id),
            )
            theirs = [e for e in active if e.fleet_id == other_id]
        looked = mine[:cap]
        pairs: list[DuplicatePair] = []
        for e in looked:
            best: tuple[float, datetime, str] | None = None
            for o in theirs:
                sim = cosine_similarity(list(e.embedding or ()), list(o.embedding or ()))
                key = (-sim, o.created_at, o.id)
                if best is None or key < (-best[0], best[1], best[2]):
                    best = (sim, o.created_at, o.id)
            if best is not None and best[0] >= threshold:
                pairs.append(DuplicatePair(e.id, best[2], best[0]))
        pairs.sort(key=lambda p: (-p.similarity, p.entry_id))
        return DuplicateScan(tuple(pairs), len(looked), len(mine) > cap)

    async def merge_fleets(
        self,
        source_id: str,
        *,
        target_fleet_id: str | None = None,
        new_fleet_name: str | None = None,
        keep_pin_ids: Sequence[str] | None = None,
        expected_pin_ids: Sequence[str] | None = None,
        merged_by: str,
    ) -> MergeOutcome:
        if (target_fleet_id is None) == (new_fleet_name is None):
            raise InvalidInput("name exactly one of target_fleet_id and new_fleet_name")
        for given in (source_id, target_fleet_id):
            if given is not None and not is_valid_uuid(given):
                raise UnknownFleet(f"unknown fleet: {given}")
        source_id = str(uuid.UUID(source_id))
        if target_fleet_id is not None:
            target_fleet_id = str(uuid.UUID(target_fleet_id))
        with self._lock:
            source = self._fleets.get(source_id)
            if source is None:
                raise UnknownFleet(f"unknown fleet: {source_id}")
            target = None
            if target_fleet_id is not None:
                target = self._fleets.get(target_fleet_id)
                if target is None:
                    raise UnknownFleet(f"unknown fleet: {target_fleet_id}")
                if target.id == source.id:
                    raise FleetMergeRefused("a fleet cannot be merged into itself")
            elif any(f.name == new_fleet_name for f in self._fleets.values()):
                raise FleetExists(f"fleet already exists: {new_fleet_name!r}")
            for side in (source, target):
                if side is not None and side.merged_into is not None:
                    self._refuse_merged(side.id)
            pins = [
                p
                for (f, _), p in self._pins.items()
                if f == source.id or (target is not None and f == target.id)
            ]
            check_pins_unchanged(pins, expected_pin_ids)
            kept, dropped = choose_pins(pins, keep_pin_ids)
            # Nothing below can fail: mutate.
            created = target is None
            if target is None:
                assert new_fleet_name is not None
                target = Fleet(id=str(uuid.uuid4()), name=new_fleet_name, created_at=self._clock())
                self._fleets[target.id] = target
            now = self._clock()
            moved = [e.id for e in self._entries.values() if e.fleet_id == source.id]
            for entry_id in moved:
                self._entries[entry_id] = replace(self._entries[entry_id], fleet_id=target.id)
            agents = sorted(a.name for a in self._agents.values() if a.home_fleet_id == source.id)
            for name in agents:
                self._agents[name] = replace(self._agents[name], home_fleet_id=target.id)
            drops = tuple(
                DroppedPin(p.fleet_id, p.entry_id, p.pinned_by, p.pinned_at) for p in dropped
            )
            for p in pins:
                self._pins.pop((p.fleet_id, p.entry_id), None)
            rekeyed = tuple(replace(p, fleet_id=target.id) for p in kept)
            for p in sorted(rekeyed, key=lambda p: p.pinned_at):
                self._pins[(p.fleet_id, p.entry_id)] = p
            if source.id in self._searches:
                mine = self._searches.pop(source.id)
                theirs = self._searches.get(target.id, (0, 0, 0))
                self._searches[target.id] = tuple(a + b for a, b in zip(mine, theirs, strict=True))  # type: ignore[assignment]
            self._fleets[source.id] = replace(source, merged_into=target.id, merged_at=now)
            record = FleetMergeRecord(
                id=str(uuid.uuid4()),
                source_fleet_id=source.id,
                target_fleet_id=target.id,
                merged_by=merged_by,
                merged_at=now,
                moved_agents=tuple(agents),
                dropped_pins=drops,
            )
            self._merges.append(record)
            self._merge_entries[record.id] = moved
            return MergeOutcome(record, target, created, len(moved), len(agents), rekeyed, drops)

    async def unmerge_fleet(self, merge_id: str) -> UnmergeOutcome:
        if not is_valid_uuid(merge_id):
            raise UnknownFleetMerge(f"unknown fleet merge: {merge_id}")
        merge_id = str(uuid.UUID(merge_id))
        with self._lock:
            record = next((m for m in self._merges if m.id == merge_id), None)
            if record is None:
                raise UnknownFleetMerge(f"unknown fleet merge: {merge_id}")
            source = self._fleets[record.source_fleet_id]
            target = self._fleets[record.target_fleet_id]
            reason = undo_refusal(record, list(reversed(self._merges)), target)
            if reason is not None:
                raise FleetMergeRefused(reason)
            moved = set(self._merge_entries[record.id])
            back = [
                eid
                for eid in self._merge_entries[record.id]
                if self._entries[eid].fleet_id == target.id
            ]
            for eid in back:
                self._entries[eid] = replace(self._entries[eid], fleet_id=source.id)
            agents = [
                n
                for n in record.moved_agents
                if n in self._agents and self._agents[n].home_fleet_id == target.id
            ]
            for name in agents:
                self._agents[name] = replace(self._agents[name], home_fleet_id=source.id)
            # Pins follow their entries back.
            returned = [key for key in list(self._pins) if key[0] == target.id and key[1] in moved]
            for key in returned:
                pin = self._pins.pop(key)
                self._pins[(source.id, key[1])] = replace(pin, fleet_id=source.id)
            restored: list[DroppedPin] = []
            skipped: list[DroppedPin] = []
            for fleet in (source, target):
                mine = [d for d in record.dropped_pins if d.fleet_id == fleet.id]
                fits = [
                    d
                    for d in mine
                    if (e := self._entries.get(d.entry_id)) is not None
                    and e.fleet_id == fleet.id
                    and (fleet.id, d.entry_id) not in self._pins
                ]
                skipped.extend(d for d in mine if d not in fits)
                held = [p.pinned_by for (f, _), p in self._pins.items() if f == fleet.id]
                put, over = plan_pin_restore(fits, held)
                skipped.extend(over)
                for d in put:
                    self._pins[(fleet.id, d.entry_id)] = Pin(
                        fleet.id, d.entry_id, d.pinned_by, d.pinned_at
                    )
                restored.extend(put)
            now = self._clock()
            self._fleets[source.id] = replace(source, merged_into=None, merged_at=None)
            done = replace(record, undone_at=now)
            self._merges[self._merges.index(record)] = done
            return UnmergeOutcome(
                done, len(back), tuple(agents), len(returned), tuple(restored), tuple(skipped)
            )

    async def list_fleet_merges(self, limit: int) -> list[FleetMergeRecord]:
        with self._lock:
            return list(reversed(self._merges))[:limit]

    async def get_fleet_merge(self, merge_id: str) -> FleetMergeRecord | None:
        with self._lock:
            return next((m for m in self._merges if m.id == merge_id), None)

    # -- agent registration / activation (ADR 0012) --------------------------

    async def register_agent(self, name: str, owner_alias: str | None = None) -> Agent:
        """Register an agent idempotently (see ``Store.register_agent``)."""
        with self._lock:
            existing = self._agents.get(name)
            if existing is not None:
                return existing
            # Insertion order is registration order: the oldest holder wins.
            for agent in self._agents.values():
                if agent.name.lower() == name.lower():
                    return agent
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
            return [
                replace(
                    a,
                    last_used_at=self._key_last_used.get(a.name),
                    key_created_at=self._key_created.get(a.name),
                )
                for a in sorted(self._agents.values(), key=lambda a: a.name)
            ]

    def record_key_issued(self, name: str, at: datetime) -> None:
        """A new key for the agent, made at ``at``, unused so far (ADR 0110).
        The in-memory authenticator fake calls this; the real adapter's key
        row carries it. Not on the ``Store`` port: nothing else writes it."""
        with self._lock:
            self._key_created[name] = at
            self._key_last_used.pop(name, None)

    def record_key_use(self, name: str, at: datetime) -> None:
        """Stamp the agent's key as used at ``at`` (ADR 0110); the real
        adapter writes ``credentials.last_used_at``. Not on the port."""
        with self._lock:
            self._key_last_used[name] = at

    def _drop_key_facts(self, name: str) -> None:
        self._key_last_used.pop(name, None)
        self._key_created.pop(name, None)

    async def activate_agent(
        self, name: str, *, trust_level: TrustLevel, home_fleet_id: str
    ) -> Agent:
        """Activate a pending or revoked agent: set trust level + home
        fleet, flip to ``active`` (ADRs 0012, 0028). ``UnknownAgent`` if
        unknown; ``InvalidAgentStatus`` if already ``active``."""
        with self._lock:
            self._refuse_merged(home_fleet_id)
            agent = self._agents.get(name)
            if agent is None:
                raise UnknownAgent(f"unknown agent: {name}")
            if agent.status not in ACTIVATABLE:
                raise InvalidAgentStatus(name, agent.status, "activate")
            activated = replace(
                agent,
                status=AgentStatus.ACTIVE,
                trust_level=trust_level,
                home_fleet_id=home_fleet_id,
                activated_at=self._clock(),
            )
            self._drop_key_facts(name)  # the next key starts unused
            self._agents[name] = activated
            return activated

    async def revoke_agent(self, name: str) -> Agent:
        """Flip a pending or active agent to ``revoked`` (ADR 0028).
        ``UnknownAgent`` if unknown; ``InvalidAgentStatus`` if already revoked."""
        with self._lock:
            agent = self._agents.get(name)
            if agent is None:
                raise UnknownAgent(f"unknown agent: {name}")
            if agent.status not in REVOCABLE:
                raise InvalidAgentStatus(name, agent.status, "revoke")
            revoked = replace(agent, status=AgentStatus.REVOKED)
            self._drop_key_facts(name)  # the key is deleted
            self._agents[name] = revoked
            return revoked

    async def set_agent_trust_level(self, name: str, level: TrustLevel) -> Agent:
        """Promote/demote an agent's trust level (ADR 0011). ``UnknownAgent`` if
        unknown. Demotion to level 0 is *dormant* (still active, no access).
        """
        with self._lock:
            agent = self._agents.get(name)
            if agent is None:
                raise UnknownAgent(f"unknown agent: {name}")
            updated = replace(agent, trust_level=level)
            self._agents[name] = updated
            return updated

    async def set_agent_home_fleet(self, name: str, fleet_id: str) -> Agent:
        """Re-parent an agent to a new home fleet (ADR 0011). The agent's
        earlier ``fleet``-scoped entries stay in the fleet they were written
        into (never re-parented). ``UnknownAgent`` if unknown.
        """
        with self._lock:
            self._refuse_merged(fleet_id)
            agent = self._agents.get(name)
            if agent is None:
                raise UnknownAgent(f"unknown agent: {name}")
            updated = replace(agent, home_fleet_id=fleet_id)
            self._agents[name] = updated
            return updated

    # -- audit log (ADR 0027) -------------------------------------------------

    async def record_audit(self, event: AuditEvent) -> AuditRecord:
        """Append one audit row (insert-only; ADR 0027)."""
        with self._lock:
            record = AuditRecord(
                id=str(uuid.uuid4()),
                occurred_at=self._clock(),
                actor_kind=event.actor_kind,
                actor=event.actor,
                action=event.action,
                target=event.target,
                detail=dict(event.detail),
            )
            self._audit.append(record)
            return record

    async def list_audit(self, filters: AuditFilters, limit: int) -> list[AuditRecord]:
        """Matching audit rows, newest first (insertion order breaks
        ``occurred_at`` ties, so a fixed test clock still reads newest
        first)."""
        with self._lock:
            ordered = [
                r
                for _, r in sorted(
                    enumerate(self._audit),
                    key=lambda pair: (pair[1].occurred_at, pair[0]),
                    reverse=True,
                )
            ]
            if filters.before is not None:
                ids = [r.id for r in ordered]
                if filters.before not in ids:
                    return []
                ordered = ordered[ids.index(filters.before) + 1 :]
            return [r for r in ordered if filters.matches(r)][:limit]


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


def _signals_for(feedback: dict[tuple[str, str, str], Feedback], entry_id: str) -> FeedbackSignals:
    helpful, stale, wrong = _count_for(feedback, entry_id)
    noted: list[datetime] = []
    flags: list[datetime] = []
    flagged: list[Feedback] = []
    flagged_notes: list[Feedback] = []
    for fb in feedback.values():
        if fb.entry_id != entry_id:
            continue
        if fb.verdict is Verdict.HELPFUL:
            if fb.note and fb.note.strip() and fb.updated_at is not None:
                noted.append(fb.updated_at)
        else:
            flags.append(fb.updated_at or _EPOCH)
            flagged.append(fb)
            if fb.note and fb.note.strip():
                flagged_notes.append(fb)
    correction = None
    if flagged_notes:
        # Newest first, then reporter and agent: Postgres's ORDER BY.
        flagged_notes.sort(key=lambda fb: (fb.user, fb.agent))
        flagged_notes.sort(key=lambda fb: fb.updated_at or _EPOCH, reverse=True)
        newest = flagged_notes[0]
        correction = Correction(
            newest.verdict,
            truncate_note(newest.note or ""),
            newest.user,
            newest.updated_at or _EPOCH,
        )
    return FeedbackSignals(
        helpful,
        stale,
        wrong,
        last_confirmed(max(noted, default=None), max(flags, default=None)),
        correction,
        newest_flag_at=max(
            (fb.updated_at for fb in flagged if fb.updated_at is not None), default=None
        ),
    )


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
