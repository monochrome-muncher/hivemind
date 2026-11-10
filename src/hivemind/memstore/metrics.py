"""Usage counters, fleet health, activity and convergence."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

from hivemind.domain.access import SCOPE_FLEET, SCOPE_ORG, SCOPE_SELF, AgentStatus
from hivemind.domain.activity import (
    Activity,
    AuthorActivity,
    DayActivity,
    ReceivedFeedback,
    ReporterActivity,
)
from hivemind.domain.entry import Entry, EntryState, ImportanceSource, Kind, UsageCount
from hivemind.domain.feedback import ORPHAN_SILENCE_DAYS, Feedback, Verdict
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
from hivemind.memstore.helpers import all_pairs_cosine, cosine_similarity
from hivemind.memstore.state import MemoryState

_DAYS_90 = timedelta(days=90)
_DAYS_60 = timedelta(days=60)
_DAYS_30 = timedelta(days=30)


def _mean_pairwise_cosine(entries: list[Entry]) -> float:
    """Mean cosine over all pairs; the caller guarantees at least two."""
    vectors = {e.id: list(e.embedding or ()) for e in entries}
    pairs = all_pairs_cosine([e.id for e in entries], vectors).values()
    return sum(pairs) / len(pairs)


class MetricsStore(MemoryState):
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
