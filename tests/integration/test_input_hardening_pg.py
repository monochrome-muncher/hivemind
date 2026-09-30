"""The REST surface over a real ``PgStore``: inputs Postgres cannot hold
(U+0000, int64 overflow, a >1 MB tsvector) are refused with a typed 422 /
413 — never the unhandled 500 they produced before ADR 0040.

Skips cleanly when Postgres is unreachable.
"""

from __future__ import annotations

import json

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
ADMIN = {"X-API-Key": "key-admin"}
ORG = {"X-API-Key": "key-org"}


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


@pytest.mark.parametrize(
    "fields",
    [
        {"summary": "x\x00y"},
        {"body": "x\x00y"},
        {"tags": ["x\x00y"]},
        {"payload": {"a": "x\x00y"}},
        {"sources": [{"type": "url", "ref": "x\x00y"}]},
    ],
)
async def test_nul_in_an_entry_is_422_not_500(client: httpx.AsyncClient, fields: dict) -> None:
    resp = await client.post(
        "/v1/entries", json={"kind": "fact", "summary": "s", **fields}, headers=KEY
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("POST", "/v1/search", {"query": "x\x00y"}),
        ("GET", "/v1/entries?author=x%00y", None),
        ("GET", "/v1/entries?tags=x%00", None),
        ("GET", "/v1/entries?limit=99999999999999999999", None),
        ("GET", "/v1/entries?offset=18446744073709551616", None),
        ("GET", "/v1/admin/audit-log?actor=x%00", None),
        ("POST", "/v1/admin/agents/a%00b/revoke", None),
        ("POST", "/v1/admin/fleets", {"name": "x\x00y"}),
        ("POST", "/v1/agents", {"name": "x\x00y"}),
    ],
)
async def test_nul_and_overflow_in_reads_and_admin_calls_are_422(
    client: httpx.AsyncClient, method: str, url: str, body: object
) -> None:
    headers = ORG if url == "/v1/agents" else ADMIN
    resp = await client.request(method, url, json=body, headers=headers)
    assert resp.status_code == 422, resp.text


async def test_nul_in_feedback_and_withdraw_text_is_422(client: httpx.AsyncClient) -> None:
    made = await client.post("/v1/entries", json={"kind": "fact", "summary": "s"}, headers=KEY)
    assert made.status_code == 201, made.text
    entry_id = made.json()["id"]
    feedback = await client.post(
        f"/v1/entries/{entry_id}/feedback",
        json={"verdict": "helpful", "note": "a\x00"},
        headers=KEY,
    )
    withdraw = await client.post(
        f"/v1/entries/{entry_id}/withdraw", json={"reason": "a\x00"}, headers=KEY
    )
    assert feedback.status_code == withdraw.status_code == 422


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


async def _post(client: httpx.AsyncClient, url: str, obj: object) -> httpx.Response:
    """POST JSON with ``ensure_ascii`` escapes, so a lone surrogate reaches
    the server as ``\\ud800`` (httpx's own encoder refuses to send one)."""
    return await client.post(
        url,
        content=json.dumps(obj).encode(),
        headers={**KEY, "Content-Type": "application/json"},
    )


# -- verifier follow-ups: surrogates, datetime range, deep payloads ---------------


@pytest.mark.parametrize(
    "fields",
    [
        {"tags": ["a\ud800"]},
        {"summary": "a\udfffb"},
        {"payload": {"a": "\ud800"}},
        {"sources": [{"type": "url", "ref": "\udc00"}]},
    ],
)
async def test_lone_surrogates_in_an_entry_are_422(client: httpx.AsyncClient, fields: dict) -> None:
    resp = await _post(client, "/v1/entries", {"kind": "fact", "summary": "s", **fields})
    assert resp.status_code == 422, resp.text


async def test_lone_surrogates_in_search_feedback_and_withdraw_are_422(
    client: httpx.AsyncClient,
) -> None:
    made = await _post(client, "/v1/entries", {"kind": "fact", "summary": "s"})
    entry_id = made.json()["id"]
    search = await _post(client, "/v1/search", {"query": "a\ud800"})
    tagged = await _post(client, "/v1/search", {"query": "q", "tags": ["\ud800"]})
    feedback = await _post(
        client, f"/v1/entries/{entry_id}/feedback", {"verdict": "helpful", "note": "\ud800"}
    )
    withdraw = await _post(client, f"/v1/entries/{entry_id}/withdraw", {"reason": "\udfff"})
    assert search.status_code == tagged.status_code == 422
    assert feedback.status_code == withdraw.status_code == 422


@pytest.mark.parametrize("value", ["0001-01-01T00:00:00+23:00", "9999-12-31T23:59:59-23:00"])
async def test_out_of_range_datetimes_are_422_not_500(
    client: httpx.AsyncClient, value: str
) -> None:
    listing = await client.get("/v1/entries", params={"created_from": value}, headers=KEY)
    search = await client.post(
        "/v1/search", json={"query": "q", "occurred_from": value}, headers=KEY
    )
    write = await client.post(
        "/v1/entries",
        json={"kind": "fact", "summary": "s", "occurred_at": value},
        headers=KEY,
    )
    assert listing.status_code == search.status_code == write.status_code == 422


async def test_a_deeply_nested_payload_is_refused_and_does_not_poison_listing(
    client: httpx.AsyncClient,
) -> None:
    node: dict = {"leaf": 1}
    for _ in range(300):
        node = {"n": node}
    resp = await client.post(
        "/v1/entries", json={"kind": "fact", "summary": "s", "payload": node}, headers=KEY
    )
    listing = await client.get("/v1/entries", headers=KEY)
    assert resp.status_code == 422
    assert listing.status_code == 200
