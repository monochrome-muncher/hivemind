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

Usage:
    hivemind-keys issue-admin
    hivemind-keys issue-agent --name alice
    hivemind-keys rotate-org
    hivemind-keys revoke --name alice
    hivemind-keys revoke-admin --key hm_xxx   (or --hash <sha256 from list>)
    hivemind-keys list
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys

import asyncpg

from hivemind.config import Settings
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


def _raw_key() -> str:
    """A fresh random 32-byte API key (the raw secret, printed once)."""
    return "hm_" + secrets.token_hex(32)


async def _issue_admin(dsn: str) -> str:
    """Issue an admin key; return the raw secret (printed once)."""
    raw_key = _raw_key()
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(_ISSUE_ADMIN, key_hash(raw_key), "admin")
    finally:
        await conn.close()
    return raw_key


async def _issue_agent(dsn: str, name: str) -> str:
    """Issue an agent key bound to the registered agent name (ADR 0012)."""
    raw_key = _raw_key()
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(_ISSUE_AGENT, key_hash(raw_key), name)
    finally:
        await conn.close()
    return raw_key


async def _rotate_org(dsn: str) -> str:
    """Rotate the shared org key (the cluster kill switch, ADR 0012)."""
    raw_key = _raw_key()
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            await conn.execute(_DELETE_ORG)
            await conn.execute(_ISSUE_ORG, key_hash(raw_key), "org")
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
        print(f"{row['key_hash'][:12]}…  {row['kind']:<6} {name}")


async def _revoke(dsn: str, name: str) -> None:
    """Retire an agent's key (the name stays reserved, ADR 0012)."""
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(_REVOKE_AGENT, name)
    finally:
        await conn.close()


async def _revoke_admin(
    dsn: str, *, raw_key: str | None = None, stored_hash: str | None = None
) -> bool:
    """Revoke a specific admin key — the second half of admin-key rotation.

    Targets the admin row by either the raw secret (SHA-256 hashed
    first, SPEC §8.1) or the stored SHA-256 hex that ``hivemind-keys
    list`` prints. Returns ``True`` if an admin row was deleted, or
    ``False`` if nothing matched (the CLI then exits non-zero so the
    no-op is loud from a script — DEPLOY.md §5)."""
    if stored_hash is not None:
        target = stored_hash
    elif raw_key is not None:
        target = key_hash(raw_key)  # the guard above proves it is set
    else:
        raise ValueError("either raw_key or stored_hash must be provided")
    conn = await asyncpg.connect(dsn)
    try:
        status = await conn.execute(_REVOKE_ADMIN, target)
    finally:
        await conn.close()
    return str(status) == "DELETE 1"


def main() -> None:
    """Console entry point (``hivemind-keys``)."""
    parser = argparse.ArgumentParser(description="Hivemind key management (ADR 0012)")
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
    dsn = Settings().database_url
    if args.cmd == "issue-admin":
        print(asyncio.run(_issue_admin(dsn)))
    elif args.cmd == "issue-agent":
        print(asyncio.run(_issue_agent(dsn, args.name)))
    elif args.cmd == "rotate-org":
        print(asyncio.run(_rotate_org(dsn)))
    elif args.cmd == "list":
        asyncio.run(_list(dsn))
    elif args.cmd == "revoke-admin":
        if (args.key is None) == (args.stored_hash is None):
            parser.error("exactly one of --key / --hash is required")
        revoked = asyncio.run(_revoke_admin(dsn, raw_key=args.key, stored_hash=args.stored_hash))
        if revoked:
            print("revoked the admin key (issue a replacement via `hivemind-keys issue-admin`)")
        else:
            print(
                "no admin key matches that identifier (check `hivemind-keys list`)",
                file=sys.stderr,
            )
            raise SystemExit(1)
    elif args.cmd == "revoke":
        asyncio.run(_revoke(dsn, args.name))
        print(f"revoked the key for agent {args.name} (name stays reserved)")


if __name__ == "__main__":
    main()
