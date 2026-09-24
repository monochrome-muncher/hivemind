"""Issue, rotate, and revoke API keys for the credential model (ADRs 0008, 0012).

Console entry point (``hivemind-keys``). Raw keys are random 32-byte
secrets printed **once** at issuance; only their SHA-256 hash is stored
in the ``credentials`` table (a leaked database never leaks usable keys
— SPEC.md §8.1).

The v2 key model (ADR 0012) reduces the keys to three:

* **org key** — one shared key; gates registration + health only.
  Rotated via ``rotate-org`` (the cluster kill switch).
* **agent key** — one per registered agent, issued under its registered
  name; the agent's trust level + home fleet ride on the key (ADR 0011).
* **admin key** — full access (fleet/level management, org-key rotation).

``user`` keys are retired: agents are first-class and register under a
name; their key is issued at activation.

Every mutating command (all but ``list``) appends a ``cli`` row to the
audit log in the same transaction as the change (ADR 0027). The row's
actor is ``--actor`` (default: the OS user) and is **unverified** —
anyone with database access can type any name — which is why its
``actor_kind`` is ``cli``, never ``admin_key``. No raw key is recorded:
admin keys appear by their 12-char fingerprint.

Usage:
    hivemind-keys [--actor NAME] issue-admin
    hivemind-keys issue-agent --name alice
    hivemind-keys rotate-org
    hivemind-keys revoke --name alice
    hivemind-keys revoke-admin --key hm_xxx   (or --hash <sha256 from list>)
    hivemind-keys list
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import secrets
import sys

import asyncpg

from hivemind.config import load_settings
from hivemind.domain.audit import ActorKind, AuditAction, key_fingerprint
from hivemind.store.auth import key_hash

_ISSUE_ADMIN = "INSERT INTO credentials (key_hash, kind, user_id) VALUES ($1, 'admin', $2)"
_ISSUE_AGENT = (
    "INSERT INTO credentials (key_hash, kind, user_id, agent_id, agent_name) "
    "VALUES ($1, 'agent', $2, $2, $2)"
)
_ISSUE_ORG = "INSERT INTO credentials (key_hash, kind, user_id) VALUES ($1, 'org', $2)"
_DELETE_ORG = "DELETE FROM credentials WHERE kind = 'org'"
_LIST = "SELECT key_hash, kind, user_id, agent_name FROM credentials ORDER BY created_at"
_REVOKE_AGENT = "DELETE FROM credentials WHERE kind = 'agent' AND agent_name = $1"
_REVOKE_ADMIN = "DELETE FROM credentials WHERE kind = 'admin' AND key_hash = $1"
_INSERT_AUDIT = (
    "INSERT INTO audit_log (actor_kind, actor, action, target, detail) "
    "VALUES ($1, $2, $3, $4, $5::jsonb)"
)
_HAS_AGENT_KEY = "SELECT 1 FROM credentials WHERE kind = 'agent' AND agent_name = $1"
_AGENT_STATUS_FOR_UPDATE = "SELECT status FROM agents WHERE name = $1 FOR UPDATE"
_REACTIVATE_REVOKED = "UPDATE agents SET status = 'active' WHERE name = $1 AND status = 'revoked'"
_SET_REVOKED = "UPDATE agents SET status = 'revoked' WHERE name = $1"


class AgentKeyExists(Exception):
    """``issue-agent`` refused: the agent already holds a key (ADR 0028 —
    one key per agent; revoke first to replace it)."""


def _raw_key() -> str:
    """A fresh random 32-byte API key (the raw secret, printed once)."""
    return "hm_" + secrets.token_hex(32)


def default_actor() -> str:
    """The ``--actor`` default: the OS user running the CLI (ADR 0027).
    Unverified — it is only as honest as whoever runs the command.

    A container running under an arbitrary uid with no passwd entry and
    no ``USER``/``LOGNAME`` makes ``getpass.getuser`` raise; the CLI must
    still work there (it is the break-glass tool), so fall back to the
    numeric uid rather than fail."""
    try:
        return getpass.getuser()
    except OSError, KeyError:
        return f"uid:{os.getuid()}"


async def _audit(
    conn: asyncpg.Connection,
    actor: str,
    action: AuditAction,
    target: str | None,
    detail: dict[str, str] | None = None,
) -> None:
    """Append a ``cli`` audit row on ``conn`` — called inside the same
    transaction as the mutation, so the two commit or roll back together
    (ADR 0027). Deliberately takes no key: ``target`` is an agent name or
    a key *fingerprint*, so a raw key cannot reach an audit column."""
    await conn.execute(
        _INSERT_AUDIT,
        ActorKind.CLI.value,
        actor,
        action.value,
        target,
        json.dumps(detail or {}),
    )


async def _issue_admin(dsn: str, *, actor: str | None = None) -> str:
    """Issue an admin key; return the raw secret (printed once). Audited
    as ``admin_key.issue`` with the new key's fingerprint as target."""
    raw_key = _raw_key()
    stored_hash = key_hash(raw_key)
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            await conn.execute(_ISSUE_ADMIN, stored_hash, "admin")
            await _audit(
                conn,
                actor or default_actor(),
                AuditAction.ADMIN_KEY_ISSUE,
                key_fingerprint(stored_hash),
            )
    finally:
        await conn.close()
    return raw_key


