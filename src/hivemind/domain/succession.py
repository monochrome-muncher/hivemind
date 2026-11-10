"""Retiring an agent with a successor (ADR 0113). The successor *owns* the retired agent's memories
(reads, supersedes, withdraws, cannot rate them); ``Entry.author`` stays the original name."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from hivemind.domain.access import AgentStatus


class SuccessionRefused(Exception):
    """The retirement or its undo is not allowed now (409 ``succession_refused``): a cycle, or
    the agent is already retired."""


class UnknownSuccession(KeyError):
    """No succession retires this agent (404 ``not_found``). A ``KeyError``, so ``str(exc)`` is quoted."""


class AgentRetired(Exception):
    """The agent was retired (ADR 0113): 409 ``agent_retired`` until the retirement is undone."""

    def __init__(self, name: str, successor: str) -> None:
        super().__init__(
            f"agent {name!r} was retired in favour of {successor!r}: "
            "undo the retirement before activating it again"
        )
        self.name = name
        self.successor = successor


@dataclass(frozen=True, slots=True)
class Succession:
    """One standing succession; ``by`` is the audit actor."""

    retired_agent: str
    successor: str
    by: str
    at: datetime
    # The status ``retire_agent`` saw under the row lock (``None`` when read back later); the
    # revoke audit follows it, not a read made before the lock.
    was: AgentStatus | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class HandedDown:
    """What passes to a successor, counted for the preview over ``authors`` (the retired agent
    and every agent that already handed down to it)."""

    authors: tuple[str, ...]
    self_notes: int
    fleet_memories: int
    # In a fleet other than the successor's home: owned, but fleet rules still limit them (ADR
    # 0113).
    fleet_memories_elsewhere: int
    pins: int


def owned_authors(name: str, inherited: tuple[str, ...]) -> tuple[str, ...]:
    """The authors ``name`` owns: itself first, then its predecessors, without repeats."""
    return tuple(dict.fromkeys((name, *inherited)))
