"""Offboard an owner: retire or revoke every agent they own in one step (ADR 0135). Admin only.

The store applies the plan in one transaction; this layer previews, deletes the key rows and
audits afterwards (ADR 0027), one set of rows per agent with the owner alias in the detail.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from hivemind.domain.access import Agent, AgentStatus
from hivemind.domain.audit import AuditAction
from hivemind.domain.offboarding import (
    OffboardAction,
    OffboardMode,
    OffboardStep,
    UnknownOwner,
    plan_offboarding,
    require_fitting,
)
from hivemind.domain.succession import HandedDown, owned_authors
from hivemind.ports import Authenticator, Credential, Store
from hivemind.services.audit import record_admin_action
from hivemind.services.guards import require_admin, require_agent


@dataclass(frozen=True, slots=True)
class OffboardedAgent:
    """One agent's step; ``handed_down`` counts what a retirement passes over (previews only)."""

    step: OffboardStep
    handed_down: HandedDown | None = None


@dataclass(frozen=True, slots=True)
class Offboarding:
    owner: str
    mode: OffboardMode
    successor: str | None
    agents: tuple[OffboardedAgent, ...]
    dry_run: bool


class OffboardingService:
    """Offboard an owner (ADR 0135)."""

    def __init__(self, store: Store, get_authenticator: Callable[[], Authenticator]) -> None:
        self._store = store
        self._get_authenticator = get_authenticator

    async def preview(
        self, alias: str, mode: OffboardMode, successor: str | None, credential: Credential
    ) -> Offboarding:
        """What offboarding ``alias`` would do to each agent, changing nothing; refuses what the
        offboarding would refuse."""
        require_admin(credential)
        require_fitting(mode, successor)
        owned = [a for a in await self._store.list_agents() if a.owner_alias == alias]
        if not owned:
            raise UnknownOwner(f"no agent has the owner alias {alias!r}")
        heir = await require_agent(self._store, successor) if successor is not None else None
        standing = await self._standing(owned)
        inherited = {a.name: await self._store.inherited_authors(a.name) for a in owned}
        steps = plan_offboarding(alias, mode, owned, heir, standing, inherited)
        agents = [await self._preview_agent(s, heir, inherited) for s in steps]
        return Offboarding(alias, mode, successor, tuple(agents), dry_run=True)

    async def _standing(self, owned: list[Agent]) -> dict[str, str]:
        found = {a.name: await self._store.get_succession(a.name) for a in owned}
        return {name: s.successor for name, s in found.items() if s is not None}

    async def _preview_agent(
        self, step: OffboardStep, heir: Agent | None, inherited: dict[str, tuple[str, ...]]
    ) -> OffboardedAgent:
        if step.action is not OffboardAction.RETIRE or heir is None:
            return OffboardedAgent(step)
        chain = owned_authors(step.name, inherited[step.name])
        return OffboardedAgent(step, await self._store.handed_down(chain, heir.home_fleet_id))

    async def offboard(
        self,
        alias: str,
        mode: OffboardMode,
        successor: str | None,
        expected: Mapping[str, str],
        credential: Credential,
    ) -> Offboarding:
        """Apply the offboarding in one store transaction, refused (``OffboardChanged``) when the
        plan under its locks is not ``expected``, the preview's (agent name to action). Then delete
        every affected agent's key row and audit each agent as ``retire`` and ``revoke`` do, with
        ``offboarded_owner`` in the detail. A failed audit write is raised, never swallowed."""
        require_admin(credential)
        authenticator = self._get_authenticator()  # before any change
        steps = await self._store.offboard_owner(
            alias, mode, successor, credential.audit_actor(), expected
        )
        acted = [s for s in steps if s.action is not OffboardAction.SKIP]
        for step in acted:
            await authenticator.revoke_agent_key(step.name)
        for step in acted:
            await self._audit(credential, alias, step, successor)
        return Offboarding(
            alias, mode, successor, tuple(OffboardedAgent(s) for s in steps), dry_run=False
        )

    async def _audit(
        self, credential: Credential, alias: str, step: OffboardStep, successor: str | None
    ) -> None:
        by_owner = {"offboarded_owner": alias}
        if step.action is OffboardAction.RETIRE:
            if step.was is AgentStatus.ACTIVE:
                revoke = {"from": step.was.value, "via": "retire", "successor": successor}
                await self._record(
                    credential, AuditAction.AGENT_REVOKE, step.name, {**revoke, **by_owner}
                )
            retire = {"successor": successor, "was": step.was.value}
            await self._record(
                credential, AuditAction.AGENT_RETIRE, step.name, {**retire, **by_owner}
            )
        else:
            await self._record(
                credential,
                AuditAction.AGENT_REVOKE,
                step.name,
                {"from": step.was.value, **by_owner},
            )

    async def _record(
        self, credential: Credential, action: AuditAction, target: str, detail: Mapping[str, Any]
    ) -> None:
        await record_admin_action(self._store, credential, action, target, detail)
