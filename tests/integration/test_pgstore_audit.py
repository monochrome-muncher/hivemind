"""Postgres-backed tests for the audit log at the ``Store`` seam
(ADR 0027, SPEC §12.5): ``PgStore.record_audit`` / ``list_audit``
round-trip, ordering, filters, and the migration's named CHECKs.

Skips cleanly when Postgres is unreachable; ``audit_log`` is truncated
between tests.
"""

from __future__ import annotations

import asyncpg
import pytest

from hivemind.config import Settings
from hivemind.domain import ActorKind, AuditAction, AuditEvent, AuditFilters
from hivemind.store import PgStore
from hivemind.store.migrate import migrate


def _dsn() -> str:
    return Settings().database_url


async def _truncate(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("TRUNCATE audit_log")
    finally:
        await conn.close()


@pytest.fixture
async def pg():
    dsn = _dsn()
    try:
        probe = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # connection refused / timeout / auth
        pytest.skip(f"Postgres unreachable at {dsn}: {exc}")
    await probe.close()
    await migrate(dsn, Settings().embedding_dim)
    await _truncate(dsn)
    store = PgStore(dsn)
    try:
        yield store
    finally:
        await store.close()
        await _truncate(dsn)


async def test_record_and_list_audit_round_trip(pg) -> None:
    stored = await pg.record_audit(
        AuditEvent(
            actor_kind=ActorKind.ADMIN_KEY,
            actor="admin:0123456789ab",
            action=AuditAction.AGENT_TRUST_LEVEL_SET,
            target="alice",
            detail={"from": 1, "to": 3},
        )
    )
    assert stored.id
    assert stored.occurred_at.tzinfo is not None
    rows = await pg.list_audit(AuditFilters(), limit=10)
    assert rows == [stored]
    assert rows[0].detail == {"from": 1, "to": 3}
    assert rows[0].action is AuditAction.AGENT_TRUST_LEVEL_SET


async def test_null_target_and_empty_detail(pg) -> None:
    stored = await pg.record_audit(
        AuditEvent(actor_kind=ActorKind.CLI, actor="chris", action=AuditAction.ORG_KEY_ROTATE)
    )
    assert stored.target is None
    assert stored.detail == {}


async def test_list_audit_newest_first_with_filters(pg) -> None:
    a1 = await pg.record_audit(
        AuditEvent(ActorKind.ADMIN_KEY, "admin:a", AuditAction.FLEET_CREATE, "f1")
    )
    b = await pg.record_audit(AuditEvent(ActorKind.CLI, "ops", AuditAction.AGENT_REVOKE, "alice"))
    a2 = await pg.record_audit(
        AuditEvent(ActorKind.ADMIN_KEY, "admin:a", AuditAction.FLEET_CREATE, "f2")
    )
    assert [r.id for r in await pg.list_audit(AuditFilters(), limit=10)] == [a2.id, b.id, a1.id]
    assert [r.id for r in await pg.list_audit(AuditFilters(), limit=1)] == [a2.id]
    assert [r.id for r in await pg.list_audit(AuditFilters(actor="admin:a"), limit=10)] == [
        a2.id,
        a1.id,
    ]
    assert [
        r.id for r in await pg.list_audit(AuditFilters(action=AuditAction.AGENT_REVOKE), limit=10)
    ] == [b.id]
    assert [r.id for r in await pg.list_audit(AuditFilters(since=b.occurred_at), limit=10)] == [
        a2.id,
        b.id,
    ]


async def test_the_vocabulary_checks_reject_unknown_values(pg) -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        with pytest.raises(asyncpg.CheckViolationError, match="audit_log_action_check"):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action) VALUES ('cli', 'x', 'nope')"
            )
        with pytest.raises(asyncpg.CheckViolationError, match="audit_log_actor_kind_check"):
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, actor, action) "
                "VALUES ('root', 'x', 'agent.revoke')"
            )
    finally:
        await conn.close()


async def test_before_pages_back_through_the_log(pg) -> None:
    """ADR 0028: `before` is a row-id cursor over (occurred_at, id)."""
    for n in range(5):
        await pg.record_audit(AuditEvent(ActorKind.CLI, "chris", AuditAction.FLEET_CREATE, f"f{n}"))
    first = await pg.list_audit(AuditFilters(), 2)
    second = await pg.list_audit(AuditFilters(before=first[-1].id), 2)
    rest = await pg.list_audit(AuditFilters(before=second[-1].id), 10)
    assert [r.target for r in first + second + rest] == ["f4", "f3", "f2", "f1", "f0"]
    unknown = AuditFilters(before="00000000-0000-0000-0000-000000000000")
    assert await pg.list_audit(unknown, 10) == []
