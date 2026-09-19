"""Access-control service (ADRs 0011-0012, SPEC §12).

The deep module of the access plane: one small interface (register /
activate / fleet / level / revoke) over a deep interior — permission
gating (which key may do what, ADR 0012), agent registration +
activation (the key is issued **once**, ADR 0012), and write-scope
resolution (which scope a trust level may write, ADR 0011).

The services are the only orchestrator; the HTTP and MCP layers are
thin (validation + auth + error mapping only). All I/O happens through
the ``Store`` and ``Authenticator`` ports.
"""

from __future__ import annotations

from dataclasses import dataclass

from hivemind.domain.access import (
    Agent,
    Fleet,
    TrustLevel,
)
from hivemind.ports import Authenticator, Credential, Store
from hivemind.services.governance import PermissionDenied


class AccessService:
    """Registration + fleet + trust-level management (ADRs 0011-0012).

    Every method takes the caller's ``credential`` and enforces the
    permission gate (ADR 0012): registration is gated on the org or
    admin key (a low-privilege bootstrap act); activation, fleet
    creation, level changes, revocation, and org-key rotation require
    the admin key (elevated acts).
    """

    def __init__(self, store: Store, authenticator: Authenticator | None = None) -> None:
        self._store = store
        self._authenticator = authenticator

    def _require_authenticator(self) -> Authenticator:
        """Key-management ops (activate / revoke / rotate) need the
        authenticator; store-only ops (register / fleet / level) do not.
        In dev (in-memory) mode there is no authenticator, so key
        management is unavailable (a production concern, ADR 0012).
        """
        if self._authenticator is None:
            raise PermissionDenied(
                "key management requires an authenticator (not configured in dev mode)"
            )
        return self._authenticator

    # -- registration (gated: org or admin key, ADR 0012) ------------------

    async def register(
        self,
        name: str,
        credential: Credential,
        owner_alias: str | None = None,
        *,
        org_only: bool = False,
    ) -> Agent:
        """Register (or re-register) an agent (ADR 0012).

        Gated on the org or admin key (REST, SPEC §5.1); the MCP
        ``hive_register`` verb is **org-key only** (SPEC §5.2) — pass
        ``org_only=True``. Idempotent: re-registering a pending name
        returns the existing record; an *active* name is a conflict
        (the name stays reserved — pick a new one, ADR 0012).
        """
        if org_only:
            self._require_org(credential)
        else:
            self._require_org_or_admin(credential)
        existing = await self._store.get_agent(name)
        if existing is not None and existing.status.value == "active":
            raise ValueError("agent name already active (name is reserved; pick a new one)")
        return await self._store.register_agent(name, owner_alias)

    # -- admin-gated operations (ADR 0012) ----------------------------------

    async def activate(
        self,
        name: str,
        trust_level: TrustLevel,
        home_fleet_id: str,
        credential: Credential,
    ) -> tuple[Agent, str]:
        """Activate a pending agent: set trust level + home fleet and flip
        it to ``active``; issue its key **once** (returned here, never
        stored again — ADR 0012). Admin-gated. Returns (agent, raw_key).
        """
        self._require_admin(credential)
        agent = await self._store.activate_agent(
            name, trust_level=trust_level, home_fleet_id=home_fleet_id
        )
        key = await self._require_authenticator().issue_agent_key(name)
        return agent, key

    async def create_fleet(self, name: str, credential: Credential) -> Fleet:
        """Create a named fleet (admin-gated, ADR 0012)."""
        self._require_admin(credential)
        return await self._store.create_fleet(name)

    async def set_trust_level(
        self, name: str, level: TrustLevel, credential: Credential
    ) -> Agent:
        """Promote/demote an agent's trust level (admin-gated, ADR 0011).
        Demotion to level 0 is *dormant* (key still valid, no access) —
        distinct from revocation (ADR 0012)."""
        self._require_admin(credential)
        return await self._store.set_agent_trust_level(name, level)

    async def set_home_fleet(
        self, name: str, fleet_id: str, credential: Credential
    ) -> Agent:
        """Re-parent an agent to a new home fleet (admin-gated, ADR 0011).
        The agent's earlier ``fleet``-scoped entries stay in the fleet
        they were written into (never re-parented)."""
        self._require_admin(credential)
        return await self._store.set_agent_home_fleet(name, fleet_id)

    async def revoke(self, name: str, credential: Credential) -> None:
        """Revoke an agent's key (admin-gated, ADR 0012). The agent record
        + name stay reserved (dormant); the key is dead."""
        self._require_admin(credential)
        await self._require_authenticator().revoke_agent_key(name)

    async def rotate_org_key(self, credential: Credential) -> str:
        """Rotate the shared org key — the cluster kill switch (ADR 0012).
        All prior org keys stop working; the new key is returned once.
        Admin-gated."""
        self._require_admin(credential)
        return await self._require_authenticator().rotate_org_key()

    # -- read (admin-gated listing) ------------------------------------------

    async def list_agents(self, credential: Credential) -> list[Agent]:
        self._require_admin(credential)
        return await self._store.list_agents()

    async def list_fleets(self, credential: Credential) -> list[Fleet]:
        self._require_admin(credential)
        return await self._store.list_fleets()

    # -- permission gates (ADR 0012) -----------------------------------------

    def _require_org_or_admin(self, credential: Credential) -> None:
        if not (credential.is_org or credential.is_admin):
            raise PermissionDenied("registration requires an org or admin key (ADR 0012)")

    def _require_org(self, credential: Credential) -> None:
        if not credential.is_org:
            raise PermissionDenied("hive_register is gated on the org key (SPEC §5.2)")

    def _require_admin(self, credential: Credential) -> None:
        if not credential.is_admin:
            raise PermissionDenied("this action requires an admin key (ADR 0012)")


