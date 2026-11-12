"""The agent portal's endpoints (ADR 0095): ``/v1/portal/entries``,
``/v1/portal/search``, ``/v1/portal/feedback`` and
``/v1/portal/entries/{id}/withdraw``, for agent keys only."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from hivemind.domain.access import TrustLevel
from hivemind.domain.audit import ActorKind, AuditAction, AuditFilters
from hivemind.domain.entry import EntryDraft, EntryState, Kind
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.ports import Credential
from tests.fakes import FIXED_NOW, make_embedder
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
    ("GET", "/v1/portal/flagged", None),
    ("GET", "/v1/portal/relied-on", None),
    ("POST", f"/v1/portal/entries/{FAKE_ID}/withdraw", {"reason": "old"}),
    ("POST", f"/v1/portal/entries/{FAKE_ID}/move-to-self", {"reason": "mine"}),
    ("POST", "/v1/portal/cleanup", {"since": "2026-10-01T00:00:00Z", "dry_run": True}),
    ("GET", "/v1/portal/cleanups", None),
    ("GET", "/v1/portal/history", None),
    ("POST", f"/v1/portal/cleanups/{FAKE_ID}/undo", None),
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


async def test_portal_list_and_search_take_an_exact_state() -> None:
    app, _blue, _red, _mine, in_blue, _by_bob, _in_red = await _seed()
    await app.store.withdraw_entry(in_blue.id, None, by_user="carol")
    async with make_client(app) as client:
        listed = await client.get(
            "/v1/portal/entries", params={"state": "withdrawn"}, headers=CAROL
        )
        searched = await client.post(
            "/v1/portal/search", json={"query": "kaniko", "state": "withdrawn"}, headers=CAROL
        )
    assert [e["id"] for e in listed.json()] == [in_blue.id]
    assert [(h["entry_id"], h["state"]) for h in searched.json()] == [(in_blue.id, "withdrawn")]


async def test_new_portal_list_filters_stay_inside_the_keys_visibility() -> None:
    app, _blue, red, mine, in_blue, by_bob, in_red = await _seed()
    wide = {
        "occurred_from": "2000-01-01T00:00:00Z",
        "occurred_to": "2100-01-01T00:00:00Z",
        "created_from": "2000-01-01T00:00:00Z",
        "created_to": "2100-01-01T00:00:00Z",
    }
    async with make_client(app) as client:
        carol = await client.get("/v1/portal/entries", params=wide, headers=CAROL)
        carol_red = await client.get(
            "/v1/portal/entries", params={**wide, "fleet_id": red.id}, headers=CAROL
        )
        carol_state = await client.get(
            "/v1/portal/entries", params={**wide, "state": "active"}, headers=CAROL
        )
        pat = await client.get("/v1/portal/entries", params=wide, headers=PAT)
        pat_by_carol = await client.get(
            "/v1/portal/entries", params={**wide, "author": "carol"}, headers=PAT
        )
        none = await client.get(
            "/v1/portal/entries",
            params={"entities": "nothing-here", "created_to": "2000-01-01T00:00:00Z"},
            headers=PAT,
        )
    assert {e["id"] for e in carol.json()} == {mine.id, in_blue.id, by_bob.id}
    assert carol_red.json() == []
    assert {e["id"] for e in carol_state.json()} == {mine.id, in_blue.id, by_bob.id}
    assert {e["id"] for e in pat.json()} == {in_blue.id, by_bob.id, in_red.id}
    assert mine.id not in {e["id"] for e in pat_by_carol.json()}
    assert none.json() == []


async def test_portal_list_rows_carry_their_verdict_counts() -> None:
    """The portal's Flags column reads them; a row nobody rated has none, as a search hit."""
    app, _blue, _red, mine, in_blue, _by_bob, _in_red = await _seed()
    await app.store.record_feedback(Feedback(in_blue.id, "bob", "bob", Verdict.WRONG, "bad port"))
    async with make_client(app) as client:
        resp = await client.get("/v1/portal/entries", params={"author": "carol"}, headers=CAROL)
    rows = {e["id"]: e for e in resp.json()}
    counts = rows[in_blue.id]["feedback"]
    assert (counts["helpful"], counts["stale"], counts["wrong"]) == (0, 0, 1)
    assert rows[mine.id]["feedback"] is None


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


async def test_portal_search_explains_its_ranking_only_when_asked() -> None:
    app, *_ = await _seed()
    async with make_client(app) as client:
        plain = await client.post("/v1/portal/search", json={"query": "kaniko"}, headers=CAROL)
        explained = await client.post(
            "/v1/portal/search", json={"query": "kaniko", "explain": True}, headers=CAROL
        )
    assert all("explain" not in h for h in plain.json())
    stripped = [{k: v for k, v in h.items() if k != "explain"} for h in explained.json()]
    assert stripped == plain.json()
    for hit in explained.json():
        e = hit["explain"]
        product = (
            e["fused"] * e["importance"]["factor"] * e["recency"]["factor"] * e["social"]["factor"]
        )
        assert hit["score"] == pytest.approx(product, rel=1e-3)


