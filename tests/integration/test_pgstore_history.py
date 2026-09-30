"""Postgres-backed tests for the chain / list / usage reads (PERF-1, PERF-2,
STORE-2, STORE-3; migration 0008).

``list_predecessors`` and ``usage_counts`` must agree with the in-memory
reference store, and the two 0008 indexes must actually be *used* by the
queries they were added for (EXPLAIN), not merely exist. Skips cleanly when
the DB is unreachable.
"""

from __future__ import annotations

import asyncpg
import pytest

from hivemind.config import Settings
from hivemind.domain.entry import EntryDraft, ImportanceSource, Kind
from hivemind.memstore import MemoryStore
from hivemind.store import PgStore
from hivemind.store.migrate import migrate
from hivemind.store.pgstore import SELECT_PREDECESSORS

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
    dsn = _dsn()
    try:
        probe = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:
        pytest.skip(f"Postgres unreachable at {dsn}: {exc}")
    await probe.close()
    await migrate(dsn, VEC_DIM)
    await _truncate(dsn)
    store = PgStore(dsn)
    try:
        yield store
    finally:
        await _truncate(dsn)
        await store.close()


def _draft(summary: str, **kw) -> EntryDraft:
    return EntryDraft(
        kind=kw.pop("kind", Kind.FACT),
        summary=summary,
        author=kw.pop("author", "alice"),
        agent="alice",
        **kw,
    )


async def _chain(store, n: int = 4) -> list[str]:
    ids: list[str] = []
    for i in range(n):
        supersedes = (ids[-1],) if ids else ()
        e = await store.create_entry(
            _draft(f"v{i}", supersedes=supersedes), VEC, embedding_model="fake"
        )
        ids.append(e.id)
    return ids


async def test_list_predecessors_returns_entries_superseded_by_the_ids(pg: PgStore) -> None:
    ids = await _chain(pg)
    await pg.create_entry(_draft("bystander"), VEC, embedding_model="fake")
    preds = await pg.list_predecessors([ids[2]])
    assert [e.id for e in preds] == [ids[1]]
    both = await pg.list_predecessors([ids[1], ids[3]])
    assert {e.id for e in both} == {ids[0], ids[2]}
    assert await pg.list_predecessors([]) == []
    assert await pg.list_predecessors(["not-a-uuid", ids[0]]) == []  # the tail has none


async def test_list_predecessors_matches_the_reference_store(pg: PgStore) -> None:
    mem = MemoryStore()
    pg_ids = await _chain(pg)
    mem_ids = await _chain(mem)
    for i in range(1, len(pg_ids)):
        pg_preds = await pg.list_predecessors([pg_ids[i]])
        mem_preds = await mem.list_predecessors([mem_ids[i]])
        # Same contents (by position in the chain), not merely the same count.
        assert [pg_ids.index(e.id) for e in pg_preds] == [mem_ids.index(e.id) for e in mem_preds]
        assert [(e.summary, e.state, e.superseded_by is not None) for e in pg_preds] == [
            (e.summary, e.state, e.superseded_by is not None) for e in mem_preds
        ]
    # A merge: two entries superseded by one successor come back together.
    a = await pg.create_entry(_draft("ma"), VEC, embedding_model="fake")
    b = await pg.create_entry(_draft("mb"), VEC, embedding_model="fake")
    merged = await pg.create_entry(
        _draft("merged", supersedes=(a.id, b.id)), VEC, embedding_model="fake"
    )
    assert {e.id for e in await pg.list_predecessors([merged.id])} == {a.id, b.id}


async def test_chain_walk_on_postgres_hides_an_invisible_middle_version(pg: PgStore) -> None:
    """ADR 0033 on the real store: v1 (org, alice) <- v2 (self, bob) <- v3
    (self, bob). alice must see v1 through the invisible v2 without ever
    receiving v2; bob (the author) sees the whole chain; an admin sees all."""
    from hivemind.domain.access import TrustLevel, Visibility
    from hivemind.services.chain import supersession_chain

    v1 = await pg.create_entry(_draft("v1", scope="org"), VEC, embedding_model="fake")
    v2 = await pg.create_entry(
        _draft("v2", scope="self", author="bob", supersedes=(v1.id,)), VEC, embedding_model="fake"
    )
    v3 = await pg.create_entry(
        _draft("v3", scope="self", author="bob", supersedes=(v2.id,)), VEC, embedding_model="fake"
    )
    head = await pg.get_entry(v3.id)
    assert head is not None

    alice = Visibility(level=TrustLevel.CONTRIBUTOR, name="alice")
    bob = Visibility(level=TrustLevel.CONTRIBUTOR, name="bob")
    admin = Visibility(level=TrustLevel.PRIVILEGED, name="admin", is_admin=True)
    _, as_alice = await supersession_chain(pg, head, visibility=alice)
    _, as_bob = await supersession_chain(pg, head, visibility=bob)
    _, as_admin = await supersession_chain(pg, head, visibility=admin)
    assert [e.id for e in as_alice] == [v1.id]
    assert {e.id for e in as_bob} == {v1.id, v2.id}
    assert {e.id for e in as_admin} == {v1.id, v2.id}
    # Forward direction: from v1, alice sees no successors (both are bob's self).
    tail = await pg.get_entry(v1.id)
    assert tail is not None
    successors, _ = await supersession_chain(pg, tail, visibility=alice)
    assert successors == []


