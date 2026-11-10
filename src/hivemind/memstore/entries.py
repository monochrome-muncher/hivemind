"""Entry reads, writes, links and lifecycle (supersede, withdraw, restore, redact)."""

from __future__ import annotations

import uuid
from dataclasses import replace

from hivemind.domain.access import SCOPE_FLEET, SCOPE_ORG, Visibility
from hivemind.domain.agent_cleanup import cleanup_reason
from hivemind.domain.audit import AuditAction
from hivemind.domain.entry import (
    Entry,
    EntryDraft,
    EntryFilters,
    EntryState,
    ExtractedEntity,
    new_entry_id,
)
from hivemind.domain.entry_restore import Restoration, chain_conflict_message, restore_refusal
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.domain.redaction import (
    REDACTED_SUMMARY,
    RedactOutcome,
    RedactRefused,
    is_redaction_stub,
)
from hivemind.domain.upstream import UpstreamLink
from hivemind.domain.validation import canonical_uuid, is_valid_uuid
from hivemind.memstore.helpers import EPOCH, canon, canonical_or_same, copy_entry
from hivemind.memstore.state import MemoryState
from hivemind.ports import SupersedeConflict


def _with_state(
    entry: Entry,
    state: EntryState,
    *,
    superseded_by: str | None = None,
    withdrawn_reason: str | None = None,
) -> Entry:
    return replace(
        entry,
        state=state,
        superseded_by=superseded_by or entry.superseded_by,
        withdrawn_reason=withdrawn_reason or entry.withdrawn_reason,
    )


