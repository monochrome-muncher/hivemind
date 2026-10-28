"""Clean up after a bad agent (ADR 0111): preview, apply, undo and the list.

The admin runs it on any agent (ADR 0012); an agent runs the same operation
on its own work from the portal (ADR 0118, the ``*_own`` methods), through
the same code. The data change is one store transaction
(``Store.cleanup_agent``); this layer validates, builds the preview, performs
the optional revoke and audits afterwards (ADR 0027), as ``FleetMergeService``
does. A cleanup writes no memory.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from hivemind.domain.access import Agent, InvalidAgentStatus, UnknownAgent, Visibility
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
from hivemind.services.audit import record_admin_action
from hivemind.services.errors import PermissionDenied

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


# Why a cleanup an admin ran cannot be undone from the portal (ADR 0118).
ADMIN_RUN_BLOCK = "an admin ran this cleanup; only an admin can undo it"


class AgentCleanupService:
    """Clean up an agent's memories and ratings and undo it (ADR 0111).

    ``revoke`` performs the audited ``agent.revoke`` (``AccessService.revoke``)
    and ``revocable`` says, before anything changes, whether it can run
    (``False`` when the agent is already revoked)."""

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
        # Run after a committed cleanup or undo (the wiring adds the metrics and
        # activity cache drops); a failing hook never fails the call.
        self.on_change: list[Callable[[], None]] = []

    def _changed(self) -> None:
        for hook in self.on_change:
            try:
                hook()
            except Exception:  # a cache drop must not fail a cleanup that committed
                logger.warning("agent cleanup change hook failed", exc_info=True)

    @staticmethod
    def _require_admin(credential: Credential) -> None:
        if not credential.is_admin:
            raise PermissionDenied("this action requires an admin key (ADR 0012)")

    @staticmethod
    def _require_agent(credential: Credential) -> str:
        """The signed-in agent's name; the admin and org keys have none (ADR 0095)."""
        if credential.is_admin or credential.is_org or credential.agent_name is None:
            raise PermissionDenied("cleaning up your own work requires an agent key")
        return credential.agent_name

    async def _agent(self, name: str) -> Agent:
        agent = await self._store.get_agent(name)
        if agent is None:
            raise UnknownAgent(f"unknown agent: {name}")
        return agent

    # -- preview ------------------------------------------------------------

    async def preview(
        self,
        agent_name: str,
        credential: Credential,
        *,
        since: datetime,
        until: datetime | None = None,
    ) -> CleanupPreview:
        """What cleaning up ``agent_name`` over ``[since, until)`` would do,
        changing nothing. ``until`` defaults to now and is returned resolved,
        so the apply can name the same window."""
        self._require_admin(credential)
        return await self._preview(agent_name, (agent_name,), since, until)

    async def preview_own(
        self, credential: Credential, *, since: datetime, until: datetime | None = None
    ) -> CleanupPreview:
        """``preview`` for the signed-in agent's own work (ADR 0118): the
        memories of every author it owns (itself and the agents retired in its
        favour, ADR 0113) and its own ratings."""
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
        agent = await self._agent(agent_name)
        until = until if until is not None else self._clock()
        check_window(since, until)
        scan = await self._store.scan_agent_cleanup(
            agent.name, since, until, authors=authors, visibility=visibility
        )
        return CleanupPreview(agent, since, until, scan)

    # -- apply --------------------------------------------------------------

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
        """Clean up the agent (admin-gated), then audit: one ``entry.unpin``
        (reason ``agent_cleanup``) per pin taken off and ``agent.cleanup``
        with the counts. With ``also_revoke`` the agent is revoked after the
        cleanup, as its own audited ``agent.revoke``; an agent that is already
        revoked is left as it is (``already_revoked``). With nothing to clean
        no record or ``agent.cleanup`` row is written. ``restore_ids`` are the
        memories to put back (none when omitted); the ``expected_*`` counts
        are what the preview showed. A failed audit write is raised, never
        swallowed; a failed revoke is not: the outcome carries ``revoke_error``."""
        self._require_admin(credential)
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
        """``cleanup`` for the signed-in agent's own work (ADR 0118): the same
        operation and record, marked ``self_service``, audited as the agent's
        own act with ``via: "portal"``. There is no revoke."""
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
        agent = await self._agent(agent_name)
        until = until if until is not None else self._clock()
        check_window(since, until)
        via = {"via": "portal"} if self_service else {}
        will_revoke = False
        if also_revoke:
            # Refuse before anything changes (no authenticator, unknown agent).
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
        record = outcome.record
        if record is not None:
            self._changed()
            for pin in outcome.unpinned:
                await record_admin_action(
                    self._store,
                    credential,
                    AuditAction.ENTRY_UNPIN,
                    pin.entry_id,
                    {
                        "fleet_id": pin.fleet_id,
                        "pin_ids": [pin.entry_id],
                        "reason": "agent_cleanup",
                        "cleanup_id": record.id,
                        **via,
                    },
                )
            await record_admin_action(
                self._store,
                credential,
                AuditAction.AGENT_CLEANUP,
                agent.name,
                {
                    "cleanup_id": record.id,
                    "since": since.isoformat(),
                    "until": until.isoformat(),
                    "withdrawn": outcome.withdrawn,
                    "ratings_set_aside": outcome.ratings_set_aside,
                    "restored": outcome.restored,
                    "pins_removed": len(outcome.unpinned),
                    "also_revoke": also_revoke,
                    **via,
                },
            )
        if also_revoke and not will_revoke:
            return replace(outcome, already_revoked=True)
        if will_revoke:
            # The cleanup is committed and audited; a failed revoke must not
            # turn that into an error the admin would answer by running it again.
            try:
                await self._revoke(agent.name, credential)
            except InvalidAgentStatus:
                return replace(outcome, already_revoked=True)  # revoked meanwhile
            except Exception as exc:
                logger.exception("revoke after cleanup of %s failed", agent.name)
                note = (
                    "the cleanup was done, but revoking the agent failed "
                    f"({type(exc).__name__}); revoke it from the agents list"
                )
                return replace(outcome, revoke_error=note)
            if record is not None:
                await self._store.mark_cleanup_revoked(record.id)
                record = replace(record, revoked=True)
            outcome = replace(outcome, record=record, revoked=True)
        return outcome

    # -- undo and the list ---------------------------------------------------

    async def undo(self, cleanup_id: str, credential: Credential) -> CleanupUndoOutcome:
        """Undo a cleanup (admin-gated) and audit it as ``agent.cleanup_undo``.
        ``UnknownAgentCleanup`` / ``AgentCleanupRefused`` per the store. An
        agent the cleanup revoked stays revoked."""
        self._require_admin(credential)
        return await self._undo(cleanup_id, credential, via={})

    async def undo_own(self, cleanup_id: str, credential: Credential) -> CleanupUndoOutcome:
        """Undo one of the signed-in agent's own cleanups from the portal
        (ADR 0118), audited with ``via: "portal"``. Only a cleanup of this
        agent that it ran itself: another agent's is an ``UnknownAgentCleanup``
        (the portal says nothing of it), one an admin ran is a
        ``PermissionDenied``. The newest-standing rule is the store's, so an
        admin cleanup of the agent that is newer blocks this one."""
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
        """Recorded cleanups, newest first, each with whether it can be undone
        now (``undo_refusal`` over the same list the store re-checks)."""
        self._require_admin(credential)
        records = await self._store.list_agent_cleanups(CLEANUP_LIST_LIMIT)
        views = []
        for record in records:
            blocked = undo_refusal(record, records)
            views.append(CleanupView(record, blocked is None, blocked))
        return views

    async def list_own(self, credential: Credential) -> list[CleanupView]:
        """The signed-in agent's own cleanups, newest first, admin-run ones
        too (they explain why memories went and block an older undo), each
        with whether it can be undone from the portal now (ADR 0118): the
        newest-standing rule over every cleanup of the agent, and only one
        the agent ran itself."""
        name = self._require_agent(credential)
        records = await self._store.list_agent_cleanups(CLEANUP_LIST_LIMIT, agent=name)
        views = []
        for record in records:
            blocked = undo_refusal(record, records)
            if blocked is None and not record.self_service:
                blocked = ADMIN_RUN_BLOCK
            views.append(CleanupView(record, blocked is None, blocked))
        return views
