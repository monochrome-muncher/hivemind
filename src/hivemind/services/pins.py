"""Pinned fleet entries (ADR 0058): pin, unpin and the fleet briefing.
A pin follows supersession to the version readers now see.
"""

from __future__ import annotations

from hivemind.domain.access import TrustLevel, Visibility
from hivemind.domain.entry import Entry, EntryState
from hivemind.domain.pin import Pin, PinnedEntry
from hivemind.domain.validation import MAX_ID_CHARS, MAX_PINS_PER_FLEET, InvalidInput, check_text
from hivemind.ports import Credential, Store
from hivemind.services.chain import get_visible_entries, get_visible_entry
from hivemind.services.errors import EntryNotActive, EntryNotFound, PermissionDenied

# How far a pin follows supersession to the current version (ADR 0058).
_PIN_FOLLOW_HOPS = 16


class PinService:
    """A privileged agent of an entry's fleet, or an admin, pins and
    unpins; any reader sees the pins they may read (ADR 0058)."""

    def __init__(self, store: Store) -> None:
        self._store = store

    async def pin(self, credential: Credential, entry_id: str) -> Pin:
        """Pin an active fleet entry to its own fleet. A privileged agent of
        that fleet or an admin may; a fleet holds at most
        ``MAX_PINS_PER_FLEET`` pins. Pinning a pinned entry is a no-op, and
        so is pinning the version an earlier pin already shows."""
        entry, fleet_id = await self._pinnable(credential, entry_id)
        if entry.state is not EntryState.ACTIVE:
            raise EntryNotActive(
                f"entry {entry_id} is {entry.state.value}; only active entries can be pinned"
            )
        shown = await self._pins_shown_as(fleet_id, entry, credential.visibility())
        if shown:
            return shown[0]
        pin = await self._store.pin_entry(
            fleet_id,
            entry.id,
            credential.agent_name or credential.user_id,
            MAX_PINS_PER_FLEET,
        )
        if pin is None:
            raise InvalidInput(
                f"the fleet already has {MAX_PINS_PER_FLEET} pinned entries: unpin one first"
            )
        return pin

    async def unpin(self, credential: Credential, entry_id: str) -> bool:
        """Remove an entry's pin (any state); whether it was pinned. The
        version a pin shows unpins it too, since that is the id readers
        see once the pinned entry is superseded."""
        entry, fleet_id = await self._pinnable(credential, entry_id)
        removed = await self._store.unpin_entry(fleet_id, entry.id)
        for pin in await self._pins_shown_as(fleet_id, entry, credential.visibility()):
            removed = await self._store.unpin_entry(fleet_id, pin.entry_id) or removed
        return removed

    async def pinned(
        self, credential: Credential, fleet_id: str | None = None
    ) -> list[PinnedEntry]:
        """A fleet's pinned entries as ``credential`` sees them, newest pin
        first (the caller's home fleet unless ``fleet_id`` names another).

        A pin follows supersession to the current version; a withdrawn
        entry shows as withdrawn until unpinned. Unreadable entries are
        left out, and a version two pins lead to is shown once (newest pin).
        """
        fleet = fleet_id or credential.home_fleet_id
        if fleet is None:
            return []
        check_text(fleet, "fleet_id", MAX_ID_CHARS)
        pins = await self._store.list_pins(fleet)
        if not pins:
            return []
        visibility = credential.visibility()
        found = await get_visible_entries(self._store, [p.entry_id for p in pins], visibility)
        result: list[PinnedEntry] = []
        shown: set[str] = set()
        for pin in pins:
            entry = found.get(pin.entry_id)
            if entry is None:
                continue
            current = await self._current(entry, visibility)
            if current.id not in shown:
                shown.add(current.id)
                result.append(PinnedEntry(pin, current))
        return result

    async def _pins_shown_as(
        self, fleet_id: str, entry: Entry, visibility: Visibility
    ) -> list[Pin]:
        """The fleet's pins of other versions that now show as ``entry``
        (pins follow supersession), newest first."""
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
        """The entry to pin or unpin and its fleet, if ``credential`` may
        (ADR 0058)."""
        entry = await get_visible_entry(self._store, entry_id, credential.visibility())
        if entry is None:  # unknown, or not visible to the caller (ADR 0033)
            raise EntryNotFound(f"unknown entry: {entry_id}")
        if entry.scope != "fleet" or entry.fleet_id is None:
            raise InvalidInput("only fleet entries can be pinned, to their own fleet")
        privileged_member = (
            credential.trust_level is TrustLevel.PRIVILEGED
            and credential.home_fleet_id == entry.fleet_id
        )
        if not (credential.is_admin or privileged_member):
            raise PermissionDenied(
                "only a privileged agent of the entry's fleet or an admin may pin or unpin"
            )
        return entry, entry.fleet_id
