"""The audit log: record and query."""

from __future__ import annotations

import json
from typing import Any

from hivemind.domain.audit import AGENT_TARGET_ACTIONS, AuditEvent, AuditFilters, AuditRecord
from hivemind.store.pgstore.connection import PgConnections
from hivemind.store.pgstore.rows import row_to_audit

# Audit log (ADR 0027): insert-only; the table has no UPDATE/DELETE path.
_AUDIT_COLUMNS = "id, occurred_at, actor_kind, actor, action, target, detail"

INSERT_AUDIT = f"""
INSERT INTO audit_log (actor_kind, actor, action, target, detail)
VALUES ($1, $2, $3, $4, $5::jsonb)
RETURNING {_AUDIT_COLUMNS}
"""


def audit_query(filters: AuditFilters, limit: int) -> tuple[str, list[Any]]:
    """The SQL and parameters of ``PgStore.list_audit``."""
    conditions: list[str] = []
    params: list[Any] = []
    if filters.actor is not None:
        params.append(filters.actor)
        conditions.append(f"actor = ${len(params)}")
    if filters.action is not None:
        params.append(filters.action.value)
        conditions.append(f"action = ${len(params)}")
    if filters.target is not None:
        params.append(filters.target)
        conditions.append(f"target = ${len(params)}")
    if filters.since is not None:
        params.append(filters.since)
        conditions.append(f"occurred_at >= ${len(params)}")
    subject_condition = ""
    if filters.subjects:
        params.append(list(filters.subjects))
        names = f"${len(params)}::text[]"
        params.append([a.value for a in AGENT_TARGET_ACTIONS])
        target_actions = f"${len(params)}::text[]"
        # One OR arm per shape, each on its own index (migration 0030), so
        # the planner can BitmapOr them. The literals match the partial
        # indexes' predicates.
        subject_condition = (
            f"((actor_kind = 'agent_key' AND actor = ANY({names})) "
            f"OR (target = ANY({names}) AND action = ANY({target_actions})) "
            f"OR (action = 'entry.withdraw' AND detail->>'author' = ANY({names})) "
            # Its own partial index (migration 0031), so the BitmapOr stays.
            f"OR (action = 'entry.restore' AND detail->>'author' = ANY({names})) "
            # And 0032's, for a redaction.
            f"OR (action = 'entry.redact' AND detail->>'author' = ANY({names})) "
            f"OR (action = 'fleet.move_agents' AND detail->'agents' ?| {names}) "
            f"OR (action IN ('agent.retire', 'agent.retire_undo') "
            f"AND detail->>'successor' = ANY({names})))"
        )
        conditions.append(subject_condition)
    if filters.before is not None:
        # Row-value comparison against the cursor row (ADR 0028); an unknown
        # id yields a NULL row, which compares to nothing. With a subject the
        # cursor row must be about it too, else nothing matches.
        params.append(filters.before)
        cursor_row = f"SELECT occurred_at, id FROM audit_log WHERE id = ${len(params)}::uuid"
        if subject_condition:
            cursor_row += f" AND {subject_condition}"
        conditions.append(f"(occurred_at, id) < ({cursor_row})")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)
    sql = (
        f"SELECT {_AUDIT_COLUMNS} FROM audit_log {where} "
        f"ORDER BY occurred_at DESC, id DESC LIMIT ${len(params)}"
    )
    return sql, params


class AuditStore(PgConnections):
    async def record_audit(self, event: AuditEvent) -> AuditRecord:
        """Append one audit row (insert-only). Failures propagate — an
        audit write is never swallowed (ADR 0027)."""
        async with self._connection() as conn:
            row = await conn.fetchrow(
                INSERT_AUDIT,
                event.actor_kind.value,
                event.actor,
                event.action.value,
                event.target,
                json.dumps(dict(event.detail)),
            )
        if row is None:
            raise RuntimeError("audit row vanished between insert and read")
        return row_to_audit(row)

    async def list_audit(self, filters: AuditFilters, limit: int) -> list[AuditRecord]:
        """Matching audit rows, newest first, at most ``limit``."""
        sql, params = audit_query(filters, limit)
        async with self._connection() as conn:
            if not filters.subjects:
                rows = await conn.fetch(sql, *params)
            else:
                # A generic plan cannot see how rare one agent's rows are and
                # walks ``occurred_at`` backwards filtering the whole log
                # (78 ms on 600k rows against 0.7 ms with the BitmapOr), and
                # asyncpg switches a cached statement to one after a few runs.
                # ``SET LOCAL``, as the search does: a pooler discards session state.
                async with conn.transaction():
                    await conn.execute("SET LOCAL plan_cache_mode = force_custom_plan")
                    rows = await conn.fetch(sql, *params)
        return [row_to_audit(r) for r in rows]
