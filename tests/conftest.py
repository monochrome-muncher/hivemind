"""Shared pytest fixtures (fakes live in ``tests.fakes``)."""

from __future__ import annotations

import os
from collections.abc import Generator

import pytest

from hivemind.config import Settings
from tests.db_guard import UnsafeTestDatabase, check_test_database
from tests.fakes import (
    FIXED_NOW,
    FakeEmbedder,
    FixedClock,
    make_clock,
    make_embedder,
    make_search_config,
    make_store,
)


@pytest.fixture
def fixed_now() -> datetime:  # noqa: F821 - re-export convenience
    return FIXED_NOW


@pytest.fixture
def clock() -> FixedClock:
    return make_clock()


@pytest.fixture
def store():
    return make_store(make_clock())


@pytest.fixture
def embedder() -> FakeEmbedder:
    return make_embedder()


@pytest.fixture
def search_config():
    return make_search_config()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """STORE-9: integration tests wipe the DB in HIVEMIND_DATABASE_URL; vet its name first."""
    if not any("integration" in item.nodeid.split("/") for item in items):
        return
    try:
        check_test_database(
            Settings().database_url,
            allow_any=os.environ.get("HM_TEST_ALLOW_ANY_DB") == "1",
        )
    except UnsafeTestDatabase as exc:
        raise pytest.UsageError(str(exc)) from None


# The Postgres-backed tests skip when the database is missing so a laptop
# without `make pg` stays green. CI sets HM_TEST_REQUIRE_PG=1 so a broken
# service container fails the run instead of passing on skipped tests.
_PG_SKIP_MARKERS = ("Postgres unreachable", "'vector' extension", "migration 0004")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report: pytest.TestReport = yield
    if (
        report.skipped
        and os.environ.get("HM_TEST_REQUIRE_PG") == "1"
        and isinstance(report.longrepr, tuple)
        and any(marker in str(report.longrepr[2]) for marker in _PG_SKIP_MARKERS)
    ):
        report.outcome = "failed"
        report.longrepr = f"HM_TEST_REQUIRE_PG=1 but the test skipped: {report.longrepr[2]}"
    return report
