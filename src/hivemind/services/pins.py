"""Pinned fleet entries (ADRs 0058, 0096): pin, unpin and the fleet briefing; a pin follows
supersession."""

from __future__ import annotations

from hivemind.domain.access import SCOPE_FLEET, SCOPE_SELF, TrustLevel, Visibility
from hivemind.domain.audit import AuditAction
from hivemind.domain.entry import Entry, EntryState
from hivemind.domain.feedback import NO_SIGNALS, FeedbackSignals
from hivemind.domain.pin import ChangeReason, Pin, PinnedEntry, PinRefusal
from hivemind.domain.validation import (
    MAX_ID_CHARS,
    MAX_PINS_PER_AGENT,
    MAX_PINS_PER_FLEET,
    InvalidInput,
    check_text,
)
from hivemind.ports import Credential, Store
from hivemind.services.audit import audit_unpins, record_admin_action
from hivemind.services.chain import get_visible_entries, get_visible_entry
from hivemind.services.errors import EntryNotActive, EntryNotFound, PermissionDenied

# Reads every entry: the walk that finds pins of withdrawn entries must not depend on the actor (ADR
# 0096).
_ANYONE = Visibility(level=TrustLevel.PRIVILEGED, name="pins", is_admin=True)

_PIN_FOLLOW_HOPS = 16


def _left_the_fleet(current: Entry) -> bool:
    """A pin's current version is gone from the briefing: withdrawn, or moved to `self` while the
    pin was being written (ADR 0145)."""
    return current.state is EntryState.WITHDRAWN or current.scope == SCOPE_SELF


