"""Renaming an agent (ADR 0159): every stored copy of its name, rewritten under one lock."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from hivemind.domain.access import Agent, AgentNameTaken, UnknownAgent
from hivemind.domain.feedback import Feedback
from hivemind.domain.fleet_merge import DroppedPin
from hivemind.memstore.state import MemoryState


def _repin(pins: tuple[DroppedPin, ...], old: str, new: str) -> tuple[DroppedPin, ...]:
    return tuple(replace(p, pinned_by=new) if p.pinned_by == old else p for p in pins)


def _renamed_key(key: tuple[str, str, str], old: str, new: str) -> tuple[str, str, str]:
    """A feedback key (entry, user, agent): a rating by the agent follows its name; an ``agent``
    label that is not the name stays."""
    entry_id, user, agent = key
    if user != old:
        return key
    return entry_id, new, new if agent == old else agent


def _renamed_feedback(fb: Feedback, old: str, new: str) -> Feedback:
    if fb.user != old:
        return fb
    return replace(fb, user=new, agent=new if fb.agent == old else fb.agent)


class AgentRenameStore(MemoryState):
    async def rename_agent(self, name: str, new_name: str) -> tuple[Agent, Agent]:
        """Rename an agent, any status (ADR 0159); returns (before, after)."""
        with self._lock:
            before = self._agents.get(name)
            if before is None:
                raise UnknownAgent(f"unknown agent: {name}")
            if name == new_name:
                return before, before
            self._refuse_taken(name, new_name)
            after = replace(before, name=new_name)
            self._rewrite_copies(name, new_name)
            self._agents = {
                (new_name if k == name else k): (after if k == name else v)
                for k, v in self._agents.items()
            }
            self._keep_former_name(name, new_name)
            return before, after

    async def former_agent_names(self, names: Sequence[str]) -> dict[str, tuple[str, ...]]:
        with self._lock:
            return {
                name: tuple(n for n, holder in self._former_names.items() if holder == name)
                for name in names
            }

    def _refuse_taken(self, old: str, new: str) -> None:
        taken = any(n != old and n.lower() == new.lower() for n in self._agents)
        reserved = any(
            holder != old and n.lower() == new.lower() for n, holder in self._former_names.items()
        )
        if taken or reserved:
            raise AgentNameTaken(new)

    def _keep_former_name(self, old: str, new: str) -> None:
        """As Postgres: the name follows, the new name stops being a former one, and a case-only
        change or an already reserved spelling leaves nothing to add."""
        followed = {
            n: (new if holder == old else holder) for n, holder in self._former_names.items()
        }
        self._former_names = {n: h for n, h in followed.items() if n.lower() != new.lower()}
        reserved = any(n.lower() == old.lower() for n in self._former_names)
        if old.lower() != new.lower() and not reserved:
            self._former_names[old] = new

    def _rewrite_copies(self, old: str, new: str) -> None:
        """The agent's key facts and every record that names it. Callers hold the lock."""
        for facts in (self._key_last_used, self._key_created, self._key_locked):
            if old in facts:
                facts[new] = facts.pop(old)
        self._entries = {
            i: replace(e, author=new, agent=new if e.agent == old else e.agent)
            if e.author == old
            else e
            for i, e in self._entries.items()
        }
        self._feedback = {
            _renamed_key(k, old, new): _renamed_feedback(fb, old, new)
            for k, fb in self._feedback.items()
        }
        self._set_aside = {_renamed_key(k, old, new): v for k, v in self._set_aside.items()}
        self._pins = {
            k: replace(p, pinned_by=new) if p.pinned_by == old else p for k, p in self._pins.items()
        }
        self._successions = {
            (new if k == old else k): replace(
                s,
                retired_agent=new if s.retired_agent == old else s.retired_agent,
                successor=new if s.successor == old else s.successor,
            )
            for k, s in self._successions.items()
        }
        self._cleanups = [
            replace(
                c,
                agent=new if c.agent == old else c.agent,
                cleaned_by=new if c.cleaned_by == old else c.cleaned_by,
                unpinned_pins=_repin(c.unpinned_pins, old, new)
                if c.undone_at is None
                else c.unpinned_pins,
            )
            for c in self._cleanups
        ]
        self._merges = [
            replace(
                m,
                moved_agents=tuple(new if a == old else a for a in m.moved_agents),
                dropped_pins=_repin(m.dropped_pins, old, new)
                if m.undone_at is None
                else m.dropped_pins,
            )
            for m in self._merges
        ]
