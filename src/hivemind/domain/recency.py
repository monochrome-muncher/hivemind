"""The recency hint a search may carry (ADR 0073). No I/O."""

from __future__ import annotations

from enum import StrEnum


class RecencyHint(StrEnum):
    """How a caller wants age to weigh in one search.

    ``DEFAULT`` is the shipped scoring. ``PREFER_RECENT`` and ``IGNORE_AGE``
    take effect only when the server's ``recency_hint_enabled`` setting is
    on; otherwise they are accepted and ignored (ADR 0073).
    """

    DEFAULT = "default"
    PREFER_RECENT = "prefer_recent"
    IGNORE_AGE = "ignore_age"


def parse_recency_hint(value: str | None) -> RecencyHint:
    """A raw value as a hint; ``None`` is ``DEFAULT``. ``ValueError`` on an unknown value, even
    with the setting off."""
    if value is None:
        return RecencyHint.DEFAULT
    try:
        return RecencyHint(value)
    except ValueError:
        known = ", ".join(h.value for h in RecencyHint)
        raise ValueError(f"unknown recency {value!r} (known: {known})") from None
