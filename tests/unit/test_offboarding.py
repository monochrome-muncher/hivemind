"""Offboarding an owner (ADR 0135): ``POST /v1/admin/owners/{alias}/offboard`` over
``MemoryStore``, through the HTTP seam. The Postgres transaction is covered by the store
contract and ``tests/integration/test_offboarding_pg.py``."""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from hivemind.domain.access import AgentStatus
from hivemind.domain.audit import AuditAction, AuditFilters
from tests.unit.test_successions import ADMIN, ADMIN_H, World

ALIAS = "jane"


@pytest.fixture
async def w() -> World:
    world = await World().setup()
    for name, fleet in (("jane-a", world.red), ("jane-b", world.blue), ("jane-c", world.red)):
        await world.agent(name, fleet)
        await world.store.set_agent_owner_alias(name, ALIAS)
    await world.store.register_agent("jane-wait", ALIAS)
    await world.agent("jane-gone", world.red)
    await world.store.set_agent_owner_alias("jane-gone", ALIAS)
    await world.store.revoke_agent("jane-gone")
    await world.agent("heir", world.blue)
    await world.store.set_agent_owner_alias("heir", "kim")
    await world.agent("jane-twin", world.blue)
    await world.store.set_agent_owner_alias("jane-twin", "Jane")
    return world


async def post(w: World, body: dict[str, object]) -> httpx.Response:
    return await w.client.post(f"/v1/admin/owners/{ALIAS}/offboard", json=body, headers=ADMIN_H)


async def offboard(
    w: World, mode: str = "retire", successor: str | None = "heir", **extra: object
) -> httpx.Response:
    """Apply (or, with ``dry_run=True``, preview). An apply sends the agents its own preview showed,
    or none when the preview is refused, so the refusal under test comes from the apply."""
    body: dict[str, object] = {"mode": mode, "successor": successor, "dry_run": False, **extra}
    if body["dry_run"] is False and "expected_agents" not in body:
        shown = await post(w, {**body, "dry_run": True})
        agents = shown.json()["agents"] if shown.status_code == 200 else []
        body["expected_agents"] = [{"name": a["name"], "action": a["action"]} for a in agents]
    return await post(w, body)


async def status_of(w: World, name: str) -> AgentStatus:
    return (await w.store.get_agent(name)).status  # type: ignore[union-attr]


def actions(response: httpx.Response) -> dict[str, str]:
    return {a["name"]: a["action"] for a in response.json()["agents"]}


async def test_a_dry_run_previews_every_agent_and_changes_nothing(w: World) -> None:
    await w.entry("jane-a", "note")
    in_red = await w.entry("jane-a", "red", scope="fleet", fleet=w.red)
    await w.store.pin_entry(w.red, in_red.id, "jane-a", 10)
    preview = await offboard(w, dry_run=True)
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert (body["owner"], body["mode"], body["successor"], body["dry_run"]) == (
        ALIAS,
        "retire",
        "heir",
        True,
    )
    assert actions(preview) == {
        "jane-a": "retire",
        "jane-b": "retire",
        "jane-c": "retire",
        "jane-gone": "retire",
        "jane-wait": "reject",
    }
    first = body["agents"][0]
    assert first["name"] == "jane-a" and first["was"] == "active"
    assert (first["self_notes"], first["fleet_memories"], first["pins"]) == (1, 1, 1)
    assert first["fleet_memories_elsewhere"] == 1
    assert await w.store.list_successions() == []
    assert await status_of(w, "jane-a") is AgentStatus.ACTIVE
    assert await status_of(w, "jane-wait") is AgentStatus.PENDING
    assert await w.audit(AuditAction.AGENT_RETIRE) == []


