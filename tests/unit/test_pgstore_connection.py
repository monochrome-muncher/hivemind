"""``PgStore`` acquires every connection through one helper: the
configured acquire timeout always applies, and a transaction commits on a
normal exit (a ``return`` inside it included) and rolls back on an error.
No live Postgres: the pool is a recording fake.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from hivemind.domain.entry import EntryFilters
from hivemind.store.pgstore import PgStore
from hivemind.store.pool import PoolTimeouts

VALID_ID = "00000000-0000-4000-8000-000000000001"


class FakeConn:
    def __init__(self, log: list[str], *, fetchval: Any = 0) -> None:
        self._log = log
        self._fetchval = fetchval

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        self._log.append("begin")
        try:
            yield
        except BaseException:
            self._log.append("rollback")
            raise
        self._log.append("commit")

    async def fetch(self, *_a: Any) -> list[Any]:
        self._log.append("fetch")
        return []

    async def fetchrow(self, *_a: Any) -> Any:
        self._log.append("fetchrow")
        return None

    async def fetchval(self, *_a: Any) -> Any:
        self._log.append("fetchval")
        return self._fetchval

    async def execute(self, *_a: Any) -> str:
        self._log.append("execute")
        return "DELETE 0"


class FakePool:
    def __init__(self, conn: FakeConn, log: list[str]) -> None:
        self._conn = conn
        self._log = log
        self.timeouts: list[float | None] = []

    @asynccontextmanager
    async def acquire(self, *, timeout: float | None = None) -> AsyncIterator[FakeConn]:
        self.timeouts.append(timeout)
        self._log.append("acquire")
        try:
            yield self._conn
        finally:
            self._log.append("release")


def _store(*, fetchval: Any = 0) -> tuple[PgStore, FakePool, list[str]]:
    log: list[str] = []
    pool = FakePool(FakeConn(log, fetchval=fetchval), log)
    store = PgStore("postgresql://example/db", timeouts=PoolTimeouts(acquire=3.5))
    store._pool = pool  # type: ignore[assignment]
    return store, pool, log


async def test_reads_acquire_with_the_configured_timeout() -> None:
    store, pool, log = _store()
    await store.get_entries([VALID_ID])
    await store.list_entries(EntryFilters())
    await store.list_pins("fleet")
    await store.list_agents()
    assert pool.timeouts == [3.5, 3.5, 3.5, 3.5]
    assert "begin" not in log  # plain reads open no transaction


async def test_vector_search_runs_in_a_transaction_for_set_local() -> None:
    store, pool, log = _store()
    await store.search_vector([0.0, 1.0], EntryFilters(), 5)
    assert pool.timeouts == [3.5]
    assert log == ["acquire", "begin", "execute", "fetch", "commit", "release"]


async def test_a_return_inside_the_transaction_commits() -> None:
    # The pin cap is reached: pin_entry returns None from inside its
    # transaction, which must still commit (and release the connection).
    store, pool, log = _store(fetchval=10)
    assert await store.pin_entry("fleet", VALID_ID, "bob", limit=10) is None
    assert log[0:2] == ["acquire", "begin"]
    assert log[-2:] == ["commit", "release"]
    assert pool.timeouts == [3.5]


async def test_an_error_inside_the_transaction_rolls_back_and_releases() -> None:
    store, _pool, log = _store()

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        async with store._transaction():
            raise Boom
    assert log == ["acquire", "begin", "rollback", "release"]
