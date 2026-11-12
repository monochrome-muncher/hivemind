"""Access-control domain model (SPEC §12, ADRs 0011-0012): trust levels and the one visibility
predicate.

``self``-scoped entries are private to their author even at level 3. ``fleet``-scoped
entries stay in the writer's home fleet at write time. Legacy ``org`` entries are
readable at level >= 1 (ADR 0011).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import IntEnum, StrEnum

from hivemind.domain.agent_role import AgentRole, role_hint
from hivemind.domain.entry import Entry, EntryState
from hivemind.domain.fleet_phase import FleetPhase


class TrustLevel(IntEnum):
    """The cumulative privilege ladder, 0-3 (SPEC §12.2, ADR 0011)."""

    UNTRUSTED = 0
    LURKER = 1
    CONTRIBUTOR = 2
    PRIVILEGED = 3

    @property
    def label(self) -> str:
        """The display name used in the admin surface and error messages."""
        return _TRUST_LEVEL_LABELS[self]


_TRUST_LEVEL_LABELS = {
    TrustLevel.UNTRUSTED: "untrusted",
    TrustLevel.LURKER: "lurker",
    TrustLevel.CONTRIBUTOR: "contributor",
    TrustLevel.PRIVILEGED: "privileged",
}


class AgentStatus(StrEnum):
    """Lifecycle of a registered agent (ADRs 0012, 0028); a revoked name stays reserved."""

    PENDING = "pending"
    ACTIVE = "active"
    REVOKED = "revoked"


# The lifecycle transitions (ADR 0028): the statuses each verb may start from.
ACTIVATABLE = frozenset({AgentStatus.PENDING, AgentStatus.REVOKED})
REVOCABLE = frozenset({AgentStatus.PENDING, AgentStatus.ACTIVE})


class UnknownAgent(KeyError):
    """No agent has this name (404 ``not_found``). A ``KeyError``, so ``str(exc)`` is quoted."""


class UnknownSearchAgent(UnknownAgent):
    """The admin search named an agent that does not exist (404 ``unknown_agent``, ADR 0136)."""


class KeyLocked(Exception):
    """The key was locked from the agent portal (ADR 0117): 401 ``key_locked`` until an admin
    replaces it. Raised by ``Authenticator.verify``."""

    def __init__(self) -> None:
        super().__init__(KEY_LOCKED_MESSAGE)


# What every surface tells the holder of a locked key.
KEY_LOCKED_MESSAGE = (
    "this API key was locked because it may have leaked; ask the admin for a new key"
)


class UnknownFleet(KeyError):
    """No fleet has this id (404 ``not_found``). A ``KeyError``, so ``str(exc)`` is quoted."""


class FleetExists(ValueError):
    """A fleet with this name already exists (409 ``fleet_exists``)."""


class FleetMerged(Exception):
    """The fleet was merged into another (ADR 0107): 409 ``fleet_merged``, naming the target."""

    def __init__(self, fleet_id: str, target_id: str, target_name: str | None) -> None:
        shown = f"{target_name!r} (id {target_id})" if target_name else f"id {target_id}"
        super().__init__(f"fleet {fleet_id} was merged into {shown}; retry against that fleet")
        self.fleet_id = fleet_id
        self.target_id = target_id
        self.target_name = target_name


class FleetBusy(TimeoutError):
    """A fleet write gave up: it timed out on the fleet's row lock (a merge or move holds it, ADRs
    0107, 0112), or its author's home fleet changed during the write (ADR 0112). A
    ``TimeoutError``, so surfaces answer a retryable 503 ``fleet_busy``."""

    def __init__(self, fleet_id: str, detail: str | None = None) -> None:
        super().__init__(
            detail or f"fleet {fleet_id} is being merged or moved, retry in a few seconds"
        )
        self.fleet_id = fleet_id


class StoreUnavailable(TimeoutError):
    """The store failed while verifying a key, which must not answer 401. A ``TimeoutError``,
    so surfaces answer a retryable 503 ``store_unavailable``. The message never carries the
    driver's text (it can name the host or the key hash)."""

    def __init__(self, cause: str) -> None:
        super().__init__(f"the store is unavailable ({cause})")
        self.cause = cause


class InvalidAgentStatus(Exception):
    """A lifecycle verb was applied to an agent in a status it cannot start from (ADR 0028)."""

    def __init__(self, name: str, status: AgentStatus, verb: str) -> None:
        super().__init__(f"cannot {verb} agent {name!r}: it is {status.value}")
        self.name = name
        self.status = status
        self.verb = verb


