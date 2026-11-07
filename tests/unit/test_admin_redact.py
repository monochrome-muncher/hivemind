"""Redacting a withdrawn memory (ADR 0125): ``POST /v1/admin/entries/{id}/redact``."""

from __future__ import annotations

import json

import pytest

from hivemind.admin.app import STATIC_DIR as ADMIN_STATIC
from hivemind.domain.audit import AuditAction, AuditFilters
from hivemind.domain.entry import EntityKind, EntryDraft, ExtractedEntity, Kind
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.portal.app import STATIC_DIR as PORTAL_STATIC
from tests.fakes import make_embedder
from tests.unit.test_api_endpoints import make_client, make_hivemind_app

ADMIN = {"X-API-Key": "key-admin"}
ALICE = {"X-API-Key": "key-alice"}
ORG = {"X-API-Key": "key-org"}
SECRET = "hunter2canary"


async def _secret(app):  # type: ignore[no-untyped-def]
    draft = EntryDraft(
        kind=Kind.FACT,
        summary=f"the {SECRET} password",
        body=f"login with {SECRET}",
        author="alice",
        agent="a",
        tags=(SECRET,),
    )
    return await app.store.create_entry(
        draft,
        await make_embedder().embed_text(draft.summary),
        entities=(ExtractedEntity(SECRET, EntityKind.SYSTEM),),
    )


def _body(entry_id: str, **over: str) -> dict[str, str]:
    return {"reason": "a credential leaked", "confirm": entry_id, **over}


async def test_redact_wipes_the_text_audits_it_and_reports_the_notes_to_review() -> None:
    app = make_hivemind_app()
    entry = await _secret(app)
    await app.store.record_feedback(
        Feedback(entry.id, "bob", "b", Verdict.WRONG, f"it says {SECRET}")
    )
    async with make_client(app) as client:
        await client.post(
            f"/v1/admin/entries/{entry.id}/withdraw", json={"reason": "leaked"}, headers=ADMIN
        )
        resp = await client.post(
            f"/v1/admin/entries/{entry.id}/redact", json=_body(entry.id), headers=ADMIN
        )
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert (out["feedback_notes"], out["flag_notes"], out["withdraw_reasons"]) == (1, 1, 1)
        assert out["entry"]["redacted"] is True and out["entry"]["redacted_at"]
        assert out["entry"]["summary"] == "[redacted]" and out["entry"]["body"] is None
        assert out["entry"]["state"] == "withdrawn"

        # The admin read still works, with the marker and the stub, history included.
        read = (await client.get(f"/v1/admin/entries/{entry.id}", headers=ADMIN)).json()
        assert read["redacted"] is True and read["summary"] == "[redacted]"
        assert read["tags"] == [] and read["entities"] == [] and read["sources"] == []
        listed = (await client.get("/v1/admin/entries?include_inactive=true", headers=ADMIN)).json()
        assert [(e["id"], e["redacted"]) for e in listed] == [(entry.id, True)]

        # Not findable by an agent, even asking for inactive memories.
        found = await client.get("/v1/entries?include_inactive=true&tags=" + SECRET, headers=ALICE)
        assert found.json() == []
        # The agent-facing shape is unchanged: no redaction fields.
        agent_view = (await client.get(f"/v1/entries/{entry.id}", headers=ALICE)).json()
        assert "redacted" not in agent_view and "redacted_at" not in agent_view
        assert agent_view["summary"] == "[redacted]" and agent_view["state"] == "withdrawn"

    (row,) = await app.store.list_audit(AuditFilters(action=AuditAction.ENTRY_REDACT), 10)
    assert row.target == entry.id and row.actor_kind.value == "admin_key"
    assert dict(row.detail) == {
        "author": "alice",
        "reason": "a credential leaked",
        "feedback_notes": 1,
        "withdraw_reasons": 1,
    }
    # The withdrawal's own audit row is never edited.
    (withdrawal,) = await app.store.list_audit(AuditFilters(action=AuditAction.ENTRY_WITHDRAW), 10)
    assert withdrawal.detail["reason"] == "leaked"
    for record in await app.store.list_audit(AuditFilters(), 50):
        assert SECRET not in json.dumps(dict(record.detail))


async def test_admin_only_is_checked_before_anything_else() -> None:
    app = make_hivemind_app()
    entry = await _secret(app)
    async with make_client(app) as client:
        await client.post(f"/v1/admin/entries/{entry.id}/withdraw", json={}, headers=ADMIN)
        for headers in (ALICE, ORG):
            for body in (_body(entry.id), _body("wrong"), {}):
                resp = await client.post(
                    f"/v1/admin/entries/{entry.id}/redact", json=body, headers=headers
                )
                assert resp.status_code == 403, (headers, body)
        no_key = await client.post(f"/v1/admin/entries/{entry.id}/redact", json=_body(entry.id))
        assert no_key.status_code == 401
    got = await app.store.get_entry(entry.id)
    assert got is not None and got.redacted_at is None and SECRET in got.summary


