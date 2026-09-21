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

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from hivemind.api.deps import (
    HivemindApp,
    api_error,
    require_credential,
)
from hivemind.api.schemas import (
    ActivateAgentRequest,
    AgentOut,
    AgentsMetrics,
    CreateEntryRequest,
    CreateFleetRequest,
    EntriesMetrics,
    EntryOut,
    FeedbackOut,
    FeedbackRequest,
    FleetOut,
    FleetsMetrics,
    HealthOut,
    HitOut,
    KeyIssuedOut,
    MetricsOut,
    RegisterAgentRequest,
    SearchRequest,
    UpdateAgentRequest,
    WithdrawRequest,
)
from hivemind.domain.access import Agent, TrustLevel
from hivemind.domain.entry import EntryDraft, EntryFilters, Kind, Source
from hivemind.embeddings import EmbeddingError
from hivemind.ports import Credential
from hivemind.services.access import resolve_write_scope
from hivemind.services.chain import supersession_chain
from hivemind.services.governance import PermissionDenied

require = Annotated[Credential, Depends(require_credential)]


def _to_utc(value: datetime | None) -> datetime | None:
    """Naive query-param datetimes are assumed UTC (SPEC.md §6.4)."""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def build_router(app: HivemindApp) -> APIRouter:
    """Build the /v1 router (SPEC.md §5.1)."""
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
            # The write scope is resolved from the credential (ADR 0011-0012):
            # the default is the highest scope the trust level permits, an
            # out-of-permission scope is rejected (403), and the entry's
            # ``author`` is the agent's registered name (verified server-
            # side, never self-reported — ADR 0012).
            author = credential.agent_name or credential.user_id
            resolution = resolve_write_scope(credential, payload.scope)
            # Draft validation (SPEC.md §4.1) runs here: a malformed draft
            # (e.g. out-of-range importance) is a 422, not a 500.
            draft = EntryDraft(
                kind=payload.kind,
                summary=payload.summary,
                author=author,
                agent=agent,
                body=payload.body,
                payload=payload.payload,
                sources=tuple(Source(type=s.type, ref=s.ref) for s in payload.sources),
                tags=tuple(payload.tags),
                occurred_at=payload.occurred_at,
                importance=payload.importance,
                scope=resolution.scope,
                fleet_id=resolution.fleet_id,
                supersedes=tuple(payload.supersedes),
            )
            entry = await app.write_service.write(draft)
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        except EmbeddingError as exc:
            # The embedding endpoint is a dependency (SPEC.md §7, ADR 0005):
            # a failure is a 502 (bad gateway), not a bare 500 (m7).
            raise api_error(502, "embedding_unavailable", str(exc)) from exc
        except ValueError as exc:
            raise api_error(422, "invalid_entry", str(exc)) from exc
        return EntryOut.from_entry(entry)

    @router.get("/entries/{entry_id}", response_model=EntryOut)
    async def get_entry(
        entry_id: str,
        credential: require,
        history: bool = False,
    ) -> EntryOut:
        """Fetch the full entry, body included (SPEC.md §5.1).

        ``?history=true`` adds the supersession chain (successors +
        superseded) — the same bounded walk the MCP ``hive_get`` uses.
        """
        entry = await app.store.get_entry(entry_id)
        if entry is None:
            raise api_error(404, "not_found", f"unknown entry: {entry_id}")
        out = EntryOut.from_entry(entry)
        if history:
            successors, superseded = await supersession_chain(app.store, entry)
            out.history = {
                "successors": [EntryOut.from_entry(e) for e in successors],
                "superseded": [EntryOut.from_entry(e) for e in superseded],
            }
        return out

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
        limit: Annotated[int | None, Query(ge=1)] = None,
        offset: Annotated[int | None, Query(ge=0)] = 0,
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
        )
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
    async def search(request: SearchRequest, credential: require) -> list[HitOut]:
        """Hybrid search with filters; compact hits, no bodies (SPEC.md §6)."""
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
        )
        hits = await app.search_service.search(
            request.query,
            filters,
            request.limit,
            offset=request.offset,
            visibility=credential.visibility(),
        )
        return [HitOut.from_hit(h) for h in hits]

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
        except ValueError as exc:
            raise api_error(409, "conflict", str(exc)) from exc
        return EntryOut.from_entry(entry)

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
        except ValueError as exc:
            # The service raises ValueError when the caller's agent identity
            # is unresolved (a plain user key without a self-reported agent).
            raise api_error(422, "agent_identity_required", str(exc)) from exc
        return FeedbackOut(
            entry_id=outcome.feedback.entry_id,
            verdict=outcome.feedback.verdict,
            quality=outcome.quality,
        )

    # --- Agent registration + fleet/trust management (ADRs 0011-0012) ------

    @router.post("/agents", response_model=AgentOut, status_code=201)
    async def register_agent(payload: RegisterAgentRequest, credential: require) -> AgentOut:
        """Register (or re-register) an agent (ADR 0012). Gated on the org
        or admin key; creates a ``pending`` agent (level 0, no fleet).
        Re-registering a pending name is idempotent; an active name is a
        conflict (the name stays reserved — pick a new one, ADR 0012).
        """
        try:
            agent = await app.access_service.register(
                payload.name, credential, owner_alias=payload.owner_alias
            )
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        except ValueError as exc:
            raise api_error(409, "name_conflict", str(exc)) from exc
        return AgentOut.from_agent(agent)

    @router.post("/admin/fleets", response_model=FleetOut, status_code=201)
    async def create_fleet(payload: CreateFleetRequest, credential: require) -> FleetOut:
        """Create a named fleet (admin-gated, ADR 0012)."""
        try:
            fleet = await app.access_service.create_fleet(payload.name, credential)
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
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
        name: str, payload: ActivateAgentRequest, credential: require
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
        return KeyIssuedOut(key=key)

    @router.patch("/admin/agents/{name}", response_model=AgentOut)
    async def update_agent(name: str, payload: UpdateAgentRequest, credential: require) -> AgentOut:
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
    async def revoke_agent(name: str, credential: require) -> None:
        """Revoke an agent's key (admin-gated, ADR 0012 — SPEC §5.1
        ``POST /v1/admin/agents/{name}/revoke``). The agent record +
        name stay reserved (dormant); the key is dead."""
        try:
            await app.access_service.revoke(name, credential)
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc

    @router.post("/admin/org-key/rotate", response_model=KeyIssuedOut)
    async def rotate_org_key(credential: require) -> KeyIssuedOut:
        """Rotate the shared org key — the cluster kill switch (ADR 0012).
        All prior org keys stop working; the new key is returned once.
        """
        try:
            key = await app.access_service.rotate_org_key(credential)
        except PermissionDenied as exc:
            raise api_error(403, "forbidden", str(exc)) from exc
        return KeyIssuedOut(key=key)

    return router
