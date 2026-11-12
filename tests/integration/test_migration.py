"""Postgres-backed integration tests for the migration chain (ADR 0020).

Covers the four properties the chain is relied on for:

* **forward** — a fresh pool applies the chain and reports its head;
* **idempotent** — re-running applies nothing (the entrypoint runs it on
  every pod start, ADR 0018);
* **concurrent** — many migrators racing a cold pool all succeed and the
  chain is applied exactly once (the advisory lock, ADR 0020);
* **loud on a dim mismatch** — the ADR 0015 guard still fires *before*
  any DDL.

Skips cleanly when Postgres is unreachable.
"""

from __future__ import annotations

import asyncio
import logging
import os

import asyncpg
import pytest
from tests.pg import ensure_migrated

from hivemind.config import Settings
from hivemind.store.migrate import (
    MIGRATION_LOCK_KEY,
    RollbackRefused,
    current_embedding_dim,
    current_schema_version,
    migrate,
)
from hivemind.store.migrate import (
    rollback as _guarded_rollback,
)

HEAD = "0034.agent-role"
ID_0033 = "0033.fleet-phase"  # the head before 0034
ID_0032 = "0032.entry-redact"  # the head before 0033
ID_0031 = "0031.entry-restore"  # the head before 0032
ID_0030 = "0030.audit-target-index"  # the head before 0031
ID_0029 = "0029.agent-owner-set"  # the head before 0030
ID_0028 = "0028.hot-fleet-moves"  # the head before 0029
ID_0027 = "0027.schema-rollback-audit"  # the head before 0028
ID_0026 = "0026.cleanup-self-service"  # the head before 0027
ID_0025 = "0025.key-lock"  # the head before 0026
PREV = "0024.agent-successions"  # the head before 0025

# Every applied migration id, oldest first. Kept explicit rather than read
# off the filesystem: the point of these assertions is that the runner
# applied exactly what shipped, and deriving the expectation from the same
# directory yoyo reads would assert that against itself.
CHAIN = [
    "0001.initial-schema",
    "0002.importance-source",
    "0003.importance-source-check",
    "0004.hnsw-vector-index",
    "0005.audit-log",
    "0006.agent-revoked-status",
    "0007.credential-uniqueness",
    "0008.history-and-list-indexes",
    "0009.audit-register",
    "0010.search-counts",
    "0011.entry-links",
    "0012.pins",
    "0013.see-also-and-agent-indexes",
    "0014.pin-audit",
    "0015.search-counts-explore",
    "0016.entry-withdrawn-at",
    "0017.fleet-merge",
    "0019.fleet-rename",
    "0020.agent-key-replace",
    "0021.credential-last-used",
    "0022.agent-cleanup",
    "0023.fleet-move",
    PREV,
    ID_0025,
    ID_0026,
    ID_0027,
    ID_0028,
    ID_0029,
    ID_0030,
    ID_0031,
    ID_0032,
    ID_0033,
    HEAD,
]


async def rollback(dsn: str, dim: int, count: int = 1, **confirm: bool) -> list[str]:
    """The per-migration tests below exercise what each older rollback does
    to its own schema, so they step over `0034`, `0033`, `0032`, `0031`, `0030`, `0029`, `0028` and `0027` first (in
    one rollback, so no `schema.rollback` row is written), which have their
    own tests at the end of the file. A test that seeds rows a guard counts
    passes the matching confirmation flag (ADR 0119); the returned ids are
    those of the requested `count` migrations below `0027`."""
    steps = {
        HEAD: 8,
        ID_0033: 7,
        ID_0032: 6,
        ID_0031: 5,
        ID_0030: 4,
        ID_0029: 3,
        ID_0028: 2,
        ID_0027: 1,
    }.get(await current_schema_version(dsn))
    if steps:
        await _guarded_rollback(dsn, dim, steps)
    return await _guarded_rollback(dsn, dim, count, **confirm)


def _dsn() -> str:
    return Settings().database_url


async def _start_at(dsn: str, dim: int, version: str) -> None:
    """The chain rolled back from the head to ``version``, for the tests whose
    subject is that migration. The `schema.rollback` row that stepping off the
    ones above it writes is cleared, so the audit log starts empty."""
    await _start_at_head(dsn, dim)
    above = CHAIN[CHAIN.index(version) + 1 :]
    assert await _guarded_rollback(dsn, dim, len(above)) == list(reversed(above))
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("DELETE FROM audit_log")
    finally:
        await conn.close()


async def _require_postgres(dsn: str) -> None:
    try:
        probe = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # connection refused / timeout / auth
        pytest.skip(f"Postgres unreachable at {dsn}: {exc}")
    await probe.close()


async def _reset_chain(dsn: str) -> None:
    """Drop the whole public schema so the next migrate starts cold."""
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    finally:
        await conn.close()


async def _start_at_head(dsn: str, dim: int) -> None:
    """Leave the pool at the head with every application table empty.

    The per-migration tests need "at head, no rows" as their start, not a
    cold schema: migrating from nothing costs about a second per test, and
    `ensure_migrated` only does that when the pool is not already at head
    (an earlier test may have left it at another revision). Tests that are
    about the cold start itself still call `_reset_chain`.
    """
    await ensure_migrated(dsn, dim)
    conn = await asyncpg.connect(dsn)
    try:
        tables = await conn.fetch(
            "SELECT quote_ident(tablename) AS t FROM pg_tables "
            "WHERE schemaname = 'public' AND tablename NOT LIKE '\\_yoyo\\_%' "
            "AND tablename <> 'yoyo_lock'"
        )
        await conn.execute("TRUNCATE " + ", ".join(r["t"] for r in tables) + " RESTART IDENTITY")
    finally:
        await conn.close()


async def test_migrate_applies_the_chain_and_reports_its_head() -> None:
    dsn, dim = _dsn(), Settings().embedding_dim
    await _require_postgres(dsn)
    await _reset_chain(dsn)

    await migrate(dsn, dim)
    assert await current_schema_version(dsn) == HEAD

    # Re-running is a no-op: same head, still one row (the entrypoint
    # runs migrate on every pod start — ADR 0018).
    await migrate(dsn, dim)
    assert await current_schema_version(dsn) == HEAD

    conn = await asyncpg.connect(dsn)
    try:
        # One row per migration in the chain (ADR 0020).
        assert await conn.fetchval("SELECT count(*) FROM _yoyo_migration") == len(CHAIN)
        # 0013: the two new indexes exist and valid; the superseded one is gone.
        for index in ("entry_links_to_created_idx", "entries_agent_created_idx"):
            assert await conn.fetchval(
                "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass($1)", index
            )
        assert await conn.fetchval("SELECT to_regclass('entry_links_to_idx')") is None
    finally:
        await conn.close()


async def test_concurrent_migrators_are_serialised_by_the_advisory_lock() -> None:
    """ADR 0020: two projects x 2-3 replicas all migrate on start.

    Without the lock this is not merely theoretical — the first
    statement (`CREATE EXTENSION IF NOT EXISTS vector`) raises
    `UniqueViolation` for the losers, which under ADR 0018 exits the
    entrypoint before the runner execs.

    Since `0004` (ADR 0025) this also covers the interaction that lock
    has with `CREATE INDEX CONCURRENTLY`: a migrator that *blocks* inside
    `pg_advisory_lock` holds a virtual xid for the whole wait, which the
    winner's concurrent index build waits on forever — an undetectable
    deadlock, because the lock holder is idle and never enters the wait
    graph. `migrate` polls `pg_try_advisory_lock` instead, and this test
    is what says so: before that change it hung here indefinitely rather
    than failing.
    """
    dsn, dim = _dsn(), Settings().embedding_dim
    await _require_postgres(dsn)
    await _reset_chain(dsn)

    results = await asyncio.gather(*[migrate(dsn, dim) for _ in range(6)], return_exceptions=True)
    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, f"concurrent migrators failed: {failures}"

    assert await current_schema_version(dsn) == HEAD
    conn = await asyncpg.connect(dsn)
    try:
        # Each migration applied exactly once, not once per migrator.
        assert await conn.fetchval("SELECT count(*) FROM _yoyo_migration") == len(CHAIN)
        assert await conn.fetchval("SELECT to_regclass('entries') IS NOT NULL")
    finally:
        await conn.close()


async def test_migrate_releases_the_lock_for_the_next_caller() -> None:
    """The advisory lock must not leak: a second migrate in the same
    process (and a third from a fresh connection) must not block."""
    dsn, dim = _dsn(), Settings().embedding_dim
    await _require_postgres(dsn)
    await _reset_chain(dsn)

    await migrate(dsn, dim)
    await asyncio.wait_for(migrate(dsn, dim), timeout=30)

    conn = await asyncpg.connect(dsn)
    try:
        held = await conn.fetchval(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
            "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
            "AND ((classid::bigint << 32) | objid::bigint) = $1",
            MIGRATION_LOCK_KEY,
        )
        assert held == 0, "advisory lock still held after migrate returned"
    finally:
        await conn.close()


async def test_current_schema_version_is_none_when_unmigrated() -> None:
    dsn = _dsn()
    await _require_postgres(dsn)
    await _reset_chain(dsn)
    assert await current_schema_version(dsn) is None


