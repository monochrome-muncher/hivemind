"""Schemas only the agent portal uses: its search, withdraw, history and relied-on page."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from hivemind.api.schemas.entries import AdminSearchRequest, EntryOut
from hivemind.domain.history import HistoryItem
from hivemind.services.read import ReliedOnItem


class PortalHistoryItemOut(BaseModel):
    """One line of the agent's history in the portal (ADR 0123). ``by`` is
    ``agent`` (the agent or one it inherits from, named in ``agent``), ``another_agent`` (an agent the key does not own, never named) or ``admin`` (never identified). ``agent`` names
    which of the agent and its predecessors acted; ``about`` the one the row
    concerns. ``entry_id`` is set only when the key can still read the entry."""

    id: str
    occurred_at: datetime
    action: str
    by: Literal["agent", "another_agent", "admin"]
    agent: str | None
    about: str | None
    summary: str
    reason: str | None
    entry_id: str | None

    @classmethod
    def from_item(cls, item: HistoryItem) -> PortalHistoryItemOut:
        return cls(
            id=item.id,
            occurred_at=item.occurred_at,
            action=item.action,
            by=item.by,
            agent=item.agent,
            about=item.about,
            summary=item.summary,
            reason=item.reason,
            entry_id=item.entry_id,
        )


class PortalSearchRequest(AdminSearchRequest):
    """``POST /v1/portal/search`` (ADR 0095): the same search and fleet
    filter, over what the agent's key can read."""


class PortalWithdrawRequest(BaseModel):
    """A withdrawal from the agent portal (ADR 0095): the reason is required."""

    reason: str


class FlagsSinceOut(BaseModel):
    stale: int
    wrong: int


class ReliedOnChangeOut(BaseModel):
    """What changed on a memory since the agent rated it helpful (ADR 0138). ``superseded_by`` is
    set only when the key can read the successor; ``withdrawn_at`` is null for a withdrawal the
    store has no date for. ``flagged_since`` counts the stale and wrong reports others filed
    after the rating, or is null when there are none. A row with nothing true and no
    ``rated_helpful_at`` is one whose change could not be found: it is still listed."""

    rated_helpful_at: datetime | None
    superseded: bool
    superseded_by: str | None
    withdrawn: bool
    withdrawn_at: datetime | None
    flagged_since: FlagsSinceOut | None


class PortalReliedOnOut(EntryOut):
    """A listed entry (with ``feedback`` counts) and what changed on it (ADR 0138)."""

    change: ReliedOnChangeOut

    @classmethod
    def from_item(cls, item: ReliedOnItem, row: EntryOut) -> PortalReliedOnOut:
        """``row`` is the entry's serialized list row; ``item`` says what changed on it."""
        entry, change = item.entry, item.change
        flags = FlagsSinceOut(stale=change.stale, wrong=change.wrong)
        return cls(
            **row.model_dump(),
            change=ReliedOnChangeOut(
                rated_helpful_at=change.rated_helpful_at,
                superseded=change.superseded,
                superseded_by=entry.superseded_by if change.superseded else None,
                withdrawn=change.withdrawn,
                withdrawn_at=entry.withdrawn_at if change.withdrawn else None,
                flagged_since=flags if change.stale + change.wrong else None,
            ),
        )
