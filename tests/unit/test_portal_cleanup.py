"""An agent cleans up its own work from the portal (ADR 0118), at the HTTP
seam over ``MemoryStore``: the same operation as the admin's cleanup (ADR
0111) limited to the signed-in agent, with the memories of the agents retired
in its favour, no revoke, and an undo that only reaches cleanups the agent ran
itself. The store-level behavior is in ``test_store_contract.py``."""

from __future__ import annotations

import httpx
import pytest

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import AgentStatus, TrustLevel
from hivemind.domain.agent_cleanup import cleanup_reason
from hivemind.domain.audit import ActorKind, AuditAction, AuditFilters
from hivemind.domain.entry import EntryDraft, EntryState, Kind
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.memstore import MemoryStore
from tests.fakes import make_embedder, make_search_config
from tests.unit.test_successions import ADMIN, StoreAuthenticator, TickingClock

ADMIN_H = {"X-API-Key": "hm_admin"}
VEC = [1.0, 0.0, 0.0, 0.0]
WINDOW = {"since": "2026-10-01T00:00:00Z", "until": "2027-01-01T00:00:00Z"}


class World:
    def __init__(self) -> None:
        self.store = MemoryStore(TickingClock())
        self.app = create_app_for_config(
            Settings(),
            store=self.store,
            embedder=make_embedder(),
            authenticator=StoreAuthenticator(self.store),
            search_config=make_search_config(),
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(self.app)), base_url="http://t"
        )
        self.fleet = ""

    async def setup(self) -> World:
        self.fleet = (await self.store.create_fleet("blue")).id
        for name in ("successor", "old", "peer", "good"):
            await self.store.register_agent(name)
            await self.store.activate_agent(
                name, trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=self.fleet
            )
        return self

    def h(self, name: str) -> dict[str, str]:
        return {"X-API-Key": f"k-{name}"}

    async def entry(self, author: str, summary: str, supersedes: tuple[str, ...] = ()) -> str:
        draft = EntryDraft(
            kind=Kind.FACT,
            summary=summary,
            author=author,
            agent=author,
            scope="org",
            supersedes=supersedes,
        )
        return (await self.store.create_entry(draft, VEC)).id

    async def rate(self, entry_id: str, user: str, verdict: Verdict = Verdict.WRONG) -> None:
        await self.store.record_feedback(Feedback(entry_id, user, "a", verdict, "n"))

    async def state(self, entry_id: str) -> EntryState:
        entry = await self.store.get_entry(entry_id)
        assert entry is not None
        return entry.state

    async def preview(self, name: str = "successor") -> dict:  # type: ignore[type-arg]
        resp = await self.client.post(
            "/v1/portal/cleanup", json={**WINDOW, "dry_run": True}, headers=self.h(name)
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    async def clean(self, name: str = "successor", **body) -> httpx.Response:  # type: ignore[no-untyped-def]
        preview = await self.preview(name)
        payload = {
            **WINDOW,
            "dry_run": False,
            "expected_withdraw": preview["withdraw_total"],
            "expected_ratings": preview["ratings_total"],
            **body,
        }
        return await self.client.post("/v1/portal/cleanup", json=payload, headers=self.h(name))

    async def admin_clean(self, name: str) -> httpx.Response:
        preview = (
            await self.client.post(
                f"/v1/admin/agents/{name}/cleanup",
                json={**WINDOW, "dry_run": True},
                headers=ADMIN_H,
            )
        ).json()
        return await self.client.post(
            f"/v1/admin/agents/{name}/cleanup",
            json={
                **WINDOW,
                "dry_run": False,
                "expected_withdraw": preview["withdraw_total"],
                "expected_ratings": preview["ratings_total"],
            },
            headers=ADMIN_H,
        )

    async def undo(self, cleanup_id: str, name: str = "successor") -> httpx.Response:
        return await self.client.post(
            f"/v1/portal/cleanups/{cleanup_id}/undo", headers=self.h(name)
        )

    async def audit(self, action: AuditAction) -> list:  # type: ignore[type-arg]
        return await self.store.list_audit(AuditFilters(action=action), 100)


@pytest.fixture
async def w():  # type: ignore[no-untyped-def]
    world = await World().setup()
    yield world
    await world.client.aclose()


async def _retire(w: World) -> None:
    await w.app.access_service.successions.retire("old", "successor", ADMIN)


async def test_preview_changes_nothing_and_covers_what_the_agent_owns(w: World) -> None:
    await _retire(w)
    good = await w.entry("good", "good fact")
    mine = await w.entry("successor", "successor junk")
    inherited = await w.entry("old", "old junk", supersedes=(good,))
    other = await w.entry("peer", "peer note")
    await w.rate(other, "successor")
    await w.rate(other, "peer")

    preview = await w.preview()

    assert preview["dry_run"] is True and preview["agent"] == "successor"
    assert {e["id"] for e in preview["withdraw"]} == {mine, inherited}
    assert preview["withdraw_total"] == 2
    assert preview["ratings_total"] == 1 and preview["ratings_by_verdict"] == {"wrong": 1}
    assert [e["id"] for e in preview["restorable"]] == [good]
    assert preview["nothing_to_clean"] is False
    assert await w.state(mine) is EntryState.ACTIVE and await w.state(good) is EntryState.SUPERSEDED


async def test_apply_withdraws_owned_memories_and_sets_aside_only_the_agents_ratings(
    w: World,
) -> None:
    await _retire(w)
    good = await w.entry("good", "good fact")
    mine = await w.entry("successor", "successor junk")
    inherited = await w.entry("old", "old junk", supersedes=(good,))
    other = await w.entry("peer", "peer note")
    await w.rate(other, "successor")
    await w.rate(other, "peer", Verdict.STALE)

    resp = await w.clean(restore_ids=[good])

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["withdrawn"], body["ratings_set_aside"], body["restored"]) == (2, 1, 1)
    assert body["nothing_to_clean"] is False and body["cleanup_id"]
    assert "revoked" not in body
    for entry_id in (mine, inherited):
        entry = await w.store.get_entry(entry_id)
        assert entry is not None and entry.state is EntryState.WITHDRAWN
        assert entry.withdrawn_reason == cleanup_reason(body["cleanup_id"])
    assert await w.state(good) is EntryState.ACTIVE and await w.state(other) is EntryState.ACTIVE
    assert await w.store.feedback_counts(other) == (0, 1, 0)  # peer's stale still counts
    agent = await w.store.get_agent("successor")
    assert agent is not None and agent.status is AgentStatus.ACTIVE


