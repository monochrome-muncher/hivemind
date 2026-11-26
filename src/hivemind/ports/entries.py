"""Entry writes, reads and lifecycle (withdraw, restore, redact)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from hivemind.domain.access import (
    Visibility,
)
from hivemind.domain.entry import (
    Entry,
    EntryDraft,
    EntryFilters,
    ExtractedEntity,
)
from hivemind.domain.entry_restore import Restoration
from hivemind.domain.redaction import RedactOutcome
from hivemind.domain.upstream import UpstreamLink


class SupersedeConflict(Exception):
    """``create_entry`` supersession targets not active at flip time (ADR 0034); nothing written.

    ``ids`` are the offending targets; the write service maps it to ``SupersedeDenied``."""

    def __init__(self, ids: list[str]) -> None:
        super().__init__("supersession targets not active: " + ", ".join(ids))
        self.ids = ids


class UnknownEntry(KeyError):
    """An entry method was given an id naming no entry (or not a UUID)."""


class EntryNotActiveConflict(ValueError):
    """``withdraw_entry`` found the entry not active; the governance service maps it to
    ``EntryNotActive``."""


@runtime_checkable
class EntryPort(Protocol):
    """Entry writes, reads and lifecycle (withdraw, restore, redact)."""

    async def create_entry(
        self,
        draft: EntryDraft,
        embedding: list[float] | None = None,
        embedding_model: str | None = None,
        entities: tuple[ExtractedEntity, ...] = (),
        entities_model: str | None = None,
    ) -> Entry:
        """Insert an entry and flip its ``draft.supersedes`` targets to ``superseded`` (SPEC §4.1).

        - ``embedding_model`` / ``entities_model`` record provenance.
        - Atomic (ADR 0034): an unknown or inactive target raises ``SupersedeConflict``, nothing
          written.
        - ``draft.see_also`` links (ADR 0057) go in the same transaction; ids naming no entry are
          dropped.
        - A fleet-scoped draft aimed at a merged fleet raises ``FleetMerged``, nothing written (ADR
          0107).
        """
        ...

    async def get_entry(self, entry_id: str) -> Entry | None: ...

    async def get_entries(self, entry_ids: list[str]) -> dict[str, Entry]:
        """Fetch full entries by ID (missing IDs are simply absent)."""
        ...

    async def list_predecessors(self, entry_ids: list[str]) -> list[Entry]:
        """Every entry whose ``superseded_by`` is in ``entry_ids``, any order (SPEC §5.1
        ``?history``).

        Unfiltered by state or visibility; the chain walk filters (ADR 0033)."""
        ...

    async def entry_links(
        self, entry_id: str, limit: int, *, visibility: Visibility | None = None
    ) -> tuple[list[str], list[str]]:
        """The "see also" links of one entry: ids it links to (oldest first, unfiltered), and
        at most ``limit`` ids linking to it (newest first).

        Only active sources ``visibility`` may read count toward the incoming ``limit`` (``None`` =
        no restriction)."""
        ...

    async def entry_links_many(
        self, entry_ids: list[str], limit: int, *, visibility: Visibility | None = None
    ) -> dict[str, tuple[list[str], list[str]]]:
        """``entry_links`` for several entries in one read (ADR 0059); every requested id is a key,
        ``([], [])`` if none."""
        ...

    async def upstream_links(
        self, entry_ids: list[str], *, visibility: Visibility | None = None
    ) -> dict[str, list[UpstreamLink]]:
        """Outgoing ``see_also`` links of several entries, with what happened to each target (ADR
        0081).

        Per entry: at most ``MAX_SEE_ALSO`` links, oldest first then by target id, targets in any
        state but readable under ``visibility`` (``None`` = all); an unreadable target is left out
        as if absent. Only entries with a link are keys; malformed ids are left out. Facts only:
        ``upstream_changed`` applies the rule."""
        ...

    async def list_entries(
        self,
        filters: EntryFilters,
        limit: int = 20,
        offset: int = 0,
        *,
        visibility: Visibility | None = None,
    ) -> list[Entry]:
        """List and filter entries (SPEC §5.1), limited to what ``visibility`` may read (ADR 0011);
        ``None`` = the flat pool."""
        ...

    async def count_entries(
        self, filters: EntryFilters, *, visibility: Visibility | None = None
    ) -> int:
        """Count entries matching ``filters`` (SPEC §5.3); ``visibility`` as in ``list_entries``."""
        ...

    async def withdraw_entry(self, entry_id: str, reason: str | None, by_user: str) -> Entry:
        """Flip an active entry to ``withdrawn`` (SPEC §4.1); persists the reason, not ``by_user``.

        ``UnknownEntry`` if unknown, ``EntryNotActiveConflict`` if not active."""
        ...

    async def redact_entry(self, entry_id: str) -> RedactOutcome:
        """Wipe a memory's text for good, in one transaction (ADR 0125): summary, body, payload,
        tags, sources, entities, outgoing see-also links, withdrawal reason, embedding, keyword
        index.

        - Sets ``redacted_at``; kind, scope, author, fleet, dates, supersession, feedback and the
          audit log stay.
        - Returns the memory now, the count of feedback notes (and stale/wrong flag notes among
          them) left unedited, and of audit rows holding a withdrawal reason (``entry.withdraw``
          ``reason``, ``entry.restore`` ``previous_reason``).
        - Allowed on ``withdrawn`` or ``superseded``. ``UnknownEntry`` if unknown; ``RedactRefused`` if
          active or already redacted.
        - Every path that reactivates a withdrawn memory skips a redacted one."""
        ...

    async def restore_entry(self, entry_id: str) -> Restoration:
        """Put a withdrawn entry back to ``active``, clearing its withdrawal reason and time (ADR
        0124); pins are not restored.

        State check and update are one atomic step. ``UnknownEntry`` if unknown; ``RestoreRefused``
        (a subclass says why) if not withdrawn, withdrawn by a standing agent cleanup, or it would
        leave two active heads in a chain.
        """
        ...
