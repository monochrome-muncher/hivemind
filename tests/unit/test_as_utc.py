"""``as_utc``: the one place a naive datetime means UTC (SPEC §6.4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from hivemind.domain.validation import as_utc


def test_a_naive_datetime_means_utc() -> None:
    assert as_utc(datetime(2026, 1, 2, 3, 4)) == datetime(2026, 1, 2, 3, 4, tzinfo=UTC)


def test_an_aware_datetime_is_kept_as_it_is() -> None:
    plus_two = timezone(timedelta(hours=2))
    value = datetime(2026, 1, 2, 3, 4, tzinfo=plus_two)
    assert as_utc(value) is value
