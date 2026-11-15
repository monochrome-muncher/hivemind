"""Offboarding an owner's agents."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from hivemind.domain.offboarding import OffboardMode, OffboardStep


@runtime_checkable
class OffboardingPort(Protocol):
    """Offboarding an owner's agents."""

    async def offboard_owner(
        self,
        alias: str,
        mode: OffboardMode,
        successor: str | None,
        by: str,
        expected: Mapping[str, str] | None = None,
    ) -> list[OffboardStep]:
        """Offboard every agent whose owner alias is exactly ``alias`` (ADR 0135), in one
        transaction with their rows (and the successor's) locked, so states are re-checked here
        and the plan is applied whole or not at all.

        ``mode`` ``retire``: each active or revoked agent is retired to ``successor`` as
        ``retire_agent`` does, a pending one is rejected. ``revoke``: active agents are revoked,
        pending ones rejected, ``successor`` is ``None``. Agents already revoked (or retired) are
        skipped. An agent that is retired yet active (an older pod re-activated it) is revoked under its
        succession in both modes. Returns one step per agent, by name. ``UnknownOwner``; ``UnknownAgent``
        (successor); ``InvalidAgentStatus`` (successor not active); ``SuccessionRefused``
        (successor of the same owner, or a cycle); ``InvalidInput`` (mode and successor do not go
        together, checked first). ``expected`` (agent name to action, the preview's) refuses with
        ``OffboardChanged`` when the plan under the locks differs. Every refusal changes nothing."""
        ...