async def test_retire_hands_every_agents_memories_to_the_successor(w: World) -> None:
    note = await w.entry("jane-a", "jane-a note")
    await w.entry("jane-b", "jane-b note")
    await w.store.retire_agent("jane-c", "heir", "earlier")
    response = await offboard(w)
    assert response.status_code == 200, response.text
    assert response.json()["dry_run"] is False
    assert actions(response) == {
        "jane-a": "retire",
        "jane-b": "retire",
        "jane-c": "skip",
        "jane-gone": "retire",
        "jane-wait": "reject",
    }
    skipped = next(a for a in response.json()["agents"] if a["name"] == "jane-c")
    assert "already retired" in skipped["reason"]
    assert await w.get("heir", note.id) == 200
    assert await w.get("jane-twin", note.id) == 404
    for name in ("jane-a", "jane-b", "jane-gone", "jane-wait"):
        assert await status_of(w, name) is AgentStatus.REVOKED
    retired = {s.retired_agent: s.successor for s in await w.store.list_successions()}
    assert retired == {
        "jane-a": "heir",
        "jane-b": "heir",
        "jane-c": "heir",
        "jane-gone": "heir",
    }
    assert await status_of(w, "heir") is AgentStatus.ACTIVE
    assert await status_of(w, "jane-twin") is AgentStatus.ACTIVE  # "Jane" is another alias


async def test_retire_audits_each_agent_with_the_owner_alias(w: World) -> None:
    await offboard(w)
    retires = {r.target: r.detail for r in await w.audit(AuditAction.AGENT_RETIRE)}
    assert retires["jane-a"] == {"successor": "heir", "was": "active", "offboarded_owner": ALIAS}
    assert retires["jane-gone"]["was"] == "revoked"
    assert set(retires) == {"jane-a", "jane-b", "jane-c", "jane-gone"}
    revokes = {r.target: r.detail for r in await w.audit(AuditAction.AGENT_REVOKE)}
    assert revokes["jane-a"] == {
        "from": "active",
        "via": "retire",
        "successor": "heir",
        "offboarded_owner": ALIAS,
    }
    assert revokes["jane-wait"] == {"from": "pending", "offboarded_owner": ALIAS}
    assert "jane-gone" not in revokes  # already revoked: no second revoke row


async def test_each_retirement_stays_individually_undoable(w: World) -> None:
    note = await w.entry("jane-a", "note")
    other = await w.entry("jane-b", "other")
    await offboard(w)
    await w.successions.undo("jane-a", ADMIN)
    assert await w.get("heir", note.id) == 404
    assert await w.get("heir", other.id) == 200


async def test_revoke_revokes_active_agents_and_rejects_pending_ones(w: World) -> None:
    response = await offboard(w, "revoke", None)
    assert response.status_code == 200, response.text
    assert actions(response) == {
        "jane-a": "revoke",
        "jane-b": "revoke",
        "jane-c": "revoke",
        "jane-gone": "skip",
        "jane-wait": "reject",
    }
    assert {
        n: await status_of(w, n) for n in ("jane-a", "jane-b", "jane-c", "jane-wait")
    } == dict.fromkeys(("jane-a", "jane-b", "jane-c", "jane-wait"), AgentStatus.REVOKED)
    assert await w.store.list_successions() == []
    assert await w.audit(AuditAction.AGENT_RETIRE) == []
    revokes = await w.audit(AuditAction.AGENT_REVOKE)
    assert {r.target for r in revokes} == {"jane-a", "jane-b", "jane-c", "jane-wait"}
    assert all(r.detail["offboarded_owner"] == ALIAS for r in revokes)
    assert (await w.client.get("/v1/entries/x", headers=w.h("jane-a"))).status_code == 401


async def test_a_revoke_dry_run_lists_the_skipped_with_a_reason(w: World) -> None:
    preview = await offboard(w, "revoke", None, dry_run=True)
    gone = next(a for a in preview.json()["agents"] if a["name"] == "jane-gone")
    assert gone["action"] == "skip" and gone["reason"] == "already revoked"
    assert await status_of(w, "jane-a") is AgentStatus.ACTIVE


