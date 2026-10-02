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

import asyncio
import logging
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Response
from pydantic import AfterValidator

from hivemind.api.deps import (
    ApiError,
    HivemindApp,
    api_error,
    require_credential,
)
from hivemind.api.prometheus import PrometheusMetrics
from hivemind.api.schemas import (
    ActivateAgentRequest,
    AgentOut,
    AgentsMetrics,
    AuditRecordOut,
    BatchFeedbackRequest,
    CreateEntryRequest,
    CreateFleetRequest,
    EntriesMetrics,
    EntriesOut,
    EntryOut,
    FeedbackOut,
    FeedbackRequest,
    FeedbackSummaryOut,
    FleetOut,
    FleetsMetrics,
    GetEntriesRequest,
    HealthOut,
    HitOut,
    KeyIssuedOut,
    MetricsOut,
    PinnedOut,
    PinOut,
    PinsOut,
    RegisterAgentRequest,
    RegisteredAgentOut,
    RelatedOut,
    SearchRequest,
    UnpinOut,
    UpdateAgentRequest,
    WhoamiOut,
    WithdrawRequest,
)
from hivemind.domain.access import Agent, InvalidAgentStatus, TrustLevel
from hivemind.domain.audit import AuditAction, AuditFilters
from hivemind.domain.entry import EntryFilters, Kind, Source
from hivemind.domain.validation import (
    MAX_IDENTITY_CHARS,
    MAX_LIMIT,
    MAX_OFFSET,
    InvalidInput,
    check_no_nul,
)
from hivemind.embeddings import EmbeddingError
from hivemind.ports import Credential
from hivemind.services.access import resolve_write_scope
from hivemind.services.chain import (
    entries_links,
    entry_links,
    get_visible_entries,
    get_visible_entry,
    supersession_chain,
)
from hivemind.services.drafts import entry_draft
from hivemind.services.errors import PermissionDenied, SupersedeDenied

require = Annotated[Credential, Depends(require_credential)]


def _no_nul(value: str | None) -> str | None:
    """A path/query string reaches Postgres as a bind parameter: no U+0000
    (ADR 0040). A ``ValueError`` here is a FastAPI 422."""
    return check_no_nul(value, "value") if value is not None else None


# Strings that may name a stored record are NUL-checked, not re-validated
# (ADR 0040). No length cap on a path name, so a legacy agent registered
# before ADR 0040 stays manageable.
NameParam = Annotated[str, Path(), AfterValidator(_no_nul)]
ActorQuery = Annotated[str | None, Query(max_length=MAX_IDENTITY_CHARS), AfterValidator(_no_nul)]

# GET /v1/admin/audit-log page size (ADR 0027): default and hard ceiling.
AUDIT_LOG_DEFAULT_LIMIT = 100
AUDIT_LOG_MAX_LIMIT = 1000

# ADR 0048: a search answered by the keyword stream alone because the
# embedding service failed says so in this response header.
DEGRADED_HEADER = "X-Hivemind-Degraded"
DEGRADED_KEYWORD_ONLY = "keyword-only"

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


