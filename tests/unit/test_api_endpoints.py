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
from hivemind.domain.access import TrustLevel
from hivemind.domain.entry import EntityKind, EntryDraft, ExtractedEntity, Kind
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from tests.fakes import make_clock, make_embedder, make_search_config


class FakeAuthenticator:
    """Dict-backed Authenticator (SPEC.md §8.1, ADR 0012): user + agent
    sub-keys + key management (the full ``Authenticator`` port)."""

    def __init__(self, keys: dict[str, Credential]) -> None:
        self._keys = keys
        self._org_key: str | None = None

    async def verify(self, key: str) -> Credential | None:
        return self._keys.get(key)

    async def issue_agent_key(self, agent_name: str) -> str:
        raw_key = f"hm_agent_{agent_name}"
        self._keys[raw_key] = Credential(
            user_id=agent_name, agent_name=agent_name, access_controlled=True
        )
        return raw_key

    async def revoke_agent_key(self, agent_name: str) -> None:
        raw_key = f"hm_agent_{agent_name}"
        self._keys.pop(raw_key, None)

    async def rotate_org_key(self) -> str:
        if self._org_key is not None:
            self._keys.pop(self._org_key, None)
        self._org_key = "hm_org"
        self._keys[self._org_key] = Credential(user_id="org", is_org=True, access_controlled=True)
        return self._org_key

    def add_key(self, raw_key: str, credential: Credential) -> None:
        """Test helper: register a credential under a raw key directly."""
        self._keys[raw_key] = credential

    async def issue_admin_key(self) -> str:
        raw_key = "hm_admin_new"
        self._keys[raw_key] = Credential(user_id="admin", is_admin=True)
        return raw_key


