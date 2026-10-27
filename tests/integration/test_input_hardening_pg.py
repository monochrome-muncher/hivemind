"""The REST surface over a real ``PgStore``: the largest body the cap allows is
still storable, because Postgres refuses a tsvector over 1 MB (ADR 0040).

The 422s for U+0000, lone surrogates, int64 overflow and out-of-range dates
come from validation before the store, so ``tests/unit/test_input_hardening*.py``
pins them.

Skips cleanly when Postgres is unreachable.
"""

from __future__ import annotations

import asyncpg
import httpx
import pytest
from tests.fakes import FakeEmbedder, make_search_config
from tests.unit.test_api_endpoints import FakeAuthenticator

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.validation import MAX_BODY_CHARS
from hivemind.ports import Credential
from hivemind.store import PgStore
from hivemind.store.migrate import migrate

KEY = {"X-API-Key": "key-alice"}


@pytest.fixture
async def client():
    dsn = Settings().database_url
    try:
        probe = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:
        pytest.skip(f"Postgres unreachable at {dsn}: {exc}")
    await probe.close()
    settings = Settings()
    await migrate(dsn, settings.embedding_dim)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("TRUNCATE entries, audit_log CASCADE")
    finally:
        await conn.close()
    store = PgStore(dsn)
    authenticator = FakeAuthenticator(
        {
            "key-alice": Credential(user_id="alice", agent_id="agent-alice"),
            "key-admin": Credential(user_id="ops", agent_id="admin-agent", is_admin=True),
            "key-org": Credential(user_id="org", is_org=True, access_controlled=True),
        }
    )
    hivemind = create_app_for_config(
        settings,
        store=store,
        embedder=FakeEmbedder(dimension=settings.embedding_dim),
        authenticator=authenticator,  # type: ignore[arg-type]
        search_config=make_search_config(),
    )
    transport = httpx.ASGITransport(app=create_app(hivemind))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://hivemind.test") as c:
            yield c
    finally:
        await store.close()


async def test_the_largest_allowed_body_of_distinct_words_is_stored(
    client: httpx.AsyncClient,
) -> None:
    """The body cap keeps the tsvector (1 MB limit) writable (SP-3)."""
    words, size, i = [], 0, 0
    while size < MAX_BODY_CHARS - 10:
        word = f"w{i:x}"
        words.append(word)
        size += len(word) + 1
        i += 1
    body = " ".join(words)[:MAX_BODY_CHARS].ljust(MAX_BODY_CHARS, " ")
    resp = await client.post(
        "/v1/entries", json={"kind": "fact", "summary": "big", "body": body}, headers=KEY
    )
    assert resp.status_code == 201, resp.text
    over = await client.post(
        "/v1/entries",
        json={"kind": "fact", "summary": "big", "body": body + "x"},
        headers=KEY,
    )
    assert over.status_code == 422
