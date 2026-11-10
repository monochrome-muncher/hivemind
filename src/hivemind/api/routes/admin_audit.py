"""The audit log and the org key rotation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from hivemind.api.routes.params import API_PREFIX, ActorQuery, AppDep, CredentialDep, IdQuery
from hivemind.api.routes.shared import (
    to_utc,
)
from hivemind.api.schemas import (
    AuditRecordOut,
    KeyIssuedOut,
)
from hivemind.domain.audit import AuditAction, AuditFilters
from hivemind.domain.validation import (
    InvalidInput,
)

router = APIRouter(prefix=API_PREFIX)

# GET /v1/admin/audit-log page size (ADR 0027): default and hard ceiling.
AUDIT_LOG_DEFAULT_LIMIT = 100
AUDIT_LOG_MAX_LIMIT = 1000


@router.get("/admin/audit-log", response_model=list[AuditRecordOut])
async def audit_log(
    credential: CredentialDep,
    app: AppDep,
    actor: ActorQuery = None,
    action: Annotated[AuditAction | None, Query()] = None,
    target: IdQuery = None,
    subject: ActorQuery = None,
    since: Annotated[datetime | None, Query()] = None,
    before: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=AUDIT_LOG_MAX_LIMIT)] = AUDIT_LOG_DEFAULT_LIMIT,
) -> list[AuditRecordOut]:
    """The audit log of admin-surface actions, newest first (admin-
    gated, ADR 0027 — SPEC §12.5). Filters AND together: ``actor``
    (exact), ``action`` (the audited vocabulary), ``target`` (exact),
    ``subject`` (an agent name: the rows about that agent, ADR 0123),
    ``since`` (inclusive; a naive timestamp is UTC), ``before`` (a row id: only strictly
    older rows, for paging back — ADR 0028), ``limit`` (1..1000,
    default 100)."""
    since = to_utc(since)
    if since is not None:
        try:
            # Outside year 1..9999 cannot be sent to Postgres (asyncpg DataError: a 500).
            since.astimezone(UTC)
        except OverflowError:
            raise InvalidInput("since is out of range") from None
    filters = AuditFilters(
        actor=actor,
        action=action,
        target=target,
        subjects=(subject,) if subject is not None else (),
        since=since,
        before=str(before) if before is not None else None,
    )
    records = await app.access_service.list_audit(credential, filters, limit)
    return [AuditRecordOut.from_record(r) for r in records]


@router.post("/admin/org-key/rotate", response_model=KeyIssuedOut)
async def rotate_org_key(credential: CredentialDep, app: AppDep) -> KeyIssuedOut:
    """Rotate the shared org key — closes registration to every
    prior org key; active agents are unaffected (ADR 0031).
    All prior org keys stop working; the new key is returned once.
    """
    key = await app.access_service.rotate_org_key(credential)
    return KeyIssuedOut(key=key)