async def test_rollback_removes_the_latest_migration() -> None:
    """ADR 0020: a structural migration (not `0001`) ships a real
    rollback, and rolling one back undoes exactly what it did.

    Peeled one at a time from the head: `0026` drops the self-service mark
    of the cleanup record (ADR 0118); `0025` drops the credential lock time
    and the key lock audit rows and narrows the audit CHECK back to `0024`'s
    list, keeping every key (ADR 0117); `0024` drops the successions and
    the retirement audit rows and narrows the audit CHECK back to `0023`'s list
    (ADR 0113); `0023` drops the move record kind
    and the move audit rows, and narrows the audit CHECK back to `0022`'s list
    (ADR 0112); `0022` drops the agent cleanup
    record, the `set_aside_by` mark (the ratings stay) and the cleanup audit
    rows (ADR 0111); `0021` drops the credential last-use time (ADR 0110) and
    touches no key; `0020` deletes the key replacement
    audit rows and narrows the audit CHECK back (ADR 0108); `0019` deletes the rename audit
    rows and narrows the audit CHECK back (ADR 0109); `0017` drops the fleet merge
    record and its two tables, the `merged_into` marker and the merge audit
    rows (ADR 0107); `0016` drops the withdrawal time
    (ADR 0098) and keeps the state and reason; `0015` drops the explore search
    counter (ADR 0078); `0014` deletes the pin audit rows
    and narrows the audit CHECKs back (ADR 0076); `0013` swaps the "see also" index
    back and drops the agent list index; `0012` drops the pins (ADR 0058);
    `0011` drops the "see also" links
    (ADR 0057); `0010` drops the search counters
    (ADR 0056); `0009` deletes the registration
    rows and narrows the audit CHECKs back (ADR 0046); `0008` drops its three indexes
    (PERF-1 / STORE-3) and nothing else; `0007` drops only its two unique
    credential indexes (ADR 0039); `0006` narrows the agent status
    CHECK back (ADR 0028); `0005` drops the (empty) audit log
    (ADR 0027) and nothing else; `0004` drops the HNSW vector
    index (ADR 0025) and leaves the `embedding` column alone; `0003`
    then drops the named CHECK constraint it added and leaves `0001` +
    `0002` (and the `importance_source` column itself) in place.
    """
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at_head(dsn, dim)

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO agent_cleanups (agent, since, until, cleaned_by, self_service) "
            "VALUES ('bad', now(), now(), 'bad', true)"
        )
    finally:
        await conn.close()
    assert await rollback(dsn, dim, count=1) == [ID_0026]
    assert await current_schema_version(dsn) == ID_0025
    conn = await asyncpg.connect(dsn)
    try:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'agent_cleanups' AND column_name = 'self_service'"
            )
            == 0
        )
        assert await conn.fetchval("SELECT count(*) FROM agent_cleanups") == 1
        await conn.execute("DELETE FROM agent_cleanups")
    finally:
        await conn.close()

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO credentials (key_hash, kind, user_id, agent_id, agent_name, locked_at) "
            "VALUES ('h-locked', 'agent', 'a', 'a', 'a', now())"
        )
        await conn.execute(
            "INSERT INTO audit_log (actor_kind, actor, action, target) "
            "VALUES ('agent_key', 'a', 'agent_key.lock', 'a'), "
            "('admin_key', 'admin:1', 'agent.retire', 'a')"
        )
    finally:
        await conn.close()
    rolled = await rollback(dsn, dim, count=1, discard_audit_rows=True, unlock_locked_keys=True)
    assert rolled == [ID_0025]
    assert await current_schema_version(dsn) == PREV
    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SELECT count(*) FROM credentials") == 1  # the key is kept
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'credentials' AND column_name = 'locked_at'"
            )
            == 0
        )
        assert [r["action"] for r in await conn.fetch("SELECT action FROM audit_log")] == [
            "agent.retire"
        ]
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action) "
                "VALUES ('agent_key', 'a', 'agent_key.lock')"
            )
        await conn.execute("DELETE FROM credentials")
        await conn.execute("DELETE FROM audit_log")
    finally:
        await conn.close()

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO successions (retired_agent, successor, \"by\") VALUES ('a', 'b', 'x')"
        )
        await conn.execute(
            "INSERT INTO audit_log (actor_kind, actor, action, target) "
            "VALUES ('admin_key', 'admin:1', 'agent.retire', 'a'), "
            "('admin_key', 'admin:1', 'agent.retire_undo', 'a'), "
            "('admin_key', 'admin:1', 'agent_key.replace', 'r')"
        )
    finally:
        await conn.close()
    rolled = await rollback(dsn, dim, count=1, discard_audit_rows=True)
    assert rolled == [PREV]
    assert await current_schema_version(dsn) == "0023.fleet-move"
    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SELECT to_regclass('successions')") is None
        assert [r["action"] for r in await conn.fetch("SELECT action FROM audit_log")] == [
            "agent_key.replace"
        ]
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action) "
                "VALUES ('admin_key', 'admin:1', 'agent.retire')"
            )
        await conn.execute("DELETE FROM audit_log")
    finally:
        await conn.close()

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO audit_log (actor_kind, actor, action, target) "
            "VALUES ('admin_key', 'admin:1', 'agent_key.replace', 'a'), "
            "('admin_key', 'admin:1', 'agent.cleanup', 'bad'), "
            "('admin_key', 'admin:1', 'agent.cleanup_undo', 'bad'), "
            "('admin_key', 'admin:1', 'fleet.rename', 'f'), "
            "('admin_key', 'admin:1', 'fleet.merge', 'f'), "
            "('admin_key', 'admin:1', 'fleet.move_agents', 'f'), "
            "('admin_key', 'admin:1', 'fleet.create', 'f')"
        )
        await conn.execute(
            "INSERT INTO entries (id, kind, summary, author, agent, occurred_at) "
            "VALUES ('00000000-0000-0000-0000-000000000001', 'fact', 'e', 'bad', 'x', now())"
        )
        await conn.execute(
            'INSERT INTO feedbacks (entry_id, "user", agent, verdict, set_aside_by) '
            "VALUES ('00000000-0000-0000-0000-000000000001', 'bad', 'x', 'wrong', gen_random_uuid())"
        )
        await conn.execute(
            "INSERT INTO agent_cleanups (agent, since, until, cleaned_by) "
            "VALUES ('bad', now(), now(), 'admin:1')"
        )
    finally:
        await conn.close()
    rolled = await rollback(dsn, dim, count=1, discard_audit_rows=True)
    assert rolled == ["0023.fleet-move"]
    assert await current_schema_version(dsn) == "0022.agent-cleanup"
    conn = await asyncpg.connect(dsn)
    try:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'fleet_merges' AND column_name = 'kind'"
            )
            == 0
        )
        assert sorted(r["action"] for r in await conn.fetch("SELECT action FROM audit_log")) == [
            "agent.cleanup",
            "agent.cleanup_undo",
            "agent_key.replace",
            "fleet.create",
            "fleet.merge",
            "fleet.rename",
        ]
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action) "
                "VALUES ('admin_key', 'admin:1', 'fleet.move_agents')"
            )
    finally:
        await conn.close()
    rolled = await rollback(dsn, dim, count=1, discard_audit_rows=True)
    assert rolled == ["0022.agent-cleanup"]
    assert await current_schema_version(dsn) == "0021.credential-last-used"
    conn = await asyncpg.connect(dsn)
    try:
        has_column = "SELECT count(*) FROM information_schema.columns WHERE column_name = $1"
        assert await conn.fetchval(has_column, "set_aside_by") == 0
        assert await conn.fetchval("SELECT to_regclass('agent_cleanups')") is None
        assert await conn.fetchval("SELECT count(*) FROM feedbacks") == 1  # the rating is kept
        assert sorted(r["action"] for r in await conn.fetch("SELECT action FROM audit_log")) == [
            "agent_key.replace",
            "fleet.create",
            "fleet.merge",
            "fleet.rename",
        ]
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action) "
                "VALUES ('admin_key', 'admin:1', 'agent.cleanup')"
            )
        await conn.execute("DELETE FROM feedbacks")
        await conn.execute("DELETE FROM entries")
    finally:
        await conn.close()
    rolled = await rollback(dsn, dim, count=1)
    assert rolled == ["0021.credential-last-used"]
    assert await current_schema_version(dsn) == "0020.agent-key-replace"
    rolled = await rollback(dsn, dim, count=1, discard_audit_rows=True)
    assert rolled == ["0020.agent-key-replace"]
    assert await current_schema_version(dsn) == "0019.fleet-rename"
    conn = await asyncpg.connect(dsn)
    try:
        assert [r["action"] for r in await conn.fetch("SELECT action FROM audit_log")] == [
            "fleet.rename",
            "fleet.merge",
            "fleet.create",
        ]
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action) "
                "VALUES ('admin_key', 'admin:1', 'agent_key.replace')"
            )
    finally:
        await conn.close()
    rolled = await rollback(dsn, dim, count=1, discard_audit_rows=True)
    assert rolled == ["0019.fleet-rename"]
    assert await current_schema_version(dsn) == "0017.fleet-merge"
    conn = await asyncpg.connect(dsn)
    try:
        assert [r["action"] for r in await conn.fetch("SELECT action FROM audit_log")] == [
            "fleet.merge",
            "fleet.create",
        ]
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action) "
                "VALUES ('admin_key', 'admin:1', 'fleet.rename')"
            )
    finally:
        await conn.close()
    rolled = await rollback(dsn, dim, count=1, discard_audit_rows=True)
    assert rolled == ["0017.fleet-merge"]
    assert await current_schema_version(dsn) == "0016.entry-withdrawn-at"
    conn = await asyncpg.connect(dsn)
    try:
        has_column = "SELECT count(*) FROM information_schema.columns WHERE column_name = $1"
        assert await conn.fetchval(has_column, "merged_into") == 0
        assert await conn.fetchval(has_column, "merged_at") == 0
        for table in ("fleet_merges", "fleet_merge_entries"):
            assert await conn.fetchval("SELECT to_regclass($1)", table) is None
        assert [r["action"] for r in await conn.fetch("SELECT action FROM audit_log")] == [
            "fleet.create"
        ]
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action) "
                "VALUES ('admin_key', 'admin:1', 'fleet.merge')"
            )
        await conn.execute("DELETE FROM audit_log")
    finally:
        await conn.close()
    rolled = await rollback(dsn, dim, count=1)
    assert rolled == ["0016.entry-withdrawn-at"]
    assert await current_schema_version(dsn) == "0015.search-counts-explore"
    conn = await asyncpg.connect(dsn)
    try:
        has_column = "SELECT count(*) FROM information_schema.columns WHERE column_name = $1"
        assert await conn.fetchval(has_column, "withdrawn_at") == 0
        assert await conn.fetchval(has_column, "withdrawn_reason") == 1  # only the time goes
    finally:
        await conn.close()

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO search_counts (fleet_id, searches, empty, explore) VALUES ('', 2, 1, 1)"
        )
        with pytest.raises(asyncpg.CheckViolationError):  # explore never exceeds searches
            await conn.execute("UPDATE search_counts SET explore = 3")
    finally:
        await conn.close()
    rolled = await rollback(dsn, dim, count=1)
    assert rolled == ["0015.search-counts-explore"]
    assert await current_schema_version(dsn) == "0014.pin-audit"
    conn = await asyncpg.connect(dsn)
    try:
        # Only the explore column goes; the other counters stay.
        row = await conn.fetchrow("SELECT * FROM search_counts")
        assert row is not None and dict(row) == {"fleet_id": "", "searches": 2, "empty": 1}
        await conn.execute("DELETE FROM search_counts")
    finally:
        await conn.close()

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO audit_log (actor_kind, actor, action, target) VALUES "
            "('agent_key', 'lead', 'entry.pin', 'e1'), "
            "('agent_key', 'lead', 'entry.unpin', 'e1'), "
            "('org_key', 'org:abc', 'agent.register', 'alice')"
        )
    finally:
        await conn.close()
    rolled = await rollback(dsn, dim, count=1, discard_audit_rows=True)
    assert rolled == ["0014.pin-audit"]
    assert await current_schema_version(dsn) == "0013.see-also-and-agent-indexes"
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch("SELECT action FROM audit_log")
        assert [r["action"] for r in rows] == ["agent.register"]  # only 0014's rows go
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action) "
                "VALUES ('agent_key', 'lead', 'entry.pin')"
            )
        await conn.execute("DELETE FROM audit_log")
    finally:
        await conn.close()

    rolled = await rollback(dsn, dim, count=1)
    assert rolled == ["0013.see-also-and-agent-indexes"]
    assert await current_schema_version(dsn) == "0012.pins"
    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SELECT to_regclass('entry_links_to_idx')") is not None
        for index in ("entry_links_to_created_idx", "entries_agent_created_idx"):
            assert await conn.fetchval("SELECT to_regclass($1)", index) is None
    finally:
        await conn.close()

    rolled = await rollback(dsn, dim, count=1)
    assert rolled == ["0012.pins"]
    assert await current_schema_version(dsn) == "0011.entry-links"
    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SELECT to_regclass('pins')") is None
    finally:
        await conn.close()

    conn = await asyncpg.connect(dsn)
    try:
        with pytest.raises(asyncpg.CheckViolationError):  # no entry links to itself
            await conn.execute(
                "INSERT INTO entry_links (from_id, to_id) VALUES "
                "('00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000001')"
            )
    finally:
        await conn.close()
    rolled = await rollback(dsn, dim, count=1)
    assert rolled == ["0011.entry-links"]
    assert await current_schema_version(dsn) == "0010.search-counts"
    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SELECT to_regclass('entry_links')") is None
    finally:
        await conn.close()

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO search_counts (fleet_id, searches, empty) VALUES ('', 2, 1)"
        )
        with pytest.raises(asyncpg.CheckViolationError):  # empty never exceeds searches
            await conn.execute("UPDATE search_counts SET empty = 3")
    finally:
        await conn.close()
    rolled = await rollback(dsn, dim, count=1)
    assert rolled == ["0010.search-counts"]
    assert await current_schema_version(dsn) == "0009.audit-register"
    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SELECT to_regclass('search_counts')") is None
    finally:
        await conn.close()

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO audit_log (actor_kind, actor, action, target) VALUES "
            "('org_key', 'org:abc', 'agent.register', 'alice'), "
            "('admin_key', 'admin:abc', 'agent.activate', 'alice')"
        )
    finally:
        await conn.close()
    rolled = await rollback(dsn, dim, count=1, discard_audit_rows=True)
    assert rolled == ["0009.audit-register"]
    assert await current_schema_version(dsn) == "0008.history-and-list-indexes"
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch("SELECT action FROM audit_log")
        assert [r["action"] for r in rows] == ["agent.activate"]  # only 0009's rows go
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action) "
                "VALUES ('org_key', 'org:abc', 'agent.register')"
            )
        await conn.execute("DELETE FROM audit_log")
    finally:
        await conn.close()

    rolled = await rollback(dsn, dim, count=1)
    assert rolled == ["0008.history-and-list-indexes"]
    assert await current_schema_version(dsn) == "0007.credential-uniqueness"
    conn = await asyncpg.connect(dsn)
    try:
        for index in (
            "entries_superseded_by_idx",
            "entries_created_at_id_idx",
            "entries_author_created_idx",
        ):
            assert await conn.fetchval("SELECT to_regclass($1)", index) is None
    finally:
        await conn.close()

    rolled = await rollback(dsn, dim, count=1)
    assert rolled == ["0007.credential-uniqueness"]
    assert await current_schema_version(dsn) == "0006.agent-revoked-status"
    conn = await asyncpg.connect(dsn)
    try:
        for index in ("credentials_one_agent_key", "credentials_one_org_key"):
            assert await conn.fetchval("SELECT to_regclass($1)", index) is None
    finally:
        await conn.close()

    rolled = await rollback(dsn, dim, count=1)
    assert rolled == ["0006.agent-revoked-status"]
    assert await current_schema_version(dsn) == "0005.audit-log"

    rolled = await rollback(dsn, dim, count=1)
    assert rolled == ["0005.audit-log"]
    assert await current_schema_version(dsn) == "0004.hnsw-vector-index"

    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SELECT to_regclass('audit_log')") is None
        # The rest of the schema is untouched.
        assert await conn.fetchval("SELECT to_regclass('entries_embedding_hnsw_idx')") is not None
    finally:
        await conn.close()

    rolled = await rollback(dsn, dim, count=1)
    assert rolled == ["0004.hnsw-vector-index"]
    assert await current_schema_version(dsn) == "0003.importance-source-check"

    conn = await asyncpg.connect(dsn)
    try:
        index = await conn.fetchval("SELECT to_regclass('entries_embedding_hnsw_idx')")
        assert index is None
        # The indexed column is untouched — an index holds nothing the
        # table does not, which is what makes this rollback legal.
        column = await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'entries' AND column_name = 'embedding'"
        )
        assert column is not None
    finally:
        await conn.close()

    rolled = await rollback(dsn, dim, count=1)
    assert rolled == ["0003.importance-source-check"]
    assert await current_schema_version(dsn) == "0002.importance-source"

    conn = await asyncpg.connect(dsn)
    try:
        constraint = await conn.fetchval(
            "SELECT 1 FROM pg_constraint WHERE conname = 'entries_importance_source_check'"
        )
        assert constraint is None
        # The column itself is untouched — only 0002's rollback drops it.
        column = await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'entries' AND column_name = 'importance_source'"
        )
        assert column is not None
    finally:
        await conn.close()


