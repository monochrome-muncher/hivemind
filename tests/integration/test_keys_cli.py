"""Postgres-backed tests for the keys CLI (SPEC §8.1, ADR 0012).

These exercise the ``hivemind-keys`` CLI's async SQL functions against
the live dev Postgres (the CLI's seam — ``main()`` is a thin argparse
wrapper over them). They cover the rotation story DEPLOY.md §5
documents: admin-key rotation (issue a new key, distribute it, then
``revoke-admin`` the old one) and ``rotate-org`` (which closes registration, ADR 0031).

Hermetic-by-construction (mirroring ``test_pgstore_access.py``): skip
cleanly when Postgres is unreachable; ``credentials`` is truncated
between tests.
"""

from __future__ import annotations

import asyncpg
import pytest

from hivemind.config import Settings
from hivemind.store import PgAuthenticator
from hivemind.store.auth import key_hash
from hivemind.store.keys import (
    AgentNotRegistered,
    _issue_admin,
    _issue_agent,
    _revoke_admin,
    _rotate_org,
)
from hivemind.store.migrate import migrate


def _dsn() -> str:
    return Settings().database_url


async def _truncate_credentials(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("TRUNCATE credentials")
    finally:
        await conn.close()


@pytest.fixture
async def pg():
    """A clean dev DB for the keys table (skips when Postgres is down)."""
    dsn = _dsn()
    dim = Settings().embedding_dim
    try:
        probe = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # connection refused / timeout / auth
        pytest.skip(f"Postgres unreachable at {dsn}: {exc}")
    await probe.close()
    await migrate(dsn, dim)
    await _truncate_credentials(dsn)
    auth = PgAuthenticator(dsn)
    try:
        yield auth
    finally:
        await _truncate_credentials(dsn)
        await auth.close()


# --- issue + verify round-trip --------------------------------------------------


async def test_issued_admin_key_verifies(pg) -> None:
    dsn = _dsn()
    raw_key = await _issue_admin(dsn)
    credential = await pg.verify(raw_key)
    assert credential is not None
    assert credential.is_admin is True


async def test_rotate_org_replaces_the_shared_key(pg) -> None:
    dsn = _dsn()
    old_key = await _rotate_org(dsn)
    assert await pg.verify(old_key) is not None  # the new key verifies
    old_key_2 = await _rotate_org(dsn)
    # rotate-org deletes the previous org row: the first key is dead now.
    assert await pg.verify(old_key) is None
    assert await pg.verify(old_key_2) is not None


async def test_issue_agent_for_unregistered_name_refuses(pg) -> None:
    """A key for an unregistered name would resolve to a non-identity
    (ADR 0012): the CLI refuses loudly instead of minting a dangling
    key that every read/write seam would later reject opaquely."""
    dsn = _dsn()
    with pytest.raises(AgentNotRegistered):
        await _issue_agent(dsn, "never-registered-agent", actor="test")


# --- revoke-admin (the admin-key rotation half, DEPLOY.md §5) ------------------


async def test_revoke_admin_by_raw_key_kills_only_that_key(pg) -> None:
    dsn = _dsn()
    key_a = await _issue_admin(dsn)
    key_b = await _issue_admin(dsn)
    assert await _revoke_admin(dsn, raw_key=key_a) is True
    assert await pg.verify(key_a) is None  # revoked: 401 on the next request
    credential_b = await pg.verify(key_b)
    assert credential_b is not None and credential_b.is_admin is True


async def test_revoke_admin_by_stored_hash(pg) -> None:
    dsn = _dsn()
    raw_key = await _issue_admin(dsn)
    assert await _revoke_admin(dsn, stored_hash=key_hash(raw_key)) is True
    assert await pg.verify(raw_key) is None


async def test_revoke_admin_by_fingerprint_prefix(pg) -> None:
    # `hivemind-keys list` prints a 12-char fingerprint; that must be enough.
    dsn = _dsn()
    orphan = await _issue_admin(dsn)
    keeper = await _issue_admin(dsn)
    assert await _revoke_admin(dsn, stored_hash=key_hash(orphan)[:12]) is True
    assert await pg.verify(orphan) is None
    assert await pg.verify(keeper) is not None


async def test_revoke_admin_refuses_short_or_non_hex_hash(pg) -> None:
    dsn = _dsn()
    raw = await _issue_admin(dsn)
    for bad in ("abc", "zzzzzzzzzzzz", ""):
        with pytest.raises(ValueError):
            await _revoke_admin(dsn, stored_hash=bad)
    assert await pg.verify(raw) is not None


async def test_revoke_admin_refuses_an_ambiguous_prefix(pg) -> None:
    dsn = _dsn()
    conn = await asyncpg.connect(dsn)
    try:
        for suffix in ("a", "b"):  # two admin rows sharing a 12-char prefix
            await conn.execute(
                "INSERT INTO credentials (key_hash, kind, user_id) VALUES ($1, 'admin', 'admin')",
                "0123456789ab" + suffix * 52,
            )
        with pytest.raises(ValueError, match="matches 2"):
            await _revoke_admin(dsn, stored_hash="0123456789ab")
        assert await conn.fetchval("SELECT count(*) FROM credentials") == 2
    finally:
        await conn.close()


async def test_revoke_admin_of_unknown_key_is_a_loud_noop(pg) -> None:
    dsn = _dsn()
    surviving = await _issue_admin(dsn)
    # A raw key that was never issued (and its hash) match nothing.
    assert await _revoke_admin(dsn, raw_key="hm_00000000000000000000000000000000") is False
    assert (
        await _revoke_admin(dsn, stored_hash=key_hash("hm_ffffffffffffffffffffffffffffffff"))
        is False
    )
    # ...and the surviving admin key is untouched.
    credential = await pg.verify(surviving)
    assert credential is not None and credential.is_admin is True
