"""The agent portal's routes (ADR 0095): agent keys only."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Response

from hivemind.api.deps import api_error
from hivemind.api.routes.params import (
    API_PREFIX,
    ActorQuery,
    AppDep,
    CredentialDep,
    IdQuery,
    NameParam,
    PrometheusDep,
)
from hivemind.api.routes.shared import (
    check_cleanup_expectations,
    pin_entry,
    run_search,
    unpin_entry,
)
from hivemind.api.schemas import (
    AgentCleanupPreviewOut,
    AgentCleanupUndoOut,
    EntryOut,
    FeedbackLogItemOut,
    FlaggedEntryOut,
    HitOut,
    KeyLockedOut,
    PinOut,
    PortalCleanupOut,
    PortalCleanupRecordOut,
    PortalCleanupRequest,
    PortalHistoryItemOut,
    PortalSearchRequest,
    PortalWithdrawRequest,
    UnpinOut,
)
from hivemind.domain.entry import EntryFilters, Kind
from hivemind.domain.feedback import FeedbackLogFilters, Verdict
from hivemind.domain.validation import (
    MAX_LIMIT,
    MAX_OFFSET,
    utcnow,
)
from hivemind.ports import Credential

router = APIRouter(prefix=API_PREFIX)

PORTAL_DEFAULT_LIMIT = 50


def require_agent(credential: Credential, what: str) -> None:
    # An active agent's own key: not admin, org or a dev-mode identity (no agent name).
    if credential.is_admin or credential.is_org or credential.agent_name is None:
        raise api_error(
            403,
            "forbidden",
            f"{what} requires an agent key, not the admin or org key",
        )


@router.get("/portal/entries", response_model=list[EntryOut])
async def portal_list_entries(
    credential: CredentialDep,
    app: AppDep,
    kind: Annotated[Kind | None, Query()] = None,
    tags: Annotated[list[str] | None, Query()] = None,
    scope: Annotated[str | None, Query()] = None,
    fleet_id: IdQuery = None,
    author: ActorQuery = None,
    include_inactive: bool = False,
    flagged: bool = False,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = PORTAL_DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> list[EntryOut]:
    """List what the agent's key can read, newest first (ADR 0095):
    the browsing subset of the ``GET /v1/entries`` filters plus
    ``fleet_id``, so the portal can show the home fleet alone even to a
    privileged agent."""
    require_agent(credential, "the agent portal")
    filters = EntryFilters(
        kind=kind,
        tags=tuple(tags or ()),
        scope=scope,
        fleet_id=fleet_id,
        author=author,
        include_inactive=include_inactive,
        flagged=flagged,
    )
    entries = await app.read_service.list(
        filters, limit=limit, offset=offset, visibility=credential.visibility()
    )
    return [EntryOut.from_entry(e) for e in entries]


@router.post("/portal/search", response_model=list[HitOut])
async def portal_search(
    request: PortalSearchRequest,
    credential: CredentialDep,
    app: AppDep,
    prometheus: PrometheusDep,
    response: Response,
) -> list[HitOut]:
    """``POST /v1/search`` for a person holding an agent key (ADR 0095),
    with a ``fleet_id`` filter. Not counted in the search counters, so
    a person browsing does not move the agents' search numbers."""
    require_agent(credential, "the agent portal")
    return await run_search(
        app, prometheus, request, credential, response, fleet_id=request.fleet_id, count=False
    )