async def test_rollback_refuses_to_drop_the_initial_schema() -> None:
    """ADR 0020: a rollback that would destroy data is not written.

    Rolling back `0001` drops every entry in the pool, so `rollback`
    refuses and names the two real remediations instead — even when the
    requested count would also take later (legal) migrations with it
    (the whole chain from HEAD, taking every later migration with it),
    and equally when `0001` is already the head in its own right
    (`count=1` after the rest have already been rolled back) — both are
    the same refusal, exercised from both approaches.
    """
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at_head(dsn, dim)

    with pytest.raises(RuntimeError) as excinfo:
        await _guarded_rollback(dsn, dim, count=len(CHAIN))
    message = str(excinfo.value)
    assert "0001.initial-schema" in message
    assert "pg-reset" in message or "backup" in message

    # The refusal left the pool intact.
    assert await current_schema_version(dsn) == HEAD

    # Now put 0001 in as the head for real (a legal rollback of everything
    # above it), and confirm count=1 refuses it exactly the same way.
    rolled = await _guarded_rollback(dsn, dim, count=len(CHAIN) - 1)
    assert rolled == list(reversed(CHAIN[1:]))
    assert await current_schema_version(dsn) == "0001.initial-schema"

    with pytest.raises(RuntimeError) as excinfo:
        await _guarded_rollback(dsn, dim, count=1)
    message = str(excinfo.value)
    assert "0001.initial-schema" in message
    assert "pg-reset" in message or "backup" in message

    assert await current_schema_version(dsn) == "0001.initial-schema"


async def test_migrate_fails_loudly_on_dim_mismatch() -> None:
    """ADR 0015: a pool provisioned at a different dim is a deployment
    error. The guard fires under the lock (so racing replicas cannot both
    pass it) but before any DDL, so there is no partial state, and the
    lock is released again when it fails.
    """
    dsn, dim = _dsn(), Settings().embedding_dim
    await _require_postgres(dsn)
    await _reset_chain(dsn)
    await migrate(dsn, dim)

    pool_dim = await current_embedding_dim(dsn)
    assert pool_dim is not None
    other_dim = 1024 if pool_dim != 1024 else 512

    with pytest.raises(RuntimeError) as excinfo:
        await migrate(dsn, other_dim)
    message = str(excinfo.value)
    assert f"{pool_dim}-dim" in message
    assert str(other_dim) in message
    assert "HIVEMIND_EMBEDDING_DIM" in message
    assert "pg-reset" in message

    # No partial state, and the lock is released after the failure.
    assert await current_embedding_dim(dsn) == pool_dim
    conn = await asyncpg.connect(dsn)
    try:
        held = await conn.fetchval(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
            "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
        )
        assert held == 0
    finally:
        await conn.close()


