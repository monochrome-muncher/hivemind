"""Agent cleanup schemas for the admin and the portal."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from hivemind.api.schemas.common import RefStr, to_utc
from hivemind.api.schemas.fleet_merge import DroppedPinOut
from hivemind.domain.agent_cleanup import CleanupOutcome, CleanupUndoOutcome
from hivemind.domain.entry import Entry, EntryState, Kind
from hivemind.services.agent_cleanup import CleanupPreview, CleanupView


class AgentCleanupRequest(BaseModel):
    """``POST /v1/admin/agents/{name}/cleanup``. ``since`` is required;
    ``until`` defaults to now. The window is on a memory's ``created_at``
    (write time) and a rating's ``updated_at``, ``since`` inclusive and
    ``until`` exclusive. ``dry_run`` is required, so a cleanup is never
    started by leaving it out. ``restore_ids`` are the superseded memories to
    put back (none when omitted); the ``expected_*`` counts are what the
    preview showed, and the apply is refused (409) when they changed."""

    since: datetime
    until: datetime | None = None
    dry_run: bool
    restore_ids: list[RefStr] | None = Field(default=None, max_length=1000)
    expected_withdraw: int | None = Field(default=None, ge=0)
    expected_ratings: int | None = Field(default=None, ge=0)
    also_revoke: bool = False

    @field_validator("since", "until")
    @classmethod
    def _window_utc(cls, v: datetime | None) -> datetime | None:
        """A naive bound is assumed UTC, as for ``occurred_at``."""
        return to_utc(v)


class CleanupEntryOut(BaseModel):
    """A memory a cleanup lists: one to withdraw, or one to restore (then
    ``superseded_by`` is the junk memory that replaced it, which may be one
    link of a chain ending at a memory the cleanup withdraws)."""

    id: str
    kind: Kind
    summary: str
    author: str
    scope: str
    fleet_id: str | None
    created_at: datetime
    state: EntryState
    superseded_by: str | None

    @classmethod
    def from_entry(cls, entry: Entry) -> CleanupEntryOut:
        return cls(
            id=entry.id,
            kind=entry.kind,
            summary=entry.summary,
            author=entry.author,
            scope=entry.scope,
            fleet_id=entry.fleet_id,
            created_at=entry.created_at,
            state=entry.state,
            superseded_by=entry.superseded_by,
        )


class AgentCleanupPreviewOut(BaseModel):
    """The ``dry_run`` answer: nothing was changed. The lists are cut (newest
    first) and say so; the totals are exact. ``until`` is the window's end as
    resolved, to send back with the apply."""

    dry_run: Literal[True] = True
    agent: str
    agent_status: str
    since: datetime
    until: datetime
    withdraw: list[CleanupEntryOut]
    withdraw_total: int
    withdraw_truncated: bool
    ratings_total: int
    ratings_by_verdict: dict[str, int]
    pins_total: int
    restorable: list[CleanupEntryOut]
    restorable_total: int
    restorable_truncated: bool
    nothing_to_clean: bool

    @classmethod
    def from_preview(cls, preview: CleanupPreview) -> AgentCleanupPreviewOut:
        scan = preview.scan
        return cls(
            agent=preview.agent.name,
            agent_status=preview.agent.status.value,
            since=preview.since,
            until=preview.until,
            withdraw=[CleanupEntryOut.from_entry(e) for e in scan.withdraw],
            withdraw_total=scan.withdraw_total,
            withdraw_truncated=scan.withdraw_total > len(scan.withdraw),
            ratings_total=scan.ratings_total,
            ratings_by_verdict=dict(scan.ratings_by_verdict),
            pins_total=scan.pins_total,
            restorable=[CleanupEntryOut.from_entry(e) for e in scan.restorable],
            restorable_total=scan.restorable_total,
            restorable_truncated=scan.restorable_total > len(scan.restorable),
            nothing_to_clean=scan.withdraw_total == 0 and scan.ratings_total == 0,
        )


class AgentCleanupOut(BaseModel):
    """A cleanup that happened. ``cleanup_id`` is ``None`` (and
    ``nothing_to_clean`` true) when there was nothing to change: no record was
    kept. ``revoked`` is true when ``also_revoke`` revoked the agent."""

    dry_run: Literal[False] = False
    cleanup_id: str | None
    agent: str
    since: datetime
    until: datetime
    withdrawn: int
    ratings_set_aside: int
    restored: int
    pins_removed: list[DroppedPinOut]
    revoked: bool
    already_revoked: bool
    revoke_error: str | None = None
    nothing_to_clean: bool

    @classmethod
    def from_outcome(
        cls, outcome: CleanupOutcome, *, agent: str, since: datetime, until: datetime
    ) -> AgentCleanupOut:
        return cls(
            cleanup_id=outcome.record.id if outcome.record is not None else None,
            agent=agent,
            since=since,
            until=until,
            withdrawn=outcome.withdrawn,
            ratings_set_aside=outcome.ratings_set_aside,
            restored=outcome.restored,
            pins_removed=[DroppedPinOut.from_dropped(p) for p in outcome.unpinned],
            revoked=outcome.revoked,
            already_revoked=outcome.already_revoked,
            revoke_error=outcome.revoke_error,
            nothing_to_clean=outcome.record is None,
        )


class AgentCleanupUndoOut(BaseModel):
    """An undone cleanup. It never un-revokes the agent; ``note`` says so when
    the cleanup had revoked it."""

    cleanup_id: str
    agent: str
    reactivated: int
    reactivate_skipped: int
    resuperseded: int
    resupersede_skipped: int
    ratings_cleared: int
    pins_restored: list[DroppedPinOut]
    pins_skipped: list[DroppedPinOut]
    note: str | None

    @classmethod
    def from_outcome(cls, outcome: CleanupUndoOutcome, *, note: str | None) -> AgentCleanupUndoOut:
        return cls(
            cleanup_id=outcome.record.id,
            agent=outcome.record.agent,
            reactivated=outcome.reactivated,
            reactivate_skipped=outcome.reactivate_skipped,
            resuperseded=outcome.resuperseded,
            resupersede_skipped=outcome.resupersede_skipped,
            ratings_cleared=outcome.ratings_cleared,
            pins_restored=[DroppedPinOut.from_dropped(p) for p in outcome.pins_restored],
            pins_skipped=[DroppedPinOut.from_dropped(p) for p in outcome.pins_skipped],
            note=note,
        )


class AgentCleanupRecordOut(BaseModel):
    """One recorded cleanup; ``undoable`` says whether an undo is allowed now
    (``undo_blocked`` says why not)."""

    id: str
    agent: str
    since: datetime
    until: datetime
    cleaned_by: str
    cleaned_at: datetime
    withdrawn: int
    ratings_set_aside: int
    restored: int
    pins_removed: int
    revoked: bool
    undone_at: datetime | None
    undoable: bool
    undo_blocked: str | None
    self_service: bool = False  # the agent ran it from the portal (ADR 0118)

    @classmethod
    def from_view(cls, view: CleanupView) -> AgentCleanupRecordOut:
        r = view.record
        return cls(
            id=r.id,
            agent=r.agent,
            since=r.since,
            until=r.until,
            cleaned_by=r.cleaned_by,
            cleaned_at=r.cleaned_at,
            withdrawn=len(r.withdrawn_ids),
            ratings_set_aside=r.ratings_set_aside,
            restored=len(r.restored),
            pins_removed=len(r.unpinned_pins),
            revoked=r.revoked,
            undone_at=r.undone_at,
            undoable=view.undoable,
            undo_blocked=view.blocked,
            self_service=r.self_service,
        )


class PortalCleanupRequest(BaseModel):
    """``POST /v1/portal/cleanup`` (ADR 0118): the admin request without
    ``also_revoke``; the agent is whoever the key is."""

    since: datetime
    until: datetime | None = None
    dry_run: bool
    restore_ids: list[RefStr] | None = Field(default=None, max_length=1000)
    expected_withdraw: int | None = Field(default=None, ge=0)
    expected_ratings: int | None = Field(default=None, ge=0)

    @field_validator("since", "until")
    @classmethod
    def _window_utc(cls, v: datetime | None) -> datetime | None:
        return to_utc(v)


class PortalCleanupOut(BaseModel):
    """A portal cleanup that happened; ``cleanup_id`` is ``None`` (and
    ``nothing_to_clean`` true) when there was nothing to change."""

    dry_run: Literal[False] = False
    cleanup_id: str | None
    since: datetime
    until: datetime
    withdrawn: int
    ratings_set_aside: int
    restored: int
    pins_removed: list[DroppedPinOut]
    nothing_to_clean: bool

    @classmethod
    def from_outcome(
        cls, outcome: CleanupOutcome, *, since: datetime, until: datetime
    ) -> PortalCleanupOut:
        return cls(
            cleanup_id=outcome.record.id if outcome.record is not None else None,
            since=since,
            until=until,
            withdrawn=outcome.withdrawn,
            ratings_set_aside=outcome.ratings_set_aside,
            restored=outcome.restored,
            pins_removed=[DroppedPinOut.from_dropped(p) for p in outcome.unpinned],
            nothing_to_clean=outcome.record is None,
        )


class PortalCleanupRecordOut(BaseModel):
    """One of the agent's cleanups as the portal lists it. ``run_by`` is
    ``agent`` or ``admin``; an admin's can be undone only by an admin."""

    id: str
    since: datetime
    until: datetime
    cleaned_at: datetime
    run_by: Literal["agent", "admin"]
    withdrawn: int
    ratings_set_aside: int
    restored: int
    pins_removed: int
    undone_at: datetime | None
    undoable: bool
    undo_blocked: str | None

    @classmethod
    def from_view(cls, view: CleanupView) -> PortalCleanupRecordOut:
        r = view.record
        return cls(
            id=r.id,
            since=r.since,
            until=r.until,
            cleaned_at=r.cleaned_at,
            run_by="agent" if r.self_service else "admin",
            withdrawn=len(r.withdrawn_ids),
            ratings_set_aside=r.ratings_set_aside,
            restored=len(r.restored),
            pins_removed=len(r.unpinned_pins),
            undone_at=r.undone_at,
            undoable=view.undoable,
            undo_blocked=view.blocked,
        )
