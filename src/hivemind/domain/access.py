"""Access-control domain model (SPEC.md §12, ADRs 0011-0012).

Pure data and the one visibility predicate every store and surface shares.

The trust ladder (SPEC §12.2, ADR 0011) is cumulative:

    level 0  untrusted    nothing                                    (reads: none)
    level 1  lurker       own + home fleet                            (reads: own, home fleet, legacy org)
    level 2  contributor  own + home fleet                            (writes: + home fleet)
    level 3  privileged   own + home fleet + every fleet             (reads: all fleets; writes stay local)

``self``-scoped entries are private to their author **even at level 3**.
``fleet``-scoped entries are fixed to the writer's home fleet at write
time (ADR 0011: re-parenting an agent never moves existing entries).
Legacy ``org``-scoped entries (the v1 flat-pool value, ADR 0002) stay
readable at any level >= 1 (grandfathered read-only, ADR 0011).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum

from hivemind.domain.entry import Entry, EntryState


class TrustLevel(IntEnum):
    """The cumulative privilege ladder (SPEC §12.2, ADR 0011).

    The *value* is the numeric level (0-3); ``label`` is the display
    name used in the admin surface and error messages.
    """

    UNTRUSTED = 0
    LURKER = 1
    CONTRIBUTOR = 2
    PRIVILEGED = 3

    @property
    def label(self) -> str:
        """The canonical level name (``untrusted``/``lurker``/
        ``contributor``/``privileged``)."""
        return _TRUST_LEVEL_LABELS[self]


_TRUST_LEVEL_LABELS = {
    TrustLevel.UNTRUSTED: "untrusted",
    TrustLevel.LURKER: "lurker",
    TrustLevel.CONTRIBUTOR: "contributor",
    TrustLevel.PRIVILEGED: "privileged",
}


class AgentStatus(StrEnum):
    """Lifecycle of a registered agent (ADR 0012).

    ``pending``: awaiting activation, no data access. ``active``: has a
    key, trust level and home fleet. ``revoked``: key killed or
    registration rejected (ADR 0028); the name stays reserved.
    """

    PENDING = "pending"
    ACTIVE = "active"
    REVOKED = "revoked"


# The lifecycle transitions (ADR 0028): the statuses each verb may start from.
ACTIVATABLE = frozenset({AgentStatus.PENDING, AgentStatus.REVOKED})
REVOCABLE = frozenset({AgentStatus.PENDING, AgentStatus.ACTIVE})


class InvalidAgentStatus(Exception):
    """A lifecycle verb was applied to an agent in a status it cannot
    start from (ADR 0028) — e.g. ``activate`` on an ``active`` agent."""

    def __init__(self, name: str, status: AgentStatus, verb: str) -> None:
        super().__init__(f"cannot {verb} agent {name!r}: it is {status.value}")
        self.name = name
        self.status = status
        self.verb = verb


# The entry-scope values (SPEC §12.1): `self` (the writer only),
# `fleet` (the home fleet it was written into), and `org` (legacy).
SCOPE_SELF = "self"
SCOPE_FLEET = "fleet"
SCOPE_ORG = "org"  # legacy v1 value: grandfathered read-only (ADR 0011).


@dataclass(frozen=True, slots=True)
class Fleet:
    """A named group of agents sharing ``fleet``-scoped entries (ADR
    0011). Admin-created; no deletion yet. Entries reference the stable ``id``.
    """

    id: str
    name: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Agent:
    """A registered agent's record (ADR 0012) — the admin's row per agent.

    ``name`` is unique and becomes the ``author`` of its writes.
    ``owner_alias`` is the human owner's contact for the admin, kept on
    this record only, **not** on entries.
    """

    name: str
    status: AgentStatus
    trust_level: TrustLevel
    home_fleet_id: str | None = None
    owner_alias: str | None = None
    created_at: datetime | None = None
    activated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Standing:
    """What the calling key is and may do (``hive_whoami``, ADR 0030).

    Plain data: the agent skill, not the API, turns it into advice.
    ``key_kind`` is ``agent``, ``org``, ``admin`` or ``legacy`` (a v1 /
    dev-mode credential with no access control). ``can_read`` uses the
    vocabulary ``own``, ``home_fleet``, ``all_fleets``, ``org`` (the
    legacy scope) and ``everything``; ``can_write_scopes`` lists the
    scopes a write may use, narrowest first.
    """

    key_kind: str
    name: str
    status: AgentStatus | None
    trust_level: TrustLevel
    home_fleet_id: str | None
    home_fleet_name: str | None
    can_read: tuple[str, ...]
    can_write_scopes: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        """The JSON shape both surfaces return (REST and MCP, ADR 0030)."""
        return {
            "key_kind": self.key_kind,
            "name": self.name,
            "status": self.status.value if self.status is not None else None,
            "trust_level": self.trust_level.value,
            "trust_level_name": self.trust_level.name.lower(),
            "home_fleet_id": self.home_fleet_id,
            "home_fleet_name": self.home_fleet_name,
            "can_read": list(self.can_read),
            "can_write_scopes": list(self.can_write_scopes),
        }


@dataclass(frozen=True, slots=True)
class Visibility:
    """The read-side view of a caller: what that caller may see.

    Derived from a credential (its trust level, home fleet, and agent
    name). ``is_admin`` is a bypass: admins may see and act on anything.
    """

    level: TrustLevel
    name: str
    home_fleet_id: str | None = None
    is_admin: bool = False

    def entry_visible(self, entry: Entry) -> bool:
        """Whether ``entry`` is visible to this caller (the matrix in
        SPEC §12.2 / ADR 0011)."""
        return entry_is_visible(entry, self)


def entry_is_visible(entry: Entry, v: Visibility) -> bool:
    """The visibility predicate (SPEC §12.2, ADR 0011).

    The single source of truth every store adapter must implement
    (``PgStore`` in SQL). Rules:
      * admin  -> everything.
      * level 0 (untrusted) -> nothing.
      * ``self``  -> only the entry's own author (private even at L3).
      * ``org``   -> readable at any level >= 1 (legacy flat-pool value).
      * ``fleet`` -> the reader's own fleet-scoped entries (wherever they
        ended up), OR entries in the reader's home fleet, OR all fleets
        at level 3 (privileged).
    """
    if v.is_admin:
        return True
    if v.level == TrustLevel.UNTRUSTED:
        return False

    scope = entry.scope
    if scope == SCOPE_SELF:
        return entry.author == v.name

    if scope == SCOPE_ORG:
        return True

    # scope == SCOPE_FLEET. Own entries stay visible after a fleet move.
    if entry.author == v.name:
        return True
    if v.level == TrustLevel.PRIVILEGED:
        return True
    return entry.fleet_id is not None and entry.fleet_id == v.home_fleet_id


def may_supersede(
    target: Entry, *, new_scope: str, new_fleet_id: str | None, writer: Visibility
) -> bool:
    """Whether ``writer`` may supersede ``target`` with an entry written at
    ``new_scope`` into ``new_fleet_id`` (ADR 0033, SPEC §4.1).

    The writer must read the target, and the successor must reach at
    least the predecessor's audience, so a supersession never hides an
    entry behind a narrower one.

      * a non-active target -> no one (ADR 0034): ``superseded_by`` is
        single-valued, so a second claim would be lost.
      * admin -> anything (that is active).
      * ``self`` target -> only the writer's own (visibility already
        guarantees that); any successor scope reaches its one reader.
      * ``fleet`` target -> only by a ``fleet`` successor written into
        the same fleet.
      * ``org`` (legacy) target -> admin only; agents cannot write ``org``.
    """
    if target.state is not EntryState.ACTIVE:
        return False
    if writer.is_admin:
        return True
    if not entry_is_visible(target, writer):
        return False
    if target.scope == SCOPE_SELF:
        return True
    if target.scope == SCOPE_FLEET:
        return new_scope == SCOPE_FLEET and target.fleet_id == new_fleet_id
    return False


__all__ = [
    "SCOPE_FLEET",
    "SCOPE_ORG",
    "SCOPE_SELF",
    "Agent",
    "AgentStatus",
    "Fleet",
    "InvalidAgentStatus",
    "Standing",
    "TrustLevel",
    "Visibility",
    "entry_is_visible",
    "may_supersede",
]
