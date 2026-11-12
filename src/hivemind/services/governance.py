"""Governance (SPEC.md §4.1, §8): withdrawal, restore, redaction and the move to `self`, plus the
one service object the surfaces hold; import feedback, pin and write names from their own
modules."""

from __future__ import annotations

from datetime import timedelta

from hivemind.config import SearchConfig
from hivemind.domain.audit import AuditAction
from hivemind.domain.entry import Entry, EntryState
from hivemind.domain.feedback import (
    FeedbackLogFilters,
    FeedbackLogItem,
    FeedbackSummary,
    FlaggedEntry,
    Verdict,
)
from hivemind.domain.pin import Pin, PinnedEntry
from hivemind.domain.redaction import RedactOutcome
from hivemind.domain.self_move import SelfMove
from hivemind.domain.validation import MAX_REASON_CHARS, InvalidInput, check_text
from hivemind.ports import Credential, Store
from hivemind.services.audit import audit_unpins, record_admin_action
from hivemind.services.chain import get_visible_entry
from hivemind.services.errors import EntryNotActive, EntryNotFound, PermissionDenied
from hivemind.services.feedback import FeedbackOutcome, FeedbackService
from hivemind.services.pins import PinService


class GovernanceService:
    """Withdrawal (SPEC.md §8.1): the author, a successor (ADR 0113) or an admin. Feedback and pin
    calls are handed on to ``FeedbackService`` and ``PinService``."""

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
        check_text(reason, "reason", MAX_REASON_CHARS)
        entry = await self._visible(credential, entry_id)
        owns = credential.owns(entry.author)
        if not (credential.is_admin or owns):
            raise PermissionDenied("only the author or an admin may withdraw an entry")
        withdrawn = await self._withdraw(credential, entry, reason)
        # Every withdrawal is audited (ADR 0141): by the admin, a successor or the author.
        detail: dict[str, str | None] = {"author": entry.author, "reason": reason}
        if owns:
            own_name = credential.agent_name or credential.user_id
            detail["via"] = "author" if entry.author == own_name else "succession"
        await record_admin_action(
            self._store, credential, AuditAction.ENTRY_WITHDRAW, entry_id, detail
        )
        await self._pins.unpin_withdrawn(credential, entry)
        return withdrawn

    async def withdraw_own(self, credential: Credential, entry_id: str, reason: str) -> Entry:
        """A portal withdrawal (ADR 0095): the caller's own entry only, even for the admin key,
        with a reason, audited with ``via: "portal"``."""
        check_text(reason, "reason", MAX_REASON_CHARS)
        if not reason.strip():
            raise InvalidInput("reason is required")
        entry = await self._visible(credential, entry_id)
        if not credential.owns(entry.author):
            raise PermissionDenied("only the author may withdraw an entry from the agent portal")
        withdrawn = await self._withdraw(credential, entry, reason)
        await record_admin_action(
            self._store,
            credential,
            AuditAction.ENTRY_WITHDRAW,
            entry_id,
            {"author": entry.author, "reason": reason, "via": "portal"},
        )
        await self._pins.unpin_withdrawn(credential, entry)
        return withdrawn

    async def move_to_self(
        self, credential: Credential, entry_id: str, reason: str, via: str | None = None
    ) -> SelfMove:
        """Narrow an active fleet memory to `self` (ADR 0145): the admin, or the author or a
        successor through the portal (``via: "portal"``). A reason is required; the move and the
        pins it removed are audited."""
        check_text(reason, "reason", MAX_REASON_CHARS)
        if not reason.strip():
            raise InvalidInput("reason is required")
        entry = await self._visible(credential, entry_id)
        if not (credential.is_admin or credential.owns(entry.author)):
            raise PermissionDenied("only the author, a successor or an admin may move a memory")
        if entry.state is not EntryState.ACTIVE:
            raise EntryNotActive(
                f"entry {entry.id} is {entry.state.value}; only an active memory can be moved"
            )
        try:
            move = await self._store.move_entry_to_self(entry.id)
        except KeyError as exc:
            raise EntryNotFound(f"unknown entry: {entry.id}") from exc
        detail: dict[str, object] = {
            "author": entry.author,
            "fleet_id": entry.fleet_id,
            "reason": reason.strip(),
            "earlier_versions": list(move.earlier_versions),
        }
        if via is not None:
            detail["via"] = via
        await record_admin_action(
            self._store, credential, AuditAction.ENTRY_MOVE_TO_SELF, entry.id, detail
        )
        by_fleet: dict[str, list[str]] = {}
        for pin in move.unpinned:
            by_fleet.setdefault(pin.fleet_id, []).append(pin.entry_id)
        for fleet_id, pin_ids in by_fleet.items():
            await audit_unpins(
                self._store,
                credential,
                entry.id,
                fleet_id,
                pin_ids,
                reason="moved_to_self",
                via=via,
            )
        return move

    async def restore(self, credential: Credential, entry_id: str, reason: str) -> Entry:
        """Put a withdrawn memory back, admin only (ADR 0124); the store refuses what the chain or
        a cleanup forbids. Pins are not restored."""
        if not credential.is_admin:
            raise PermissionDenied("only an admin may restore a withdrawn entry")
        check_text(reason, "reason", MAX_REASON_CHARS)
        if not reason.strip():
            raise InvalidInput("reason is required")
        entry = await self._visible(credential, entry_id)
        try:
            restoration = await self._store.restore_entry(entry.id)
        except KeyError as exc:
            raise EntryNotFound(f"unknown entry: {entry.id}") from exc
        await record_admin_action(
            self._store,
            credential,
            AuditAction.ENTRY_RESTORE,
            entry.id,
            {
                "author": entry.author,
                "previous_reason": restoration.previous_reason,
                "reason": reason.strip(),
            },
        )
        return restoration.entry

    async def redact(
        self, credential: Credential, entry_id: str, reason: str, confirm: str
    ) -> RedactOutcome:
        """Wipe a withdrawn memory's text for good (ADR 0125), admin only; ``confirm`` is the id
        typed back. The audit row never holds the old text. An active or already redacted memory is
        a 409 (``RedactRefused``)."""
        if not credential.is_admin:
            raise PermissionDenied("only an admin may redact a memory")
        check_text(reason, "reason", MAX_REASON_CHARS)
        if not reason.strip():
            raise InvalidInput("reason is required")
        entry = await self._visible(credential, entry_id)
        if confirm.strip().lower() != entry.id:
            raise InvalidInput("confirm must be the id of the memory being redacted")
        try:
            outcome = await self._store.redact_entry(entry.id)
        except KeyError as exc:
            raise EntryNotFound(f"unknown entry: {entry.id}") from exc
        await record_admin_action(
            self._store,
            credential,
            AuditAction.ENTRY_REDACT,
            entry.id,
            {
                "author": entry.author,
                "reason": reason.strip(),
                "feedback_notes": outcome.feedback_notes,
                "withdraw_reasons": outcome.withdraw_reasons,
            },
        )
        return outcome

    async def _visible(self, credential: Credential, entry_id: str) -> Entry:
        entry = await get_visible_entry(self._store, entry_id, credential.visibility())
        if entry is None:  # unknown, or not visible (ADR 0033)
            raise EntryNotFound(f"unknown entry: {entry_id}")
        return entry

    async def _withdraw(self, credential: Credential, entry: Entry, reason: str | None) -> Entry:
        """Withdraw ``entry`` once the caller's right is checked. Callers then run
        ``unpin_withdrawn`` (ADR 0096) after the audit row: its failure propagates but leaves the
        withdrawal in place."""
        if entry.state.value != "active":
            raise EntryNotActive(
                f"entry {entry.id} is already {entry.state.value}; "
                "only active entries can be withdrawn"
            )
        try:
            return await self._store.withdraw_entry(entry.id, reason, by_user=credential.user_id)
        except KeyError as exc:  # the store's "unknown entry"
            raise EntryNotFound(f"unknown entry: {entry.id}") from exc
        except ValueError as exc:  # the store's "not active": a concurrent withdraw won the race
            raise EntryNotActive(str(exc)) from exc

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
        own: bool = False,
        limit: int,
        offset: int = 0,
    ) -> list[FlaggedEntry]:
        return await self._feedback.flag_backlog(
            credential,
            fleet_id=fleet_id,
            older_than=older_than,
            orphaned=orphaned,
            own=own,
            limit=limit,
            offset=offset,
        )

    async def pin(self, credential: Credential, entry_id: str, via: str | None = None) -> Pin:
        return await self._pins.pin(credential, entry_id, via)

    async def unpin(self, credential: Credential, entry_id: str, via: str | None = None) -> bool:
        return await self._pins.unpin(credential, entry_id, via)

    async def pinned(
        self, credential: Credential, fleet_id: str | None = None
    ) -> list[PinnedEntry]:
        return await self._pins.pinned(credential, fleet_id)
