"""Audit log schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from hivemind.domain.audit import AuditRecord


class AuditRecordOut(BaseModel):
    """One audit-log row (ADR 0027, SPEC §12.5). Never carries a raw key:
    key-producing actions record a key fingerprint or nothing."""

    id: str
    occurred_at: str
    actor_kind: str
    actor: str
    action: str
    target: str | None
    detail: dict[str, Any]

    @classmethod
    def from_record(cls, record: AuditRecord) -> AuditRecordOut:
        return cls(
            id=record.id,
            occurred_at=record.occurred_at.isoformat(),
            actor_kind=record.actor_kind.value,
            actor=record.actor,
            action=record.action.value,
            target=record.target,
            detail=dict(record.detail),
        )