async def test_the_admin_cleanup_of_the_same_agent_still_goes_by_author_only(w: World) -> None:
    await _retire(w)
    mine = await w.entry("successor", "successor junk")
    inherited = await w.entry("old", "old junk")
    resp = await w.admin_clean("successor")
    assert resp.status_code == 200, resp.text
    assert await w.state(mine) is EntryState.WITHDRAWN
    assert await w.state(inherited) is EntryState.ACTIVE


async def test_the_record_and_the_audit_say_the_agent_did_it_from_the_portal(w: World) -> None:
    mine = await w.entry("successor", "successor junk")
    await w.rate(mine, "successor")
    cleanup_id = (await w.clean()).json()["cleanup_id"]

    record = await w.store.get_agent_cleanup(cleanup_id)
    assert record is not None and record.self_service is True and record.agent == "successor"
    assert record.cleaned_by == "successor"
    [row] = await w.audit(AuditAction.AGENT_CLEANUP)
    assert (row.actor_kind, row.actor, row.target) == (
        ActorKind.AGENT_KEY,
        "successor",
        "successor",
    )
    assert row.detail["via"] == "portal" and row.detail["cleanup_id"] == cleanup_id
    assert row.detail["withdrawn"] == 1 and row.detail["also_revoke"] is False

    admin_list = (await w.client.get("/v1/admin/agent-cleanups", headers=ADMIN_H)).json()
    assert [(c["id"], c["self_service"], c["cleaned_by"]) for c in admin_list] == [
        (cleanup_id, True, "successor")
    ]

    undone = await w.undo(cleanup_id)
    assert undone.status_code == 200, undone.text
    [undo_row] = await w.audit(AuditAction.AGENT_CLEANUP_UNDO)
    assert (undo_row.actor_kind, undo_row.actor) == (ActorKind.AGENT_KEY, "successor")
    assert undo_row.detail["via"] == "portal"


async def test_pins_taken_off_are_audited_with_the_portal_marker(w: World) -> None:
    mine = await w.entry("successor", "successor junk")
    assert await w.store.pin_entry(w.fleet, mine, "successor", 10)
    await w.clean()
    [row] = await w.audit(AuditAction.ENTRY_UNPIN)
    assert row.detail["reason"] == "agent_cleanup" and row.detail["via"] == "portal"
    assert row.actor == "successor"


