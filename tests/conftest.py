"""Shared pytest fixtures (fakes live in ``tests.fakes``)."""

from __future__ import annotations

import pytest

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
