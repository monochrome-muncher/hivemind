"""Postgres-backed integration test for ``Store.count_entries`` (ROADMAP
§3.3, the minimal usage-counters surface).

Validates that the cheap ``COUNT`` behind the metrics service works in
Postgres (not just the in-memory reference store): the filter-condition
translation (``_filter_conditions``) plus the visibility clause are
exercised against the live dev Postgres. Skips cleanly when the DB is
unreachable.
"""

from __future__ import annotations

import asyncpg
import pytest

from hivemind.config import Settings
from hivemind.domain.access import Visibility
from hivemind.domain.entry import EntryDraft, EntryFilters, Kind
from hivemind.store import PgStore
from hivemind.store.migrate import migrate

VEC_DIM = Settings().embedding_dim
VEC = [0.01] * VEC_DIM
VEC[3] = 1.0


def _dsn() -> str:
    return Settings().database_url


async def _truncate(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("TRUNCATE entries, fleets, agents, feedbacks CASCADE")
    finally:
        await conn.close()


@pytest.fixture
async def pg() -> PgStore:
    """A ready ``PgStore`` on a clean dev DB (skips when Postgres is down)."""
    dsn = _dsn()
    dim = Settings().embedding_dim
    try:
        probe = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # connection refused / timeout / auth
        pytest.skip(f"Postgres unreachable at {dsn}: {exc}")
    await probe.close()
    await migrate(dsn, dim)
    await _truncate(dsn)
    store = PgStore(dsn)
    try:
        yield store
    finally:
        await _truncate(dsn)
        await store.close()


async def _seed(
    store: PgStore,
    summary: str,
    *,
    scope: str = "org",
    kind: Kind = Kind.FACT,
    fleet_id: str | None = None,
) -> None:
    draft = EntryDraft(
        kind=kind,
        summary=summary,
        author="alice",
        agent="alice",
        scope=scope,
        fleet_id=fleet_id,
    )
    await store.create_entry(draft, VEC, embedding_model="fake")


async def test_count_entries_total_and_by_scope(pg: PgStore) -> None:
    await _seed(pg, "s1", scope="org")
    await _seed(pg, "s2", scope="fleet")
    await _seed(pg, "s3", scope="self")
    assert await pg.count_entries(EntryFilters()) == 3
    assert await pg.count_entries(EntryFilters(scope="org")) == 1
    assert await pg.count_entries(EntryFilters(scope="fleet")) == 1
    assert await pg.count_entries(EntryFilters(scope="self")) == 1


async def test_count_entries_by_kind(pg: PgStore) -> None:
    await _seed(pg, "s1", kind=Kind.FACT)
    await _seed(pg, "s2", kind=Kind.INSIGHT)
    await _seed(pg, "s3", kind=Kind.INSIGHT)
    assert await pg.count_entries(EntryFilters(kind=Kind.INSIGHT)) == 2
    assert await pg.count_entries(EntryFilters(kind=Kind.FACT)) == 1


async def test_count_entries_by_fleet(pg: PgStore) -> None:
    fleet = await pg.create_fleet("data-eng")
    await _seed(pg, "s1", scope="fleet", fleet_id=fleet.id)
    await _seed(pg, "s2", scope="fleet", fleet_id=fleet.id)
    other = await pg.create_fleet("ml")
    await _seed(pg, "s3", scope="fleet", fleet_id=other.id)
    assert await pg.count_entries(EntryFilters(fleet_id=fleet.id)) == 2
    assert await pg.count_entries(EntryFilters(fleet_id=other.id)) == 1


async def test_count_entries_active_only_by_default(pg: PgStore) -> None:
    draft = EntryDraft(
        kind=Kind.FACT, summary="to withdraw", author="alice", agent="alice", scope="org"
    )
    entry = await pg.create_entry(draft, VEC, embedding_model="fake")
    await pg.withdraw_entry(entry.id, "no longer relevant", "alice")
    # Default (active-only) count excludes the withdrawn entry.
    assert await pg.count_entries(EntryFilters()) == 0
    # include_inactive lifts the state restriction.
    assert await pg.count_entries(EntryFilters(include_inactive=True)) == 1


async def test_count_entries_with_visibility(pg: PgStore) -> None:
    # A level-1 reader sees only org + own self-scope + own-fleet entries.
    fleet = await pg.create_fleet("home")
    await _seed(pg, "org entry", scope="org")
    await _seed(pg, "fleet entry", scope="fleet", fleet_id=fleet.id)
    await _seed(pg, "private self", scope="self")
    reader = Visibility(
        level=1,
        name="alice",
        home_fleet_id=fleet.id,
        is_admin=False,
    )
    # The reader's own self-scope entry + org + home-fleet entry are visible.
    assert await pg.count_entries(EntryFilters(), visibility=reader) == 3
    # A level-0 reader sees nothing.
    untrusted = Visibility(level=0, name="stranger", home_fleet_id=None, is_admin=False)
    assert await pg.count_entries(EntryFilters(), visibility=untrusted) == 0
