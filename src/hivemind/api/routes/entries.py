"""Entries: write, read, list, search and withdraw."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response
from fastapi.responses import JSONResponse

from hivemind.api.deps import api_error
from hivemind.api.routes.params import API_PREFIX, AppDep, CredentialDep, PrometheusDep
from hivemind.api.routes.shared import (
    entry_detail,
    run_search,
    to_utc,
)
from hivemind.api.schemas import (
    CheckOut,
    CreateEntryRequest,
    EntriesOut,
    EntryOut,
    GetEntriesRequest,
    HitOut,
    RelatedOut,
    SearchRequest,
    WarningOut,
    WithdrawRequest,
)
from hivemind.domain.entry import EntryFilters, Kind, Source
from hivemind.domain.validation import (
    MAX_LIMIT,
    MAX_OFFSET,
    InvalidInput,
)
from hivemind.services.access import resolve_write_scope
from hivemind.services.drafts import entry_draft

router = APIRouter(prefix=API_PREFIX)


@router.post(
    "/entries",
    response_model=EntryOut,
    status_code=201,
    responses={200: {"model": CheckOut, "description": "?check=true: nothing stored"}},
)
async def create_entry(
    payload: CreateEntryRequest,
    credential: CredentialDep,
    app: AppDep,
    check: bool = False,
) -> EntryOut | JSONResponse:
    """Create an entry (SPEC.md §4.1, §8.1).

    ``?check=true`` runs every validation and reports ``related`` and
    ``warnings`` as 200 ``{"checked": true, ...}``, storing nothing.

    ``author`` is stamped from the credential; ``agent`` comes
    from the agent sub-key, or the self-reported value for plain
    user keys. Entries are immutable once created (ADR 0001).
    """
    agent = credential.agent_id or payload.agent
    if agent is None:
        raise api_error(
            422,
            "agent_identity_required",
            "agent identity is required: use an agent sub-key or pass 'agent'",
        )
    try:
        # Pure parsing: a ValueError here is a malformed draft; an out-of-permission scope is a 403.
        resolution = resolve_write_scope(credential, payload.scope)
        draft = entry_draft(
            credential,
            resolution,
            agent=agent,
            kind=payload.kind,
            summary=payload.summary,
            body=payload.body,
            payload=payload.payload,
            sources=tuple(Source(type=s.type, ref=s.ref) for s in payload.sources),
            tags=payload.tags,
            occurred_at=payload.occurred_at,
            importance=payload.importance,
            supersedes=payload.supersedes,
            see_also=payload.see_also,
        )
    except ValueError as exc:
        raise api_error(422, "invalid_entry", str(exc)) from exc
    try:
        if check:
            checked = await app.write_service.check(draft, writer=credential.visibility())
            return JSONResponse(
                CheckOut(
                    related=[RelatedOut.from_related(r) for r in checked.related],
                    warnings=[WarningOut.from_warning(w) for w in checked.warnings],
                ).model_dump(mode="json")
            )
        written = await app.write_service.write_and_relate(draft, writer=credential.visibility())
    except InvalidInput as exc:  # e.g. an unknown see_also entry
        raise api_error(422, "invalid_entry", str(exc)) from exc
    out = EntryOut.from_entry(written.entry)
    out.related = [RelatedOut.from_related(r) for r in written.related]
    out.warnings = [WarningOut.from_warning(w) for w in written.warnings]
    return out


@router.get("/entries/{entry_id}", response_model=EntryOut)
async def get_entry(
    entry_id: str,
    credential: CredentialDep,
    app: AppDep,
    history: bool = False,
) -> EntryOut:
    """Fetch the full entry, body included (SPEC.md §5.1).

    ``?history=true`` adds the supersession chain (successors +
    superseded) — the same bounded walk the MCP ``hive_get`` uses.
    ``feedback`` carries the verdict counts and the newest reports,
    notes included (ADR 0051).
    """
    return await entry_detail(app, entry_id, credential, history=history)


@router.post("/entries/get", response_model=EntriesOut)
async def get_entries(
    request: GetEntriesRequest, credential: CredentialDep, app: AppDep
) -> EntriesOut:
    """Read up to 10 entries by id in one call (ADR 0055), each with
    its feedback like ``GET /v1/entries/{id}``. Ids that name nothing
    the caller may read are listed in ``not_found`` (ADR 0033: the same
    answer for "invisible" and "does not exist")."""
    batch = await app.read_service.entries(request.entry_ids, visibility=credential.visibility())
    return EntriesOut(
        entries=[EntryOut.from_detail(d) for d in batch.entries],
        not_found=list(batch.not_found),
    )


@router.get("/entries", response_model=list[EntryOut])
async def list_entries(
    credential: CredentialDep,
    app: AppDep,
    kind: Annotated[Kind | None, Query()] = None,
    tags: Annotated[list[str] | None, Query()] = None,
    entities: Annotated[list[str] | None, Query()] = None,
    scope: Annotated[str | None, Query()] = None,
    author: Annotated[str | None, Query()] = None,
    agent: Annotated[str | None, Query()] = None,
    occurred_from: Annotated[datetime | None, Query()] = None,
    occurred_to: Annotated[datetime | None, Query()] = None,
    created_from: Annotated[datetime | None, Query()] = None,
    created_to: Annotated[datetime | None, Query()] = None,
    include_inactive: bool = False,
    flagged: bool = False,
    relied_on_changed: bool = False,
    limit: Annotated[int | None, Query(ge=1, le=MAX_LIMIT)] = None,
    offset: Annotated[int | None, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> list[EntryOut]:
    """List/filter entries without a query (SPEC.md §5.1, §5.3).

    ``entities`` (ADR 0016, SPEC §13) filters by machine-extracted
    entity names: AND-semantics, case-insensitive (the store layer
    matches on lower-cased names). ``relied_on_changed`` (ADR 0098) lists
    the entries the caller rated helpful that were superseded, withdrawn
    or flagged since; it needs an agent identity.
    """
    filters = EntryFilters(
        kind=kind,
        tags=tuple(tags or ()),
        entities=tuple(entities or ()),
        scope=scope,
        author=author,
        agent=agent,
        occurred_from=to_utc(occurred_from),
        occurred_to=to_utc(occurred_to),
        created_from=to_utc(created_from),
        created_to=to_utc(created_to),
        include_inactive=include_inactive,
        flagged=flagged,
        relied_on_changed=relied_on_changed,
    )
    entries = await app.read_service.list(
        filters,
        limit=limit,
        offset=offset if offset is not None else 0,
        visibility=credential.visibility(),
        credential=credential,
    )
    return [EntryOut.from_entry(e) for e in entries]


@router.post("/search", response_model=list[HitOut])
async def search(
    request: SearchRequest,
    credential: CredentialDep,
    app: AppDep,
    prometheus: PrometheusDep,
    response: Response,
) -> list[HitOut]:
    """Hybrid search with filters; compact hits, no bodies (SPEC.md §6).

    When the embedding service is down the hits come from the keyword
    stream alone and the response carries ``X-Hivemind-Degraded:
    keyword-only`` (ADR 0048); the body keeps its shape. ``recency``
    (ADR 0073) is applied only when the server turned the hint on, and
    then the response carries ``X-Hivemind-Recency`` with the hint used.
    """
    return await run_search(app, prometheus, request, credential, response)


@router.post("/entries/{entry_id}/withdraw", response_model=EntryOut)
async def withdraw(
    entry_id: str, request: WithdrawRequest, credential: CredentialDep, app: AppDep
) -> EntryOut:
    """Withdraw an entry (SPEC.md §4.1): the author or an admin.

    404 unknown entry, 403 non-author, 409 already inactive.
    """
    return EntryOut.from_entry(
        await app.governance_service.withdraw(credential, entry_id, request.reason)
    )