async def test_usage_counts_group_like_the_reference_store(pg: PgStore) -> None:
    fleet = await pg.create_fleet("data-eng")
    mem = MemoryStore()
    mem_fleet = await mem.create_fleet("data-eng")
    for store, fid in ((pg, fleet.id), (mem, mem_fleet.id)):
        await store.create_entry(_draft("a", scope="org"), VEC, embedding_model="f")
        await store.create_entry(
            _draft("b", scope="fleet", fleet_id=fid, kind=Kind.INSIGHT, author="bob"),
            VEC,
            embedding_model="f",
        )
        withdrawn = await store.create_entry(
            _draft("c", importance_source=ImportanceSource.CALLER), VEC, embedding_model="f"
        )
        await store.withdraw_entry(withdrawn.id, "x", "admin")

    def norm(rows, fid):
        return sorted(
            (
                r.scope,
                r.kind.value,
                r.importance_source.value,
                r.author,
                r.fleet_id == fid,
                r.fleet_id is None,
                r.active,
                r.count,
            )
            for r in rows
        )

    assert norm(await pg.usage_counts(), fleet.id) == norm(await mem.usage_counts(), mem_fleet.id)
    assert sum(r.count for r in await pg.usage_counts()) == 3


async def test_the_partial_index_serves_the_predecessor_query(pg: PgStore) -> None:
    """The reverse link must be answered from ``entries_superseded_by_idx``
    (partial, ``WHERE superseded_by IS NOT NULL``) — the planner has to be
    able to prove the query's predicate implies the index's."""
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("SET enable_seqscan = off")
        plan = "\n".join(
            r[0]
            for r in await conn.fetch(
                "EXPLAIN " + SELECT_PREDECESSORS,
                ["00000000-0000-0000-0000-000000000001"],
            )
        )
    finally:
        await conn.close()
    assert "entries_superseded_by_idx" in plan, plan


async def test_the_default_list_order_is_served_by_the_created_at_index(pg: PgStore) -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        # Enough rows (and fresh stats) that the planner's choice reflects
        # the index, not the cost of an empty table.
        await conn.execute(
            "INSERT INTO entries (kind, summary, occurred_at, created_at, author, agent) "
            "SELECT 'fact', 's' || g, now(), now() - g * interval '1 second', 'a', 'a' "
            "FROM generate_series(1, 5000) g"
        )
        await conn.execute("ANALYZE entries")
        await conn.execute("SET enable_seqscan = off")
        plan = "\n".join(
            r[0]
            for r in await conn.fetch(
                "EXPLAIN SELECT id FROM entries WHERE state = 'active' "
                "ORDER BY created_at DESC, id DESC LIMIT 20"
            )
        )
    finally:
        await conn.close()
    assert "entries_created_at_id_idx" in plan, plan
    assert "Sort" not in plan, plan


async def test_the_author_list_order_is_served_by_the_composite_index(pg: PgStore) -> None:
    """An author whose entries are all OLD must not be answered by walking
    the global created_at index (82 ms at 200k rows, measured): the
    (author, created_at, id) index gives a range scan that stops at LIMIT."""
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(
            "INSERT INTO entries (kind, summary, occurred_at, created_at, author, agent) "
            "SELECT 'fact', 's' || g, now(), now() - interval '1 day' - g * interval '1 second', "
            "CASE WHEN g > 19700 THEN 'old-author' ELSE 'author-' || (g % 50) END, 'a' "
            "FROM generate_series(1, 20000) g"
        )
        await conn.execute("ANALYZE entries")
        plan = "\n".join(
            r[0]
            for r in await conn.fetch(
                "EXPLAIN SELECT id FROM entries WHERE state = 'active' AND author = 'old-author' "
                "ORDER BY created_at DESC, id DESC LIMIT 20"
            )
        )
    finally:
        await conn.close()
    assert "entries_author_created_idx" in plan, plan
    assert "Sort" not in plan, plan
