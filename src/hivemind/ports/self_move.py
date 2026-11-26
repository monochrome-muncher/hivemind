"""Moving an entry to its author's own scope."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from hivemind.domain.self_move import SelfMove


@runtime_checkable
class SelfMovePort(Protocol):
    """Moving an entry to its author's own scope."""

    async def move_entry_to_self(self, entry_id: str) -> SelfMove:
        """Narrow an active fleet memory to `self` in one transaction (ADR 0145).

        - Moves the fleet versions by the same author it replaced (through that author's
          versions only) with it.
        - Removes every pin on a moved version.
        - ``UnknownEntry`` if unknown; ``SelfMoveRefused`` unless active and fleet-scoped, or when
          a moved version replaced another agent's fleet memory (nothing changes);
          ``FleetMerged`` / ``FleetBusy`` as a fleet write."""
        ...
