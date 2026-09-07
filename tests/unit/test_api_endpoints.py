"""Unit tests for the Hivemind REST surface (SPEC.md §5).

Seams under test: the FastAPI app built by ``create_app``/
``create_app_for_config`` — driven over HTTP with a real ``MemoryStore``,
the deterministic ``FakeEmbedder``, and a dict-backed authenticator, so
no HTTP-level test touches a real database or embedding service.

Error responses use the standard envelope:
``{"error": {"code": ..., "message": ...}}``.
"""

from __future__ import annotations

import httpx
import pytest

from hivemind.api.deps import HivemindApp, create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from tests.fakes import make_clock, make_embedder, make_search_config


class FakeAuthenticator:
    """Dict-backed Authenticator (SPEC.md §8.1): user + agent sub-keys."""

    def __init__(self, keys: dict[str, Credential]) -> None:
        self._keys = keys

    async def verify(self, key: str) -> Credential | None:
        return self._keys.get(key)


def make_authenticator() -> FakeAuthenticator:
    return FakeAuthenticator(
        keys={
            "key-alice": Credential(user_id="alice", agent_id="agent-alice"),
            "key-bob": Credential(user_id="bob", agent_id="agent-bob"),
            "key-alice-user": Credential(user_id="alice"),
            "key-admin": Credential(user_id="ops", agent_id="admin-agent", is_admin=True),
        }
    )


def make_hivemind_app() -> HivemindApp:
    """A full HivemindApp wired from fakes: real services, fake ports."""
    return create_app_for_config(
        Settings(),
        store=MemoryStore(make_clock()),
        embedder=make_embedder(),
        authenticator=make_authenticator(),
        search_config=make_search_config(),
    )


def make_client(app: HivemindApp) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(app))
    return httpx.AsyncClient(transport=transport, base_url="http://hivemind.test")


