"""The admin-only memory endpoints (ADR 0080): ``/v1/admin/entries``,
``/v1/admin/entries/{id}``, ``/v1/admin/search`` and
``/v1/admin/entries/{id}/withdraw``."""

from __future__ import annotations

import pytest

from hivemind.domain.access import TrustLevel
from hivemind.domain.audit import AuditAction, AuditFilters
from hivemind.domain.entry import EntryDraft, Kind
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.ports import Credential
from tests.fakes import make_embedder
from tests.unit.test_api_endpoints import make_client, make_hivemind_app

ADMIN = {"X-API-Key": "key-admin"}
ALICE = {"X-API-Key": "key-alice"}
ORG = {"X-API-Key": "key-org"}
PRIVILEGED = {"X-API-Key": "key-privileged"}
FAKE_ID = "00000000-0000-4000-8000-000000000001"


async def _add(app, summary: str, **kw):  # type: ignore[no-untyped-def]
    draft = EntryDraft(
        kind=Kind.FACT, summary=summary, author=kw.pop("author", "alice"), agent="a", **kw
    )
    return await app.store.create_entry(draft, await make_embedder().embed_text(summary))


async def _seed():  # type: ignore[no-untyped-def]
    app = make_hivemind_app()
    blue = await app.store.create_fleet("blue")
    red = await app.store.create_fleet("red")
    mine = await _add(app, "alice private note kaniko", scope="self")
    in_blue = await _add(app, "blue fleet deploy kaniko", scope="fleet", fleet_id=blue.id)
    in_red = await _add(
        app, "red fleet deploy kaniko", scope="fleet", fleet_id=red.id, author="bob"
    )
    return app, blue, red, mine, in_blue, in_red


ROUTES = [
    ("GET", "/v1/admin/entries", None),
    ("GET", f"/v1/admin/entries/{FAKE_ID}", None),
    ("POST", "/v1/admin/search", {"query": "kaniko"}),
    ("POST", f"/v1/admin/entries/{FAKE_ID}/withdraw", {}),
    ("GET", "/v1/admin/activity", None),
]


@pytest.mark.parametrize(
    "headers", [ALICE, ORG, PRIVILEGED], ids=["agent-key", "org-key", "privileged-agent-key"]
)
@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
async def test_every_admin_endpoint_is_403_for_non_admins(method, path, body, headers) -> None:
    app = make_hivemind_app()
    app.authenticator.add_key(  # type: ignore[attr-defined]
        "key-privileged",
        Credential(
            user_id="pat",
            agent_name="pat",
            access_controlled=True,
            trust_level=TrustLevel.PRIVILEGED,
            home_fleet_id="fleet-a",
        ),
    )
    client = make_client(app)
    async with client:
        resp = await client.request(method, path, json=body, headers=headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


async def test_admin_list_sees_self_and_fleet_entries_of_any_fleet() -> None:
    app, *_ = await _seed()
    async with make_client(app) as client:
        resp = await client.get("/v1/admin/entries", headers=ADMIN)
        plain = await client.get("/v1/entries", headers=ADMIN)
    assert resp.status_code == 200
    assert {e["summary"] for e in resp.json()} == {
        "alice private note kaniko",
        "blue fleet deploy kaniko",
        "red fleet deploy kaniko",
    }
    assert len(plain.json()) == 3


async def test_admin_list_filters_by_fleet_and_author() -> None:
    app, _blue, red, *_ = await _seed()
    async with make_client(app) as client:
        by_fleet = await client.get("/v1/admin/entries", params={"fleet_id": red.id}, headers=ADMIN)
        by_author = await client.get("/v1/admin/entries", params={"author": "bob"}, headers=ADMIN)
        paged = await client.get("/v1/admin/entries", params={"limit": 1}, headers=ADMIN)
        too_big = await client.get("/v1/admin/entries", params={"limit": 101}, headers=ADMIN)
    assert [e["summary"] for e in by_fleet.json()] == ["red fleet deploy kaniko"]
    assert [e["author"] for e in by_author.json()] == ["bob"]
    assert len(paged.json()) == 1
    assert too_big.status_code == 422


async def test_malformed_fleet_filter_answers_empty_not_500() -> None:
    app, *_ = await _seed()
    async with make_client(app) as client:
        listed = await client.get(
            "/v1/admin/entries", params={"fleet_id": "not-a-uuid"}, headers=ADMIN
        )
        searched = await client.post(
            "/v1/admin/search",
            json={"query": "kaniko", "fleet_id": "not-a-uuid"},
            headers=ADMIN,
        )
    assert listed.status_code == searched.status_code == 200
    assert listed.json() == searched.json() == []


async def test_admin_detail_has_feedback_links_and_history() -> None:
    app, blue, _, mine, in_blue, _ = await _seed()
    newer = await app.store.create_entry(
        EntryDraft(
            kind=Kind.FACT,
            summary="blue deploy v2 kaniko",
            author="alice",
            agent="a",
            scope="fleet",
            fleet_id=blue.id,
            supersedes=(in_blue.id,),
        ),
        await make_embedder().embed_text("blue deploy v2 kaniko"),
    )
    await app.store.record_feedback(Feedback(in_blue.id, "bob", "bob", Verdict.STALE, "old"))
    async with make_client(app) as client:
        resp = await client.get(f"/v1/admin/entries/{in_blue.id}", headers=ADMIN)
        private = await client.get(f"/v1/admin/entries/{mine.id}", headers=ADMIN)
        missing = await client.get(f"/v1/admin/entries/{FAKE_ID}", headers=ADMIN)
        malformed = await client.get("/v1/admin/entries/not-an-id", headers=ADMIN)
    body = resp.json()
    assert body["feedback"]["stale"] == 1
    assert [e["id"] for e in body["history"]["successors"]] == [newer.id]
    assert body["history"]["superseded"] == []
    assert body["see_also"] == [] and body["linked_from"] == []
    assert private.status_code == 200
    assert missing.status_code == malformed.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


async def test_admin_search_sees_every_fleet_and_is_not_counted() -> None:
    app, _blue, red, *_ = await _seed()
    async with make_client(app) as client:
        everything = await client.post("/v1/admin/search", json={"query": "kaniko"}, headers=ADMIN)
        one_fleet = await client.post(
            "/v1/admin/search", json={"query": "kaniko", "fleet_id": red.id}, headers=ADMIN
        )
        assert await app.store.search_counts() == []
        counted = await client.post("/v1/search", json={"query": "kaniko"}, headers=ADMIN)
    assert everything.status_code == 200
    assert len(everything.json()) == 3
    assert [h["summary"] for h in one_fleet.json()] == ["red fleet deploy kaniko"]
    assert counted.status_code == 200
    assert sum(c.searches for c in await app.store.search_counts()) == 1


async def test_admin_withdraw_works_is_audited_and_conflicts_the_second_time() -> None:
    app, _, _, _, in_blue, _ = await _seed()
    async with make_client(app) as client:
        first = await client.post(
            f"/v1/admin/entries/{in_blue.id}/withdraw", json={"reason": "wrong"}, headers=ADMIN
        )
        second = await client.post(
            f"/v1/admin/entries/{in_blue.id}/withdraw", json={}, headers=ADMIN
        )
        unknown = await client.post(f"/v1/admin/entries/{FAKE_ID}/withdraw", json={}, headers=ADMIN)
    assert first.status_code == 200
    assert first.json()["state"] == "withdrawn"
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conflict"
    assert unknown.status_code == 404
    records = await app.store.list_audit(AuditFilters(action=AuditAction.ENTRY_WITHDRAW), 10)
    assert [r.target for r in records] == [in_blue.id]
