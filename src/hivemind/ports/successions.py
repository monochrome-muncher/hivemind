"""Retirements: an agent handing its memories down to a successor."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from hivemind.domain.succession import HandedDown, Succession


@runtime_checkable
class SuccessionPort(Protocol):
    """Retirements: an agent handing its memories down to a successor."""

    async def retire_agent(self, retired: str, successor: str, by: str) -> Succession:
        """Record that ``retired`` hands its memories to ``successor`` and revoke ``retired`` (when
        active), in one transaction (ADR 0113). The key stops working with the status.

        Refusals change nothing: ``UnknownAgent``; ``InvalidAgentStatus`` (``retired`` pending or
        ``successor`` not active); ``SuccessionRefused`` (``retired`` already hands down, or
        ``successor`` hands down to it directly or through a chain). ``was`` is ``retired``'s
        status seen under the lock."""
        ...

    async def undo_succession(self, retired: str) -> Succession:
        """Delete ``retired``'s succession; the agent stays revoked. ``UnknownSuccession`` when none
        stands."""
        ...

    async def get_succession(self, retired: str) -> Succession | None: ...

    async def list_successions(self) -> list[Succession]:
        """Every standing succession, newest first."""
        ...

    async def inherited_authors(self, name: str) -> tuple[str, ...]:
        """Agents that hand down to ``name``, transitively, sorted
        (``Credential.inherits_from``)."""
        ...

    async def handed_down(self, authors: Sequence[str], home_fleet_id: str | None) -> HandedDown:
        """What owning ``authors`` brings: active ``self`` notes, ``fleet`` memories (and how many
        sit outside ``home_fleet_id``) and pins."""
        ...
