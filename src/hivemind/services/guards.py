"""Checks several services make the same way."""

from __future__ import annotations

from hivemind.domain.access import Agent, UnknownAgent
from hivemind.ports import AgentPort, Credential
from hivemind.services.errors import PermissionDenied


def require_admin(credential: Credential) -> None:
    if not credential.is_admin:
        raise PermissionDenied("this action requires an admin key (ADR 0012)")


async def require_agent(store: AgentPort, name: str) -> Agent:
    agent = await store.get_agent(name)
    if agent is None:
        raise UnknownAgent(f"unknown agent: {name}")
    return agent
