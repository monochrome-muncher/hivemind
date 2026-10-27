"""Shared pytest fixtures (fakes live in ``tests.fakes``)."""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Generator

import psycopg
import pytest
from psycopg import errors as pg_errors
from psycopg import sql

from hivemind.config import Settings
from tests.db_guard import (
    UnsafeTestDatabase,
    check_test_database,
    database_name,
    worker_dsn,
)
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
    """STORE-9: integration tests wipe the DB in HIVEMIND_DATABASE_URL; vet its name first.

    Runs before any test, so the worker database set here is what every DSN
    read sees. Nothing may read ``Settings().database_url`` at import time:
    it would capture the base database instead of the worker's.
    """
    if not any("integration" in item.nodeid.split("/") for item in items):
        return
    allow_any = os.environ.get("HM_TEST_ALLOW_ANY_DB") == "1"
    workerinput = getattr(config, "workerinput", None)
    worker = workerinput["workerid"] if workerinput else None
    base = Settings().database_url
    own = None
    try:
        check_test_database(base, allow_any=allow_any)
        if worker:
            # The base database is vetted above, before anything is created.
            own = worker_dsn(base, worker)
            check_test_database(own, allow_any=allow_any)
    except UnsafeTestDatabase as exc:
        if worker:  # an xdist worker's UsageError dies without a message
            print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        raise pytest.UsageError(str(exc)) from None
    if own is not None:
        _use_worker_database(base, own)


def _use_worker_database(base: str, own: str) -> None:
    """Point this xdist worker (and every subprocess it spawns) at its own database.

    The integration tests TRUNCATE and DROP SCHEMA, so workers cannot share
    one: the worker always switches, even when the database cannot be made
    here, and its tests then skip (or fail under HM_TEST_REQUIRE_PG=1) as
    on an unreachable Postgres. The database is created on first use with
    the vector extension, which the Postgres tests probe for before they
    migrate, and kept for the next run (`DROP DATABASE` to clean up).
    """
    os.environ["HIVEMIND_DATABASE_URL"] = own
    name = database_name(own)
    try:
        with psycopg.connect(base, autocommit=True, connect_timeout=5) as conn:
            exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            if exists.fetchone() is None:
                # DuplicateDatabase: lost a race with another run.
                with contextlib.suppress(pg_errors.DuplicateDatabase):
                    conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        with psycopg.connect(own, autocommit=True, connect_timeout=5) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except psycopg.OperationalError:
        return
    except pg_errors.FeatureNotSupported, pg_errors.UndefinedFile:
        return  # no pgvector: the tests skip on their extension probe
    except psycopg.Error as exc:
        raise pytest.UsageError(
            f"could not create the xdist worker database {name!r}: {exc}"
        ) from None


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


@pytest.fixture(autouse=True)
def _no_dev_bind_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bind host in the shell (`make test` no longer passes the dev
    HIVEMIND_HOST=127.0.0.1 on) must not reach the tests: on a loopback bind
    with no allowed hosts the MCP SDK turns on its own Host check, so
    in-process clients (Host `testserver`) got a 421. Tests that need a bind
    host set it themselves."""
    monkeypatch.delenv("HIVEMIND_HOST", raising=False)
