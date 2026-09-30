"""Shared pytest fixtures (fakes live in ``tests.fakes``)."""

from __future__ import annotations

import os

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
            allow_any=os.environ.get("HIVEMIND_TEST_ALLOW_ANY_DB") == "1",
        )
    except UnsafeTestDatabase as exc:
        raise pytest.UsageError(str(exc)) from None
