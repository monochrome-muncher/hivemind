"""The agent portal's endpoints (ADR 0095): ``/v1/portal/entries``,
``/v1/portal/search``, ``/v1/portal/feedback`` and
``/v1/portal/entries/{id}/withdraw``, for agent keys only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hivemind.domain.access import TrustLevel
from hivemind.domain.audit import ActorKind, AuditAction, AuditFilters
from hivemind.domain.entry import EntryDraft, EntryState, Kind
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.ports import Credential
from tests.fakes import make_embedder
from tests.unit.test_api_endpoints import make_client, make_hivemind_app

ADMIN = {"X-API-Key": "key-admin"}
ORG = {"X-API-Key": "key-org"}
DEV = {"X-API-Key": "key-alice"}  # a dev-mode identity: no agent name
CAROL = {"X-API-Key": "key-carol"}  # a contributor in blue
PAT = {"X-API-Key": "key-pat"}  # privileged in blue: reads every fleet
FAKE_ID = "00000000-0000-4000-8000-000000000001"

ROUTES = [
    ("GET", "/v1/portal/entries", None),
    ("POST", "/v1/portal/search", {"query": "kaniko"}),
    ("GET", "/v1/portal/feedback?direction=given", None),
    ("POST", f"/v1/portal/entries/{FAKE_ID}/withdraw", {"reason": "old"}),
]


async def _add(app, summary: str, **kw):  # type: ignore[no-untyped-def]
    draft = EntryDraft(
        kind=Kind.FACT, summary=summary, author=kw.pop("author", "carol"), agent="a", **kw
    )
    return await app.store.create_entry(draft, await make_embedder().embed_text(summary))


async def _seed():  # type: ignore[no-untyped-def]
    app = make_hivemind_app()
    blue = await app.store.create_fleet("blue")
    red = await app.store.create_fleet("red")
    for key, name, level in (
        ("key-carol", "carol", TrustLevel.CONTRIBUTOR),
        ("key-pat", "pat", TrustLevel.PRIVILEGED),
    ):
        app.authenticator.add_key(  # type: ignore[attr-defined]
            key,
            Credential(
                user_id=name,
                agent_name=name,
                access_controlled=True,
                trust_level=level,
                home_fleet_id=blue.id,
            ),
        )
    mine = await _add(app, "carol private note kaniko", scope="self")
    in_blue = await _add(app, "blue fleet deploy kaniko", scope="fleet", fleet_id=blue.id)
    by_bob = await _add(app, "bob blue fleet kaniko", scope="fleet", fleet_id=blue.id, author="bob")
    in_red = await _add(
        app, "red fleet deploy kaniko", scope="fleet", fleet_id=red.id, author="bob"
    )
    return app, blue, red, mine, in_blue, by_bob, in_red


@pytest.mark.parametrize("headers", [ADMIN, ORG, DEV], ids=["admin-key", "org-key", "dev-mode"])
@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
async def test_every_portal_endpoint_is_403_for_anything_but_an_agent_key(
    method, path, body, headers
) -> None:
    app = make_hivemind_app()
    async with make_client(app) as client:
        resp = await client.request(method, path, json=body, headers=headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert "agent key" in resp.json()["error"]["message"]


async def test_portal_list_reads_what_the_key_reads_and_filters_by_fleet() -> None:
    app, blue, red, mine, in_blue, by_bob, in_red = await _seed()
    async with make_client(app) as client:
        carol = await client.get("/v1/portal/entries", headers=CAROL)
        own = await client.get("/v1/portal/entries", params={"author": "carol"}, headers=CAROL)
        pat_all = await client.get("/v1/portal/entries", headers=PAT)
        pat_home = await client.get("/v1/portal/entries", params={"fleet_id": blue.id}, headers=PAT)
        pat_red = await client.get("/v1/portal/entries", params={"fleet_id": red.id}, headers=PAT)
        carol_red = await client.get(
            "/v1/portal/entries", params={"fleet_id": red.id}, headers=CAROL
        )
    assert carol.status_code == 200
    assert {e["id"] for e in carol.json()} == {mine.id, in_blue.id, by_bob.id}
    assert {e["id"] for e in own.json()} == {mine.id, in_blue.id}
    # Pat is privileged: every fleet, but not carol's private note.
    assert {e["id"] for e in pat_all.json()} == {in_blue.id, by_bob.id, in_red.id}
    assert {e["id"] for e in pat_home.json()} == {in_blue.id, by_bob.id}
    assert [e["id"] for e in pat_red.json()] == [in_red.id]
    assert carol_red.json() == []


async def test_portal_search_ranks_like_search_but_is_not_counted() -> None:
    app, blue, _red, mine, in_blue, by_bob, _in_red = await _seed()
    async with make_client(app) as client:
        hits = await client.post("/v1/portal/search", json={"query": "kaniko"}, headers=CAROL)
        home = await client.post(
            "/v1/portal/search",
            json={"query": "kaniko", "fleet_id": blue.id, "author": "bob"},
            headers=PAT,
        )
        assert await app.store.search_counts() == []
        plain = await client.post("/v1/search", json={"query": "kaniko"}, headers=CAROL)
    assert hits.status_code == 200
    assert [h["entry_id"] for h in hits.json()] == [h["entry_id"] for h in plain.json()]
    assert {h["entry_id"] for h in hits.json()} == {mine.id, in_blue.id, by_bob.id}
    assert [h["entry_id"] for h in home.json()] == [by_bob.id]
    assert sum(c.searches for c in await app.store.search_counts()) == 1


async def test_portal_feedback_lists_the_verdicts_given_and_received() -> None:
    app, _blue, _red, mine, in_blue, by_bob, in_red = await _seed()
    t0 = datetime(2026, 6, 1, tzinfo=UTC)
    # carol rated bob's entries; bob and pat rated carol's.
    await app.store.record_feedback(Feedback(by_bob.id, "carol", "a", Verdict.HELPFUL, None, t0))
    await app.store.record_feedback(
        Feedback(in_red.id, "carol", "a", Verdict.WRONG, "red port", t0 + timedelta(hours=1))
    )
    await app.store.record_feedback(
        Feedback(in_blue.id, "bob", "b", Verdict.STALE, "moved", t0 + timedelta(hours=2))
    )
    await app.store.record_feedback(
        Feedback(in_blue.id, "pat", "p", Verdict.HELPFUL, None, t0 + timedelta(hours=3))
    )
    async with make_client(app) as client:
        given = await client.get("/v1/portal/feedback?direction=given", headers=CAROL)
        received = await client.get("/v1/portal/feedback?direction=received", headers=CAROL)
        stale = await client.get(
            "/v1/portal/feedback",
            params={"direction": "received", "verdict": "stale"},
            headers=CAROL,
        )
        paged = await client.get(
            "/v1/portal/feedback",
            params={"direction": "received", "limit": 1, "offset": 1},
            headers=CAROL,
        )
        missing = await client.get("/v1/portal/feedback", headers=CAROL)
        bad = await client.get("/v1/portal/feedback?direction=sideways", headers=CAROL)
    assert given.status_code == 200
    # Carol rated a red entry once, but her key cannot read red: left out.
    assert [(r["reporter"], r["entry"]["id"]) for r in given.json()] == [("carol", by_bob.id)]
    assert [(r["reporter"], r["verdict"]) for r in received.json()] == [
        ("pat", "helpful"),
        ("bob", "stale"),
    ]
    assert [r["note"] for r in stale.json()] == ["moved"]
    assert [r["reporter"] for r in paged.json()] == ["bob"]
    assert mine.id not in {r["entry"]["id"] for r in received.json()}
    assert missing.status_code == bad.status_code == 422


async def test_portal_withdraw_takes_own_entries_with_a_reason_and_audits_them() -> None:
    app, _blue, _red, mine, in_blue, by_bob, in_red = await _seed()
    async with make_client(app) as client:
        no_reason = await client.post(
            f"/v1/portal/entries/{mine.id}/withdraw", json={}, headers=CAROL
        )
        blank = await client.post(
            f"/v1/portal/entries/{mine.id}/withdraw", json={"reason": "  "}, headers=CAROL
        )
        not_hers = await client.post(
            f"/v1/portal/entries/{by_bob.id}/withdraw", json={"reason": "no"}, headers=CAROL
        )
        unreadable = await client.post(
            f"/v1/portal/entries/{in_red.id}/withdraw", json={"reason": "no"}, headers=CAROL
        )
        # Every refusal above left its entry untouched.
        for refused in (mine, by_bob, in_red, in_blue):
            stored = await app.store.get_entry(refused.id)
            assert stored is not None and stored.state is EntryState.ACTIVE
        done = await client.post(
            f"/v1/portal/entries/{in_blue.id}/withdraw",
            json={"reason": "the port moved"},
            headers=CAROL,
        )
        again = await client.post(
            f"/v1/portal/entries/{in_blue.id}/withdraw", json={"reason": "again"}, headers=CAROL
        )
        # Her own call to the data plane stays unaudited (SPEC §12.5).
        plain = await client.post(
            f"/v1/entries/{mine.id}/withdraw", json={"reason": "mine"}, headers=CAROL
        )
    assert no_reason.status_code == blank.status_code == 422
    assert not_hers.status_code == 403
    assert unreadable.status_code == 404
    assert done.status_code == 200
    assert done.json()["state"] == "withdrawn"
    assert done.json()["withdrawn_reason"] == "the port moved"
    assert again.status_code == 409
    assert plain.status_code == 200
    records = await app.store.list_audit(AuditFilters(action=AuditAction.ENTRY_WITHDRAW), 10)
    assert [(r.actor_kind, r.actor, r.target, r.detail) for r in records] == [
        (
            ActorKind.AGENT_KEY,
            "carol",
            in_blue.id,
            {"author": "carol", "reason": "the port moved", "via": "portal"},
        )
    ]
