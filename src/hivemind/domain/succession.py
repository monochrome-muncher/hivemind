"""Retiring an agent with a successor: the records and refusals (ADR 0113).

Pure data, no I/O. A succession hands every memory an agent wrote to another
agent: the successor then *owns* them (reads, supersedes, withdraws, cannot
rate them), while ``Entry.author`` stays the original name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from hivemind.domain.access import AgentStatus


class SuccessionRefused(Exception):
    """The retirement or its undo is not allowed in the agents' current
    state (409 ``succession_refused``): the successor would hand down to
    the agent being retired (a cycle), or the agent is already retired."""


class UnknownSuccession(KeyError):
    """No succession retires this agent (404 ``not_found``). A ``KeyError``, so ``str(exc)`` is quoted."""


class AgentRetired(Exception):
    """The agent was retired with a successor (ADR 0113), so it cannot be
    activated again until the admin undoes the retirement (409
    ``agent_retired``)."""

    def __init__(self, name: str, successor: str) -> None:
        super().__init__(
            f"agent {name!r} was retired in favour of {successor!r}: "
            "undo the retirement before activating it again"
        )
        self.name = name
        self.successor = successor


@dataclass(frozen=True, slots=True)
class Succession:
    """One standing succession: ``retired_agent`` hands its memories to
    ``successor``. ``by`` is the audit actor that recorded it."""

    retired_agent: str
    successor: str
    by: str
    at: datetime
    # The retired agent's status as ``retire_agent`` saw it under the row
    # lock (``None`` on a record read back later): the revoke audit and
    # ``was`` follow it, not a read made before the lock.
    was: AgentStatus | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class HandedDown:
    """What passes to a successor, counted for the preview. ``authors`` are
    the agents whose memories pass over: the retired agent and every agent
    that already handed down to it. The counts are over those authors."""

    authors: tuple[str, ...]
    self_notes: int
    fleet_memories: int
    # Fleet memories that sit in a fleet other than the successor's home
    # fleet: the successor owns them, and fleet rules still limit what it
    # may do with them (ADR 0113).
    fleet_memories_elsewhere: int
    pins: int


def owned_authors(name: str, inherited: tuple[str, ...]) -> tuple[str, ...]:
    """The authors ``name`` owns: itself first, then every agent that hands
    down to it, without repeats."""
    return tuple(dict.fromkeys((name, *inherited)))
