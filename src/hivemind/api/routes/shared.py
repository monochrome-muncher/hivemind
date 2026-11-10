"""Helpers used by more than one route module."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import Response

from hivemind.api.deps import HivemindApp, api_error
from hivemind.api.prometheus import PrometheusMetrics
from hivemind.api.schemas import (
    AgentCleanupRequest,
    EntryOut,
    HitOut,
    PinOut,
    PortalCleanupRequest,
    SearchRequest,
    UnpinOut,
)
from hivemind.domain.entry import EntryFilters
from hivemind.domain.recency import RecencyHint
from hivemind.domain.validation import InvalidInput
from hivemind.ports import Credential

# Set when a search was answered by the keyword stream alone (ADR 0048).
DEGRADED_HEADER = "X-Hivemind-Degraded"
DEGRADED_KEYWORD_ONLY = "keyword-only"

# Names a non-default recency hint (ADR 0073).
RECENCY_HEADER = "X-Hivemind-Recency"


def check_cleanup_expectations(payload: AgentCleanupRequest | PortalCleanupRequest) -> None:
    """An apply names the preview's totals (``InvalidInput`` otherwise)."""
    if payload.expected_withdraw is None or payload.expected_ratings is None:
        raise InvalidInput(
            "expected_withdraw and expected_ratings (the preview's totals) are required "
            "when dry_run is false; preview first"
        )


def to_utc(value: datetime | None) -> datetime | None:
    """Naive query-param datetimes are assumed UTC (SPEC.md §6.4)."""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def require_admin(credential: Credential, what: str) -> None:
    if not credential.is_admin:
        raise api_error(403, "forbidden", f"{what} requires an admin key")


async def entry_detail(
    app: HivemindApp,
    entry_id: str,
    credential: Credential,
    *,
    history: bool,
    model: type[EntryOut] = EntryOut,
) -> EntryOut:
    """The full entry with its feedback and links, and (``history``) the
    supersession chain: shared by ``GET /v1/entries/{id}`` and the admin
    read (ADR 0080)."""
    # ADR 0033: an invisible entry is a 404, and the chain leaves out invisible versions.
    detail = await app.read_service.entry(
        entry_id,
        visibility=credential.visibility(),
        include_history=history,
    )
    if detail is None:
        raise api_error(404, "not_found", f"unknown entry: {entry_id}")
    out = model.from_detail(detail)
    if history:
        out.history = {
            "successors": [model.from_entry(e) for e in detail.successors],
            "superseded": [model.from_entry(e) for e in detail.superseded],
        }
    return out


async def run_search(
    app: HivemindApp,
    prometheus: PrometheusMetrics,
    request: SearchRequest,
    credential: Credential,
    response: Response,
    *,
    fleet_id: str | None = None,
    count: bool = True,
) -> list[HitOut]:
    """The search behind ``POST /v1/search`` and the admin search;
    ``count=False`` keeps the latter out of the search counters."""
    filters = EntryFilters(
        kind=request.kind,
        tags=tuple(request.tags),
        entities=tuple(request.entities),
        scope=request.scope,
        fleet_id=fleet_id,
        author=request.author,
        agent=request.agent,
        occurred_from=request.occurred_from,
        occurred_to=request.occurred_to,
        created_from=request.created_from,
        created_to=request.created_to,
        include_inactive=request.include_inactive,
        flagged=request.flagged,
    )
    result = await app.search_service.search_result(
        request.query,
        filters,
        request.limit,
        offset=request.offset,
        visibility=credential.visibility(),
        explore=request.explore,
        recency=request.recency or RecencyHint.DEFAULT,
        count=count,
    )
    if result.recency is not RecencyHint.DEFAULT:
        response.headers[RECENCY_HEADER] = result.recency.value
    if result.degraded:
        response.headers[DEGRADED_HEADER] = DEGRADED_KEYWORD_ONLY
        prometheus.degraded_searches.inc()
    return [HitOut.from_hit(h) for h in result.hits]


async def pin_entry(
    app: HivemindApp, credential: Credential, entry_id: str, via: str | None = None
) -> PinOut:
    pin = await app.governance_service.pin(credential, entry_id, via)
    return PinOut.from_pin(pin)


async def unpin_entry(
    app: HivemindApp, credential: Credential, entry_id: str, via: str | None = None
) -> UnpinOut:
    removed = await app.governance_service.unpin(credential, entry_id, via)
    return UnpinOut(entry_id=entry_id, removed=removed)
