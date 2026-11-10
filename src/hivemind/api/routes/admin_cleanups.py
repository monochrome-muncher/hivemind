"""Admin agent cleanups (ADR 0111): clean up after an agent, list, undo."""

from __future__ import annotations

from fastapi import APIRouter

from hivemind.api.routes.params import API_PREFIX, AppDep, CredentialDep, NameParam
from hivemind.api.routes.shared import (
    check_cleanup_expectations,
    require_admin,
)
from hivemind.api.schemas import (
    AgentCleanupOut,
    AgentCleanupPreviewOut,
    AgentCleanupRecordOut,
    AgentCleanupRequest,
    AgentCleanupUndoOut,
)
from hivemind.domain.validation import (
    utcnow,
)

router = APIRouter(prefix=API_PREFIX)


@router.post(
    "/admin/agents/{name}/cleanup",
    response_model=AgentCleanupPreviewOut | AgentCleanupOut,
)
async def cleanup_agent(
    name: NameParam, payload: AgentCleanupRequest, credential: CredentialDep, app: AppDep
) -> AgentCleanupPreviewOut | AgentCleanupOut:
    """Clean up after a bad agent (admin-gated, audited ``agent.cleanup``,
    ADR 0111): withdraw the active memories it wrote in the window
    (``created_at`` in ``[since, until)``, any scope), set aside its ratings
    last written in the window, and put back the memories it superseded
    that ``restore_ids`` names. ``dry_run: true`` changes nothing and
    answers the preview. ``also_revoke`` revokes the agent after the
    cleanup (its own ``agent.revoke``). ``expected_withdraw`` /
    ``expected_ratings`` (the preview's totals) refuse the apply when they
    changed (409 ``cleanup_refused``), as does a ``restore_ids`` memory
    that is not restorable. With nothing to clean the answer is 200 with
    zero counts and no ``cleanup_id``. 404 unknown agent, 422 empty window or
    a missing ``expected_*`` count on an apply."""
    require_admin(credential, "cleaning up after an agent")
    cleanups = app.access_service.cleanups
    if payload.dry_run:
        preview = await cleanups.preview(name, credential, since=payload.since, until=payload.until)
        return AgentCleanupPreviewOut.from_preview(preview)
    check_cleanup_expectations(payload)
    until = payload.until if payload.until is not None else utcnow()
    outcome = await cleanups.cleanup(
        name,
        credential,
        since=payload.since,
        until=until,
        restore_ids=payload.restore_ids,
        expected_withdraw=payload.expected_withdraw,
        expected_ratings=payload.expected_ratings,
        also_revoke=payload.also_revoke,
    )
    return AgentCleanupOut.from_outcome(outcome, agent=name, since=payload.since, until=until)


@router.get("/admin/agent-cleanups", response_model=list[AgentCleanupRecordOut])
async def list_agent_cleanups(
    credential: CredentialDep, app: AppDep
) -> list[AgentCleanupRecordOut]:
    """Recorded agent cleanups, newest first (admin-gated, ADR 0111), each
    with whether it can be undone now."""
    require_admin(credential, "the agent cleanups")
    views = await app.access_service.cleanups.list_cleanups(credential)
    return [AgentCleanupRecordOut.from_view(v) for v in views]


@router.post("/admin/agent-cleanups/{cleanup_id}/undo", response_model=AgentCleanupUndoOut)
async def undo_agent_cleanup(
    cleanup_id: NameParam, credential: CredentialDep, app: AppDep
) -> AgentCleanupUndoOut:
    """Undo an agent cleanup (admin-gated, audited ``agent.cleanup_undo``,
    ADR 0111): the memories still withdrawn by it are active again, the
    restored ones are superseded again where that still fits, its rating
    marks are cleared and its pins are restored where the fleet has room.
    It does not un-revoke the agent. Only the newest standing cleanup of
    an agent can be undone (409 ``cleanup_refused``)."""
    require_admin(credential, "undoing an agent cleanup")
    outcome = await app.access_service.cleanups.undo(cleanup_id, credential)
    return AgentCleanupUndoOut.from_outcome(
        outcome,
        note=(
            "the agent was revoked by this cleanup and stays revoked; "
            "re-activate it separately if it should work again"
            if outcome.record.revoked
            else None
        ),
    )
