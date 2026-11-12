"""Moving a fleet memory to `self` (ADR 0145): ``POST /v1/admin/entries/{id}/move-to-self`` and
``POST /v1/portal/entries/{id}/move-to-self``."""

from __future__ import annotations

from hivemind.domain.audit import ActorKind, AuditAction, AuditFilters
from hivemind.domain.entry import EntryDraft, EntryState, Kind
from hivemind.domain.feedback import Feedback, Verdict
from tests.fakes import make_embedder
from tests.unit.test_api_endpoints import make_client
from tests.unit.test_portal_api import ADMIN, CAROL, PAT, _seed


def _move(entry_id: str, surface: str = "admin") -> str:
    return f"/v1/{surface}/entries/{entry_id}/move-to-self"


async def _supersede(app, old_id: str, summary: str, **kw):  # type: ignore[no-untyped-def]
    draft = EntryDraft(
        kind=Kind.FACT,
        summary=summary,
        author=kw.pop("author", "carol"),
        agent="a",
        supersedes=(old_id,),
        **kw,
    )
    return await app.store.create_entry(draft, await make_embedder().embed_text(summary))


async def test_the_admin_moves_a_fleet_memory_out_of_the_fleets_sight() -> None:
    app, blue, _red, _mine, in_blue, _by_bob, _in_red = await _seed()
    await app.store.pin_entry(blue.id, in_blue.id, "carol", limit=10)
    await app.store.record_feedback(
        Feedback(entry_id=in_blue.id, user="pat", agent="pat", verdict=Verdict.HELPFUL)
    )
    async with make_client(app) as client:
        before = await client.post("/v1/portal/search", json={"query": "deploy"}, headers=PAT)
        done = await client.post(
            _move(in_blue.id), json={"reason": "meant for carol only"}, headers=ADMIN
        )
        again = await client.post(_move(in_blue.id), json={"reason": "twice"}, headers=ADMIN)
        by_pat = await client.get(f"/v1/entries/{in_blue.id}", headers=PAT)
        by_carol = await client.get(f"/v1/entries/{in_blue.id}", headers=CAROL)
    assert in_blue.id in [h["entry_id"] for h in before.json()]
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["entry"]["scope"] == "self" and body["entry"]["state"] == "active"
    assert body["earlier_versions"] == []
    assert body["unpinned"] == [{"fleet_id": blue.id, "entry_id": in_blue.id}]
    assert again.status_code == 409 and again.json()["error"]["code"] == "conflict"
    assert by_pat.status_code == 404
    assert by_carol.status_code == 200 and by_carol.json()["scope"] == "self"
    assert await app.store.list_pins(blue.id) == []
    # The rating stays on the memory, for its author.
    summary = await app.governance_service.feedback_summary(in_blue.id)
    assert summary.helpful == 1
    [moved] = await app.store.list_audit(AuditFilters(action=AuditAction.ENTRY_MOVE_TO_SELF), 10)
    assert moved.actor_kind is ActorKind.ADMIN_KEY and moved.target == in_blue.id
    assert moved.detail == {
        "author": "carol",
        "fleet_id": blue.id,
        "reason": "meant for carol only",
        "earlier_versions": [],
    }
    [unpin] = await app.store.list_audit(AuditFilters(action=AuditAction.ENTRY_UNPIN), 10)
    assert unpin.detail == {
        "fleet_id": blue.id,
        "pin_ids": [in_blue.id],
        "reason": "moved_to_self",
    }


async def test_the_portal_moves_the_agents_own_memories_only() -> None:
    app, blue, _red, mine, in_blue, by_bob, in_red = await _seed()
    older = await _supersede(
        app, in_blue.id, "blue fleet deploy v2", scope="fleet", fleet_id=blue.id
    )
    async with make_client(app) as client:
        no_reason = await client.post(_move(older.id, "portal"), json={}, headers=CAROL)
        not_hers = await client.post(
            _move(by_bob.id, "portal"), json={"reason": "no"}, headers=CAROL
        )
        unreadable = await client.post(
            _move(in_red.id, "portal"), json={"reason": "no"}, headers=CAROL
        )
        private = await client.post(_move(mine.id, "portal"), json={"reason": "no"}, headers=CAROL)
        superseded = await client.post(
            _move(in_blue.id, "portal"), json={"reason": "no"}, headers=CAROL
        )
        done = await client.post(
            _move(older.id, "portal"), json={"reason": "a scratch note"}, headers=CAROL
        )
    assert no_reason.status_code == 422
    assert not_hers.status_code == 403
    assert unreadable.status_code == 404
    assert private.status_code == 409
    assert superseded.status_code == 409
    assert done.status_code == 200, done.text
    assert done.json()["earlier_versions"] == [in_blue.id]
    for entry_id in (in_blue.id, older.id):
        stored = await app.store.get_entry(entry_id)
        assert stored is not None and stored.scope == "self"
    stored_bob = await app.store.get_entry(by_bob.id)
    assert stored_bob is not None and stored_bob.scope == "fleet"
    [record] = await app.store.list_audit(AuditFilters(action=AuditAction.ENTRY_MOVE_TO_SELF), 10)
    assert (record.actor_kind, record.actor) == (ActorKind.AGENT_KEY, "carol")
    assert record.detail["via"] == "portal"
    assert record.detail["earlier_versions"] == [in_blue.id]


async def test_a_memory_that_replaced_another_agents_is_refused_and_left_alone() -> None:
    app, blue, _red, _mine, _in_blue, by_bob, _in_red = await _seed()
    hers = await _supersede(app, by_bob.id, "carol's take", scope="fleet", fleet_id=blue.id)
    async with make_client(app) as client:
        refused = await client.post(_move(hers.id), json={"reason": "x"}, headers=ADMIN)
    assert refused.status_code == 409
    assert "another agent" in refused.json()["error"]["message"]
    for entry_id in (hers.id, by_bob.id):
        stored = await app.store.get_entry(entry_id)
        assert stored is not None and stored.scope == "fleet"
    assert await app.store.list_audit(AuditFilters(action=AuditAction.ENTRY_MOVE_TO_SELF), 10) == []


async def test_another_agent_cannot_supersede_the_moved_memory() -> None:
    app, _blue, _red, _mine, in_blue, _by_bob, _in_red = await _seed()
    async with make_client(app) as client:
        await client.post(_move(in_blue.id), json={"reason": "private"}, headers=ADMIN)
        denied = await client.post(
            "/v1/entries",
            json={
                "kind": "fact",
                "summary": "pat's",
                "scope": "fleet",
                "supersedes": [in_blue.id],
                "agent": "a",
            },
            headers=PAT,
        )
        own = await client.post(
            "/v1/entries",
            json={
                "kind": "fact",
                "summary": "shared again",
                "scope": "fleet",
                "supersedes": [in_blue.id],
                "agent": "a",
            },
            headers=CAROL,
        )
    assert denied.status_code in (403, 404, 409), denied.text
    assert own.status_code in (200, 201), own.text
    stored = await app.store.get_entry(in_blue.id)
    assert stored is not None and stored.state is EntryState.SUPERSEDED