async def test_0015_adds_the_explore_counter_over_existing_search_counts() -> None:
    """ADR 0078: rows counted before 0015 keep their searches and empty
    counts and start at zero explore searches; the CHECK is in force."""
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at_head(dsn, dim)
    await rollback(dsn, dim, count=11)
    assert await current_schema_version(dsn) == "0014.pin-audit"

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO search_counts (fleet_id, searches, empty) VALUES ('', 5, 2), ('f1', 1, 0)"
        )
    finally:
        await conn.close()

    await migrate(dsn, dim)
    assert await current_schema_version(dsn) == HEAD
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT fleet_id, searches, empty, explore FROM search_counts ORDER BY fleet_id"
        )
        assert [tuple(r) for r in rows] == [("", 5, 2, 0), ("f1", 1, 0, 0)]
        with pytest.raises(asyncpg.CheckViolationError):  # never more explore than searches
            await conn.execute("UPDATE search_counts SET explore = 6 WHERE fleet_id = ''")
        await conn.execute("DELETE FROM search_counts")
    finally:
        await conn.close()


async def test_0006_backfills_revoked_and_rolls_back_without_loss() -> None:
    """ADR 0028: an `active` agent with no agent credential is a pre-0006
    revocation, so 0006 marks it `revoked`; an active agent that holds a
    key stays `active`. The rollback maps `revoked` back to the pre-0006
    shape (`active`, no key) and destroys no row."""
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at_head(dsn, dim)
    await rollback(dsn, dim, count=20)  # 0026, 0025, 0024 down to 0006
    assert await current_schema_version(dsn) == "0005.audit-log"

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO agents (name, status, trust_level) VALUES "
            "('keyed', 'active', 2), ('keyless', 'active', 2), ('waiting', 'pending', 0)"
        )
        await conn.execute(
            "INSERT INTO credentials (key_hash, kind, user_id, agent_id, agent_name) "
            "VALUES ('h-keyed', 'agent', 'keyed', 'keyed', 'keyed')"
        )
    finally:
        await conn.close()

    await migrate(dsn, dim)  # applies 0006 to 0024
    statuses = "SELECT name, status FROM agents ORDER BY name"
    conn = await asyncpg.connect(dsn)
    try:
        assert [tuple(r) for r in await conn.fetch(statuses)] == [
            ("keyed", "active"),
            ("keyless", "revoked"),
            ("waiting", "pending"),
        ]
        await conn.execute("UPDATE agents SET status = 'revoked' WHERE name = 'waiting'")
    finally:
        await conn.close()

    await rollback(
        dsn, dim, count=20
    )  # 0026, 0025, 0024, 0023, 0022, 0021, 0020, 0019, 0017, 0016, 0015, 0014, 0013, 0012, 0011, 0010, 0009, 0008, 0007, then 0006
    conn = await asyncpg.connect(dsn)
    try:
        assert [tuple(r) for r in await conn.fetch(statuses)] == [
            ("keyed", "active"),
            ("keyless", "active"),
            ("waiting", "active"),
        ]
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE agents SET status = 'revoked' WHERE name = 'keyed'")
        await conn.execute("DELETE FROM credentials")
        await conn.execute("DELETE FROM agents")
    finally:
        await conn.close()


async def test_0007_dedupes_keeping_the_newest_then_enforces_uniqueness() -> None:
    """ADR 0039: duplicate live keys (the pre-0007 race) are resolved
    deterministically before the unique indexes go on: per agent name and
    for the org key the NEWEST row survives, older ones are deleted.
    Legacy ``user`` / name-less rows are untouched. The rollback drops only
    the indexes."""
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at_head(dsn, dim)
    await rollback(
        dsn, dim, count=19
    )  # 0026, 0025, 0024, 0023, 0022, 0021, 0020, 0019, 0017, 0016, 0015, 0014, 0013, 0012, 0011, 0010, 0009, 0008 (indexes) then 0007
    assert await current_schema_version(dsn) == "0006.agent-revoked-status"

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO credentials (key_hash, kind, user_id, agent_id, agent_name, created_at) "
            "VALUES ('a-old', 'agent', 'bob', 'bob', 'bob', '2026-01-01'), "
            "('a-new', 'agent', 'bob', 'bob', 'bob', '2026-02-01'), "
            "('c-only', 'agent', 'carol', 'carol', 'carol', '2026-01-01'), "
            "('tie-a', 'agent', 'dan', 'dan', 'dan', '2026-03-01'), "
            "('tie-b', 'agent', 'dan', 'dan', 'dan', '2026-03-01')"
        )
        await conn.execute(
            "INSERT INTO credentials (key_hash, kind, user_id, created_at) VALUES "
            "('o-old', 'org', 'org', '2026-01-01'), ('o-new', 'org', 'org', '2026-02-01'), "
            "('u1', 'user', 'legacy', '2026-01-01'), ('u2', 'user', 'legacy', '2026-01-02')"
        )
    finally:
        await conn.close()

    await migrate(dsn, dim)
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch("SELECT key_hash FROM credentials ORDER BY key_hash")
        assert [r["key_hash"] for r in rows] == ["a-new", "c-only", "o-new", "tie-b", "u1", "u2"]
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO credentials (key_hash, kind, user_id, agent_name) "
                "VALUES ('x', 'agent', 'carol', 'carol')"
            )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO credentials (key_hash, kind, user_id) VALUES ('y', 'org', 'o')"
            )
    finally:
        await conn.close()

    await rollback(
        dsn, dim, count=19
    )  # 0026, 0025, 0024, 0023, 0022, 0021, 0020, 0019, 0017, 0016, 0015, 0014, 0013, 0012, 0011, 0010, 0009, 0008 (indexes) then 0007
    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SELECT to_regclass('credentials_one_agent_key')") is None
        assert await conn.fetchval("SELECT to_regclass('credentials_one_org_key')") is None
        assert await conn.fetchval("SELECT count(*) FROM credentials") == 6
    finally:
        await conn.close()
    await migrate(dsn, dim)


async def _backend_count(dsn: str, application: str) -> int:
    """Open backends that identify themselves as ``application`` (set through
    the DSN), so another client of the database does not change the count."""
    conn = await asyncpg.connect(dsn)
    try:
        return int(
            await conn.fetchval(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE backend_type = 'client backend' AND application_name = $1",
                application,
            )
        )
    finally:
        await conn.close()


async def test_migrate_and_rollback_leave_no_connection_open() -> None:
    """yoyo's backend holds a psycopg connection and has no close(); the
    runner must close it, or every migrate() call (each pod start, every
    integration fixture) leaks one connection until the process exits."""
    plain, dim = _dsn(), Settings().embedding_dim
    await _require_postgres(plain)
    # Empty tables first: an earlier test on this worker's database can leave a
    # `schema.rollback` row, and the rollback below (which steps over 0027)
    # would then be refused without --discard-audit-rows.
    await _start_at_head(plain, dim)
    application = f"hm-migration-test-{os.getpid()}"
    dsn = f"{plain}{'&' if '?' in plain else '?'}application_name={application}"
    for _ in range(3):
        await migrate(dsn, dim)
    await rollback(dsn, dim, count=1)
    await migrate(dsn, dim)
    assert await _backend_count(plain, application) == 0


async def test_migrate_fails_loudly_on_an_invalid_hnsw_index() -> None:
    """A crashed CREATE INDEX CONCURRENTLY leaves the index INVALID; the
    migration's IF NOT EXISTS skips it, so migrate must detect it."""
    dsn, dim = _dsn(), Settings().embedding_dim
    await _require_postgres(dsn)
    await _reset_chain(dsn)
    await migrate(dsn, dim)

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "UPDATE pg_index SET indisvalid = false "
            "WHERE indexrelid = 'entries_embedding_hnsw_idx'::regclass"
        )
    finally:
        await conn.close()

    with pytest.raises(RuntimeError, match=r"INVALID.*REINDEX INDEX CONCURRENTLY"):
        await migrate(dsn, dim)

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("REINDEX INDEX CONCURRENTLY entries_embedding_hnsw_idx")
    finally:
        await conn.close()
    await migrate(dsn, dim)  # repaired: clean again


async def test_migrate_rejects_an_unindexable_dim_before_any_ddl() -> None:
    dsn = _dsn()
    await _require_postgres(dsn)
    await _reset_chain(dsn)
    with pytest.raises(RuntimeError, match="2000"):
        await migrate(dsn, 3072)
    assert await current_schema_version(dsn) is None


async def test_lost_lock_connection_does_not_mask_the_migration_result(monkeypatch) -> None:
    """If the lock connection dies mid-run the unlock must not raise an
    InterfaceError over the real outcome."""
    from hivemind.store import migrate as m

    dsn, dim = _dsn(), Settings().embedding_dim
    await _require_postgres(dsn)
    await _reset_chain(dsn)

    real_apply = m._apply_chain

    def apply_then_kill_lock(dsn_: str, dim_: int) -> None:
        real_apply(dsn_, dim_)
        import psycopg

        with psycopg.connect(dsn_, autocommit=True) as c:
            c.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_locks WHERE locktype = 'advisory' "
                "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
                "AND pid <> pg_backend_pid()"
            )

    monkeypatch.setattr(m, "_apply_chain", apply_then_kill_lock)
    with pytest.raises(RuntimeError, match="lock connection was lost"):
        await migrate(dsn, dim)
    monkeypatch.undo()
    await migrate(dsn, dim)
    assert await current_schema_version(dsn) == HEAD