class AgentNotActive(InvalidAgentStatus):
    """The admin search named an agent that cannot search: pending or revoked (409
    ``agent_not_active``, ADR 0136)."""

    def __init__(self, name: str, status: AgentStatus) -> None:
        super().__init__(name, status, "search as")


# Entry-scope values (SPEC §12.1).
SCOPE_SELF = "self"
SCOPE_FLEET = "fleet"
SCOPE_ORG = "org"  # legacy v1 value: grandfathered read-only (ADR 0011).

# Scope ordering, lowest to highest (ADR 0011): the one source for "which scope exceeds which".
SCOPE_RANK: dict[str, int] = {SCOPE_SELF: 1, SCOPE_FLEET: 2, SCOPE_ORG: 3}


def write_scopes_for(trust_level: TrustLevel) -> tuple[str, ...]:
    """The scopes ``trust_level`` may write (ADR 0011), lowest first. ``org`` is off the
    ladder: only ``Credential.max_write_scope`` (admin, legacy) reaches it."""
    if trust_level <= TrustLevel.UNTRUSTED:
        return ()
    if trust_level <= TrustLevel.LURKER:
        return (SCOPE_SELF,)
    return (SCOPE_SELF, SCOPE_FLEET)


@dataclass(frozen=True, slots=True)
class Fleet:
    """A named group of agents sharing ``fleet``-scoped entries (ADR 0011); entries reference
    ``id``."""

    id: str
    name: str
    created_at: datetime
    # Set when merged into another fleet (ADR 0107): the target's id and the time.
    merged_into: str | None = None
    merged_at: datetime | None = None
    # ADR 0143: as stored, so it may have ended; read it through ``phase_in_force``.
    phase: FleetPhase | None = None

    def phase_in_force(self, now: datetime) -> FleetPhase | None:
        return self.phase if self.phase is not None and self.phase.in_force(now) else None


@dataclass(frozen=True, slots=True)
class Agent:
    """A registered agent's record (ADR 0012). ``name`` is unique and is the ``author`` of
    its writes; ``owner_alias`` is the owner's contact, kept here only, never on entries."""

    name: str
    status: AgentStatus
    trust_level: TrustLevel
    home_fleet_id: str | None = None
    owner_alias: str | None = None
    created_at: datetime | None = None
    activated_at: datetime | None = None
    # Key facts for the idle list (ADR 0110), filled by ``Store.list_agents`` only. Nothing
    # ranks or permits on them. ``last_used_at`` is to the hour; ``None`` = never or no key.
    last_used_at: datetime | None = None
    key_created_at: datetime | None = None
    # When the key was locked from the portal; ``None`` = usable or no key.
    key_locked_at: datetime | None = None
    role: AgentRole | None = None


def is_idle(agent: Agent, now: datetime, idle_days: int) -> bool:
    """Whether an active agent's key sat unused for ``idle_days`` (ADR 0110). A never-used key
    counts from its issue, then from activation. Pending and revoked agents are never idle."""
    if agent.status is not AgentStatus.ACTIVE:
        return False
    since = agent.last_used_at or agent.key_created_at or agent.activated_at or agent.created_at
    return since is not None and now - since >= timedelta(days=idle_days)


@dataclass(frozen=True, slots=True)
class Standing:
    """What the calling key is and may do (``hive_whoami``, ADR 0030).

    ``key_kind``: ``agent``, ``org``, ``admin`` or ``legacy``. ``can_read`` is drawn from
    ``own``, ``home_fleet``, ``all_fleets``, ``org``, ``everything``; ``can_write_scopes``
    is narrowest first.
    """

    key_kind: str
    name: str
    status: AgentStatus | None
    trust_level: TrustLevel
    home_fleet_id: str | None
    home_fleet_name: str | None
    can_read: tuple[str, ...]
    can_write_scopes: tuple[str, ...]
    # ADR 0113: the agents that hand down to this one; their memories are its own.
    inherits_from: tuple[str, ...] = ()
    # ADR 0114: to the hour; ``None`` for a non-agent key or one never used.
    key_last_used_at: datetime | None = None
    # ADR 0143: the home fleet's phase in force; the reply has no key for it without one.
    fleet_phase: FleetPhase | None = None
    role: AgentRole | None = None

    def as_dict(self) -> dict[str, object]:
        """The JSON shape both surfaces return (REST and MCP, ADR 0030)."""
        standing: dict[str, object] = {
            "key_kind": self.key_kind,
            "name": self.name,
            "status": self.status.value if self.status is not None else None,
            "trust_level": self.trust_level.value,
            "trust_level_name": self.trust_level.label,
            "home_fleet_id": self.home_fleet_id,
            "home_fleet_name": self.home_fleet_name,
            "can_read": list(self.can_read),
            "can_write_scopes": list(self.can_write_scopes),
            "inherits_from": list(self.inherits_from),
            "key_last_used_at": self.key_last_used_at.isoformat()
            if self.key_last_used_at
            else None,
        }
        if self.fleet_phase is not None:
            standing["fleet_phase"] = self.fleet_phase.as_dict()
        if self.role is not None:
            standing["role"] = self.role.value
            standing["role_hint"] = role_hint(self.role, self.home_fleet_name)
        return standing


