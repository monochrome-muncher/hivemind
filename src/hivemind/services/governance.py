"""Governance (SPEC.md §4.1, §8): withdrawal, plus the one service object
the surfaces hold for feedback and pins.

The write path lives in ``services.write``, feedback in
``services.feedback`` and pins in ``services.pins``; the names they
define are re-exported here so existing imports keep working.
"""

from __future__ import annotations

from hivemind.config import SearchConfig
from hivemind.domain.audit import AuditAction
from hivemind.domain.entry import Entry
from hivemind.domain.feedback import FeedbackSummary, Verdict
from hivemind.domain.pin import Pin, PinnedEntry
from hivemind.domain.validation import MAX_REASON_CHARS, check_text
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

    def __init__(self, store: Store, config: SearchConfig | None = None) -> None:
        self._store = store
        self._feedback = FeedbackService(store, config)
        self._pins = PinService(store)

    async def withdraw(
        self, credential: Credential, entry_id: str, reason: str | None = None
    ) -> Entry:
        check_text(reason, "reason", MAX_REASON_CHARS)  # ADR 0040
        entry = await get_visible_entry(self._store, entry_id, credential.visibility())
        if entry is None:  # unknown, or not visible to the caller (ADR 0033)
            raise EntryNotFound(f"unknown entry: {entry_id}")
        if not (credential.is_admin or entry.author == credential.user_id):
            raise PermissionDenied("only the author or an admin may withdraw an entry")
        if entry.state.value != "active":
            raise EntryNotActive(
                f"entry {entry_id} is already {entry.state.value}; "
                "only active entries can be withdrawn"
            )
        withdrawn = await self._store.withdraw_entry(entry_id, reason, by_user=credential.user_id)
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

    # -- pinned entries (services.pins) ---------------------------------------

    async def pin(self, credential: Credential, entry_id: str) -> Pin:
        return await self._pins.pin(credential, entry_id)

    async def unpin(self, credential: Credential, entry_id: str) -> bool:
        return await self._pins.unpin(credential, entry_id)

    async def pinned(
        self, credential: Credential, fleet_id: str | None = None
    ) -> list[PinnedEntry]:
        return await self._pins.pinned(credential, fleet_id)
