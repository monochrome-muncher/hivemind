"""One agent's history as the agent portal shows it (ADR 0123). No I/O.

Audit rows (ADR 0027) become ``HistoryItem`` through an allowlist: only the ``detail`` fields
read below pass, and the row's ``actor`` never does (admin, org or CLI becomes ``by="admin"``).
Key fingerprints, other agents' names (own and inherited ones excepted, ADR 0113), fleet and
row ids and the owner alias never reach a ``HistoryItem``. A fleet is named only if the key can
read it; an entry id is kept only if the key can still read the entry.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from hivemind.domain.access import TrustLevel, Visibility
from hivemind.domain.audit import ActorKind, AuditAction, AuditRecord

OTHER_FLEET = "another fleet"
OTHER_AGENT = "another agent"
THIS_AGENT = "this agent"
AN_ADMIN = "an admin"


def fleet_readable(visibility: Visibility, fleet_id: str) -> bool:
    """Whether the key may know the fleet's name: the same reach as its memory reads (ADR 0011)."""
    return (
        visibility.is_admin
        or visibility.level is TrustLevel.PRIVILEGED
        or fleet_id == visibility.home_fleet_id
    )


@dataclass(frozen=True, slots=True)
class HistoryContext:
    """What the mapper may name: the agent, its predecessors, readable fleets and entries."""

    me: str
    inherited: tuple[str, ...]
    fleet_names: Mapping[str, str]
    readable_entries: frozenset[str]

    @property
    def agents(self) -> tuple[str, ...]:
        return (self.me, *self.inherited)

    def agent(self, name: object) -> str:
        """``this agent`` for the agent itself, a predecessor's name, else a generic phrase."""
        if name == self.me:
            return THIS_AGENT
        if isinstance(name, str) and name in self.inherited:
            return name
        return OTHER_AGENT

    def fleet(self, fleet_id: object) -> str:
        if isinstance(fleet_id, str) and fleet_id in self.fleet_names:
            return self.fleet_names[fleet_id]
        return OTHER_FLEET


@dataclass(frozen=True, slots=True)
class HistoryItem:
    """One history line. ``agent`` names which of the agent or its predecessors acted;
    ``about`` is the one the row concerns."""

    id: str
    occurred_at: datetime
    action: str
    by: Literal["agent", "another_agent", "admin"]
    agent: str | None
    about: str | None
    summary: str
    reason: str | None = None
    entry_id: str | None = None


def referenced_fleet_ids(record: AuditRecord) -> list[str]:
    """The fleet ids a row would name, so the service reads only those."""
    detail = record.detail
    ids: list[object] = []
    match record.action:
        case AuditAction.AGENT_ACTIVATE:
            ids = [detail.get("home_fleet_id")]
        case AuditAction.AGENT_HOME_FLEET_SET:
            ids = [detail.get("from"), detail.get("to")]
        case AuditAction.FLEET_MOVE_AGENTS:
            ids = [record.target, detail.get("target_fleet_id")]
        case AuditAction.ENTRY_PIN | AuditAction.ENTRY_UNPIN:
            ids = [detail.get("fleet_id")]
    return [i for i in ids if isinstance(i, str)]


def referenced_entry_id(record: AuditRecord) -> str | None:
    """The entry a row is about (its target), for the actions that name one."""
    if record.action in (
        AuditAction.ENTRY_WITHDRAW,
        AuditAction.ENTRY_RESTORE,
        AuditAction.ENTRY_REDACT,
        AuditAction.ENTRY_MOVE_TO_SELF,
        AuditAction.ENTRY_PIN,
        AuditAction.ENTRY_UNPIN,
    ):
        return record.target
    return None