class EntryStore(MemoryState):
    async def create_entry(
        self,
        draft: EntryDraft,
        embedding: list[float] | None = None,
        embedding_model: str | None = None,
        entities: tuple[ExtractedEntity, ...] = (),
        entities_model: str | None = None,
    ) -> Entry:
        """Insert a new entry, flipping any ``draft.supersedes`` targets to
        ``superseded`` (SPEC.md §4.1, §7)."""
        now = self._clock()
        entry = Entry(
            id=new_entry_id(),
            kind=draft.kind,
            summary=draft.summary,
            author=draft.author,
            agent=draft.agent,
            occurred_at=draft.resolved_occurred_at(),
            created_at=now,
            body=draft.body,
            payload=dict(draft.payload) if draft.payload is not None else None,
            sources=tuple(draft.sources),
            tags=tuple(draft.tags),
            importance=draft.importance,
            importance_source=draft.importance_source,
            scope=draft.scope,
            fleet_id=draft.fleet_id,
            embedding=tuple(embedding) if embedding is not None else None,
            embedding_model=embedding_model,
            entities=tuple(entities),  # ADR 0016: machine-extracted facets
            entities_model=entities_model,
        )
        with self._lock:
            self._refuse_merged(draft.fleet_id)
            if draft.scope == SCOPE_FLEET:
                self._refuse_stale_home(draft.fleet_id, draft.author)
            # Explicit supersessions (SPEC.md §4.1), atomic (ADR 0034):
            # every target must be active, or nothing is written.
            # Any spelling of a uuid names its entry, as in Postgres; a
            # malformed id names none and is denied.
            wanted = list(dict.fromkeys(draft.supersedes))
            denied = [
                t
                for t in wanted
                if (e := self._entries.get(canonical_or_same(t))) is None
                or e.state is not EntryState.ACTIVE
                # A fleet target moves only under a successor in its own fleet
                # (or an org one), as PgStore's guarded flip.
                or (
                    e.scope == SCOPE_FLEET
                    and draft.scope != SCOPE_ORG
                    and (e.fleet_id is None or e.fleet_id != draft.fleet_id)
                )
            ]
            if denied:
                raise SupersedeConflict(denied)
            self._entries[entry.id] = entry
            for target_id in dict.fromkeys(canonical_or_same(t) for t in wanted):
                self._entries[target_id] = _with_state(
                    self._entries[target_id],
                    EntryState.SUPERSEDED,
                    superseded_by=entry.id,
                )
            # One write's links in id order, as PgStore reads them back
            # (same ``created_at``, then ``to_id``).
            for target_id in sorted({c for t in draft.see_also if (c := canon(t))}):
                if target_id in self._entries and target_id != entry.id:
                    self._links[(entry.id, target_id)] = now
        return entry

    async def get_entry(self, entry_id: str) -> Entry | None:
        # Any spelling Postgres's uuid type reads (upper case, no hyphens).
        key = canonical_uuid(entry_id) if is_valid_uuid(entry_id) else None
        with self._lock:
            entry = self._entries.get(key) if key is not None else None
            return copy_entry(entry) if entry is not None else None

    async def get_entries(self, entry_ids: list[str]) -> dict[str, Entry]:
        # Keyed by the canonical id, as Postgres answers, whatever the spelling.
        with self._lock:
            result: dict[str, Entry] = {}
            for eid in entry_ids:
                key = canonical_uuid(eid) if is_valid_uuid(eid) else None
                entry = self._entries.get(key) if key is not None else None
                if entry is not None:
                    result[entry.id] = copy_entry(entry)
            return result

    async def list_predecessors(self, entry_ids: list[str]) -> list[Entry]:
        wanted = {str(uuid.UUID(eid)) for eid in entry_ids if is_valid_uuid(eid)}
        with self._lock:
            return [
                copy_entry(entry)
                for entry in self._entries.values()
                if entry.superseded_by in wanted
            ]

    async def entry_links(
        self, entry_id: str, limit: int, *, visibility: Visibility | None = None
    ) -> tuple[list[str], list[str]]:
        if not is_valid_uuid(entry_id):
            return [], []
        key = str(uuid.UUID(entry_id))
        with self._lock:
            # PgStore's ``ORDER BY created_at, to_id`` and
            # ``created_at DESC, from_id``.
            out_rows = sorted((at, to) for (frm, to), at in self._links.items() if frm == key)
            in_rows = [
                (at, frm)
                for (frm, to), at in self._links.items()
                if to == key
                and (src := self._entries.get(frm)) is not None
                and src.state is EntryState.ACTIVE
                and self._visible(src, visibility)
            ]
        in_rows.sort(key=lambda r: r[1])
        in_rows.sort(key=lambda r: r[0], reverse=True)
        return [to for _, to in out_rows], [frm for _, frm in in_rows[:limit]]

    async def entry_links_many(
        self, entry_ids: list[str], limit: int, *, visibility: Visibility | None = None
    ) -> dict[str, tuple[list[str], list[str]]]:
        return {eid: await self.entry_links(eid, limit, visibility=visibility) for eid in entry_ids}

    async def upstream_links(
        self, entry_ids: list[str], *, visibility: Visibility | None = None
    ) -> dict[str, list[UpstreamLink]]:
        wanted = {str(uuid.UUID(eid)) for eid in entry_ids if is_valid_uuid(eid)}
        out: dict[str, list[UpstreamLink]] = {}
        with self._lock:
            targets = {to for (frm, to) in self._links if frm in wanted}
            by_target: dict[str, list[Feedback]] = {}
            for fb in self._live_feedback().values():
                if fb.entry_id in targets:
                    by_target.setdefault(fb.entry_id, []).append(fb)
            for (frm, to), linked_at in self._links.items():
                target = self._entries.get(to)
                if frm not in wanted or target is None or not self._visible(target, visibility):
                    continue
                successor = (
                    self._entries.get(target.superseded_by) if target.superseded_by else None
                )
                rows = by_target.get(to, [])
                flags = [fb.updated_at or EPOCH for fb in rows if fb.verdict is not Verdict.HELPFUL]
                noted = [
                    fb.updated_at
                    for fb in rows
                    if fb.verdict is Verdict.HELPFUL
                    and fb.note
                    and fb.note.strip()
                    and fb.updated_at is not None
                ]
                out.setdefault(frm, []).append(
                    UpstreamLink(
                        from_id=frm,
                        to_id=to,
                        linked_at=linked_at,
                        state=target.state,
                        superseded_at=successor.created_at if successor else None,
                        superseded_author=successor.author if successor else None,
                        newest_flag_at=max(flags, default=None),
                        newest_noted_helpful_at=max(noted, default=None),
                    )
                )
        for links in out.values():
            links.sort(key=lambda link: (link.linked_at, link.to_id))
        return out

    async def list_entries(
        self,
        filters: EntryFilters,
        limit: int = 20,
        offset: int = 0,
        *,
        visibility: Visibility | None = None,
    ) -> list[Entry]:
        with self._lock:
            matches = [
                copy_entry(entry)
                for entry in self._entries.values()
                if self._matches(entry, filters) and self._visible(entry, visibility)
            ]
            # Most-recent-first (SPEC.md §11.4), tie-broken by id DESC to
            # match PgStore's `ORDER BY created_at DESC, id DESC` exactly.
            matches.sort(key=lambda e: (e.created_at, e.id), reverse=True)
            return matches[offset : offset + limit]

    async def count_entries(
        self, filters: EntryFilters, *, visibility: Visibility | None = None
    ) -> int:
        with self._lock:
            return sum(
                1
                for entry in self._entries.values()
                if self._matches(entry, filters) and self._visible(entry, visibility)
            )

    async def withdraw_entry(self, entry_id: str, reason: str | None, by_user: str) -> Entry:
        if not is_valid_uuid(entry_id):
            raise KeyError(entry_id)
        key = str(uuid.UUID(entry_id))
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                raise KeyError(entry_id)
            if entry.state is not EntryState.ACTIVE:
                raise ValueError(f"entry {entry_id} is not active (state={entry.state.value})")
            now = self._clock()
            updated = replace(
                _with_state(entry, EntryState.WITHDRAWN, withdrawn_reason=reason),
                withdrawn_at=now,
            )
            self._entries[key] = updated
            self._withdrawn_at[key] = now
            return updated

    async def restore_entry(self, entry_id: str) -> Restoration:
        if not is_valid_uuid(entry_id):
            raise KeyError(entry_id)
        key = str(uuid.UUID(entry_id))
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                raise KeyError(entry_id)
            standing = next(
                (
                    c.id
                    for c in self._cleanups
                    if c.undone_at is None
                    and key in c.withdrawn_ids
                    and entry.withdrawn_reason == cleanup_reason(c.id)
                ),
                None,
            )
            refusal = restore_refusal(
                key,
                entry.state.value,
                entry.withdrawn_reason,
                standing,
                self._chain_conflict(key),
                entry.redacted_at is not None
                or is_redaction_stub(
                    entry.summary, entry.body, entry.withdrawn_reason, entry.embedding is not None
                ),
            )
            if refusal is not None:
                raise refusal
            restored = replace(
                entry,
                state=EntryState.ACTIVE,
                withdrawn_reason=None,
                withdrawn_at=None,
            )
            self._entries[key] = restored
            self._withdrawn_at.pop(key, None)
            return Restoration(copy_entry(restored), entry.withdrawn_reason)

    def _chain_conflict(self, key: str) -> str | None:
        """The chain rules (ADR 0124), as PgStore's queries answer them."""
        was_cut = any(item.entry_id == key for rec in self._cleanups for item in rec.restored)
        linked = next(
            (
                e.id
                for e in self._entries.values()
                if e.superseded_by == key and e.state is EntryState.ACTIVE
            ),
            None,
        )
        instead = None
        for rec in self._cleanups:
            for item in rec.restored:
                root = self._entries.get(item.entry_id)
                if root is None or root.state is not EntryState.ACTIVE:
                    continue
                seen: set[str] = set()
                cur: str | None = item.superseded_by
                while cur and cur not in seen:
                    if cur == key:
                        instead = root.id
                    seen.add(cur)
                    node = self._entries.get(cur)
                    cur = node.superseded_by if node else None
        return chain_conflict_message(key, was_cut, linked, instead)

    async def redact_entry(self, entry_id: str) -> RedactOutcome:
        if not is_valid_uuid(entry_id):
            raise KeyError(entry_id)
        key = str(uuid.UUID(entry_id))
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                raise KeyError(entry_id)
            if entry.redacted_at is not None:
                raise RedactRefused(f"entry {entry_id} is already redacted")
            if entry.state is EntryState.ACTIVE:
                raise RedactRefused(
                    f"entry {entry_id} is active: withdraw it first "
                    "(a superseded or withdrawn memory can be redacted)"
                )
            wiped = replace(
                entry,
                summary=REDACTED_SUMMARY,
                body=None,
                payload=None,
                sources=(),
                tags=(),
                entities=(),
                entities_model=None,
                embedding=None,
                embedding_model=None,
                withdrawn_reason=(
                    REDACTED_SUMMARY if entry.state is EntryState.WITHDRAWN else None
                ),
                redacted_at=self._clock(),
            )
            self._entries[key] = wiped
            for link in [k for k in self._links if k[0] == key]:
                del self._links[link]
            notes = [
                fb
                for fb in self._feedback.values()
                if fb.entry_id == key and (fb.note or "").strip()
            ]
            flags = [fb for fb in notes if fb.verdict in (Verdict.STALE, Verdict.WRONG)]
            reason_field = {
                AuditAction.ENTRY_WITHDRAW: "reason",
                AuditAction.ENTRY_RESTORE: "previous_reason",  # copied from the memory
            }
            reasons = sum(
                1
                for rec in self._audit
                if rec.target == key
                and (field := reason_field.get(rec.action)) is not None
                and str(rec.detail.get(field) or "").strip()
            )
            return RedactOutcome(wiped, len(notes), len(flags), reasons)