async def _issue_agent(dsn: str, name: str, *, actor: str | None = None) -> str:
    """Issue an agent key bound to the registered agent name (ADR 0012).
    Audited as ``agent_key.issue`` with the agent name as target.

    Refuses (``AgentKeyExists``) when the agent already holds a key —
    one key per agent (ADR 0028). A ``revoked`` agent flips back to
    ``active``, so status and key never disagree after a CLI run."""
    raw_key = _raw_key()
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            await conn.execute(_AGENT_STATUS_FOR_UPDATE, name)
            if await conn.fetchval(_HAS_AGENT_KEY, name) is not None:
                raise AgentKeyExists(name)
            await conn.execute(_ISSUE_AGENT, key_hash(raw_key), name)
            await conn.execute(_REACTIVATE_REVOKED, name)
            await _audit(conn, actor or default_actor(), AuditAction.AGENT_KEY_ISSUE, name)
    finally:
        await conn.close()
    return raw_key


async def _rotate_org(dsn: str, *, actor: str | None = None) -> str:
    """Rotate the shared org key (the cluster kill switch, ADR 0012).
    Audited as ``org_key.rotate`` (no target)."""
    raw_key = _raw_key()
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            await conn.execute(_DELETE_ORG)
            await conn.execute(_ISSUE_ORG, key_hash(raw_key), "org")
            await _audit(conn, actor or default_actor(), AuditAction.ORG_KEY_ROTATE, None)
    finally:
        await conn.close()
    return raw_key


async def _list(dsn: str) -> None:
    """List credential hashes (never raw keys)."""
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(_LIST)
    finally:
        await conn.close()
    if not rows:
        print("(no credentials)")
        return
    for row in rows:
        name = row["agent_name"] or row["user_id"] or "-"
        print(f"{key_fingerprint(row['key_hash'])}…  {row['kind']:<6} {name}")


async def _revoke(dsn: str, name: str, *, actor: str | None = None) -> None:
    """Retire an agent's key and set it ``revoked`` (the name stays
    reserved, ADRs 0012, 0028). Audited as ``agent.revoke`` with the
    agent name as target and the prior status as ``detail.from`` (when
    the agent has a record)."""
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            prior = await conn.fetchval(_AGENT_STATUS_FOR_UPDATE, name)
            await conn.execute(_REVOKE_AGENT, name)
            await conn.execute(_SET_REVOKED, name)
            detail = {"from": prior} if prior is not None else None
            await _audit(conn, actor or default_actor(), AuditAction.AGENT_REVOKE, name, detail)
    finally:
        await conn.close()