async def test_a_rollback_that_lost_its_lock_says_so(monkeypatch, caplog) -> None:
    """As for apply: without the lock another replica may have applied the
    chain during the rollback, so success cannot be reported. The error
    names what was rolled back, and nothing advises re-applying it."""
    from hivemind.store import migrate as m

    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at_head(dsn, dim)

    real_rollback = m._rollback_chain

    def rollback_then_kill_lock(dsn_: str, dim_: int, count_: int) -> list[str]:
        rolled = real_rollback(dsn_, dim_, count_)
        import psycopg

        with psycopg.connect(dsn_, autocommit=True) as c:
            c.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_locks WHERE locktype = 'advisory' "
                "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
                "AND pid <> pg_backend_pid()"
            )
        return rolled

    monkeypatch.setattr(m, "_rollback_chain", rollback_then_kill_lock)
    with pytest.raises(RuntimeError, match="lost during the rollback") as exc:
        await _guarded_rollback(dsn, dim, count=1)
    assert HEAD in str(exc.value)
    assert "Re-run `hivemind-migrate`" not in caplog.text
    monkeypatch.undo()
    await migrate(dsn, dim)
    assert await current_schema_version(dsn) == HEAD


async def test_0016_backfills_withdrawn_entries_and_rolls_back_without_loss() -> None:
    """ADR 0098: an entry withdrawn before 0016 gets the migration's time (so a
    helpful rating older than that lists it once); active and superseded
    entries stay NULL; the rollback drops only the column."""
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at_head(dsn, dim)
    await rollback(
        dsn, dim, count=10
    )  # 0026, 0025, 0024, 0023, 0022, 0021, 0020, 0019, 0017, then 0016
    assert await current_schema_version(dsn) == "0015.search-counts-explore"

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO entries (kind, summary, author, agent, state, withdrawn_reason, occurred_at) "
            "VALUES ('fact', 'gone', 'a', 'x', 'withdrawn', 'obsolete', now()), "
            "('fact', 'live', 'a', 'x', 'active', NULL, now())"
        )
    finally:
        await conn.close()

    await migrate(dsn, dim)
    assert await current_schema_version(dsn) == HEAD
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch("SELECT summary, withdrawn_at FROM entries ORDER BY summary")
        by_summary = {r["summary"]: r["withdrawn_at"] for r in rows}
        assert by_summary["gone"] is not None and by_summary["live"] is None
    finally:
        await conn.close()

    await rollback(dsn, dim, count=10)
    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SELECT count(*) FROM entries") == 2  # no row lost
        row = await conn.fetchrow("SELECT withdrawn_reason FROM entries WHERE summary = 'gone'")
        assert row is not None and row["withdrawn_reason"] == "obsolete"
        await conn.execute("DELETE FROM entries")
    finally:
        await conn.close()
    await migrate(dsn, dim)


async def test_0017_adds_the_merge_columns_and_tables_and_rolls_back_without_losing_fleets() -> (
    None
):
    """ADR 0107: existing fleets get NULL ``merged_into`` / ``merged_at`` (not
    merged); the audit CHECK takes the two new actions; the rollback drops
    the columns and the record tables and keeps every fleet and entry."""
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at_head(dsn, dim)
    await rollback(dsn, dim, count=9)  # 0026, 0025, 0024, 0023, 0022, 0021, 0020, 0019, then 0017
    assert await current_schema_version(dsn) == "0016.entry-withdrawn-at"

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("INSERT INTO fleets (name) VALUES ('old')")
    finally:
        await conn.close()

    await migrate(dsn, dim)
    assert await current_schema_version(dsn) == HEAD
    conn = await asyncpg.connect(dsn)
    try:
        row = await conn.fetchrow("SELECT merged_into, merged_at FROM fleets WHERE name = 'old'")
        assert row is not None and (row["merged_into"], row["merged_at"]) == (None, None)
        merge_id = await conn.fetchval(
            "INSERT INTO fleet_merges (source_fleet_id, target_fleet_id, merged_by) "
            "VALUES (gen_random_uuid(), gen_random_uuid(), 'admin:1') RETURNING id"
        )
        await conn.execute(
            "INSERT INTO fleet_merge_entries (merge_id, entry_id) VALUES ($1, gen_random_uuid())",
            merge_id,
        )
        for action in ("fleet.merge", "fleet.unmerge"):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action) "
                "VALUES ('admin_key', 'admin:1', $1)",
                action,
            )
        defaults = await conn.fetchrow(
            "SELECT moved_agents, dropped_pins, undone_at FROM fleet_merges WHERE id = $1", merge_id
        )
        assert defaults is not None
        assert (
            list(defaults["moved_agents"]),
            defaults["dropped_pins"],
            defaults["undone_at"],
        ) == (
            [],
            "[]",
            None,
        )
    finally:
        await conn.close()

    await rollback(
        dsn, dim, count=9, discard_audit_rows=True
    )  # 0026, 0025, 0024, 0023, 0022, 0021, 0020, 0019, then 0017
    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SELECT count(*) FROM fleets") == 1
        assert await conn.fetchval("SELECT count(*) FROM audit_log") == 0
        await conn.execute("DELETE FROM fleets")
    finally:
        await conn.close()
    await migrate(dsn, dim)


async def test_0020_takes_the_key_replace_action_and_rolls_back_dropping_only_its_rows() -> None:
    """ADR 0108: the audit CHECK takes `agent_key.replace` next to everything
    0019 allowed; the rollback deletes those rows, keeps every other audit row
    and credential, and narrows the CHECK to 0019's list (`fleet.rename` stays)."""
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at_head(dsn, dim)
    await rollback(dsn, dim, count=6)  # 0026, 0025, 0024, 0023, 0022, then 0021
    assert await current_schema_version(dsn) == "0020.agent-key-replace"

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO audit_log (actor_kind, actor, action, target, detail) VALUES "
            "('admin_key', 'admin:1', 'agent_key.replace', 'a', "
            '\'{"old_key": "aaaaaaaaaaaa", "new_key": "bbbbbbbbbbbb"}\'), '
            "('admin_key', 'admin:1', 'fleet.rename', 'f', '{}'), "
            "('admin_key', 'admin:1', 'agent.revoke', 'a', '{}')"
        )
        await conn.execute(
            "INSERT INTO credentials (key_hash, kind, user_id, agent_id, agent_name) "
            "VALUES ('h-a', 'agent', 'a', 'a', 'a')"
        )
    finally:
        await conn.close()

    assert await rollback(dsn, dim, count=1, discard_audit_rows=True) == ["0020.agent-key-replace"]
    conn = await asyncpg.connect(dsn)
    try:
        assert [
            r["action"] for r in await conn.fetch("SELECT action FROM audit_log ORDER BY action")
        ] == [
            "agent.revoke",
            "fleet.rename",
        ]
        assert await conn.fetchval("SELECT count(*) FROM credentials") == 1
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action) "
                "VALUES ('admin_key', 'admin:1', 'agent_key.replace')"
            )
        await conn.execute("DELETE FROM audit_log")
        await conn.execute("DELETE FROM credentials")
    finally:
        await conn.close()
    await migrate(dsn, dim)


async def test_0022_adds_the_set_aside_mark_and_the_cleanup_table_and_rolls_back_keeping_ratings() -> (
    None
):
    """ADR 0111: ratings written before 0022 are not set aside (NULL mark) and
    keep counting; the audit CHECK takes the two new actions; the rollback drops
    the mark and the record table and keeps every rating."""
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at_head(dsn, dim)
    await rollback(dsn, dim, count=5)  # 0026, 0025, 0024, 0023, then 0022
    assert await current_schema_version(dsn) == "0021.credential-last-used"

    entry = "00000000-0000-0000-0000-000000000002"
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO entries (id, kind, summary, author, agent, occurred_at) "
            "VALUES ($1, 'fact', 'e', 'good', 'x', now())",
            entry,
        )
        await conn.execute(
            "INSERT INTO feedbacks (entry_id, \"user\", agent, verdict) VALUES ($1, 'bad', 'x', 'wrong')",
            entry,
        )
    finally:
        await conn.close()

    await migrate(dsn, dim)
    assert await current_schema_version(dsn) == HEAD
    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SELECT set_aside_by FROM feedbacks") is None
        cleanup_id = await conn.fetchval(
            "INSERT INTO agent_cleanups (agent, since, until, cleaned_by) "
            "VALUES ('bad', now(), now(), 'admin:1') RETURNING id"
        )
        defaults = await conn.fetchrow(
            "SELECT withdrawn_ids, restored, ratings_set_aside, unpinned_pins, revoked, undone_at "
            "FROM agent_cleanups WHERE id = $1",
            cleanup_id,
        )
        assert defaults is not None
        assert (
            list(defaults["withdrawn_ids"]),
            defaults["restored"],
            defaults["ratings_set_aside"],
            defaults["unpinned_pins"],
            defaults["revoked"],
            defaults["undone_at"],
        ) == ([], "[]", 0, "[]", False, None)
        await conn.execute("UPDATE feedbacks SET set_aside_by = $1", cleanup_id)
        for action in ("agent.cleanup", "agent.cleanup_undo"):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action) "
                "VALUES ('admin_key', 'admin:1', $1)",
                action,
            )
    finally:
        await conn.close()

    await rollback(dsn, dim, count=5, discard_audit_rows=True)  # 0026, 0025, 0024, 0023, then 0022
    assert await current_schema_version(dsn) == "0021.credential-last-used"
    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SELECT count(*) FROM feedbacks") == 1  # the rating is kept
        assert await conn.fetchval("SELECT count(*) FROM audit_log") == 0
        await conn.execute("DELETE FROM feedbacks")
        await conn.execute("DELETE FROM entries")
    finally:
        await conn.close()
    await migrate(dsn, dim)