# -- write-scope resolution (ADR 0011) ---------------------------------------


@dataclass(frozen=True, slots=True)
class WriteResolution:
    """The resolved write scope + fleet for a write (ADR 0011)."""

    scope: str
    fleet_id: str | None


# Scope ranks: a writer may not write a scope higher than its level allows.
_SCOPE_RANK = {"self": 1, "fleet": 2, "org": 3}


def resolve_write_scope(
    credential: Credential, requested_scope: str | None = None
) -> WriteResolution:
    """Resolve + validate the write scope for this credential (ADR 0011).

    * If ``requested_scope`` is omitted, it defaults to the highest scope
      the trust level permits (L1 → ``self``; L2/L3 → ``fleet``).
    * An explicit scope may not exceed the level's maximum (L1 may not
      write ``fleet``; level 0 may not write at all).
    * ``fleet`` scope requires a home fleet (the agent's entries land in
      its home fleet, ADR 0011).

    Raises ``PermissionDenied`` on any out-of-permission scope.
    """
    if not credential.can_write():
        raise PermissionDenied("level 0 (untrusted) agents may not write (ADR 0011)")
    max_scope = credential.max_write_scope()
    if requested_scope is None:
        scope = max_scope
    else:
        scope = requested_scope
        if _SCOPE_RANK.get(scope, 99) > _SCOPE_RANK.get(max_scope, 99):
            raise PermissionDenied(
                f"trust level {credential.trust_level.value} may not write "
                f"scope '{scope}' (ADR 0011)"
            )
    fleet_id: str | None = None
    if scope == "fleet" and credential.access_controlled and not credential.is_admin:
        # v2 fleet scope is bound to the agent's home fleet (ADR 0011):
        # the entry lands in the home fleet. Legacy (v1) and admins have no
        # home-fleet binding (flat pool / bypass).
        if credential.home_fleet_id is None:
            raise PermissionDenied(
                "agent has no home fleet; cannot write scope 'fleet' (ADR 0011)"
            )
        fleet_id = credential.home_fleet_id
    return WriteResolution(scope=scope, fleet_id=fleet_id)