async def test_the_apply_needs_the_previews_numbers_and_refuses_when_they_changed(
    w: World,
) -> None:
    await w.entry("successor", "one")
    missing = await w.client.post(
        "/v1/portal/cleanup", json={**WINDOW, "dry_run": False}, headers=w.h("successor")
    )
    assert missing.status_code == 422 and missing.json()["error"]["code"] == "invalid_input"
    no_dry_run = await w.client.post("/v1/portal/cleanup", json=WINDOW, headers=w.h("successor"))
    assert no_dry_run.status_code == 422

    stale = await w.client.post(
        "/v1/portal/cleanup",
        json={**WINDOW, "dry_run": False, "expected_withdraw": 0, "expected_ratings": 0},
        headers=w.h("successor"),
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "cleanup_refused"
    bad_restore = await w.clean(restore_ids=["00000000-0000-4000-8000-000000000001"])
    assert bad_restore.status_code == 409
    empty = await w.client.post(
        "/v1/portal/cleanup",
        json={
            "since": "2027-01-02T00:00:00Z",
            "until": "2027-01-01T00:00:00Z",
            "dry_run": True,
        },
        headers=w.h("successor"),
    )
    assert empty.status_code == 422


async def test_nothing_in_the_window_keeps_no_record(w: World) -> None:
    resp = await w.clean()
    body = resp.json()
    assert resp.status_code == 200 and body["nothing_to_clean"] is True
    assert body["cleanup_id"] is None
    assert (await w.client.get("/v1/portal/cleanups", headers=w.h("successor"))).json() == []


async def test_a_lurker_can_clean_up_its_own_work(w: World) -> None:
    await w.store.set_agent_trust_level("peer", TrustLevel.LURKER)
    mine = await w.entry("peer", "peer note")
    resp = await w.clean("peer")
    assert resp.status_code == 200 and resp.json()["withdrawn"] == 1
    assert await w.state(mine) is EntryState.WITHDRAWN


async def test_an_agent_undoes_its_own_cleanup(w: World) -> None:
    await _retire(w)
    good = await w.entry("good", "good fact")
    mine = await w.entry("successor", "successor junk", supersedes=(good,))
    inherited = await w.entry("old", "old junk")
    cleanup_id = (await w.clean(restore_ids=[good])).json()["cleanup_id"]

    listed = (await w.client.get("/v1/portal/cleanups", headers=w.h("successor"))).json()
    assert [(c["id"], c["run_by"], c["undoable"], c["withdrawn"]) for c in listed] == [
        (cleanup_id, "agent", True, 2)
    ]
    assert "cleaned_by" not in listed[0]

    resp = await w.undo(cleanup_id)

    assert resp.status_code == 200, resp.text
    assert (resp.json()["reactivated"], resp.json()["resuperseded"]) == (2, 1)
    assert await w.state(mine) is EntryState.ACTIVE
    assert await w.state(inherited) is EntryState.ACTIVE
    assert await w.state(good) is EntryState.SUPERSEDED
    again = await w.undo(cleanup_id)
    assert again.status_code == 409 and again.json()["error"]["code"] == "cleanup_refused"
    listed = (await w.client.get("/v1/portal/cleanups", headers=w.h("successor"))).json()
    assert listed[0]["undone_at"] is not None and listed[0]["undoable"] is False


async def test_the_admin_can_undo_a_cleanup_the_agent_ran(w: World) -> None:
    mine = await w.entry("successor", "successor junk")
    cleanup_id = (await w.clean()).json()["cleanup_id"]
    resp = await w.client.post(f"/v1/admin/agent-cleanups/{cleanup_id}/undo", headers=ADMIN_H)
    assert resp.status_code == 200, resp.text
    assert resp.json()["reactivated"] == 1
    assert await w.state(mine) is EntryState.ACTIVE


async def test_an_agent_cannot_undo_a_cleanup_an_admin_ran(w: World) -> None:
    mine = await w.entry("successor", "successor junk")
    cleanup_id = (await w.admin_clean("successor")).json()["cleanup_id"]

    listed = (await w.client.get("/v1/portal/cleanups", headers=w.h("successor"))).json()
    assert [(c["id"], c["run_by"], c["undoable"]) for c in listed] == [(cleanup_id, "admin", False)]
    assert "admin" in listed[0]["undo_blocked"]

    resp = await w.undo(cleanup_id)

    assert resp.status_code == 403 and resp.json()["error"]["code"] == "forbidden"
    assert await w.state(mine) is EntryState.WITHDRAWN
    assert await w.audit(AuditAction.AGENT_CLEANUP_UNDO) == []


async def test_the_newest_standing_cleanup_rule_spans_admin_and_self_cleanups(w: World) -> None:
    first = await w.entry("successor", "first junk")
    mine_cleanup = (await w.clean()).json()["cleanup_id"]
    second = await w.entry("successor", "second junk")
    admin_cleanup = (await w.admin_clean("successor")).json()["cleanup_id"]

    blocked = await w.undo(mine_cleanup)
    assert blocked.status_code == 409 and admin_cleanup in blocked.json()["error"]["message"]
    listed = (await w.client.get("/v1/portal/cleanups", headers=w.h("successor"))).json()
    assert [(c["id"], c["undoable"]) for c in listed] == [
        (admin_cleanup, False),
        (mine_cleanup, False),
    ]

    # The other way round: a self cleanup newer than an admin's blocks the admin's undo.
    done = await w.client.post(f"/v1/admin/agent-cleanups/{admin_cleanup}/undo", headers=ADMIN_H)
    assert done.status_code == 200
    third = await w.entry("successor", "third junk")
    newer = (await w.clean()).json()["cleanup_id"]
    refused = await w.client.post(f"/v1/admin/agent-cleanups/{admin_cleanup}/undo", headers=ADMIN_H)
    assert refused.status_code == 409
    assert await w.state(first) is EntryState.WITHDRAWN
    assert await w.state(second) is EntryState.WITHDRAWN  # re-activated, then cleaned again
    assert await w.state(third) is EntryState.WITHDRAWN
    listed = (await w.client.get("/v1/portal/cleanups", headers=w.h("successor"))).json()
    assert listed[0]["id"] == newer and listed[0]["undoable"] is True


async def test_another_agents_cleanup_is_not_found_and_not_listed(w: World) -> None:
    await w.entry("peer", "peer note")
    theirs = (await w.clean("peer")).json()["cleanup_id"]
    await w.entry("successor", "successor note")
    ours = (await w.clean("successor")).json()["cleanup_id"]

    assert (await w.undo(theirs, "successor")).status_code == 404
    assert (await w.undo("00000000-0000-4000-8000-000000000001")).status_code == 404
    listed = (await w.client.get("/v1/portal/cleanups", headers=w.h("successor"))).json()
    assert [c["id"] for c in listed] == [ours]
    still = await w.store.get_agent_cleanup(theirs)
    assert still is not None and still.undone_at is None


async def test_the_cleanup_routes_refuse_the_admin_and_org_keys(w: World) -> None:
    for headers in (ADMIN_H, {"X-API-Key": "hm_org"}):
        for method, path, body in (
            ("POST", "/v1/portal/cleanup", {**WINDOW, "dry_run": True}),
            ("GET", "/v1/portal/cleanups", None),
            ("POST", "/v1/portal/cleanups/00000000-0000-4000-8000-000000000001/undo", None),
        ):
            resp = await w.client.request(method, path, json=body, headers=headers)
            assert resp.status_code == 403, (path, resp.text)


async def test_a_memory_in_a_fleet_the_key_cannot_read_is_never_offered_or_restored(
    w: World,
) -> None:
    red = (await w.store.create_fleet("red")).id
    for name in ("old2", "peer2"):
        await w.store.register_agent(name)
        await w.store.activate_agent(name, trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=red)

    async def fleet_entry(author: str, summary: str, supersedes: tuple[str, ...] = ()) -> str:
        draft = EntryDraft(
            kind=Kind.FACT,
            summary=summary,
            author=author,
            agent=author,
            scope="fleet",
            fleet_id=red,
            supersedes=supersedes,
        )
        return (await w.store.create_entry(draft, VEC)).id

    secret = await fleet_entry("peer2", "RED SECRET")
    await fleet_entry("old2", "junk", supersedes=(secret,))
    await w.app.access_service.successions.retire("old2", "successor", ADMIN)
    read = await w.client.get(f"/v1/entries/{secret}", headers=w.h("successor"))
    assert read.status_code == 404

    preview = await w.preview()
    assert preview["restorable"] == [] and preview["restorable_total"] == 0
    assert preview["withdraw_total"] == 1  # the retired agent's junk is the successor's own
    refused = await w.clean(restore_ids=[secret])
    assert refused.status_code == 409 and "RED SECRET" not in refused.text
    assert await w.state(secret) is EntryState.SUPERSEDED
    done = await w.clean()
    assert done.status_code == 200 and done.json()["restored"] == 0
    assert await w.state(secret) is EntryState.SUPERSEDED
