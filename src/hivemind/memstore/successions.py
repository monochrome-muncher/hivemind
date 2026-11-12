"""Agent retirements and the authorship they hand down."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from hivemind.domain.access import (
    SCOPE_FLEET,
    SCOPE_SELF,
    AgentStatus,
    InvalidAgentStatus,
    UnknownAgent,
)
from hivemind.domain.entry import EntryState
from hivemind.domain.succession import HandedDown, Succession, SuccessionRefused, UnknownSuccession
from hivemind.domain.validation import InvalidInput
from hivemind.memstore.state import MemoryState


class SuccessionStore(MemoryState):
    async def retire_agent(self, retired: str, successor: str, by: str) -> Succession:
        if retired == successor:
            raise InvalidInput("an agent cannot be its own successor")
        with self._lock:
            for name in (retired, successor):
                if name not in self._agents:
                    raise UnknownAgent(f"unknown agent: {name}")
            old, new = self._agents[retired], self._agents[successor]
            if old.status is AgentStatus.PENDING:
                raise InvalidAgentStatus(retired, old.status, "retire")
            if new.status is not AgentStatus.ACTIVE:
                raise InvalidAgentStatus(successor, new.status, "make a successor of")
            if (standing := self._successions.get(retired)) is not None:
                raise SuccessionRefused(
                    f"agent {retired!r} is already retired in favour of {standing.successor!r}"
                )
            if successor in self._inherited(retired):
                raise SuccessionRefused(
                    f"{successor!r} already hands its memories down to {retired!r}, "
                    "directly or through others: that would be a cycle"
                )
            succession = Succession(retired, successor, by, self._clock(), was=old.status)
            self._successions[retired] = succession
            if old.status is AgentStatus.ACTIVE:
                self._agents[retired] = replace(old, status=AgentStatus.REVOKED)
            return succession

    async def undo_succession(self, retired: str) -> Succession:
        with self._lock:
            succession = self._successions.pop(retired, None)
        if succession is None:
            raise UnknownSuccession(f"no succession retires {retired!r}")
        return succession

    async def get_succession(self, retired: str) -> Succession | None:
        with self._lock:
            return self._successions.get(retired)

    async def list_successions(self) -> list[Succession]:
        with self._lock:
            rows = list(self._successions.values())
        rows.sort(key=lambda s: s.retired_agent)
        rows.sort(key=lambda s: s.at, reverse=True)
        return rows

    async def inherited_authors(self, name: str) -> tuple[str, ...]:
        with self._lock:
            return self._inherited(name)

    async def handed_down(self, authors: Sequence[str], home_fleet_id: str | None) -> HandedDown:
        names = set(authors)
        with self._lock:
            mine = [
                e
                for e in self._entries.values()
                if e.state is EntryState.ACTIVE and e.author in names
            ]
            pins = sum(1 for p in self._pins.values() if p.pinned_by in names)
        fleet = [e for e in mine if e.scope == SCOPE_FLEET]
        return HandedDown(
            authors=tuple(authors),
            self_notes=sum(1 for e in mine if e.scope == SCOPE_SELF),
            fleet_memories=len(fleet),
            fleet_memories_elsewhere=sum(1 for e in fleet if e.fleet_id != home_fleet_id),
            pins=pins,
        )