def build_router(app: HivemindApp, prometheus: PrometheusMetrics | None = None) -> APIRouter:
    """Build the /v1 router (SPEC.md §5.1). ``prometheus`` counts degraded
    searches for ``GET /metrics`` (ADR 0050)."""
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

    @router.get("/metrics", response_model=MetricsOut)
    async def metrics(credential: require) -> MetricsOut:
        """Usage counters (ROADMAP §3.3, Tier 3). Admin-gated: the report
        is operational data (usage volume, fleet writes, trust-level
        distribution, pending-agent count). Makes the SPEC §10
        usage-based triggers measurable (pair with the §1.1 eval harness)."""
        if not credential.is_admin:
            raise api_error(403, "forbidden", "metrics requires an admin key")
        report = await app.metrics_service.usage_report()
        return MetricsOut(
            entries=EntriesMetrics(**report["entries"]),
            fleets=FleetsMetrics(**report["fleets"]),
            agents=AgentsMetrics(**report["agents"]),
        )

    @router.post("/entries", response_model=EntryOut, status_code=201)
    async def create_entry(payload: CreateEntryRequest, credential: require) -> EntryOut:
        """Create an entry (SPEC.md §4.1, §8.1).

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
            written = await app.write_service.write_and_relate(
                draft, writer=credential.visibility()
            )
        except SupersedeDenied as exc:
            # ADR 0033: a supersession target out of the writer's reach.
            raise api_error(403, "supersede_denied", str(exc)) from exc
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        except EmbeddingError as exc:
            raise _embedding_unavailable(exc) from exc
        except ValueError as exc:
            raise api_error(422, "invalid_entry", str(exc)) from exc
        out = EntryOut.from_entry(written.entry)
        out.related = [RelatedOut.from_related(r) for r in written.related]
        return out

    @router.get("/entries/{entry_id}", response_model=EntryOut)
    async def get_entry(
        entry_id: str,
        credential: require,
        history: bool = False,
    ) -> EntryOut:
        """Fetch the full entry, body included (SPEC.md §5.1).

        ``?history=true`` adds the supersession chain (successors +
        superseded) — the same bounded walk the MCP ``hive_get`` uses.
        ``feedback`` carries the verdict counts and the newest reports,
        notes included (ADR 0051).
        """
        # ADR 0033: follows readability; an invisible entry is a 404 like
        # an unknown one, and the chain leaves out invisible versions.
        visibility = credential.visibility()
        entry = await get_visible_entry(app.store, entry_id, visibility)
        if entry is None:
            raise api_error(404, "not_found", f"unknown entry: {entry_id}")
        summary, links = await asyncio.gather(
            app.governance_service.feedback_summary(entry.id),
            entry_links(app.store, entry.id, visibility),
        )
        out = EntryOut.from_entry(entry)
        out.feedback = FeedbackSummaryOut.from_summary(summary)
        out.set_links(links)
        if history:
            successors, superseded = await supersession_chain(
                app.store, entry, visibility=visibility
            )
            out.history = {
                "successors": [EntryOut.from_entry(e) for e in successors],
                "superseded": [EntryOut.from_entry(e) for e in superseded],
            }
        return out

    @router.post("/entries/get", response_model=EntriesOut)
    async def get_entries(request: GetEntriesRequest, credential: require) -> EntriesOut:
        """Read up to 10 entries by id in one call (ADR 0055), each with
        its feedback like ``GET /v1/entries/{id}``. Ids that name nothing
        the caller may read are listed in ``not_found`` (ADR 0033: the same
        answer for "invisible" and "does not exist")."""
        ids = list(dict.fromkeys(request.entry_ids))
        visibility = credential.visibility()
        found = await get_visible_entries(app.store, ids, visibility)
        entries = [found[eid] for eid in ids if eid in found]
        # A fixed number of store reads for the whole batch (ADR 0059).
        entry_ids = [e.id for e in entries]
        summaries, links = await asyncio.gather(
            app.governance_service.feedback_summaries(entry_ids),
            entries_links(app.store, entry_ids, visibility),
        )
        outs = []
        for entry in entries:
            out = EntryOut.from_entry(entry)
            out.feedback = FeedbackSummaryOut.from_summary(summaries[entry.id])
            out.set_links(links[entry.id])
            outs.append(out)
        return EntriesOut(entries=outs, not_found=[eid for eid in ids if eid not in found])

    @router.get("/entries", response_model=list[EntryOut])
    async def list_entries(
        credential: require,
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
        limit: Annotated[int | None, Query(ge=1, le=MAX_LIMIT)] = None,
        offset: Annotated[int | None, Query(ge=0, le=MAX_OFFSET)] = 0,
    ) -> list[EntryOut]:
        """List/filter entries without a query (SPEC.md §5.1, §5.3).

        ``entities`` (ADR 0016, SPEC §13) filters by machine-extracted
        entity names: AND-semantics, case-insensitive (the store layer
        matches on lower-cased names).
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
        )
        filters.validate()  # ADR 0040 (raises InvalidInput -> 422)
        effective_limit = limit if limit is not None else app.search_config.default_limit
        effective_offset = offset if offset is not None else 0
        entries = await app.store.list_entries(
            filters,
            limit=effective_limit,
            offset=effective_offset,
            visibility=credential.visibility(),
        )
        return [EntryOut.from_entry(e) for e in entries]

    @router.post("/search", response_model=list[HitOut])
    async def search(
        request: SearchRequest, credential: require, response: Response
    ) -> list[HitOut]:
        """Hybrid search with filters; compact hits, no bodies (SPEC.md §6).

        When the embedding service is down the hits come from the keyword
        stream alone and the response carries ``X-Hivemind-Degraded:
        keyword-only`` (ADR 0048); the body keeps its shape.
        """
        filters = EntryFilters(
            kind=request.kind,
            tags=tuple(request.tags),
            entities=tuple(request.entities),
            scope=request.scope,
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
        )
        if result.degraded:
            response.headers[DEGRADED_HEADER] = DEGRADED_KEYWORD_ONLY
            if prometheus is not None:
                prometheus.degraded_searches.inc()
        return [HitOut.from_hit(h) for h in result.hits]

    @router.post("/entries/{entry_id}/withdraw", response_model=EntryOut)
    async def withdraw(entry_id: str, request: WithdrawRequest, credential: require) -> EntryOut:
        """Withdraw an entry (SPEC.md §4.1): the author or an admin.

        404 unknown entry, 403 non-author, 409 already inactive.
        """
        try:
            entry = await app.governance_service.withdraw(credential, entry_id, request.reason)
        except LookupError:
            raise api_error(404, "not_found", f"unknown entry: {entry_id}") from None
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        except InvalidInput as exc:
            raise api_error(422, "invalid_input", str(exc)) from exc
        except ValueError as exc:
            raise api_error(409, "conflict", str(exc)) from exc
        return EntryOut.from_entry(entry)

    @router.put("/entries/{entry_id}/pin", response_model=PinOut)
    async def pin(entry_id: str, credential: require) -> PinOut:
        """Pin an active fleet entry to its fleet's briefing (ADR 0058): a
        privileged agent of that fleet or an admin. Idempotent.

        404 unknown entry, 403 not allowed, 409 inactive, 422 not a fleet
        entry or the fleet already has 10 pins.
        """
        try:
            pin = await app.governance_service.pin(credential, entry_id)
        except LookupError:
            raise api_error(404, "not_found", f"unknown entry: {entry_id}") from None
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        except InvalidInput as exc:
            raise api_error(422, "invalid_input", str(exc)) from exc
        except ValueError as exc:
            raise api_error(409, "conflict", str(exc)) from exc
        return PinOut.from_pin(pin)

    @router.delete("/entries/{entry_id}/pin", response_model=UnpinOut)
    async def unpin(entry_id: str, credential: require) -> UnpinOut:
        """Unpin an entry (ADR 0058); ``removed`` says whether it was pinned."""
        try:
            removed = await app.governance_service.unpin(credential, entry_id)
        except LookupError:
            raise api_error(404, "not_found", f"unknown entry: {entry_id}") from None
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        except InvalidInput as exc:
            raise api_error(422, "invalid_input", str(exc)) from exc
        return UnpinOut(entry_id=entry_id, removed=removed)

    @router.get("/pins", response_model=PinsOut)
    async def pins(credential: require, fleet_id: Annotated[str | None, Query()] = None) -> PinsOut:
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
        request: BatchFeedbackRequest, credential: require
    ) -> list[FeedbackOut]:
        """One verdict (and note) on up to 16 entries at once (ADR 0053).

        All or nothing: an id the caller may not read is a 404 naming it,
        and nothing is recorded.
        """
        try:
            outcomes = await app.governance_service.record_feedback_many(
                credential,
                request.entry_ids,
                request.verdict,
                request.note,
                agent=request.agent,
            )
        except LookupError as exc:
            raise api_error(404, "not_found", str(exc)) from None
        except InvalidInput as exc:
            raise api_error(422, "invalid_input", str(exc)) from exc
        except ValueError as exc:
            raise api_error(422, "agent_identity_required", str(exc)) from exc
        return [
            FeedbackOut(entry_id=o.feedback.entry_id, verdict=o.feedback.verdict, quality=o.quality)
            for o in outcomes
        ]

    @router.post("/entries/{entry_id}/feedback", response_model=FeedbackOut)
    async def feedback(entry_id: str, request: FeedbackRequest, credential: require) -> FeedbackOut:
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
        except LookupError:
            raise api_error(404, "not_found", f"unknown entry: {entry_id}") from None
        except InvalidInput as exc:
            raise api_error(422, "invalid_input", str(exc)) from exc
        except ValueError as exc:
            # ValueError: the caller's agent identity is unresolved.
            raise api_error(422, "agent_identity_required", str(exc)) from exc
        return FeedbackOut(
            entry_id=outcome.feedback.entry_id,
            verdict=outcome.feedback.verdict,
            quality=outcome.quality,
        )

    @router.get("/whoami", response_model=WhoamiOut)
    async def whoami(credential: require) -> WhoamiOut:
        """The calling key's standing (ADR 0030): its kind, the agent's
        name / status / trust level / home fleet, and what it may read
        and write. Any valid key may ask; not audited."""
        return WhoamiOut.from_standing(await app.access_service.whoami(credential))

    # --- Agent registration + fleet/trust management (ADRs 0011-0012) ------

    @router.post("/agents", response_model=RegisteredAgentOut, status_code=201)
    async def register_agent(
        payload: RegisterAgentRequest, credential: require, response: Response
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
        except ValueError as exc:
            raise api_error(409, "name_conflict", str(exc)) from exc
        if registration.already_registered:
            response.status_code = 200
        return RegisteredAgentOut(
            **AgentOut.from_agent(registration.agent).model_dump(),
            already_registered=registration.already_registered,
            message=registration.message,
        )

    @router.post("/admin/fleets", response_model=FleetOut, status_code=201)
    async def create_fleet(payload: CreateFleetRequest, credential: require) -> FleetOut:
        """Create a named fleet (admin-gated, ADR 0012)."""
        try:
            fleet = await app.access_service.create_fleet(payload.name, credential)
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        except InvalidInput as exc:
            raise api_error(422, "invalid_input", str(exc)) from exc
        except ValueError as exc:
            raise api_error(409, "fleet_exists", str(exc)) from exc
        return FleetOut.from_fleet(fleet)

    @router.get("/admin/fleets", response_model=list[FleetOut])
    async def list_fleets(credential: require) -> list[FleetOut]:
        """List fleets (admin-gated, ADR 0012)."""
        try:
            fleets = await app.access_service.list_fleets(credential)
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        return [FleetOut.from_fleet(f) for f in fleets]

    @router.get("/admin/agents", response_model=list[AgentOut])
    async def list_agents(credential: require) -> list[AgentOut]:
        """List registered agents (admin-gated, ADR 0012)."""
        try:
            agents = await app.access_service.list_agents(credential)
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        return [AgentOut.from_agent(a) for a in agents]

    @router.post("/admin/agents/{name}/activate", response_model=KeyIssuedOut)
    async def activate_agent(
        name: NameParam, payload: ActivateAgentRequest, credential: require
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
        except KeyError as exc:
            raise api_error(404, "not_found", str(exc)) from exc
        except InvalidAgentStatus as exc:
            raise api_error(409, "invalid_status", str(exc)) from exc
        return KeyIssuedOut(key=key)

    @router.patch("/admin/agents/{name}", response_model=AgentOut)
    async def update_agent(
        name: NameParam, payload: UpdateAgentRequest, credential: require
    ) -> AgentOut:
        """Change an agent's trust level and/or home fleet (admin-gated,
        ADR 0011 — SPEC §5.1 ``PATCH /v1/admin/agents/{name}``).
        Demotion to ``untrusted`` (level 0) is *dormant* (key still
        valid, no access) — distinct from revocation (ADR 0012).
        Re-parenting never moves the agent's earlier ``fleet``-scoped
        entries (they stay in the fleet they were written into).
        """
        if payload.trust_level is None and payload.home_fleet_id is None:
            raise api_error(422, "update_required", "supply trust_level and/or home_fleet_id")
        try:
            # Resolve the fleet BEFORE any mutation so a PATCH cannot half-apply.
            if (
                payload.home_fleet_id is not None
                and await app.store.get_fleet(payload.home_fleet_id) is None
            ):
                raise KeyError(f"unknown fleet: {payload.home_fleet_id}")
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
                    raise KeyError(f"unknown agent: {name}")
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        except KeyError as exc:
            raise api_error(404, "not_found", str(exc)) from exc
        return AgentOut.from_agent(agent)

    @router.post("/admin/agents/{name}/revoke")
    async def revoke_agent(name: NameParam, credential: require) -> None:
        """Revoke an agent (admin-gated, ADRs 0012, 0028 — SPEC §5.1
        ``POST /v1/admin/agents/{name}/revoke``): the key is dead and the
        status is ``revoked``; on a pending agent this rejects the
        registration. The record + name stay reserved. 404 for an
        unknown name, 409 if already revoked."""
        try:
            await app.access_service.revoke(name, credential)
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        except KeyError as exc:
            raise api_error(404, "not_found", str(exc)) from exc
        except InvalidAgentStatus as exc:
            raise api_error(409, "invalid_status", str(exc)) from exc

    @router.get("/admin/audit-log", response_model=list[AuditRecordOut])
    async def audit_log(
        credential: require,
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
    async def rotate_org_key(credential: require) -> KeyIssuedOut:
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