async def test_the_typed_back_id_must_match_and_the_reason_is_required() -> None:
    app = make_hivemind_app()
    entry = await _secret(app)
    async with make_client(app) as client:
        await client.post(f"/v1/admin/entries/{entry.id}/withdraw", json={}, headers=ADMIN)
        for body in (
            _body(entry.id, confirm="nope"),
            _body(entry.id, confirm=""),
            _body(entry.id, reason="   "),
            {"reason": "x"},
            {},
        ):
            resp = await client.post(
                f"/v1/admin/entries/{entry.id}/redact", json=body, headers=ADMIN
            )
            assert resp.status_code == 422, body
    got = await app.store.get_entry(entry.id)
    assert got is not None and got.redacted_at is None
    assert await app.store.list_audit(AuditFilters(action=AuditAction.ENTRY_REDACT), 10) == []


async def test_only_inactive_unredacted_memories_can_be_redacted() -> None:
    app = make_hivemind_app()
    entry = await _secret(app)
    async with make_client(app) as client:
        active = await client.post(
            f"/v1/admin/entries/{entry.id}/redact", json=_body(entry.id), headers=ADMIN
        )
        assert active.status_code == 409
        assert "withdraw" in active.json()["error"]["message"]
        await client.post(f"/v1/admin/entries/{entry.id}/withdraw", json={}, headers=ADMIN)
        assert (
            await client.post(
                f"/v1/admin/entries/{entry.id}/redact", json=_body(entry.id), headers=ADMIN
            )
        ).status_code == 200
        again = await client.post(
            f"/v1/admin/entries/{entry.id}/redact", json=_body(entry.id), headers=ADMIN
        )
        assert again.status_code == 409
        unknown = "00000000-0000-4000-8000-0000000000ff"
        gone = await client.post(
            f"/v1/admin/entries/{unknown}/redact", json=_body(unknown), headers=ADMIN
        )
        assert gone.status_code == 404
    assert len(await app.store.list_audit(AuditFilters(action=AuditAction.ENTRY_REDACT), 10)) == 1


async def test_a_superseded_memory_can_be_redacted_and_history_shows_the_stub() -> None:
    app = make_hivemind_app()
    old = await _secret(app)
    head = await app.store.create_entry(
        EntryDraft(
            kind=Kind.FACT, summary="rotated", author="alice", agent="a", supersedes=(old.id,)
        ),
        None,
    )
    async with make_client(app) as client:
        resp = await client.post(
            f"/v1/admin/entries/{old.id}/redact", json=_body(old.id), headers=ADMIN
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["entry"]["state"] == "superseded"
        read = (await client.get(f"/v1/admin/entries/{head.id}", headers=ADMIN)).json()
        (prior,) = read["history"]["superseded"]
        assert (prior["summary"], prior["redacted"]) == ("[redacted]", True)
        agent = (await client.get(f"/v1/entries/{head.id}?history=true", headers=ALICE)).json()
        assert agent["history"]["superseded"][0]["summary"] == "[redacted]"
        active = await client.post(
            f"/v1/admin/entries/{head.id}/redact", json=_body(head.id), headers=ADMIN
        )
        assert active.status_code == 409


async def test_the_history_walk_shows_the_stub() -> None:
    app = make_hivemind_app()
    old = await app.store.create_entry(
        EntryDraft(kind=Kind.FACT, summary="v1", author="alice", agent="a"), None
    )
    head = await app.store.create_entry(
        EntryDraft(
            kind=Kind.FACT, summary=f"v2 {SECRET}", author="alice", agent="a", supersedes=(old.id,)
        ),
        None,
    )
    await app.store.withdraw_entry(head.id, None, by_user="alice")
    await app.store.redact_entry(head.id)
    async with make_client(app) as client:
        read = (await client.get(f"/v1/admin/entries/{old.id}", headers=ADMIN)).json()
        (successor,) = read["history"]["successors"]
        assert (successor["summary"], successor["redacted"]) == ("[redacted]", True)
        agent = (await client.get(f"/v1/entries/{old.id}?history=true", headers=ALICE)).json()
        (a_succ,) = agent["history"]["successors"]
        assert a_succ["summary"] == "[redacted]" and "redacted" not in a_succ


def test_the_redact_action_is_passed_in_by_the_admin_panel_and_never_reaches_the_portal() -> None:
    drawer = (ADMIN_STATIC / "drawer.js").read_text()
    assert "redactEntry: null" in drawer  # the shared drawer offers it only when handed one
    assert "redact" in (ADMIN_STATIC / "admin.js").read_text()
    for script in PORTAL_STATIC.glob("*.js"):
        text = script.read_text()
        if script.name != "drawer.js":
            assert "redact" not in text.lower(), script.name


@pytest.mark.parametrize("action", ["entry.redact"])
def test_the_audit_page_filters_by_the_new_action(action: str) -> None:
    assert f'"{action}"' in (ADMIN_STATIC / "manage.js").read_text()