@router.get("/portal/feedback", response_model=list[FeedbackLogItemOut])
async def portal_feedback(
    credential: CredentialDep,
    app: AppDep,
    direction: Annotated[Literal["given", "received"], Query()],
    verdict: Annotated[Verdict | None, Query()] = None,
    entry_id: IdQuery = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = PORTAL_DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> list[FeedbackLogItemOut]:
    """The agent's side of the feedback log (ADR 0095): the verdicts it
    ``given``, or the verdicts its own memories ``received``, newest
    first, on entries its key can read."""
    require_agent(credential, "the agent portal")
    name = credential.agent_name
    filters = FeedbackLogFilters(
        reporter=name if direction == "given" else None,
        author=name if direction == "received" else None,
        verdict=verdict,
        entry_id=entry_id,
    )
    items = await app.governance_service.feedback_log(
        credential, filters, limit=limit, offset=offset
    )
    return [FeedbackLogItemOut.from_item(i) for i in items]


@router.post("/portal/entries/{entry_id}/withdraw", response_model=EntryOut)
async def portal_withdraw(
    entry_id: str, request: PortalWithdrawRequest, credential: CredentialDep, app: AppDep
) -> EntryOut:
    """Withdraw one of the agent's own entries from the portal (ADR
    0095): a reason is required and the withdrawal is audited as
    ``entry.withdraw`` with ``via: "portal"``."""
    require_agent(credential, "the agent portal")
    return EntryOut.from_entry(
        await app.governance_service.withdraw_own(credential, entry_id, request.reason)
    )


@router.get("/portal/flagged", response_model=list[FlaggedEntryOut])
async def portal_flagged(
    credential: CredentialDep,
    app: AppDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = PORTAL_DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> list[FlaggedEntryOut]:
    """The agent's own and inherited active memories that carry a
    standing ``stale`` or ``wrong`` verdict, oldest flag first (ADR
    0115): ``GET /v1/admin/flagged`` narrowed to what the agent owns,
    on entries its key can read."""
    require_agent(credential, "the agent portal")
    items = await app.governance_service.flag_backlog(
        credential, own=True, limit=limit, offset=offset
    )
    return [FlaggedEntryOut.from_flagged(i) for i in items]


@router.put("/portal/entries/{entry_id}/pin", response_model=PinOut)
async def portal_pin(entry_id: str, credential: CredentialDep, app: AppDep) -> PinOut:
    """Pin an entry from the portal with ``PUT /v1/entries/{id}/pin``'s
    rights, caps and errors (ADR 0116); the audit row says ``via: "portal"``."""
    require_agent(credential, "the agent portal")
    return await pin_entry(app, credential, entry_id, via="portal")


@router.delete("/portal/entries/{entry_id}/pin", response_model=UnpinOut)
async def portal_unpin(entry_id: str, credential: CredentialDep, app: AppDep) -> UnpinOut:
    """Unpin an entry from the portal (ADR 0116), audited ``via: "portal"``."""
    require_agent(credential, "the agent portal")
    return await unpin_entry(app, credential, entry_id, via="portal")


@router.post(
    "/portal/cleanup",
    response_model=AgentCleanupPreviewOut | PortalCleanupOut,
)
async def portal_cleanup(
    payload: PortalCleanupRequest, credential: CredentialDep, app: AppDep
) -> AgentCleanupPreviewOut | PortalCleanupOut:
    """The admin agent cleanup for the signed-in agent's own work (ADR
    0118; ``POST /v1/admin/agents/{name}/cleanup`` with the agent fixed to
    the key and no revoke). Withdraws the active memories of every author
    the agent owns written in the window, sets aside its own ratings,
    restores the ``restore_ids`` it superseded; ``dry_run`` first, the
    ``expected_*`` totals on the apply. Audited ``agent.cleanup`` with
    ``via: "portal"``. 403 ``forbidden`` for a dormant (level 0) agent
    (SPEC §12.2)."""
    require_agent(credential, "the agent portal")
    cleanups = app.access_service.cleanups
    if payload.dry_run:
        preview = await cleanups.preview_own(credential, since=payload.since, until=payload.until)
        return AgentCleanupPreviewOut.from_preview(preview)
    check_cleanup_expectations(payload)
    until = payload.until if payload.until is not None else utcnow()
    outcome = await cleanups.cleanup_own(
        credential,
        since=payload.since,
        until=until,
        restore_ids=payload.restore_ids,
        expected_withdraw=payload.expected_withdraw,
        expected_ratings=payload.expected_ratings,
    )
    return PortalCleanupOut.from_outcome(outcome, since=payload.since, until=until)


@router.get("/portal/cleanups", response_model=list[PortalCleanupRecordOut])
async def portal_cleanups(credential: CredentialDep, app: AppDep) -> list[PortalCleanupRecordOut]:
    """The signed-in agent's cleanups, newest first, the admin's too (ADR
    0118), each with whether the agent can undo it now. 403 ``forbidden``
    for a dormant (level 0) agent (SPEC §12.2)."""
    require_agent(credential, "the agent portal")
    views = await app.access_service.cleanups.list_own(credential)
    return [PortalCleanupRecordOut.from_view(v) for v in views]


@router.get("/portal/history", response_model=list[PortalHistoryItemOut])
async def portal_history(
    credential: CredentialDep,
    app: AppDep,
    before: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = PORTAL_DEFAULT_LIMIT,
) -> list[PortalHistoryItemOut]:
    """What happened to the signed-in agent and to the agents it inherits
    from (ADR 0113), newest first, from the audit log (ADR 0123): its own
    actions and the admin's, the admin as "an admin" with no identity, only
    allowlisted detail, a fleet named only if the key can read it, an entry
    id only if it can still read the entry. ``before`` is the last row's
    ``id`` for the next page. 403 ``forbidden`` for a dormant (level 0)
    agent (SPEC §12.2). Agent keys only."""
    require_agent(credential, "the agent portal")
    items = await app.access_service.history.list_own(
        credential, before=str(before) if before is not None else None, limit=limit
    )
    return [PortalHistoryItemOut.from_item(i) for i in items]


@router.post("/portal/cleanups/{cleanup_id}/undo", response_model=AgentCleanupUndoOut)
async def portal_undo_cleanup(
    cleanup_id: NameParam, credential: CredentialDep, app: AppDep
) -> AgentCleanupUndoOut:
    """Undo one of the agent's own cleanups (ADR 0118): only the newest
    standing cleanup of the agent, and only one the agent ran itself (403
    for an admin's, 404 for any other agent's, 409 ``cleanup_refused``
    when a newer one stands; also 403 for a dormant, level 0 agent, SPEC
    §12.2). Audited ``agent.cleanup_undo`` with ``via: "portal"``."""
    require_agent(credential, "the agent portal")
    outcome = await app.access_service.cleanups.undo_own(cleanup_id, credential)
    return AgentCleanupUndoOut.from_outcome(outcome, note=None)


@router.post("/portal/key/lock", response_model=KeyLockedOut)
async def portal_lock_key(credential: CredentialDep, app: AppDep) -> KeyLockedOut:
    """Lock the calling agent's own key ("this key leaked", ADR 0117):
    it fails every request from the next one on, with 401 ``key_locked``,
    until an admin replaces it. The agent stays active; memories, pins,
    ratings, trust and fleet are untouched. 409 ``key_not_locked`` if the key
    was replaced or locked in the meantime. Audited ``agent_key.lock``
    with ``via: "portal"``. Agent keys only."""
    require_agent(credential, "locking a key")
    locked = await app.access_service.lock_own_key(credential)
    if not locked:  # replaced or locked between the key check and the lock
        raise api_error(
            409, "key_not_locked", "this key was already replaced or locked; nothing changed"
        )
    return KeyLockedOut(key_id=str(credential.key_id))