async def test_0021_adds_a_nullable_last_use_and_rolls_back_without_touching_keys() -> None:
    """ADR 0110: an active agent's existing key gets the migration's time as
    ``last_used_at`` (a grace period); other keys stay NULL; the rollback
    drops only the column and keeps every key."""
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at_head(dsn, dim)
    await rollback(dsn, dim, count=6)  # 0026, 0025, 0024, 0023, 0022, then 0021
    assert await current_schema_version(dsn) == "0020.agent-key-replace"

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO agents (name, status, trust_level) VALUES "
            "('live', 'active', 1), ('waiting', 'pending', 0)"
        )
        await conn.execute(
            "INSERT INTO credentials (key_hash, kind, user_id, agent_id, agent_name) "
            "VALUES ('h-wait', 'agent', 'waiting', 'waiting', 'waiting')"
        )
        await conn.execute(
            "INSERT INTO credentials (key_hash, kind, user_id, agent_id, agent_name) "
            "VALUES ('h-live', 'agent', 'live', 'live', 'live')"
        )
        await conn.execute(
            "INSERT INTO credentials (key_hash, kind, user_id) VALUES ('h-old', 'admin', 'admin')"
        )
    finally:
        await conn.close()

    await migrate(dsn, dim)  # applies 0021 up to the head
    assert await current_schema_version(dsn) == HEAD
    conn = await asyncpg.connect(dsn)
    try:
        stamps = {
            r["key_hash"]: r["last_used_at"]
            for r in await conn.fetch("SELECT key_hash, last_used_at FROM credentials")
        }
        # An active agent's key starts at the upgrade; other kinds stay NULL.
        assert stamps["h-live"] is not None
        assert stamps["h-old"] is None
        assert stamps["h-wait"] is None  # a key of a non-active agent
    finally:
        await conn.close()

    await rollback(dsn, dim, count=6)  # 0026, 0025, 0024, 0023, 0022, then 0021
    assert await current_schema_version(dsn) == "0020.agent-key-replace"
    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SELECT count(*) FROM credentials") == 3  # no key lost
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'credentials' AND column_name = 'last_used_at'"
            )
            == 0
        )
        await conn.execute("DELETE FROM credentials")
        await conn.execute("DELETE FROM agents")
    finally:
        await conn.close()
    await migrate(dsn, dim)


async def test_0023_adds_the_move_kind_and_rolls_back_keeping_merge_records() -> None:
    """ADR 0112: merge records written before 0023 read as kind ``merge``; the
    kind CHECK and the audit CHECK take the new values (the cleanup actions of
    0022 stay allowed); the rollback removes the move records (and their entry
    lists) and the two move audit actions and keeps every merge record, the
    cleanup records and the earlier audit actions."""
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at_head(dsn, dim)
    await rollback(dsn, dim, count=4)  # 0026, 0025, 0024, then 0023
    assert await current_schema_version(dsn) == "0022.agent-cleanup"

    conn = await asyncpg.connect(dsn)
    try:
        old_merge = await conn.fetchval(
            "INSERT INTO fleet_merges (source_fleet_id, target_fleet_id, merged_by) "
            "VALUES (gen_random_uuid(), gen_random_uuid(), 'admin:1') RETURNING id"
        )
    finally:
        await conn.close()

    await migrate(dsn, dim)
    assert await current_schema_version(dsn) == HEAD
    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SELECT kind FROM fleet_merges WHERE id = $1", old_merge) == (
            "merge"
        )
        move = await conn.fetchval(
            "INSERT INTO fleet_merges (source_fleet_id, target_fleet_id, merged_by, kind) "
            "VALUES (gen_random_uuid(), gen_random_uuid(), 'admin:1', 'move') RETURNING id"
        )
        await conn.execute(
            "INSERT INTO fleet_merge_entries (merge_id, entry_id) VALUES ($1, gen_random_uuid())",
            move,
        )
        await conn.execute(
            "INSERT INTO agent_cleanups (agent, since, until, cleaned_by) "
            "VALUES ('bad', now(), now(), 'admin:1')"
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO fleet_merges (source_fleet_id, target_fleet_id, merged_by, kind) "
                "VALUES (gen_random_uuid(), gen_random_uuid(), 'admin:1', 'split')"
            )
        for action in (
            "fleet.move_agents",
            "fleet.move_undo",
            "fleet.rename",
            "agent_key.replace",
            "agent.cleanup",
            "agent.cleanup_undo",
        ):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action) "
                "VALUES ('admin_key', 'admin:1', $1)",
                action,
            )
    finally:
        await conn.close()

    await rollback(dsn, dim, count=4, discard_audit_rows=True)  # 0026, 0025, 0024, then 0023
    assert await current_schema_version(dsn) == "0022.agent-cleanup"
    conn = await asyncpg.connect(dsn)
    try:
        assert [str(r["id"]) for r in await conn.fetch("SELECT id FROM fleet_merges")] == [
            str(old_merge)
        ]
        assert await conn.fetchval("SELECT count(*) FROM fleet_merge_entries") == 0
        assert await conn.fetchval("SELECT count(*) FROM agent_cleanups") == 1
        assert sorted(r["action"] for r in await conn.fetch("SELECT action FROM audit_log")) == [
            "agent.cleanup",
            "agent.cleanup_undo",
            "agent_key.replace",
            "fleet.rename",
        ]
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action) "
                "VALUES ('admin_key', 'admin:1', 'fleet.move_agents')"
            )
        await conn.execute("DELETE FROM audit_log")
        await conn.execute("DELETE FROM fleet_merges")
        await conn.execute("DELETE FROM agent_cleanups")
    finally:
        await conn.close()
    await migrate(dsn, dim)


async def test_0024_adds_successions_and_the_retire_audit_actions() -> None:
    """ADR 0113: one succession per retired agent, never to itself; the audit
    CHECK takes the two new actions next to everything 0023 allowed; the
    rollback drops the table and those rows, keeps every agent and every
    other audit row (the cleanup and move actions of 0022 and 0023 stay), and
    narrows the CHECK back to 0023's list."""
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at_head(dsn, dim)
    assert await current_schema_version(dsn) == HEAD
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO agents (name, status, trust_level) VALUES ('a', 'revoked', 2)"
        )
        await conn.execute(
            'INSERT INTO successions (retired_agent, successor, "by") '
            "VALUES ('a', 'b', 'admin:1')"
        )
        row = await conn.fetchrow("SELECT at FROM successions WHERE retired_agent = 'a'")
        assert row is not None and row["at"] is not None
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO successions (retired_agent, successor, \"by\") VALUES ('a', 'c', 'x')"
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO successions (retired_agent, successor, \"by\") VALUES ('z', 'z', 'x')"
            )
        for action in (
            "agent.retire",
            "agent.retire_undo",
            "agent.cleanup",
            "agent.cleanup_undo",
            "fleet.move_agents",
            "fleet.move_undo",
        ):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action) "
                "VALUES ('admin_key', 'admin:1', $1)",
                action,
            )
    finally:
        await conn.close()
    assert await rollback(dsn, dim, count=3, discard_audit_rows=True) == [
        ID_0026,
        ID_0025,
        PREV,
    ]  # 0026, 0025, then 0024
    assert await current_schema_version(dsn) == "0023.fleet-move"
    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SELECT to_regclass('successions')") is None
        assert sorted(r["action"] for r in await conn.fetch("SELECT action FROM audit_log")) == [
            "agent.cleanup",
            "agent.cleanup_undo",
            "fleet.move_agents",
            "fleet.move_undo",
        ]
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action) "
                "VALUES ('admin_key', 'admin:1', 'agent.retire')"
            )
        await conn.execute("DELETE FROM audit_log")
        assert await conn.fetchval("SELECT count(*) FROM agents") == 1
        await conn.execute("DELETE FROM agents")
    finally:
        await conn.close()
    await migrate(dsn, dim)


async def test_0025_adds_a_nullable_lock_time_and_rolls_back_without_touching_keys() -> None:
    """ADR 0117: existing keys read as unlocked (NULL); the audit CHECK takes
    ``agent_key.lock`` and keeps the earlier actions; the rollback drops only the
    column and the lock audit rows and keeps every key."""
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at_head(dsn, dim)
    assert await rollback(dsn, dim, count=2) == [ID_0026, ID_0025]
    assert await current_schema_version(dsn) == PREV

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO credentials (key_hash, kind, user_id, agent_id, agent_name) "
            "VALUES ('h-old', 'agent', 'a', 'a', 'a')"
        )
    finally:
        await conn.close()

    await migrate(dsn, dim)
    assert await current_schema_version(dsn) == HEAD
    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SELECT locked_at FROM credentials") is None
        await conn.execute("UPDATE credentials SET locked_at = now()")
        for action in ("agent_key.lock", "agent_key.replace", "agent.retire"):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action) VALUES ('agent_key', 'a', $1)",
                action,
            )
    finally:
        await conn.close()

    assert await rollback(dsn, dim, count=2, discard_audit_rows=True, unlock_locked_keys=True) == [
        ID_0026,
        ID_0025,
    ]
    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SELECT count(*) FROM credentials") == 1  # no key lost
        assert sorted(r["action"] for r in await conn.fetch("SELECT action FROM audit_log")) == [
            "agent.retire",
            "agent_key.replace",
        ]
        await conn.execute("DELETE FROM credentials")
        await conn.execute("DELETE FROM audit_log")
    finally:
        await conn.close()
    await migrate(dsn, dim)


