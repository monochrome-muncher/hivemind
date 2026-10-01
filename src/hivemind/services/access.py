"""Access-control service (ADRs 0011-0012, SPEC §12).

Permission gating (ADR 0012), registration and activation (the key is
issued **once**), and write-scope resolution (ADR 0011).

Every admin mutation is audited **after** it succeeds (ADR 0027). The
two writes share no transaction, so an audit failure is raised, never
swallowed: the caller must see the action went unaudited. ``_audit``
takes no key argument, so a raw key cannot reach an audit column.
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
    Standing,
    TrustLevel,
)
from hivemind.domain.audit import AuditAction, AuditFilters, AuditRecord
from hivemind.domain.validation import (
    RESERVED_AGENT_NAMES,  # noqa: F401  (re-exported: ADR 0033 names, ADR 0040 rules)
    validate_agent_name,
    validate_fleet_name,
    validate_owner_alias,
)
from hivemind.ports import Authenticator, Credential, Store
from hivemind.services.audit import record_admin_action
from hivemind.services.governance import PermissionDenied

_NAME_TAKEN = "agent name is already taken (names are never reused; pick another name)"


class NameTaken(ValueError):
    """The name belongs to another registration (ADR 0039). Deliberately
    says nothing about that registration (its status, its owner)."""


@dataclass(frozen=True, slots=True)
class Registration:
    """The answer to a registration (ADR 0039): the agent record, whether
    the caller had already registered this name (same alias), and a
    plain-language ``message`` saying what the status means for the caller."""

    agent: Agent
    already_registered: bool
    message: str


def _same_owner(stored: str | None, given: str | None) -> bool:
    """Alias equality for re-registration: trimmed, case-insensitive, and a
    missing alias equals only a missing alias."""

    def norm(alias: str | None) -> str | None:
        alias = alias.strip().casefold() if alias is not None else None
        return alias or None

    return norm(stored) == norm(given)


def _status_message(status: AgentStatus, *, new: bool = False) -> str:
    if status is AgentStatus.PENDING:
        if new:
            return (
                "registered: pending admin activation. The agent key is issued once, "
                "at activation, and delivered to you by your admin - not by this call."
            )
        return (
            "already registered and still pending admin activation. Do not register again; "
            "wait for your admin to activate it and hand you the agent key."
        )
    if status is AgentStatus.ACTIVE:
        return (
            "already registered and active: ask your admin for the agent key "
            "(it is issued once, at activation). Registering again does nothing."
        )
    return (
        "this registration was revoked (rejected) by an admin. The name stays reserved: "
        "ask your admin, or register under a different name."
    )


class AccessService:
    """Registration + fleet + trust-level management (ADRs 0011-0012).

    Every method enforces the permission gate (ADR 0012): registration
    needs the org or admin key; everything else needs the admin key.
    """

    def __init__(self, store: Store, authenticator: Authenticator | None = None) -> None:
        self._store = store
        self._authenticator = authenticator

    def _require_authenticator(self) -> Authenticator:
        """The authenticator for key management (absent in dev mode)."""
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
    ) -> Registration:
        """Register (or re-register) an agent (ADR 0012, refined by ADR 0039).

        Org or admin key (REST, SPEC §5.1); ``org_only=True`` for MCP
        ``hive_register`` (SPEC §5.2). A new name becomes ``pending``.
        Re-registering by the same alias returns the current status; by a
        different alias raises ``NameTaken``, worded identically for every
        status so nothing leaks. The first alias is never overwritten, so
        a key cannot be delivered to a squatter.
        """
        if org_only:
            self._require_org(credential)
        else:
            self._require_org_or_admin(credential)
        validate_agent_name(name)  # ADR 0040: format + reserved names
        validate_owner_alias(owner_alias)
        existing = await self._store.get_agent(name)
        if existing is not None:
            if not _same_owner(existing.owner_alias, owner_alias):
                raise NameTaken(_NAME_TAKEN)
            return Registration(existing, True, _status_message(existing.status))
        agent = await self._store.register_agent(name, owner_alias)
        # A racing registrant may have won, or ``name`` is a case variant of
        # an existing agent's, refused whoever owns it (ADR 0045).
        if agent.name != name or not _same_owner(agent.owner_alias, owner_alias):
            raise NameTaken(_NAME_TAKEN)
        # Only new registrations are audited (ADR 0046); the alias is the
        # registrant's claim, not a verified identity.
        await self._audit(
            credential,
            AuditAction.AGENT_REGISTER,
            name,
            {"owner_alias": owner_alias} if owner_alias is not None else None,
        )
        return Registration(agent, False, _status_message(agent.status, new=True))

    # -- admin-gated operations (ADR 0012) ----------------------------------

    async def _require_fleet(self, fleet_id: str) -> None:
        """A home-fleet reference must name a real fleet (SPEC §12.1);
        checked before any mutation so a typo is a typed 404."""
        if await self._store.get_fleet(fleet_id) is None:
            raise KeyError(f"unknown fleet: {fleet_id}")

    async def activate(
        self,
        name: str,
        trust_level: TrustLevel,
        home_fleet_id: str,
        credential: Credential,
    ) -> tuple[Agent, str]:
        """Activate a pending or revoked agent and issue its key **once**;
        returns (agent, raw_key). ``InvalidAgentStatus`` if already active
        (ADR 0028). The status flips first and the key is issued last,
        only while still ``active`` (ADR 0039), so a race or failure
        leaves the agent less privileged, never more."""
        self._require_admin(credential)
        await self._require_fleet(home_fleet_id)
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
        validate_fleet_name(name)  # ADR 0040
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
        await self._require_fleet(fleet_id)
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
        """Revoke an agent (ADRs 0012, 0028): status first (its key stops
        authenticating, ADR 0039), then delete the key row. On a pending
        agent this rejects the registration; the name stays reserved.
        ``KeyError`` if unknown; ``InvalidAgentStatus`` if already revoked."""
        self._require_admin(credential)
        before = await self._store.get_agent(name)
        if before is None:
            raise KeyError(f"unknown agent: {name}")
        if before.status not in REVOCABLE:
            raise InvalidAgentStatus(name, before.status, "revoke")
        authenticator = self._require_authenticator()
        await self._store.revoke_agent(name)
        await authenticator.revoke_agent_key(name)
        await self._audit(credential, AuditAction.AGENT_REVOKE, name, {"from": before.status.value})

    async def rotate_org_key(self, credential: Credential) -> str:
        """Rotate the shared org key (ADR 0031): every prior org key stops
        working, active agents are unaffected; the new key is returned once."""
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

    # -- the caller's own standing (ADR 0030) -------------------------------

    async def whoami(self, credential: Credential) -> Standing:
        """What the calling key is and may do (``hive_whoami``, ADR 0030).

        Any valid key may ask. Derived from the rules the data plane
        enforces, so the answer cannot drift from actual behaviour."""
        if credential.is_admin:
            key_kind = "admin"
        elif credential.is_org:
            key_kind = "org"
        elif not credential.access_controlled:
            key_kind = "legacy"
        else:
            key_kind = "agent"

        agent = None
        if key_kind == "agent" and credential.agent_name is not None:
            agent = await self._store.get_agent(credential.agent_name)
        fleet_name = None
        if credential.home_fleet_id is not None:
            fleet = await self._store.get_fleet(credential.home_fleet_id)
            fleet_name = fleet.name if fleet is not None else None

        return Standing(
            key_kind=key_kind,
            name=credential.agent_name or credential.user_id,
            status=agent.status if agent is not None else None,
            trust_level=credential.trust_level,
            home_fleet_id=credential.home_fleet_id,
            home_fleet_name=fleet_name,
            can_read=_readable(key_kind, credential),
            can_write_scopes=_writable_scopes(key_kind, credential),
        )

    # -- audit (ADR 0027) ------------------------------------------------------

    async def _audit(
        self,
        credential: Credential,
        action: AuditAction,
        target: str | None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        """Record a succeeded admin action. Takes no key on purpose."""
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


def _readable(key_kind: str, credential: Credential) -> tuple[str, ...]:
    """``can_read`` for ``whoami`` (ADR 0030) — mirrors ``entry_is_visible``."""
    if key_kind in ("admin", "legacy"):
        return ("everything",)
    if key_kind == "org" or credential.trust_level is TrustLevel.UNTRUSTED:
        return ()
    fleets = "all_fleets" if credential.trust_level is TrustLevel.PRIVILEGED else "home_fleet"
    return ("own", fleets, "org")


def _writable_scopes(key_kind: str, credential: Credential) -> tuple[str, ...]:
    """``can_write_scopes`` for ``whoami`` (ADR 0030) — every scope up to
    ``max_write_scope``, minus ``fleet`` for an agent with no home fleet
    (``resolve_write_scope`` would reject it)."""
    if key_kind == "org" or not credential.can_write():
        return ()
    max_rank = _SCOPE_RANK[credential.max_write_scope()]
    scopes = tuple(scope for scope, rank in _SCOPE_RANK.items() if rank <= max_rank)
    if key_kind == "agent" and credential.home_fleet_id is None:
        scopes = tuple(scope for scope in scopes if scope != "fleet")
    return scopes


def resolve_write_scope(
    credential: Credential, requested_scope: str | None = None
) -> WriteResolution:
    """Resolve + validate the write scope for this credential (ADR 0011).

    Omitted → the level's maximum (L1 ``self``, L2/L3 ``fleet``). An
    explicit scope may not exceed it; level 0 may not write; ``fleet``
    needs a home fleet. ``PermissionDenied`` otherwise.
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
        # The entry lands in the home fleet; legacy and admin are unbound.
        if credential.home_fleet_id is None:
            raise PermissionDenied("agent has no home fleet; cannot write scope 'fleet' (ADR 0011)")
        fleet_id = credential.home_fleet_id
    return WriteResolution(scope=scope, fleet_id=fleet_id)
