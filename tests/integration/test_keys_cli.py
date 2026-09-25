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
from hivemind.store.keys import _issue_admin, _revoke_admin, _rotate_org
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
