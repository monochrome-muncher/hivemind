"""Fleet merge and move (ADRs 0107, 0112): records, pin choice and the undo rule, shared by both
stores. A move is recorded beside merges (``FleetMergeRecord.kind``), so one undo rule covers
both."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from hivemind.domain.access import SCOPE_FLEET, Fleet
from hivemind.domain.entry import Entry
from hivemind.domain.pin import Pin
from hivemind.domain.validation import MAX_PINS_PER_AGENT, MAX_PINS_PER_FLEET, InvalidInput

# Pins a merged fleet keeps at most: the fleet cap (ADR 0058).
MERGE_PIN_LIMIT = MAX_PINS_PER_FLEET

# Entries of the smaller fleet looked up for duplicates; past it ``DuplicateScan.truncated``.
DUPLICATE_LOOKUP_CAP = 2_000

# Up to this many vectors in the fleet looked *into*, the preview uses an exact scan of it, not the
# filtered HNSW index: without a `fleet_id` index (ADR 0126) the filtered walk ran to
# `hnsw.max_scan_tuples` (1,156 ms for two 20-entry fleets at 200k entries). Past it the exact scan
# costs more.
EXACT_DUPLICATE_LOOKUP_MAX = 5_000

# ``pinned_by`` of a pin the admin key placed: exempt from the per-agent cap.
ADMIN_PINNER = "admin"


class FleetMergeRefused(Exception):
    """The merge or undo is not allowed now (409 ``merge_refused``): same fleet on both sides,
    or an undo that would cross a newer merge."""


class FleetMoveRefused(FleetMergeRefused):
    """The move or its undo is not allowed now (409 ``move_refused``): an agent outside the source
    fleet, the same fleet twice, changes since the preview, or an undo crossing a newer one."""


class UnknownFleetMerge(KeyError):
    """No merge has this id (404 ``not_found``). A ``KeyError``, so ``str(exc)`` is quoted."""


# ``FleetMergeRecord.kind``
KIND_MERGE = "merge"
KIND_MOVE = "move"


@dataclass(frozen=True, slots=True)
class DroppedPin:
    """A pin a merge dropped, kept so an undo can restore it into its former ``fleet_id``."""

    fleet_id: str
    entry_id: str
    pinned_by: str
    pinned_at: datetime


@dataclass(frozen=True, slots=True)
class FleetMergeRecord:
    """One recorded merge or move into ``target_fleet_id`` (entry ids live in
    ``fleet_merge_entries``)."""

    id: str
    source_fleet_id: str
    target_fleet_id: str
    merged_by: str
    merged_at: datetime
    moved_agents: tuple[str, ...] = ()
    dropped_pins: tuple[DroppedPin, ...] = ()
    undone_at: datetime | None = None
    # ``move`` takes some agents and their fleet memories; the source stays a fleet.
    kind: str = KIND_MERGE


@dataclass(frozen=True, slots=True)
class FleetFacts:
    """What a fleet holds today: its agents and its entries by state."""

    agents: int
    entries: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class DuplicatePair:
    """An active entry and its nearest in the other fleet, at or above the near-duplicate
    cut-off."""

    entry_id: str
    match_id: str
    similarity: float


@dataclass(frozen=True, slots=True)
class DuplicateScan:
    """Likely duplicate pairs between two fleets, most similar first."""

    pairs: tuple[DuplicatePair, ...] = ()
    looked_up: int = 0
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class MergeOutcome:
    """What a merge did."""

    record: FleetMergeRecord
    target: Fleet
    target_created: bool
    entries_moved: int
    agents_moved: int
    pins_kept: tuple[Pin, ...]
    pins_dropped: tuple[DroppedPin, ...]


@dataclass(frozen=True, slots=True)
class UnmergeOutcome:
    """What an undo did. Counters stay summed (ADR 0107)."""

    record: FleetMergeRecord
    entries_restored: int
    agents_restored: tuple[str, ...]
    pins_returned: int
    pins_restored: tuple[DroppedPin, ...]
    pins_skipped: tuple[DroppedPin, ...]
    # A move's undo only: pins on a returning memory that overflowed the source's cap and were
    # removed from the target. Also in ``pins_skipped``.
    pins_unpinned: tuple[DroppedPin, ...] = ()


@dataclass(frozen=True, slots=True)
class MovePlan:
    """The memories a move would re-file (``states`` counts them) and how many stay behind
    because their chain's head does (``held_back``)."""

    entry_ids: tuple[str, ...] = ()
    states: Mapping[str, int] | None = None
    held_back: int = 0


@dataclass(frozen=True, slots=True)
class MoveOutcome:
    """What a move did."""

    record: FleetMergeRecord
    target: Fleet
    target_created: bool
    entries_moved: int
    entries_held_back: int
    agents_moved: int
    pins_kept: tuple[Pin, ...]
    pins_dropped: tuple[DroppedPin, ...]


def plan_move_entries(
    entries: Iterable[Entry], source_fleet_id: str, movers: Iterable[str]
) -> tuple[list[Entry], int]:
    """Which entries a move re-files and how many stay (ADR 0112). A ``fleet``-scoped source-fleet
    entry by a mover moves only if the head of its ``superseded_by`` chain is one too, so an old
    version never leaves while the live one stays. ``entries`` must hold every chain entry.
    Returns (moved, held back)."""
    names = set(movers)
    by_id = {e.id: e for e in entries}
    candidates = {
        e.id
        for e in by_id.values()
        if e.scope == SCOPE_FLEET and e.fleet_id == source_fleet_id and e.author in names
    }
    moved: list[Entry] = []
    held = 0
    for entry_id in candidates:
        seen = {entry_id}
        head = entry_id
        while (nxt := by_id[head].superseded_by) is not None and nxt in by_id and nxt not in seen:
            head = nxt
            seen.add(head)
        if head in candidates and by_id[head].superseded_by is None:
            moved.append(by_id[entry_id])
        else:
            held += 1
    return moved, held


