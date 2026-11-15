"""A fleet's pinned entries."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from hivemind.domain.pin import Pin, PinRefusal, PinWrite


@runtime_checkable
class PinPort(Protocol):
    """A fleet's pinned entries."""

    async def pin_entry(
        self,
        fleet_id: str,
        entry_id: str,
        pinned_by: str,
        limit: int,
        replaces: Sequence[str] = (),
        agent_limit: int | None = None,
        also_owned_by: Sequence[str] = (),
    ) -> PinWrite | PinRefusal:
        """Pin ``entry_id`` to ``fleet_id`` with a fresh ``pinned_at`` / ``pinned_by`` (a re-pin
        refreshes).

        - Pins in ``replaces`` are removed in the same step and reported in ``PinWrite.replaced``.
        - ``PinRefusal``, nothing written, when the fleet would hold more than ``limit`` pins (a
          move or refresh never grows the count), or ``pinned_by`` more than ``agent_limit`` of them
          (``None`` = exempt; a refresh restamps ``pinned_by``, so it counts). Both checked
          atomically (ADR 0096, ``pin_refusal``); pins of ``also_owned_by`` (ADR 0113) count as its
          own.
        - A malformed ``entry_id`` is refused as ``FLEET``; a merged ``fleet_id`` raises
          ``FleetMerged`` (ADR 0107).
        - The caller has checked the entry and the permission."""
        ...

    async def unpin_entry(self, fleet_id: str, entry_id: str) -> bool:
        """Remove a pin; whether there was one."""
        ...

    async def list_pins(self, fleet_id: str) -> list[Pin]:
        """A fleet's pins, newest first."""
        ...
