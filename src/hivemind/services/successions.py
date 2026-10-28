"""Retire an agent with a successor, undo it, and list them (ADR 0113).

Admin only (ADR 0012). The ownership change is one store transaction
(``Store.retire_agent``: the succession row and the revoke of the retired
agent); this layer validates, builds the preview, deletes the retired agent's
key row and audits afterwards (ADR 0027), as ``AccessService`` does. Nothing
is rewritten: ``author`` stays the original agent on every memory.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from hivemind.domain.access import Agent, AgentStatus, InvalidAgentStatus, UnknownAgent
from hivemind.domain.audit import AuditAction
from hivemind.domain.succession import (
    HandedDown,
    Succession,
    SuccessionRefused,
    owned_authors,
)
from hivemind.domain.validation import InvalidInput
from hivemind.ports import Authenticator, Credential, Store
from hivemind.services.audit import record_admin_action
from hivemind.services.errors import PermissionDenied


@dataclass(frozen=True, slots=True)
class RetirePreview:
    """What retiring ``retired`` in favour of ``successor`` would do."""

    retired: Agent
    successor: Agent
    handed_down: HandedDown
    # Everything the successor would own afterwards (its own name, the agents
    # that already hand down to it, then the retired agent's chain).
    successor_owns: tuple[str, ...]
    # Whether the retired agent's key is revoked by this step (it is active).
    revokes: bool


class SuccessionService:
    """Retire an agent with a successor and undo it (ADR 0113)."""

    def __init__(self, store: Store, get_authenticator: Callable[[], Authenticator]) -> None:
        self._store = store
        self._get_authenticator = get_authenticator

    @staticmethod
    def _require_admin(credential: Credential) -> None:
        if not credential.is_admin:
            raise PermissionDenied("this action requires an admin key (ADR 0012)")

    async def _agent(self, name: str) -> Agent:
        agent = await self._store.get_agent(name)
        if agent is None:
            raise UnknownAgent(f"unknown agent: {name}")
        return agent

    # -- preview ------------------------------------------------------------

    async def preview(self, retired: str, successor: str, credential: Credential) -> RetirePreview:
        """What retiring ``retired`` in favour of ``successor`` would do,
        changing nothing: the memories that pass over (active ``self`` notes,
        ``fleet`` memories, pins) and the chain. Refuses what the retirement
        would refuse (``InvalidInput`` for the same agent, ``UnknownAgent``,
        ``InvalidAgentStatus``, ``SuccessionRefused``)."""
        self._require_admin(credential)
        old, new = await self._check(retired, successor)
        chain = owned_authors(retired, await self._store.inherited_authors(retired))
        facts = await self._store.handed_down(chain, new.home_fleet_id)
        mine = owned_authors(successor, await self._store.inherited_authors(successor))
        return RetirePreview(
            retired=old,
            successor=new,
            handed_down=facts,
            successor_owns=owned_authors(successor, (*mine[1:], *chain)),
            revokes=old.status is AgentStatus.ACTIVE,
        )

    async def _check(self, retired: str, successor: str) -> tuple[Agent, Agent]:
        """The refusals the store re-checks under its locks."""
        if retired == successor:
            raise InvalidInput("an agent cannot be its own successor")
        old, new = await self._agent(retired), await self._agent(successor)
        if old.status is AgentStatus.PENDING:
            raise InvalidAgentStatus(retired, old.status, "retire")
        if new.status is not AgentStatus.ACTIVE:
            raise InvalidAgentStatus(successor, new.status, "make a successor of")
        standing = await self._store.get_succession(retired)
        if standing is not None:
            raise SuccessionRefused(
                f"agent {retired!r} is already retired in favour of {standing.successor!r}"
            )
        if successor in await self._store.inherited_authors(retired):
            raise SuccessionRefused(
                f"{successor!r} already hands its memories down to {retired!r}, "
                "directly or through others: that would be a cycle"
            )
        return old, new

    # -- retire and undo ------------------------------------------------------

    async def retire(self, retired: str, successor: str, credential: Credential) -> Succession:
        """Retire ``retired`` in favour of ``successor`` (admin-gated): one
        store transaction records the succession and revokes the agent (its
        key stops authenticating with the status), then its key row is
        deleted and the action audited: ``agent.revoke`` (as for any revoke,
        with ``via: "retire"``) when the agent was active, then ``agent.retire``.
        A failed audit write is raised, never swallowed."""
        self._require_admin(credential)
        authenticator = self._get_authenticator()  # before any change
        if retired == successor:
            raise InvalidInput("an agent cannot be its own successor")
        succession = await self._store.retire_agent(retired, successor, credential.audit_actor())
        was = succession.was or AgentStatus.ACTIVE
        await authenticator.revoke_agent_key(retired)
        if was is AgentStatus.ACTIVE:
            await record_admin_action(
                self._store,
                credential,
                AuditAction.AGENT_REVOKE,
                retired,
                {"from": was.value, "via": "retire", "successor": successor},
            )
        await record_admin_action(
            self._store,
            credential,
            AuditAction.AGENT_RETIRE,
            retired,
            {"successor": successor, "was": was.value},
        )
        return succession

    async def undo(self, retired: str, credential: Credential) -> Succession:
        """Delete the succession of ``retired`` (admin-gated), audited as
        ``agent.retire_undo``. The successor stops owning the memories; the
        agent stays revoked (activating it needs a new key, as for any revoked
        agent). ``UnknownSuccession`` when none stands."""
        self._require_admin(credential)
        succession = await self._store.undo_succession(retired)
        await record_admin_action(
            self._store,
            credential,
            AuditAction.AGENT_RETIRE_UNDO,
            retired,
            {"successor": succession.successor},
        )
        return succession

    async def list_successions(self, credential: Credential) -> list[Succession]:
        """Every standing succession, newest first (admin-gated)."""
        self._require_admin(credential)
        return await self._store.list_successions()
