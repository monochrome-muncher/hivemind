"""The audit log of admin-surface actions (ADR 0027, SPEC §12.5).

Pure data. The log is append-only and ``target`` is a bare string (no foreign key), so a
revoked agent's history outlives its record. No field ever carries a raw API key: key
actions record a fingerprint or nothing.

Actor kinds (ADRs 0027, 0046, 0076): ``admin_key`` (actor ``admin:<fingerprint>``, verified),
``org_key`` (``org:<fingerprint>``, identifies no person: every agent shares the key),
``agent_key`` (the verified agent name: a pin, unpin, or the unpin a withdrawal triggers) and
``cli`` (the operator-typed ``--actor``, **unverified**).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class ActorKind(StrEnum):
    """Who performed an audited action, and how far to trust the name."""

    ADMIN_KEY = "admin_key"
    ORG_KEY = "org_key"
    AGENT_KEY = "agent_key"
    CLI = "cli"


class AuditAction(StrEnum):
    """The audited action vocabulary (the ``audit_log_action_check``
    constraint, migrations ``0005``, ``0009``, ``0014``, ``0017``, ``0019``, ``0020``, ``0022``, ``0023``, ``0024``, ``0025``, ``0027``, ``0029``, ``0031``, ``0032``, ``0033`` and ``0034``)."""

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
    FLEET_PHASE_SET = "fleet.phase_set"
    FLEET_PHASE_CLEAR = "fleet.phase_clear"
    AGENT_ROLE_SET = "agent.role_set"
    AGENT_ROLE_CLEAR = "agent.role_clear"


# First 12 hex chars of the stored SHA-256 key hash, as `hivemind-keys list` shows. Non-secret.
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


# Actions whose ``target`` is an agent name; a ``subjects`` match on the target is limited
# to these, so a fleet named like an agent is not that agent's history.
AGENT_TARGET_ACTIONS: tuple[AuditAction, ...] = (
    AuditAction.AGENT_REGISTER,
    AuditAction.AGENT_ACTIVATE,
    AuditAction.AGENT_TRUST_LEVEL_SET,
    AuditAction.AGENT_HOME_FLEET_SET,
    AuditAction.AGENT_OWNER_SET,
    AuditAction.AGENT_ROLE_SET,
    AuditAction.AGENT_ROLE_CLEAR,
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

    ``subjects`` (ADR 0123) keeps rows *about* any named agent: as actor, as ``target``
    (``AGENT_TARGET_ACTIONS``), as ``detail.author``, in ``detail.agents`` of a fleet move
    or as ``detail.successor``."""

    actor: str | None = None
    action: AuditAction | None = None
    since: datetime | None = None  # inclusive lower bound on occurred_at
    target: str | None = None  # exact
    subjects: tuple[str, ...] = ()
    # Paging cursor (ADR 0028): rows strictly older than this id (occurred_at, then id); an
    # unknown id matches nothing. Positional, so the store applies it, not ``matches``.
    before: str | None = None

    def matches(self, record: AuditRecord) -> bool:
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
