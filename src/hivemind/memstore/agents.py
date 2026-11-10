"""Agent registration, activation, trust and key facts."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from hivemind.domain.access import (
    ACTIVATABLE,
    REVOCABLE,
    Agent,
    AgentStatus,
    InvalidAgentStatus,
    TrustLevel,
    UnknownAgent,
)
from hivemind.domain.succession import AgentRetired
from hivemind.memstore.state import MemoryState


class AgentStore(MemoryState):
    async def register_agent(self, name: str, owner_alias: str | None = None) -> Agent:
        """Register an agent idempotently (see ``Store.register_agent``)."""
        with self._lock:
            existing = self._agents.get(name)
            if existing is not None:
                return existing
            # Insertion order is registration order: the oldest holder wins.
            for agent in self._agents.values():
                if agent.name.lower() == name.lower():
                    return agent
            agent = Agent(
                name=name,
                status=AgentStatus.PENDING,
                trust_level=TrustLevel.UNTRUSTED,
                owner_alias=owner_alias,
                created_at=self._clock(),
            )
            self._agents[name] = agent
            return agent

    async def get_agent(self, name: str) -> Agent | None:
        with self._lock:
            return self._agents.get(name)

    async def list_agents(self) -> list[Agent]:
        with self._lock:
            return [
                replace(
                    a,
                    last_used_at=self._key_last_used.get(a.name),
                    key_created_at=self._key_created.get(a.name),
                    key_locked_at=self._key_locked.get(a.name),
                )
                for a in sorted(self._agents.values(), key=lambda a: a.name)
            ]

    async def agent_key_last_used(self, name: str) -> datetime | None:
        with self._lock:
            return self._key_last_used.get(name)

    def record_key_issued(self, name: str, at: datetime) -> None:
        """A new key for the agent, made at ``at``, unused so far (ADR 0110).
        The in-memory authenticator fake calls this; the real adapter's key
        row carries it. Not on the ``Store`` port: nothing else writes it."""
        with self._lock:
            self._key_created[name] = at
            self._key_last_used.pop(name, None)
            self._key_locked.pop(name, None)  # a new key row carries no lock

    def record_key_locked(self, name: str, at: datetime) -> None:
        """The agent's key was locked from the portal at ``at`` (ADR 0117);
        the real adapter writes ``credentials.locked_at``. Not on the port."""
        with self._lock:
            self._key_locked[name] = at

    def record_key_use(self, name: str, at: datetime) -> None:
        """Stamp the agent's key as used at ``at`` (ADR 0110); the real
        adapter writes ``credentials.last_used_at``. Not on the port."""
        with self._lock:
            self._key_last_used[name] = at

    def _drop_key_facts(self, name: str) -> None:
        self._key_last_used.pop(name, None)
        self._key_created.pop(name, None)
        self._key_locked.pop(name, None)

    async def activate_agent(
        self, name: str, *, trust_level: TrustLevel, home_fleet_id: str
    ) -> Agent:
        """Activate a pending or revoked agent: set trust level + home
        fleet, flip to ``active`` (ADRs 0012, 0028). ``UnknownAgent`` if
        unknown; ``InvalidAgentStatus`` if already ``active``."""
        with self._lock:
            self._refuse_merged(home_fleet_id)
            agent = self._agents.get(name)
            if agent is None:
                raise UnknownAgent(f"unknown agent: {name}")
            if (standing := self._successions.get(name)) is not None:
                raise AgentRetired(name, standing.successor)  # ADR 0113
            if agent.status not in ACTIVATABLE:
                raise InvalidAgentStatus(name, agent.status, "activate")
            activated = replace(
                agent,
                status=AgentStatus.ACTIVE,
                trust_level=trust_level,
                home_fleet_id=home_fleet_id,
                activated_at=self._clock(),
            )
            self._drop_key_facts(name)  # the next key starts unused
            self._agents[name] = activated
            return activated

    async def revoke_agent(self, name: str) -> Agent:
        """Flip a pending or active agent to ``revoked`` (ADR 0028).
        ``UnknownAgent`` if unknown; ``InvalidAgentStatus`` if already revoked."""
        with self._lock:
            agent = self._agents.get(name)
            if agent is None:
                raise UnknownAgent(f"unknown agent: {name}")
            if agent.status not in REVOCABLE:
                raise InvalidAgentStatus(name, agent.status, "revoke")
            revoked = replace(agent, status=AgentStatus.REVOKED)
            self._drop_key_facts(name)  # the key is deleted
            self._agents[name] = revoked
            return revoked

    async def set_agent_trust_level(self, name: str, level: TrustLevel) -> Agent:
        """Promote/demote an agent's trust level (ADR 0011). ``UnknownAgent`` if
        unknown. Demotion to level 0 is *dormant* (still active, no access).
        """
        with self._lock:
            agent = self._agents.get(name)
            if agent is None:
                raise UnknownAgent(f"unknown agent: {name}")
            updated = replace(agent, trust_level=level)
            self._agents[name] = updated
            return updated

    async def set_agent_home_fleet(self, name: str, fleet_id: str) -> Agent:
        """Re-parent an agent to a new home fleet (ADR 0011). The agent's
        earlier ``fleet``-scoped entries stay in the fleet they were written
        into (never re-parented). ``UnknownAgent`` if unknown.
        """
        with self._lock:
            self._refuse_merged(fleet_id)
            agent = self._agents.get(name)
            if agent is None:
                raise UnknownAgent(f"unknown agent: {name}")
            updated = replace(agent, home_fleet_id=fleet_id)
            self._agents[name] = updated
            return updated

    async def set_agent_owner_alias(self, name: str, owner_alias: str) -> tuple[Agent, Agent]:
        """Set an agent's owner alias (ADR 0122); returns (before, after)."""
        with self._lock:
            agent = self._agents.get(name)
            if agent is None:
                raise UnknownAgent(f"unknown agent: {name}")
            updated = replace(agent, owner_alias=owner_alias)
            self._agents[name] = updated
            return agent, updated
