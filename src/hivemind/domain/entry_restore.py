"""Restoring one withdrawn memory (ADR 0124).

Pure data and rules, no I/O. Both stores apply ``restore_refusal`` so they
agree on which restore is refused and with what words.
"""

from __future__ import annotations

from dataclasses import dataclass

from hivemind.domain.entry import Entry


class RestoreRefused(Exception):
    """A restore the current state does not allow (409). ``code`` is the
    machine-readable error code; ``str(exc)`` tells the admin what to do."""

    code = "restore_refused"


class EntryNotWithdrawn(RestoreRefused):
    """The memory is active or superseded: there is nothing to restore."""

    code = "entry_not_withdrawn"


class RestoreViaCleanup(RestoreRefused):
    """A standing agent cleanup withdrew the memory: its undo puts it back
    together with the rest of that cleanup."""

    code = "restore_via_cleanup"


class RestoreChainConflict(RestoreRefused):
    """Putting the memory back would leave a chain with two active heads."""

    code = "restore_chain_conflict"


@dataclass(frozen=True, slots=True)
class Restoration:
    """What a restore did: the memory, active again, and the reason it was
    withdrawn with (kept for the audit row; the entry no longer holds it)."""

    entry: Entry
    previous_reason: str | None


def not_withdrawn_message(entry_id: str, state: str) -> str:
    return f"entry {entry_id} is {state}, not withdrawn; only a withdrawn memory can be restored"


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
    """The chain rule a restore of ``entry_id`` would break, or ``None``.

    ``was_cut``: some cleanup record lists the memory as one it put back (a
    cleanup cut its link to the memory that replaced it, and no later state can
    tell whether that replacement or the memory would be the head, since either
    can be withdrawn and restored again). ``still_linked_by``: an active memory
    that names it as its successor (the state rules never produce one).
    ``put_back_instead``: an active memory a cleanup put back whose recorded
    successor chain leads to ``entry_id``."""
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
) -> RestoreRefused | None:
    """Why ``entry_id`` cannot be restored, or ``None`` when it can.

    ``standing_cleanup`` is the id of a not-undone cleanup that withdrew the
    memory (the store looks it up); ``chain_conflict`` is the message from
    ``chain_conflict_message`` (the chain rules in ADR 0124).

    Where a later "redacted" state (a withdrawn memory whose text was blanked)
    arrives, it must refuse here too: a blanked memory has nothing left to
    bring back, and an empty memory must never become active.
    """
    if state != "withdrawn":
        return EntryNotWithdrawn(not_withdrawn_message(entry_id, state))
    if standing_cleanup is not None:
        return RestoreViaCleanup(cleanup_message(entry_id, standing_cleanup))
    if chain_conflict is not None:
        return RestoreChainConflict(chain_conflict)
    return None
