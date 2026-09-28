"""Concurrent first use opens exactly one pool.

``PgStore`` and ``PgAuthenticator`` open their asyncpg pool lazily. Before
the fix, every call that arrived while the first pool was still being
created built its own, and all but the last were orphaned with their
connections open — on a fresh pod, concurrent first requests leaked whole
pools (and the integration suite, running concurrent tests, exhausted
``max_connections``).
"""

from __future__ import annotations

import asyncio

import pytest

from hivemind.store import auth as auth_module
from hivemind.store import pgstore as pgstore_module


class _FakePool:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("module", "factory"),
    [
        (pgstore_module, lambda: pgstore_module.PgStore("postgresql://x/y")),
        (auth_module, lambda: auth_module.PgAuthenticator("postgresql://x/y")),
    ],
    ids=["PgStore", "PgAuthenticator"],
)
async def test_concurrent_first_use_creates_one_pool(module, factory, monkeypatch) -> None:
    created: list[_FakePool] = []

    async def slow_make_pool(*_args, **_kwargs) -> _FakePool:
        await asyncio.sleep(0.01)  # creation takes time: the race window
        pool = _FakePool()
        created.append(pool)
        return pool

    monkeypatch.setattr(module, "make_pool", slow_make_pool)
    owner = factory()
    pools = await asyncio.gather(*(owner._ensure_pool() for _ in range(5)))
    assert len(created) == 1
    assert all(p is created[0] for p in pools)
    await owner.close()
    assert created[0].closed
