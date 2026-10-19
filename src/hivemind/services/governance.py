"""Governance (SPEC.md §4.1, §8): withdrawal, plus the one service object
the surfaces hold for feedback and pins.

The write path lives in ``services.write``, feedback in
``services.feedback`` and pins in ``services.pins``; the names they
define are re-exported here so existing imports keep working.
"""

from __future__ import annotations

from datetime import timedelta

from hivemind.config import SearchConfig
from hivemind.domain.audit import AuditAction
from hivemind.domain.entry import Entry
from hivemind.domain.feedback import (
    FeedbackLogFilters,
    FeedbackLogItem,
    FeedbackSummary,
    FlaggedEntry,
    Verdict,
)
from hivemind.domain.pin import Pin, PinnedEntry
from hivemind.domain.validation import MAX_REASON_CHARS, InvalidInput, check_text
from hivemind.ports import Credential, Store
from hivemind.services.audit import record_admin_action
from hivemind.services.chain import get_visible_entry
from hivemind.services.errors import EntryNotActive, EntryNotFound

# Defined in ``services.errors``; re-exported because callers import them from here.
from hivemind.services.errors import PermissionDenied as PermissionDenied
from hivemind.services.errors import SupersedeDenied as SupersedeDenied

# Defined in the modules named above; re-exported for the same reason.
from hivemind.services.feedback import FeedbackOutcome as FeedbackOutcome
from hivemind.services.feedback import FeedbackService
from hivemind.services.pins import PinService
from hivemind.services.write import RELATED_ON_WRITE_LIMIT as RELATED_ON_WRITE_LIMIT
from hivemind.services.write import RelatedEntry as RelatedEntry
from hivemind.services.write import WriteResult as WriteResult
from hivemind.services.write import WriteService as WriteService


class GovernanceService:
    """Withdrawal (SPEC.md §4.1, §8.1): the author may withdraw their own
    entries, an admin may withdraw any. Feedback and pins are handled by
    ``FeedbackService`` and ``PinService``; this class hands those calls
    on, so the REST and MCP surfaces keep one governance object.

    ``config`` supplies the quality-formula weights (SPEC.md §6.4), so
    the reported quality matches what search uses.
    """

    def __init__(
        self,
        store: Store,
        config: SearchConfig | None = None,
        require_stored_keys: bool = False,
    ) -> None:
        self._store = store
        self._feedback = FeedbackService(store, config, require_stored_keys=require_stored_keys)
        self._pins = PinService(store, require_stored_keys=require_stored_keys)

    async def withdraw(
        self, credential: Credential, entry_id: str, reason: str | None = None
    ) -> Entry:
        check_text(reason, "reason", MAX_REASON_CHARS)  # ADR 0040
        entry = await self._visible(credential, entry_id)
        if not (credential.is_admin or entry.author == credential.user_id):
            raise PermissionDenied("only the author or an admin may withdraw an entry")
        withdrawn = await self._withdraw(credential, entry, reason)
        if credential.is_admin and entry.author != credential.user_id:
            # An admin withdrawing someone else's entry is audited (ADR 0027).
            await record_admin_action(
                self._store,
                credential,
                AuditAction.ENTRY_WITHDRAW,
                entry_id,
                {"author": entry.author, "reason": reason},
            )
        return withdrawn

    async def withdraw_own(self, credential: Credential, entry_id: str, reason: str) -> Entry:
        """A withdrawal from the agent portal (ADR 0095): the caller's own
        entry only, with a reason, audited as ``entry.withdraw`` with
        ``via: "portal"`` so an operator can tell it from an agent's own
        call. Not even the admin key may withdraw someone else's entry here."""
        check_text(reason, "reason", MAX_REASON_CHARS)  # ADR 0040
        if not reason.strip():
            raise InvalidInput("reason is required")
        entry = await self._visible(credential, entry_id)
        if entry.author != credential.user_id:
            raise PermissionDenied("only the author may withdraw an entry from the agent portal")
        withdrawn = await self._withdraw(credential, entry, reason)
        await record_admin_action(
            self._store,
            credential,
            AuditAction.ENTRY_WITHDRAW,
            entry_id,
            {"author": entry.author, "reason": reason, "via": "portal"},
        )
        return withdrawn

    async def _visible(self, credential: Credential, entry_id: str) -> Entry:
        entry = await get_visible_entry(self._store, entry_id, credential.visibility())
        if entry is None:  # unknown, or not visible to the caller (ADR 0033)
            raise EntryNotFound(f"unknown entry: {entry_id}")
        return entry

    async def _withdraw(self, credential: Credential, entry: Entry, reason: str | None) -> Entry:
        """Withdraw ``entry`` once the caller's right to has been checked."""
        if entry.state.value != "active":
            raise EntryNotActive(
                f"entry {entry.id} is already {entry.state.value}; "
                "only active entries can be withdrawn"
            )
        try:
            return await self._store.withdraw_entry(entry.id, reason, by_user=credential.user_id)
        except KeyError as exc:  # the store's "unknown entry"
            raise EntryNotFound(str(exc)) from exc
        except ValueError as exc:  # the store's "not active": a concurrent withdraw won the race
            raise EntryNotActive(str(exc)) from exc

    # -- feedback (services.feedback) -----------------------------------------

    async def record_feedback(
        self,
        credential: Credential,
        entry_id: str,
        verdict: Verdict,
        note: str | None = None,
        agent: str | None = None,
    ) -> FeedbackOutcome:
        return await self._feedback.record_feedback(credential, entry_id, verdict, note, agent)

    async def record_feedback_many(
        self,
        credential: Credential,
        entry_ids: list[str],
        verdict: Verdict,
        note: str | None = None,
        agent: str | None = None,
    ) -> list[FeedbackOutcome]:
        return await self._feedback.record_feedback_many(
            credential, entry_ids, verdict, note, agent
        )

    async def feedback_summary(self, entry_id: str) -> FeedbackSummary:
        return await self._feedback.feedback_summary(entry_id)

    async def feedback_summaries(self, entry_ids: list[str]) -> dict[str, FeedbackSummary]:
        return await self._feedback.feedback_summaries(entry_ids)

    async def quality(self, entry_id: str) -> float:
        return await self._feedback.quality(entry_id)

    async def feedback_log(
        self,
        credential: Credential,
        filters: FeedbackLogFilters,
        *,
        limit: int,
        offset: int = 0,
    ) -> list[FeedbackLogItem]:
        return await self._feedback.feedback_log(credential, filters, limit=limit, offset=offset)

    async def flag_backlog(
        self,
        credential: Credential,
        *,
        fleet_id: str | None = None,
        older_than: timedelta | None = None,
        orphaned: bool = False,
        limit: int,
        offset: int = 0,
    ) -> list[FlaggedEntry]:
        return await self._feedback.flag_backlog(
            credential,
            fleet_id=fleet_id,
            older_than=older_than,
            orphaned=orphaned,
            limit=limit,
            offset=offset,
        )

    # -- pinned entries (services.pins) ---------------------------------------

    async def pin(self, credential: Credential, entry_id: str) -> Pin:
        return await self._pins.pin(credential, entry_id)

    async def unpin(self, credential: Credential, entry_id: str) -> bool:
        return await self._pins.unpin(credential, entry_id)

    async def pinned(
        self, credential: Credential, fleet_id: str | None = None
    ) -> list[PinnedEntry]:
        return await self._pins.pinned(credential, fleet_id)