@dataclass(frozen=True, slots=True)
class Visibility:
    """What a caller may see, derived from its credential. ``is_admin`` bypasses every rule."""

    level: TrustLevel
    name: str
    home_fleet_id: str | None = None
    is_admin: bool = False
    # ADR 0113: agents that hand down to ``name``; their entries count as its own.
    inherited: tuple[str, ...] = ()

    @property
    def owned(self) -> tuple[str, ...]:
        """The authors this reader owns: ``name`` first, then ``inherited``."""
        return (self.name, *self.inherited)

    def owns(self, author: str) -> bool:
        return author == self.name or author in self.inherited

    def entry_visible(self, entry: Entry) -> bool:
        return entry_is_visible(entry, self)


def entry_is_visible(entry: Entry, v: Visibility) -> bool:
    """The visibility predicate (SPEC §12.2, ADR 0011), which every store adapter must match
    (``PgStore`` in SQL):
      * admin -> everything; level 0 -> nothing.
      * ``self`` -> only the author (private even at L3) or an heir (ADR 0113).
      * ``org`` -> any level >= 1.
      * ``fleet`` -> the reader's own entries, the home fleet's, or all at level 3.
    """
    if v.is_admin:
        return True
    if v.level == TrustLevel.UNTRUSTED:
        return False

    scope = entry.scope
    if scope == SCOPE_SELF:
        return v.owns(entry.author)

    if scope == SCOPE_ORG:
        return True

    # Fleet scope: own entries stay visible after a fleet move.
    if v.owns(entry.author):
        return True
    if v.level == TrustLevel.PRIVILEGED:
        return True
    # Two empty fleet ids are "no fleet", not the same fleet.
    return bool(entry.fleet_id) and entry.fleet_id == v.home_fleet_id


def may_supersede(
    target: Entry, *, new_scope: str, new_fleet_id: str | None, writer: Visibility
) -> bool:
    """Whether ``writer`` may supersede ``target`` with an entry at ``new_scope`` in
    ``new_fleet_id`` (ADR 0033, SPEC §4.1). The writer must read the target and the successor
    must reach at least its audience, so a supersession never narrows who sees it.

      * non-active target -> no one (ADR 0034).
      * admin -> any active target with an ``org`` successor (ADR 0060), or its own ``self`` entry.
      * ``self`` target -> the writer's own, any successor scope.
      * ``fleet`` target -> a ``fleet`` successor in the same fleet.
      * ``org`` target -> admin only.
    """
    if target.state is not EntryState.ACTIVE:
        return False
    if writer.is_admin:
        own_note = target.scope == SCOPE_SELF and writer.owns(target.author)
        return own_note or new_scope == SCOPE_ORG
    if not entry_is_visible(target, writer):
        return False
    if target.scope == SCOPE_SELF:
        return True
    if target.scope == SCOPE_FLEET:
        return new_scope == SCOPE_FLEET and target.fleet_id == new_fleet_id
    return False


__all__ = [
    "KEY_LOCKED_MESSAGE",
    "SCOPE_FLEET",
    "SCOPE_ORG",
    "SCOPE_SELF",
    "Agent",
    "AgentStatus",
    "Fleet",
    "FleetBusy",
    "FleetExists",
    "FleetMerged",
    "InvalidAgentStatus",
    "KeyLocked",
    "Standing",
    "StoreUnavailable",
    "TrustLevel",
    "UnknownAgent",
    "UnknownFleet",
    "Visibility",
    "entry_is_visible",
    "is_idle",
    "may_supersede",
]
