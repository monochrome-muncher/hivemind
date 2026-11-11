"""Offboarding an owner (ADR 0135)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from hivemind.api.schemas.common import RefStr
from hivemind.services.offboarding import OffboardedAgent, Offboarding


class ExpectedAgent(BaseModel):
    """One agent and the action the preview showed for it."""

    name: RefStr
    action: Literal["retire", "revoke", "reject", "skip"]


class OffboardRequest(BaseModel):
    """``POST /v1/admin/owners/{alias}/offboard``. ``dry_run`` is required, so an offboarding is
    never started by leaving it out; ``successor`` goes with ``retire`` and only with it. An
    applying request (``dry_run: false``) carries ``expected_agents``, everything the preview
    showed: it is refused (409) when the agents are no longer planned exactly so."""

    mode: Literal["retire", "revoke"]
    successor: RefStr | None = None
    expected_agents: list[ExpectedAgent] | None = Field(default=None, max_length=1000)
    dry_run: bool

    @model_validator(mode="after")
    def _applying_needs_the_preview(self) -> OffboardRequest:
        if not self.dry_run and self.expected_agents is None:
            raise ValueError("expected_agents (from the preview) is required when dry_run is false")
        return self

    def expected(self) -> dict[str, str]:
        return {a.name: a.action for a in self.expected_agents or []}


class OffboardedAgentOut(BaseModel):
    """One agent's outcome. ``was`` is its status when planned; ``reason`` says why it is
    skipped; the counts are what a retirement passes over (previews only)."""

    name: str
    action: Literal["retire", "revoke", "reject", "skip"]
    was: str
    reason: str | None = None
    self_notes: int | None = None
    fleet_memories: int | None = None
    fleet_memories_elsewhere: int | None = None
    pins: int | None = None

    @classmethod
    def from_outcome(cls, outcome: OffboardedAgent) -> OffboardedAgentOut:
        step, facts = outcome.step, outcome.handed_down
        return cls(
            name=step.name,
            action=step.action.value,
            was=step.was.value,
            reason=step.reason,
            self_notes=facts.self_notes if facts else None,
            fleet_memories=facts.fleet_memories if facts else None,
            fleet_memories_elsewhere=facts.fleet_memories_elsewhere if facts else None,
            pins=facts.pins if facts else None,
        )


class OffboardingOut(BaseModel):
    owner: str
    mode: Literal["retire", "revoke"]
    successor: str | None
    agents: list[OffboardedAgentOut]
    dry_run: bool

    @classmethod
    def from_offboarding(cls, result: Offboarding) -> OffboardingOut:
        return cls(
            owner=result.owner,
            mode=result.mode.value,
            successor=result.successor,
            agents=[OffboardedAgentOut.from_outcome(a) for a in result.agents],
            dry_run=result.dry_run,
        )
