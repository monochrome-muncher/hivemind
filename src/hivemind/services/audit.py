"""Recording admin-surface actions in the audit log (ADR 0027, SPEC §12.5).

The one writer the app-side services share (``AccessService`` and
``GovernanceService``'s admin withdrawal). It is handed an action, a
target and a detail payload — never a key — so a raw API key cannot
reach an audit column by construction.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hivemind.domain.audit import ActorKind, AuditAction, AuditEvent, AuditRecord
from hivemind.ports import Credential, Store


async def record_admin_action(
    store: Store,
    credential: Credential,
    action: AuditAction,
    target: str | None,
    detail: Mapping[str, Any] | None = None,
) -> AuditRecord:
    """Append an ``admin_key`` audit row for an action ``credential`` has
    just performed. The actor is the admin key's fingerprint
    (``Credential.audit_actor``). Called **after** the action succeeds
    and not atomic with it (two ports, no shared transaction); a failure
    here propagates — it is never swallowed (ADR 0027)."""
    return await store.record_audit(
        AuditEvent(
            actor_kind=ActorKind.ADMIN_KEY,
            actor=credential.audit_actor(),
            action=action,
            target=target,
            detail=dict(detail or {}),
        )
    )
