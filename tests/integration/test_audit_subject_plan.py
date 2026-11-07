"""The audit log's subject query uses the indexes of migration 0030 (ADR 0123).

A seeded table large enough that a scan is visibly worse; the plan is read
with ``EXPLAIN`` under the custom plan ``PgStore.list_audit`` forces. The
shape is checked, not the timing.
"""

from __future__ import annotations

import asyncpg
from tests.pg import ensure_migrated, reset_tables

from hivemind.config import Settings
from hivemind.domain.audit import AuditFilters
from hivemind.store.pgstore import audit_query

SEED = """
INSERT INTO audit_log (actor_kind, actor, action, target, detail, occurred_at)
SELECT 'admin_key', 'admin:abc123', 'agent.trust_level_set', 'agent-' || (i % 2000),
       '{"from": 1, "to": 2}'::jsonb, now() - (i || ' seconds')::interval
FROM generate_series(1, 40000) i;
INSERT INTO audit_log (actor_kind, actor, action, target, detail, occurred_at)
SELECT 'agent_key', 'agent-' || (i % 2000), 'entry.pin', gen_random_uuid()::text,
       '{"fleet_id": "f"}'::jsonb, now() - (i || ' seconds')::interval
FROM generate_series(1, 40000) i;
INSERT INTO audit_log (actor_kind, actor, action, target, detail, occurred_at)
SELECT 'admin_key', 'admin:abc123', 'entry.withdraw', gen_random_uuid()::text,
       jsonb_build_object('author', 'agent-' || (i % 2000)), now() - (i || ' seconds')::interval
FROM generate_series(1, 20000) i;
INSERT INTO audit_log (actor_kind, actor, action, target, detail, occurred_at)
SELECT 'admin_key', 'admin:abc123', 'fleet.move_agents', 'f' || (i % 20),
       jsonb_build_object('agents', jsonb_build_array('agent-' || (i % 2000))),
       now() - (i || ' seconds')::interval
FROM generate_series(1, 2000) i;
INSERT INTO audit_log (actor_kind, actor, action, target, detail, occurred_at)
SELECT 'admin_key', 'admin:abc123', 'agent.retire', 'agent-' || (i % 2000),
       jsonb_build_object('successor', 'agent-' || ((i + 1) % 2000)), now() - (i || ' seconds')::interval
FROM generate_series(1, 1000) i;
ANALYZE audit_log;
"""


async def test_the_subject_query_is_a_bitmap_or_over_the_new_indexes() -> None:
    settings = Settings()
    dsn = settings.database_url
    await ensure_migrated(dsn, settings.embedding_dim)
    await reset_tables(dsn)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(SEED)
        sql, params = audit_query(AuditFilters(subjects=("agent-42",)), 50)
        async with conn.transaction():
            await conn.execute("SET LOCAL plan_cache_mode = force_custom_plan")
            plan = "\n".join(r[0] for r in await conn.fetch("EXPLAIN " + sql, *params))
        assert "BitmapOr" in plan, plan
        for index in (
            "audit_log_actor_idx",
            "audit_log_target_idx",
            "audit_log_withdraw_author_idx",
            "audit_log_restore_author_idx",
            "audit_log_redact_author_idx",
            "audit_log_move_agents_idx",
            "audit_log_successor_idx",
        ):
            assert index in plan, plan
        sql, params = audit_query(AuditFilters(target="agent-42"), 50)
        plan = "\n".join(r[0] for r in await conn.fetch("EXPLAIN " + sql, *params))
        assert "audit_log_target_idx" in plan, plan
    finally:
        await conn.close()
        await reset_tables(dsn)
