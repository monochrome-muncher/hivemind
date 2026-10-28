"""Clean up after a bad agent: the records, the reason text and the undo
rule (ADR 0111).

Pure data and rules, no I/O. Both stores apply ``undo_refusal`` and
``check_restore_ids`` so they agree.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from hivemind.domain.entry import Entry
from hivemind.domain.fleet_merge import DroppedPin
from hivemind.domain.validation import InvalidInput

# The most memories (to withdraw) and restorable memories one preview lists.
# The counts are always exact; the lists are cut and say so.
PREVIEW_WITHDRAW_LIMIT = 200
PREVIEW_RESTORE_LIMIT = 500

# The most cleanups one listing returns; the undo rule needs every newer
# cleanup of an agent in view.
CLEANUP_LIST_LIMIT = 500

_REASON_PREFIX = "admin cleanup "


def cleanup_reason(cleanup_id: str) -> str:
    """The ``withdrawn_reason`` a cleanup gives every memory it withdraws, the
    same whoever ran it (``AgentCleanupRecord.self_service`` tells them apart,
    ADR 0118). An undo re-activates only memories still withdrawn with this
    reason."""
    return _REASON_PREFIX + cleanup_id


class AgentCleanupRefused(Exception):
    """The cleanup or undo is not allowed in the current state (409
    ``cleanup_refused``): the preview is stale, ``restore_ids`` names a
    memory that is not restorable, a cleanup of the same agent is running,
    or the undo is not for the newest standing cleanup."""


class UnknownAgentCleanup(KeyError):
    """No cleanup has this id (404 ``not_found``). A ``KeyError``, so ``str(exc)`` is quoted."""


@dataclass(frozen=True, slots=True)
class RestoredEntry:
    """A memory a cleanup put back to ``active``, with the memory that had
    superseded it (a junk memory of the agent) and the head of that chain (the
    agent's memory the cleanup withdrew), kept so an undo can supersede it
    again. ``head`` is empty in a record written without one."""

    entry_id: str
    superseded_by: str
    head: str = ""


@dataclass(frozen=True, slots=True)
class AgentCleanupRecord:
    """One recorded cleanup. The ratings it set aside carry its id in
    ``feedbacks.set_aside_by``; ``ratings_set_aside`` is their number."""

    id: str
    agent: str
    since: datetime
    until: datetime
    cleaned_by: str
    cleaned_at: datetime
    withdrawn_ids: tuple[str, ...] = ()
    restored: tuple[RestoredEntry, ...] = ()
    ratings_set_aside: int = 0
    unpinned_pins: tuple[DroppedPin, ...] = ()
    revoked: bool = False
    undone_at: datetime | None = None
    # Run by the agent itself from the portal (ADR 0118), not by an admin.
    self_service: bool = False


@dataclass(frozen=True, slots=True)
class CleanupScan:
    """What a cleanup of ``agent`` over ``[since, until)`` would change.

    ``withdraw`` and ``restorable`` are cut to the preview limits (newest
    first); the ``*_total`` counts are exact. A ``restorable`` entry is
    ``superseded``, is not one of the agent's own window memories, and its
    ``superseded_by`` chain passes only through the agent's own window
    memories and ends at a memory the cleanup would withdraw."""

    since: datetime
    until: datetime
    withdraw: tuple[Entry, ...] = ()
    withdraw_total: int = 0
    ratings_total: int = 0
    ratings_by_verdict: Mapping[str, int] = field(default_factory=dict)
    pins_total: int = 0
    restorable: tuple[Entry, ...] = ()
    restorable_total: int = 0


@dataclass(frozen=True, slots=True)
class CleanupOutcome:
    """What a cleanup did. ``record`` is ``None`` when there was nothing to
    clean (no record is kept, so it never blocks an undo of an older one)."""

    record: AgentCleanupRecord | None
    withdrawn: int = 0
    ratings_set_aside: int = 0
    restored: int = 0
    unpinned: tuple[DroppedPin, ...] = ()
    revoked: bool = False
    already_revoked: bool = False
    revoke_error: str | None = None


@dataclass(frozen=True, slots=True)
class CleanupUndoOutcome:
    """What an undo did. It never un-revokes an agent."""

    record: AgentCleanupRecord
    reactivated: int
    reactivate_skipped: int
    resuperseded: int
    resupersede_skipped: int
    ratings_cleared: int
    pins_restored: tuple[DroppedPin, ...]
    pins_skipped: tuple[DroppedPin, ...]


def check_window(since: datetime, until: datetime) -> None:
    """``InvalidInput`` unless the window is not empty (``since < until``)."""
    if since >= until:
        raise InvalidInput("since must be before until")


def check_restore_ids(restorable: Sequence[str], restore_ids: Sequence[str] | None) -> list[str]:
    """The ``restore_ids`` to restore (deduplicated, in the order given).

    ``AgentCleanupRefused`` for an id that is not in the ``restorable`` set
    as it stands now: it changed since the preview, or was never offered."""
    wanted = list(dict.fromkeys(restore_ids or ()))
    known = set(restorable)
    stray = [w for w in wanted if w not in known]
    if stray:
        raise AgentCleanupRefused(
            "restore_ids names memories that cannot be restored (changed since the preview, "
            "preview again): " + ", ".join(stray)
        )
    return wanted


def check_counts_unchanged(
    withdraw: int, ratings: int, expected_withdraw: int | None, expected_ratings: int | None
) -> None:
    """``AgentCleanupRefused`` when what the cleanup would change is not what
    the preview showed (``expected_*``): a write or a rating in the window
    since would otherwise be cleaned unseen. ``None`` skips a check."""
    if (expected_withdraw is not None and expected_withdraw != withdraw) or (
        expected_ratings is not None and expected_ratings != ratings
    ):
        raise AgentCleanupRefused(
            "the agent's memories or ratings changed since the preview, preview again"
        )


def undo_refusal(
    record: AgentCleanupRecord, records_newest_first: Sequence[AgentCleanupRecord]
) -> str | None:
    """Why ``record`` cannot be undone now, or ``None`` when it can.

    Only the newest standing (not undone) cleanup of an agent may be undone,
    so each memory has one unambiguous origin (ADR 0111)."""
    if record.undone_at is not None:
        return "this cleanup was already undone"
    for other in records_newest_first:
        if other.id == record.id:
            break
        if other.agent == record.agent and other.undone_at is None:
            return f"cleanup {other.id} of this agent is newer: undo it first"
    return None
