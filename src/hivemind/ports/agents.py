"""Agent registration and attributes."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from hivemind.domain.access import (
    Agent,
    TrustLevel,
)
from hivemind.domain.agent_role import AgentRole


@runtime_checkable
class AgentPort(Protocol):
    """Agent registration and attributes."""

    async def register_agent(self, name: str, owner_alias: str | None = None) -> Agent:
        """Register idempotently: an existing agent comes back unchanged, a new one is ``pending``
        (level 0, no fleet). A case-only variant of a name returns that agent (ADR 0045)."""
        ...

    async def get_agent(self, name: str) -> Agent | None: ...

    async def list_agents(self) -> list[Agent]: ...

    async def agent_key_last_used(self, name: str) -> datetime | None:
        """When the agent's key last authenticated, to the hour (ADR 0110);
        ``None`` if it never has, or the agent has no key (ADR 0114)."""
        ...

    async def activate_agent(
        self, name: str, *, trust_level: TrustLevel, home_fleet_id: str
    ) -> Agent:
        """Atomically flip a pending or revoked agent to ``active`` (ADR 0012, 0028).

        ``UnknownAgent``; ``InvalidAgentStatus`` if already ``active``; ``FleetMerged`` (ADR
        0107)."""
        ...

    async def revoke_agent(self, name: str) -> Agent:
        """Flip a pending or active agent to ``revoked``; the credential is the ``Authenticator``'s.

        ``UnknownAgent``; ``InvalidAgentStatus`` if already ``revoked``."""
        ...

    async def set_agent_trust_level(self, name: str, level: TrustLevel) -> Agent:
        """Change an agent's trust level (ADR 0011). Level 0 is dormant (key valid, no access), not
        revoked (ADR 0012). ``UnknownAgent``."""
        ...

    async def set_agent_home_fleet(self, name: str, fleet_id: str) -> Agent:
        """Re-parent an agent; its earlier ``fleet``-scoped entries stay where written.

        ``UnknownAgent``; ``FleetMerged`` (ADR 0107)."""
        ...

    async def set_agent_owner_alias(self, name: str, owner_alias: str) -> tuple[Agent, Agent]:
        """Set an agent's owner alias, any status (ADR 0122).

        Returns the agent before and after, read under one row lock so the audit ``from`` is the
        value replaced; an equal alias changes nothing. The alias lives only on the agent row.
        ``UnknownAgent``."""
        ...

    async def set_agent_role(self, name: str, role: AgentRole | None) -> tuple[Agent, Agent]:
        """Set an agent's role, or clear it with ``None``, any status (ADR 0144).

        Returns the agent before and after, read under one row lock, so a caller can tell whether
        anything changed; the same role changes nothing. A role is not set on a revoked agent:
        ``InvalidAgentStatus``. ``UnknownAgent``."""
        ...