async def post_entry(
    client: httpx.AsyncClient,
    key: str,
    summary: str,
    **body: object,
) -> dict:
    payload = {"kind": "fact", "summary": summary, **body}
    resp = await client.post("/v1/entries", json=payload, headers={"X-API-Key": key})
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestHealth:
    async def test_health_is_public_and_reports_ok(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            resp = await client.get("/v1/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestAuth:
    async def test_missing_key_is_401(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            resp = await client.get("/v1/entries")
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"]["code"] == "missing_api_key"

    async def test_unknown_key_is_401(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            resp = await client.get("/v1/entries", headers={"X-API-Key": "nope"})
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"]["code"] == "unknown_api_key"


class TestCreateEntry:
    async def test_create_stamps_provenance_from_credential(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            resp = await client.post(
                "/v1/entries",
                json={"kind": "fact", "summary": "auth service uses JWT"},
                headers={"X-API-Key": "key-alice"},
            )
        assert resp.status_code == 201
        entry = resp.json()
        assert entry["author"] == "alice"  # from the credential, not the body
        assert entry["agent"] == "agent-alice"  # from the agent sub-key
        assert entry["state"] == "active"
        assert entry["kind"] == "fact"
        assert entry["id"]
        assert entry["occurred_at"]
        assert entry["created_at"]

    async def test_create_round_trip_returns_full_entry(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            created = await post_entry(
                client,
                "key-alice",
                "churn model uses weekly cohorts",
                body="the full analysis",
                payload={"cutoff": 0.42},
                sources=[{"type": "path", "ref": "/data/cohort_2026.csv"}],
                tags=["churn", "modeling"],
                importance=4,
            )
            resp = await client.get(
                f"/v1/entries/{created['id']}", headers={"X-API-Key": "key-alice"}
            )
        assert resp.status_code == 200
        entry = resp.json()
        assert entry["body"] == "the full analysis"
        assert entry["payload"] == {"cutoff": 0.42}
        assert entry["sources"] == [{"type": "path", "ref": "/data/cohort_2026.csv"}]
        assert entry["tags"] == ["churn", "modeling"]
        assert entry["importance"] == 4

    async def test_user_key_with_self_reported_agent(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            created = await post_entry(
                client,
                "key-alice-user",
                "a backdated fact",
                agent="my-local-agent",
                occurred_at="2026-01-15T09:00:00Z",
            )
        assert created["agent"] == "my-local-agent"
        assert created["author"] == "alice"
        assert created["occurred_at"].startswith("2026-01-15")

    async def test_user_key_without_agent_is_422(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            resp = await client.post(
                "/v1/entries",
                json={"kind": "fact", "summary": "no agent identity"},
                headers={"X-API-Key": "key-alice-user"},
            )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "agent_identity_required"

    async def test_unknown_kind_is_rejected(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            resp = await client.post(
                "/v1/entries",
                json={"kind": "opinion", "summary": "not a v1 kind"},
                headers={"X-API-Key": "key-alice"},
            )
        assert resp.status_code == 422

    async def test_invalid_importance_is_422(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            resp = await client.post(
                "/v1/entries",
                json={"kind": "fact", "summary": "bad importance", "importance": 9},
                headers={"X-API-Key": "key-alice"},
            )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "invalid_entry"


class TestGetEntry:
    async def test_get_unknown_entry_is_404(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            resp = await client.get(
                "/v1/entries/does-not-exist", headers={"X-API-Key": "key-alice"}
            )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"


class TestSearch:
    async def test_search_returns_compact_hits_without_body(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            await post_entry(client, "key-alice", "auth service uses JWT tokens")
            resp = await client.post(
                "/v1/search",
                json={"query": "JWT auth"},
                headers={"X-API-Key": "key-bob"},
            )
        assert resp.status_code == 200
        hits = resp.json()
        assert len(hits) == 1
        hit = hits[0]
        # Progressive disclosure (SPEC.md §6.1): the hit has no body.
        assert "body" not in hit
        assert hit["entry_id"]
        assert hit["kind"] == "fact"
        assert hit["summary"] == "auth service uses JWT tokens"
        assert hit["score"] > 0.0
        assert hit["author"] == "alice"

    async def test_search_respects_filters(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            await post_entry(client, "key-alice", "weekly cohorts", kind="fact")
            await post_entry(client, "key-bob", "weekly cohorts", kind="decision")
            resp = await client.post(
                "/v1/search",
                json={"query": "weekly cohorts", "kind": "fact"},
                headers={"X-API-Key": "key-alice"},
            )
        hits = resp.json()
        assert len(hits) == 1
        assert hits[0]["kind"] == "fact"

    async def test_search_limit_is_respected(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            for i in range(5):
                await post_entry(client, "key-alice", f"note number {i} about cohorts")
            resp = await client.post(
                "/v1/search",
                json={"query": "cohorts note", "limit": 2},
                headers={"X-API-Key": "key-alice"},
            )
        assert len(resp.json()) == 2


class TestListEntries:
    async def test_list_filters_by_kind(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            await post_entry(client, "key-alice", "a fact about auth", kind="fact")
            await post_entry(client, "key-bob", "a decision on rollout", kind="decision")
            resp = await client.get(
                "/v1/entries",
                params=[("kind", "fact"), ("limit", "50")],
                headers={"X-API-Key": "key-alice"},
            )
        listed = resp.json()
        assert len(listed) == 1
        assert listed[0]["kind"] == "fact"

    async def test_list_filters_by_author(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            await post_entry(client, "key-alice", "alice's fact")
            await post_entry(client, "key-bob", "bob's fact")
            resp = await client.get(
                "/v1/entries",
                params=[("author", "bob")],
                headers={"X-API-Key": "key-alice"},
            )
        listed = resp.json()
        assert len(listed) == 1
        assert listed[0]["author"] == "bob"

    async def test_list_filters_by_tags_and_paginates(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            await post_entry(client, "key-alice", "first tagged", tags=["x", "y"])
            await post_entry(client, "key-alice", "second tagged", tags=["x"])
            resp = await client.get(
                "/v1/entries",
                params=[("tags", "x")],
                headers={"X-API-Key": "key-alice"},
            )
            assert len(resp.json()) == 2
            page = await client.get(
                "/v1/entries",
                params=[("tags", "x"), ("limit", "1"), ("offset", "1")],
                headers={"X-API-Key": "key-alice"},
            )
        assert len(page.json()) == 1

    async def test_list_hides_inactive_by_default(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            v1 = await post_entry(client, "key-alice", "token lifetime is 15 minutes")
            v2 = await post_entry(
                client, "key-alice", "token lifetime is 30 minutes", supersedes=[v1["id"]]
            )
            default_list = await client.get("/v1/entries", headers={"X-API-Key": "key-alice"})
            with_inactive = await client.get(
                "/v1/entries",
                params=[("include_inactive", "true")],
                headers={"X-API-Key": "key-alice"},
            )
        default_ids = [e["id"] for e in default_list.json()]
        inactive_ids = {e["id"] for e in with_inactive.json()}
        assert v2["id"] in default_ids
        assert v1["id"] not in default_ids
        assert {v1["id"], v2["id"]} <= inactive_ids


class TestWithdraw:
    async def test_author_can_withdraw_own_entry(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            entry = await post_entry(client, "key-alice", "retract me")
            resp = await client.post(
                f"/v1/entries/{entry['id']}/withdraw",
                json={"reason": "turned out to be wrong"},
                headers={"X-API-Key": "key-alice"},
            )
        assert resp.status_code == 200
        withdrawn = resp.json()
        assert withdrawn["state"] == "withdrawn"
        assert withdrawn["withdrawn_reason"] == "turned out to be wrong"

    async def test_foreign_user_cannot_withdraw(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            entry = await post_entry(client, "key-alice", "alice's entry")
            resp = await client.post(
                f"/v1/entries/{entry['id']}/withdraw",
                json={},
                headers={"X-API-Key": "key-bob"},
            )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden"

    async def test_admin_can_withdraw_any_entry(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            entry = await post_entry(client, "key-bob", "bob's entry")
            resp = await client.post(
                f"/v1/entries/{entry['id']}/withdraw",
                json={"reason": "admin correction"},
                headers={"X-API-Key": "key-admin"},
            )
        assert resp.status_code == 200
        assert resp.json()["state"] == "withdrawn"

    async def test_withdraw_unknown_entry_is_404(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            resp = await client.post(
                "/v1/entries/nope/withdraw", json={}, headers={"X-API-Key": "key-alice"}
            )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    async def test_withdraw_inactive_entry_is_409(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            entry = await post_entry(client, "key-alice", "once active")
            await client.post(
                f"/v1/entries/{entry['id']}/withdraw",
                json={},
                headers={"X-API-Key": "key-alice"},
            )
            resp = await client.post(
                f"/v1/entries/{entry['id']}/withdraw",
                json={},
                headers={"X-API-Key": "key-alice"},
            )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "conflict"


class TestFeedback:
    async def test_feedback_upsert_reports_quality(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            entry = await post_entry(client, "key-alice", "a fact bob relied on")
            resp = await client.post(
                f"/v1/entries/{entry['id']}/feedback",
                json={"verdict": "helpful", "note": "used it in the report"},
                headers={"X-API-Key": "key-bob"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["entry_id"] == entry["id"]
        assert body["verdict"] == "helpful"
        # quality = 1.0 + 0.05 (one helpful verdict; SPEC.md §4.2)
        assert body["quality"] == pytest.approx(1.05)

    async def test_feedback_unknown_entry_is_404(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            resp = await client.post(
                "/v1/entries/nope/feedback",
                json={"verdict": "helpful"},
                headers={"X-API-Key": "key-bob"},
            )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"


class TestSupersessionInvariants:
    async def test_search_hides_superseded_by_default(self) -> None:
        # A real clock advance between the two writes: the successor is
        # newer than the entry it supersedes (SPEC.md §6.3 invariant
        # orders the chain by creation time, newest first).
        clock = make_clock()
        app = create_app_for_config(
            Settings(),
            store=MemoryStore(clock),
            embedder=make_embedder(),
            authenticator=make_authenticator(),
            search_config=make_search_config(),
        )
        client = make_client(app)
        async with client:
            v1 = await post_entry(client, "key-alice", "token lifetime is 15 minutes")
            clock.advance_days(1)
            v2 = await post_entry(
                client, "key-alice", "token lifetime is 30 minutes", supersedes=[v1["id"]]
            )
            hidden = await client.post(
                "/v1/search",
                json={"query": "token lifetime"},
                headers={"X-API-Key": "key-alice"},
            )
            visible = await client.post(
                "/v1/search",
                json={"query": "token lifetime", "include_inactive": True},
                headers={"X-API-Key": "key-alice"},
            )
        hidden_ids = {h["entry_id"] for h in hidden.json()}
        visible_ids = [h["entry_id"] for h in visible.json()]
        assert v1["id"] not in hidden_ids
        assert v2["id"] in hidden_ids
        # The successor outranks the superseded entry (SPEC.md §6.3).
        assert v2["id"] in visible_ids and v1["id"] in visible_ids
        assert visible_ids.index(v2["id"]) < visible_ids.index(v1["id"])