def make_authenticator() -> FakeAuthenticator:
    return FakeAuthenticator(
        keys={
            "key-alice": Credential(user_id="alice", agent_id="agent-alice"),
            "key-bob": Credential(user_id="bob", agent_id="agent-bob"),
            "key-alice-user": Credential(user_id="alice"),
            "key-admin": Credential(user_id="ops", agent_id="admin-agent", is_admin=True),
            # v2 access-model keys (ADRs 0011-0012):
            "key-org": Credential(user_id="org", is_org=True, access_controlled=True),
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


def make_app_with_clock(clock) -> HivemindApp:
    """A HivemindApp over a caller-controlled clock (for created_at tests)."""
    return create_app_for_config(
        Settings(),
        store=MemoryStore(clock),
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


# --------------------------------------------------------------------------- #
# Review-fix seams (SPEC.md §5.1/§5.3/§6.4/§8.1)
# --------------------------------------------------------------------------- #
class TestHistoryChain:
    async def test_get_with_history_returns_successors_and_superseded(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            r1 = await post_entry(client, "key-alice", "token TTL v1", body="15 min")
            r2 = await post_entry(
                client,
                "key-alice",
                "token TTL v2",
                body="30 min",
                supersedes=[r1["id"]],
            )
            # Fetch the oldest with history -> it should list its successor.
            resp = await client.get(
                f"/v1/entries/{r1['id']}",
                params={"history": "true"},
                headers={"X-API-Key": "key-bob"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["history"] is not None
        assert [e["id"] for e in body["history"]["successors"]] == [r2["id"]]
        assert body["history"]["superseded"] == []

    async def test_get_without_history_omits_chain(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            r1 = await post_entry(client, "key-alice", "v1")
            await post_entry(client, "key-alice", "v2", supersedes=[r1["id"]])
            resp = await client.get(f"/v1/entries/{r1['id']}", headers={"X-API-Key": "key-bob"})
        assert resp.status_code == 200
        assert resp.json().get("history") in (None, {})


class TestSearchFiltersAndPaging:
    async def test_search_offset_paginates(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            for i in range(5):
                await post_entry(client, "key-alice", f"cohort note number {i}")
            resp = await client.post(
                "/v1/search",
                json={"query": "cohort note", "limit": 2, "offset": 1},
                headers={"X-API-Key": "key-bob"},
            )
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    async def test_search_created_range_filters(self) -> None:
        """SPEC.md §5.3: ``created_from``/``created_to`` are created_at (store time)."""
        from datetime import timedelta

        from tests.fakes import FIXED_NOW, make_clock

        clock = make_clock()
        app = make_app_with_clock(clock)
        client = make_client(app)
        async with client:
            old = await post_entry(client, "key-alice", "old cohort insight")
            clock.advance_days(10)
            new = await post_entry(client, "key-alice", "new cohort insight")
            resp = await client.post(
                "/v1/search",
                json={
                    "query": "cohort insight",
                    "created_from": (FIXED_NOW + timedelta(days=5)).isoformat(),
                },
                headers={"X-API-Key": "key-bob"},
            )
        hits = resp.json()
        ids = {h["entry_id"] for h in hits}
        assert new["id"] in ids
        assert old["id"] not in ids

    async def test_list_created_range_filters(self) -> None:
        """SPEC.md §5.3: ``created_from``/``created_to`` on the list endpoint."""
        from datetime import timedelta

        from tests.fakes import FIXED_NOW, make_clock

        clock = make_clock()
        app = make_app_with_clock(clock)
        client = make_client(app)
        async with client:
            old = await post_entry(client, "key-alice", "old entry")
            clock.advance_days(10)
            new = await post_entry(client, "key-alice", "new entry")
            resp = await client.get(
                "/v1/entries",
                params={
                    "created_from": (FIXED_NOW + timedelta(days=5)).isoformat(),
                    "limit": "50",
                },
                headers={"X-API-Key": "key-bob"},
            )
        ids = {e["id"] for e in resp.json()}
        assert new["id"] in ids
        assert old["id"] not in ids


class TestFeedbackAgentResolution:
    async def test_user_key_self_reports_agent(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            e = await post_entry(client, "key-alice", "a fact")
            resp = await client.post(
                f"/v1/entries/{e['id']}/feedback",
                json={"verdict": "helpful", "agent": "claude-code"},
                headers={"X-API-Key": "key-alice-user"},
            )
        assert resp.status_code == 200
        assert resp.json()["verdict"] == "helpful"

    async def test_user_key_without_agent_is_422(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            e = await post_entry(client, "key-alice", "a fact")
            resp = await client.post(
                f"/v1/entries/{e['id']}/feedback",
                json={"verdict": "stale"},
                headers={"X-API-Key": "key-alice-user"},
            )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "agent_identity_required"


class TestEmbeddingFailure:
    async def test_create_maps_embedding_error_to_502(self) -> None:
        from hivemind.api.main import create_app_for_config
        from hivemind.config import Settings
        from hivemind.embeddings import EmbeddingError
        from hivemind.memstore import MemoryStore
        from tests.fakes import make_clock, make_search_config

        class ExplodingEmbedder:
            dimension = 4

            @property
            def model_name(self) -> str:
                return "exploding"

            async def embed_text(self, text):
                raise EmbeddingError("embedding endpoint is down")

            async def embed_entry(self, draft):
                raise EmbeddingError("embedding endpoint is down")

            def entry_embeddable_text(self, draft) -> str:
                return draft.summary

        app = create_app_for_config(
            Settings(),
            store=MemoryStore(make_clock()),
            embedder=ExplodingEmbedder(),
            authenticator=make_authenticator(),
            search_config=make_search_config(),
        )
        client = make_client(app)
        async with client:
            resp = await client.post(
                "/v1/entries",
                json={"kind": "fact", "summary": "needs an embedding", "agent": "a"},
                headers={"X-API-Key": "key-alice-user"},
            )
        assert resp.status_code == 502
        assert resp.json()["error"]["code"] == "embedding_unavailable"


class TestNaiveDatetimeNormalization:
    async def test_naive_occurred_at_normalized_to_utc(self) -> None:
        from hivemind.api.schemas import CreateEntryRequest

        aware = CreateEntryRequest(
            kind="fact",
            summary="x",
            agent="a",
            occurred_at="2026-06-01T12:00:00",  # naive
        )
        assert aware.occurred_at is not None
        assert aware.occurred_at.tzinfo is not None
        assert aware.occurred_at.utcoffset() is not None


class TestAccessEndpoints:
    """Agent registration + fleet/trust management (ADRs 0011-0012, SPEC §12)."""

    async def test_register_agent_with_org_key(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            resp = await client.post(
                "/v1/agents",
                json={"name": "alice"},
                headers={"X-API-Key": "key-org"},
            )
        assert resp.status_code == 201
        agent = resp.json()
        assert agent["name"] == "alice"
        assert agent["status"] == "pending"
        assert agent["trust_level"] == 0

    async def test_register_agent_with_agent_key_denied(self) -> None:
        # An agent key (not org/admin) may not register (ADR 0012).
        client = make_client(make_hivemind_app())
        async with client:
            resp = await client.post(
                "/v1/agents",
                json={"name": "alice"},
                headers={"X-API-Key": "key-alice"},
            )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden"

    async def test_create_fleet_admin_only(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            ok = await client.post(
                "/v1/admin/fleets",
                json={"name": "data-eng"},
                headers={"X-API-Key": "key-admin"},
            )
            assert ok.status_code == 201
            assert ok.json()["name"] == "data-eng"
            denied = await client.post(
                "/v1/admin/fleets",
                json={"name": "ml"},
                headers={"X-API-Key": "key-org"},
            )
        assert denied.status_code == 403

    async def test_activate_agent_issues_key_once(self) -> None:
        app = make_hivemind_app()
        # Seed: a pending agent + a home fleet.
        await app.store.register_agent("alice")
        fleet = await app.store.create_fleet("data-eng")
        client = make_client(app)
        async with client:
            resp = await client.post(
                "/v1/admin/agents/alice/activate",
                json={"trust_level": 2, "home_fleet_id": fleet.id},
                headers={"X-API-Key": "key-admin"},
            )
        assert resp.status_code == 200
        assert resp.json()["key"]  # the raw key is returned once
        # The agent is now active at level 2 with the home fleet.
        agent = await app.store.get_agent("alice")
        assert agent is not None
        assert agent.status.value == "active"
        assert agent.trust_level.value == 2

    async def test_set_trust_level_demotes(self) -> None:
        app = make_hivemind_app()
        await app.store.register_agent("alice")
        fleet = await app.store.create_fleet("data-eng")
        await app.store.activate_agent(
            "alice", trust_level=TrustLevel.PRIVILEGED, home_fleet_id=fleet.id
        )
        client = make_client(app)
        async with client:
            resp = await client.patch(
                "/v1/admin/agents/alice",
                json={"trust_level": 0},
                headers={"X-API-Key": "key-admin"},
            )
        assert resp.status_code == 200
        # Demotion to level 0 is *dormant* (still active, no access).
        assert resp.json()["trust_level"] == 0
        assert resp.json()["status"] == "active"

    async def test_patch_home_fleet_reparents(self) -> None:
        app = make_hivemind_app()
        await app.store.register_agent("alice")
        fa = await app.store.create_fleet("data-eng")
        fb = await app.store.create_fleet("ml")
        await app.store.activate_agent(
            "alice", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=fa.id
        )
        client = make_client(app)
        async with client:
            resp = await client.patch(
                "/v1/admin/agents/alice",
                json={"home_fleet_id": fb.id},
                headers={"X-API-Key": "key-admin"},
            )
        assert resp.status_code == 200
        assert resp.json()["home_fleet_id"] == fb.id

    async def test_patch_empty_body_is_422(self) -> None:
        app = make_hivemind_app()
        await app.store.register_agent("alice")
        client = make_client(app)
        async with client:
            resp = await client.patch(
                "/v1/admin/agents/alice",
                json={},
                headers={"X-API-Key": "key-admin"},
            )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "update_required"

    async def test_revoke_agent_admin_only(self) -> None:
        app = make_hivemind_app()
        await app.store.register_agent("alice")
        client = make_client(app)
        async with client:
            denied = await client.post(
                "/v1/admin/agents/alice/revoke", headers={"X-API-Key": "key-org"}
            )
            assert denied.status_code == 403
            ok = await client.post(
                "/v1/admin/agents/alice/revoke", headers={"X-API-Key": "key-admin"}
            )
        assert ok.status_code == 200

    async def test_rotate_org_key_admin_only(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            denied = await client.post("/v1/admin/org-key/rotate", headers={"X-API-Key": "key-org"})
            assert denied.status_code == 403
            ok = await client.post("/v1/admin/org-key/rotate", headers={"X-API-Key": "key-admin"})
        assert ok.status_code == 200
        assert ok.json()["key"]  # a new raw org key is returned once

    # --- write-scope resolution (ADR 0011): an omitted scope defaults to
    # --- the highest scope the trust level permits, not a forced 'org' --

    async def _provision_l2_agent(self, app: HivemindApp, name: str = "carol") -> tuple[str, str]:
        """Provision an L2 (contributor) agent + fleet + key (ADRs 0011-0012)."""
        fleet = await app.store.create_fleet("eng")
        await app.store.register_agent(name)
        await app.store.activate_agent(
            name, trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=fleet.id
        )
        key = f"key-{name}"
        app.authenticator.add_key(
            key,
            Credential(
                user_id=name,
                agent_id=name,
                agent_name=name,
                access_controlled=True,
                trust_level=TrustLevel.CONTRIBUTOR,
                home_fleet_id=fleet.id,
            ),
        )
        return key, fleet.id

    async def test_create_entry_l2_omitted_scope_defaults_to_fleet(self) -> None:
        """ADR 0011: an omitted scope defaults to the highest scope the
        trust level permits (L2 -> fleet). The REST schema must not force
        'org' — that rejected every L2/L1 write (dogfood finding 1)."""
        app = make_hivemind_app()
        key, fleet_id = await self._provision_l2_agent(app)
        client = make_client(app)
        async with client:
            entry = await post_entry(client, key, "omitted scope probe")
            assert entry["scope"] == "fleet"
            assert entry["fleet_id"] == fleet_id

    async def test_create_entry_l2_explicit_org_scope_rejected(self) -> None:
        """An *explicit* out-of-permission scope is still rejected (403)."""
        app = make_hivemind_app()
        key, _ = await self._provision_l2_agent(app)
        client = make_client(app)
        async with client:
            resp = await client.post(
                "/v1/entries",
                json={"kind": "fact", "summary": "org probe", "scope": "org"},
                headers={"X-API-Key": key},
            )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden"

    async def test_create_entry_legacy_omitted_scope_stays_org(self) -> None:
        """Legacy (v1) credentials keep the flat pool: omitted scope -> org."""
        client = make_client(make_hivemind_app())
        async with client:
            entry = await post_entry(client, "key-alice", "legacy probe")
            assert entry["scope"] == "org"


class TestMetricsEndpoint:
    """GET /v1/metrics: the minimal usage-counters surface (ROADMAP §3.3)."""

    async def test_metrics_reports_usage_counters(self) -> None:
        app = make_hivemind_app()
        # Seed: two fleets with writes, one pending agent, one active.
        store = app.store
        f1 = await store.create_fleet("data-eng")
        await store.create_entry(
            EntryDraft(
                kind="fact", summary="s1", author="alice", agent="a1", scope="fleet", fleet_id=f1.id
            ),
            [0.1, 0.1, 0.1, 0.1],
        )
        await store.create_entry(
            EntryDraft(kind="insight", summary="s2", author="bob", agent="b1", scope="org"),
            [0.1, 0.1, 0.1, 0.1],
        )
        await store.register_agent("pending-agent")
        client = make_client(app)
        async with client:
            resp = await client.get("/v1/metrics", headers={"X-API-Key": "key-admin"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["entries"]["total"] == 2
        assert data["entries"]["by_scope"] == {"fleet": 1, "org": 1}
        assert data["fleets"]["total"] == 1
        assert data["fleets"]["writes_by_fleet"] == {"data-eng": 1}
        assert data["agents"]["total"] == 1
        assert data["agents"]["pending"] == 1

    async def test_metrics_requires_admin_key(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            denied = await client.get("/v1/metrics", headers={"X-API-Key": "key-alice"})
            assert denied.status_code == 403
            assert denied.json()["error"]["code"] == "forbidden"


# --------------------------------------------------------------------------- #
# Entity facets (ADR 0016, SPEC §13) ------------------------------------------ #


async def _seed_entity_entries(app: HivemindApp) -> dict[str, str]:
    """Seed two entries carrying machine-extracted entity facets (ADR 0016)."""
    a = await app.store.create_entry(
        EntryDraft(
            kind=Kind.FACT,
            summary="Postgres connection pool exhausted",
            author="alice",
            agent="a1",
        ),
        entities=(
            ExtractedEntity(name="Postgres", kind=EntityKind.SYSTEM),
            ExtractedEntity(name="auth-service", kind=EntityKind.SERVICE),
        ),
        entities_model="test-extractor",
    )
    b = await app.store.create_entry(
        EntryDraft(
            kind=Kind.FACT,
            summary="Kubernetes autoscaler misconfigured",
            author="alice",
            agent="a1",
        ),
        entities=(ExtractedEntity(name="Kubernetes", kind=EntityKind.SYSTEM),),
        entities_model="test-extractor",
    )
    return {"a": a.id, "b": b.id}


class TestEntitiesSurface:
    """Entity facets (ADR 0016, SPEC §13): machine-extracted entities
    are filterable by name (AND-semantics, case-insensitive) and exposed
    on full-entry reads; compact hits stay slim; writes are machine-only
    (an agent declares facets via tags, never via a write request)."""

    async def test_list_filters_by_entities_and_case_insensitively(self) -> None:
        app = make_hivemind_app()
        ids = await _seed_entity_entries(app)
        client = make_client(app)
        async with client:
            # Filter names are case-insensitive against the stored names.
            resp = await client.get(
                "/v1/entries",
                params=[("entities", "POSTGRES")],
                headers={"X-API-Key": "key-alice"},
            )
            assert [e["id"] for e in resp.json()] == [ids["a"]]
            # AND-semantics: every listed name must be on the entry.
            both = await client.get(
                "/v1/entries",
                params=[("entities", "postgres"), ("entities", "auth-service")],
                headers={"X-API-Key": "key-alice"},
            )
            assert [e["id"] for e in both.json()] == [ids["a"]]
            missing = await client.get(
                "/v1/entries",
                params=[("entities", "postgres"), ("entities", "kubernetes")],
                headers={"X-API-Key": "key-alice"},
            )
            assert missing.json() == []

    async def test_search_filters_by_entities(self) -> None:
        app = make_hivemind_app()
        ids = await _seed_entity_entries(app)
        client = make_client(app)
        async with client:
            resp = await client.post(
                "/v1/search",
                json={"query": "postgres pool", "entities": ["postgres"]},
                headers={"X-API-Key": "key-alice"},
            )
            assert [h["entry_id"] for h in resp.json()] == [ids["a"]]
            # AND-semantics: no entry carries both names.
            none = await client.post(
                "/v1/search",
                json={"query": "pool", "entities": ["postgres", "kubernetes"]},
                headers={"X-API-Key": "key-alice"},
            )
            assert none.json() == []

    async def test_get_entry_exposes_entities_and_provenance(self) -> None:
        app = make_hivemind_app()
        ids = await _seed_entity_entries(app)
        client = make_client(app)
        async with client:
            resp = await client.get(f"/v1/entries/{ids['a']}", headers={"X-API-Key": "key-alice"})
        entry = resp.json()
        assert entry["entities"] == [
            {"name": "Postgres", "kind": "system"},
            {"name": "auth-service", "kind": "service"},
        ]
        assert entry["entities_model"] == "test-extractor"

    async def test_get_entry_without_extraction_defaults_to_empty(self) -> None:
        client = make_client(make_hivemind_app())
        async with client:
            created = await post_entry(client, "key-alice", "a plain entry")
            resp = await client.get(
                f"/v1/entries/{created['id']}", headers={"X-API-Key": "key-alice"}
            )
        entry = resp.json()
        assert entry["entities"] == []
        assert entry["entities_model"] is None

    async def test_search_hits_do_not_carry_entities(self) -> None:
        """Progressive disclosure (SPEC §6.1): compact hits stay slim."""
        app = make_hivemind_app()
        await _seed_entity_entries(app)
        client = make_client(app)
        async with client:
            resp = await client.post(
                "/v1/search", json={"query": "postgres pool"}, headers={"X-API-Key": "key-alice"}
            )
        hits = resp.json()
        assert len(hits) >= 1
        for hit in hits:
            assert "entities" not in hit
            assert "entities_model" not in hit

    async def test_write_requests_have_no_entities_field(self) -> None:
        """Machine-only (ADR 0016): a write request carrying ``entities``
        is not a facet channel — the machine is the single writer (the
        extra field is ignored; tags remain the agent's channel)."""
        client = make_client(make_hivemind_app())
        async with client:
            resp = await client.post(
                "/v1/entries",
                json={
                    "kind": "fact",
                    "summary": "no facets",
                    "entities": [{"name": "redis", "kind": "system"}],
                },
                headers={"X-API-Key": "key-alice"},
            )
        assert resp.status_code == 201
        assert resp.json()["entities"] == []
