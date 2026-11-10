"""Clean up after a bad agent (ADR 0111): preview, apply, undo, list; the ``*_own`` methods are the
portal's (ADR 0118).

The data change is one store transaction; this layer validates, previews, performs the optional
revoke and audits afterwards (ADR 0027). A cleanup writes no memory.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from hivemind.domain.access import Agent, InvalidAgentStatus, TrustLevel, Visibility
from hivemind.domain.agent_cleanup import (
    CLEANUP_LIST_LIMIT,
    AgentCleanupRecord,
    CleanupOutcome,
    CleanupScan,
    CleanupUndoOutcome,
    UnknownAgentCleanup,
    check_window,
    undo_refusal,
)
from hivemind.domain.audit import AuditAction
from hivemind.domain.validation import utcnow
from hivemind.ports import Credential, Store
from hivemind.services.audit import audit_unpins, record_admin_action
from hivemind.services.errors import PermissionDenied
from hivemind.services.guards import require_admin, require_agent

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CleanupPreview:
    """What ``dry_run`` reports: the agent, the resolved window and the scan."""

    agent: Agent
    since: datetime
    until: datetime
    scan: CleanupScan


@dataclass(frozen=True, slots=True)
class CleanupView:
    """A recorded cleanup as listed, and whether it can be undone now."""

    record: AgentCleanupRecord
    undoable: bool
    blocked: str | None


ADMIN_RUN_BLOCK = "an admin ran this cleanup; only an admin can undo it"


class AgentCleanupService:
    """Clean up an agent's memories and ratings and undo it (ADR 0111). ``revoke`` is
    ``AccessService.revoke``; ``revocable`` says before anything changes whether it can run."""

    def __init__(
        self,
        store: Store,
        *,
        revoke: Callable[[str, Credential], Awaitable[None]],
        revocable: Callable[[str, Credential], Awaitable[bool]],
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._store = store
        self._revoke = revoke
        self._revocable = revocable
        self._clock = clock
        # Run after a committed cleanup or undo; a failing hook never fails the call.
        self.on_change: list[Callable[[], None]] = []

    def _changed(self) -> None:
        for hook in self.on_change:
            try:
                hook()
            except Exception:  # a cache drop must not fail a cleanup that committed
                logger.warning("agent cleanup change hook failed", exc_info=True)

    @staticmethod
    def _require_agent(credential: Credential) -> str:
        """The signed-in agent's name; the admin and org keys have none (ADR 0095). A dormant
        (level 0) agent can do nothing (SPEC §12.2), the cleanup included."""
        if credential.is_admin or credential.is_org or credential.agent_name is None:
            raise PermissionDenied("cleaning up your own work requires an agent key")
        if credential.trust_level is TrustLevel.UNTRUSTED:
            raise PermissionDenied(
                "a dormant (untrusted, level 0) agent can do nothing; ask your admin"
            )
        return credential.agent_name

    async def preview(
        self,
        agent_name: str,
        credential: Credential,
        *,
        since: datetime,
        until: datetime | None = None,
    ) -> CleanupPreview:
        """What cleaning up ``agent_name`` over ``[since, until)`` would do. ``until`` defaults to
        now and is returned resolved, so the apply can name the same window."""
        require_admin(credential)
        return await self._preview(agent_name, (agent_name,), since, until)

    async def preview_own(
        self, credential: Credential, *, since: datetime, until: datetime | None = None
    ) -> CleanupPreview:
        """``preview`` for the signed-in agent's own work (ADR 0118): every author it owns
        (ADR 0113) and its own ratings."""
        name = self._require_agent(credential)
        return await self._preview(
            name, credential.owned_authors, since, until, credential.visibility()
        )

    async def _preview(
        self,
        agent_name: str,
        authors: Sequence[str],
        since: datetime,
        until: datetime | None,
        visibility: Visibility | None = None,
    ) -> CleanupPreview:
        agent = await require_agent(self._store, agent_name)
        until = until if until is not None else self._clock()
        check_window(since, until)
        scan = await self._store.scan_agent_cleanup(
            agent.name, since, until, authors=authors, visibility=visibility
        )
        return CleanupPreview(agent, since, until, scan)

    async def cleanup(
        self,
        agent_name: str,
        credential: Credential,
        *,
        since: datetime,
        until: datetime | None = None,
        restore_ids: Sequence[str] | None = None,
        expected_withdraw: int | None = None,
        expected_ratings: int | None = None,
        also_revoke: bool = False,
    ) -> CleanupOutcome:
        """Clean up the agent, then audit one ``entry.unpin`` per pin taken off and
        ``agent.cleanup`` with the counts; nothing to clean writes neither record nor row.
        ``also_revoke`` revokes afterwards as its own audited ``agent.revoke`` (``already_revoked``
        if it was). The ``expected_*`` counts are what the preview showed. A failed audit write is
        raised; a failed revoke is not: the outcome carries ``revoke_error``."""
        require_admin(credential)
        return await self._apply(
            agent_name,
            credential,
            authors=(agent_name,),
            self_service=False,
            since=since,
            until=until,
            restore_ids=restore_ids,
            expected_withdraw=expected_withdraw,
            expected_ratings=expected_ratings,
            also_revoke=also_revoke,
        )

    async def cleanup_own(
        self,
        credential: Credential,
        *,
        since: datetime,
        until: datetime | None = None,
        restore_ids: Sequence[str] | None = None,
        expected_withdraw: int | None = None,
        expected_ratings: int | None = None,
    ) -> CleanupOutcome:
        """``cleanup`` for the signed-in agent's own work (ADR 0118): marked ``self_service``,
        audited as the agent's own act with ``via: "portal"``; no revoke."""
        name = self._require_agent(credential)
        return await self._apply(
            name,
            credential,
            authors=credential.owned_authors,
            self_service=True,
            visibility=credential.visibility(),
            since=since,
            until=until,
            restore_ids=restore_ids,
            expected_withdraw=expected_withdraw,
            expected_ratings=expected_ratings,
            also_revoke=False,
        )

    async def _apply(
        self,
        agent_name: str,
        credential: Credential,
        *,
        authors: Sequence[str],
        self_service: bool,
        visibility: Visibility | None = None,
        since: datetime,
        until: datetime | None,
        restore_ids: Sequence[str] | None,
        expected_withdraw: int | None,
        expected_ratings: int | None,
        also_revoke: bool,
    ) -> CleanupOutcome:
        agent = await require_agent(self._store, agent_name)
        until = until if until is not None else self._clock()
        check_window(since, until)
        will_revoke = False
        if also_revoke:
            # Refuse before anything changes.
            will_revoke = await self._revocable(agent.name, credential)
        outcome = await self._store.cleanup_agent(
            agent.name,
            since,
            until,
            restore_ids=restore_ids,
            expected_withdraw=expected_withdraw,
            expected_ratings=expected_ratings,
            cleaned_by=credential.audit_actor(),
            authors=authors,
            self_service=self_service,
            visibility=visibility,
        )
        if outcome.record is not None:
            self._changed()
            await self._audit_cleanup(
                agent.name,
                credential,
                outcome,
                outcome.record,
                since=since,
                until=until,
                self_service=self_service,
                also_revoke=also_revoke,
            )
        if also_revoke and not will_revoke:
            return replace(outcome, already_revoked=True)
        if will_revoke:
            return await self._revoke_after_cleanup(agent.name, credential, outcome)
        return outcome

    async def _audit_cleanup(
        self,
        agent_name: str,
        credential: Credential,
        outcome: CleanupOutcome,
        record: AgentCleanupRecord,
        *,
        since: datetime,
        until: datetime,
        self_service: bool,
        also_revoke: bool,
    ) -> None:
        """Audit the unpins the cleanup made, then the cleanup itself."""
        for pin in outcome.unpinned:
            await audit_unpins(
                self._store,
                credential,
                pin.entry_id,
                pin.fleet_id,
                [pin.entry_id],
                reason="agent_cleanup",
                cleanup_id=record.id,
                via="portal" if self_service else None,
            )
        await record_admin_action(
            self._store,
            credential,
            AuditAction.AGENT_CLEANUP,
            agent_name,
            {
                "cleanup_id": record.id,
                "since": since.isoformat(),
                "until": until.isoformat(),
                "withdrawn": outcome.withdrawn,
                "ratings_set_aside": outcome.ratings_set_aside,
                "restored": outcome.restored,
                "pins_removed": len(outcome.unpinned),
                "also_revoke": also_revoke,
                **({"via": "portal"} if self_service else {}),
            },
        )

    async def _revoke_after_cleanup(
        self, agent_name: str, credential: Credential, outcome: CleanupOutcome
    ) -> CleanupOutcome:
        """Revoke the cleaned agent and report it on ``outcome``."""
        # The cleanup is committed and audited; a failed revoke must not become an error that
        # invites running it again.
        try:
            await self._revoke(agent_name, credential)
        except InvalidAgentStatus:
            return replace(outcome, already_revoked=True)  # revoked meanwhile
        except Exception as exc:
            logger.exception("revoke after cleanup of %s failed", agent_name)
            note = (
                "the cleanup was done, but revoking the agent failed "
                f"({type(exc).__name__}); revoke it from the agents list"
            )
            return replace(outcome, revoke_error=note)
        record = outcome.record
        if record is not None:
            # A failed mark only leaves the record's badge off; not an error either.
            try:
                await self._store.mark_cleanup_revoked(record.id)
                record = replace(record, revoked=True)
            except Exception:
                logger.exception("marking cleanup %s as revoked failed", record.id)
        return replace(outcome, record=record, revoked=True)

    async def undo(self, cleanup_id: str, credential: Credential) -> CleanupUndoOutcome:
        """Undo a cleanup and audit it as ``agent.cleanup_undo``. An agent the cleanup revoked
        stays revoked."""
        require_admin(credential)
        return await self._undo(cleanup_id, credential, via={})

    async def undo_own(self, cleanup_id: str, credential: Credential) -> CleanupUndoOutcome:
        """Undo one of the agent's own cleanups from the portal (ADR 0118), audited with
        ``via: "portal"``. Another agent's is ``UnknownAgentCleanup`` (the portal says nothing of
        it); an admin-run one is ``PermissionDenied``. The store's newest-standing rule applies."""
        name = self._require_agent(credential)
        record = await self._store.get_agent_cleanup(cleanup_id)
        if record is None or record.agent != name:
            raise UnknownAgentCleanup(f"unknown agent cleanup: {cleanup_id}")
        if not record.self_service:
            raise PermissionDenied(ADMIN_RUN_BLOCK)
        return await self._undo(cleanup_id, credential, via={"via": "portal"})

    async def _undo(
        self, cleanup_id: str, credential: Credential, *, via: dict[str, str]
    ) -> CleanupUndoOutcome:
        outcome = await self._store.undo_agent_cleanup(cleanup_id)
        self._changed()
        record = outcome.record
        for pin in outcome.pins_restored:
            await record_admin_action(
                self._store,
                credential,
                AuditAction.ENTRY_PIN,
                pin.entry_id,
                {
                    "fleet_id": pin.fleet_id,
                    "reason": "agent_cleanup_undo",
                    "cleanup_id": record.id,
                    **via,
                },
            )
        await record_admin_action(
            self._store,
            credential,
            AuditAction.AGENT_CLEANUP_UNDO,
            record.agent,
            {
                "cleanup_id": record.id,
                "reactivated": outcome.reactivated,
                "reactivate_skipped": outcome.reactivate_skipped,
                "resuperseded": outcome.resuperseded,
                "resupersede_skipped": outcome.resupersede_skipped,
                "ratings_cleared": outcome.ratings_cleared,
                "pins_restored": len(outcome.pins_restored),
                "pins_skipped": len(outcome.pins_skipped),
                **via,
            },
        )
        return outcome

    async def list_cleanups(self, credential: Credential) -> list[CleanupView]:
        """Recorded cleanups, newest first, each with whether it can be undone now."""
        require_admin(credential)
        records = await self._store.list_agent_cleanups(CLEANUP_LIST_LIMIT)
        views = []
        for record in records:
            blocked = undo_refusal(record, records)
            views.append(CleanupView(record, blocked is None, blocked))
        return views

    async def list_own(self, credential: Credential) -> list[CleanupView]:
        """The agent's own cleanups, newest first, admin-run ones too (they explain why memories
        went and block an older undo), each with whether the portal can undo it now (ADR 0118)."""
        name = self._require_agent(credential)
        records = await self._store.list_agent_cleanups(CLEANUP_LIST_LIMIT, agent=name)
        views = []
        for record in records:
            blocked = undo_refusal(record, records)
            if blocked is None and not record.self_service:
                blocked = ADMIN_RUN_BLOCK
            views.append(CleanupView(record, blocked is None, blocked))
        return views
