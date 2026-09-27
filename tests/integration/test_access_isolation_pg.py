"""The reported ``self``-isolation bug, end to end on real Postgres (ADR 0033).

Two contributors with their own keys, issued and verified by the real
``PgAuthenticator``: one must not read, feed back on, withdraw or
supersede the other's ``self`` entry, while its own and fleet rules keep
working. Skips when Postgres is down, like the rest of the suite.
"""

from __future__ import annotations

import asyncpg
import pytest
from tests.fakes import FakeEmbedder, make_search_config

from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.mcp.app import McpHivemind, hive_feedback, hive_get, hive_withdraw, hive_write
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.services.governance import GovernanceService, WriteService
from hivemind.services.search import SearchService
from hivemind.store import PgAuthenticator, PgStore
from hivemind.store.keys import _issue_admin
from hivemind.store.migrate import migrate

TABLES = "TRUNCATE audit_log, credentials, agents, fleets, entries, feedbacks"


@pytest.fixture
async def pool():
    dsn = Settings().database_url
    try:
        probe = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # connection refused / timeout / auth
        pytest.skip(f"Postgres unreachable at {dsn}: {exc}")
    await probe.close()
    await migrate(dsn, Settings().embedding_dim)
    conn = await asyncpg.connect(dsn)
    await conn.execute(TABLES)
    await conn.close()
    store, auth = PgStore(dsn), PgAuthenticator(dsn)
    try:
        yield dsn, store, auth
    finally:
        await store.close()
        await auth.close()
        conn = await asyncpg.connect(dsn)
        await conn.execute(TABLES)
        await conn.close()


def _surface(store: PgStore, credential: Credential) -> McpHivemind:
    embedder = FakeEmbedder(dimension=Settings().embedding_dim)
    config = make_search_config()
    return McpHivemind(
        store=store,
        write_service=WriteService(store, embedder),
        search_service=SearchService(store, embedder, config),
        governance_service=GovernanceService(store),
        access_service=AccessService(store),
        search_config=config,
        credential=credential,
    )


async def test_two_contributors_with_their_own_keys(pool) -> None:
    dsn, store, auth = pool
    admin = await auth.verify(await _issue_admin(dsn, actor="test"))
    assert admin is not None
    access = AccessService(store, auth)
    fleet = await access.create_fleet("platform", admin)
    creds = {}
    for name in ("alice", "bob"):
        await store.register_agent(name, "owner")
        _, key = await access.activate(name, TrustLevel.CONTRIBUTOR, fleet.id, admin)
        creds[name] = await auth.verify(key)
    alice, bob = _surface(store, creds["alice"]), _surface(store, creds["bob"])

    secret = await hive_write(alice, kind="fact", summary="alice private", scope="self")
    assert secret["author"] == "alice"
    sid = str(secret["id"])

    for result in (
        await hive_get(bob, entry_id=sid),
        await hive_feedback(bob, entry_id=sid, verdict="wrong"),
        await hive_withdraw(bob, entry_id=sid),
    ):
        assert result["error"]["code"] == "not_found"  # type: ignore[index]
    hijack = await hive_write(bob, kind="fact", summary="x", supersedes=[sid])
    assert hijack["error"]["code"] == "supersede_denied"  # type: ignore[index]
    assert (await store.get_entry(sid)).state.value == "active"  # type: ignore[union-attr]

    # Own reads and fleet collaboration still work.
    assert (await hive_get(alice, entry_id=sid))["id"] == sid
    shared = await hive_write(alice, kind="fact", summary="fleet fact")
    fixed = await hive_write(
        bob, kind="fact", summary="fleet fact, corrected", supersedes=[str(shared["id"])]
    )
    assert "error" not in fixed, fixed
