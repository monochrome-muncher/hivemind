"""The ``upstream_changed`` marker on a search hit (SPEC §6.1, ADR 0081): an insight or decision
whose ``see_also`` target was withdrawn, superseded by someone else or flagged. Display only, never
ranks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from hivemind.domain.entry import EntryState, Kind
from hivemind.domain.feedback import last_confirmed

# Changed targets a hit lists (SPEC §6.1).
MAX_UPSTREAM_CHANGED = 3

# Only these kinds are built on what they link to; a fact is observed.
UPSTREAM_KINDS = frozenset({Kind.INSIGHT, Kind.DECISION})


class UpstreamChangeKind(StrEnum):
    WITHDRAWN = "withdrawn"
    SUPERSEDED = "superseded"
    FLAGGED = "flagged"


@dataclass(frozen=True, slots=True)
class UpstreamLink:
    """One outgoing ``see_also`` link and its readable target, in any state.
    ``superseded_at``/``superseded_author`` describe the successor. ``newest_flag_at`` and
    ``newest_noted_helpful_at`` (revoked reporters left out) are the two dates "last confirmed"
    is made of (SPEC §4.2)."""

    from_id: str
    to_id: str
    linked_at: datetime
    state: EntryState
    superseded_at: datetime | None = None
    superseded_author: str | None = None
    newest_flag_at: datetime | None = None
    newest_noted_helpful_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class UpstreamChange:
    """One changed target of a hit: its id and what happened to it."""

    id: str
    change: UpstreamChangeKind


def _change_of(
    link: UpstreamLink, author: str
) -> tuple[UpstreamChangeKind, datetime | None] | None:
    """The change on the target since the link was made, with its date (``None`` for a
    withdrawal)."""
    if link.state is EntryState.WITHDRAWN:
        # Withdrawals are undated here, so a withdrawn target always counts.
        return UpstreamChangeKind.WITHDRAWN, None
    if (
        link.state is EntryState.SUPERSEDED
        and link.superseded_at is not None
        and link.superseded_at > link.linked_at
        and link.superseded_author != author
    ):
        return UpstreamChangeKind.SUPERSEDED, link.superseded_at
    if (
        link.newest_flag_at is not None
        and link.newest_flag_at > link.linked_at
        and last_confirmed(link.newest_noted_helpful_at, link.newest_flag_at) is None
    ):
        return UpstreamChangeKind.FLAGGED, link.newest_flag_at
    return None


def upstream_changed(
    kind: Kind, author: str, links: list[UpstreamLink]
) -> tuple[UpstreamChange, ...]:
    """The changed targets of an entry (ADR 0081): at most ``MAX_UPSTREAM_CHANGED``, withdrawn
    first, then newest first, ties by id. Empty unless insight or decision."""
    if kind not in UPSTREAM_KINDS:
        return ()
    found: list[tuple[UpstreamChange, datetime | None]] = []
    for link in links:
        changed = _change_of(link, author)
        if changed is not None:
            found.append((UpstreamChange(link.to_id, changed[0]), changed[1]))
    found.sort(key=lambda f: f[0].id)
    found.sort(key=lambda f: f[1].timestamp() if f[1] is not None else float("inf"), reverse=True)
    return tuple(change for change, _ in found[:MAX_UPSTREAM_CHANGED])