def describe(record: AuditRecord, ctx: HistoryContext) -> HistoryItem:
    """The portal's line for ``record``: only allowlisted detail, no actor identity."""
    agent_acted = record.actor_kind is ActorKind.AGENT_KEY
    # An agent key outside the caller's own set (say, after an undone retirement) is
    # another agent, never "this agent".
    actor_name = record.actor if agent_acted and record.actor in ctx.agents else None
    by: Literal["agent", "another_agent", "admin"] = (
        "admin" if not agent_acted else ("agent" if actor_name is not None else "another_agent")
    )
    entry_id = referenced_entry_id(record)
    shown_entry = entry_id if entry_id is not None and entry_id in ctx.readable_entries else None
    summary, reason, about = _summary(record, ctx, agent_acted)
    return HistoryItem(
        id=record.id,
        occurred_at=record.occurred_at,
        action=record.action.value,
        by=by,
        agent=actor_name,
        about=about,
        summary=summary,
        reason=reason,
        entry_id=shown_entry,
    )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _level(value: object) -> str:
    """A trust level as ``2 (contributor)``; the CLI records it as a string."""
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return f"{value} ({TrustLevel(value).label})"
        except ValueError:
            pass
    return "an unknown level"


def _when(value: object) -> str:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M UTC")
        except ValueError:
            pass
    return "an unknown time"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


_UNPIN_REASONS = {
    "withdrawn": "the memory was withdrawn",
    "agent_cleanup": "by a cleanup",
    "fleet_move_undo": "by an undone fleet move",
    "fleet_merge_undo": "by an undone fleet merge",
    "fleet_move": "by a fleet move",
    "moved_to_self": "the memory was moved to self",
}


def _about(record: AuditRecord, ctx: HistoryContext) -> str | None:
    """The agent (or predecessor) the row concerns, if it can be named."""
    candidates: list[object] = [record.target, record.detail.get("author")]
    agents = record.detail.get("agents")
    if isinstance(agents, list):
        candidates.extend(agents)
    candidates.append(record.detail.get("successor"))
    if record.actor_kind is ActorKind.AGENT_KEY:  # a CLI or admin actor is never named
        candidates.append(record.actor)
    for c in candidates:
        if isinstance(c, str) and c in ctx.agents:
            return c
    return None


