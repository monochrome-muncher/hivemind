"""Offboarding an owner: every agent of one owner retired or revoked in one step."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from hivemind.domain.access import AgentStatus, UnknownAgent
from hivemind.domain.offboarding import (
    OffboardAction,
    OffboardMode,
    OffboardStep,
    UnknownOwner,
    plan_offboarding,
    require_expected,
    require_fitting,
)
from hivemind.domain.succession import Succession
from hivemind.memstore.successions import SuccessionStore


class OffboardingStore(SuccessionStore):
    async def offboard_owner(
        self,
        alias: str,
        mode: OffboardMode,
        successor: str | None,
        by: str,
        expected: Mapping[str, str] | None = None,
    ) -> list[OffboardStep]:
        require_fitting(mode, successor)
        with self._lock:
            owned = sorted(
                (a for a in self._agents.values() if a.owner_alias == alias), key=lambda a: a.name
            )
            if not owned:
                raise UnknownOwner(f"no agent has the owner alias {alias!r}")
            heir = self._agents.get(successor) if successor is not None else None
            if successor is not None and heir is None:
                raise UnknownAgent(f"unknown agent: {successor}")
            standing = {
                a.name: self._successions[a.name].successor
                for a in owned
                if a.name in self._successions
            }
            inherited = {a.name: self._inherited(a.name) for a in owned}
            steps = plan_offboarding(alias, mode, owned, heir, standing, inherited)
            require_expected(steps, expected)
            for step in steps:
                self._apply(step, successor, by)
        return steps

    def _apply(self, step: OffboardStep, successor: str | None, by: str) -> None:
        agent = self._agents[step.name]
        if step.action is OffboardAction.RETIRE and successor is not None:
            self._successions[step.name] = Succession(
                step.name, successor, by, self._clock(), was=agent.status
            )
            if agent.status is AgentStatus.ACTIVE:
                self._agents[step.name] = replace(agent, status=AgentStatus.REVOKED)
        elif step.action in (OffboardAction.REVOKE, OffboardAction.REJECT):
            self._agents[step.name] = replace(agent, status=AgentStatus.REVOKED)
            self._drop_key_facts(step.name)