def newest_first(pins: Iterable[Pin]) -> list[Pin]:
    """Pins newest first, ties by entry id (the order ``list_pins`` uses)."""
    by_id = sorted(pins, key=lambda p: p.entry_id)
    return sorted(by_id, key=lambda p: p.pinned_at, reverse=True)


def default_kept(pins: Iterable[Pin]) -> list[Pin]:
    """The pins the preview ticks: the newest ``MERGE_PIN_LIMIT``."""
    return newest_first(pins)[:MERGE_PIN_LIMIT]


def check_pins_unchanged(
    pins: Iterable[Pin],
    expected_pin_ids: Sequence[str] | None,
    refusal: type[FleetMergeRefused] = FleetMergeRefused,
) -> None:
    """``refusal`` (``FleetMoveRefused`` for a move) when the current ``pins`` differ from the
    preview's ``expected_pin_ids``, so none is kept or dropped unseen. ``None`` skips the check."""
    if expected_pin_ids is None:
        return
    if {p.entry_id for p in pins} != set(expected_pin_ids):
        raise refusal("pins changed since the preview, preview again")


def choose_pins(
    pins: Iterable[Pin], keep_pin_ids: Sequence[str] | None
) -> tuple[list[Pin], list[Pin]]:
    """Split ``pins`` into (kept, dropped), newest first. Over ``MERGE_PIN_LIMIT`` the admin must
    name the keepers; otherwise no ``keep_pin_ids`` keeps all. ``InvalidInput`` for a pick over the
    limit or naming no pin."""
    ordered = newest_first(pins)
    if keep_pin_ids is None:
        if len(ordered) > MERGE_PIN_LIMIT:
            raise InvalidInput(
                f"the two fleets hold {len(ordered)} pins but a fleet keeps at most "
                f"{MERGE_PIN_LIMIT}: choose them with keep_pin_ids"
            )
        return ordered, []
    wanted = list(dict.fromkeys(keep_pin_ids))
    if len(wanted) > MERGE_PIN_LIMIT:
        raise InvalidInput(f"keep_pin_ids names {len(wanted)} pins; at most {MERGE_PIN_LIMIT}")
    known = {p.entry_id for p in ordered}
    unknown = [w for w in wanted if w not in known]
    if unknown:
        raise InvalidInput("keep_pin_ids names entries that are not pinned: " + ", ".join(unknown))
    keep = set(wanted)
    return (
        [p for p in ordered if p.entry_id in keep],
        [p for p in ordered if p.entry_id not in keep],
    )


def plan_pin_restore(
    dropped: Iterable[DroppedPin], held_by: Sequence[str]
) -> tuple[list[DroppedPin], list[DroppedPin]]:
    """Which ``dropped`` pins an undo restores into a fleet whose pins were placed by ``held_by``:
    the newest fitting ``MERGE_PIN_LIMIT`` and ``MAX_PINS_PER_AGENT`` (the admin is exempt);
    the rest are skipped and reported."""
    by_id = sorted(dropped, key=lambda p: p.entry_id)
    ordered = sorted(by_id, key=lambda p: p.pinned_at, reverse=True)
    per_agent: dict[str, int] = {}
    for name in held_by:
        per_agent[name] = per_agent.get(name, 0) + 1
    room = max(MERGE_PIN_LIMIT - len(held_by), 0)
    put: list[DroppedPin] = []
    over: list[DroppedPin] = []
    for d in ordered:
        agent_full = (
            d.pinned_by != ADMIN_PINNER and per_agent.get(d.pinned_by, 0) >= MAX_PINS_PER_AGENT
        )
        if len(put) >= room or agent_full:
            over.append(d)
            continue
        put.append(d)
        per_agent[d.pinned_by] = per_agent.get(d.pinned_by, 0) + 1
    return put, over


def undo_refusal(
    record: FleetMergeRecord,
    records_newest_first: Sequence[FleetMergeRecord],
    target: Fleet | None,
    source: Fleet | None = None,
) -> str | None:
    """Why ``record`` cannot be undone now, or ``None``. Only the newest standing merge or move
    involving the target may be undone, while the target is not itself merged, so each moved
    entry has one origin (ADR 0107). A move's source is held to the same rule (ADR 0112)."""
    what = "move" if record.kind == KIND_MOVE else "merge"
    if record.undone_at is not None:
        return f"this {what} was already undone"
    fleets = {record.target_fleet_id}
    if record.kind == KIND_MOVE:
        fleets.add(record.source_fleet_id)
    for other in records_newest_first:
        if other.id == record.id:
            break
        if other.undone_at is None and fleets & {other.source_fleet_id, other.target_fleet_id}:
            kind = "move" if other.kind == KIND_MOVE else "merge"
            return f"{kind} {other.id} is newer and involves the same fleet: undo it first"
    for fleet in (target, source if record.kind == KIND_MOVE else None):
        if fleet is not None and fleet.merged_into is not None:
            return (
                f"the fleet {fleet.name!r} has since been merged into another fleet: "
                "undo that merge first"
            )
    return None
