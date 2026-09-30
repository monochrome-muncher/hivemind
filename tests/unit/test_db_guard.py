"""STORE-9: the integration suite TRUNCATEs / DROPs the database it is pointed at."""

from __future__ import annotations

import pytest

from tests.db_guard import UnsafeTestDatabase, check_test_database


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://hivemind:hivemind@localhost:5432/hivemind",  # the dev default
        "postgresql://hivemind:hivemind@localhost:5432/hivemind_a7",
        "postgresql://u:p@db:5432/hivemind_test",
        "postgresql://u:p@db:5432/ci?sslmode=disable",
        "postgresql://u:p@db:5432/scratch_dev",
    ],
)
def test_dev_and_test_database_names_are_accepted(dsn: str) -> None:
    check_test_database(dsn, allow_any=False)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u:p@prod-db:5432/memory",
        "postgresql://u:p@prod-db:5432/hivemind-production",
        "postgresql://u:p@prod-db:5432/postgres",
    ],
)
def test_other_database_names_are_refused(dsn: str) -> None:
    with pytest.raises(UnsafeTestDatabase, match="HIVEMIND_TEST_ALLOW_ANY_DB"):
        check_test_database(dsn, allow_any=False)


def test_escape_hatch_allows_any_name() -> None:
    check_test_database("postgresql://u:p@prod-db:5432/memory", allow_any=True)