@pytest.mark.parametrize("path_alias", ["ops/jane", "ops%2Fjane"])
async def test_an_alias_with_a_slash_can_be_offboarded(w: World, path_alias: str) -> None:
    await w.store.set_agent_owner_alias("jane-c", "ops/jane")
    url = f"/v1/admin/owners/{path_alias}/offboard"
    preview = await w.client.post(url, json={"mode": "revoke", "dry_run": True}, headers=ADMIN_H)
    assert preview.status_code == 200
    assert (preview.json()["owner"], actions(preview)) == ("ops/jane", {"jane-c": "revoke"})


@pytest.mark.parametrize("dry_run", [True, False])
async def test_a_body_that_mode_and_successor_do_not_fit_is_a_422(w: World, dry_run: bool) -> None:
    for mode, successor in (("retire", None), ("revoke", "heir")):
        resp = await offboard(w, mode, successor, dry_run=dry_run)
        assert (resp.status_code, resp.json()["error"]["code"]) == (422, "invalid_input")
    bad_mode = await offboard(w, "delete", None, dry_run=dry_run)
    assert bad_mode.status_code == 422
    missing = await w.client.post(
        f"/v1/admin/owners/{ALIAS}/offboard",
        json={"mode": "revoke", "successor": None},
        headers=ADMIN_H,
    )
    assert missing.status_code == 422  # dry_run is required
    assert await status_of(w, "jane-a") is AgentStatus.ACTIVE


@pytest.mark.parametrize("dry_run", [True, False])
async def test_refusals_answer_typed_errors_and_change_nothing(w: World, dry_run: bool) -> None:
    async def code(**kw: object) -> tuple[int, str]:
        resp = await offboard(w, dry_run=dry_run, **kw)
        return resp.status_code, resp.json()["error"]["code"]

    assert await code(successor="jane-b") == (409, "succession_refused")  # the leaver's own agent
    assert await code(successor="jane-wait") == (409, "invalid_status")
    assert await code(successor="jane-gone") == (409, "invalid_status")
    assert await code(successor="ghost") == (404, "not_found")
    unknown = await w.client.post(
        "/v1/admin/owners/nobody/offboard",
        json={"mode": "revoke", "dry_run": dry_run, "expected_agents": []},
        headers=ADMIN_H,
    )
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "unknown_owner")
    lower = await w.client.post(
        "/v1/admin/owners/JANE/offboard",
        json={"mode": "revoke", "dry_run": dry_run, "expected_agents": []},
        headers=ADMIN_H,
    )
    assert lower.status_code == 404  # aliases match exactly as stored
    assert await w.store.list_successions() == []
    assert await status_of(w, "jane-a") is AgentStatus.ACTIVE


async def test_the_refusal_message_names_the_same_owner_problem(w: World) -> None:
    resp = await offboard(w, successor="jane-b", dry_run=True)
    assert "another owner" in resp.json()["error"]["message"]


async def test_one_agents_cycle_refuses_the_whole_offboarding(w: World) -> None:
    await w.store.retire_agent("heir", "jane-a", "x")  # heir hands down to jane-a
    agents = w.store._agents  # type: ignore[attr-defined]
    agents["heir"] = replace(agents["heir"], status=AgentStatus.ACTIVE)  # as an older pod could
    for dry_run in (True, False):
        resp = await offboard(w, dry_run=dry_run)
        assert (resp.status_code, resp.json()["error"]["code"]) == (409, "succession_refused")
        assert "cycle" in resp.json()["error"]["message"]
    assert [s.retired_agent for s in await w.store.list_successions()] == ["heir"]
    for name in ("jane-a", "jane-b", "jane-c"):
        assert await status_of(w, name) is AgentStatus.ACTIVE
    assert await status_of(w, "jane-wait") is AgentStatus.PENDING
    assert await w.audit(AuditAction.AGENT_RETIRE) == []


