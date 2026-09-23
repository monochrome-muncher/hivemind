"""The audit log of admin-surface actions (ADR 0027, SPEC §12.5).

Pure data — no I/O. An ``AuditEvent`` is what a writer hands the
``Store`` port (``record_audit``); an ``AuditRecord`` is the stored row
(server-assigned ``id`` + ``occurred_at``). The log is **append-only**:
there is no update or delete, and ``target`` is a bare string (no
foreign key), so a revoked agent's history outlives its record.

Two actor kinds (ADR 0027):

* ``admin_key`` — the app admin surface (``AccessService`` /
  ``GovernanceService``). The actor is ``admin:<key fingerprint>``, a
  server-verified identity.
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
    CLI = "cli"  # the hivemind-keys CLI: an unverified, operator-typed name


class AuditAction(StrEnum):
    """The audited action vocabulary (the ``audit_log_action_check``
    constraint, migration ``0005``)."""

    AGENT_ACTIVATE = "agent.activate"
    AGENT_TRUST_LEVEL_SET = "agent.trust_level_set"
    AGENT_HOME_FLEET_SET = "agent.home_fleet_set"
    AGENT_REVOKE = "agent.revoke"
    FLEET_CREATE = "fleet.create"
    ORG_KEY_ROTATE = "org_key.rotate"
    ENTRY_WITHDRAW = "entry.withdraw"
    ADMIN_KEY_ISSUE = "admin_key.issue"
    ADMIN_KEY_REVOKE = "admin_key.revoke"
    AGENT_KEY_ISSUE = "agent_key.issue"


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


@dataclass(frozen=True, slots=True)
class AuditFilters:
    """Read-side filters for the audit log (all optional, AND-ed)."""

    actor: str | None = None
    action: AuditAction | None = None
    since: datetime | None = None  # inclusive lower bound on occurred_at

    def matches(self, record: AuditRecord) -> bool:
        """Whether ``record`` passes every set filter."""
        if self.actor is not None and record.actor != self.actor:
            return False
        if self.action is not None and record.action is not self.action:
            return False
        return not (self.since is not None and record.occurred_at < self.since)
