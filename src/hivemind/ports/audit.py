"""The audit log."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from hivemind.domain.audit import AuditEvent, AuditFilters, AuditRecord


@runtime_checkable
class AuditPort(Protocol):
    """The audit log."""

    async def record_audit(self, event: AuditEvent) -> AuditRecord:
        """Append one admin-action row (insert-only). Failures propagate, never swallowed (ADR
        0027)."""
        ...

    async def list_audit(self, filters: AuditFilters, limit: int) -> list[AuditRecord]:
        """Audit rows matching ``filters``, newest first."""
        ...
