"""Access-control service (ADRs 0011-0012, SPEC §12).

The deep module of the access plane: one small interface (register /
activate / fleet / level / revoke) over a deep interior — permission
gating (which key may do what, ADR 0012), agent registration +
activation (the key is issued **once**, ADR 0012), and write-scope
resolution (which scope a trust level may write, ADR 0011).

The services are the only orchestrator; the HTTP and MCP layers are
thin (validation + auth + error mapping only). All I/O happens through
the ``Store`` and ``Authenticator`` ports.

Every admin mutation is recorded in the audit log (ADR 0027) **after**
it succeeds. The mutation and the audit write span two ports with no
shared transaction, so they are not atomic: an audit-write failure is
raised (never swallowed — the action happened and the caller must see
that it went unaudited). The audit writer, ``_audit``, takes no key
argument: a raw key cannot reach an audit column by construction.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from hivemind.domain.access import (
    REVOCABLE,
    Agent,
    AgentStatus,
    Fleet,
    InvalidAgentStatus,
    TrustLevel,
)
from hivemind.domain.audit import AuditAction, AuditFilters, AuditRecord
from hivemind.ports import Authenticator, Credential, Store
from hivemind.services.audit import record_admin_action
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
        if existing is not None and existing.status is not AgentStatus.PENDING:
            raise ValueError(
                f"agent name already {existing.status.value} (name is reserved; pick a new one)"
            )
        return await self._store.register_agent(name, owner_alias)

    # -- admin-gated operations (ADR 0012) ----------------------------------

    async def activate(
        self,
        name: str,
        trust_level: TrustLevel,
        home_fleet_id: str,
        credential: Credential,
    ) -> tuple[Agent, str]:
        """Activate a pending or revoked agent: set trust level + home
        fleet and flip it to ``active``; issue its key **once** (returned
        here, never stored again — ADR 0012). Admin-gated. Returns (agent,
        raw_key). ``InvalidAgentStatus`` if it is already ``active``
        (ADR 0028: one key per agent). The status flips first and the key
        is issued last, so a failure in between leaves the agent active
        with no key — less privileged, never more."""
        self._require_admin(credential)
        agent = await self._store.activate_agent(
            name, trust_level=trust_level, home_fleet_id=home_fleet_id
        )
        key = await self._require_authenticator().issue_agent_key(name)
        await self._audit(
            credential,
            AuditAction.AGENT_ACTIVATE,
            name,
            {"trust_level": trust_level.value, "home_fleet_id": home_fleet_id},
        )
        return agent, key

    async def create_fleet(self, name: str, credential: Credential) -> Fleet:
        """Create a named fleet (admin-gated, ADR 0012)."""
        self._require_admin(credential)
        fleet = await self._store.create_fleet(name)
        await self._audit(credential, AuditAction.FLEET_CREATE, fleet.id, {"name": name})
        return fleet

    async def set_trust_level(self, name: str, level: TrustLevel, credential: Credential) -> Agent:
        """Promote/demote an agent's trust level (admin-gated, ADR 0011).
        Demotion to level 0 is *dormant* (key still valid, no access) —
        distinct from revocation (ADR 0012)."""
        self._require_admin(credential)
        before = await self._store.get_agent(name)
        agent = await self._store.set_agent_trust_level(name, level)
        await self._audit(
            credential,
            AuditAction.AGENT_TRUST_LEVEL_SET,
            name,
            {
                "from": before.trust_level.value if before is not None else None,
                "to": level.value,
            },
        )
        return agent

    async def set_home_fleet(self, name: str, fleet_id: str, credential: Credential) -> Agent:
        """Re-parent an agent to a new home fleet (admin-gated, ADR 0011).
        The agent's earlier ``fleet``-scoped entries stay in the fleet
        they were written into (never re-parented)."""
        self._require_admin(credential)
        before = await self._store.get_agent(name)
        agent = await self._store.set_agent_home_fleet(name, fleet_id)
        await self._audit(
            credential,
            AuditAction.AGENT_HOME_FLEET_SET,
            name,
            {"from": before.home_fleet_id if before is not None else None, "to": fleet_id},
        )
        return agent

    async def revoke(self, name: str, credential: Credential) -> None:
        """Revoke an agent (admin-gated, ADRs 0012, 0028): kill its key and
        set it ``revoked``. On a ``pending`` agent this rejects the
        registration. The record + name stay reserved. ``KeyError`` if
        unknown; ``InvalidAgentStatus`` if already ``revoked``. The key is
        deleted first, so a failure before the status flip leaves the
        agent keyless — less privileged, never more."""
        self._require_admin(credential)
        before = await self._store.get_agent(name)
        if before is None:
            raise KeyError(f"unknown agent: {name}")
        if before.status not in REVOCABLE:
            raise InvalidAgentStatus(name, before.status, "revoke")
        await self._require_authenticator().revoke_agent_key(name)
        await self._store.revoke_agent(name)
        await self._audit(credential, AuditAction.AGENT_REVOKE, name, {"from": before.status.value})

    async def rotate_org_key(self, credential: Credential) -> str:
        """Rotate the shared org key — the cluster kill switch (ADR 0012).
        All prior org keys stop working; the new key is returned once.
        Admin-gated."""
        self._require_admin(credential)
        key = await self._require_authenticator().rotate_org_key()
        await self._audit(credential, AuditAction.ORG_KEY_ROTATE, None)
        return key

    # -- read (admin-gated listing) ------------------------------------------

    async def list_agents(self, credential: Credential) -> list[Agent]:
        self._require_admin(credential)
        return await self._store.list_agents()

    async def list_fleets(self, credential: Credential) -> list[Fleet]:
        self._require_admin(credential)
        return await self._store.list_fleets()

    async def list_audit(
        self, credential: Credential, filters: AuditFilters, limit: int
    ) -> list[AuditRecord]:
        """The audit log, newest first (admin-gated, ADR 0027)."""
        self._require_admin(credential)
        return await self._store.list_audit(filters, limit)

    # -- audit (ADR 0027) ------------------------------------------------------

    async def _audit(
        self,
        credential: Credential,
        action: AuditAction,
        target: str | None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        """Record an admin action that has already succeeded. Deliberately
        takes no key: the raw key a key-producing action returns never
        reaches this method, so it cannot reach an audit column."""
        await record_admin_action(self._store, credential, action, target, detail)

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
            raise PermissionDenied("agent has no home fleet; cannot write scope 'fleet' (ADR 0011)")
        fleet_id = credential.home_fleet_id
    return WriteResolution(scope=scope, fleet_id=fleet_id)
