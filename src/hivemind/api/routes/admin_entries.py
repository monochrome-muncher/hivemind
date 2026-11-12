"""The admin console's reads and entry actions (ADR 0080): admin key only."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated

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
    entry_detail,
    listed_with_counts,
    require_admin,
    run_search,
    to_utc,
)
from hivemind.api.schemas import (
    ActivityOut,
    AdminEntryOut,
    AdminSearchRequest,
    EntryOut,
    ExplainedHitOut,
    FeedbackLogItemOut,
    FlaggedEntryOut,
    HitOut,
    PinnedOut,
    PinsOut,
    RedactOut,
    RedactRequest,
    RestoreRequest,
    WithdrawRequest,
)
from hivemind.domain.entry import EntryFilters, EntryState, Kind
from hivemind.domain.feedback import FeedbackLogFilters, Verdict
from hivemind.domain.validation import (
    MAX_LIMIT,
    MAX_OFFSET,
)
from hivemind.services.activity import DEFAULT_DAYS, MAX_DAYS, MIN_DAYS

router = APIRouter(prefix=API_PREFIX)

# GET /v1/admin/entries default page size (ADR 0080); the ceiling is MAX_LIMIT.
ADMIN_ENTRIES_DEFAULT_LIMIT = 50
# GET /v1/admin/feedback default page size; the ceiling is MAX_LIMIT.
ADMIN_FEEDBACK_DEFAULT_LIMIT = 50
ADMIN_FLAGGED_DEFAULT_LIMIT = 50
MAX_FLAG_AGE_DAYS = 3650  # the flag backlog's age filter: ten years


@router.get("/admin/entries", response_model=list[AdminEntryOut])
async def admin_list_entries(
    credential: CredentialDep,
    app: AppDep,
    kind: Annotated[Kind | None, Query()] = None,
    tags: Annotated[list[str] | None, Query()] = None,
    entities: Annotated[list[str] | None, Query()] = None,
    scope: Annotated[str | None, Query()] = None,
    fleet_id: Annotated[str | None, Query()] = None,
    author: Annotated[str | None, Query()] = None,
    agent: Annotated[str | None, Query()] = None,
    occurred_from: Annotated[datetime | None, Query()] = None,
    occurred_to: Annotated[datetime | None, Query()] = None,
    created_from: Annotated[datetime | None, Query()] = None,
    created_to: Annotated[datetime | None, Query()] = None,
    include_inactive: bool = False,
    state: Annotated[EntryState | None, Query()] = None,
    flagged: bool = False,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = ADMIN_ENTRIES_DEFAULT_LIMIT,
    offset: Annotated[int | None, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> list[EntryOut]:
    """List/filter entries as the admin sees them (ADR 0080): every
    scope of every fleet, newest first, with the ``GET /v1/entries``
    filters plus ``fleet_id``."""
    require_admin(credential, "the admin entry list")
    filters = EntryFilters(
        kind=kind,
        tags=tuple(tags or ()),
        entities=tuple(entities or ()),
        scope=scope,
        fleet_id=fleet_id,
        author=author,
        agent=agent,
        occurred_from=to_utc(occurred_from),
        occurred_to=to_utc(occurred_to),
        created_from=to_utc(created_from),
        created_to=to_utc(created_to),
        include_inactive=include_inactive,
        state=state,
        flagged=flagged,
    )
    entries = await app.read_service.list(
        filters,
        limit=limit,
        offset=offset if offset is not None else 0,
        visibility=credential.visibility(),
    )
    return await listed_with_counts(app, entries, AdminEntryOut)


@router.get("/admin/entries/{entry_id}", response_model=AdminEntryOut)
async def admin_get_entry(entry_id: str, credential: CredentialDep, app: AppDep) -> EntryOut:
    """One entry with its feedback, links and history always filled
    (ADR 0080): ``GET /v1/entries/{id}?history=true`` for the admin."""
    require_admin(credential, "the admin entry read")
    return await entry_detail(app, entry_id, credential, history=True, model=AdminEntryOut)


@router.post("/admin/search", response_model=list[ExplainedHitOut])
async def admin_search(
    request: AdminSearchRequest,
    credential: CredentialDep,
    app: AppDep,
    prometheus: PrometheusDep,
    response: Response,
) -> list[HitOut]:
    """``POST /v1/search`` for the admin, with a ``fleet_id`` filter
    (ADR 0080). With ``as_agent`` it sees what that agent's own search
    sees (ADR 0136). With ``explain`` each hit carries the parts of its
    score (ADR 0137). Not counted in the search counters (ADR 0056); a
    degraded answer still counts for ``GET /metrics``."""
    require_admin(credential, "the admin search")
    searcher = credential
    if request.as_agent is not None:
        searcher = await app.access_service.credential_for_agent(credential, request.as_agent)
    return await run_search(
        app,
        prometheus,
        request,
        searcher,
        response,
        fleet_id=request.fleet_id,
        state=request.state,
        count=False,
        explain=request.explain,
        model=ExplainedHitOut,
    )


@router.post("/admin/entries/{entry_id}/withdraw", response_model=EntryOut)
async def admin_withdraw(
    entry_id: str, request: WithdrawRequest, credential: CredentialDep, app: AppDep
) -> EntryOut:
    """Withdraw an entry as the admin (ADR 0080): the same call as
    ``POST /v1/entries/{id}/withdraw``, audited as ``entry.withdraw``."""
    require_admin(credential, "the admin withdraw")
    return EntryOut.from_entry(
        await app.governance_service.withdraw(credential, entry_id, request.reason)
    )


@router.post("/admin/entries/{entry_id}/restore", response_model=EntryOut)
async def admin_restore(
    entry_id: str, request: RestoreRequest, credential: CredentialDep, app: AppDep
) -> EntryOut:
    """Put a withdrawn memory back to ``active`` (ADR 0124). The reason
    is required; pins are not restored; 409 for a memory that is not
    withdrawn, was withdrawn by a standing agent cleanup, or would leave
    two current versions in a chain. Audited as ``entry.restore``."""
    require_admin(credential, "the admin restore")
    return EntryOut.from_entry(
        await app.governance_service.restore(credential, entry_id, request.reason)
    )


@router.post("/admin/entries/{entry_id}/redact", response_model=RedactOut)
async def admin_redact(
    entry_id: str, request: RedactRequest, credential: CredentialDep, app: AppDep
) -> RedactOut:
    """Wipe a withdrawn memory's text for good (ADR 0125): admin only,
    ``confirm`` is the memory id typed back (422 otherwise). 404 unknown,
    409 active (withdraw it first) or already redacted. Audited as ``entry.redact``;
    the answer counts the feedback notes left to review."""
    require_admin(credential, "the admin redact")
    return RedactOut.from_outcome(
        await app.governance_service.redact(credential, entry_id, request.reason, request.confirm)
    )


@router.get("/admin/activity", response_model=ActivityOut)
async def admin_activity(
    credential: CredentialDep,
    app: AppDep,
    days: Annotated[int, Query(ge=MIN_DAYS, le=MAX_DAYS)] = DEFAULT_DAYS,
) -> ActivityOut:
    """Who wrote and who reported, per agent, fleet and UTC day over
    the last ``days`` days (ADR 0080). Observation only."""
    require_admin(credential, "the activity view")
    return ActivityOut(**await app.activity_service.report(days))


@router.get("/admin/feedback", response_model=list[FeedbackLogItemOut])
async def admin_feedback(
    credential: CredentialDep,
    app: AppDep,
    reporter: ActorQuery = None,
    verdict: Annotated[Verdict | None, Query()] = None,
    author: ActorQuery = None,
    fleet_id: IdQuery = None,
    entry_id: IdQuery = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = ADMIN_FEEDBACK_DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> list[FeedbackLogItemOut]:
    """Who gave which verdict on which entry, newest first: every
    feedback row of every fleet, each with the entry it rates.
    Observation only, like the activity view."""
    require_admin(credential, "the feedback log")
    filters = FeedbackLogFilters(
        reporter=reporter,
        verdict=verdict,
        author=author,
        fleet_id=fleet_id,
        entry_id=entry_id,
    )
    items = await app.governance_service.feedback_log(
        credential, filters, limit=limit, offset=offset
    )
    return [FeedbackLogItemOut.from_item(i) for i in items]


@router.get("/admin/flagged", response_model=list[FlaggedEntryOut])
async def admin_flagged(
    credential: CredentialDep,
    app: AppDep,
    fleet_id: IdQuery = None,
    older_than_days: Annotated[int, Query(ge=0, le=MAX_FLAG_AGE_DAYS)] = 0,
    orphaned: bool = False,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = ADMIN_FLAGGED_DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> list[FlaggedEntryOut]:
    """The flag backlog (ADR 0093): active entries with a standing
    ``stale`` or ``wrong`` verdict, oldest first flag first, with
    whether the author is still around. ``older_than_days`` keeps
    entries first flagged more than that many days ago; ``orphaned`` keeps
    entries with no owner. Observation only, like the health metrics."""
    require_admin(credential, "the flag backlog")
    items = await app.governance_service.flag_backlog(
        credential,
        fleet_id=fleet_id,
        older_than=timedelta(days=older_than_days),
        orphaned=orphaned,
        limit=limit,
        offset=offset,
    )
    return [FlaggedEntryOut.from_flagged(i) for i in items]


@router.get("/admin/fleets/{fleet_id}/pins", response_model=PinsOut)
async def admin_fleet_pins(fleet_id: NameParam, credential: CredentialDep, app: AppDep) -> PinsOut:
    """One fleet's pinned entries as the admin sees them: ``GET /v1/pins``
    for any fleet, newest pin first, each with why it changed since it
    was pinned. 404 for a fleet that does not exist."""
    require_admin(credential, "the fleet pins")
    if await app.store.get_fleet(fleet_id) is None:
        raise api_error(404, "not_found", f"unknown fleet: {fleet_id}")
    pinned = await app.governance_service.pinned(credential, fleet_id)
    return PinsOut(fleet_id=fleet_id, pins=[PinnedOut.from_pinned(p) for p in pinned])
