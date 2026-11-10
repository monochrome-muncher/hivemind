"""Fleet merge, move and unmerge schemas, previews and records."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from hivemind.api.schemas.common import RefStr
from hivemind.api.schemas.fleets import FleetOut
from hivemind.domain.entry import EntryState, Kind
from hivemind.domain.fleet_merge import DroppedPin, MergeOutcome, MoveOutcome, UnmergeOutcome
from hivemind.services.fleet_merge import MergePreview, MergeView, MovePreview


class FleetMergeRequest(BaseModel):
    """``POST /v1/admin/fleets/{source_id}/merge``: name exactly one of
    ``target_fleet_id`` and ``new_fleet_name``. ``dry_run`` is required, so a
    merge is never started by leaving it out."""

    target_fleet_id: RefStr | None = None
    new_fleet_name: str | None = None
    keep_pin_ids: list[RefStr] | None = Field(default=None, max_length=50)
    # Every pin the preview showed: the merge is refused (409) when the pins
    # of the two fleets are not exactly these, so none is kept or dropped unseen.
    expected_pin_ids: list[RefStr] | None = Field(default=None, max_length=200)
    dry_run: bool


class MergeSideOut(BaseModel):
    """One fleet of a previewed merge; ``id`` is ``None`` for a new target."""

    id: str | None
    name: str
    new: bool
    agents: int
    entries: dict[str, int]
    pins: int


class MergePinOut(BaseModel):
    """A pin of either fleet; ``default_kept`` marks the newest ten."""

    entry_id: str
    fleet_id: str
    pinned_by: str
    pinned_at: datetime
    kind: Kind | None
    summary: str | None
    state: EntryState | None
    default_kept: bool


class MergeDuplicateOut(BaseModel):
    """A likely duplicate pair: an active entry of the smaller fleet and its
    nearest active entry in the other fleet."""

    entry_id: str
    entry_summary: str | None
    match_id: str
    match_summary: str | None
    similarity: float


class FleetMergePreviewOut(BaseModel):
    """The ``dry_run`` answer: nothing was changed."""

    dry_run: Literal[True] = True
    source: MergeSideOut
    target: MergeSideOut
    pins: list[MergePinOut]
    pin_limit: int
    needs_pin_choice: bool
    duplicates: list[MergeDuplicateOut]
    duplicates_looked_up: int
    duplicates_truncated: bool

    @classmethod
    def from_preview(cls, preview: MergePreview) -> FleetMergePreviewOut:
        def side(item: Any, new: bool) -> MergeSideOut:
            return MergeSideOut(
                id=item.fleet.id if item.fleet is not None else None,
                name=item.name,
                new=new,
                agents=item.facts.agents,
                entries=dict(item.facts.entries),
                pins=item.pins,
            )

        return cls(
            source=side(preview.source, False),
            target=side(preview.target, preview.target.fleet is None),
            pins=[
                MergePinOut(
                    entry_id=p.pin.entry_id,
                    fleet_id=p.pin.fleet_id,
                    pinned_by=p.pin.pinned_by,
                    pinned_at=p.pin.pinned_at,
                    kind=p.entry.kind if p.entry is not None else None,
                    summary=p.entry.summary if p.entry is not None else None,
                    state=p.entry.state if p.entry is not None else None,
                    default_kept=p.default_kept,
                )
                for p in preview.pins
            ],
            pin_limit=preview.pin_limit,
            needs_pin_choice=preview.needs_pin_choice,
            duplicates=[
                MergeDuplicateOut(
                    entry_id=d.entry_id,
                    entry_summary=d.entry.summary if d.entry is not None else None,
                    match_id=d.match_id,
                    match_summary=d.match.summary if d.match is not None else None,
                    similarity=round(d.similarity, 4),
                )
                for d in preview.duplicates
            ],
            duplicates_looked_up=preview.duplicates_looked_up,
            duplicates_truncated=preview.duplicates_truncated,
        )


class FleetMoveRequest(BaseModel):
    """``POST /v1/admin/fleets/{source_id}/move`` (ADR 0112): the agents to
    move (each must have the source as home fleet) and exactly one of
    ``target_fleet_id`` and ``new_fleet_name``. ``dry_run`` is required, so a
    move is never started by leaving it out. The pin fields work as in a merge."""

    agents: list[RefStr] = Field(min_length=1, max_length=200)
    target_fleet_id: RefStr | None = None
    new_fleet_name: str | None = None
    keep_pin_ids: list[RefStr] | None = Field(default=None, max_length=50)
    expected_pin_ids: list[RefStr] | None = Field(default=None, max_length=200)
    dry_run: bool


class FleetMovePreviewOut(BaseModel):
    """The move ``dry_run`` answer: nothing was changed. ``leaving`` memories
    go with the agents; the ``staying_agents`` lose access to them."""

    dry_run: Literal[True] = True
    source: MergeSideOut
    target: MergeSideOut
    agents: list[str]
    staying_agents: list[str]
    leaving: int
    leaving_by_state: dict[str, int]
    held_back: int
    pins: list[MergePinOut]
    pin_limit: int
    needs_pin_choice: bool

    @classmethod
    def from_preview(cls, preview: MovePreview) -> FleetMovePreviewOut:
        shared = FleetMergePreviewOut.from_preview(
            MergePreview(
                source=preview.source,
                target=preview.target,
                pins=preview.pins,
                pin_limit=preview.pin_limit,
                needs_pin_choice=preview.needs_pin_choice,
                duplicates=(),
                duplicates_looked_up=0,
                duplicates_truncated=False,
            )
        )
        return cls(
            source=shared.source,
            target=shared.target,
            agents=list(preview.agents),
            staying_agents=list(preview.staying_agents),
            leaving=preview.leaving,
            leaving_by_state=dict(preview.leaving_by_state),
            held_back=preview.held_back,
            pins=shared.pins,
            pin_limit=preview.pin_limit,
            needs_pin_choice=preview.needs_pin_choice,
        )


class DroppedPinOut(BaseModel):
    """A pin a merge unpinned (or an undo could not restore)."""

    fleet_id: str
    entry_id: str
    pinned_by: str
    pinned_at: datetime

    @classmethod
    def from_dropped(cls, pin: DroppedPin) -> DroppedPinOut:
        return cls(
            fleet_id=pin.fleet_id,
            entry_id=pin.entry_id,
            pinned_by=pin.pinned_by,
            pinned_at=pin.pinned_at,
        )


class FleetMergeOut(BaseModel):
    """A merge that happened."""

    dry_run: Literal[False] = False
    merge_id: str
    source_fleet_id: str
    target: FleetOut
    target_created: bool
    entries_moved: int
    agents_moved: list[str]
    pins_kept: int
    pins_dropped: list[DroppedPinOut]

    @classmethod
    def from_outcome(cls, outcome: MergeOutcome) -> FleetMergeOut:
        record = outcome.record
        return cls(
            merge_id=record.id,
            source_fleet_id=record.source_fleet_id,
            target=FleetOut.from_fleet(outcome.target),
            target_created=outcome.target_created,
            entries_moved=outcome.entries_moved,
            agents_moved=list(record.moved_agents),
            pins_kept=len(outcome.pins_kept),
            pins_dropped=[DroppedPinOut.from_dropped(p) for p in outcome.pins_dropped],
        )


class FleetMoveOut(BaseModel):
    """A move that happened. ``merge_id`` is the history id (the undo takes
    it); the source fleet stays a normal fleet."""

    dry_run: Literal[False] = False
    merge_id: str
    source_fleet_id: str
    target: FleetOut
    target_created: bool
    entries_moved: int
    entries_held_back: int
    agents_moved: list[str]
    pins_kept: int
    pins_dropped: list[DroppedPinOut]

    @classmethod
    def from_outcome(cls, outcome: MoveOutcome) -> FleetMoveOut:
        record = outcome.record
        return cls(
            merge_id=record.id,
            source_fleet_id=record.source_fleet_id,
            target=FleetOut.from_fleet(outcome.target),
            target_created=outcome.target_created,
            entries_moved=outcome.entries_moved,
            entries_held_back=outcome.entries_held_back,
            agents_moved=list(record.moved_agents),
            pins_kept=len(outcome.pins_kept),
            pins_dropped=[DroppedPinOut.from_dropped(p) for p in outcome.pins_dropped],
        )


class FleetUnmergeOut(BaseModel):
    """An undone merge or move. Search counters stay summed."""

    kind: str = "merge"
    merge_id: str
    source_fleet_id: str
    target_fleet_id: str
    entries_restored: int
    agents_restored: list[str]
    pins_returned: int
    pins_restored: list[DroppedPinOut]
    pins_skipped: list[DroppedPinOut]

    @classmethod
    def from_outcome(cls, outcome: UnmergeOutcome) -> FleetUnmergeOut:
        record = outcome.record
        return cls(
            kind=record.kind,
            merge_id=record.id,
            source_fleet_id=record.source_fleet_id,
            target_fleet_id=record.target_fleet_id,
            entries_restored=outcome.entries_restored,
            agents_restored=list(outcome.agents_restored),
            pins_returned=outcome.pins_returned,
            pins_restored=[DroppedPinOut.from_dropped(p) for p in outcome.pins_restored],
            pins_skipped=[DroppedPinOut.from_dropped(p) for p in outcome.pins_skipped],
        )


class FleetMergeRecordOut(BaseModel):
    """One recorded merge or move; ``undoable`` says whether an undo is allowed now
    (``undo_blocked`` says why not)."""

    id: str
    kind: str  # "merge" or "move" (ADR 0112)
    source_fleet_id: str
    source_fleet_name: str | None
    target_fleet_id: str
    target_fleet_name: str | None
    merged_by: str
    merged_at: str
    moved_agents: list[str]
    dropped_pins: int
    undone_at: str | None
    undoable: bool
    undo_blocked: str | None

    @classmethod
    def from_view(cls, view: MergeView) -> FleetMergeRecordOut:
        r = view.record
        return cls(
            id=r.id,
            kind=r.kind,
            source_fleet_id=r.source_fleet_id,
            source_fleet_name=view.source_name,
            target_fleet_id=r.target_fleet_id,
            target_fleet_name=view.target_name,
            merged_by=r.merged_by,
            merged_at=r.merged_at.isoformat(),
            moved_agents=list(r.moved_agents),
            dropped_pins=len(r.dropped_pins),
            undone_at=r.undone_at.isoformat() if r.undone_at is not None else None,
            undoable=view.undoable,
            undo_blocked=view.blocked,
        )
