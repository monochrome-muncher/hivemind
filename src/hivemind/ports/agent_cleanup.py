"""Agent cleanups and their undo."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from hivemind.domain.access import (
    Visibility,
)
from hivemind.domain.agent_cleanup import (
    AgentCleanupRecord,
    CleanupOutcome,
    CleanupScan,
    CleanupUndoOutcome,
)


@runtime_checkable
class AgentCleanupPort(Protocol):
    """Agent cleanups and their undo."""

    async def scan_agent_cleanup(
        self,
        agent: str,
        since: datetime,
        until: datetime,
        *,
        authors: Sequence[str] | None = None,
        visibility: Visibility | None = None,
    ) -> CleanupScan:
        """Read-only: what cleaning up ``agent`` over ``[since, until)`` would change (ADR 0111).

        - ``authors``: whose memories count (``[agent]`` if omitted; the portal passes every author
          the agent owns, ADR 0118). ``visibility`` limits restorable memories; ratings are always
          ``agent``'s own.
        - Holds: those authors' active memories created in the window (any scope), the agent's
          ratings updated in the window and not already set aside, the pins on those memories, and
          the memories they superseded (still ``superseded`` by a memory to withdraw).
        - Lists are cut to the preview limits, newest first; totals are exact."""
        ...

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
        """Clean up ``agent`` in one transaction (ADR 0111).

        - Withdraws the memories ``scan_agent_cleanup`` lists (reason ``cleanup_reason``), unpins
          them, sets the window's ratings aside, puts ``restore_ids`` back to ``active``.
        - Records the change for ``undo_agent_cleanup``; nothing to change keeps no record
          (``record`` is ``None``).
        - ``AgentCleanupRefused`` (nothing changes): a ``restore_ids`` memory not restorable, counts
          differ from ``expected_*`` (the preview), or another cleanup of the agent is running.
          ``InvalidInput`` for an empty window.
        - The agent's own writes and ratings wait for the row locks, never interleave.
        - ``authors`` as in ``scan_agent_cleanup``; ``self_service`` records the agent ran it itself
          (ADR 0118); ``visibility`` (self-service) limits what may be offered and restored."""
        ...

    async def mark_cleanup_revoked(self, cleanup_id: str) -> None:
        """Note on the record that the cleanup also revoked the agent."""
        ...

    async def undo_agent_cleanup(self, cleanup_id: str) -> CleanupUndoOutcome:
        """Undo a cleanup (ADR 0111): reactivate memories still withdrawn with its reason,
        re-supersede the restored ones (only if still active and their former successor is active),
        clear its rating marks, re-pin where the fleet is under the caps. Does not un-revoke.

        ``UnknownAgentCleanup``; ``AgentCleanupRefused`` when ``undo_refusal`` says no."""
        ...

    async def list_agent_cleanups(
        self, limit: int, *, agent: str | None = None
    ) -> list[AgentCleanupRecord]:
        """Recorded cleanups, newest first; only ``agent``'s when given."""
        ...

    async def get_agent_cleanup(self, cleanup_id: str) -> AgentCleanupRecord | None: ...
