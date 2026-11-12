"""Moving a fleet memory to `self` (ADR 0145): what a move changed, and when one is refused. Both
stores walk the same versions and answer with the same record."""

from __future__ import annotations

from dataclasses import dataclass

from hivemind.domain.entry import Entry


class SelfMoveRefused(Exception):
    """The memory cannot be moved to `self` (409): it is not an active fleet memory, or it (or an
    earlier version of it by its author) replaced another agent's fleet memory."""


@dataclass(frozen=True, slots=True, order=True)
class RemovedPin:
    fleet_id: str
    entry_id: str


@dataclass(frozen=True, slots=True)
class SelfMove:
    """A finished move. ``entry`` is the memory, now `self`. ``earlier_versions`` are the
    versions by the same author it replaced, moved with it. ``unpinned`` are the pins removed from
    every moved version."""

    entry: Entry
    earlier_versions: tuple[str, ...] = ()
    unpinned: tuple[RemovedPin, ...] = ()


def self_move_refusal(entry_id: str, state: str, scope: str) -> SelfMoveRefused | None:
    if state != "active":
        return SelfMoveRefused(
            f"entry {entry_id} is {state}: only an active memory can be moved to self"
        )
    if scope != "fleet":
        return SelfMoveRefused(
            f"entry {entry_id} is {scope}-scoped: only a fleet memory can be moved to self"
        )
    return None


def replaced_others_refusal(entry_id: str, others: list[str]) -> SelfMoveRefused | None:
    """Moving would hide the line of another agent's fleet memory behind a private note."""
    if not others:
        return None
    return SelfMoveRefused(
        f"entry {entry_id} replaced a fleet memory by another agent ({', '.join(sorted(others))}): "
        "it cannot be moved to self; withdraw it instead"
    )


__all__ = [
    "RemovedPin",
    "SelfMove",
    "SelfMoveRefused",
    "replaced_others_refusal",
    "self_move_refusal",
]
