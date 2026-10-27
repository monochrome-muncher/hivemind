"""Fleet merge (ADR 0107): preview, merge, undo and the merge list.

Admin only (ADR 0012). The data move is one store transaction
(``Store.merge_fleets``); this layer validates, builds the preview and
audits afterwards (ADR 0027), as ``AccessService`` does. A merge writes no
memory.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from hivemind.domain.access import Fleet, FleetExists, FleetMerged, UnknownFleet
from hivemind.domain.audit import AuditAction
from hivemind.domain.entry import Entry
from hivemind.domain.fleet_merge import (
    DUPLICATE_LOOKUP_CAP,
    MERGE_PIN_LIMIT,
    FleetFacts,
    FleetMergeRecord,
    FleetMergeRefused,
    MergeOutcome,
    UnmergeOutcome,
    default_kept,
    newest_first,
    undo_refusal,
)
from hivemind.domain.pin import Pin
from hivemind.domain.validation import InvalidInput, validate_fleet_name
from hivemind.ports import Credential, Store
from hivemind.services.audit import record_admin_action
from hivemind.services.errors import PermissionDenied
from hivemind.services.write_warnings import NEAR_DUPLICATE_SIMILARITY

logger = logging.getLogger(__name__)

# The most merges one listing returns; the list is small, and the undo
# rule needs every newer merge of a fleet in view.
MERGE_LIST_LIMIT = 500


@dataclass(frozen=True, slots=True)
class PreviewSide:
    """One fleet of a previewed merge. ``fleet`` is ``None`` for a new
    target (``name`` is then the name it would get)."""

    name: str
    fleet: Fleet | None
    facts: FleetFacts
    pins: int


@dataclass(frozen=True, slots=True)
class PreviewPin:
    """A pin of either fleet, with the entry it points at (when it still
    exists) and whether the merge keeps it unless the admin changes the pick."""

    pin: Pin
    entry: Entry | None
    default_kept: bool


@dataclass(frozen=True, slots=True)
class PreviewDuplicate:
    """A likely duplicate pair (shown only; the merge changes no memory)."""

    entry: Entry | None
    match: Entry | None
    entry_id: str
    match_id: str
    similarity: float


@dataclass(frozen=True, slots=True)
class MergePreview:
    """What ``dry_run`` reports."""

    source: PreviewSide
    target: PreviewSide
    pins: tuple[PreviewPin, ...]
    pin_limit: int
    needs_pin_choice: bool
    duplicates: tuple[PreviewDuplicate, ...]
    duplicates_looked_up: int
    duplicates_truncated: bool


@dataclass(frozen=True, slots=True)
class MergeView:
    """A recorded merge as listed: names, and whether it can be undone now."""

    record: FleetMergeRecord
    source_name: str | None
    target_name: str | None
    undoable: bool
    blocked: str | None


class FleetMergeService:
    """Merge a fleet into another and undo it (ADR 0107)."""

    def __init__(self, store: Store) -> None:
        self._store = store
        # Run after a committed merge or undo (the wiring adds the metrics and
        # activity cache drops); a failing hook never fails the call.
        self.on_change: list[Callable[[], None]] = []

    def _changed(self) -> None:
        for hook in self.on_change:
            try:
                hook()
            except Exception:  # a cache drop must not fail a merge that committed
                logger.warning("fleet merge change hook failed", exc_info=True)

    @staticmethod
    def _require_admin(credential: Credential) -> None:
        if not credential.is_admin:
            raise PermissionDenied("this action requires an admin key (ADR 0012)")

    @staticmethod
    def _check_target(target_fleet_id: str | None, new_fleet_name: str | None) -> None:
        if (target_fleet_id is None) == (new_fleet_name is None):
            raise InvalidInput("name exactly one of target_fleet_id and new_fleet_name")
        if new_fleet_name is not None:
            validate_fleet_name(new_fleet_name)  # ADR 0040

    async def _existing(self, fleet_id: str) -> Fleet:
        fleet = await self._store.get_fleet(fleet_id)
        if fleet is None:
            raise UnknownFleet(f"unknown fleet: {fleet_id}")
        if fleet.merged_into is not None:
            target = await self._store.get_fleet(fleet.merged_into)
            raise FleetMerged(fleet.id, fleet.merged_into, target.name if target else None)
        return fleet

    # -- preview ------------------------------------------------------------

    async def preview(
        self,
        source_id: str,
        credential: Credential,
        *,
        target_fleet_id: str | None = None,
        new_fleet_name: str | None = None,
    ) -> MergePreview:
        """What merging ``source_id`` would do, changing nothing: counts,
        the pins of both fleets (the newest ``MERGE_PIN_LIMIT`` ticked) and
        the likely duplicate pairs. Refuses what the merge would refuse."""
        self._require_admin(credential)
        self._check_target(target_fleet_id, new_fleet_name)
        source = await self._existing(source_id)
        target: Fleet | None = None
        if target_fleet_id is not None:
            target = await self._existing(target_fleet_id)
            if target.id == source.id:
                raise FleetMergeRefused("a fleet cannot be merged into itself")
        elif any(f.name == new_fleet_name for f in await self._store.list_fleets()):
            raise FleetExists(f"fleet already exists: {new_fleet_name!r}")

        source_facts = await self._store.fleet_facts(source.id)
        source_pins = await self._store.list_pins(source.id)
        if target is None:
            assert new_fleet_name is not None
            target_side = PreviewSide(new_fleet_name, None, FleetFacts(0, {}), 0)
            target_pins: list[Pin] = []
        else:
            target_pins = await self._store.list_pins(target.id)
            target_side = PreviewSide(
                target.name, target, await self._store.fleet_facts(target.id), len(target_pins)
            )

        all_pins = newest_first([*source_pins, *target_pins])
        ticked = {p.entry_id for p in default_kept(all_pins)}
        entries = await self._store.get_entries([p.entry_id for p in all_pins])
        pins = tuple(PreviewPin(p, entries.get(p.entry_id), p.entry_id in ticked) for p in all_pins)

        duplicates: tuple[PreviewDuplicate, ...] = ()
        looked_up, truncated = 0, False
        if target is not None:
            # The smaller fleet is the one looked up, so the cap bites less.
            mine = source_facts.entries.get("active", 0)
            theirs = target_side.facts.entries.get("active", 0)
            small, other = (source, target) if mine <= theirs else (target, source)
            scan = await self._store.duplicate_pairs(
                small.id, other.id, NEAR_DUPLICATE_SIMILARITY, DUPLICATE_LOOKUP_CAP
            )
            ids = [i for p in scan.pairs for i in (p.entry_id, p.match_id)]
            found = await self._store.get_entries(ids)
            duplicates = tuple(
                PreviewDuplicate(
                    found.get(p.entry_id),
                    found.get(p.match_id),
                    p.entry_id,
                    p.match_id,
                    p.similarity,
                )
                for p in scan.pairs
            )
            looked_up, truncated = scan.looked_up, scan.truncated
        return MergePreview(
            source=PreviewSide(source.name, source, source_facts, len(source_pins)),
            target=target_side,
            pins=pins,
            pin_limit=MERGE_PIN_LIMIT,
            needs_pin_choice=len(all_pins) > MERGE_PIN_LIMIT,
            duplicates=duplicates,
            duplicates_looked_up=looked_up,
            duplicates_truncated=truncated,
        )

    # -- merge --------------------------------------------------------------

    async def merge(
        self,
        source_id: str,
        credential: Credential,
        *,
        target_fleet_id: str | None = None,
        new_fleet_name: str | None = None,
        keep_pin_ids: Sequence[str] | None = None,
        expected_pin_ids: Sequence[str] | None = None,
    ) -> MergeOutcome:
        """Merge ``source_id`` into the target (admin-gated), then audit:
        ``fleet.create`` for a new target, one ``entry.unpin`` (reason
        ``fleet_merge``) per dropped pin and ``fleet.merge``. More than
        ``MERGE_PIN_LIMIT`` pins in the two fleets need ``keep_pin_ids``
        (``InvalidInput``). ``expected_pin_ids`` (the pins the preview showed)
        makes a merge refuse when a pin was added or removed since. A failed audit write is raised, never swallowed."""
        self._require_admin(credential)
        self._check_target(target_fleet_id, new_fleet_name)
        outcome = await self._store.merge_fleets(
            source_id,
            target_fleet_id=target_fleet_id,
            new_fleet_name=new_fleet_name,
            keep_pin_ids=keep_pin_ids,
            expected_pin_ids=expected_pin_ids,
            merged_by=credential.audit_actor(),
        )
        self._changed()
        record = outcome.record
        if outcome.target_created:
            await record_admin_action(
                self._store,
                credential,
                AuditAction.FLEET_CREATE,
                outcome.target.id,
                {"name": outcome.target.name, "via": "fleet_merge", "merge_id": record.id},
            )
        for pin in outcome.pins_dropped:
            await record_admin_action(
                self._store,
                credential,
                AuditAction.ENTRY_UNPIN,
                pin.entry_id,
                {
                    "fleet_id": pin.fleet_id,
                    "pin_ids": [pin.entry_id],
                    "reason": "fleet_merge",
                    "merge_id": record.id,
                },
            )
        await record_admin_action(
            self._store,
            credential,
            AuditAction.FLEET_MERGE,
            record.source_fleet_id,
            {
                "merge_id": record.id,
                "target_fleet_id": record.target_fleet_id,
                "target_fleet_name": outcome.target.name,
                "created_target": outcome.target_created,
                "entries": outcome.entries_moved,
                "agents": outcome.agents_moved,
                "pins_kept": len(outcome.pins_kept),
                "pins_dropped": len(outcome.pins_dropped),
            },
        )
        return outcome

    # -- undo and the list ---------------------------------------------------

    async def undo(self, merge_id: str, credential: Credential) -> UnmergeOutcome:
        """Undo a merge (admin-gated) and audit it as ``fleet.unmerge``.
        ``UnknownFleetMerge`` / ``FleetMergeRefused`` per the store."""
        self._require_admin(credential)
        outcome = await self._store.unmerge_fleet(merge_id)
        self._changed()
        record = outcome.record
        await record_admin_action(
            self._store,
            credential,
            AuditAction.FLEET_UNMERGE,
            record.source_fleet_id,
            {
                "merge_id": record.id,
                "target_fleet_id": record.target_fleet_id,
                "entries": outcome.entries_restored,
                "agents": len(outcome.agents_restored),
                "pins_returned": outcome.pins_returned,
                "pins_restored": len(outcome.pins_restored),
                "pins_skipped": [p.entry_id for p in outcome.pins_skipped],
            },
        )
        return outcome

    async def list_merges(self, credential: Credential) -> list[MergeView]:
        """Recorded merges, newest first, each with whether it can be
        undone now (``undo_refusal`` over the same list the store re-checks)."""
        self._require_admin(credential)
        records = await self._store.list_fleet_merges(MERGE_LIST_LIMIT)
        fleets = {f.id: f for f in await self._store.list_fleets()}
        views = []
        for record in records:
            blocked = undo_refusal(record, records, fleets.get(record.target_fleet_id))
            source, target = fleets.get(record.source_fleet_id), fleets.get(record.target_fleet_id)
            views.append(
                MergeView(
                    record,
                    source.name if source else None,
                    target.name if target else None,
                    blocked is None,
                    blocked,
                )
            )
        return views
