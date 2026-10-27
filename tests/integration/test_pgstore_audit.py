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


async def test_pin_and_unpin_rows_by_an_agent_are_accepted(pg) -> None:
    """ADR 0076: migration 0014 widened both CHECKs for agent pin rows."""
    for action in (AuditAction.ENTRY_PIN, AuditAction.ENTRY_UNPIN):
        await pg.record_audit(
            AuditEvent(ActorKind.AGENT_KEY, "lead", action, "e1", {"fleet_id": "f1"})
        )
    rows = await pg.list_audit(AuditFilters(actor="lead"), limit=10)
    assert {r.action for r in rows} == {AuditAction.ENTRY_PIN, AuditAction.ENTRY_UNPIN}
    assert all(r.actor_kind is ActorKind.AGENT_KEY for r in rows)


async def test_null_target_and_empty_detail(pg) -> None:
    stored = await pg.record_audit(
        AuditEvent(actor_kind=ActorKind.CLI, actor="john", action=AuditAction.ORG_KEY_ROTATE)
    )
    assert stored.target is None
    assert stored.detail == {}


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
