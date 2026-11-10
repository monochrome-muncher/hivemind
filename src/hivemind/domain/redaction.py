"""Redacting a withdrawn memory that holds a secret or personal data (ADR 0125); both stores
replace the same fields with the same stubs."""

from __future__ import annotations

from dataclasses import dataclass

from hivemind.domain.entry import Entry

# Replaces the summary and the withdrawal reason; body, payload, tags, sources, entities and
# links are emptied.
REDACTED_SUMMARY = "[redacted]"


def is_redaction_stub(
    summary: str, body: str | None, withdrawn_reason: str | None, has_embedding: bool
) -> bool:
    """Whether a memory is a redaction stub even with no ``redacted_at`` (rolling back migration
    ``0032`` and reapplying it leaves that NULL). It must never be restored. A real memory
    has an embedding, so this never matches one."""
    return (
        summary == REDACTED_SUMMARY
        and withdrawn_reason == REDACTED_SUMMARY
        and body is None
        and not has_embedding
    )


# Other agents' feedback notes may quote the text: never edited, only counted, for admin review.


@dataclass(frozen=True, slots=True)
class RedactOutcome:
    """A finished redaction: the stub, and the untouched ``feedback_notes`` (``flag_notes`` of
    them stale/wrong) and audit rows holding a withdrawal reason (``withdraw_reasons``; the
    audit log is never edited)."""

    entry: Entry
    feedback_notes: int
    flag_notes: int
    withdraw_reasons: int = 0


class RedactRefused(Exception):
    """The memory cannot be redacted now (409 ``conflict``): still active, or already redacted."""
