"""Fleet merges and agent moves."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from hivemind.domain.fleet_merge import (
    DuplicateScan,
    FleetMergeRecord,
    MergeOutcome,
    MoveOutcome,
    MovePlan,
    UnmergeOutcome,
)


@runtime_checkable
class FleetMergePort(Protocol):
    """Fleet merges and agent moves."""

    async def duplicate_pairs(
        self, fleet_id: str, other_id: str, threshold: float, cap: int
    ) -> DuplicateScan:
        """For the newest ``cap`` active vectored entries of ``fleet_id``, the nearest active entry
        of ``other_id`` at cosine similarity >= ``threshold``, highest first.

        ``truncated`` when more than ``cap`` qualified. Approximate on Postgres (ADR 0025)."""
        ...

    async def merge_fleets(
        self,
        source_id: str,
        *,
        target_fleet_id: str | None = None,
        new_fleet_name: str | None = None,
        keep_pin_ids: Sequence[str] | None = None,
        expected_pin_ids: Sequence[str] | None = None,
        merged_by: str,
    ) -> MergeOutcome:
        """Merge ``source_id`` into an existing target or a new fleet, in one transaction (ADR
        0107).

        - Re-files its entries (any state), moves its agents, re-keys the kept pins, unpins the
          rest, adds its search counters to the target's, marks it merged.
        - Records everything moved so ``unmerge_fleet`` can reverse it.
        - Refusals change nothing: ``UnknownFleet``; ``FleetMerged`` (either side already merged);
          ``FleetMergeRefused`` (same fleet on both sides, or the pins now differ from
          ``expected_pin_ids``, the pins the preview showed); ``FleetExists`` (taken new name);
          ``InvalidInput`` (neither or both targets, or a pin choice ``choose_pins`` refuses).
        - A write into a fleet being merged waits briefly, then raises ``FleetBusy`` (retryable)."""
        ...

    async def plan_fleet_move(self, source_id: str, agents: Sequence[str]) -> MovePlan:
        """Read-only: the memories moving ``agents`` out of ``source_id`` would re-file
        (``plan_move_entries``, ADR 0112)."""
        ...

    async def move_agents(
        self,
        source_id: str,
        agents: Sequence[str],
        *,
        target_fleet_id: str | None = None,
        new_fleet_name: str | None = None,
        keep_pin_ids: Sequence[str] | None = None,
        expected_pin_ids: Sequence[str] | None = None,
        moved_by: str,
    ) -> MoveOutcome:
        """Move ``agents`` (home fleet ``source_id``) and the ``fleet``-scoped memories they wrote
        there into an existing target or a new fleet, in one transaction (ADR 0112).

        - Memories ``plan_move_entries`` selects are re-filed (any state); their pins follow; the
          target's pins plus those are trimmed with ``choose_pins``.
        - The source stays a normal fleet; search counters stay.
        - Recorded in the merge list as kind ``move`` so ``unmerge_fleet`` can reverse it.
        - Refusals change nothing: ``UnknownFleet``, ``FleetMerged`` (either side), ``FleetExists``,
          ``InvalidInput`` (no agents, not exactly one target, bad pin choice), ``FleetMoveRefused``
          (same fleet, an agent not in the source, ``expected_pin_ids`` stale, busy locks).
        - Writes into the two fleets wait briefly, then raise ``FleetBusy``."""
        ...

    async def unmerge_fleet(self, merge_id: str) -> UnmergeOutcome:
        """Undo a merge or move (ADR 0107, 0112): recorded entries and agents still in the target go
        back to the source, with their pins and the dropped pins that fit.

        Search counters stay summed; a merge also clears the source's merged mark.
        ``UnknownFleetMerge``; ``FleetMergeRefused`` (``FleetMoveRefused`` for a move) when
        ``undo_refusal`` says no."""
        ...

    async def list_fleet_merges(self, limit: int) -> list[FleetMergeRecord]:
        """Recorded merges, newest first."""
        ...

    async def get_fleet_merge(self, merge_id: str) -> FleetMergeRecord | None: ...