async def _revoke_admin(
    dsn: str,
    *,
    raw_key: str | None = None,
    stored_hash: str | None = None,
    actor: str | None = None,
) -> bool:
    """Revoke a specific admin key — the second half of admin-key rotation.

    Targets the admin row by either the raw secret (SHA-256 hashed
    first, SPEC §8.1) or the stored SHA-256 hex that ``hivemind-keys
    list`` prints. Returns ``True`` if an admin row was deleted, or
    ``False`` if nothing matched (the CLI then exits non-zero so the
    no-op is loud from a script — DEPLOY.md §5).

    A successful revocation is audited as ``admin_key.revoke`` with the
    key's **fingerprint** as target — whichever form identified it (a
    raw key is normalised to its hash first, so it never reaches the
    audit row). A no-op records nothing."""
    if stored_hash is not None:
        target = stored_hash
    elif raw_key is not None:
        target = key_hash(raw_key)  # the guard above proves it is set
    else:
        raise ValueError("either raw_key or stored_hash must be provided")
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            status = await conn.execute(_REVOKE_ADMIN, target)
            revoked = str(status) == "DELETE 1"
            if revoked:
                await _audit(
                    conn,
                    actor or default_actor(),
                    AuditAction.ADMIN_KEY_REVOKE,
                    key_fingerprint(target),
                )
    finally:
        await conn.close()
    return revoked


def main() -> None:
    """Console entry point (``hivemind-keys``)."""
    parser = argparse.ArgumentParser(description="Hivemind key management (ADR 0012)")
    parser.add_argument(
        "--actor",
        default=None,
        help=(
            "who to record in the audit log for a mutating command (default: the OS "
            "user). UNVERIFIED: it is whatever you type, so audit rows from this CLI "
            "are marked actor_kind=cli, never admin_key (ADR 0027)"
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("issue-admin", help="issue a new admin key")

    issue_agent = sub.add_parser("issue-agent", help="issue an agent key for a registered agent")
    issue_agent.add_argument("--name", required=True)

    sub.add_parser("rotate-org", help="rotate the shared org key (cluster kill switch)")

    sub.add_parser("list", help="list credential hashes (never raw keys)")

    revoke_admin = sub.add_parser(
        "revoke-admin",
        help="revoke a specific admin key (rotation: issue a new one, then revoke the old)",
    )
    revoke_admin.add_argument(
        "--key", help="the raw admin secret to revoke (hashed before matching)"
    )
    revoke_admin.add_argument(
        "--hash", dest="stored_hash", help="the stored SHA-256 hex from `hivemind-keys list`"
    )

    revoke = sub.add_parser("revoke", help="revoke an agent's key (name stays reserved)")
    revoke.add_argument("--name", required=True)

    args = parser.parse_args()
    actor: str = args.actor if args.actor is not None else default_actor()
    dsn = load_settings().database_url
    if args.cmd == "issue-admin":
        print(asyncio.run(_issue_admin(dsn, actor=actor)))
    elif args.cmd == "issue-agent":
        try:
            print(asyncio.run(_issue_agent(dsn, args.name, actor=actor)))
        except AgentKeyExists:
            print(
                f"agent {args.name} already holds a key; revoke it first "
                "(`hivemind-keys revoke --name ...`) to issue a replacement",
                file=sys.stderr,
            )
            raise SystemExit(1) from None
    elif args.cmd == "rotate-org":
        print(asyncio.run(_rotate_org(dsn, actor=actor)))
    elif args.cmd == "list":
        asyncio.run(_list(dsn))
    elif args.cmd == "revoke-admin":
        if (args.key is None) == (args.stored_hash is None):
            parser.error("exactly one of --key / --hash is required")
        revoked = asyncio.run(
            _revoke_admin(dsn, raw_key=args.key, stored_hash=args.stored_hash, actor=actor)
        )
        if revoked:
            print("revoked the admin key (issue a replacement via `hivemind-keys issue-admin`)")
        else:
            print(
                "no admin key matches that identifier (check `hivemind-keys list`)",
                file=sys.stderr,
            )
            raise SystemExit(1)
    elif args.cmd == "revoke":
        asyncio.run(_revoke(dsn, args.name, actor=actor))
        print(f"revoked the key for agent {args.name} (name stays reserved)")


if __name__ == "__main__":
    main()
