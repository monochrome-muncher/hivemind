"""Postgres round-trip of jsonb columns through ``PgStore`` and the REST app.

``payload`` (and ``entities``) are jsonb columns. Without a jsonb codec on
the pool, asyncpg hands them back as JSON *strings*: REST entry responses
failed validation (500) and MCP returned a string where the schema says
object. These tests write a payload and read it back through every read
path, on real Postgres. Skips cleanly when Postgres is unreachable.
"""

from __future__ import annotations

import asyncpg
import httpx
import pytest
from tests.fakes import FakeEmbedder, make_search_config

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.entry import EntryDraft, EntryFilters, Kind
from hivemind.store import PgAuthenticator, PgStore
from hivemind.store.migrate import migrate

PAYLOAD = {"n": 1, "nested": {"a": [1, 2, {"b": None}]}, "s": "x"}


@pytest.fixture
async def store():
    dsn, dim = Settings().database_url, Settings().embedding_dim
    try:
        probe = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:
        pytest.skip(f"Postgres unreachable at {dsn}: {exc}")
    await probe.close()
    await migrate(dsn, dim)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("TRUNCATE credentials, feedbacks, entries")
    finally:
        await conn.close()
    s = PgStore(dsn)
    try:
        yield s, dim
    finally:
        await s.close()


def _draft(summary: str = "payload entry") -> EntryDraft:
    return EntryDraft(
        kind=Kind.FACT,
        summary=summary,
        author="admin",
        agent="a",
        payload=PAYLOAD,
        scope="org",
    )


async def test_payload_roundtrips_as_a_dict_through_the_store(store) -> None:
    s, dim = store
    created = await s.create_entry(_draft(), [0.1] * dim)
    assert created.payload == PAYLOAD
    got = await s.get_entry(created.id)
    assert got is not None and got.payload == PAYLOAD
    listed = await s.list_entries(EntryFilters(), limit=10, offset=0)
    assert [e.payload for e in listed] == [PAYLOAD]


async def test_payload_roundtrips_through_the_rest_app(store) -> None:
    s, dim = store
    auth = PgAuthenticator(Settings().database_url)
    try:
        admin = await auth.issue_admin_key()
        app = create_app_for_config(
            Settings(),
            store=s,
            embedder=FakeEmbedder(dimension=dim),
            authenticator=auth,
            search_config=make_search_config(),
        )
        h = {"X-API-Key": admin}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(app)), base_url="http://t"
        ) as c:
            r = await c.post(
                "/v1/entries",
                json={"kind": "fact", "summary": "payload rest", "payload": PAYLOAD, "agent": "a"},
                headers=h,
            )
            assert r.status_code == 201, r.text
            assert r.json()["payload"] == PAYLOAD
            eid = r.json()["id"]

            r = await c.get(f"/v1/entries/{eid}", headers=h)
            assert r.status_code == 200, r.text
            assert r.json()["payload"] == PAYLOAD

            r = await c.get("/v1/entries", headers=h)
            assert r.status_code == 200, r.text
            assert r.json()[0]["payload"] == PAYLOAD

            r = await c.post("/v1/search", json={"query": "payload rest"}, headers=h)
            assert r.status_code == 200, r.text
            assert r.json()[0]["entry_id"] == eid

            r = await c.post(f"/v1/entries/{eid}/withdraw", json={"reason": "t"}, headers=h)
            assert r.status_code == 200, r.text
            assert r.json()["payload"] == PAYLOAD
    finally:
        await auth.close()
