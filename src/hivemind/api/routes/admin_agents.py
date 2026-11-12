"""Admin agent management: create, activate, rekey, update, revoke, retire."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from hivemind.api.deps import api_error
from hivemind.api.routes.params import API_PREFIX, AppDep, CredentialDep, NameParam
from hivemind.api.routes.shared import (
    require_admin,
)
from hivemind.api.schemas import (
    ActivateAgentRequest,
    AdminAgentOut,
    AgentOut,
    CreateAgentRequest,
    CreatedAgentOut,
    KeyIssuedOut,
    OffboardingOut,
    OffboardRequest,
    RetiredOut,
    RetirePreviewOut,
    RetireRequest,
    SuccessionOut,
    UpdateAgentRequest,
)
from hivemind.domain.access import TrustLevel
from hivemind.domain.offboarding import OffboardMode

router = APIRouter(prefix=API_PREFIX)


@router.get("/admin/agents", response_model=list[AdminAgentOut])
async def list_agents(credential: CredentialDep, app: AppDep) -> list[AdminAgentOut]:
    """List registered agents with their key's last use and whether
    they are idle (admin-gated, ADRs 0012, 0110)."""
    agents = await app.access_service.list_agents(credential)
    now = datetime.now(UTC)
    return [
        AdminAgentOut.from_admin_agent(a, now=now, idle_days=app.idle_agent_days) for a in agents
    ]


@router.post("/admin/agents", response_model=CreatedAgentOut, status_code=201)
async def create_agent(
    payload: CreateAgentRequest, credential: CredentialDep, app: AppDep
) -> CreatedAgentOut:
    """Register and activate an agent for its owner in one call
    (admin-gated, ADR 0120): the same registration and activation as
    ``POST /v1/agents`` followed by ``activate``, audited the same way.
    The key is returned **once**. An existing name is a 409
    ``name_conflict``; an unknown fleet a 404."""
    agent, key = await app.access_service.create_agent(
        payload.name,
        payload.owner_alias,
        TrustLevel(payload.trust_level),
        payload.home_fleet_id,
        credential,
    )
    return CreatedAgentOut(**AgentOut.from_agent(agent).model_dump(), key=key)


@router.post("/admin/agents/{name}/activate", response_model=KeyIssuedOut)
async def activate_agent(
    name: NameParam, payload: ActivateAgentRequest, credential: CredentialDep, app: AppDep
) -> KeyIssuedOut:
    """Activate a pending agent (admin-gated, ADR 0012): set trust
    level + home fleet, flip to ``active``, and issue the agent key
    **once** (returned here, never stored again).
    """
    _agent, key = await app.access_service.activate(
        name,
        TrustLevel(payload.trust_level),
        payload.home_fleet_id,
        credential,
    )
    return KeyIssuedOut(key=key)


@router.post("/admin/agents/{name}/key", response_model=KeyIssuedOut)
async def replace_agent_key(
    name: NameParam, credential: CredentialDep, app: AppDep
) -> KeyIssuedOut:
    """Replace an active agent's key (admin-gated, ADR 0108): the old
    key stops working at once and the new one is returned **once**.
    Status, trust level, fleet and memories are untouched. 404 for an
    unknown name, 409 ``invalid_status`` unless the agent is active."""
    key = await app.access_service.replace_agent_key(name, credential)
    return KeyIssuedOut(key=key)


@router.patch("/admin/agents/{name}", response_model=AgentOut)
async def update_agent(
    name: NameParam, payload: UpdateAgentRequest, credential: CredentialDep, app: AppDep
) -> AgentOut:
    """Change an agent's trust level, home fleet and/or owner alias
    (admin-gated, ADRs 0011, 0122 — SPEC §5.1 ``PATCH /v1/admin/agents/{name}``).
    Demotion to ``untrusted`` (level 0) is *dormant* (key still
    valid, no access) — distinct from revocation (ADR 0012).
    Re-parenting never moves the agent's earlier ``fleet``-scoped
    entries (they stay in the fleet they were written into; a fleet
    move, ADR 0112, takes them along).
    """
    # The key first: a non-admin must not learn whether a fleet exists.
    require_admin(credential, "changing an agent")
    if (
        payload.trust_level is None
        and payload.home_fleet_id is None
        and payload.owner_alias is None
    ):
        raise api_error(
            422, "update_required", "supply trust_level, home_fleet_id and/or owner_alias"
        )
    agent = await app.access_service.update_agent(
        name,
        credential,
        trust_level=(TrustLevel(payload.trust_level) if payload.trust_level is not None else None),
        home_fleet_id=payload.home_fleet_id,
        owner_alias=payload.owner_alias,
    )
    return AgentOut.from_agent(agent)


@router.post("/admin/agents/{name}/revoke")
async def revoke_agent(name: NameParam, credential: CredentialDep, app: AppDep) -> None:
    """Revoke an agent (admin-gated, ADRs 0012, 0028 — SPEC §5.1
    ``POST /v1/admin/agents/{name}/revoke``): the key is dead and the
    status is ``revoked``; on a pending agent this rejects the
    registration. The record + name stay reserved. 404 for an
    unknown name, 409 if already revoked."""
    await app.access_service.revoke(name, credential)


@router.post(
    "/admin/agents/{name}/retire",
    response_model=RetirePreviewOut | RetiredOut,
)
async def retire_agent(
    name: NameParam, payload: RetireRequest, credential: CredentialDep, app: AppDep
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
    require_admin(credential, "retiring an agent")
    successions = app.access_service.successions
    if payload.dry_run:
        preview = await successions.preview(name, payload.successor, credential)
        return RetirePreviewOut.from_preview(preview)
    succession = await successions.retire(name, payload.successor, credential)
    return RetiredOut.model_validate(SuccessionOut.from_succession(succession).model_dump())


@router.get("/admin/successions", response_model=list[SuccessionOut])
async def list_successions(credential: CredentialDep, app: AppDep) -> list[SuccessionOut]:
    """Standing successions, newest first (admin-gated, ADR 0113)."""
    require_admin(credential, "the successions")
    rows = await app.access_service.successions.list_successions(credential)
    return [SuccessionOut.from_succession(s) for s in rows]


@router.post("/admin/successions/{retired}/undo", response_model=SuccessionOut)
async def undo_succession(
    retired: NameParam, credential: CredentialDep, app: AppDep
) -> SuccessionOut:
    """Undo a retirement (admin-gated, audited ``agent.retire_undo``, ADR
    0113): the successor stops owning the memories. The retired agent
    stays revoked; activating it again issues a new key. 404 when no
    succession retires that agent."""
    require_admin(credential, "undoing a retirement")
    succession = await app.access_service.successions.undo(retired, credential)
    return SuccessionOut.from_succession(succession)


@router.post("/admin/owners/{alias:path}/offboard", response_model=OffboardingOut)
async def offboard_owner(
    alias: NameParam, payload: OffboardRequest, credential: CredentialDep, app: AppDep
) -> OffboardingOut:
    """Offboard an owner (admin-gated, ADR 0135): ``retire`` hands every one of their agents'
    memories to ``successor`` as ``POST /v1/admin/agents/{name}/retire`` does, ``revoke`` revokes
    them all; pending agents are rejected, revoked and retired ones skipped. The alias matches
    exactly as stored. ``dry_run: true`` answers the plan per agent and changes nothing; otherwise
    everything is applied in one transaction, audited per agent as ``agent.retire`` and
    ``agent.revoke`` with ``offboarded_owner``. 404 ``unknown_owner`` / unknown successor, 409
    ``invalid_status`` (the successor is not active) / ``succession_refused`` (the successor
    belongs to the same owner, or a cycle), 409 ``offboard_changed`` when the agents are no longer planned as the
    request's ``expected_agents`` (required when applying), 422 ``retire`` without or ``revoke`` with a
    successor."""
    require_admin(credential, "offboarding an owner")
    offboarding = app.access_service.offboarding
    mode = OffboardMode(payload.mode)
    if payload.dry_run:
        result = await offboarding.preview(alias, mode, payload.successor, credential)
    else:
        result = await offboarding.offboard(
            alias, mode, payload.successor, payload.expected(), credential
        )
    return OffboardingOut.from_offboarding(result)
