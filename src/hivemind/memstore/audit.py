"""The append-only audit log."""

from __future__ import annotations

import uuid

from hivemind.domain.audit import AuditEvent, AuditFilters, AuditRecord
from hivemind.memstore.state import MemoryState


class AuditStore(MemoryState):
    async def record_audit(self, event: AuditEvent) -> AuditRecord:
        """Append one audit row (insert-only; ADR 0027)."""
        with self._lock:
            record = AuditRecord(
                id=str(uuid.uuid4()),
                occurred_at=self._clock(),
                actor_kind=event.actor_kind,
                actor=event.actor,
                action=event.action,
                target=event.target,
                detail=dict(event.detail),
            )
            self._audit.append(record)
            return record

    async def list_audit(self, filters: AuditFilters, limit: int) -> list[AuditRecord]:
        """Matching audit rows, newest first (insertion order breaks
        ``occurred_at`` ties, so a fixed test clock still reads newest
        first)."""
        with self._lock:
            ordered = [
                r
                for _, r in sorted(
                    enumerate(self._audit),
                    key=lambda pair: (pair[1].occurred_at, pair[0]),
                    reverse=True,
                )
            ]
            if filters.before is not None:
                ids = [r.id for r in ordered]
                if filters.before not in ids:
                    return []
                cursor = ordered[ids.index(filters.before)]
                if filters.subjects and not AuditFilters(subjects=filters.subjects).matches(cursor):
                    return []  # the cursor row must be about the subject too
                ordered = ordered[ids.index(filters.before) + 1 :]
            return [r for r in ordered if filters.matches(r)][:limit]
