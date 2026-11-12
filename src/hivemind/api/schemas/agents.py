"""Agent schemas: registration, activation, creation, update, the AgentOut family, whoami and key replies."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from hivemind.api.schemas.common import RefStr
from hivemind.api.schemas.fleets import FleetPhaseOut
from hivemind.domain.access import KEY_LOCKED_MESSAGE, Agent, Standing, is_idle


class RegisterAgentRequest(BaseModel):
    """Register an agent (ADR 0012): the name is the org-unique identity
    (it becomes the entry ``author``); ``owner_alias`` is an optional
    owner contact for the org key holder (out-of-band delivery)."""

    name: str
    owner_alias: str | None = None


class ActivateAgentRequest(BaseModel):
    """Activate a pending agent: set its trust level + home
    fleet; the agent key is issued once. The trust level defaults to
    `lurker` (1) (SPEC §5.1). Out-of-range values are a 422, not a 500."""

    trust_level: int = Field(default=1, ge=0, le=3)
    home_fleet_id: RefStr


class CreateAgentRequest(BaseModel):
    """Register and activate an agent for its owner in one admin call
    : the owner alias is required, the trust level defaults to
    `lurker` (1) as on activation."""

    name: str
    owner_alias: str
    trust_level: int = Field(default=1, ge=0, le=3)
    home_fleet_id: RefStr


class UpdateAgentRequest(BaseModel):
    """Change an agent's trust level, home fleet and/or owner alias (the
    SPEC §5.1 ``PATCH /v1/admin/agents/{name}`` body; ADRs 0011, 0122). At
    least one field must be supplied. Out-of-range trust levels are a 422,
    not a 500."""

    trust_level: int | None = Field(default=None, ge=0, le=3)
    home_fleet_id: RefStr | None = None
    owner_alias: str | None = None


class AgentOut(BaseModel):
    """A registered agent."""

    name: str
    status: str
    trust_level: int
    home_fleet_id: str | None
    owner_alias: str | None = None
    created_at: str | None = None
    activated_at: str | None = None

    @classmethod
    def from_agent(cls, agent: Agent) -> AgentOut:
        return cls(
            name=agent.name,
            status=agent.status.value,
            trust_level=agent.trust_level.value,
            home_fleet_id=agent.home_fleet_id,
            owner_alias=agent.owner_alias,
            created_at=agent.created_at.isoformat() if agent.created_at else None,
            activated_at=agent.activated_at.isoformat() if agent.activated_at else None,
        )


class AdminAgentOut(AgentOut):
    """A row of ``GET /v1/admin/agents``: the agent plus when its key last
    worked and whether that makes it idle (ADR 0110). ``idle_after_days``
    is ``HIVEMIND_IDLE_AGENT_DAYS``, repeated so a panel can say it."""

    last_used_at: str | None = None
    key_locked: bool = False  # locked from the portal; replace the key
    key_locked_at: str | None = None
    idle: bool = False
    idle_after_days: int = 30

    @classmethod
    def from_admin_agent(cls, agent: Agent, *, now: datetime, idle_days: int) -> AdminAgentOut:
        return cls(
            **AgentOut.from_agent(agent).model_dump(),
            last_used_at=agent.last_used_at.isoformat() if agent.last_used_at else None,
            key_locked=agent.key_locked_at is not None,
            key_locked_at=agent.key_locked_at.isoformat() if agent.key_locked_at else None,
            idle=is_idle(agent, now, idle_days),
            idle_after_days=idle_days,
        )


class RegisteredAgentOut(AgentOut):
    """The answer to ``POST /v1/agents``: the agent record plus
    whether this caller had already registered the name (same alias) and
    what its status means for the caller (pending / active → ask the admin
    for the key / revoked)."""

    already_registered: bool
    message: str


class CreatedAgentOut(AgentOut):
    """The answer to ``POST /v1/admin/agents``: the new active
    agent plus its key, returned **once**."""

    key: str
    note: str = "store this key now; it is shown only once"


class WhoamiOut(BaseModel):
    """The calling key's standing (``GET /v1/whoami``)."""

    key_kind: str
    name: str
    status: str | None
    trust_level: int
    trust_level_name: str
    home_fleet_id: str | None
    home_fleet_name: str | None
    can_read: list[str]
    can_write_scopes: list[str]
    inherits_from: list[str]
    key_last_used_at: str | None = None
    # ADR 0143: the home fleet's phase in force; left out of the reply without one.
    fleet_phase: FleetPhaseOut | None = None

    @classmethod
    def from_standing(cls, standing: Standing) -> WhoamiOut:
        return cls.model_validate(standing.as_dict())


class KeyIssuedOut(BaseModel):
    """A freshly issued key (returned **once**, ADR 0012)."""

    key: str

    note: str = "store this key now; it is shown only once"


class KeyLockedOut(BaseModel):
    """The answer to ``POST /v1/portal/key/lock`` (ADR 0117): the key is
    locked and what to do next. ``key_id`` is the non-secret fingerprint."""

    locked: bool = True
    key_id: str
    message: str = KEY_LOCKED_MESSAGE
