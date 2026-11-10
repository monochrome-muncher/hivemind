"""Retire an agent with a successor, undo it, and list them (ADR 0113). Admin only (ADR 0012).

One store transaction records the succession and revokes the agent; this layer validates, deletes
the key row and audits afterwards (ADR 0027). Nothing is rewritten: ``author`` stays the original.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from hivemind.domain.access import Agent, AgentStatus, InvalidAgentStatus
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
from hivemind.services.guards import require_admin, require_agent


@dataclass(frozen=True, slots=True)
class RetirePreview:
    """What retiring ``retired`` in favour of ``successor`` would do."""

    retired: Agent
    successor: Agent
    handed_down: HandedDown
    # The successor's own name, agents already handing down to it, then the retired chain.
    successor_owns: tuple[str, ...]
    # True when the retired agent is active, so this step revokes its key.
    revokes: bool


class SuccessionService:
    """Retire an agent with a successor and undo it (ADR 0113)."""

    def __init__(self, store: Store, get_authenticator: Callable[[], Authenticator]) -> None:
        self._store = store
        self._get_authenticator = get_authenticator

    async def preview(self, retired: str, successor: str, credential: Credential) -> RetirePreview:
        """What retiring ``retired`` in favour of ``successor`` would do, changing nothing; refuses
        what the retirement would refuse."""
        require_admin(credential)
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
        old, new = (
            await require_agent(self._store, retired),
            await require_agent(self._store, successor),
        )
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

    async def retire(self, retired: str, successor: str, credential: Credential) -> Succession:
        """Retire ``retired`` in favour of ``successor``: one store transaction records the
        succession and revokes the agent (its key stops authenticating), then the key row is
        deleted and audited: ``agent.revoke`` (``via: "retire"``) if it was active, then
        ``agent.retire``. A failed audit write is raised, never swallowed."""
        require_admin(credential)
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
        """Delete the succession of ``retired``, audited as ``agent.retire_undo``. The successor
        stops owning the memories; the agent stays revoked."""
        require_admin(credential)
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
        """Every standing succession, newest first."""
        require_admin(credential)
        return await self._store.list_successions()
