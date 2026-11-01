"""``audit_unpins`` writes the one ``entry.unpin`` detail shape every caller shares."""

from __future__ import annotations

from hivemind.domain.audit import AuditAction, AuditFilters
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.audit import audit_unpins

ADMIN = Credential(user_id="admin", is_admin=True, key_id="0123456789ab")


async def _detail(**kwargs: str | None) -> dict[str, object]:
    store = MemoryStore()
    await audit_unpins(store, ADMIN, "e1", "f1", ["e1", "e2"], **kwargs)
    (row,) = await store.list_audit(AuditFilters(), limit=10)
    assert row.action is AuditAction.ENTRY_UNPIN
    assert row.target == "e1"
    return dict(row.detail)


async def test_detail_keys_are_ordered_and_optional_ones_left_out() -> None:
    bare = await _detail()
    assert list(bare) == ["fleet_id", "pin_ids"]
    assert bare == {"fleet_id": "f1", "pin_ids": ["e1", "e2"]}


async def test_reason_ids_then_via() -> None:
    detail = await _detail(reason="agent_cleanup", cleanup_id="c1", via="portal")
    assert list(detail) == ["fleet_id", "pin_ids", "reason", "cleanup_id", "via"]
    assert detail["via"] == "portal"
