"""Postgres-only tests for agent registration (ADR 0045).

The fleet, agent and visibility behaviour both adapters share is in
``test_store_contract.py``; this file keeps what only the SQL has: the
folded-name lock and the case variants that predate it.
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest

from hivemind.config import Settings
from hivemind.store import PgStore
from hivemind.store.migrate import migrate
from hivemind.store.pgstore import AGENT_NAME_LOCK


def _dsn() -> str:
    return Settings().database_url


async def _truncate_access(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("TRUNCATE fleets, agents, entries, feedbacks, credentials")
    finally:
        await conn.close()


@pytest.fixture
async def pg():
    """A ready ``PgStore`` on a clean dev DB (skips when Postgres is down)."""
    dsn = _dsn()
    dim = Settings().embedding_dim
    try:
        probe = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # connection refused / timeout / auth
        pytest.skip(f"Postgres unreachable at {dsn}: {exc}")
    await probe.close()
    await migrate(dsn, dim)
    await _truncate_access(dsn)
    store = PgStore(dsn)
    try:
        yield store
    finally:
        await _truncate_access(dsn)
        await store.close()


async def test_case_variant_registrations_are_serialised(pg) -> None:
    # A registration of `bob` that is still uncommitted must hold off one of
    # `Bob`, or both would pass the folded check (ADR 0045).
    pool = await pg._ensure_pool()
    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        await conn.execute(AGENT_NAME_LOCK, "bob")
        await conn.execute(
            "INSERT INTO agents (name, status, trust_level) VALUES ('bob', 'pending', 0)"
        )
        racing = asyncio.create_task(pg.register_agent("Bob"))
        await asyncio.sleep(0.3)
        assert not racing.done()  # blocked on the folded-name lock
        await tx.commit()
    holder = await asyncio.wait_for(racing, 5)
    assert holder.name == "bob"
    assert [a.name for a in await pg.list_agents()] == ["bob"]


async def test_legacy_case_variants_still_resolve_exactly(pg) -> None:
    # Names that already coexist (registered before ADR 0045) stay usable.
    pool = await pg._ensure_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agents (name, status, trust_level) VALUES ('carol', 'pending', 0), "
            "('Carol', 'pending', 0)"
        )
    assert (await pg.register_agent("Carol")).name == "Carol"
    assert (await pg.register_agent("carol")).name == "carol"
