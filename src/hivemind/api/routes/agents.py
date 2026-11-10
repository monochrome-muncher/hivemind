"""The calling key's standing and agent registration."""

from __future__ import annotations

from fastapi import APIRouter, Response

from hivemind.api.routes.params import API_PREFIX, AppDep, CredentialDep
from hivemind.api.schemas import (
    AgentOut,
    RegisterAgentRequest,
    RegisteredAgentOut,
    WhoamiOut,
)

router = APIRouter(prefix=API_PREFIX)


@router.get("/whoami", response_model=WhoamiOut)
async def whoami(credential: CredentialDep, app: AppDep) -> WhoamiOut:
    """The calling key's standing (ADR 0030): its kind, the agent's
    name / status / trust level / home fleet, and what it may read
    and write. Any valid key may ask; not audited."""
    return WhoamiOut.from_standing(await app.access_service.whoami(credential))


@router.post("/agents", response_model=RegisteredAgentOut, status_code=201)
async def register_agent(
    payload: RegisterAgentRequest, credential: CredentialDep, app: AppDep, response: Response
) -> RegisteredAgentOut:
    """Register (or re-register) an agent (ADR 0012, ADR 0039). Gated on
    the org or admin key; creates a ``pending`` agent (level 0, no
    fleet) → 201. The same name by the same ``owner_alias`` answers 200
    with the current status and what to do next; a name owned by
    another alias is a 409 ``name_conflict`` that says nothing more.
    """
    registration = await app.access_service.register(
        payload.name, credential, owner_alias=payload.owner_alias
    )
    if registration.already_registered:
        response.status_code = 200
    return RegisteredAgentOut(
        **AgentOut.from_agent(registration.agent).model_dump(),
        already_registered=registration.already_registered,
        message=registration.message,
    )