async def test_an_apply_without_the_preview_is_a_422(w: World) -> None:
    for body in (
        {"mode": "revoke", "dry_run": False},
        {"mode": "revoke", "dry_run": False, "expected_agents": None},
    ):
        resp = await post(w, body)
        assert resp.status_code == 422
    assert await status_of(w, "jane-a") is AgentStatus.ACTIVE


async def test_an_apply_is_refused_when_the_agents_changed_since_the_preview(w: World) -> None:
    preview = await offboard(w, "revoke", None, dry_run=True)
    shown = [{"name": a["name"], "action": a["action"]} for a in preview.json()["agents"]]
    await w.app.access_service.revoke("jane-b", ADMIN)
    stale = await post(w, {"mode": "revoke", "dry_run": False, "expected_agents": shown})
    assert (stale.status_code, stale.json()["error"]["code"]) == (409, "offboard_changed")
    assert "jane-b" in stale.json()["error"]["message"]
    assert await status_of(w, "jane-a") is AgentStatus.ACTIVE
    assert await status_of(w, "jane-wait") is AgentStatus.PENDING
    again = await offboard(w, "revoke", None)
    assert again.status_code == 200 and actions(again)["jane-b"] == "skip"


async def test_revoke_with_any_successor_is_a_422_even_when_it_does_not_exist(w: World) -> None:
    for dry_run in (True, False):
        resp = await offboard(w, "revoke", "ghost", dry_run=dry_run, expected_agents=[])
        assert (resp.status_code, resp.json()["error"]["code"]) == (422, "invalid_input")


@pytest.mark.parametrize("mode", ["retire", "revoke"])
async def test_an_active_agent_with_a_standing_succession_is_revoked_not_skipped(
    w: World, mode: str
) -> None:
    await w.store.retire_agent("jane-c", "heir", "x")
    agents = w.store._agents  # type: ignore[attr-defined]
    agents["jane-c"] = replace(agents["jane-c"], status=AgentStatus.ACTIVE)  # an older pod
    response = await offboard(w, mode, "heir" if mode == "retire" else None)
    assert response.status_code == 200, response.text
    assert actions(response)["jane-c"] == "revoke"
    assert await status_of(w, "jane-c") is AgentStatus.REVOKED
    standing = [s for s in await w.store.list_successions() if s.retired_agent == "jane-c"]
    assert len(standing) == 1 and standing[0].by == "x"  # the old succession, not a new one


async def test_every_key_is_deleted_before_the_first_audit_row(w: World) -> None:
    events: list[str] = []
    authenticator = w.auth
    revoke_key, record = authenticator.revoke_agent_key, w.store.record_audit

    async def logged_revoke(name: str) -> None:
        events.append("key")
        await revoke_key(name)

    async def logged_record(event):  # type: ignore[no-untyped-def]
        events.append("audit")
        return await record(event)

    authenticator.revoke_agent_key = logged_revoke  # type: ignore[method-assign]
    w.store.record_audit = logged_record  # type: ignore[method-assign]
    await offboard(w, "revoke", None)
    assert events == ["key"] * 4 + ["audit"] * 4


async def test_non_admin_keys_get_403_before_anything_is_looked_up(w: World) -> None:
    for headers in (w.h("heir"), {"X-API-Key": "hm_org"}):
        for owner in (ALIAS, "nobody"):
            resp = await w.client.post(
                f"/v1/admin/owners/{owner}/offboard",
                json={
                    "mode": "retire",
                    "successor": "ghost",
                    "dry_run": False,
                    "expected_agents": [],
                },
                headers=headers,
            )
            assert (resp.status_code, resp.json()["error"]["code"]) == (403, "forbidden")
    assert await status_of(w, "jane-a") is AgentStatus.ACTIVE
    assert await w.store.list_audit(AuditFilters(), 100) == []
