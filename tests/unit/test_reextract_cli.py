"""The ``hivemind-reextract`` command line (ADR 0131); the run itself is tested against Postgres."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hivemind.store import reextract
from hivemind.store.reextract import Scope, build_parser


def test_a_dry_run_over_active_entries_by_default() -> None:
    args = build_parser().parse_args([])
    assert args.apply is False
    assert args.include_inactive is False
    assert (args.batch_size, args.concurrency, args.limit, args.after) == (50, 4, None, None)


def test_a_time_without_a_zone_is_utc() -> None:
    args = build_parser().parse_args(["--created-before", "2026-11-11"])
    assert args.created_before == datetime(2026, 11, 11, tzinfo=UTC)


@pytest.mark.parametrize(
    "argv",
    [
        ["--created-before", "last week"],
        ["--after", "not-an-id"],
        ["--fleet", "dev"],
        ["--batch-size", "0"],
        ["--concurrency", "-2"],
        ["--limit", "ten"],
    ],
)
def test_bad_arguments_are_refused(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(argv)
    assert exc.value.code == 2


def test_an_empty_time_range_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        reextract.main(["--created-after", "2026-11-11", "--created-before", "2026-11-01"])
    assert exc.value.code == 1
    assert "--created-after must be earlier" in capsys.readouterr().err


def test_it_refuses_to_run_with_extraction_off(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HIVEMIND_EXTRACTOR_ENDPOINT", "")
    with pytest.raises(SystemExit) as exc:
        reextract.main([])
    assert exc.value.code == 1
    assert "extraction is off" in capsys.readouterr().err


def test_the_scope_numbers_its_parameters_after_the_cursor() -> None:
    fleet = "6f1c1f43-6a0d-4c7e-9d55-0d1f8f6f9c11"
    after = datetime(2026, 1, 1, tzinfo=UTC)
    where, params = Scope(fleet_id=fleet, created_after=after).where(first_param=3)
    assert where == " AND state = 'active' AND fleet_id = $3::uuid AND created_at >= $4"
    assert params == [fleet, after]
