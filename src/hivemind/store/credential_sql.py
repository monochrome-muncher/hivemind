"""The credential and audit-insert SQL shared by the authenticator (``auth.py``), the
``hivemind-keys`` CLI (``keys.py``) and the migration runner. A module of its own, importing nothing
heavy, so ``migrate.py`` stays importable at container start."""

from __future__ import annotations

import json
from collections.abc import Mapping

import asyncpg

from hivemind.domain.audit import ActorKind, AuditAction

# Rows ``verify`` can never resolve (ADR 0039): inert, but live again on a rollback to a release
# that does not join ``agents``.
DEAD_CREDENTIAL_PREDICATE = (
    "(c.kind = 'user' OR (c.kind = 'agent' AND (c.agent_name IS NULL OR NOT EXISTS "
    "(SELECT 1 FROM agents a WHERE a.name = c.agent_name AND a.status = 'active'))))"
)
COUNT_DEAD_CREDENTIALS = f"SELECT count(*) FROM credentials c WHERE {DEAD_CREDENTIAL_PREDICATE}"

# Serialises org-key rotation (API and CLI): the second waits, then deletes the first's row.
ORG_KEY_LOCK = "SELECT pg_advisory_xact_lock(hashtext('hivemind:org-key'))"
LOCK_AGENT_STATUS = "SELECT status FROM agents WHERE name = $1 FOR NO KEY UPDATE"

# Key management (ADR 0012).
ISSUE_ADMIN_KEY = "INSERT INTO credentials (key_hash, kind, user_id) VALUES ($1, 'admin', $2)"
ISSUE_ORG_KEY = "INSERT INTO credentials (key_hash, kind, user_id) VALUES ($1, 'org', $2)"
ISSUE_AGENT_KEY = (
    "INSERT INTO credentials (key_hash, kind, user_id, agent_id, agent_name) "
    "VALUES ($1, 'agent', $2, $2, $2)"
)
DELETE_ORG_KEYS = "DELETE FROM credentials WHERE kind = 'org'"
DELETE_AGENT_KEYS = "DELETE FROM credentials WHERE kind = 'agent' AND agent_name = $1"
DELETE_AGENT_KEYS_RETURNING = DELETE_AGENT_KEYS + " RETURNING key_hash"

INSERT_AUDIT = (
    "INSERT INTO audit_log (actor_kind, actor, action, target, detail) "
    "VALUES ($1, $2, $3, $4, $5::jsonb)"
)


async def insert_audit(
    conn: asyncpg.Connection,
    actor_kind: ActorKind,
    actor: str,
    action: AuditAction,
    target: str | None,
    detail: Mapping[str, object] | None = None,
) -> None:
    """Append an audit row on ``conn``, inside the caller's transaction (ADR 0027)."""
    await conn.execute(
        INSERT_AUDIT, actor_kind.value, actor, action.value, target, json.dumps(detail or {})
    )
