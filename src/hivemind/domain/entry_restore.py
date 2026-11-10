"""Restoring one withdrawn memory (ADR 0124); both stores apply ``restore_refusal``."""

from __future__ import annotations

from dataclasses import dataclass

from hivemind.domain.entry import Entry


class RestoreRefused(Exception):
    """A restore the current state does not allow (409); ``code`` is the error code."""

    code = "restore_refused"


class EntryRedacted(RestoreRefused):
    """The memory was redacted (ADR 0125): its text is gone and an empty memory must never be
    active."""

    code = "entry_redacted"


class EntryNotWithdrawn(RestoreRefused):
    """The memory is active or superseded: there is nothing to restore."""

    code = "entry_not_withdrawn"


class RestoreViaCleanup(RestoreRefused):
    """A standing agent cleanup withdrew the memory; undo that cleanup instead."""

    code = "restore_via_cleanup"


class RestoreChainConflict(RestoreRefused):
    """Putting the memory back would leave a chain with two active heads."""

    code = "restore_chain_conflict"


@dataclass(frozen=True, slots=True)
class Restoration:
    """The memory, active again, and its former withdrawal reason (for the audit row)."""

    entry: Entry
    previous_reason: str | None


def not_withdrawn_message(entry_id: str, state: str) -> str:
    return f"entry {entry_id} is {state}, not withdrawn; only a withdrawn memory can be restored"


def redacted_message(entry_id: str) -> str:
    return (
        f"entry {entry_id} was redacted and its text is gone for good; "
        "a redacted memory cannot be restored. Write a new memory instead"
    )


def cleanup_message(entry_id: str, cleanup_id: str) -> str:
    return (
        f"entry {entry_id} was withdrawn by agent cleanup {cleanup_id}, which is still standing; "
        "undo that cleanup instead (POST /v1/admin/agent-cleanups/"
        f"{cleanup_id}/undo), it puts back everything the cleanup changed"
    )


def chain_message_cut(entry_id: str) -> str:
    return (
        f"entry {entry_id} was put back by an agent cleanup after a newer memory replaced it, "
        "and the link between them was cut; restoring it could leave two current versions "
        "of the same thing. Write a new memory instead"
    )


def chain_message_reverse(entry_id: str, active_id: str) -> str:
    return (
        f"an agent cleanup put memory {active_id} back as the current version in place of "
        f"entry {entry_id}; restoring {entry_id} would leave two current versions of the same "
        "thing. Undo that cleanup or write a new memory instead"
    )


def chain_conflict_message(
    entry_id: str, was_cut: bool, still_linked_by: str | None, put_back_instead: str | None
) -> str | None:
    """The chain rule a restore of ``entry_id`` would break, or ``None`` (ADR 0124).

    ``was_cut``: a cleanup record lists it as put back, so its link to its replacement was
    cut and no later state says which should be head. ``still_linked_by``: an active memory
    naming it as successor. ``put_back_instead``: an active memory a cleanup put back whose
    recorded chain leads to ``entry_id``."""
    if was_cut:
        return chain_message_cut(entry_id)
    if put_back_instead is not None:
        return chain_message_reverse(entry_id, put_back_instead)
    if still_linked_by is not None:
        return chain_message_reverse(entry_id, still_linked_by)
    return None


def restore_refusal(
    entry_id: str,
    state: str,
    reason: str | None,
    standing_cleanup: str | None,
    chain_conflict: str | None,
    redacted: bool = False,
) -> RestoreRefused | None:
    """Why ``entry_id`` cannot be restored, or ``None``. ``standing_cleanup`` is the id of a
    not-undone cleanup that withdrew it; ``chain_conflict`` comes from ``chain_conflict_message``.
    A redacted memory (ADR 0125) is refused first."""
    if redacted:
        return EntryRedacted(redacted_message(entry_id))
    if state != "withdrawn":
        return EntryNotWithdrawn(not_withdrawn_message(entry_id, state))
    if standing_cleanup is not None:
        return RestoreViaCleanup(cleanup_message(entry_id, standing_cleanup))
    if chain_conflict is not None:
        return RestoreChainConflict(chain_conflict)
    return None
