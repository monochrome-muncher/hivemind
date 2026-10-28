"""The Hivemind REST routes (SPEC.md §5.1).

Routes stay thin: pydantic validation, service calls, and
exception -> ApiError mapping. No business rules live here — those
are in the service layer (SPEC.md §5, ADR 0001).

Error codes: ``missing_api_key``/``unknown_api_key`` (401),
``forbidden`` (403), ``not_found`` (404), ``conflict`` (409),
``embedding_unavailable`` (502), and ``invalid_entry``/
``agent_identity_required`` (422).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Response
from fastapi.responses import JSONResponse
from pydantic import AfterValidator

from hivemind.api.deps import (
    ApiError,
    HivemindApp,
    api_error,
    get_app,
    get_prometheus,
    require_credential,
)
from hivemind.api.prometheus import PrometheusMetrics
from hivemind.api.schemas import (
    ActivateAgentRequest,
    ActivityOut,
    AdminAgentOut,
    AdminSearchRequest,
    AgentCleanupOut,
    AgentCleanupPreviewOut,
    AgentCleanupRecordOut,
    AgentCleanupRequest,
    AgentCleanupUndoOut,
    AgentOut,
    AgentsMetrics,
    AuditRecordOut,
    BatchFeedbackRequest,
    CheckOut,
    CreateEntryRequest,
    CreateFleetRequest,
    DroppedPinOut,
    EntriesMetrics,
    EntriesOut,
    EntryOut,
    FeedbackLogItemOut,
    FeedbackMetrics,
    FeedbackOut,
    FeedbackRequest,
    FeedbackSummaryOut,
    FlaggedEntryOut,
    FleetMergeOut,
    FleetMergePreviewOut,
    FleetMergeRecordOut,
    FleetMergeRequest,
    FleetMoveOut,
    FleetMovePreviewOut,
    FleetMoveRequest,
    FleetOut,
    FleetsMetrics,
    FleetUnmergeOut,
    GetEntriesRequest,
    HealthMetrics,
    HealthOut,
    HitOut,
    KeyIssuedOut,
    MetricsOut,
    PinnedOut,
    PinOut,
    PinsOut,
    PortalSearchRequest,
    PortalWithdrawRequest,
    RegisterAgentRequest,
    RegisteredAgentOut,
    RelatedOut,
    RenameFleetRequest,
    RetiredOut,
    RetirePreviewOut,
    RetireRequest,
    SearchesMetrics,
    SearchRequest,
    SuccessionOut,
    UnpinOut,
    UpdateAgentRequest,
    WarningOut,
    WhoamiOut,
    WithdrawRequest,
)
from hivemind.domain.access import (
    Agent,
    FleetExists,
    FleetMerged,
    InvalidAgentStatus,
    TrustLevel,
    UnknownAgent,
    UnknownFleet,
)
from hivemind.domain.agent_cleanup import AgentCleanupRefused, UnknownAgentCleanup
from hivemind.domain.audit import AuditAction, AuditFilters
from hivemind.domain.entry import Entry, EntryFilters, Kind, Source
from hivemind.domain.feedback import FeedbackLogFilters, Verdict
from hivemind.domain.fleet_merge import (
    FleetMergeRefused,
    FleetMoveRefused,
    UnknownFleetMerge,
)
from hivemind.domain.recency import RecencyHint
from hivemind.domain.succession import AgentRetired, SuccessionRefused, UnknownSuccession
from hivemind.domain.validation import (
    MAX_ID_CHARS,
    MAX_IDENTITY_CHARS,
    MAX_LIMIT,
    MAX_OFFSET,
    InvalidInput,
    check_no_nul,
    utcnow,
)
from hivemind.embeddings import EmbeddingError
from hivemind.ports import Credential
from hivemind.services.access import resolve_write_scope
from hivemind.services.drafts import entry_draft
from hivemind.services.errors import (
    AgentIdentityRequired,
    EntryNotActive,
    EntryNotFound,
    NameTaken,
    PermissionDenied,
    SupersedeDenied,
)

require = Annotated[Credential, Depends(require_credential)]
hive = Annotated[HivemindApp, Depends(get_app)]
prom = Annotated[PrometheusMetrics, Depends(get_prometheus)]


def _no_nul(value: str | None) -> str | None:
    """A path/query string reaches Postgres as a bind parameter: no U+0000
    (ADR 0040). A ``ValueError`` here is a FastAPI 422."""
    return check_no_nul(value, "value") if value is not None else None


# Strings that may name a stored record are NUL-checked, not re-validated
# (ADR 0040). No length cap on a path name, so a legacy agent registered
# before ADR 0040 stays manageable.
NameParam = Annotated[str, Path(), AfterValidator(_no_nul)]
ActorQuery = Annotated[str | None, Query(max_length=MAX_IDENTITY_CHARS), AfterValidator(_no_nul)]
IdQuery = Annotated[str | None, Query(max_length=MAX_ID_CHARS), AfterValidator(_no_nul)]

# GET /v1/admin/audit-log page size (ADR 0027): default and hard ceiling.
AUDIT_LOG_DEFAULT_LIMIT = 100
AUDIT_LOG_MAX_LIMIT = 1000

# GET /v1/admin/entries default page size (ADR 0080); the ceiling is MAX_LIMIT.
ADMIN_ENTRIES_DEFAULT_LIMIT = 50
# GET /v1/admin/feedback default page size; the ceiling is MAX_LIMIT.
ADMIN_FEEDBACK_DEFAULT_LIMIT = 50
ADMIN_FLAGGED_DEFAULT_LIMIT = 50
PORTAL_DEFAULT_LIMIT = 50
MAX_FLAG_AGE_DAYS = 3650  # the flag backlog's age filter: ten years
MIN_ACTIVITY_DAYS = 1
MAX_ACTIVITY_DAYS = 365

# ADR 0048: a search answered by the keyword stream alone because the
# embedding service failed says so in this response header.
DEGRADED_HEADER = "X-Hivemind-Degraded"
DEGRADED_KEYWORD_ONLY = "keyword-only"

# ADR 0073: a search scored under a non-default recency hint says which one.
RECENCY_HEADER = "X-Hivemind-Recency"

logger = logging.getLogger(__name__)


def _embedding_unavailable(exc: EmbeddingError) -> ApiError:
    """An embedding failure is a 502 (SPEC.md §7, ADR 0005) with a fixed
    message; the detail is logged, never returned (PC-1)."""
    logger.warning("embedding unavailable: %s", exc)
    return api_error(502, "embedding_unavailable", "the embedding service is unavailable")


def _to_utc(value: datetime | None) -> datetime | None:
    """Naive query-param datetimes are assumed UTC (SPEC.md §6.4)."""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


@cache
def build_router() -> APIRouter:
    """The /v1 router (SPEC.md §5.1), built once per process and shared by
    every app: it holds no app state, and the handlers take the
    ``HivemindApp`` and the Prometheus metrics from the request. Building it
    is the costly part of ``create_app``, and the tests build hundreds of
    apps."""
    router = APIRouter(prefix="/v1")

    @router.get("/health", response_model=HealthOut)
    async def health() -> HealthOut:
        """Liveness/readiness (public, no auth — SPEC.md §5.1)."""
        return HealthOut(status="ok")

    @router.get("/liveness", response_model=HealthOut)
    async def liveness() -> HealthOut:
        """Shallow unauthenticated liveness probe (ADR 0019): 200 = the
        process answers. Public by design — k8s liveness probes carry
        no credential. (``/v1/health`` stays the static public 200 it
        is today; the REST surface has no deep DB-probe endpoint in v1.)
        """
        return HealthOut(status="ok")

    async def _entry_detail(
        app: HivemindApp, entry_id: str, credential: Credential, *, history: bool
    ) -> EntryOut:
        """The full entry with its feedback and links, and (``history``) the
        supersession chain: shared by ``GET /v1/entries/{id}`` and the admin
        read (ADR 0080)."""
        # ADR 0033: follows readability; an invisible entry is a 404 like
        # an unknown one, and the chain leaves out invisible versions.
        detail = await app.read_service.entry(
            entry_id,
            visibility=credential.visibility(),
            include_history=history,
        )
        if detail is None:
            raise api_error(404, "not_found", f"unknown entry: {entry_id}")
        out = EntryOut.from_entry(detail.entry)
        out.feedback = FeedbackSummaryOut.from_summary(detail.feedback)
        out.set_links(detail.links)
        if history:
            out.history = {
                "successors": [EntryOut.from_entry(e) for e in detail.successors],
                "superseded": [EntryOut.from_entry(e) for e in detail.superseded],
            }
        return out

    async def _search(
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

    async def _withdraw(entry_id: str, withdrawal: Awaitable[Entry]) -> EntryOut:
        """Await a governance withdrawal (audited there as ``entry.withdraw``
        when it should be) and map its typed errors."""
        try:
            entry = await withdrawal
        except EntryNotFound:
            raise api_error(404, "not_found", f"unknown entry: {entry_id}") from None
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        except InvalidInput as exc:
            raise api_error(422, "invalid_input", str(exc)) from exc
        except EntryNotActive as exc:
            raise api_error(409, "conflict", str(exc)) from exc
        return EntryOut.from_entry(entry)

    @router.get("/metrics", response_model=MetricsOut)
    async def metrics(credential: require, app: hive) -> MetricsOut:
        """Usage counters (ROADMAP §3.3, Tier 3). Admin-gated: the report
        is operational data (usage volume, fleet writes, trust-level
        distribution, pending-agent count). Makes the SPEC §10
        usage-based triggers measurable (pair with the §1.1 eval harness)."""
        if not credential.is_admin:
            raise api_error(403, "forbidden", "metrics requires an admin key")
        report = await app.metrics_service.usage_report()
        return MetricsOut(
            entries=EntriesMetrics(**asdict(report.entries)),
            fleets=FleetsMetrics(**asdict(report.fleets)),
            agents=AgentsMetrics(**asdict(report.agents)),
            searches=SearchesMetrics(**asdict(report.searches)),
            feedback=FeedbackMetrics(**asdict(report.feedback)),
            health=HealthMetrics(**asdict(report.health)) if report.health is not None else None,
        )

    @router.post(
        "/entries",
        response_model=EntryOut,
        status_code=201,
        responses={200: {"model": CheckOut, "description": "?check=true: nothing stored"}},
    )
    async def create_entry(
        payload: CreateEntryRequest,
        credential: require,
        app: hive,
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
            # Pure parsing, no service or store I/O: a ValueError here is the
            # malformed draft (domain/entry.py raises plain ValueError and InvalidInput).
            # Scope and author come from the credential (ADRs 0011-0012);
            # an out-of-permission scope is a 403.
            resolution = resolve_write_scope(credential, payload.scope)
            # Draft validation (SPEC.md §4.1): a malformed draft is a 422.
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
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
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
            written = await app.write_service.write_and_relate(
                draft, writer=credential.visibility()
            )
        except SupersedeDenied as exc:
            # ADR 0033: a supersession target out of the writer's reach.
            raise api_error(403, "supersede_denied", str(exc)) from exc
        except FleetMerged as exc:  # ADR 0107: the home fleet was merged away
            raise api_error(409, "fleet_merged", str(exc)) from exc
        except EmbeddingError as exc:
            raise _embedding_unavailable(exc) from exc
        except InvalidInput as exc:  # e.g. an unknown see_also entry
            raise api_error(422, "invalid_entry", str(exc)) from exc
        out = EntryOut.from_entry(written.entry)
        out.related = [RelatedOut.from_related(r) for r in written.related]
        out.warnings = [WarningOut.from_warning(w) for w in written.warnings]
        return out

    @router.get("/entries/{entry_id}", response_model=EntryOut)
    async def get_entry(
        entry_id: str,
        credential: require,
        app: hive,
        history: bool = False,
    ) -> EntryOut:
        """Fetch the full entry, body included (SPEC.md §5.1).

        ``?history=true`` adds the supersession chain (successors +
        superseded) — the same bounded walk the MCP ``hive_get`` uses.
        ``feedback`` carries the verdict counts and the newest reports,
        notes included (ADR 0051).
        """
        return await _entry_detail(app, entry_id, credential, history=history)

    @router.post("/entries/get", response_model=EntriesOut)
    async def get_entries(request: GetEntriesRequest, credential: require, app: hive) -> EntriesOut:
        """Read up to 10 entries by id in one call (ADR 0055), each with
        its feedback like ``GET /v1/entries/{id}``. Ids that name nothing
        the caller may read are listed in ``not_found`` (ADR 0033: the same
        answer for "invisible" and "does not exist")."""
        batch = await app.read_service.entries(
            request.entry_ids, visibility=credential.visibility()
        )
        outs = []
        for detail in batch.entries:
            out = EntryOut.from_entry(detail.entry)
            out.feedback = FeedbackSummaryOut.from_summary(detail.feedback)
            out.set_links(detail.links)
            outs.append(out)
        return EntriesOut(entries=outs, not_found=list(batch.not_found))

    @router.get("/entries", response_model=list[EntryOut])
    async def list_entries(
        credential: require,
        app: hive,
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
            occurred_from=_to_utc(occurred_from),
            occurred_to=_to_utc(occurred_to),
            created_from=_to_utc(created_from),
            created_to=_to_utc(created_to),
            include_inactive=include_inactive,
            flagged=flagged,
            relied_on_changed=relied_on_changed,
        )
        try:
            entries = await app.read_service.list(
                filters,
                limit=limit,
                offset=offset if offset is not None else 0,
                visibility=credential.visibility(),
                credential=credential,
            )
        except AgentIdentityRequired as exc:
            raise api_error(422, "agent_identity_required", str(exc)) from exc
        return [EntryOut.from_entry(e) for e in entries]

    @router.post("/search", response_model=list[HitOut])
    async def search(
        request: SearchRequest, credential: require, app: hive, prometheus: prom, response: Response
    ) -> list[HitOut]:
        """Hybrid search with filters; compact hits, no bodies (SPEC.md §6).

        When the embedding service is down the hits come from the keyword
        stream alone and the response carries ``X-Hivemind-Degraded:
        keyword-only`` (ADR 0048); the body keeps its shape. ``recency``
        (ADR 0073) is applied only when the server turned the hint on, and
        then the response carries ``X-Hivemind-Recency`` with the hint used.
        """
        return await _search(app, prometheus, request, credential, response)

    @router.post("/entries/{entry_id}/withdraw", response_model=EntryOut)
    async def withdraw(
        entry_id: str, request: WithdrawRequest, credential: require, app: hive
    ) -> EntryOut:
        """Withdraw an entry (SPEC.md §4.1): the author or an admin.

        404 unknown entry, 403 non-author, 409 already inactive.
        """
        return await _withdraw(
            entry_id, app.governance_service.withdraw(credential, entry_id, request.reason)
        )

    @router.put("/entries/{entry_id}/pin", response_model=PinOut)
    async def pin(entry_id: str, credential: require, app: hive) -> PinOut:
        """Pin an active fleet entry to its fleet's briefing (ADR 0058): a
        contributor or privileged agent of that fleet or an admin. Idempotent.

        404 unknown entry, 403 not allowed, 409 inactive, 422 not a fleet
        entry, the fleet already has 10 pins or the agent 3.
        """
        try:
            pin = await app.governance_service.pin(credential, entry_id)
        except EntryNotFound:
            raise api_error(404, "not_found", f"unknown entry: {entry_id}") from None
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        except InvalidInput as exc:
            raise api_error(422, "invalid_input", str(exc)) from exc
        except EntryNotActive as exc:
            raise api_error(409, "conflict", str(exc)) from exc
        except FleetMerged as exc:  # ADR 0107
            raise api_error(409, "fleet_merged", str(exc)) from exc
        return PinOut.from_pin(pin)

    @router.delete("/entries/{entry_id}/pin", response_model=UnpinOut)
    async def unpin(entry_id: str, credential: require, app: hive) -> UnpinOut:
        """Unpin an entry (ADR 0058); ``removed`` says whether it was pinned."""
        try:
            removed = await app.governance_service.unpin(credential, entry_id)
        except EntryNotFound:
            raise api_error(404, "not_found", f"unknown entry: {entry_id}") from None
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        except InvalidInput as exc:
            raise api_error(422, "invalid_input", str(exc)) from exc
        return UnpinOut(entry_id=entry_id, removed=removed)

    @router.get("/pins", response_model=PinsOut)
    async def pins(
        credential: require, app: hive, fleet_id: Annotated[str | None, Query()] = None
    ) -> PinsOut:
        """A fleet's pinned entries, newest pin first (ADR 0058): the
        caller's home fleet unless ``fleet_id`` names another. Entries the
        caller may not read are left out."""
        try:
            pinned = await app.governance_service.pinned(credential, fleet_id)
        except InvalidInput as exc:
            raise api_error(422, "invalid_input", str(exc)) from exc
        return PinsOut(
            fleet_id=fleet_id or credential.home_fleet_id,
            pins=[PinnedOut.from_pinned(p) for p in pinned],
        )

    @router.post("/feedback", response_model=list[FeedbackOut])
    async def batch_feedback(
        request: BatchFeedbackRequest, credential: require, app: hive
    ) -> list[FeedbackOut]:
        """One verdict (and note) on up to 16 entries at once (ADR 0053).

        All or nothing: an id the caller may not read is a 404 naming it,
        and one of the caller's own is a 403 (ADR 0065); nothing is recorded.
        """
        try:
            outcomes = await app.governance_service.record_feedback_many(
                credential,
                request.entry_ids,
                request.verdict,
                request.note,
                agent=request.agent,
            )
        except EntryNotFound as exc:
            raise api_error(404, "not_found", str(exc)) from None
        except PermissionDenied as exc:  # ADR 0065: the caller's own entry
            raise api_error(403, "forbidden", str(exc)) from exc
        except InvalidInput as exc:
            raise api_error(422, "invalid_input", str(exc)) from exc
        except AgentIdentityRequired as exc:
            raise api_error(422, "agent_identity_required", str(exc)) from exc
        return [
            FeedbackOut(entry_id=o.feedback.entry_id, verdict=o.feedback.verdict, quality=o.quality)
            for o in outcomes
        ]

    @router.post("/entries/{entry_id}/feedback", response_model=FeedbackOut)
    async def feedback(
        entry_id: str, request: FeedbackRequest, credential: require, app: hive
    ) -> FeedbackOut:
        """Report a verdict on an entry (SPEC.md §4.2); upsert per reporter.

        A plain user key self-reports the agent instance via ``agent``
        (SPEC.md §8.1); an agent sub-key's credential agent wins.
        """
        try:
            outcome = await app.governance_service.record_feedback(
                credential,
                entry_id,
                request.verdict,
                request.note,
                agent=request.agent,
            )
        except EntryNotFound:
            raise api_error(404, "not_found", f"unknown entry: {entry_id}") from None
        except PermissionDenied as exc:  # ADR 0065: the caller's own entry
            raise api_error(403, "forbidden", str(exc)) from exc
        except InvalidInput as exc:
            raise api_error(422, "invalid_input", str(exc)) from exc
        except AgentIdentityRequired as exc:
            raise api_error(422, "agent_identity_required", str(exc)) from exc
        return FeedbackOut(
            entry_id=outcome.feedback.entry_id,
            verdict=outcome.feedback.verdict,
            quality=outcome.quality,
        )

    @router.get("/whoami", response_model=WhoamiOut)
    async def whoami(credential: require, app: hive) -> WhoamiOut:
        """The calling key's standing (ADR 0030): its kind, the agent's
        name / status / trust level / home fleet, and what it may read
        and write. Any valid key may ask; not audited."""
        return WhoamiOut.from_standing(await app.access_service.whoami(credential))

    # --- Agent registration + fleet/trust management (ADRs 0011-0012) ------

    @router.post("/agents", response_model=RegisteredAgentOut, status_code=201)
    async def register_agent(
        payload: RegisterAgentRequest, credential: require, app: hive, response: Response
    ) -> RegisteredAgentOut:
        """Register (or re-register) an agent (ADR 0012, ADR 0039). Gated on
        the org or admin key; creates a ``pending`` agent (level 0, no
        fleet) → 201. The same name by the same ``owner_alias`` answers 200
        with the current status and what to do next; a name owned by
        another alias is a 409 ``name_conflict`` that says nothing more.
        """
        try:
            registration = await app.access_service.register(
                payload.name, credential, owner_alias=payload.owner_alias
            )
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        except InvalidInput as exc:  # a bad or reserved name is not a conflict (ADR 0040)
            raise api_error(422, "invalid_input", str(exc)) from exc
        except NameTaken as exc:
            raise api_error(409, "name_conflict", str(exc)) from exc
        if registration.already_registered:
            response.status_code = 200
        return RegisteredAgentOut(
            **AgentOut.from_agent(registration.agent).model_dump(),
            already_registered=registration.already_registered,
            message=registration.message,
        )

    # --- Agent portal (ADR 0095): agent keys only ------------------------

    def _require_agent(credential: Credential, what: str) -> None:
        # An active agent's own key: not the admin or org key, and not a
        # dev-mode identity (which has no agent name).
        if credential.is_admin or credential.is_org or credential.agent_name is None:
            raise api_error(
                403,
                "forbidden",
                f"{what} requires an agent key, not the admin or org key",
            )

    @router.get("/portal/entries", response_model=list[EntryOut])
    async def portal_list_entries(
        credential: require,
        app: hive,
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
        _require_agent(credential, "the agent portal")
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
        credential: require,
        app: hive,
        prometheus: prom,
        response: Response,
    ) -> list[HitOut]:
        """``POST /v1/search`` for a person holding an agent key (ADR 0095),
        with a ``fleet_id`` filter. Not counted in the search counters, so
        a person browsing does not move the agents' search numbers."""
        _require_agent(credential, "the agent portal")
        return await _search(
            app, prometheus, request, credential, response, fleet_id=request.fleet_id, count=False
        )

    @router.get("/portal/feedback", response_model=list[FeedbackLogItemOut])
    async def portal_feedback(
        credential: require,
        app: hive,
        direction: Annotated[Literal["given", "received"], Query()],
        verdict: Annotated[Verdict | None, Query()] = None,
        entry_id: IdQuery = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = PORTAL_DEFAULT_LIMIT,
        offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
    ) -> list[FeedbackLogItemOut]:
        """The agent's side of the feedback log (ADR 0095): the verdicts it
        ``given``, or the verdicts its own memories ``received``, newest
        first, on entries its key can read."""
        _require_agent(credential, "the agent portal")
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
        entry_id: str, request: PortalWithdrawRequest, credential: require, app: hive
    ) -> EntryOut:
        """Withdraw one of the agent's own entries from the portal (ADR
        0095): a reason is required and the withdrawal is audited as
        ``entry.withdraw`` with ``via: "portal"``."""
        _require_agent(credential, "the agent portal")
        return await _withdraw(
            entry_id, app.governance_service.withdraw_own(credential, entry_id, request.reason)
        )

    # --- Admin console reads (ADR 0080): admin key only ------------------

    def _require_admin(credential: Credential, what: str) -> None:
        if not credential.is_admin:
            raise api_error(403, "forbidden", f"{what} requires an admin key")

    @router.get("/admin/entries", response_model=list[EntryOut])
    async def admin_list_entries(
        credential: require,
        app: hive,
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
        flagged: bool = False,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = ADMIN_ENTRIES_DEFAULT_LIMIT,
        offset: Annotated[int | None, Query(ge=0, le=MAX_OFFSET)] = 0,
    ) -> list[EntryOut]:
        """List/filter entries as the admin sees them (ADR 0080): every
        scope of every fleet, newest first, with the ``GET /v1/entries``
        filters plus ``fleet_id``."""
        _require_admin(credential, "the admin entry list")
        filters = EntryFilters(
            kind=kind,
            tags=tuple(tags or ()),
            entities=tuple(entities or ()),
            scope=scope,
            fleet_id=fleet_id,
            author=author,
            agent=agent,
            occurred_from=_to_utc(occurred_from),
            occurred_to=_to_utc(occurred_to),
            created_from=_to_utc(created_from),
            created_to=_to_utc(created_to),
            include_inactive=include_inactive,
            flagged=flagged,
        )
        entries = await app.read_service.list(
            filters,
            limit=limit,
            offset=offset if offset is not None else 0,
            visibility=credential.visibility(),
        )
        return [EntryOut.from_entry(e) for e in entries]

    @router.get("/admin/entries/{entry_id}", response_model=EntryOut)
    async def admin_get_entry(entry_id: str, credential: require, app: hive) -> EntryOut:
        """One entry with its feedback, links and history always filled
        (ADR 0080): ``GET /v1/entries/{id}?history=true`` for the admin."""
        _require_admin(credential, "the admin entry read")
        return await _entry_detail(app, entry_id, credential, history=True)

    @router.post("/admin/search", response_model=list[HitOut])
    async def admin_search(
        request: AdminSearchRequest,
        credential: require,
        app: hive,
        prometheus: prom,
        response: Response,
    ) -> list[HitOut]:
        """``POST /v1/search`` for the admin, with a ``fleet_id`` filter
        (ADR 0080). Not counted in the search counters (ADR 0056); a
        degraded answer still counts for ``GET /metrics``."""
        _require_admin(credential, "the admin search")
        return await _search(
            app, prometheus, request, credential, response, fleet_id=request.fleet_id, count=False
        )

    @router.post("/admin/entries/{entry_id}/withdraw", response_model=EntryOut)
    async def admin_withdraw(
        entry_id: str, request: WithdrawRequest, credential: require, app: hive
    ) -> EntryOut:
        """Withdraw an entry as the admin (ADR 0080): the same call as
        ``POST /v1/entries/{id}/withdraw``, audited as ``entry.withdraw``."""
        _require_admin(credential, "the admin withdraw")
        return await _withdraw(
            entry_id, app.governance_service.withdraw(credential, entry_id, request.reason)
        )

    @router.get("/admin/activity", response_model=ActivityOut)
    async def admin_activity(
        credential: require,
        app: hive,
        days: Annotated[int, Query(ge=MIN_ACTIVITY_DAYS, le=MAX_ACTIVITY_DAYS)] = 30,
    ) -> ActivityOut:
        """Who wrote and who reported, per agent, fleet and UTC day over
        the last ``days`` days (ADR 0080). Observation only."""
        _require_admin(credential, "the activity view")
        return ActivityOut(**await app.activity_service.report(days))

    @router.get("/admin/feedback", response_model=list[FeedbackLogItemOut])
    async def admin_feedback(
        credential: require,
        app: hive,
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
        _require_admin(credential, "the feedback log")
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
        credential: require,
        app: hive,
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
        _require_admin(credential, "the flag backlog")
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
    async def admin_fleet_pins(fleet_id: NameParam, credential: require, app: hive) -> PinsOut:
        """One fleet's pinned entries as the admin sees them: ``GET /v1/pins``
        for any fleet, newest pin first, each with why it changed since it
        was pinned. 404 for a fleet that does not exist."""
        _require_admin(credential, "the fleet pins")
        if await app.store.get_fleet(fleet_id) is None:
            raise api_error(404, "not_found", f"unknown fleet: {fleet_id}")
        pinned = await app.governance_service.pinned(credential, fleet_id)
        return PinsOut(fleet_id=fleet_id, pins=[PinnedOut.from_pinned(p) for p in pinned])

    @router.post("/admin/fleets", response_model=FleetOut, status_code=201)
    async def create_fleet(payload: CreateFleetRequest, credential: require, app: hive) -> FleetOut:
        """Create a named fleet (admin-gated, ADR 0012)."""
        try:
            fleet = await app.access_service.create_fleet(payload.name, credential)
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        except InvalidInput as exc:
            raise api_error(422, "invalid_input", str(exc)) from exc
        except FleetExists as exc:
            raise api_error(409, "fleet_exists", str(exc)) from exc
        return FleetOut.from_fleet(fleet)

    @router.patch("/admin/fleets/{fleet_id}", response_model=FleetOut)
    async def rename_fleet(
        fleet_id: NameParam, payload: RenameFleetRequest, credential: require, app: hive
    ) -> FleetOut:
        """Rename a fleet (admin-gated, audited ``fleet.rename``, ADR 0109).
        The id, pins, counters and entries are unchanged; a merged fleet can
        be renamed; the current name again is a 200 with no audit row. 404
        unknown fleet, 409 ``fleet_exists`` when another fleet has the name,
        422 for a name a new fleet could not have."""
        try:
            fleet = await app.access_service.rename_fleet(fleet_id, payload.name, credential)
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        except InvalidInput as exc:
            raise api_error(422, "invalid_input", str(exc)) from exc
        except UnknownFleet as exc:
            raise api_error(404, "not_found", str(exc)) from exc
        except FleetExists as exc:
            raise api_error(409, "fleet_exists", str(exc)) from exc
        return FleetOut.from_fleet(fleet)

    @router.get("/admin/fleets", response_model=list[FleetOut])
    async def list_fleets(
        credential: require, app: hive, include_merged: bool = False
    ) -> list[FleetOut]:
        """List fleets (admin-gated, ADR 0012). Fleets merged into another
        are left out unless ``?include_merged=true`` (ADR 0107)."""
        try:
            fleets = await app.access_service.list_fleets(credential, include_merged=include_merged)
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        return [FleetOut.from_fleet(f) for f in fleets]

    @router.post(
        "/admin/fleets/{source_id}/merge",
        response_model=FleetMergePreviewOut | FleetMergeOut,
    )
    async def merge_fleet(
        source_id: NameParam, payload: FleetMergeRequest, credential: require, app: hive
    ) -> FleetMergePreviewOut | FleetMergeOut:
        """Merge a fleet into another or into a new one (admin-gated, audited
        ``fleet.merge``, ADR 0107): its memories, agents and kept pins move
        in one transaction, with no key change. Name exactly one of
        ``target_fleet_id`` and ``new_fleet_name``. ``dry_run: true`` changes
        nothing and answers the preview (counts, both fleets' pins, likely
        duplicate pairs). More than 10 pins across the two fleets need
        ``keep_pin_ids`` (422); ``expected_pin_ids`` (the pins the preview showed)
        refuses the merge when they changed since (409 ``merge_refused``). 404 unknown fleet, 409 ``fleet_merged`` /
        ``merge_refused`` / ``fleet_exists``."""
        _require_admin(credential, "merging fleets")
        merges = app.access_service.merges
        try:
            if payload.dry_run:
                preview = await merges.preview(
                    source_id,
                    credential,
                    target_fleet_id=payload.target_fleet_id,
                    new_fleet_name=payload.new_fleet_name,
                )
                return FleetMergePreviewOut.from_preview(preview)
            outcome = await merges.merge(
                source_id,
                credential,
                target_fleet_id=payload.target_fleet_id,
                new_fleet_name=payload.new_fleet_name,
                keep_pin_ids=payload.keep_pin_ids,
                expected_pin_ids=payload.expected_pin_ids,
            )
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        except InvalidInput as exc:
            raise api_error(422, "invalid_input", str(exc)) from exc
        except UnknownFleet as exc:
            raise api_error(404, "not_found", str(exc)) from exc
        except FleetMerged as exc:
            raise api_error(409, "fleet_merged", str(exc)) from exc
        except FleetMergeRefused as exc:
            raise api_error(409, "merge_refused", str(exc)) from exc
        except FleetExists as exc:
            raise api_error(409, "fleet_exists", str(exc)) from exc
        record = outcome.record
        return FleetMergeOut(
            merge_id=record.id,
            source_fleet_id=record.source_fleet_id,
            target=FleetOut.from_fleet(outcome.target),
            target_created=outcome.target_created,
            entries_moved=outcome.entries_moved,
            agents_moved=list(record.moved_agents),
            pins_kept=len(outcome.pins_kept),
            pins_dropped=[DroppedPinOut.from_dropped(p) for p in outcome.pins_dropped],
        )

    @router.post(
        "/admin/fleets/{source_id}/move",
        response_model=FleetMovePreviewOut | FleetMoveOut,
    )
    async def move_fleet_agents(
        source_id: NameParam, payload: FleetMoveRequest, credential: require, app: hive
    ) -> FleetMovePreviewOut | FleetMoveOut:
        """Move agents and the ``fleet`` memories they wrote there to another
        fleet or a new one (admin-gated, audited ``fleet.move_agents``, ADR
        0112), in one transaction and with no key change. Name exactly one of
        ``target_fleet_id`` and ``new_fleet_name``. A memory moves when its
        author is a mover and the head of its supersession chain moves too;
        its pins follow. ``dry_run: true`` changes nothing and answers how
        many memories leave, who stays and loses access, and the pins in play.
        The pin fields work as in a merge (422 over 10 pins without
        ``keep_pin_ids``; ``expected_pin_ids`` refuses when they changed).
        404 unknown fleet, 409 ``fleet_merged`` / ``move_refused`` (also an
        agent that is not in the fleet, named in the message) /
        ``fleet_exists``. Undo is the merge undo route."""
        _require_admin(credential, "moving agents between fleets")
        merges = app.access_service.merges
        try:
            if payload.dry_run:
                preview = await merges.preview_move(
                    source_id,
                    payload.agents,
                    credential,
                    target_fleet_id=payload.target_fleet_id,
                    new_fleet_name=payload.new_fleet_name,
                )
                return FleetMovePreviewOut.from_preview(preview)
            outcome = await merges.move(
                source_id,
                payload.agents,
                credential,
                target_fleet_id=payload.target_fleet_id,
                new_fleet_name=payload.new_fleet_name,
                keep_pin_ids=payload.keep_pin_ids,
                expected_pin_ids=payload.expected_pin_ids,
            )
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        except InvalidInput as exc:
            raise api_error(422, "invalid_input", str(exc)) from exc
        except UnknownFleet as exc:
            raise api_error(404, "not_found", str(exc)) from exc
        except FleetMerged as exc:
            raise api_error(409, "fleet_merged", str(exc)) from exc
        except FleetMoveRefused as exc:
            raise api_error(409, "move_refused", str(exc)) from exc
        except FleetExists as exc:
            raise api_error(409, "fleet_exists", str(exc)) from exc
        record = outcome.record
        return FleetMoveOut(
            merge_id=record.id,
            source_fleet_id=record.source_fleet_id,
            target=FleetOut.from_fleet(outcome.target),
            target_created=outcome.target_created,
            entries_moved=outcome.entries_moved,
            entries_held_back=outcome.entries_held_back,
            agents_moved=list(record.moved_agents),
            pins_kept=len(outcome.pins_kept),
            pins_dropped=[DroppedPinOut.from_dropped(p) for p in outcome.pins_dropped],
        )

    @router.get("/admin/fleet-merges", response_model=list[FleetMergeRecordOut])
    async def list_fleet_merges(credential: require, app: hive) -> list[FleetMergeRecordOut]:
        """Recorded fleet merges and moves (``kind``), newest first
        (admin-gated, ADR 0107, 0112), each with whether it can be undone now."""
        _require_admin(credential, "the fleet merges")
        views = await app.access_service.merges.list_merges(credential)
        return [FleetMergeRecordOut.from_view(v) for v in views]

    @router.post("/admin/fleet-merges/{merge_id}/undo", response_model=FleetUnmergeOut)
    async def undo_fleet_merge(
        merge_id: NameParam, credential: require, app: hive
    ) -> FleetUnmergeOut:
        """Undo a fleet merge or move (admin-gated, audited ``fleet.unmerge`` /
        ``fleet.move_undo``, ADR 0107, 0112): the recorded entries and agents
        still in the target go back, dropped pins are restored where they fit.
        Only the newest standing merge or move involving the target (and, for
        a move, its source) can be undone (409 ``merge_refused`` /
        ``move_refused``)."""
        _require_admin(credential, "undoing a fleet merge")
        try:
            outcome = await app.access_service.merges.undo(merge_id, credential)
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        except UnknownFleetMerge as exc:
            raise api_error(404, "not_found", str(exc)) from exc
        except FleetMoveRefused as exc:
            raise api_error(409, "move_refused", str(exc)) from exc
        except FleetMergeRefused as exc:
            raise api_error(409, "merge_refused", str(exc)) from exc
        record = outcome.record
        return FleetUnmergeOut(
            kind=record.kind,
            merge_id=record.id,
            source_fleet_id=record.source_fleet_id,
            target_fleet_id=record.target_fleet_id,
            entries_restored=outcome.entries_restored,
            agents_restored=list(outcome.agents_restored),
            pins_returned=outcome.pins_returned,
            pins_restored=[DroppedPinOut.from_dropped(p) for p in outcome.pins_restored],
            pins_skipped=[DroppedPinOut.from_dropped(p) for p in outcome.pins_skipped],
        )

    @router.post(
        "/admin/agents/{name}/cleanup",
        response_model=AgentCleanupPreviewOut | AgentCleanupOut,
    )
    async def cleanup_agent(
        name: NameParam, payload: AgentCleanupRequest, credential: require, app: hive
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
        _require_admin(credential, "cleaning up after an agent")
        cleanups = app.access_service.cleanups
        try:
            if payload.dry_run:
                preview = await cleanups.preview(
                    name, credential, since=payload.since, until=payload.until
                )
                return AgentCleanupPreviewOut.from_preview(preview)
            if payload.expected_withdraw is None or payload.expected_ratings is None:
                raise InvalidInput(
                    "expected_withdraw and expected_ratings (the preview's totals) are required "
                    "when dry_run is false; preview first"
                )
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
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        except InvalidInput as exc:
            raise api_error(422, "invalid_input", str(exc)) from exc
        except UnknownAgent as exc:
            raise api_error(404, "not_found", str(exc)) from exc
        except AgentCleanupRefused as exc:
            raise api_error(409, "cleanup_refused", str(exc)) from exc
        return AgentCleanupOut(
            cleanup_id=outcome.record.id if outcome.record is not None else None,
            agent=name,
            since=payload.since,
            until=until,
            withdrawn=outcome.withdrawn,
            ratings_set_aside=outcome.ratings_set_aside,
            restored=outcome.restored,
            pins_removed=[DroppedPinOut.from_dropped(p) for p in outcome.unpinned],
            revoked=outcome.revoked,
            already_revoked=outcome.already_revoked,
            revoke_error=outcome.revoke_error,
            nothing_to_clean=outcome.record is None,
        )

    @router.get("/admin/agent-cleanups", response_model=list[AgentCleanupRecordOut])
    async def list_agent_cleanups(credential: require, app: hive) -> list[AgentCleanupRecordOut]:
        """Recorded agent cleanups, newest first (admin-gated, ADR 0111), each
        with whether it can be undone now."""
        _require_admin(credential, "the agent cleanups")
        views = await app.access_service.cleanups.list_cleanups(credential)
        return [AgentCleanupRecordOut.from_view(v) for v in views]

    @router.post("/admin/agent-cleanups/{cleanup_id}/undo", response_model=AgentCleanupUndoOut)
    async def undo_agent_cleanup(
        cleanup_id: NameParam, credential: require, app: hive
    ) -> AgentCleanupUndoOut:
        """Undo an agent cleanup (admin-gated, audited ``agent.cleanup_undo``,
        ADR 0111): the memories still withdrawn by it are active again, the
        restored ones are superseded again where that still fits, its rating
        marks are cleared and its pins are restored where the fleet has room.
        It does not un-revoke the agent. Only the newest standing cleanup of
        an agent can be undone (409 ``cleanup_refused``)."""
        _require_admin(credential, "undoing an agent cleanup")
        try:
            outcome = await app.access_service.cleanups.undo(cleanup_id, credential)
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        except UnknownAgentCleanup as exc:
            raise api_error(404, "not_found", str(exc)) from exc
        except AgentCleanupRefused as exc:
            raise api_error(409, "cleanup_refused", str(exc)) from exc
        record = outcome.record
        return AgentCleanupUndoOut(
            cleanup_id=record.id,
            agent=record.agent,
            reactivated=outcome.reactivated,
            reactivate_skipped=outcome.reactivate_skipped,
            resuperseded=outcome.resuperseded,
            resupersede_skipped=outcome.resupersede_skipped,
            ratings_cleared=outcome.ratings_cleared,
            pins_restored=[DroppedPinOut.from_dropped(p) for p in outcome.pins_restored],
            pins_skipped=[DroppedPinOut.from_dropped(p) for p in outcome.pins_skipped],
            note=(
                "the agent was revoked by this cleanup and stays revoked; "
                "re-activate it separately if it should work again"
                if record.revoked
                else None
            ),
        )

    @router.get("/admin/agents", response_model=list[AdminAgentOut])
    async def list_agents(credential: require, app: hive) -> list[AdminAgentOut]:
        """List registered agents with their key's last use and whether
        they are idle (admin-gated, ADRs 0012, 0110)."""
        try:
            agents = await app.access_service.list_agents(credential)
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        now = datetime.now(UTC)
        return [
            AdminAgentOut.from_admin_agent(a, now=now, idle_days=app.idle_agent_days)
            for a in agents
        ]

    @router.post("/admin/agents/{name}/activate", response_model=KeyIssuedOut)
    async def activate_agent(
        name: NameParam, payload: ActivateAgentRequest, credential: require, app: hive
    ) -> KeyIssuedOut:
        """Activate a pending agent (admin-gated, ADR 0012): set trust
        level + home fleet, flip to ``active``, and issue the agent key
        **once** (returned here, never stored again).
        """
        try:
            _agent, key = await app.access_service.activate(
                name,
                TrustLevel(payload.trust_level),
                payload.home_fleet_id,
                credential,
            )
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        except (UnknownAgent, UnknownFleet) as exc:
            raise api_error(404, "not_found", str(exc)) from exc
        except InvalidAgentStatus as exc:
            raise api_error(409, "invalid_status", str(exc)) from exc
        except FleetMerged as exc:  # ADR 0107
            raise api_error(409, "fleet_merged", str(exc)) from exc
        except AgentRetired as exc:  # ADR 0113
            raise api_error(409, "agent_retired", str(exc)) from exc
        return KeyIssuedOut(key=key)

    @router.post("/admin/agents/{name}/key", response_model=KeyIssuedOut)
    async def replace_agent_key(name: NameParam, credential: require, app: hive) -> KeyIssuedOut:
        """Replace an active agent's key (admin-gated, ADR 0108): the old
        key stops working at once and the new one is returned **once**.
        Status, trust level, fleet and memories are untouched. 404 for an
        unknown name, 409 ``invalid_status`` unless the agent is active."""
        try:
            key = await app.access_service.replace_agent_key(name, credential)
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        except UnknownAgent as exc:
            raise api_error(404, "not_found", str(exc)) from exc
        except InvalidAgentStatus as exc:
            raise api_error(409, "invalid_status", str(exc)) from exc
        return KeyIssuedOut(key=key)

    @router.patch("/admin/agents/{name}", response_model=AgentOut)
    async def update_agent(
        name: NameParam, payload: UpdateAgentRequest, credential: require, app: hive
    ) -> AgentOut:
        """Change an agent's trust level and/or home fleet (admin-gated,
        ADR 0011 — SPEC §5.1 ``PATCH /v1/admin/agents/{name}``).
        Demotion to ``untrusted`` (level 0) is *dormant* (key still
        valid, no access) — distinct from revocation (ADR 0012).
        Re-parenting never moves the agent's earlier ``fleet``-scoped
        entries (they stay in the fleet they were written into; a fleet
        move, ADR 0112, takes them along).
        """
        # The key first: a non-admin must not learn whether a fleet exists.
        _require_admin(credential, "changing an agent")
        if payload.trust_level is None and payload.home_fleet_id is None:
            raise api_error(422, "update_required", "supply trust_level and/or home_fleet_id")
        try:
            # Resolve the fleet BEFORE any mutation so a PATCH cannot half-apply.
            if (
                payload.home_fleet_id is not None
                and await app.store.get_fleet(payload.home_fleet_id) is None
            ):
                raise UnknownFleet(f"unknown fleet: {payload.home_fleet_id}")
            agent: Agent | None = None
            if payload.trust_level is not None:
                agent = await app.access_service.set_trust_level(
                    name, TrustLevel(payload.trust_level), credential
                )
            if payload.home_fleet_id is not None:
                agent = await app.access_service.set_home_fleet(
                    name, payload.home_fleet_id, credential
                )
            if agent is None:
                # Neither field changed anything; still resolve the agent
                # (404 for an unknown name, SPEC §5.1).
                agent = await app.store.get_agent(name)
                if agent is None:
                    raise UnknownAgent(f"unknown agent: {name}")
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        except (UnknownAgent, UnknownFleet) as exc:
            raise api_error(404, "not_found", str(exc)) from exc
        except FleetMerged as exc:  # ADR 0107
            raise api_error(409, "fleet_merged", str(exc)) from exc
        return AgentOut.from_agent(agent)

    @router.post("/admin/agents/{name}/revoke")
    async def revoke_agent(name: NameParam, credential: require, app: hive) -> None:
        """Revoke an agent (admin-gated, ADRs 0012, 0028 — SPEC §5.1
        ``POST /v1/admin/agents/{name}/revoke``): the key is dead and the
        status is ``revoked``; on a pending agent this rejects the
        registration. The record + name stay reserved. 404 for an
        unknown name, 409 if already revoked."""
        try:
            await app.access_service.revoke(name, credential)
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        except UnknownAgent as exc:
            raise api_error(404, "not_found", str(exc)) from exc
        except InvalidAgentStatus as exc:
            raise api_error(409, "invalid_status", str(exc)) from exc

    @router.post(
        "/admin/agents/{name}/retire",
        response_model=RetirePreviewOut | RetiredOut,
    )
    async def retire_agent(
        name: NameParam, payload: RetireRequest, credential: require, app: hive
    ) -> RetirePreviewOut | RetiredOut:
        """Retire an agent in favour of a successor (admin-gated, audited
        ``agent.retire``, ADR 0113): the successor owns every memory the agent
        wrote (reads, supersedes and withdraws them, cannot rate them) and the
        agent is revoked in the same step. ``author`` on the memories stays
        the retired agent. ``dry_run: true`` changes nothing and answers the
        preview (counts that pass over, what the successor would own).
        404 unknown agent, 409 ``invalid_status`` (the successor is not
        active, the agent is pending) / ``succession_refused`` (already
        retired, or a cycle), 422 the same agent twice."""
        _require_admin(credential, "retiring an agent")
        successions = app.access_service.successions
        try:
            if payload.dry_run:
                preview = await successions.preview(name, payload.successor, credential)
                return RetirePreviewOut.from_preview(preview)
            succession = await successions.retire(name, payload.successor, credential)
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        except InvalidInput as exc:
            raise api_error(422, "invalid_input", str(exc)) from exc
        except UnknownAgent as exc:
            raise api_error(404, "not_found", str(exc)) from exc
        except InvalidAgentStatus as exc:
            raise api_error(409, "invalid_status", str(exc)) from exc
        except SuccessionRefused as exc:
            raise api_error(409, "succession_refused", str(exc)) from exc
        return RetiredOut.model_validate(SuccessionOut.from_succession(succession).model_dump())

    @router.get("/admin/successions", response_model=list[SuccessionOut])
    async def list_successions(credential: require, app: hive) -> list[SuccessionOut]:
        """Standing successions, newest first (admin-gated, ADR 0113)."""
        _require_admin(credential, "the successions")
        rows = await app.access_service.successions.list_successions(credential)
        return [SuccessionOut.from_succession(s) for s in rows]

    @router.post("/admin/successions/{retired}/undo", response_model=SuccessionOut)
    async def undo_succession(retired: NameParam, credential: require, app: hive) -> SuccessionOut:
        """Undo a retirement (admin-gated, audited ``agent.retire_undo``, ADR
        0113): the successor stops owning the memories. The retired agent
        stays revoked; activating it again issues a new key. 404 when no
        succession retires that agent."""
        _require_admin(credential, "undoing a retirement")
        try:
            succession = await app.access_service.successions.undo(retired, credential)
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        except UnknownSuccession as exc:
            raise api_error(404, "not_found", str(exc)) from exc
        return SuccessionOut.from_succession(succession)

    @router.get("/admin/audit-log", response_model=list[AuditRecordOut])
    async def audit_log(
        credential: require,
        app: hive,
        actor: ActorQuery = None,
        action: Annotated[AuditAction | None, Query()] = None,
        since: Annotated[datetime | None, Query()] = None,
        before: Annotated[UUID | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=AUDIT_LOG_MAX_LIMIT)] = AUDIT_LOG_DEFAULT_LIMIT,
    ) -> list[AuditRecordOut]:
        """The audit log of admin-surface actions, newest first (admin-
        gated, ADR 0027 — SPEC §12.5). Filters AND together: ``actor``
        (exact), ``action`` (the audited vocabulary), ``since`` (inclusive;
        a naive timestamp is UTC), ``before`` (a row id: only strictly
        older rows, for paging back — ADR 0028), ``limit`` (1..1000,
        default 100)."""
        since = _to_utc(since)
        if since is not None:
            try:
                # An offset that moves the value outside year 1..9999 cannot
                # be sent to Postgres (asyncpg answers a DataError: a 500).
                since.astimezone(UTC)
            except OverflowError:
                raise InvalidInput("since is out of range") from None
        filters = AuditFilters(
            actor=actor,
            action=action,
            since=since,
            before=str(before) if before is not None else None,
        )
        try:
            records = await app.access_service.list_audit(credential, filters, limit)
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        return [AuditRecordOut.from_record(r) for r in records]

    @router.post("/admin/org-key/rotate", response_model=KeyIssuedOut)
    async def rotate_org_key(credential: require, app: hive) -> KeyIssuedOut:
        """Rotate the shared org key — closes registration to every
        prior org key; active agents are unaffected (ADR 0031).
        All prior org keys stop working; the new key is returned once.
        """
        try:
            key = await app.access_service.rotate_org_key(credential)
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        return KeyIssuedOut(key=key)

    return router
