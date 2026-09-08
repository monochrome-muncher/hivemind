"""Issue, list, and revoke API keys for the credential model (ADR 0008).

Console entry point (``hivemind-keys``). Raw keys are random 32-byte
secrets printed **once** at issuance; only their SHA-256 hash is stored
in the ``credentials`` table (a leaked database never leaks usable
keys — SPEC.md §8.1).

Usage:
    hivemind-keys issue --user alice [--agent alice-cli-1] [--admin]
    hivemind-keys list
    hivemind-keys revoke --user alice [--agent alice-cli-1]
"""

from __future__ import annotations

import argparse
import asyncio
import secrets

import asyncpg

from hivemind.config import Settings
from hivemind.store.auth import key_hash

_ISSUE = "INSERT INTO credentials (key_hash, kind, user_id, agent_id) VALUES ($1, $2, $3, $4)"
_LIST = "SELECT key_hash, kind, user_id, agent_id FROM credentials ORDER BY created_at"
_REVOKE_AGENT = "DELETE FROM credentials WHERE kind = 'agent' AND user_id = $1 AND agent_id = $2"
_REVOKE_USER = "DELETE FROM credentials WHERE user_id = $1"


def _issue(dsn: str, user: str, agent: str | None, admin: bool) -> None:
    """Issue a key for ``user`` (and optionally ``agent``); print the
    raw secret once, store only its hash."""
    kind = "admin" if admin else ("agent" if agent is not None else "user")
    raw_key = "hm_" + secrets.token_hex(32)

    async def run() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(_ISSUE, key_hash(raw_key), kind, user, agent)
        finally:
            await conn.close()

    asyncio.run(run())
    print(raw_key)


def _list(dsn: str) -> None:
    async def run() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            for row in await conn.fetch(_LIST):
                agent = row["agent_id"] or "-"
                print(
                    f"{row['key_hash'][:12]}…  {row['kind']:<6} user={row['user_id']} agent={agent}"
                )
        finally:
            await conn.close()

    asyncio.run(run())


def _revoke(dsn: str, user: str, agent: str | None) -> None:
    async def run() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            if agent is not None:
                await conn.execute(_REVOKE_AGENT, user, agent)
            else:
                await conn.execute(_REVOKE_USER, user)
        finally:
            await conn.close()

    asyncio.run(run())
    print(f"revoked keys for user {user}" + (f" agent {agent}" if agent else ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="Hivemind key management")
    sub = parser.add_subparsers(dest="cmd", required=True)

    issue = sub.add_parser("issue", help="issue a new API key")
    issue.add_argument("--user", required=True)
    issue.add_argument("--agent", default=None)
    issue.add_argument("--admin", action="store_true")

    sub.add_parser("list", help="list credential hashes (never raw keys)")

    revoke = sub.add_parser("revoke", help="revoke keys for a user")
    revoke.add_argument("--user", required=True)
    revoke.add_argument("--agent", default=None)

    args = parser.parse_args()
    dsn = Settings().database_url
    if args.cmd == "issue":
        _issue(dsn, args.user, args.agent, args.admin)
    elif args.cmd == "list":
        _list(dsn)
    elif args.cmd == "revoke":
        _revoke(dsn, args.user, args.agent)


if __name__ == "__main__":
    main()