# ADR 0119: the rollback guard and the `schema.rollback` row.
# These start at 0027 (see `_start_at`): the migrations above it are rolled back first.

_ID_0006 = "0006.agent-revoked-status"


async def _audit_actions(dsn: str) -> list[str]:
    conn = await asyncpg.connect(dsn)
    try:
        return [
            r["action"] for r in await conn.fetch("SELECT action FROM audit_log ORDER BY action")
        ]
    finally:
        await conn.close()


async def _insert_audit(dsn: str, *actions: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        for action in actions:
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action, target) "
                "VALUES ('admin_key', 'admin:1', $1, 't')",
                action,
            )
    finally:
        await conn.close()


async def test_rollback_refuses_to_delete_audit_rows_without_the_flag() -> None:
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at(dsn, dim, ID_0027)
    await _insert_audit(dsn, "agent_key.lock", "agent_key.lock", "fleet.merge")

    # 0027, 0026, 0025: 0025's rollback would delete the two key-lock rows.
    with pytest.raises(RollbackRefused) as exc:
        await _guarded_rollback(dsn, dim, count=3)
    message = str(exc.value)
    assert ID_0025 in message
    assert "agent_key.lock" in message
    assert "2 audit row" in message
    assert "--discard-audit-rows" in message
    assert "fleet.merge" not in message  # 0017 is not in this chain

    # Nothing changed: same head, every row still there.
    assert await current_schema_version(dsn) == ID_0027
    assert await _audit_actions(dsn) == ["agent_key.lock", "agent_key.lock", "fleet.merge"]

    # A chain that deletes nothing is not refused.
    assert await _guarded_rollback(dsn, dim, count=2) == [ID_0027, ID_0026]
    assert await current_schema_version(dsn) == ID_0025


async def test_rollback_with_the_flag_discards_the_rows_and_warns_past_0027(
    caplog: pytest.LogCaptureFixture,
) -> None:
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at(dsn, dim, ID_0027)
    await _insert_audit(dsn, "agent_key.lock", "agent_key.lock", "fleet.merge")

    with caplog.at_level(logging.WARNING, logger="hivemind.store.migrate"):
        rolled = await _guarded_rollback(dsn, dim, count=3, discard_audit_rows=True)
    assert rolled == [ID_0027, ID_0026, ID_0025]
    assert await current_schema_version(dsn) == PREV
    # Only the rolled-back actions went; 0027 itself is gone, so no audit row.
    assert await _audit_actions(dsn) == ["fleet.merge"]
    assert "discarded_audit_rows" in caplog.text
    assert '"discarded_audit_rows": 2' in caplog.text


async def test_rolling_back_0027_is_guarded_by_its_own_rows() -> None:
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at(dsn, dim, ID_0027)
    await _insert_audit(dsn, "schema.rollback")

    with pytest.raises(RollbackRefused, match=r"schema\.rollback"):
        await _guarded_rollback(dsn, dim, count=1)
    assert await current_schema_version(dsn) == ID_0027

    assert await _guarded_rollback(dsn, dim, count=1, discard_audit_rows=True) == [ID_0027]
    assert await _audit_actions(dsn) == []
    conn = await asyncpg.connect(dsn)
    try:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action) "
                "VALUES ('cli', 'x', 'schema.rollback')"
            )
    finally:
        await conn.close()


async def test_a_rollback_that_leaves_0027_applied_writes_a_schema_rollback_row() -> None:
    """The row is written when a rollback leaves `0027` applied: rolling back
    `0029` alone does that, with its real rollback."""
    import json

    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at_head(dsn, dim)

    assert await rollback_via_runner(dsn, dim) == [HEAD]
    await ensure_migrated(dsn, dim)
    assert await rollback_via_runner(dsn, dim, discard=True) == [HEAD]

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT actor_kind, actor, action, target, detail::text AS detail FROM audit_log"
        )
    finally:
        await conn.close()
    assert [r["action"] for r in rows] == ["schema.rollback", "schema.rollback"]
    assert {(r["actor_kind"], r["target"]) for r in rows} == {("cli", HEAD)}
    assert all(r["actor"] for r in rows)

    details = sorted((json.loads(r["detail"]) for r in rows), key=len)
    assert details == [
        {"rolled_back": [HEAD]},
        {"rolled_back": [HEAD], "discarded_audit_rows": 0},
    ]


async def rollback_via_runner(dsn: str, dim: int, *, discard: bool = False) -> list[str]:
    return await _guarded_rollback(dsn, dim, count=1, discard_audit_rows=discard)


async def test_rollback_refuses_to_revive_the_keys_of_revoked_agents() -> None:
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at(dsn, dim, ID_0027)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO agents (name, status, trust_level) VALUES "
            "('gone', 'revoked', 2), ('live', 'active', 2)"
        )
        await conn.execute(
            "INSERT INTO credentials (key_hash, kind, user_id, agent_id, agent_name) VALUES "
            "('h-gone', 'agent', 'gone', 'gone', 'gone'), "
            "('h-live', 'agent', 'live', 'live', 'live')"
        )
    finally:
        await conn.close()
    count = CHAIN.index(ID_0027) + 1 - CHAIN.index(_ID_0006)  # nothing above 0027 is applied here

    with pytest.raises(RollbackRefused) as exc:
        await _guarded_rollback(dsn, dim, count=count, discard_audit_rows=True)
    assert _ID_0006 in str(exc.value)
    assert "1 credential row" in str(exc.value)
    assert "--revive-revoked-keys" in str(exc.value)
    assert await current_schema_version(dsn) == ID_0027

    # A chain above 0006 does not look at agents at all.
    assert await _guarded_rollback(dsn, dim, count=1) == [ID_0027]

    rolled = await _guarded_rollback(
        dsn, dim, count=count - 1, discard_audit_rows=True, revive_revoked_keys=True
    )
    assert rolled[-1] == _ID_0006
    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SELECT status FROM agents WHERE name = 'gone'") == "active"
        await conn.execute("DELETE FROM credentials")
        await conn.execute("DELETE FROM agents")
    finally:
        await conn.close()


async def test_rollback_refuses_to_unlock_locked_keys_without_the_flag() -> None:
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at(dsn, dim, ID_0027)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO agents (name, status, trust_level) VALUES ('a', 'active', 2)"
        )
        await conn.execute(
            "INSERT INTO credentials (key_hash, kind, user_id, agent_id, agent_name, locked_at) "
            "VALUES ('h-locked', 'agent', 'a', 'a', 'a', now())"
        )
    finally:
        await conn.close()

    with pytest.raises(RollbackRefused) as exc:
        await _guarded_rollback(dsn, dim, count=3)  # 0027, 0026, 0025
    assert ID_0025 in str(exc.value)
    assert "1 locked key" in str(exc.value)
    assert "--unlock-locked-keys" in str(exc.value)
    assert await current_schema_version(dsn) == ID_0027

    # A chain that stops above 0025 does not count them.
    assert await _guarded_rollback(dsn, dim, count=2) == [ID_0027, ID_0026]
    assert await _guarded_rollback(dsn, dim, count=1, unlock_locked_keys=True) == [ID_0025]
    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SELECT count(*) FROM credentials") == 1  # the key is kept
    finally:
        await conn.close()


# ADR 0125: 0032 adds the redaction marker and the `entry.redact` audit action.


async def test_0032_adds_the_redaction_marker_and_the_audit_action() -> None:
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at(dsn, dim, ID_0032)
    conn = await asyncpg.connect(dsn)
    try:
        column = await conn.fetchrow(
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = 'entries' AND column_name = 'redacted_at'"
        )
        assert column is not None
        assert (column["data_type"], column["is_nullable"], column["column_default"]) == (
            "timestamp with time zone",
            "YES",
            None,
        )
        assert (
            await conn.fetchval(
                "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = 'audit_log_redact_author_idx'"
            )
            is True
        )
        await conn.execute(
            "INSERT INTO audit_log (actor_kind, actor, action, target) "
            "VALUES ('admin_key', 'admin:1', 'entry.redact', 't')"
        )
    finally:
        await conn.close()


async def test_0032_rolls_back_refused_without_the_flag_and_never_restores_text() -> None:
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at(dsn, dim, ID_0032)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO entries (kind, summary, author, agent, state, withdrawn_reason, "
            "occurred_at, redacted_at) VALUES ('fact', '[redacted]', 'a', 'a', 'withdrawn', "
            "'[redacted]', now(), now())"
        )
    finally:
        await conn.close()
    await _insert_audit(dsn, "entry.redact", "entry.redact")

    with pytest.raises(RollbackRefused) as exc:
        await _guarded_rollback(dsn, dim, count=1)
    assert ID_0032 in str(exc.value)
    assert "entry.redact" in str(exc.value)
    assert "--discard-audit-rows" in str(exc.value)
    assert await current_schema_version(dsn) == ID_0032

    assert await _guarded_rollback(dsn, dim, count=1, discard_audit_rows=True) == [ID_0032]
    conn = await asyncpg.connect(dsn)
    try:
        # The marker is gone, the memory stays withdrawn with its stub (the
        # text is not coming back), and the older CHECK refuses the action.
        row = await conn.fetchrow(
            "SELECT state, summary, withdrawn_reason FROM entries WHERE author = 'a'"
        )
        assert row is not None
        assert (row["state"], row["summary"], row["withdrawn_reason"]) == (
            "withdrawn",
            "[redacted]",
            "[redacted]",
        )
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'entries' AND column_name = 'redacted_at'"
            )
            == 0
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action) "
                "VALUES ('cli', 'x', 'entry.redact')"
            )
    finally:
        await conn.close()
    await migrate(dsn, dim)


# ADR 0143: 0033 adds the fleet phase columns and the two phase audit actions.