async def test_portal_explain_ranks_count_only_what_the_key_can_read() -> None:
    app, _blue, red, *_ = await _seed()
    # Newer memories the key cannot read, which would rank first if the streams counted them.
    for i in range(5):
        await _add(
            app, f"kaniko kaniko kaniko red {i}", scope="fleet", fleet_id=red.id, author="bob"
        )
        await _add(app, f"kaniko kaniko kaniko bob note {i}", scope="self", author="bob")
    async with make_client(app) as client:
        hits = await client.post(
            "/v1/portal/search",
            json={"query": "kaniko", "explain": True, "limit": 50},
            headers=CAROL,
        )
    readable = len(hits.json())
    assert readable == 3
    for hit in hits.json():
        for rank in (hit["explain"]["keyword_rank"], hit["explain"]["vector_rank"]):
            assert rank is None or 1 <= rank <= readable


async def test_the_data_plane_search_still_ignores_explain() -> None:
    app, *_ = await _seed()
    async with make_client(app) as client:
        hits = await client.post(
            "/v1/search", json={"query": "kaniko", "explain": True}, headers=CAROL
        )
    assert hits.status_code == 200 and all("explain" not in h for h in hits.json())


async def test_portal_flagged_lists_the_agents_own_and_inherited_flagged_memories() -> None:
    """ADR 0115: only active entries the caller owns (its name plus the
    agents it inherits from), oldest flag first, bounded by what it reads."""
    app, blue, _red, mine, in_blue, by_bob, in_red = await _seed()
    old = await _add(app, "carol predecessor note", scope="fleet", fleet_id=blue.id, author="old")
    gone = await _add(app, "carol withdrawn note", scope="fleet", fleet_id=blue.id)
    await app.store.withdraw_entry(gone.id, None, by_user="carol")
    now = datetime.now(UTC)
    for entry, days, verdict in (
        (in_blue, 2, Verdict.STALE),
        (mine, 9, Verdict.WRONG),
        (by_bob, 30, Verdict.WRONG),
        (in_red, 30, Verdict.WRONG),
        (old, 5, Verdict.STALE),
        (gone, 40, Verdict.WRONG),
    ):
        await app.store.record_feedback(
            Feedback(entry.id, "zed", "zed", verdict, None, now - timedelta(days=days))
        )
    heir = Credential(
        user_id="carol",
        agent_name="carol",
        access_controlled=True,
        trust_level=TrustLevel.CONTRIBUTOR,
        home_fleet_id=blue.id,
        inherits_from=("old",),
    )
    app.authenticator.add_key("key-heir", heir)  # type: ignore[attr-defined]
    async with make_client(app) as client:
        plain = await client.get("/v1/portal/flagged", headers=CAROL)
        inheriting = await client.get("/v1/portal/flagged", headers={"X-API-Key": "key-heir"})
        paged = await client.get("/v1/portal/flagged", params={"limit": 1}, headers=CAROL)
        pat = await client.get("/v1/portal/flagged", headers=PAT)
    assert plain.status_code == 200
    assert [r["id"] for r in plain.json()] == [mine.id, in_blue.id]
    assert [r["id"] for r in inheriting.json()] == [mine.id, old.id, in_blue.id]
    first = plain.json()[0]
    assert (first["stale"], first["wrong"], first["author"]) == (0, 1, "carol")
    assert [r["id"] for r in paged.json()] == [mine.id]
    assert pat.json() == []  # pat owns nothing, though it reads the fleet


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
        # Her own call to the data plane is audited too, as the author (ADR 0141).
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
            mine.id,
            {"author": "carol", "reason": "mine", "via": "author"},
        ),
        (
            ActorKind.AGENT_KEY,
            "carol",
            in_blue.id,
            {"author": "carol", "reason": "the port moved", "via": "portal"},
        ),
    ]


