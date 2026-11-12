"""``POST /v1/admin/search`` with ``explain`` on real Postgres (ADR 0137): both streams are ranked,
the parts reproduce each score, and the ranking is the one without ``explain``."""

from __future__ import annotations

import httpx
import pytest
from tests.fakes import FakeEmbedder, make_search_config
from tests.pg import ensure_migrated, reset_tables

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.store import PgAuthenticator, PgStore
from hivemind.store.keys import _issue_admin

QUERY = "kaniko cache"


@pytest.fixture
async def world():
    settings = Settings()
    dsn = settings.database_url
    await ensure_migrated(dsn, settings.embedding_dim)
    await reset_tables(dsn)
    store, auth = PgStore(dsn), PgAuthenticator(dsn)
    app = create_app_for_config(
        settings,
        store=store,
        embedder=FakeEmbedder(dimension=settings.embedding_dim),
        authenticator=auth,
        search_config=make_search_config(),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url="http://t"
    )
    admin = {"X-API-Key": await _issue_admin(dsn, actor="test")}
    try:
        yield client, admin
    finally:
        await client.aclose()
        await store.close()
        await auth.close()


async def test_explain_end_to_end(world) -> None:
    client, admin = world
    for summary, importance in (
        ("kaniko cache setup in the build runner", 3),
        ("kaniko cache layers explained", 5),
        ("unrelated rollout checklist", 3),
    ):
        written = await client.post(
            "/v1/entries",
            json={
                "kind": "fact",
                "summary": summary,
                "importance": importance,
                "scope": "org",
                "agent": "writer",
            },
            headers=admin,
        )
        assert written.status_code == 201, written.text

    plain = await client.post("/v1/admin/search", json={"query": QUERY}, headers=admin)
    explained = await client.post(
        "/v1/admin/search", json={"query": QUERY, "explain": True}, headers=admin
    )
    assert plain.status_code == explained.status_code == 200
    assert [h["entry_id"] for h in explained.json()] == [h["entry_id"] for h in plain.json()]
    assert all("explain" not in h for h in plain.json())

    hits = explained.json()
    assert len(hits) >= 2
    for hit in hits:
        e = hit["explain"]
        assert e["mode"] == "hybrid"
        assert e["keyword_rank"] is not None or e["vector_rank"] is not None
        product = (
            e["fused"] * e["importance"]["factor"] * e["recency"]["factor"] * e["social"]["factor"]
        )
        assert hit["score"] == pytest.approx(product, rel=1e-3)
    assert any(h["explain"]["keyword_rank"] == 1 for h in hits)