def _summary(
    record: AuditRecord, ctx: HistoryContext, agent_acted: bool
) -> tuple[str, str | None, str | None]:
    d = record.detail
    about = _about(record, ctx)
    reason: str | None = None
    match record.action:
        case AuditAction.AGENT_REGISTER:
            text = "Registered, waiting for activation"
        case AuditAction.AGENT_ACTIVATE:
            text = f"Activated as {_level(d.get('trust_level'))}"
            if isinstance(d.get("home_fleet_id"), str):
                text += f" in {ctx.fleet(d.get('home_fleet_id'))}"
        case AuditAction.AGENT_TRUST_LEVEL_SET:
            if d.get("from") is None:
                text = f"Trust level set to {_level(d.get('to'))}"
            else:
                text = f"Trust level changed from {_level(d.get('from'))} to {_level(d.get('to'))}"
        case AuditAction.AGENT_HOME_FLEET_SET:
            if d.get("from") is None:
                text = f"Home fleet set to {ctx.fleet(d.get('to'))}"
            else:
                text = (
                    f"Home fleet changed from {ctx.fleet(d.get('from'))} "
                    f"to {ctx.fleet(d.get('to'))}"
                )
        case AuditAction.AGENT_OWNER_SET:
            text = "The owner recorded for it was changed"  # the aliases are not shown
        case AuditAction.AGENT_ROLE_SET:
            text = f"Marked as a {d.get('role')}"
        case AuditAction.AGENT_ROLE_CLEAR:
            text = f"No longer a {d.get('role')}"
        case AuditAction.AGENT_REVOKE:
            if d.get("via") == "retire":
                text = f"Revoked when it was retired in favour of {ctx.agent(d.get('successor'))}"
            else:
                text = "Revoked"
        case AuditAction.AGENT_RETIRE:
            text = f"Retired; its memories now belong to {ctx.agent(d.get('successor'))}"
        case AuditAction.AGENT_RETIRE_UNDO:
            text = (
                "Retirement undone; its memories no longer belong to "
                f"{ctx.agent(d.get('successor'))}"
            )
        case AuditAction.AGENT_KEY_ISSUE:
            text = "A key was issued"
        case AuditAction.AGENT_KEY_REPLACE:
            text = "The key was replaced"
        case AuditAction.AGENT_KEY_LOCK:
            text = "The key was locked from the portal"
        case AuditAction.AGENT_CLEANUP:
            text = (
                f"Cleanup of {_when(d.get('since'))} to {_when(d.get('until'))}: "
                f"{_plural(_count(d.get('withdrawn')), 'memory', 'memories')} withdrawn, "
                f"{_plural(_count(d.get('ratings_set_aside')), 'rating', 'ratings')} set aside, "
                f"{_plural(_count(d.get('restored')), 'memory', 'memories')} restored, "
                f"{_plural(_count(d.get('pins_removed')), 'pin', 'pins')} removed"
            )
        case AuditAction.AGENT_CLEANUP_UNDO:
            text = (
                "Cleanup undone: "
                f"{_plural(_count(d.get('reactivated')), 'memory', 'memories')} reactivated, "
                f"{_plural(_count(d.get('ratings_cleared')), 'rating', 'ratings')} put back, "
                f"{_plural(_count(d.get('pins_restored')), 'pin', 'pins')} restored"
            )
        case AuditAction.ENTRY_WITHDRAW:
            reason = _text(d.get("reason"))
            author = d.get("author")
            if agent_acted and record.actor not in ctx.agents:
                reason = None  # another agent's words
                text = f"A memory by {ctx.agent(author)} was withdrawn"
            elif d.get("via") == "succession":
                text = f"Withdrew a memory inherited from {ctx.agent(author)}"
            elif agent_acted:
                text = "Withdrew a memory"
            else:
                text = f"A memory by {ctx.agent(author)} was withdrawn"
        case AuditAction.ENTRY_RESTORE:
            # Only an admin restores (ADR 0124); as with a redact, the reason is shown only for
            # this agent's own or inherited memory.
            author = d.get("author")
            if author in ctx.agents:
                reason = _text(d.get("reason"))
            text = f"An admin restored a memory by {ctx.agent(author)}"
        case AuditAction.ENTRY_REDACT:
            # ADR 0125: as a restore; never the counts of other agents' notes in the detail.
            author = d.get("author")
            if author in ctx.agents:
                reason = _text(d.get("reason"))
            text = f"An admin redacted a memory by {ctx.agent(author)}"
        case AuditAction.ENTRY_MOVE_TO_SELF:
            author = d.get("author")
            if author in ctx.agents:
                reason = _text(d.get("reason"))
            if agent_acted and record.actor in ctx.agents:
                text = (
                    "Moved a memory to self"
                    if author == record.actor
                    else f"Moved a memory inherited from {ctx.agent(author)} to self"
                )
            elif agent_acted:
                text = f"A memory by {ctx.agent(author)} was moved to self"
            else:
                text = f"An admin moved a memory by {ctx.agent(author)} to self"
        case AuditAction.ENTRY_PIN:
            text = "Pinned a memory"
            if isinstance(d.get("fleet_id"), str):
                text += f" in {ctx.fleet(d.get('fleet_id'))}"
        case AuditAction.ENTRY_UNPIN:
            text = "Unpinned a memory"
            why = _UNPIN_REASONS.get(str(d.get("reason")))
            if why:
                text += f" ({why})"
        case AuditAction.FLEET_MOVE_AGENTS:
            text = f"Moved from {ctx.fleet(record.target)} to {ctx.fleet(d.get('target_fleet_id'))}"
        case _:
            text = "Something changed"
    return text, reason, about


__all__ = [
    "AN_ADMIN",
    "OTHER_AGENT",
    "OTHER_FLEET",
    "THIS_AGENT",
    "HistoryContext",
    "HistoryItem",
    "describe",
    "fleet_readable",
    "referenced_entry_id",
    "referenced_fleet_ids",
]
