"""The admin-only memory endpoints (ADR 0080): ``/v1/admin/entries``,
``/v1/admin/entries/{id}``, ``/v1/admin/search`` and
``/v1/admin/entries/{id}/withdraw``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hivemind.domain.access import TrustLevel
from hivemind.domain.audit import AuditAction, AuditFilters
from hivemind.domain.entry import EntryDraft, EntryState, Kind
from hivemind.domain.feedback import Feedback, FeedbackLogFilters, Verdict
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.feedback import FeedbackService
from tests.fakes import FIXED_NOW, FixedClock, make_embedder
from tests.unit.test_api_endpoints import make_app_with_clock, make_client, make_hivemind_app

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
    ("GET", "/v1/admin/feedback", None),
    ("GET", f"/v1/admin/fleets/{FAKE_ID}/pins", None),
    ("GET", "/v1/admin/flagged", None),
    ("POST", f"/v1/admin/fleets/{FAKE_ID}/merge", {"new_fleet_name": "c", "dry_run": True}),
    ("GET", "/v1/admin/fleet-merges", None),
    ("POST", f"/v1/admin/fleet-merges/{FAKE_ID}/undo", None),
    ("PATCH", f"/v1/admin/fleets/{FAKE_ID}", {"name": "renamed"}),
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


# A valid request body for each admin route that takes one; the rest take none.
ADMIN_BODIES = {
    ("POST", "/v1/admin/search"): {"query": "kaniko"},
    ("POST", "/v1/admin/entries/{entry_id}/withdraw"): {},
    ("POST", "/v1/admin/fleets"): {"name": "blue"},
    ("POST", "/v1/admin/agents/{name}/activate"): {"trust_level": 1, "home_fleet_id": FAKE_ID},
    ("PATCH", "/v1/admin/agents/{name}"): {"trust_level": 1},
    ("POST", "/v1/admin/fleets/{source_id}/merge"): {"new_fleet_name": "c", "dry_run": False},
    ("PATCH", "/v1/admin/fleets/{fleet_id}"): {"name": "renamed"},
}


@pytest.mark.parametrize("headers", [ALICE, ORG], ids=["agent-key", "org-key"])
async def test_every_registered_admin_route_is_403_for_agent_and_org_keys(headers) -> None:
    """The route list comes from the app, so a new ``/v1/admin/*`` route is
    covered here the day it is added (give it a body above if it needs one)."""
    app = make_hivemind_app()
    await app.store.register_agent("ghost")
    async with make_client(app) as client:
        spec = (await client.get("/openapi.json")).json()
        routes = [
            (method.upper(), path)
            for path, operations in spec["paths"].items()
            if path.startswith("/v1/admin/")
            for method in operations
        ]
        assert len(routes) >= len(ROUTES)
        for method, template in routes:
            path = template.format(
                entry_id=FAKE_ID,
                fleet_id=FAKE_ID,
                name="ghost",
                source_id=FAKE_ID,
                merge_id=FAKE_ID,
            )
            body = ADMIN_BODIES.get((method, template))
            resp = await client.request(method, path, json=body, headers=headers)
            assert resp.status_code == 403, (method, template, resp.text)
            assert resp.json()["error"]["code"] == "forbidden", (method, template)
    assert await app.store.list_fleets() == []
    assert (await app.store.get_agent("ghost")).status.value == "pending"  # type: ignore[union-attr]


@pytest.mark.parametrize("headers", [ALICE, ORG], ids=["agent-key", "org-key"])
async def test_agent_patch_by_a_non_admin_is_403_before_any_lookup(headers) -> None:
    """A known fleet, an unknown fleet and an empty body all answer 403, so
    a non-admin key cannot probe which fleet ids exist."""
    app = make_hivemind_app()
    known = await app.store.create_fleet("blue")
    async with make_client(app) as client:
        known_fleet = await client.patch(
            "/v1/admin/agents/ghost", json={"home_fleet_id": known.id}, headers=headers
        )
        unknown_fleet = await client.patch(
            "/v1/admin/agents/ghost", json={"home_fleet_id": FAKE_ID}, headers=headers
        )
        empty = await client.patch("/v1/admin/agents/ghost", json={}, headers=headers)
    assert known_fleet.status_code == unknown_fleet.status_code == empty.status_code == 403
    assert known_fleet.json() == unknown_fleet.json() == empty.json()


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
    app, _, _, mine, in_blue, in_red = await _seed()
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
    assert unknown.json()["error"]["code"] == "not_found"
    # The refused calls changed nothing: the first withdrawal's reason stands,
    # and the only other entries are still active.
    stored = await app.store.get_entry(in_blue.id)
    assert stored is not None and stored.withdrawn_reason == "wrong"
    for other in (mine, in_red):
        untouched = await app.store.get_entry(other.id)
        assert untouched is not None and untouched.state is EntryState.ACTIVE
    records = await app.store.list_audit(AuditFilters(action=AuditAction.ENTRY_WITHDRAW), 10)
    assert [r.target for r in records] == [in_blue.id]


async def test_admin_feedback_lists_who_rated_what_across_fleets() -> None:
    app, blue, red, mine, in_blue, in_red = await _seed()
    t0 = datetime(2026, 6, 1, tzinfo=UTC)
    await app.store.record_feedback(
        Feedback(in_blue.id, "bob", "codex", Verdict.WRONG, "wrong port", t0)
    )
    await app.store.record_feedback(
        Feedback(in_red.id, "alice", "alice", Verdict.HELPFUL, None, t0 + timedelta(hours=1))
    )
    await app.store.record_feedback(
        Feedback(mine.id, "bob", "bob", Verdict.STALE, None, t0 + timedelta(hours=2))
    )
    async with make_client(app) as client:
        everything = await client.get("/v1/admin/feedback", headers=ADMIN)
        by_bob = await client.get("/v1/admin/feedback", params={"reporter": "bob"}, headers=ADMIN)
        wrong = await client.get("/v1/admin/feedback", params={"verdict": "wrong"}, headers=ADMIN)
        on_bob = await client.get("/v1/admin/feedback", params={"author": "bob"}, headers=ADMIN)
        in_red_fleet = await client.get(
            "/v1/admin/feedback", params={"fleet_id": red.id}, headers=ADMIN
        )
        one_entry = await client.get(
            "/v1/admin/feedback", params={"entry_id": in_blue.id}, headers=ADMIN
        )
        paged = await client.get(
            "/v1/admin/feedback", params={"limit": 1, "offset": 1}, headers=ADMIN
        )
        bad_verdict = await client.get(
            "/v1/admin/feedback", params={"verdict": "meh"}, headers=ADMIN
        )
        bad_limit = await client.get("/v1/admin/feedback", params={"limit": 0}, headers=ADMIN)
    assert everything.status_code == 200
    rows = everything.json()
    assert [(r["reporter"], r["entry"]["summary"]) for r in rows] == [
        ("bob", "alice private note kaniko"),
        ("alice", "red fleet deploy kaniko"),
        ("bob", "blue fleet deploy kaniko"),
    ]
    assert rows[2] == {
        "reporter": "bob",
        "agent": "codex",
        "verdict": "wrong",
        "note": "wrong port",
        "updated_at": "2026-06-01T00:00:00Z",
        "entry": {
            "id": in_blue.id,
            "kind": "fact",
            "summary": "blue fleet deploy kaniko",
            "author": "alice",
            "scope": "fleet",
            "fleet_id": blue.id,
            "state": "active",
        },
    }
    assert [r["entry"]["id"] for r in by_bob.json()] == [mine.id, in_blue.id]
    assert [r["entry"]["id"] for r in wrong.json()] == [in_blue.id]
    assert [r["entry"]["id"] for r in on_bob.json()] == [in_red.id]
    assert [r["entry"]["id"] for r in in_red_fleet.json()] == [in_red.id]
    assert [r["reporter"] for r in one_entry.json()] == ["bob"]
    assert [r["entry"]["id"] for r in paged.json()] == [in_red.id]
    assert bad_verdict.status_code == bad_limit.status_code == 422


async def test_the_feedback_log_service_follows_the_callers_visibility() -> None:
    """The service behind the admin route also serves an agent's own view:
    an agent key reads rows on the entries it can read, nothing more."""
    app, blue, _red, mine, in_blue, in_red = await _seed()
    for entry in (mine, in_blue, in_red):
        await app.store.record_feedback(Feedback(entry.id, "zed", "zed", Verdict.HELPFUL))
    lurker = Credential(
        user_id="carol",
        agent_name="carol",
        access_controlled=True,
        trust_level=TrustLevel.LURKER,
        home_fleet_id=blue.id,
    )
    rows = await app.governance_service.feedback_log(
        lurker, FeedbackLogFilters(reporter="zed"), limit=10
    )
    assert [r.feedback.entry_id for r in rows] == [in_blue.id]


async def test_admin_feedback_refuses_bad_filters_and_matches_nothing_on_a_bad_id() -> None:
    app = make_hivemind_app()
    async with make_client(app) as client:
        nul = await client.get("/v1/admin/feedback", params={"reporter": "a\x00b"}, headers=ADMIN)
        long_id = await client.get(
            "/v1/admin/feedback", params={"entry_id": "x" * 65}, headers=ADMIN
        )
        malformed = await client.get(
            "/v1/admin/feedback", params={"entry_id": "not-an-id", "fleet_id": "x"}, headers=ADMIN
        )
    assert nul.status_code == long_id.status_code == 422
    assert malformed.status_code == 200 and malformed.json() == []


async def test_admin_fleet_pins_lists_any_fleets_pins_with_their_changes() -> None:
    app, blue, red, _mine, in_blue, in_red = await _seed()
    t0 = datetime(2026, 6, 1, tzinfo=UTC)
    await app.store.pin_entry(blue.id, in_blue.id, "alice", 10)
    await app.store.pin_entry(red.id, in_red.id, "bob", 10)
    await app.store.record_feedback(
        Feedback(in_red.id, "carol", "carol", Verdict.WRONG, "moved", t0 + timedelta(days=3650))
    )
    async with make_client(app) as client:
        blue_pins = await client.get(f"/v1/admin/fleets/{blue.id}/pins", headers=ADMIN)
        red_pins = await client.get(f"/v1/admin/fleets/{red.id}/pins", headers=ADMIN)
        unknown = await client.get(f"/v1/admin/fleets/{FAKE_ID}/pins", headers=ADMIN)
        malformed = await client.get("/v1/admin/fleets/not-a-fleet/pins", headers=ADMIN)
    assert blue_pins.status_code == 200
    assert blue_pins.json()["fleet_id"] == blue.id
    [pin] = blue_pins.json()["pins"]
    assert (pin["id"], pin["pinned_by"], pin["changed_since_pin"]) == (in_blue.id, "alice", False)
    [flagged] = red_pins.json()["pins"]
    assert (flagged["id"], flagged["change_reason"]) == (in_red.id, "flagged")
    assert unknown.status_code == malformed.status_code == 404
    assert unknown.json()["error"]["code"] == "not_found"


async def test_admin_flagged_lists_the_flag_backlog_oldest_flag_first() -> None:
    """ADR 0093: active entries with a standing stale/wrong verdict, the
    oldest first flag first; the age and orphan filters; a bad id matches
    nothing."""
    app, blue, red, mine, in_blue, in_red = await _seed()
    now = datetime.now(UTC)
    for entry, user, verdict, days in (
        (in_blue, "bob", Verdict.WRONG, 20),
        (in_blue, "carol", Verdict.HELPFUL, 1),
        (in_red, "alice", Verdict.STALE, 3),
        (mine, "bob", Verdict.HELPFUL, 2),
    ):
        await app.store.record_feedback(
            Feedback(entry.id, user, user, verdict, None, now - timedelta(days=days))
        )
    async with make_client(app) as client:
        everything = await client.get("/v1/admin/flagged", headers=ADMIN)
        old = await client.get("/v1/admin/flagged", params={"older_than_days": 14}, headers=ADMIN)
        in_red_fleet = await client.get(
            "/v1/admin/flagged", params={"fleet_id": red.id}, headers=ADMIN
        )
        orphans = await client.get("/v1/admin/flagged", params={"orphaned": True}, headers=ADMIN)
        paged = await client.get(
            "/v1/admin/flagged", params={"limit": 1, "offset": 1}, headers=ADMIN
        )
        malformed = await client.get(
            "/v1/admin/flagged", params={"fleet_id": "not-a-fleet"}, headers=ADMIN
        )
        too_old = await client.get(
            "/v1/admin/flagged", params={"older_than_days": 3651}, headers=ADMIN
        )
        long_id = await client.get(
            "/v1/admin/flagged", params={"fleet_id": "x" * 65}, headers=ADMIN
        )
    assert everything.status_code == 200
    rows = everything.json()
    assert [r["id"] for r in rows] == [in_blue.id, in_red.id]
    first = rows[0]
    assert {k: first[k] for k in ("kind", "author", "scope", "fleet_id")} == {
        "kind": "fact",
        "author": "alice",
        "scope": "fleet",
        "fleet_id": blue.id,
    }
    # The test store's clock is fixed in the past, so every author has been
    # silent for more than 30 days: every flagged entry is an orphan.
    assert (first["helpful"], first["stale"], first["wrong"], first["orphaned"]) == (1, 0, 1, True)
    assert first["first_flag_at"] == first["last_flag_at"]
    assert [r["id"] for r in old.json()] == [in_blue.id]
    assert [r["id"] for r in in_red_fleet.json()] == [in_red.id]
    assert [r["id"] for r in orphans.json()] == [in_blue.id, in_red.id]
    assert [r["id"] for r in paged.json()] == [in_red.id]
    assert malformed.status_code == 200 and malformed.json() == []
    assert too_old.status_code == long_id.status_code == 422


async def test_the_orphan_filter_keeps_out_authors_who_wrote_recently() -> None:
    """The flag backlog judges silence against the real clock, so the store
    clock is moved to now for one author; the other wrote long ago."""
    clock = FixedClock(FIXED_NOW)
    app = make_app_with_clock(clock)
    long_gone = await _add(app, "erin wrote this ages ago", author="erin")
    clock.set(datetime.now(UTC))
    active = await _add(app, "dave wrote this today", author="dave")
    for entry in (long_gone, active):
        await app.store.record_feedback(Feedback(entry.id, "zed", "zed", Verdict.STALE))
    async with make_client(app) as client:
        everything = await client.get("/v1/admin/flagged", headers=ADMIN)
        orphans = await client.get("/v1/admin/flagged", params={"orphaned": True}, headers=ADMIN)
    assert {r["id"]: r["orphaned"] for r in everything.json()} == {
        long_gone.id: True,
        active.id: False,
    }
    assert [r["id"] for r in orphans.json()] == [long_gone.id]


async def test_the_orphan_judgement_follows_the_services_clock() -> None:
    """The store and the service share one fixed clock, so silence is counted
    from it and not from the real time: erin has been quiet 40 days, dave
    wrote today."""
    clock = FixedClock(FIXED_NOW)
    store = MemoryStore(clock)
    erin = await store.create_entry(EntryDraft(Kind.FACT, "erin wrote first", "erin", "a"))
    clock.advance_days(40)
    dave = await store.create_entry(EntryDraft(Kind.FACT, "dave wrote later", "dave", "a"))
    for entry in (erin, dave):
        await store.record_feedback(Feedback(entry.id, "zed", "zed", Verdict.STALE))
    service = FeedbackService(store, now_fn=clock)
    admin = Credential(user_id="root", is_admin=True)
    rows = await service.flag_backlog(admin, limit=10)
    assert {r.entry_id: r.orphaned for r in rows} == {erin.id: True, dave.id: False}
    only = await service.flag_backlog(admin, orphaned=True, limit=10)
    assert [r.entry_id for r in only] == [erin.id]


async def test_the_flag_backlog_service_follows_the_callers_visibility() -> None:
    app, blue, _red, mine, in_blue, in_red = await _seed()
    for entry in (mine, in_blue, in_red):
        await app.store.record_feedback(Feedback(entry.id, "zed", "zed", Verdict.STALE))
    lurker = Credential(
        user_id="carol",
        agent_name="carol",
        access_controlled=True,
        trust_level=TrustLevel.LURKER,
        home_fleet_id=blue.id,
    )
    rows = await app.governance_service.flag_backlog(lurker, limit=10)
    assert [r.entry_id for r in rows] == [in_blue.id]
