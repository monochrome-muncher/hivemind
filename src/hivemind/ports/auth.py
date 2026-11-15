"""The authentication seam."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from hivemind.domain.access import (
    SCOPE_ORG,
    SCOPE_SELF,
    TrustLevel,
    Visibility,
    write_scopes_for,
)
from hivemind.domain.audit import ActorKind
from hivemind.domain.succession import owned_authors


@runtime_checkable
class Authenticator(Protocol):
    """Credential check and key management (SPEC §8, ADR 0012). A raw key is returned once; only its
    hash is stored (SPEC §8.1)."""

    async def verify(self, key: str) -> Credential | None:
        """Resolve a key to its credential, or ``None``.

        An agent key resolves only while its agent is ``active`` (ADR 0039): pending, revoked or
        missing agents, pre-v2 ``user`` keys and name-less agent keys give ``None``. A portal-locked
        key of an active agent raises ``KeyLocked`` (ADR 0117)."""
        ...

    async def credential_for_agent(self, name: str) -> Credential | None:
        """What ``verify`` returns for the agent's key, built from the agent's record instead of a
        key (``key_id`` is ``None``; a locked key changes nothing). ``None`` unless the agent is
        ``active``, whether unknown, pending or revoked (ADR 0136)."""
        ...

    async def issue_agent_key(self, agent_name: str) -> str:
        """Issue an agent key; returns the raw secret once. ``UnknownAgent``; ``InvalidAgentStatus``
        unless ``active`` (ADR 0039). Replaces any stale key row: one live key per agent."""
        ...

    async def replace_agent_key(
        self, agent_name: str, *, audit_actor_kind: ActorKind, audit_actor: str
    ) -> KeyReplacement:
        """Swap an active agent's key in one transaction (ADR 0108): the old key stops at once.

        The ``agent_key.replace`` audit row (both fingerprints, never a key) is written in the same
        transaction under the given actor. ``UnknownAgent``; ``InvalidAgentStatus`` unless
        ``active``. The raw key is returned once."""
        ...

    async def lock_agent_key(self, agent_name: str, *, key_id: str) -> bool:
        """Lock the agent's own key from the portal (ADR 0117): ``verify`` raises ``KeyLocked`` from
        the next request.

        Only the key with fingerprint ``key_id`` is locked, so a key replaced meanwhile is left
        alone. Writes the ``agent_key.lock`` audit row (actor kind ``agent_key``, the agent's name,
        the fingerprint, ``via: "portal"``, never a key) in the same transaction. ``False``, nothing
        written, when that key is already locked or gone."""
        ...

    async def revoke_agent_key(self, agent_name: str) -> None:
        """Retire the agent's key (the name stays reserved, ADR 0012)."""
        ...

    async def rotate_org_key(self) -> str:
        """Rotate the shared org key (closes registration, ADR 0031); returns the raw secret
        once."""
        ...

    async def issue_admin_key(self) -> str:
        """Issue an admin key; return the raw secret once."""
        ...


@dataclass(frozen=True, slots=True)
class KeyReplacement:
    """Outcome of ``Authenticator.replace_agent_key`` (ADR 0108): the new raw key (shown once) and
    both non-secret fingerprints; ``old_key_id`` is ``None`` if the agent held no key."""

    raw_key: str = field(repr=False)
    new_key_id: str
    old_key_id: str | None


@dataclass(frozen=True, slots=True)
class Credential:
    """A verified caller: who acts and with which rights.

    ``user_id`` / ``agent_id`` are the v1 identity (SPEC §8.1). v2 adds (ADRs 0011-0012):

    * ``is_org``: the shared org key (registration and health only).
    * ``trust_level`` / ``home_fleet_id``: the agent's privilege; ``agent_name`` is its verified
      name (the entry ``author``).
    * ``key_id``: the key's non-secret fingerprint (first 12 hex chars of its hash, ADR 0027);
      ``None`` with no stored key (dev mode, fakes).
    """

    user_id: str
    agent_id: str | None = None
    is_admin: bool = False
    is_org: bool = False
    access_controlled: bool = False
    trust_level: TrustLevel = TrustLevel.UNTRUSTED
    home_fleet_id: str | None = None
    agent_name: str | None = None
    key_id: str | None = None  # ADR 0027: key fingerprint, never the key
    # Agents handing down to this one, transitively (ADR 0113): their memories are its to read,
    # supersede and withdraw, not to rate; ``author`` stays the original name.
    inherits_from: tuple[str, ...] = ()

    @property
    def owned_authors(self) -> tuple[str, ...]:
        """Authors whose memories this caller owns: its own name, then every agent handing down to
        it (ADR 0113)."""
        return owned_authors(self.agent_name or self.user_id, self.inherits_from)

    def owns(self, author: str) -> bool:
        """Whether a memory by ``author`` is this caller's own."""
        return author == (self.agent_name or self.user_id) or author in self.inherits_from

    def audit_actor(self) -> str:
        """The audit ``actor`` (ADR 0027): ``admin:<fingerprint>`` (admin keys share ``user_id``),
        ``org:<fingerprint>`` (ADR 0046), else the agent's name (ADR 0096)."""
        if not (self.is_org or self.is_admin):
            return self.agent_name or self.user_id
        if self.key_id is None:
            return self.user_id
        return f"{'org' if self.is_org else 'admin'}:{self.key_id}"

    def visibility(self) -> Visibility:
        """The reader's ``Visibility`` (ADR 0011): admins see everything, others per the trust
        matrix."""
        name = self.agent_name or self.user_id
        if self.is_admin or not self.access_controlled:
            return Visibility(
                level=TrustLevel.PRIVILEGED,
                name=name,
                home_fleet_id=self.home_fleet_id,
                is_admin=True,
            )
        return Visibility(
            level=self.trust_level,
            name=name,
            home_fleet_id=self.home_fleet_id,
            inherited=self.inherits_from,
        )

    def max_write_scope(self) -> str:
        """The highest scope this credential may write (ADR 0011): levels 2/3 ``fleet``, level 1
        ``self``; level 0 writes nothing (enforced at the write seam)."""
        if self.is_admin or not self.access_controlled:
            return SCOPE_ORG
        # Level 0 keeps `self` here but writes nothing: `can_write` gates it.
        scopes = write_scopes_for(self.trust_level)
        return scopes[-1] if scopes else SCOPE_SELF

    def can_write(self) -> bool:
        """Whether this credential may write at all: not level 0, always the admin key."""
        if self.is_admin or not self.access_controlled:
            return True
        return self.trust_level > TrustLevel.UNTRUSTED

    @property
    def is_stored(self) -> bool:
        """Whether the authenticator issued this credential (ADR 0082): an admin, org or active
        agent key. Otherwise it is a dev-mode, hand-built identity."""
        return self.is_admin or self.is_org or self.agent_name is not None

    def effective(self, require_stored_keys: bool = False) -> Credential:
        """This credential, or its trust-level-0 stand-in when ``HIVEMIND_REQUIRE_STORED_KEYS`` (ADR
        0082) is set and this one is dev-mode. Stored credentials come back as the same object."""
        if require_stored_keys and not self.is_stored:
            return Credential(
                user_id=self.user_id,
                agent_id=self.agent_id,  # attribution survives the downgrade
                access_controlled=True,
                trust_level=TrustLevel.UNTRUSTED,
            )
        return self

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        kind = "admin" if self.is_admin else ("org" if self.is_org else "agent")
        return (
            f"Credential({kind}, user={self.user_id!r}, "
            f"agent={self.agent_name or self.agent_id!r}, level={self.trust_level.value})"
        )
