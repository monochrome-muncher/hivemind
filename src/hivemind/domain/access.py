"""Access-control domain model (SPEC.md §12, ADRs 0011-0012).

Pure data + the visibility predicate — no I/O. This is the storage-
agnostic core of the fleet/trust model: every store adapter (in-memory,
Postgres) and both surfaces (REST, MCP) share this single definition of
"what can this reader see?".

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

from hivemind.domain.entry import Entry


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

    ``pending`` = self-registered, awaiting admin activation (no
    data-plane access). ``active`` = admin-activated (an agent key is
    issued; the agent holds a trust level + home fleet). ``revoked`` =
    its key was killed, or its registration rejected (ADR 0028); the
    name stays reserved and re-activation issues a fresh key.
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
    """A named group of agents that share ``fleet``-scoped entries
    (ADR 0011). Created by the admin; no deletion in this increment.

    ``id`` is a stable identifier (a new entry's ``fleet_id`` references
    it, so re-parenting an agent never touches existing entries).
    """

    id: str
    name: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Agent:
    """A registered agent's record (ADR 0012) — the admin's row per agent.

    ``name`` is the agent's unique registered name (it becomes the
    entry's ``author`` for its writes; verified server-side, never
    self-reported). ``owner_alias`` is the *human* owner's name/alias
    (a username or email the admin uses to reach the owner out-of-band —
    it is stored on the agent record, **not** stamped on entries).
    """

    name: str
    status: AgentStatus
    trust_level: TrustLevel
    home_fleet_id: str | None = None
    owner_alias: str | None = None
    created_at: datetime | None = None
    activated_at: datetime | None = None


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

    Pure and total: given an entry (its ``scope``, ``fleet_id``, and
    ``author``) and a reader's ``Visibility``, returns whether the entry
    is visible. This is the single source of truth every store adapter
    must implement (in-memory in Python, Postgres in SQL).

    Rules:
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
        # Private to the author (even at level 3) — that is what `self` is for.
        return entry.author == v.name

    if scope == SCOPE_ORG:
        # Legacy org-wide value: readable at any level >= 1 (untrusted is
        # already returned False above).
        return True

    # scope == SCOPE_FLEET
    if entry.author == v.name:
        # My own fleet-scoped entries are always visible (they stay in the
        # fleet they were written into, even if I've since moved fleets).
        return True
    if v.level == TrustLevel.PRIVILEGED:
        # Level 3 reads across **all** fleets (read-broad, write-local).
        return True
    # Lurker/contributor: visible only within my home fleet.
    return entry.fleet_id is not None and entry.fleet_id == v.home_fleet_id


__all__ = [
    "SCOPE_FLEET",
    "SCOPE_ORG",
    "SCOPE_SELF",
    "Agent",
    "AgentStatus",
    "Fleet",
    "TrustLevel",
    "Visibility",
    "entry_is_visible",
]