class PinService:
    """Pins and unpins by a contributor or privileged agent of the fleet, or an admin; readers see
    the pins they may read (ADRs 0058, 0096)."""

    def __init__(self, store: Store, require_stored_keys: bool = False) -> None:
        self._store = store
        # A dev-mode credential drops to trust level 0 when stored keys are required (ADR 0082).
        self._require_stored_keys = require_stored_keys

    async def pin(self, credential: Credential, entry_id: str, via: str | None = None) -> Pin:
        """Pin an active fleet entry to its own fleet (ADR 0058). A fleet holds at most
        ``MAX_PINS_PER_FLEET`` pins and an agent ``MAX_PINS_PER_AGENT`` (admin exempt; inherited
        pins count as its own, ADR 0113). Re-pinning, or pinning the version an older pin now
        shows, restamps ``pinned_at`` and ``pinned_by`` (ADR 0076) and counts toward the agent cap
        (ADR 0096). Every pin is audited, naming the pin it replaced; ``via`` (ADR 0116) goes into
        the audit detail."""
        credential = credential.effective(self._require_stored_keys)
        entry, fleet_id = await self._pinnable(credential, entry_id)
        if entry.state is not EntryState.ACTIVE:
            raise EntryNotActive(
                f"entry {entry_id} is {entry.state.value}; only active entries can be pinned"
            )
        # Pins left on withdrawn entries (older ones, or a race) would count toward the caps.
        await self._sweep_withdrawn(credential, fleet_id)
        shown = await self._pins_shown_as(fleet_id, entry, credential.visibility())
        written = await self._store.pin_entry(
            fleet_id,
            entry.id,
            credential.agent_name or credential.user_id,
            MAX_PINS_PER_FLEET,
            [p.entry_id for p in shown],
            None if credential.is_admin else MAX_PINS_PER_AGENT,
            credential.inherits_from,
        )
        if written is PinRefusal.AGENT:
            raise InvalidInput(
                f"you already hold {MAX_PINS_PER_AGENT} of this fleet's pins: unpin one first"
            )
        if written is PinRefusal.FLEET:
            raise InvalidInput(
                f"the fleet already has {MAX_PINS_PER_FLEET} pinned entries: unpin one first"
            )
        detail: dict[str, object] = {"fleet_id": fleet_id}
        if written.replaced:
            detail["from"] = written.replaced[0] if len(written.replaced) == 1 else written.replaced
        if via is not None:
            detail["via"] = via
        await record_admin_action(self._store, credential, AuditAction.ENTRY_PIN, entry.id, detail)
        return written.pin

    async def unpin(self, credential: Credential, entry_id: str, via: str | None = None) -> bool:
        """Remove an entry's pin (any state) and the pins that now show it; whether any was
        removed. Audited once with the removed pin ids (ADR 0076). Anyone who may pin may unpin,
        someone else's pin included (ADR 0096); ``via`` as in ``pin``."""
        credential = credential.effective(self._require_stored_keys)
        entry, fleet_id = await self._pinnable(credential, entry_id)
        return bool(
            await self._unpin_shown(credential, entry, fleet_id, credential.visibility(), via=via)
        )

    async def unpin_withdrawn(self, credential: Credential, entry: Entry) -> list[str]:
        """Take a just-withdrawn fleet ``entry`` out of its briefing (ADR 0096): its pins, audited
        as one ``entry.unpin`` with ``reason: "withdrawn"``. No permission check beyond the
        withdrawal's own; the walk reads as anyone, so a withdrawer who left the fleet still finds
        older pinned versions. Returns the removed pin ids."""
        if entry.scope != SCOPE_FLEET or entry.fleet_id is None:
            return []
        return await self._unpin_shown(
            credential, entry, entry.fleet_id, _ANYONE, reason="withdrawn"
        )

    async def _sweep_withdrawn(self, credential: Credential, fleet_id: str) -> None:
        """Remove the fleet's pins whose current version left the fleet, one audited ``entry.unpin``
        each. Reads as anyone, so no caller's visibility hides one."""
        pins = await self._store.list_pins(fleet_id)
        if not pins:
            return
        found = await get_visible_entries(self._store, [p.entry_id for p in pins], _ANYONE)
        for pin in pins:
            pinned = found.get(pin.entry_id)
            if pinned is None:
                continue
            current = await self._current(pinned, _ANYONE)
            if _left_the_fleet(current) and await self._store.unpin_entry(fleet_id, pin.entry_id):
                await audit_unpins(
                    self._store,
                    credential,
                    current.id,
                    fleet_id,
                    [pin.entry_id],
                    reason="withdrawn"
                    if current.state is EntryState.WITHDRAWN
                    else "moved_to_self",
                )

    async def _unpin_shown(
        self,
        credential: Credential,
        entry: Entry,
        fleet_id: str,
        visibility: Visibility,
        reason: str | None = None,
        via: str | None = None,
    ) -> list[str]:
        removed_ids: list[str] = []
        if await self._store.unpin_entry(fleet_id, entry.id):
            removed_ids.append(entry.id)
        for pin in await self._pins_shown_as(fleet_id, entry, visibility):
            if await self._store.unpin_entry(fleet_id, pin.entry_id):
                removed_ids.append(pin.entry_id)
        if removed_ids:
            await audit_unpins(
                self._store, credential, entry.id, fleet_id, removed_ids, reason=reason, via=via
            )
        return removed_ids

    async def pinned(
        self, credential: Credential, fleet_id: str | None = None
    ) -> list[PinnedEntry]:
        """A fleet's pinned entries as ``credential`` sees them, newest pin first (home fleet unless
        ``fleet_id``). A pin shows its current version; unreadable and withdrawn ones are left out
        (ADR 0096); a version two pins lead to shows once. Each carries why it changed (ADR
        0076)."""
        credential = credential.effective(self._require_stored_keys)
        fleet = fleet_id or credential.home_fleet_id
        if fleet is None:
            return []
        check_text(fleet, "fleet_id", MAX_ID_CHARS)
        pins = await self._store.list_pins(fleet)
        if not pins:
            return []
        visibility = credential.visibility()
        found = await get_visible_entries(self._store, [p.entry_id for p in pins], visibility)
        shown_pins: list[tuple[Pin, Entry]] = []
        shown: set[str] = set()
        for pin in pins:
            entry = found.get(pin.entry_id)
            if entry is None:
                continue
            current = await self._current(entry, visibility)
            if _left_the_fleet(current):
                continue
            if current.id not in shown:
                shown.add(current.id)
                shown_pins.append((pin, current))
        # One batched read, not one per pin.
        signals = await self._store.feedback_signals([current.id for _, current in shown_pins])
        return [
            PinnedEntry(
                pin, current, _change_reason(pin, current, signals.get(current.id, NO_SIGNALS))
            )
            for pin, current in shown_pins
        ]

    async def _pins_shown_as(
        self, fleet_id: str, entry: Entry, visibility: Visibility
    ) -> list[Pin]:
        """The fleet's pins of other versions that now show as ``entry``, newest first."""
        pins = [p for p in await self._store.list_pins(fleet_id) if p.entry_id != entry.id]
        if not pins:
            return []
        found = await get_visible_entries(self._store, [p.entry_id for p in pins], visibility)
        return [
            pin
            for pin in pins
            if (pinned := found.get(pin.entry_id)) is not None
            and (await self._current(pinned, visibility)).id == entry.id
        ]

    async def _current(self, entry: Entry, visibility: Visibility) -> Entry:
        """The newest version of ``entry`` the reader may see (bounded walk)."""
        for _ in range(_PIN_FOLLOW_HOPS):
            if entry.state is not EntryState.SUPERSEDED or entry.superseded_by is None:
                break
            successor = await get_visible_entry(self._store, entry.superseded_by, visibility)
            if successor is None:
                break
            entry = successor
        return entry

    async def _pinnable(self, credential: Credential, entry_id: str) -> tuple[Entry, str]:
        """The entry to pin or unpin and its fleet, if ``credential`` may."""
        entry = await get_visible_entry(self._store, entry_id, credential.visibility())
        if entry is None:  # unknown, or not visible (ADR 0033)
            raise EntryNotFound(f"unknown entry: {entry_id}")
        if entry.scope != SCOPE_FLEET or entry.fleet_id is None:
            raise InvalidInput("only fleet entries can be pinned, to their own fleet")
        member = (
            credential.trust_level in (TrustLevel.CONTRIBUTOR, TrustLevel.PRIVILEGED)
            and credential.home_fleet_id == entry.fleet_id
        )
        if not (credential.is_admin or member):
            raise PermissionDenied(
                "only a contributor or privileged agent of the entry's fleet, "
                "or an admin, may pin or unpin"
            )
        return entry, entry.fleet_id


def _change_reason(pin: Pin, current: Entry, signals: FeedbackSignals) -> ChangeReason | None:
    """Why the briefing no longer shows what was pinned (ADR 0076): ``superseded``, else
    ``flagged`` (a stale or wrong verdict newer than the pin), else ``None``."""
    if current.id != pin.entry_id:
        return "superseded"
    if signals.newest_flag_at is not None and signals.newest_flag_at > pin.pinned_at:
        return "flagged"
    return None
