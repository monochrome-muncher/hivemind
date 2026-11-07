"""Redacting a withdrawn memory that holds a secret or personal data (ADR 0125).

Pure data and rules, no I/O. Both stores replace the same fields with the
same stubs, so they agree.
"""

from __future__ import annotations

from dataclasses import dataclass

from hivemind.domain.entry import Entry

# What a redacted memory shows in place of its text. The summary and the
# withdrawal reason carry this; body, payload, tags, sources, entities and
# links are emptied.
REDACTED_SUMMARY = "[redacted]"

# Feedback and flag notes written by other agents may quote the text. They are
# never edited, only counted, so an admin can go and review them.


@dataclass(frozen=True, slots=True)
class RedactOutcome:
    """A finished redaction: the memory as it is now (the stub), and how many
    feedback notes on it (``feedback_notes``, flag notes included) and how many
    of those are stale or wrong flags (``flag_notes``) were left untouched, and
    how many audit rows for it hold a non-empty withdrawal reason
    (``withdraw_reasons``: the ``reason`` of an ``entry.withdraw`` row and the
    ``previous_reason`` of an ``entry.restore`` row; the audit log is never
    edited)."""

    entry: Entry
    feedback_notes: int
    flag_notes: int
    withdraw_reasons: int = 0


class RedactRefused(Exception):
    """The memory cannot be redacted now (409 ``conflict``): it is still
    active (withdraw it first), or it is already redacted."""
