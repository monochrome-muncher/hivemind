"""Admin agent cleanups and their undo."""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import replace
from datetime import datetime

from hivemind.domain.access import Visibility, entry_is_visible
from hivemind.domain.agent_cleanup import (
    PREVIEW_RESTORE_LIMIT,
    PREVIEW_WITHDRAW_LIMIT,
    AgentCleanupRecord,
    AgentCleanupRefused,
    CleanupOutcome,
    CleanupScan,
    CleanupUndoOutcome,
    RestoredEntry,
    UnknownAgentCleanup,
    check_counts_unchanged,
    check_restore_ids,
    check_window,
    cleanup_reason,
)
from hivemind.domain.agent_cleanup import undo_refusal as cleanup_undo_refusal
from hivemind.domain.entry import Entry, EntryState
from hivemind.domain.fleet_merge import DroppedPin
from hivemind.domain.validation import is_valid_uuid
from hivemind.memstore.helpers import canonical_or_same, copy_entry
from hivemind.memstore.state import MemoryState


class AgentCleanupStore(MemoryState):
    def _chain_end(self, entry_id: str, stop: frozenset[str] = frozenset()) -> str:
        """The id a pin on ``entry_id`` shows: its chain's end, or the first ``stop`` memory on
        the way. Callers hold the lock."""
        seen = {entry_id}
        current = self._entries.get(entry_id)
        while (
            current is not None
            and current.id not in stop
            and current.state is EntryState.SUPERSEDED
            and current.superseded_by is not None
            and current.superseded_by not in seen
        ):
            seen.add(current.superseded_by)
            nxt = self._entries.get(current.superseded_by)
            if nxt is None:
                return current.superseded_by
            current = nxt
        return current.id if current is not None else entry_id

    def _cleanup_sets(
        self,
        agent: str,
        since: datetime,
        until: datetime,
        authors: Collection[str],
        visibility: Visibility | None = None,
    ) -> tuple[list[Entry], list[tuple[Entry, str, str]], list[tuple[str, str, str]]]:
        """The memories to withdraw, the restorable ones (each with its replacement and chain end),
        both newest first, and the rating keys to set aside; restorable as in ``CleanupScan``.
        Callers hold the lock."""
        withdraw = sorted(
            (
                e
                for e in self._entries.values()
                if e.author in authors
                and e.state is EntryState.ACTIVE
                and since <= e.created_at < until
            ),
            key=lambda e: (e.created_at, e.id),
            reverse=True,
        )
        own = {
            e.id: e
            for e in self._entries.values()
            if e.author in authors and since <= e.created_at < until
        }
        found: list[tuple[Entry, str, str]] = []
        for e in self._entries.values():
            if (
                e.id in own
                or e.state is not EntryState.SUPERSEDED
                or e.superseded_by not in own
                or e.redacted_at is not None
            ):
                continue
            first = e.superseded_by
            cur = own[first]
            hops = 0
            while (
                cur.state is EntryState.SUPERSEDED and cur.superseded_by in own and hops <= len(own)
            ):
                cur = own[cur.superseded_by]
                hops += 1
            if cur.state is EntryState.ACTIVE and (
                visibility is None or entry_is_visible(e, visibility)
            ):
                found.append((e, first, cur.id))
        restorable = sorted(found, key=lambda t: (t[0].created_at, t[0].id), reverse=True)
        ratings = [
            key
            for key, fb in self._feedback.items()
            if fb.user == agent
            and key not in self._set_aside
            and fb.updated_at is not None
            and since <= fb.updated_at < until
        ]
        return withdraw, restorable, ratings

    async def scan_agent_cleanup(
        self,
        agent: str,
        since: datetime,
        until: datetime,
        *,
        authors: Sequence[str] | None = None,
        visibility: Visibility | None = None,
    ) -> CleanupScan:
        with self._lock:
            withdraw, restorable, ratings = self._cleanup_sets(
                agent, since, until, sorted(set(authors or ()) | {agent}), visibility
            )
            by_verdict = Counter(self._feedback[k].verdict.value for k in ratings)
            ids = {e.id for e in withdraw}
            offered = {t[0].id for t in restorable}
            pins = sum(
                1
                for (_, eid) in self._pins
                if eid not in offered and self._chain_end(eid, frozenset(offered)) in ids
            )
            return CleanupScan(
                since=since,
                until=until,
                withdraw=tuple(copy_entry(e) for e in withdraw[:PREVIEW_WITHDRAW_LIMIT]),
                withdraw_total=len(withdraw),
                ratings_total=len(ratings),
                ratings_by_verdict=dict(by_verdict),
                pins_total=pins,
                restorable=tuple(copy_entry(t[0]) for t in restorable[:PREVIEW_RESTORE_LIMIT]),
                restorable_total=len(restorable),
            )

    async def cleanup_agent(
        self,
        agent: str,
        since: datetime,
        until: datetime,
        *,
        restore_ids: Sequence[str] | None = None,
        expected_withdraw: int | None = None,
        expected_ratings: int | None = None,
        cleaned_by: str,
        authors: Sequence[str] | None = None,
        self_service: bool = False,
        visibility: Visibility | None = None,
    ) -> CleanupOutcome:
        check_window(since, until)
        # Spelled as the store reads ids back; a malformed one is simply not restorable.
        wanted = [
            str(uuid.UUID(r)) if is_valid_uuid(r) else r for r in dict.fromkeys(restore_ids or ())
        ]
        with self._lock:
            withdraw, restorable, ratings = self._cleanup_sets(
                agent, since, until, sorted(set(authors or ()) | {agent}), visibility
            )
            restore = check_restore_ids([t[0].id for t in restorable], wanted)
            check_counts_unchanged(len(withdraw), len(ratings), expected_withdraw, expected_ratings)
            if not withdraw and not ratings:
                return CleanupOutcome(None)
            # Nothing below can fail: mutate.
            cleanup_id = str(uuid.uuid4())
            now = self._clock()
            reason = cleanup_reason(cleanup_id)
            withdraw_ids = [e.id for e in sorted(withdraw, key=lambda e: e.id)]
            for entry in withdraw:
                self._entries[entry.id] = replace(
                    entry, state=EntryState.WITHDRAWN, withdrawn_reason=reason, withdrawn_at=now
                )
                self._withdrawn_at[entry.id] = now
            gone = set(withdraw_ids)
            keep = set(restore)
            unpinned = tuple(
                DroppedPin(p.fleet_id, p.entry_id, p.pinned_by, p.pinned_at)
                for (_, eid), p in sorted(self._pins.items(), key=lambda kv: kv[0])
                if eid not in keep and self._chain_end(eid, frozenset(keep)) in gone
            )
            for d in unpinned:
                del self._pins[(d.fleet_id, d.entry_id)]
            by_id = {t[0].id: t for t in restorable}
            restored = tuple(RestoredEntry(e, by_id[e][1], by_id[e][2]) for e in restore)
            for item in restored:
                self._entries[item.entry_id] = replace(
                    self._entries[item.entry_id], state=EntryState.ACTIVE, superseded_by=None
                )
            for key in ratings:
                self._set_aside[key] = cleanup_id
            record = AgentCleanupRecord(
                id=cleanup_id,
                agent=agent,
                since=since,
                until=until,
                cleaned_by=cleaned_by,
                cleaned_at=now,
                withdrawn_ids=tuple(withdraw_ids),
                restored=restored,
                ratings_set_aside=len(ratings),
                unpinned_pins=unpinned,
                self_service=self_service,
            )
            self._cleanups.append(record)
            return CleanupOutcome(record, len(withdraw_ids), len(ratings), len(restored), unpinned)

    async def mark_cleanup_revoked(self, cleanup_id: str) -> None:
        cleanup_id = canonical_or_same(cleanup_id)
        with self._lock:
            for i, record in enumerate(self._cleanups):
                if record.id == cleanup_id:
                    self._cleanups[i] = replace(record, revoked=True)

    async def undo_agent_cleanup(self, cleanup_id: str) -> CleanupUndoOutcome:
        cleanup_id = canonical_or_same(cleanup_id)
        with self._lock:
            record = next((c for c in self._cleanups if c.id == cleanup_id), None)
            if record is None:
                raise UnknownAgentCleanup(f"unknown agent cleanup: {cleanup_id}")
            reason = cleanup_undo_refusal(record, list(reversed(self._cleanups)))
            if reason is not None:
                raise AgentCleanupRefused(reason)
            why = cleanup_reason(record.id)
            back = [
                eid
                for eid in record.withdrawn_ids
                if (e := self._entries.get(eid)) is not None
                and e.state is EntryState.WITHDRAWN
                and e.withdrawn_reason == why
                and e.redacted_at is None  # a redacted memory never comes back (ADR 0125)
            ]
            for eid in back:
                self._entries[eid] = replace(
                    self._entries[eid],
                    state=EntryState.ACTIVE,
                    withdrawn_reason=None,
                    withdrawn_at=None,
                )
                self._withdrawn_at.pop(eid, None)
            again = []
            for item in record.restored:
                entry = self._entries.get(item.entry_id)
                head = self._entries.get(item.head or item.superseded_by)
                if (
                    entry is not None
                    and entry.state is EntryState.ACTIVE
                    and head is not None
                    and head.state is EntryState.ACTIVE
                ):
                    self._entries[item.entry_id] = replace(
                        entry, state=EntryState.SUPERSEDED, superseded_by=item.superseded_by
                    )
                    again.append(item.entry_id)
            marked = [k for k, cid in self._set_aside.items() if cid == record.id]
            for key in marked:
                del self._set_aside[key]
            restored_pins: list[DroppedPin] = []
            skipped_pins: list[DroppedPin] = []
            for fleet_id in sorted({d.fleet_id for d in record.unpinned_pins}):
                mine = [d for d in record.unpinned_pins if d.fleet_id == fleet_id]
                put, left = self._restore_pins(
                    fleet_id,
                    mine,
                    lambda e: e.state in (EntryState.ACTIVE, EntryState.SUPERSEDED),
                )
                restored_pins.extend(put)
                skipped_pins.extend(left)
            done = replace(record, undone_at=self._clock())
            self._cleanups[self._cleanups.index(record)] = done
            return CleanupUndoOutcome(
                done,
                reactivated=len(back),
                reactivate_skipped=len(record.withdrawn_ids) - len(back),
                resuperseded=len(again),
                resupersede_skipped=len(record.restored) - len(again),
                ratings_cleared=len(marked),
                pins_restored=tuple(restored_pins),
                pins_skipped=tuple(skipped_pins),
            )

    async def list_agent_cleanups(
        self, limit: int, *, agent: str | None = None
    ) -> list[AgentCleanupRecord]:
        with self._lock:
            mine = [c for c in reversed(self._cleanups) if agent is None or c.agent == agent]
            return mine[:limit]

    async def get_agent_cleanup(self, cleanup_id: str) -> AgentCleanupRecord | None:
        cleanup_id = canonical_or_same(cleanup_id)
        with self._lock:
            return next((c for c in self._cleanups if c.id == cleanup_id), None)
