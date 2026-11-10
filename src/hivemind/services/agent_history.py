"""The agent portal's history page (ADR 0123): audit rows about the agent and the agents it
inherits from, through the privacy allowlist in ``domain.history``.

A fleet name or an entry id reaches the answer only if the key can read it.
"""

from __future__ import annotations

from hivemind.domain.access import TrustLevel
from hivemind.domain.audit import AuditFilters
from hivemind.domain.history import (
    HistoryContext,
    HistoryItem,
    describe,
    fleet_readable,
    referenced_entry_id,
    referenced_fleet_ids,
)
from hivemind.ports import Credential, Store
from hivemind.services.chain import get_visible_entries
from hivemind.services.errors import PermissionDenied


class AgentHistoryService:
    def __init__(self, store: Store) -> None:
        self._store = store

    async def list_own(
        self, credential: Credential, *, before: str | None, limit: int
    ) -> list[HistoryItem]:
        """The agent's history, newest first; ``before`` is the last row id of the previous page.
        A dormant (level 0) agent is refused (SPEC §12.2)."""
        name = credential.agent_name
        if credential.is_admin or credential.is_org or name is None:
            raise PermissionDenied("reading an agent's own history requires an agent key")
        if credential.trust_level is TrustLevel.UNTRUSTED:
            raise PermissionDenied(
                "a dormant (untrusted, level 0) agent can do nothing; ask your admin"
            )
        records = await self._store.list_audit(
            AuditFilters(subjects=(name, *credential.inherits_from), before=before), limit
        )
        visibility = credential.visibility()
        fleet_ids = {
            fid
            for r in records
            for fid in referenced_fleet_ids(r)
            if fleet_readable(visibility, fid)
        }
        fleet_names: dict[str, str] = {}
        for fid in sorted(fleet_ids):
            fleet = await self._store.get_fleet(fid)
            if fleet is not None:
                fleet_names[fid] = fleet.name
        entry_ids = [e for r in records if (e := referenced_entry_id(r)) is not None]
        readable = await get_visible_entries(self._store, entry_ids, visibility)
        ctx = HistoryContext(
            me=name,
            inherited=tuple(credential.inherits_from),
            fleet_names=fleet_names,
            readable_entries=frozenset(readable),
        )
        return [describe(r, ctx) for r in records]
