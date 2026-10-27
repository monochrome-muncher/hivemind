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
import os

import asyncpg
import pytest

from hivemind.config import Settings
from hivemind.store.migrate import (
    MIGRATION_LOCK_KEY,
    current_embedding_dim,
    current_schema_version,
    migrate,
    rollback,
)

HEAD = "0021.credential-last-used"

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
    HEAD,
]


def _dsn() -> str:
    return Settings().database_url


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

    Peeled one at a time from the head: `0021` drops the credential last-use
    time (ADR 0110) and touches no key; `0019` deletes the rename audit
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
    await _require_postgres(dsn)
    await _reset_chain(dsn)
    await migrate(dsn, dim)

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO audit_log (actor_kind, actor, action, target) "
            "VALUES ('admin_key', 'admin:1', 'fleet.rename', 'f'), "
            "('admin_key', 'admin:1', 'fleet.merge', 'f'), "
            "('admin_key', 'admin:1', 'fleet.create', 'f')"
        )
    finally:
        await conn.close()
    rolled = await rollback(dsn, dim, count=1)
    assert rolled == [HEAD]
    assert await current_schema_version(dsn) == "0019.fleet-rename"
    rolled = await rollback(dsn, dim, count=1)
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
    rolled = await rollback(dsn, dim, count=1)
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
    rolled = await rollback(dsn, dim, count=1)
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
    rolled = await rollback(dsn, dim, count=1)
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
    await _require_postgres(dsn)
    await _reset_chain(dsn)
    await migrate(dsn, dim)

    with pytest.raises(RuntimeError) as excinfo:
        await rollback(dsn, dim, count=len(CHAIN))
    message = str(excinfo.value)
    assert "0001.initial-schema" in message
    assert "pg-reset" in message or "backup" in message

    # The refusal left the pool intact.
    assert await current_schema_version(dsn) == HEAD

    # Now put 0001 in as the head for real (a legal rollback of everything
    # above it), and confirm count=1 refuses it exactly the same way.
    rolled = await rollback(dsn, dim, count=len(CHAIN) - 1)
    assert rolled == list(reversed(CHAIN[1:]))
    assert await current_schema_version(dsn) == "0001.initial-schema"

    with pytest.raises(RuntimeError) as excinfo:
        await rollback(dsn, dim, count=1)
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
    await _require_postgres(dsn)
    await _reset_chain(dsn)
    await migrate(dsn, dim)
    await rollback(dsn, dim, count=5)
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
    await _require_postgres(dsn)
    await _reset_chain(dsn)
    await migrate(dsn, dim)
    await rollback(dsn, dim, count=14)  # 0021 down to 0006
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

    await migrate(dsn, dim)  # applies 0006 to 0021
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
        dsn, dim, count=14
    )  # 0021, 0019, 0017, 0016, 0015, 0014, 0013, 0012, 0011, 0010, 0009, 0008, 0007, then 0006
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
    await _require_postgres(dsn)
    await _reset_chain(dsn)
    await migrate(dsn, dim)
    await rollback(
        dsn, dim, count=13
    )  # 0021, 0019, 0017, 0016, 0015, 0014, 0013, 0012, 0011, 0010, 0009, 0008 (indexes) then 0007
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
        dsn, dim, count=13
    )  # 0021, 0019, 0017, 0016, 0015, 0014, 0013, 0012, 0011, 0010, 0009, 0008 (indexes) then 0007
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
    await _require_postgres(dsn)
    await _reset_chain(dsn)
    await migrate(dsn, dim)

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
        await rollback(dsn, dim, count=1)
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
    await _require_postgres(dsn)
    await _reset_chain(dsn)
    await migrate(dsn, dim)
    await rollback(dsn, dim, count=4)  # 0021, 0019, 0017, then 0016
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

    await rollback(dsn, dim, count=4)
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
    await _require_postgres(dsn)
    await _reset_chain(dsn)
    await migrate(dsn, dim)
    await rollback(dsn, dim, count=3)  # 0021, 0019, then 0017
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

    await rollback(dsn, dim, count=3)  # 0021, 0019, then 0017
    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SELECT count(*) FROM fleets") == 1
        assert await conn.fetchval("SELECT count(*) FROM audit_log") == 0
        await conn.execute("DELETE FROM fleets")
    finally:
        await conn.close()
    await migrate(dsn, dim)


async def test_0021_adds_a_nullable_last_use_and_rolls_back_without_touching_keys() -> None:
    """ADR 0110: an active agent's existing key gets the migration's time as
    ``last_used_at`` (a grace period); other keys stay NULL; the rollback
    drops only the column and keeps every key."""
    dsn, dim = _dsn(), Settings().embedding_dim
    await _require_postgres(dsn)
    await _reset_chain(dsn)
    await migrate(dsn, dim)
    await rollback(dsn, dim, count=1)
    assert await current_schema_version(dsn) == "0019.fleet-rename"

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

    await migrate(dsn, dim)
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

    await rollback(dsn, dim, count=1)
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
