"""The audit log of admin-surface actions (ADR 0027, SPEC §12.5).

Pure data — no I/O. An ``AuditEvent`` is what a writer hands the
``Store`` port (``record_audit``); an ``AuditRecord`` is the stored row
(server-assigned ``id`` + ``occurred_at``). The log is **append-only**:
there is no update or delete, and ``target`` is a bare string (no
foreign key), so a revoked agent's history outlives its record.

Four actor kinds (ADR 0027, ADR 0046, ADR 0076):

* ``admin_key`` — the app admin surface (``AccessService`` /
  ``GovernanceService``). The actor is ``admin:<key fingerprint>``, a
  server-verified identity.
* ``org_key`` — a registration made with the shared org key (ADR 0046).
  The actor is ``org:<key fingerprint>``: verified as *an* org-key holder,
  which identifies no person (every agent shares that key).
* ``agent_key`` — a contributor or privileged agent acting inside its
  own fleet (a pin or unpin, ADRs 0076, 0096; the unpin a withdrawal
  triggers is the withdrawer's). The actor is the server-verified agent name.
* ``cli`` — the ``hivemind-keys`` bootstrap / break-glass CLI. The
  actor is the operator-supplied ``--actor`` (default: the OS user) and
  is **unverified** — anyone with database access can type anything.

No field of either type ever carries a raw API key: key-producing
actions record a key *fingerprint* (the first 12 hex chars of the
stored SHA-256 hash) or nothing (ADR 0027).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class ActorKind(StrEnum):
    """Who performed an audited action, and how far to trust the name."""

    ADMIN_KEY = "admin_key"  # the app admin surface: a verified admin key
    ORG_KEY = "org_key"  # a registration with the shared org key (ADR 0046)
    AGENT_KEY = "agent_key"  # a contributor's or privileged agent's own key: pin, unpin (ADR 0096)
    CLI = "cli"  # the hivemind-keys CLI: an unverified, operator-typed name


class AuditAction(StrEnum):
    """The audited action vocabulary (the ``audit_log_action_check``
    constraint, migrations ``0005``, ``0009``, ``0014``, ``0017``, ``0019``, ``0020``, ``0022``, ``0023``, ``0024``, ``0025``, ``0027``, ``0029``, ``0031`` and ``0032``)."""

    AGENT_REGISTER = "agent.register"  # ADR 0046
    AGENT_ACTIVATE = "agent.activate"
    AGENT_TRUST_LEVEL_SET = "agent.trust_level_set"
    AGENT_HOME_FLEET_SET = "agent.home_fleet_set"
    AGENT_OWNER_SET = "agent.owner_set"  # ADR 0122
    AGENT_REVOKE = "agent.revoke"
    FLEET_CREATE = "fleet.create"
    ORG_KEY_ROTATE = "org_key.rotate"
    ENTRY_WITHDRAW = "entry.withdraw"
    ADMIN_KEY_ISSUE = "admin_key.issue"
    ADMIN_KEY_REVOKE = "admin_key.revoke"
    AGENT_KEY_ISSUE = "agent_key.issue"
    ENTRY_PIN = "entry.pin"  # ADR 0076
    ENTRY_UNPIN = "entry.unpin"  # ADR 0076
    FLEET_MERGE = "fleet.merge"  # ADR 0107
    FLEET_UNMERGE = "fleet.unmerge"  # ADR 0107
    AGENT_KEY_REPLACE = "agent_key.replace"  # ADR 0108
    FLEET_RENAME = "fleet.rename"  # ADR 0109
    FLEET_MOVE_AGENTS = "fleet.move_agents"  # ADR 0112
    FLEET_MOVE_UNDO = "fleet.move_undo"  # ADR 0112
    AGENT_CLEANUP = "agent.cleanup"  # ADR 0111
    AGENT_CLEANUP_UNDO = "agent.cleanup_undo"  # ADR 0111
    AGENT_RETIRE = "agent.retire"  # ADR 0113
    AGENT_RETIRE_UNDO = "agent.retire_undo"  # ADR 0113
    AGENT_KEY_LOCK = "agent_key.lock"
    SCHEMA_ROLLBACK = "schema.rollback"  # ADR 0119
    ENTRY_REDACT = "entry.redact"  # ADR 0125
    ENTRY_RESTORE = "entry.restore"  # ADR 0124


# The key fingerprint is the first 12 hex chars of the stored SHA-256
# key hash — exactly what `hivemind-keys list` displays. Non-secret: it
# identifies a key without being usable as one.
KEY_FINGERPRINT_LENGTH = 12


def key_fingerprint(stored_key_hash: str) -> str:
    """The non-secret fingerprint of a key, from its stored SHA-256 hex."""
    return stored_key_hash[:KEY_FINGERPRINT_LENGTH]


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """An admin action to record (the write-side draft)."""

    actor_kind: ActorKind
    actor: str
    action: AuditAction
    target: str | None = None  # None only for org_key.rotate
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """A stored audit-log row."""

    id: str
    occurred_at: datetime
    actor_kind: ActorKind
    actor: str
    action: AuditAction
    target: str | None
    detail: Mapping[str, Any]


# The actions whose ``target`` is an agent's name (every other target is a
# fleet id, an entry id or a key fingerprint). A ``subjects`` match on the
# target is limited to these, so a fleet named like an agent is not that
# agent's history.
AGENT_TARGET_ACTIONS: tuple[AuditAction, ...] = (
    AuditAction.AGENT_REGISTER,
    AuditAction.AGENT_ACTIVATE,
    AuditAction.AGENT_TRUST_LEVEL_SET,
    AuditAction.AGENT_HOME_FLEET_SET,
    AuditAction.AGENT_OWNER_SET,
    AuditAction.AGENT_REVOKE,
    AuditAction.AGENT_KEY_ISSUE,
    AuditAction.AGENT_KEY_REPLACE,
    AuditAction.AGENT_KEY_LOCK,
    AuditAction.AGENT_CLEANUP,
    AuditAction.AGENT_CLEANUP_UNDO,
    AuditAction.AGENT_RETIRE,
    AuditAction.AGENT_RETIRE_UNDO,
)
# Retirements name the successor in ``detail.successor``.
SUCCESSOR_ACTIONS: tuple[AuditAction, ...] = (
    AuditAction.AGENT_RETIRE,
    AuditAction.AGENT_RETIRE_UNDO,
)


def _names_in(detail_value: Any, names: tuple[str, ...]) -> bool:
    return isinstance(detail_value, list) and any(
        isinstance(n, str) and n in names for n in detail_value
    )


@dataclass(frozen=True, slots=True)
class AuditFilters:
    """Read-side filters for the audit log (all optional, AND-ed).

    ``subjects`` (ADR 0123) keeps the rows *about* any of the named agents:
    the agent's own key as actor, an agent-named ``target`` (``AGENT_TARGET_ACTIONS``),
    a withdrawal or restore of its memory (``detail.author``), a fleet move that lists it
    (``detail.agents``) and a retirement that names it successor
    (``detail.successor``). The admin API passes one name; the agent portal
    passes the agent and the agents it inherits from."""

    actor: str | None = None
    action: AuditAction | None = None
    since: datetime | None = None  # inclusive lower bound on occurred_at
    target: str | None = None  # exact
    subjects: tuple[str, ...] = ()
    # Paging cursor (ADR 0028): a row id; only rows strictly older than it
    # (by occurred_at, then id) match. An unknown id matches nothing. It is
    # positional, so the store applies it — ``matches`` does not.
    before: str | None = None

    def matches(self, record: AuditRecord) -> bool:
        """Whether ``record`` passes every set filter."""
        if self.actor is not None and record.actor != self.actor:
            return False
        if self.action is not None and record.action is not self.action:
            return False
        if self.target is not None and record.target != self.target:
            return False
        if self.since is not None and record.occurred_at < self.since:
            return False
        return not self.subjects or self._about_a_subject(record)

    def _about_a_subject(self, record: AuditRecord) -> bool:
        names = self.subjects
        action = record.action
        detail = record.detail
        if record.actor_kind is ActorKind.AGENT_KEY and record.actor in names:
            return True
        if action in AGENT_TARGET_ACTIONS and record.target in names:
            return True
        if (
            action
            in (AuditAction.ENTRY_WITHDRAW, AuditAction.ENTRY_RESTORE, AuditAction.ENTRY_REDACT)
            and detail.get("author") in names
        ):
            return True
        if action is AuditAction.FLEET_MOVE_AGENTS and _names_in(detail.get("agents"), names):
            return True
        return action in SUCCESSOR_ACTIONS and detail.get("successor") in names
