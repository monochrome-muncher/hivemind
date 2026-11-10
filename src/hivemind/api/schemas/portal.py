"""Schemas only the agent portal uses: its search, withdraw and history."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from hivemind.api.schemas.entries import AdminSearchRequest
from hivemind.domain.history import HistoryItem


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