async def test_0033_adds_the_phase_columns_with_their_checks() -> None:
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at(dsn, dim, ID_0033)
    conn = await asyncpg.connect(dsn)
    try:
        columns = await conn.fetch(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_name = 'fleets' "
            "AND column_name IN ('phase', 'phase_until') ORDER BY column_name"
        )
        assert [
            (c["column_name"], c["data_type"], c["is_nullable"], c["column_default"])
            for c in columns
        ] == [
            ("phase", "text", "YES", None),
            ("phase_until", "timestamp with time zone", "YES", None),
        ]
        await conn.execute("INSERT INTO fleets (name) VALUES ('plain')")
        await conn.execute(
            "INSERT INTO fleets (name, phase, phase_until) "
            "VALUES ('exploring', 'explore', now() + interval '1 day')"
        )
        for bad in (
            "INSERT INTO fleets (name, phase, phase_until) VALUES ('x', 'sprint', now())",
            "INSERT INTO fleets (name, phase) VALUES ('x', 'explore')",
            "INSERT INTO fleets (name, phase_until) VALUES ('x', now())",
        ):
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(bad)
        for action in ("fleet.phase_set", "fleet.phase_clear"):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action, target) "
                f"VALUES ('admin_key', 'admin:1', '{action}', 't')"
            )
        await conn.execute("DELETE FROM audit_log")
        await conn.execute("DELETE FROM fleets")
    finally:
        await conn.close()


async def test_0033_rolls_back_refused_without_the_flag_and_drops_the_columns() -> None:
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at(dsn, dim, ID_0033)
    await _insert_audit(dsn, "fleet.phase_set", "fleet.merge")

    with pytest.raises(RollbackRefused) as exc:
        await _guarded_rollback(dsn, dim, count=1)
    assert ID_0033 in str(exc.value)
    assert "fleet.phase_set" in str(exc.value)
    assert "--discard-audit-rows" in str(exc.value)
    assert await current_schema_version(dsn) == ID_0033

    assert await _guarded_rollback(dsn, dim, count=1, discard_audit_rows=True) == [ID_0033]
    assert await current_schema_version(dsn) == ID_0032
    assert await _audit_actions(dsn) == ["fleet.merge", "schema.rollback"]
    conn = await asyncpg.connect(dsn)
    try:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'fleets' AND column_name IN ('phase', 'phase_until')"
            )
            == 0
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action) "
                "VALUES ('cli', 'x', 'fleet.phase_clear')"
            )
    finally:
        await conn.close()
    await migrate(dsn, dim)


# ADR 0144: 0034 adds the agent role column and the two role audit actions.


async def test_0034_adds_the_role_column_with_its_check() -> None:
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at_head(dsn, dim)
    conn = await asyncpg.connect(dsn)
    try:
        column = await conn.fetchrow(
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = 'agents' AND column_name = 'role'"
        )
        assert column is not None
        assert (column["data_type"], column["is_nullable"], column["column_default"]) == (
            "text",
            "YES",
            None,
        )
        await conn.execute(
            "INSERT INTO agents (name, status, trust_level) VALUES ('a', 'pending', 0)"
        )
        await conn.execute("UPDATE agents SET role = 'scout' WHERE name = 'a'")
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE agents SET role = 'queen' WHERE name = 'a'")
        for action in ("agent.role_set", "agent.role_clear"):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action, target) "
                f"VALUES ('admin_key', 'admin:1', '{action}', 'a')"
            )
        await conn.execute("DELETE FROM audit_log")
        await conn.execute("DELETE FROM agents")
    finally:
        await conn.close()


async def test_0034_rolls_back_refused_without_the_flag_and_drops_the_column() -> None:
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at_head(dsn, dim)
    await _insert_audit(dsn, "agent.role_set", "fleet.phase_set")

    with pytest.raises(RollbackRefused) as exc:
        await _guarded_rollback(dsn, dim, count=1)
    assert HEAD in str(exc.value)
    assert "agent.role_set" in str(exc.value)
    assert "--discard-audit-rows" in str(exc.value)
    assert await current_schema_version(dsn) == HEAD

    assert await _guarded_rollback(dsn, dim, count=1, discard_audit_rows=True) == [HEAD]
    assert await current_schema_version(dsn) == ID_0033
    assert await _audit_actions(dsn) == ["fleet.phase_set", "schema.rollback"]
    conn = await asyncpg.connect(dsn)
    try:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'agents' AND column_name = 'role'"
            )
            == 0
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action) "
                "VALUES ('cli', 'x', 'agent.role_clear')"
            )
    finally:
        await conn.close()
    await migrate(dsn, dim)


async def _fleet_index_and_fillfactor(dsn: str) -> tuple[int, list[str] | None]:
    conn = await asyncpg.connect(dsn)
    try:
        indexes = await conn.fetchval(
            "SELECT count(*) FROM pg_indexes WHERE tablename = 'entries' "
            "AND indexname = 'entries_fleet_idx'"
        )
        options = await conn.fetchval(
            "SELECT reloptions FROM pg_class WHERE oid = 'public.entries'::regclass"
        )
        return int(indexes), options
    finally:
        await conn.close()


async def test_0028_drops_the_fleet_index_and_the_rollback_restores_it() -> None:
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at(dsn, dim, ID_0028)
    assert await _fleet_index_and_fillfactor(dsn) == (0, ["fillfactor=50"])

    # Rolling back 0028 leaves 0027 applied, so a `schema.rollback` row is written.
    assert await _guarded_rollback(dsn, dim, count=1) == [ID_0028]
    assert await current_schema_version(dsn) == ID_0027
    assert await _fleet_index_and_fillfactor(dsn) == (1, None)
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch("SELECT action, target FROM audit_log")
    finally:
        await conn.close()
    assert [(r["action"], r["target"]) for r in rows] == [("schema.rollback", ID_0028)]

    # A re-apply is clean, and so is a second drop / set on an applied state.
    await ensure_migrated(dsn, dim)
    assert await current_schema_version(dsn) == HEAD
    assert await _fleet_index_and_fillfactor(dsn) == (0, ["fillfactor=50"])


async def test_0029_widens_the_audit_check_and_rolls_back_to_0028() -> None:
    """ADR 0122: the audit CHECK takes `agent.owner_set` and keeps the earlier
    actions; the rollback deletes only those rows (behind the guard), leaves the
    owner aliases on the agent rows alone, and writes the `schema.rollback`
    row because `0027` stays applied (ADR 0119)."""
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at(dsn, dim, ID_0029)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO agents (name, owner_alias, status, trust_level) "
            "VALUES ('a', 'jane.doe', 'active', 2)"
        )
    finally:
        await conn.close()
    await _insert_audit(dsn, "agent.owner_set", "agent_key.lock")

    with pytest.raises(RollbackRefused, match=r"agent\.owner_set"):
        await _guarded_rollback(dsn, dim, count=1)
    assert await current_schema_version(dsn) == ID_0029

    assert await _guarded_rollback(dsn, dim, count=1, discard_audit_rows=True) == [ID_0029]
    assert await current_schema_version(dsn) == ID_0028
    assert await _audit_actions(dsn) == ["agent_key.lock", "schema.rollback"]
    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SELECT owner_alias FROM agents") == "jane.doe"
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action) "
                "VALUES ('admin_key', 'x', 'agent.owner_set')"
            )
        await conn.execute("DELETE FROM audit_log")
        await conn.execute("DELETE FROM agents")
    finally:
        await conn.close()
    await migrate(dsn, dim)
    await _insert_audit(dsn, "agent.owner_set")
    assert await _audit_actions(dsn) == ["agent.owner_set"]
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("DELETE FROM audit_log")
    finally:
        await conn.close()


async def test_rolling_back_0031_is_guarded_by_its_own_rows() -> None:
    """ADR 0124: the rollback drops the `entry.restore` audit rows its older
    CHECK cannot hold, so it needs `--discard-audit-rows`; the restored
    memories stay active. 0030 stays applied, so the rollback is recorded."""
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at(dsn, dim, ID_0031)
    await _insert_audit(dsn, "entry.restore", "fleet.merge")

    with pytest.raises(RollbackRefused, match=r"entry\.restore"):
        await _guarded_rollback(dsn, dim, count=1)
    assert await current_schema_version(dsn) == ID_0031

    assert await _guarded_rollback(dsn, dim, count=1, discard_audit_rows=True) == [ID_0031]
    assert await current_schema_version(dsn) == ID_0030
    assert await _audit_actions(dsn) == ["fleet.merge", "schema.rollback"]
    conn = await asyncpg.connect(dsn)
    try:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action) "
                "VALUES ('cli', 'x', 'entry.restore')"
            )
    finally:
        await conn.close()


async def test_0030_builds_the_audit_indexes_and_its_rollback_drops_them() -> None:
    """ADR 0123: four concurrent indexes, all valid; the rollback removes them."""
    dsn, dim = _dsn(), Settings().embedding_dim
    await _start_at(dsn, dim, ID_0031)
    names = [
        "audit_log_target_idx",
        "audit_log_withdraw_author_idx",
        "audit_log_move_agents_idx",
        "audit_log_successor_idx",
    ]
    sql = (
        "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE i.indisvalid AND c.relname = ANY($1::text[]) ORDER BY c.relname"
    )
    conn = await asyncpg.connect(dsn)
    try:
        assert [r[0] for r in await conn.fetch(sql, names)] == sorted(names)
        assert await _guarded_rollback(dsn, dim, count=2) == [ID_0031, ID_0030]
        assert await conn.fetch(sql, names) == []
        assert await current_schema_version(dsn) == ID_0029
        await migrate(dsn, dim)
        assert [r[0] for r in await conn.fetch(sql, names)] == sorted(names)
    finally:
        await conn.close()
