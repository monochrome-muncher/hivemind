"""The shared base of the in-memory store: its collections, locks and cross-area helpers."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from datetime import datetime

from hivemind.domain.access import (
    Agent,
    AgentStatus,
    Fleet,
    FleetBusy,
    FleetMerged,
    Visibility,
    entry_is_visible,
)
from hivemind.domain.agent_cleanup import AgentCleanupRecord
from hivemind.domain.audit import AuditRecord
from hivemind.domain.entry import Entry, EntryFilters, EntryState
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.domain.fleet_merge import DroppedPin, FleetMergeRecord, plan_pin_restore
from hivemind.domain.pin import Pin
from hivemind.domain.succession import Succession
from hivemind.domain.validation import utcnow
from hivemind.memstore.helpers import copy_entry, count_for


class MemoryState:
    """The in-memory collections and the private helpers the areas share."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock: Callable[[], datetime] = clock or utcnow
        self._lock = threading.RLock()
        self._entries: dict[str, Entry] = {}
        self._feedback: dict[tuple[str, str, str], Feedback] = {}
        # Ratings an admin cleanup set aside -> its id (ADR 0111);
        # Postgres: feedbacks.set_aside_by
        self._set_aside: dict[tuple[str, str, str], str] = {}
        self._withdrawn_at: dict[str, datetime] = {}  # ADR 0098; Postgres: entries.withdrawn_at
        self._fleets: dict[str, Fleet] = {}
        self._agents: dict[str, Agent] = {}
        # The agent's key facts (ADR 0110), kept apart from the agent record
        # and merged only into ``list_agents``, as the real adapter joins them.
        self._key_last_used: dict[str, datetime] = {}
        self._key_locked: dict[str, datetime] = {}
        self._key_created: dict[str, datetime] = {}
        self._audit: list[AuditRecord] = []  # append-only (ADR 0027)
        self._pins: dict[tuple[str, str], Pin] = {}  # (fleet, entry), ADR 0058
        self._successions: dict[str, Succession] = {}  # retired agent, ADR 0113
        self._searches: dict[str | None, tuple[int, int, int]] = {}  # ADR 0056, 0078
        # (from, to) -> when the link was made, insertion-ordered (ADR 0057, 0081)
        self._links: dict[tuple[str, str], datetime] = {}
        # Fleet merges, oldest first, and the entry ids each moved (ADR 0107)
        self._merges: list[FleetMergeRecord] = []
        self._merge_entries: dict[str, list[str]] = {}
        # Agent cleanups, oldest first (ADR 0111)
        self._cleanups: list[AgentCleanupRecord] = []

    def _visible(self, entry: Entry, visibility: Visibility | None) -> bool:
        return visibility is None or entry_is_visible(entry, visibility)

    def _live_feedback(self) -> dict[tuple[str, str, str], Feedback]:
        """Feedback rows of non-revoked reporters (ADR 0067) not set aside (ADR 0111). Callers hold
        the lock."""
        revoked = {n for n, a in self._agents.items() if a.status is AgentStatus.REVOKED}
        if not revoked and not self._set_aside:
            return self._feedback
        return {
            k: fb
            for k, fb in self._feedback.items()
            if fb.user not in revoked and k not in self._set_aside
        }

    def _matches(self, entry: Entry, filters: EntryFilters) -> bool:
        """``filters.matches`` plus ``flagged`` (ADR 0054) and ``relied_on_changed`` (ADR 0098),
        which read feedback rows. Callers hold the lock."""
        if not filters.matches(copy_entry(entry)):
            return False
        if filters.flagged:
            _, stale, wrong = count_for(self._live_feedback(), entry.id)
            if stale + wrong == 0:
                return False
        return not filters.relied_on_changed or self._relied_on_changed(entry, filters.reporter)

    def _relied_on_changed(self, entry: Entry, reporter: tuple[str, str] | None) -> bool:
        """ADR 0098, as PgStore's clause: the reporter's helpful row, then since it a withdrawal
        (undated counts), another user's successor, or another user's newer live stale/wrong
        report. Callers hold the lock."""
        if reporter is None:
            return False
        key = (entry.id, *reporter)
        own = self._feedback.get(key)
        if (
            own is None
            or key in self._set_aside
            or own.verdict is not Verdict.HELPFUL
            or own.updated_at is None
        ):
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

    def _refuse_merged(self, fleet_id: str | None) -> None:
        """``FleetMerged`` for a merged fleet (ADR 0107); an unknown id passes, as on Postgres.
        Caller holds the lock."""
        fleet = self._fleets.get(fleet_id) if fleet_id is not None else None
        if fleet is not None and fleet.merged_into is not None:
            target = self._fleets.get(fleet.merged_into)
            raise FleetMerged(fleet.id, fleet.merged_into, target.name if target else None)

    def _refuse_stale_home(self, fleet_id: str | None, author: str) -> None:
        """``FleetBusy`` when a move changed the author's home fleet mid-write (ADR 0112). Caller
        holds the lock."""
        agent = self._agents.get(author)
        if (
            fleet_id is not None
            and agent is not None
            and agent.home_fleet_id is not None
            and agent.home_fleet_id != fleet_id
        ):
            raise FleetBusy(
                fleet_id,
                f"the home fleet of {author} changed during this write, retry in a few seconds",
            )

    def _restore_pins(
        self, fleet_id: str, pins: Sequence[DroppedPin], may_pin: Callable[[Entry], bool]
    ) -> tuple[list[DroppedPin], list[DroppedPin]]:
        """Put ``pins`` back on ``fleet_id`` where the entry is still in the fleet,
        ``may_pin`` accepts it, it is not pinned, and the fleet's cap has room.
        Returns ``(restored, skipped)``. Call with ``self._lock`` held."""
        fits = [
            d
            for d in pins
            if (e := self._entries.get(d.entry_id)) is not None
            and e.fleet_id == fleet_id
            and may_pin(e)
            and (fleet_id, d.entry_id) not in self._pins
        ]
        skipped = [d for d in pins if d not in fits]
        held = [p.pinned_by for (f, _), p in self._pins.items() if f == fleet_id]
        put, over = plan_pin_restore(fits, held)
        skipped.extend(over)
        for d in put:
            self._pins[(fleet_id, d.entry_id)] = Pin(fleet_id, d.entry_id, d.pinned_by, d.pinned_at)
        return list(put), skipped

    async def health_check(self) -> bool:
        """In-memory pool: always healthy (ADR 0019)."""
        return True
