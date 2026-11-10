"""Recording admin-surface actions in the audit log (ADR 0027, SPEC §12.5).

The one writer the app-side services share. It takes an action, a target and a detail payload,
never a key, so a raw key cannot reach an audit column.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from hivemind.domain.audit import ActorKind, AuditAction, AuditEvent, AuditRecord
from hivemind.ports import Credential, Store


def actor_kind_of(credential: Credential) -> ActorKind:
    if credential.is_org:
        return ActorKind.ORG_KEY
    return ActorKind.ADMIN_KEY if credential.is_admin else ActorKind.AGENT_KEY


async def record_admin_action(
    store: Store,
    credential: Credential,
    action: AuditAction,
    target: str | None,
    detail: Mapping[str, Any] | None = None,
) -> AuditRecord:
    """Append an audit row for an action ``credential`` has just performed; the actor is the key's
    fingerprint. Called **after** the action succeeds, not atomic with it; a failure here
    propagates, never swallowed (ADR 0027)."""
    return await store.record_audit(
        AuditEvent(
            actor_kind=actor_kind_of(credential),
            actor=credential.audit_actor(),
            action=action,
            target=target,
            detail=dict(detail or {}),
        )
    )


async def audit_unpins(
    store: Store,
    credential: Credential,
    target: str,
    fleet_id: str,
    pin_ids: Sequence[str],
    *,
    reason: str | None = None,
    via: str | None = None,
    **ids: str,
) -> AuditRecord:
    """Append one ``entry.unpin`` row. Detail key order is fixed: ``fleet_id``, ``pin_ids``,
    ``reason``, any ``ids``, then ``via``; ``None`` ones are left out."""
    detail: dict[str, Any] = {"fleet_id": fleet_id, "pin_ids": list(pin_ids)}
    if reason is not None:
        detail["reason"] = reason
    detail.update(ids)
    if via is not None:
        detail["via"] = via
    return await record_admin_action(store, credential, AuditAction.ENTRY_UNPIN, target, detail)
