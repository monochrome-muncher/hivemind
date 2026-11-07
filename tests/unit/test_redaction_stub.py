"""``is_redaction_stub`` (ADR 0125): what a redaction leaves behind is
recognised without ``redacted_at``, and a real memory never is."""

from __future__ import annotations

import pytest

from hivemind.domain.redaction import REDACTED_SUMMARY, is_redaction_stub


def test_a_stub_is_recognised() -> None:
    assert is_redaction_stub(REDACTED_SUMMARY, None, REDACTED_SUMMARY, has_embedding=False)


@pytest.mark.parametrize(
    ("summary", "body", "reason", "has_embedding"),
    [
        # A real memory has an embedding, whatever its words.
        (REDACTED_SUMMARY, None, REDACTED_SUMMARY, True),
        (REDACTED_SUMMARY, "some body", REDACTED_SUMMARY, False),
        (REDACTED_SUMMARY, None, "withdrawn for a reason", False),
        ("a memory", None, REDACTED_SUMMARY, False),
    ],
)
def test_anything_else_is_not_a_stub(
    summary: str, body: str | None, reason: str | None, has_embedding: bool
) -> None:
    assert not is_redaction_stub(summary, body, reason, has_embedding)