async def _relied_seed():  # type: ignore[no-untyped-def]
    """Carol (blue contributor, agent id set as a real key has) rated entries helpful two days ago;
    since then one was superseded by bob, one withdrawn, one flagged by zed; one is unchanged and
    one she rated stale."""
    app = make_hivemind_app()
    blue = await app.store.create_fleet("blue")
    red = await app.store.create_fleet("red")
    carol = Credential(
        user_id="carol",
        agent_id="carol",
        agent_name="carol",
        access_controlled=True,
        trust_level=TrustLevel.CONTRIBUTOR,
        home_fleet_id=blue.id,
    )
    app.authenticator.add_key("key-carol", carol)  # type: ignore[attr-defined]
    app.authenticator.add_key(  # type: ignore[attr-defined]
        "key-pat", replace(carol, user_id="pat", agent_id="pat", agent_name="pat")
    )
    then = FIXED_NOW - timedelta(days=2)
    seeded = {}
    for name in ("replaced", "gone", "flagged", "quiet", "rated_stale"):
        entry = await _add(app, f"{name} kaniko", scope="fleet", fleet_id=blue.id, author="bob")
        verdict = Verdict.STALE if name == "rated_stale" else Verdict.HELPFUL
        await app.store.record_feedback(Feedback(entry.id, "carol", "carol", verdict, None, then))
        seeded[name] = entry
    successor = await _add(
        app,
        "replaced kaniko v2",
        scope="fleet",
        fleet_id=blue.id,
        author="bob",
        supersedes=(seeded["replaced"].id,),
    )
    await app.store.withdraw_entry(seeded["gone"].id, "obsolete", by_user="bob")
    flag_at = FIXED_NOW - timedelta(days=1)
    for who, verdict in (("zed", Verdict.STALE), ("yan", Verdict.WRONG), ("xia", Verdict.WRONG)):
        await app.store.record_feedback(
            Feedback(seeded["flagged"].id, who, who, verdict, None, flag_at)
        )
    return app, seeded, successor, red


async def test_portal_relied_on_lists_what_the_agent_rated_helpful_that_changed() -> None:
    app, seeded, successor, _red = await _relied_seed()
    async with make_client(app) as client:
        response = await client.get("/v1/portal/relied-on", headers=CAROL)
        entries = await client.get("/v1/entries", params={"relied_on_changed": True}, headers=CAROL)
    assert response.status_code == 200
    rows = {r["id"]: r for r in response.json()}
    assert set(rows) == {seeded[n].id for n in ("replaced", "gone", "flagged")}
    assert set(rows) == {r["id"] for r in entries.json()}  # the same rows as the entries filter
    replaced = rows[seeded["replaced"].id]["change"]
    assert replaced["superseded"] and replaced["superseded_by"] == successor.id
    assert replaced["withdrawn"] is False and replaced["flagged_since"] is None
    assert replaced["rated_helpful_at"]
    gone = rows[seeded["gone"].id]
    assert gone["change"]["withdrawn"] and gone["change"]["withdrawn_at"]
    assert gone["withdrawn_reason"] == "obsolete" and gone["state"] == "withdrawn"
    flagged = rows[seeded["flagged"].id]
    assert flagged["change"]["flagged_since"] == {"stale": 1, "wrong": 2}
    assert flagged["change"]["superseded_by"] is None
    assert [flagged["feedback"][v] for v in ("helpful", "stale", "wrong")] == [1, 1, 2]


async def test_portal_relied_on_hides_a_successor_the_key_cannot_read() -> None:
    app, _seeded, _successor, red = await _relied_seed()
    old = await _add(app, "ports kaniko", scope="org", author="bob")
    rated = FIXED_NOW - timedelta(days=2)
    await app.store.record_feedback(
        Feedback(old.id, "carol", "carol", Verdict.HELPFUL, None, rated)
    )
    await _add(
        app, "ports kaniko v2", scope="fleet", fleet_id=red.id, author="bob", supersedes=(old.id,)
    )
    async with make_client(app) as client:
        rows = {
            r["id"]: r for r in (await client.get("/v1/portal/relied-on", headers=CAROL)).json()
        }
    change = rows[old.id]["change"]
    assert change["superseded"] is True and change["superseded_by"] is None


async def test_portal_relied_on_pages_and_filters_by_kind() -> None:
    app, _seeded, _successor, _red = await _relied_seed()
    async with make_client(app) as client:
        everything = (await client.get("/v1/portal/relied-on", headers=CAROL)).json()
        first = await client.get("/v1/portal/relied-on", params={"limit": 2}, headers=CAROL)
        rest = await client.get(
            "/v1/portal/relied-on", params={"limit": 2, "offset": 2}, headers=CAROL
        )
        decisions = await client.get(
            "/v1/portal/relied-on", params={"kind": "decision"}, headers=CAROL
        )
        facts = await client.get("/v1/portal/relied-on", params={"kind": "fact"}, headers=CAROL)
        nothing = await client.get("/v1/portal/relied-on", headers=PAT)
        bad = await client.get("/v1/portal/relied-on", params={"limit": 0}, headers=CAROL)
    assert [r["id"] for r in first.json() + rest.json()] == [r["id"] for r in everything]
    assert len(first.json()) == 2 and len(rest.json()) == 1
    assert decisions.json() == []
    assert len(facts.json()) == 3
    assert nothing.json() == []  # pat rated nothing
    assert bad.status_code == 422
