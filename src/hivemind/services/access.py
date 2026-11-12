"""Access-control service (ADRs 0011-0012, SPEC §12): permission gates, registration, activation,
write scope.

Admin mutations are audited **after** they succeed (ADR 0027). The two writes share no transaction,
so an audit failure is raised, never swallowed. ``_audit`` takes no key, so a raw key cannot reach
an audit column.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from hivemind.domain.access import (
    REVOCABLE,
    SCOPE_FLEET,
    SCOPE_ORG,
    SCOPE_RANK,
    SCOPE_SELF,
    Agent,
    AgentNotActive,
    AgentStatus,
    Fleet,
    FleetMerged,
    InvalidAgentStatus,
    Standing,
    TrustLevel,
    UnknownAgent,
    UnknownFleet,
    UnknownSearchAgent,
    write_scopes_for,
)
from hivemind.domain.agent_role import AgentRole
from hivemind.domain.audit import AuditAction, AuditFilters, AuditRecord
from hivemind.domain.fleet_phase import FleetPhase, PhaseName
from hivemind.domain.validation import (
    RESERVED_AGENT_NAMES,  # noqa: F401  (re-exported: ADR 0033 names, ADR 0040 rules)
    InvalidInput,
    utcnow,
    validate_agent_name,
    validate_fleet_name,
    validate_owner_alias,
)
from hivemind.ports import Authenticator, Credential, Store
from hivemind.services.agent_cleanup import AgentCleanupService
from hivemind.services.agent_history import AgentHistoryService
from hivemind.services.audit import actor_kind_of, record_admin_action
from hivemind.services.errors import NameTaken as NameTaken  # re-exported: defined in errors
from hivemind.services.errors import PermissionDenied
from hivemind.services.fleet_merge import FleetMergeService
from hivemind.services.guards import require_admin
from hivemind.services.offboarding import OffboardingService
from hivemind.services.successions import SuccessionService

_NAME_TAKEN = "agent name is already taken (names are never reused; pick another name)"


@dataclass(frozen=True, slots=True)
class Registration:
    """The answer to a registration (ADR 0039), with a plain-language ``message`` for the caller."""

    agent: Agent
    already_registered: bool
    message: str


def _same_owner(stored: str | None, given: str | None) -> bool:
    """Alias equality: trimmed, case-insensitive; a missing alias equals only a missing alias."""

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
    """Registration, fleets and trust levels (ADRs 0011-0012). Registration needs the org or admin
    key; everything else the admin key."""

    def __init__(
        self,
        store: Store,
        authenticator: Authenticator | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._store = store
        self._authenticator = authenticator
        self._clock = clock
        self.merges = FleetMergeService(store)
        self.successions = SuccessionService(store, self._require_authenticator)
        self.offboarding = OffboardingService(store, self._require_authenticator)
        self.history = AgentHistoryService(store)
        self.cleanups = AgentCleanupService(store, revoke=self.revoke, revocable=self.revocable)
        # Called after an owner alias changes, to drop cached owner-level figures (ADR 0122).
        self.on_owner_change: list[Callable[[], None]] = []

    def _require_authenticator(self) -> Authenticator:
        """The authenticator for key management (absent in dev mode)."""
        if self._authenticator is None:
            raise PermissionDenied(
                "key management requires an authenticator (not configured in dev mode)"
            )
        return self._authenticator

    async def register(
        self,
        name: str,
        credential: Credential,
        owner_alias: str | None = None,
        *,
        org_only: bool = False,
    ) -> Registration:
        """Register (or re-register) an agent (ADRs 0012, 0039); ``org_only`` for MCP (SPEC §5.2).

        A new name is ``pending``. The same alias gets the current status; a different one raises
        ``NameTaken``, worded the same for every status so nothing leaks, so a key cannot reach a
        squatter.
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
        # A racing registrant won, or ``name`` is a case variant of an existing one (ADR 0045).
        if agent.name != name or not _same_owner(agent.owner_alias, owner_alias):
            raise NameTaken(_NAME_TAKEN)
        # Only new registrations are audited (ADR 0046); the alias is a claim, not a verified
        # identity.
        await self._audit(
            credential,
            AuditAction.AGENT_REGISTER,
            name,
            {"owner_alias": owner_alias} if owner_alias is not None else None,
        )
        return Registration(agent, False, _status_message(agent.status, new=True))

    async def _require_fleet(self, fleet_id: str) -> None:
        """A home-fleet reference must name a real fleet (SPEC §12.1), checked before any mutation.
        A merged fleet takes no members (ADR 0107): ``FleetMerged``."""
        fleet = await self._store.get_fleet(fleet_id)
        if fleet is None:
            raise UnknownFleet(f"unknown fleet: {fleet_id}")
        if fleet.merged_into is not None:
            target = await self._store.get_fleet(fleet.merged_into)
            raise FleetMerged(fleet.id, fleet.merged_into, target.name if target else None)

    async def activate(
        self,
        name: str,
        trust_level: TrustLevel,
        home_fleet_id: str,
        credential: Credential,
    ) -> tuple[Agent, str]:
        """Activate a pending or revoked agent and issue its key **once**: (agent, raw_key).

        The status flips first and the key is issued last, so a race or failure leaves the agent
        less privileged, never more (ADRs 0028, 0039). ``InvalidAgentStatus`` if already active.
        """
        require_admin(credential)
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

    async def create_agent(
        self,
        name: str,
        owner_alias: str,
        trust_level: TrustLevel,
        home_fleet_id: str,
        credential: Credential,
    ) -> tuple[Agent, str]:
        """Register and activate in one admin call (ADR 0120): (agent, raw_key), key shown **once**.

        Same two steps and audit rows as self-registration plus activation. Everything is checked
        before the record is created; an existing name (any status, any case) is ``NameTaken``. If
        activation fails, the agent stays ``pending``.
        """
        require_admin(credential)
        validate_agent_name(name)
        validate_owner_alias(owner_alias)
        if not owner_alias.strip():
            raise InvalidInput("owner_alias is required")
        self._require_authenticator()  # dev mode has no keys to issue: fail before any write
        await self._require_fleet(home_fleet_id)
        existing = await self._store.get_agent(name)
        if existing is not None:
            raise NameTaken(
                f"an agent named {existing.name!r} already exists ({existing.status.value}); "
                "activate it, re-activate it or replace its key instead, or pick another name"
            )
        registration = await self.register(name, credential, owner_alias)
        if registration.already_registered:
            # Registered by someone else between the check and the insert.
            raise NameTaken(_NAME_TAKEN)
        return await self.activate(name, trust_level, home_fleet_id, credential)

    async def replace_agent_key(self, name: str, credential: Credential) -> str:
        """Replace an active agent's key (ADR 0108): the new raw key, **once**. The old key stops
        at once; ``InvalidAgentStatus`` unless ``active``. The audit row has both fingerprints."""
        require_admin(credential)
        # The authenticator audits in the swap's transaction: no window with a new key and no row.
        replacement = await self._require_authenticator().replace_agent_key(
            name,
            audit_actor_kind=actor_kind_of(credential),
            audit_actor=credential.audit_actor(),
        )
        return replacement.raw_key

    async def lock_own_key(self, credential: Credential) -> bool:
        """Lock the calling agent's own key (the portal's "this key leaked"); the agent stays
        ``active``. An admin's key replacement (ADR 0108) clears the lock. Agent keys only; the
        audit row is written with the lock. ``False``, nothing changed, if the key was replaced
        or locked meanwhile."""
        if (
            credential.is_admin
            or credential.is_org
            or credential.agent_name is None
            or credential.key_id is None
        ):
            raise PermissionDenied("locking a key requires an agent key")
        return await self._require_authenticator().lock_agent_key(
            credential.agent_name, key_id=credential.key_id
        )

    async def create_fleet(self, name: str, credential: Credential) -> Fleet:
        """Create a named fleet."""
        require_admin(credential)
        validate_fleet_name(name)
        fleet = await self._store.create_fleet(name)
        await self._audit(credential, AuditAction.FLEET_CREATE, fleet.id, {"name": name})
        return fleet

    async def rename_fleet(self, fleet_id: str, name: str, credential: Credential) -> Fleet:
        """Rename a fleet, merged ones included (ADR 0109). The current name writes no audit row."""
        require_admin(credential)
        validate_fleet_name(name)
        before, after = await self._store.rename_fleet(fleet_id, name)
        if before.name != after.name:
            await self._audit(
                credential,
                AuditAction.FLEET_RENAME,
                after.id,
                {"from": before.name, "to": after.name},
            )
        return after

    def now(self) -> datetime:
        return self._clock()

    async def set_fleet_phase(
        self, fleet_id: str, name: PhaseName, until: datetime, credential: Credential
    ) -> Fleet:
        """Put a fleet in a phase until ``until``, replacing any phase it has (ADR 0143)."""
        require_admin(credential)
        phase = FleetPhase.starting(name, until, self._clock())
        _, fleet = await self._store.set_fleet_phase(fleet_id, phase)
        await self._audit(
            credential,
            AuditAction.FLEET_PHASE_SET,
            fleet.id,
            {"phase": phase.name.value, "until": phase.until.isoformat()},
        )
        return fleet

    async def clear_fleet_phase(self, fleet_id: str, credential: Credential) -> Fleet:
        """End a fleet's phase now (ADR 0143), audited with the phase it ended; a fleet with no
        phase in force changes nothing and writes no row."""
        require_admin(credential)
        before, fleet = await self._store.set_fleet_phase(fleet_id, None)
        ended = before.phase_in_force(self._clock())
        if ended is not None:
            await self._audit(
                credential,
                AuditAction.FLEET_PHASE_CLEAR,
                fleet.id,
                {"phase": ended.name.value, "until": ended.until.isoformat()},
            )
        return fleet

    async def set_trust_level(self, name: str, level: TrustLevel, credential: Credential) -> Agent:
        """Promote or demote a trust level (ADR 0011). Level 0 is *dormant* (key valid, no access),
        not revoked (ADR 0012)."""
        require_admin(credential)
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
        """Re-parent an agent to a new home fleet (ADR 0011); its earlier ``fleet`` entries stay
        in the fleet they were written into."""
        require_admin(credential)
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

    async def set_owner_alias(self, name: str, owner_alias: str, credential: Credential) -> Agent:
        """Correct an agent's owner alias, any status (ADR 0122). An unchanged value writes no
        audit row; a case-only change is a change."""
        require_admin(credential)
        validate_owner_alias(owner_alias)
        if not owner_alias.strip():
            raise InvalidInput("owner_alias must not be blank")
        before, agent = await self._store.set_agent_owner_alias(name, owner_alias)
        if before.owner_alias != owner_alias:
            await self._audit(
                credential,
                AuditAction.AGENT_OWNER_SET,
                name,
                {"from": before.owner_alias, "to": owner_alias},
            )
            for hook in self.on_owner_change:
                hook()
        return agent

    async def set_agent_role(self, name: str, role: AgentRole, credential: Credential) -> Agent:
        """Mark an agent with a role (ADR 0144); a role it already has changes and audits
        nothing. ``InvalidAgentStatus`` for a revoked agent."""
        require_admin(credential)
        return await self._change_role(name, role, credential)

    async def clear_agent_role(self, name: str, credential: Credential) -> Agent:
        """Take an agent's role away (ADR 0144); an agent with none changes and audits nothing."""
        require_admin(credential)
        return await self._change_role(name, None, credential)

    async def _change_role(
        self, name: str, role: AgentRole | None, credential: Credential
    ) -> Agent:
        before, agent = await self._store.set_agent_role(name, role)
        if role is not None and before.role is not role:
            await self._audit(credential, AuditAction.AGENT_ROLE_SET, name, {"role": role.value})
        elif role is None and before.role is not None:
            await self._audit(
                credential, AuditAction.AGENT_ROLE_CLEAR, name, {"role": before.role.value}
            )
        return agent

    async def update_agent(
        self,
        name: str,
        credential: Credential,
        *,
        trust_level: TrustLevel | None = None,
        home_fleet_id: str | None = None,
        owner_alias: str | None = None,
    ) -> Agent:
        """``PATCH /v1/admin/agents/{name}``: trust level, home fleet and/or owner alias, each
        audited on its own. The fleet and alias are checked first, so a refused one changes
        nothing."""
        require_admin(credential)
        if owner_alias is not None:
            validate_owner_alias(owner_alias)
            if not owner_alias.strip():
                raise InvalidInput("owner_alias must not be blank")
        if home_fleet_id is not None:
            await self._require_fleet(home_fleet_id)
        agent: Agent | None = None
        if trust_level is not None:
            agent = await self.set_trust_level(name, trust_level, credential)
        if home_fleet_id is not None:
            agent = await self.set_home_fleet(name, home_fleet_id, credential)
        if owner_alias is not None:
            agent = await self.set_owner_alias(name, owner_alias, credential)
        if agent is None:
            agent = await self._store.get_agent(name)
            if agent is None:
                raise UnknownAgent(f"unknown agent: {name}")
        return agent

    async def revoke(self, name: str, credential: Credential) -> None:
        """Revoke an agent (ADRs 0012, 0028): status first (its key stops authenticating, ADR 0039),
        then the key row. A pending agent's registration is rejected; the name stays reserved."""
        require_admin(credential)
        before = await self._store.get_agent(name)
        if before is None:
            raise UnknownAgent(f"unknown agent: {name}")
        if before.status not in REVOCABLE:
            raise InvalidAgentStatus(name, before.status, "revoke")
        authenticator = self._require_authenticator()
        await self._store.revoke_agent(name)
        await authenticator.revoke_agent_key(name)
        await self._audit(credential, AuditAction.AGENT_REVOKE, name, {"from": before.status.value})

    async def revocable(self, name: str, credential: Credential) -> bool:
        """Whether ``revoke`` would revoke ``name`` now. Changes nothing, so a caller can refuse
        before changing anything else."""
        require_admin(credential)
        agent = await self._store.get_agent(name)
        if agent is None:
            raise UnknownAgent(f"unknown agent: {name}")
        if agent.status not in REVOCABLE:
            return False
        self._require_authenticator()
        return True

    async def rotate_org_key(self, credential: Credential) -> str:
        """Rotate the shared org key (ADR 0031): prior org keys stop; the new key is returned
        once."""
        require_admin(credential)
        key = await self._require_authenticator().rotate_org_key()
        await self._audit(credential, AuditAction.ORG_KEY_ROTATE, None)
        return key

    async def list_agents(self, credential: Credential) -> list[Agent]:
        require_admin(credential)
        return await self._store.list_agents()

    async def list_fleets(
        self, credential: Credential, *, include_merged: bool = False
    ) -> list[Fleet]:
        """Fleets; merged ones only with ``include_merged`` (ADR 0107)."""
        require_admin(credential)
        fleets = await self._store.list_fleets()
        return fleets if include_merged else [f for f in fleets if f.merged_into is None]

    async def list_audit(
        self, credential: Credential, filters: AuditFilters, limit: int
    ) -> list[AuditRecord]:
        """The audit log, newest first (ADR 0027)."""
        require_admin(credential)
        return await self._store.list_audit(filters, limit)

    async def credential_for_agent(self, credential: Credential, name: str) -> Credential:
        """The credential an active agent searches with, for the admin's "search as" (ADR 0136):
        ``UnknownSearchAgent`` or ``AgentNotActive`` when it has none. Never carries key material."""
        require_admin(credential)
        agent_credential = await self._require_authenticator().credential_for_agent(name)
        if agent_credential is not None:
            return agent_credential
        agent = await self._store.get_agent(name)
        if agent is None:
            raise UnknownSearchAgent(f"unknown agent: {name}")
        raise AgentNotActive(name, agent.status)

    async def whoami(self, credential: Credential) -> Standing:
        """What the calling key is and may do (``hive_whoami``, ADR 0030), derived from the rules
        the data plane enforces so it cannot drift."""
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
        fleet = (
            await self._store.get_fleet(credential.home_fleet_id)
            if credential.home_fleet_id is not None
            else None
        )
        key_last_used = None
        if agent is not None:
            key_last_used = await self._store.agent_key_last_used(agent.name)

        return Standing(
            key_kind=key_kind,
            name=credential.agent_name or credential.user_id,
            status=agent.status if agent is not None else None,
            trust_level=credential.trust_level,
            home_fleet_id=credential.home_fleet_id,
            home_fleet_name=fleet.name if fleet is not None else None,
            can_read=_readable(key_kind, credential),
            can_write_scopes=_writable_scopes(key_kind, credential),
            inherits_from=credential.inherits_from if key_kind == "agent" else (),
            key_last_used_at=key_last_used,
            fleet_phase=fleet.phase_in_force(self._clock()) if fleet is not None else None,
            role=agent.role if agent is not None else None,
        )

    async def home_fleet_phase(
        self, credential: Credential, fleet_id: str | None = None
    ) -> FleetPhase | None:
        """The phase in force of the fleet ``hive_pinned`` shows (ADR 0143), which is the caller's
        home fleet unless ``fleet_id`` names another. The hint speaks to the caller's own fleet, so
        another fleet's phase is not told."""
        home = credential.home_fleet_id
        if home is None or fleet_id not in (None, home):
            return None
        fleet = await self._store.get_fleet(home)
        return fleet.phase_in_force(self._clock()) if fleet is not None else None

    async def _audit(
        self,
        credential: Credential,
        action: AuditAction,
        target: str | None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        """Record a succeeded admin action. Takes no key on purpose."""
        await record_admin_action(self._store, credential, action, target, detail)

    def _require_org_or_admin(self, credential: Credential) -> None:
        if not (credential.is_org or credential.is_admin):
            raise PermissionDenied("registration requires an org or admin key (ADR 0012)")

    def _require_org(self, credential: Credential) -> None:
        if not credential.is_org:
            raise PermissionDenied("hive_register is gated on the org key (SPEC §5.2)")


@dataclass(frozen=True, slots=True)
class WriteResolution:
    """The resolved write scope and fleet (ADR 0011)."""

    scope: str
    fleet_id: str | None


def _readable(key_kind: str, credential: Credential) -> tuple[str, ...]:
    """``can_read`` for ``whoami``; mirrors ``entry_is_visible``."""
    if key_kind in ("admin", "legacy"):
        return ("everything",)
    if key_kind == "org" or credential.trust_level is TrustLevel.UNTRUSTED:
        return ()
    fleets = "all_fleets" if credential.trust_level is TrustLevel.PRIVILEGED else "home_fleet"
    return ("own", fleets, "org")


def _writable_scopes(key_kind: str, credential: Credential) -> tuple[str, ...]:
    """``can_write_scopes`` for ``whoami``: up to ``max_write_scope``, minus ``fleet`` where
    ``resolve_write_scope`` would reject it."""
    if key_kind == "org" or not credential.can_write():
        return ()
    if credential.is_admin or not credential.access_controlled:
        # Off-ladder access (admin / legacy / dev-mode): every scope.
        scopes: tuple[str, ...] = (SCOPE_SELF, SCOPE_FLEET, SCOPE_ORG)
    else:
        scopes = write_scopes_for(credential.trust_level)
    if (key_kind == "agent" and credential.home_fleet_id is None) or key_kind == "admin":
        scopes = tuple(scope for scope in scopes if scope != SCOPE_FLEET)
    return scopes


def resolve_write_scope(
    credential: Credential, requested_scope: str | None = None
) -> WriteResolution:
    """Resolve and validate the write scope (ADR 0011). Omitted gives the level's maximum (L1
    ``self``, L2/L3 ``fleet``, admin ``org``); an explicit scope may not exceed it. ``fleet`` needs
    a home fleet, so the admin key may not write it (ADR 0060)."""
    if not credential.can_write():
        raise PermissionDenied("level 0 (untrusted) agents may not write (ADR 0011)")
    max_scope = credential.max_write_scope()
    if requested_scope is None:
        scope = max_scope
    else:
        scope = requested_scope
        if SCOPE_RANK.get(scope, 99) > SCOPE_RANK.get(max_scope, 99):
            raise PermissionDenied(
                f"trust level {credential.trust_level.value} may not write "
                f"scope '{scope}' (ADR 0011)"
            )
    if scope == SCOPE_FLEET and credential.is_admin:
        # The admin key has no fleet: a fleet entry would reach no fleet (ADR 0060).
        raise PermissionDenied(
            "the admin key writes 'self' or 'org' entries, not 'fleet' (ADR 0060)"
        )
    fleet_id: str | None = None
    if scope == SCOPE_FLEET and credential.access_controlled and not credential.is_admin:
        # The entry lands in the home fleet; legacy and admin are unbound.
        if credential.home_fleet_id is None:
            raise PermissionDenied("agent has no home fleet; cannot write scope 'fleet' (ADR 0011)")
        fleet_id = credential.home_fleet_id
    return WriteResolution(scope=scope, fleet_id=fleet_id)
