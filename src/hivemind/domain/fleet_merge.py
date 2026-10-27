"""Fleet merge: the records, the pin choice and the undo rule (ADR 0107).

Pure data and rules, no I/O. Both stores apply ``choose_pins`` and
``undo_refusal`` so they agree.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from hivemind.domain.access import Fleet
from hivemind.domain.pin import Pin
from hivemind.domain.validation import MAX_PINS_PER_AGENT, MAX_PINS_PER_FLEET, InvalidInput

# The pins a merged fleet keeps at most: the fleet cap (ADR 0058).
MERGE_PIN_LIMIT = MAX_PINS_PER_FLEET

# The most entries of the smaller fleet looked up for likely duplicates in a
# preview; past it the preview says so (``DuplicateScan.truncated``).
DUPLICATE_LOOKUP_CAP = 2_000

# ``pinned_by`` of a pin the admin key placed: exempt from the per-agent cap.
ADMIN_PINNER = "admin"


class FleetMergeRefused(Exception):
    """The merge or undo is not allowed in the fleets' current state
    (409 ``merge_refused``): same fleet on both sides, or an undo that
    would cross a newer merge."""


class UnknownFleetMerge(KeyError):
    """No merge has this id (404 ``not_found``). A ``KeyError``, so ``str(exc)`` is quoted."""


@dataclass(frozen=True, slots=True)
class DroppedPin:
    """A pin a merge took out of a briefing, kept so an undo can restore it.
    ``fleet_id`` is the fleet it belonged to before the merge."""

    fleet_id: str
    entry_id: str
    pinned_by: str
    pinned_at: datetime


@dataclass(frozen=True, slots=True)
class FleetMergeRecord:
    """One recorded merge: what moved from ``source_fleet_id`` into
    ``target_fleet_id`` (the entry ids live in ``fleet_merge_entries``)."""

    id: str
    source_fleet_id: str
    target_fleet_id: str
    merged_by: str
    merged_at: datetime
    moved_agents: tuple[str, ...] = ()
    dropped_pins: tuple[DroppedPin, ...] = ()
    undone_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FleetFacts:
    """What a fleet holds today: its agents and its entries by state."""

    agents: int
    entries: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class DuplicatePair:
    """An active entry of the smaller fleet and its nearest active entry in
    the other fleet, at or above the near-duplicate cut-off."""

    entry_id: str
    match_id: str
    similarity: float


@dataclass(frozen=True, slots=True)
class DuplicateScan:
    """The likely duplicate pairs between two fleets (highest similarity
    first). ``looked_up`` entries were checked; ``truncated`` when the
    smaller fleet had more active entries than the lookup cap."""

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


def newest_first(pins: Iterable[Pin]) -> list[Pin]:
    """Pins newest first, ties by entry id (the order ``list_pins`` uses)."""
    by_id = sorted(pins, key=lambda p: p.entry_id)
    return sorted(by_id, key=lambda p: p.pinned_at, reverse=True)


def default_kept(pins: Iterable[Pin]) -> list[Pin]:
    """The pins the preview ticks: the newest ``MERGE_PIN_LIMIT``."""
    return newest_first(pins)[:MERGE_PIN_LIMIT]


def check_pins_unchanged(pins: Iterable[Pin], expected_pin_ids: Sequence[str] | None) -> None:
    """``FleetMergeRefused`` when the two fleets' current ``pins`` are not
    exactly the ones the preview showed (``expected_pin_ids``): a pin added
    or removed since would otherwise be kept or dropped unseen. ``None``
    skips the check."""
    if expected_pin_ids is None:
        return
    if {p.entry_id for p in pins} != set(expected_pin_ids):
        raise FleetMergeRefused("pins changed since the preview, preview again")


def choose_pins(
    pins: Iterable[Pin], keep_pin_ids: Sequence[str] | None
) -> tuple[list[Pin], list[Pin]]:
    """Split the two fleets' ``pins`` into (kept, dropped), each newest first.

    With more than ``MERGE_PIN_LIMIT`` pins the admin must name which to
    keep. With fewer, no ``keep_pin_ids`` keeps all, and one can still drop
    some. ``InvalidInput`` for a pick over the limit or naming no pin."""
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
    """Which ``dropped`` pins an undo puts back into a fleet whose current
    pins were placed by ``held_by`` (one name per pin): the newest that fit
    under ``MERGE_PIN_LIMIT`` for the fleet and ``MAX_PINS_PER_AGENT`` per
    pinning agent (the admin is exempt, as when pinning); the others are
    skipped and reported, oldest first to go."""
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
) -> str | None:
    """Why ``record`` cannot be undone now, or ``None`` when it can.

    Only the newest standing merge involving the target (as target or as
    source) may be undone, and only while the target is not itself merged,
    so each moved entry has one unambiguous origin (ADR 0107)."""
    if record.undone_at is not None:
        return "this merge was already undone"
    fleets = {record.target_fleet_id}
    for other in records_newest_first:
        if other.id == record.id:
            break
        if other.undone_at is None and fleets & {other.source_fleet_id, other.target_fleet_id}:
            return f"merge {other.id} is newer and involves the same target fleet: undo it first"
    if target is not None and target.merged_into is not None:
        return "the target fleet has since been merged into another fleet: undo that merge first"
    return None
