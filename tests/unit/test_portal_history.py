"""The agent portal's history (ADR 0123): ``GET /v1/portal/history``.

The rows are real audit rows in the shapes the writers produce. The tests sit
at the API seam and pin the privacy rules: another agent's rows never appear,
the admin's identity never appears, an unreadable fleet is "another fleet", an
entry id survives only while the key can read the entry, key fingerprints and
owner aliases never leave the audit log.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from hivemind.domain.access import TrustLevel
from hivemind.domain.audit import ActorKind, AuditAction, AuditEvent
from hivemind.domain.entry import EntryDraft, Kind
from hivemind.ports import Credential
from tests.fakes import make_embedder
from tests.unit.test_api_endpoints import make_client, make_hivemind_app

CAROL = {"X-API-Key": "key-carol"}  # contributor in blue, inherits from "old"
PAT = {"X-API-Key": "key-pat"}  # privileged in blue: reads every fleet
ZED = {"X-API-Key": "key-zed"}  # dormant (level 0)
ADMIN_ACTOR = "admin:ab12cd34ef56"
ORG_ACTOR = "org:0f0f0f0f0f0f"
CLI_ACTOR = "operator-joe"
SECRETS = (
    "ab12cd34ef56",
    "0f0f0f0f0f0f",
    "operator-joe",
    "fp-old-key",
    "fp-new-key",
    "secret-owner",
)


async def _row(app, kind, actor, action, target, detail=None) -> None:  # type: ignore[no-untyped-def]
    await app.store.record_audit(AuditEvent(kind, actor, action, target, detail or {}))


async def _entry(app, summary: str, **kw: Any):  # type: ignore[no-untyped-def]
    draft = EntryDraft(
        kind=Kind.FACT, summary=summary, author=kw.pop("author", "carol"), agent="a", **kw
    )
    return await app.store.create_entry(draft, await make_embedder().embed_text(summary))


async def _seed():  # type: ignore[no-untyped-def]
    app = make_hivemind_app()
    blue = await app.store.create_fleet("blue")
    red = await app.store.create_fleet("red")
    for key, name, level, inherits in (
        ("key-carol", "carol", TrustLevel.CONTRIBUTOR, ("old",)),
        ("key-pat", "pat", TrustLevel.PRIVILEGED, ()),
        ("key-zed", "zed", TrustLevel.UNTRUSTED, ()),
    ):
        app.authenticator.add_key(  # type: ignore[attr-defined]
            key,
            Credential(
                user_id=name,
                agent_name=name,
                access_controlled=True,
                trust_level=level,
                home_fleet_id=blue.id,
                inherits_from=inherits,
            ),
        )
    return app, blue, red


async def _history(app, headers=CAROL, **params):  # type: ignore[no-untyped-def]
    async with make_client(app) as client:
        resp = await client.get("/v1/portal/history", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_it_lists_the_agents_rows_and_its_predecessors_newest_first() -> None:
    app, blue, _red = await _seed()
    A, K = ActorKind.ADMIN_KEY, ActorKind.AGENT_KEY
    await _row(
        app,
        ActorKind.ORG_KEY,
        ORG_ACTOR,
        AuditAction.AGENT_REGISTER,
        "carol",
        {"owner_alias": "secret-owner"},
    )
    await _row(
        app,
        A,
        ADMIN_ACTOR,
        AuditAction.AGENT_ACTIVATE,
        "carol",
        {"trust_level": 1, "home_fleet_id": blue.id},
    )
    await _row(
        app, A, ADMIN_ACTOR, AuditAction.AGENT_TRUST_LEVEL_SET, "carol", {"from": 1, "to": 2}
    )
    await _row(app, A, ADMIN_ACTOR, AuditAction.AGENT_TRUST_LEVEL_SET, "old", {"from": 2, "to": 3})
    await _row(
        app,
        A,
        ADMIN_ACTOR,
        AuditAction.AGENT_KEY_REPLACE,
        "carol",
        {"old_key": "fp-old-key", "new_key": "fp-new-key"},
    )
    await _row(
        app, K, "carol", AuditAction.AGENT_KEY_LOCK, "carol", {"key": "fp-old-key", "via": "portal"}
    )
    await _row(
        app,
        A,
        ADMIN_ACTOR,
        AuditAction.AGENT_RETIRE,
        "old",
        {"successor": "carol", "was": "active"},
    )
    rows = await _history(app)
    assert [r["action"] for r in rows] == [
        "agent.retire",
        "agent_key.lock",
        "agent_key.replace",
        "agent.trust_level_set",
        "agent.trust_level_set",
        "agent.activate",
        "agent.register",
    ]
    by_action = {(r["action"], r["about"]): r for r in rows}
    assert (
        by_action["agent.retire", "old"]["summary"]
        == "Retired; its memories now belong to this agent"
    )
    assert by_action["agent.retire", "old"]["by"] == "admin"
    lock = by_action["agent_key.lock", "carol"]
    assert (lock["by"], lock["agent"]) == ("agent", "carol")
    assert by_action["agent.trust_level_set", "carol"]["summary"] == (
        "Trust level changed from 1 (lurker) to 2 (contributor)"
    )
    assert by_action["agent.trust_level_set", "old"]["about"] == "old"
    assert by_action["agent.activate", "carol"]["summary"] == "Activated as 1 (lurker) in blue"
    dump = json.dumps(rows)
    for secret in SECRETS:
        assert secret not in dump
    assert set(rows[0]) == {
        "id",
        "occurred_at",
        "action",
        "by",
        "agent",
        "about",
        "summary",
        "reason",
        "entry_id",
    }


async def test_another_agents_rows_never_appear() -> None:
    app, blue, _red = await _seed()
    A, K = ActorKind.ADMIN_KEY, ActorKind.AGENT_KEY
    bobs = await _entry(app, "bob note", author="bob", scope="fleet", fleet_id=blue.id)
    await _row(app, A, ADMIN_ACTOR, AuditAction.AGENT_TRUST_LEVEL_SET, "bob", {"from": 1, "to": 3})
    await _row(app, A, ADMIN_ACTOR, AuditAction.AGENT_REVOKE, "bob", {"from": "active"})
    await _row(app, K, "bob", AuditAction.ENTRY_PIN, bobs.id, {"fleet_id": blue.id})
    await _row(
        app, K, "bob", AuditAction.AGENT_KEY_LOCK, "bob", {"key": "fp-old-key", "via": "portal"}
    )
    await _row(
        app,
        A,
        ADMIN_ACTOR,
        AuditAction.ENTRY_WITHDRAW,
        bobs.id,
        {"author": "bob", "reason": "stale"},
    )
    await _row(
        app,
        A,
        ADMIN_ACTOR,
        AuditAction.AGENT_RETIRE,
        "bob",
        {"successor": "bobby", "was": "active"},
    )
    # A fleet or an entry whose id equals an agent's name is not that agent's row.
    await _row(app, A, ADMIN_ACTOR, AuditAction.FLEET_RENAME, "carol", {"from": "x", "to": "y"})
    await _row(app, A, ADMIN_ACTOR, AuditAction.ENTRY_PIN, "carol", {"fleet_id": blue.id})
    # A cli actor and an agent_key actor spelled like the agent are not it either.
    await _row(app, ActorKind.CLI, "carol", AuditAction.AGENT_REVOKE, "bob", {"from": "active"})
    assert await _history(app) == []
    assert await _history(app, PAT) == []


async def test_a_move_that_lists_the_agent_shows_no_other_agent_and_no_unreadable_fleet() -> None:
    app, blue, red = await _seed()
    move = {
        "move_id": "m1",
        "target_fleet_id": blue.id,
        "target_fleet_name": "blue",
        "agents": ["carol", "bob", "dave"],
        "entries": 3,
    }
    await _row(app, ActorKind.ADMIN_KEY, ADMIN_ACTOR, AuditAction.FLEET_MOVE_AGENTS, red.id, move)
    await _row(
        app,
        ActorKind.ADMIN_KEY,
        ADMIN_ACTOR,
        AuditAction.FLEET_MOVE_AGENTS,
        red.id,
        {**move, "agents": ["bob"]},
    )
    (row,) = await _history(app)
    assert row["summary"] == "Moved from another fleet to blue"
    assert row["about"] == "carol"
    dump = json.dumps(row)
    assert "bob" not in dump and "dave" not in dump and '"red"' not in dump and "red " not in dump
    assert red.id not in dump and blue.id not in dump
    # A privileged key can read every fleet, so it may know red.
    await _row(
        app,
        ActorKind.ADMIN_KEY,
        ADMIN_ACTOR,
        AuditAction.FLEET_MOVE_AGENTS,
        red.id,
        {**move, "agents": ["pat"]},
    )
    (pat_row,) = await _history(app, PAT)
    assert pat_row["summary"] == "Moved from red to blue"


async def test_home_fleet_changes_name_only_fleets_the_key_can_read() -> None:
    app, blue, red = await _seed()
    await _row(
        app,
        ActorKind.ADMIN_KEY,
        ADMIN_ACTOR,
        AuditAction.AGENT_HOME_FLEET_SET,
        "carol",
        {"from": red.id, "to": blue.id},
    )
    await _row(
        app,
        ActorKind.ADMIN_KEY,
        ADMIN_ACTOR,
        AuditAction.AGENT_HOME_FLEET_SET,
        "pat",
        {"from": red.id, "to": blue.id},
    )
    (carol,) = await _history(app)
    (pat,) = await _history(app, PAT)
    assert carol["summary"] == "Home fleet changed from another fleet to blue"
    assert pat["summary"] == "Home fleet changed from red to blue"
    assert red.id not in json.dumps(carol)


async def test_withdrawals_show_the_reason_never_the_admin_and_link_only_readable_entries() -> None:
    app, blue, _red = await _seed()
    visible = await _entry(app, "carol fleet note", scope="fleet", fleet_id=blue.id)
    hidden = await _entry(app, "carol private note", author="carol", scope="self")
    # Visible to carol, but carol cannot read it for pat; and one that is gone from carol's reach:
    moved = await _entry(
        app,
        "bob note in another fleet",
        author="bob",
        scope="fleet",
        fleet_id=(await app.store.create_fleet("green")).id,
    )
    A = ActorKind.ADMIN_KEY
    await _row(
        app,
        A,
        ADMIN_ACTOR,
        AuditAction.ENTRY_WITHDRAW,
        visible.id,
        {"author": "carol", "reason": "outdated"},
    )
    await _row(
        app,
        ActorKind.AGENT_KEY,
        "carol",
        AuditAction.ENTRY_WITHDRAW,
        hidden.id,
        {"author": "carol", "reason": "mine", "via": "portal"},
    )
    await _row(
        app,
        ActorKind.AGENT_KEY,
        "carol",
        AuditAction.ENTRY_WITHDRAW,
        moved.id,
        {"author": "old", "reason": "inherited", "via": "succession"},
    )
    rows = {r["reason"]: r for r in await _history(app)}
    assert rows["outdated"]["entry_id"] == visible.id
    assert rows["outdated"]["by"] == "admin" and rows["outdated"]["agent"] is None
    assert rows["outdated"]["summary"] == "A memory by this agent was withdrawn"
    assert rows["mine"]["entry_id"] == hidden.id
    assert rows["mine"]["summary"] == "Withdrew a memory" and rows["mine"]["agent"] == "carol"
    # carol cannot read an entry in green (not her fleet): the row stays, the link goes.
    assert rows["inherited"]["entry_id"] is None
    assert rows["inherited"]["summary"] == "Withdrew a memory inherited from old"
    assert ADMIN_ACTOR not in json.dumps(rows)


async def test_a_restore_shows_as_an_admin_restoring_a_memory_without_naming_the_admin() -> None:
    app, blue, _red = await _seed()
    visible = await _entry(app, "carol fleet note", scope="fleet", fleet_id=blue.id)
    elsewhere = await _entry(
        app,
        "bob note in another fleet",
        author="bob",
        scope="fleet",
        fleet_id=(await app.store.create_fleet("green")).id,
    )
    for entry, author, reason in (
        (visible, "carol", "it was right"),
        (elsewhere, "carol", "also right"),
    ):
        await _row(
            app,
            ActorKind.ADMIN_KEY,
            ADMIN_ACTOR,
            AuditAction.ENTRY_RESTORE,
            entry.id,
            {"author": author, "previous_reason": "secret old reason", "reason": reason},
        )
    rows = await _history(app)
    mine = {r["reason"]: r for r in rows if r["action"] == "entry.restore" and r["reason"]}
    assert mine["it was right"]["summary"] == "An admin restored a memory by this agent"
    assert mine["it was right"]["by"] == "admin" and mine["it was right"]["agent"] is None
    assert mine["it was right"]["entry_id"] == visible.id
    assert mine["also right"]["entry_id"] is None  # carol cannot read it
    text = json.dumps(rows)
    assert ADMIN_ACTOR not in text and "secret old reason" not in text


async def test_a_redaction_shows_as_an_admin_redacting_a_memory_with_no_counts() -> None:
    app, blue, _red = await _seed()
    mine = await _entry(app, "carol fleet note", scope="fleet", fleet_id=blue.id)
    elsewhere = await _entry(
        app,
        "bob note in another fleet",
        author="bob",
        scope="fleet",
        fleet_id=(await app.store.create_fleet("green")).id,
    )
    for entry, author, reason in ((mine, "carol", "token in body"), (elsewhere, "bob", "bobs")):
        await _row(
            app,
            ActorKind.ADMIN_KEY,
            ADMIN_ACTOR,
            AuditAction.ENTRY_REDACT,
            entry.id,
            {
                "author": author,
                "reason": reason,
                "feedback_notes": 7,
                "withdraw_reasons": 5,
            },
        )
    rows = await _history(app)
    redactions = [r for r in rows if r["action"] == "entry.redact"]
    # Only the rows about this agent: bob's redaction is not in carol's history.
    assert len(redactions) == 1
    (row,) = redactions
    assert row["summary"] == "An admin redacted a memory by this agent"
    assert row["reason"] == "token in body"
    assert row["by"] == "admin" and row["agent"] is None and row["entry_id"] == mine.id
    text = json.dumps(rows)
    assert ADMIN_ACTOR not in text
    assert "feedback" not in text and "withdraw_reasons" not in text and " 7 " not in text


async def test_a_withdrawn_entry_that_is_gone_from_sight_loses_its_link() -> None:
    app, _blue, _red = await _seed()
    await _row(
        app,
        ActorKind.ADMIN_KEY,
        ADMIN_ACTOR,
        AuditAction.ENTRY_WITHDRAW,
        "00000000-0000-4000-8000-0000000000aa",
        {"author": "carol", "reason": "gone"},
    )
    (row,) = await _history(app)
    assert row["entry_id"] is None and row["reason"] == "gone"


async def test_pins_and_unpins_by_the_agent_and_a_cleanup_by_an_admin() -> None:
    app, blue, red = await _seed()
    K, A = ActorKind.AGENT_KEY, ActorKind.ADMIN_KEY
    note = await _entry(app, "carol pinned", scope="fleet", fleet_id=blue.id)
    await _row(
        app, K, "carol", AuditAction.ENTRY_PIN, note.id, {"fleet_id": blue.id, "via": "portal"}
    )
    await _row(
        app,
        K,
        "carol",
        AuditAction.ENTRY_UNPIN,
        note.id,
        {"fleet_id": red.id, "pin_ids": [note.id], "reason": "withdrawn"},
    )
    await _row(
        app,
        A,
        ADMIN_ACTOR,
        AuditAction.AGENT_CLEANUP,
        "carol",
        {
            "cleanup_id": "c1",
            "since": "2026-10-01T00:00:00+00:00",
            "until": "2026-10-02T12:30:00+00:00",
            "withdrawn": 1,
            "ratings_set_aside": 2,
            "restored": 0,
            "pins_removed": 1,
            "also_revoke": True,
        },
    )
    cleanup, unpin, pin = await _history(app)
    assert pin["summary"] == "Pinned a memory in blue" and pin["entry_id"] == note.id
    assert unpin["summary"] == "Unpinned a memory (the memory was withdrawn)"
    assert cleanup["by"] == "admin"
    assert cleanup["summary"] == (
        "Cleanup of 2026-10-01 00:00 UTC to 2026-10-02 12:30 UTC: "
        "1 memory withdrawn, 2 ratings set aside, 0 memories restored, 1 pin removed"
    )
    # The row's own random id may contain "c1"; the cleanup id must not leak anywhere else.
    assert "c1" not in json.dumps({k: v for k, v in cleanup.items() if k != "id"})


async def test_a_retirement_that_was_undone_names_no_agent_the_key_does_not_own() -> None:
    app, _blue, _red = await _seed()
    A = ActorKind.ADMIN_KEY
    await _row(
        app,
        A,
        ADMIN_ACTOR,
        AuditAction.AGENT_RETIRE,
        "old",
        {"successor": "stranger", "was": "active"},
    )
    await _row(app, A, ADMIN_ACTOR, AuditAction.AGENT_RETIRE_UNDO, "old", {"successor": "stranger"})
    await _row(
        app,
        A,
        ADMIN_ACTOR,
        AuditAction.AGENT_REVOKE,
        "old",
        {"from": "active", "via": "retire", "successor": "stranger"},
    )
    rows = await _history(app)
    assert [r["summary"] for r in rows] == [
        "Revoked when it was retired in favour of another agent",
        "Retirement undone; its memories no longer belong to another agent",
        "Retired; its memories now belong to another agent",
    ]
    assert "stranger" not in json.dumps(rows)


async def test_it_pages_with_before_and_limit() -> None:
    app, _blue, _red = await _seed()
    for n in range(5):
        await _row(
            app,
            ActorKind.ADMIN_KEY,
            ADMIN_ACTOR,
            AuditAction.AGENT_TRUST_LEVEL_SET,
            "carol",
            {"from": n, "to": n + 1},
        )
    first = await _history(app, limit=2)
    second = await _history(app, limit=2, before=first[-1]["id"])
    third = await _history(app, limit=2, before=second[-1]["id"])
    assert [len(first), len(second), len(third)] == [2, 2, 1]
    everything = await _history(app)
    assert [r["id"] for r in first + second + third] == [r["id"] for r in everything]
    assert len({r["id"] for r in everything}) == 5
    assert await _history(app, before="00000000-0000-4000-8000-000000000001") == []


async def test_a_dormant_agent_is_refused_and_a_bad_limit_is_a_422() -> None:
    app, _blue, _red = await _seed()
    async with make_client(app) as client:
        dormant = await client.get("/v1/portal/history", headers=ZED)
        zero = await client.get("/v1/portal/history", params={"limit": 0}, headers=CAROL)
        none = await client.get("/v1/portal/history")
    assert dormant.status_code == 403
    assert zero.status_code == 422
    assert none.status_code == 401


@pytest.mark.parametrize("what", ["locked", "revoked"])
async def test_a_locked_or_revoked_key_gets_the_usual_refusal(what: str) -> None:
    from tests.unit.test_key_lock import ADMIN, _active_agent, _app, _client

    app = _app()
    key = await _active_agent(app, "carol")
    fake = app.authenticator._by_key[key]  # type: ignore[attr-defined]
    app.authenticator._by_key[key] = replace(  # type: ignore[attr-defined]
        fake, access_controlled=True, trust_level=TrustLevel.CONTRIBUTOR
    )
    headers = {"X-API-Key": key}
    async with _client(app) as client:
        assert (await client.get("/v1/portal/history", headers=headers)).status_code == 200
        if what == "locked":
            await client.post("/v1/portal/key/lock", headers=headers)
            expected = "key_locked"
        else:
            await app.access_service.revoke("carol", ADMIN)
            expected = None
        resp = await client.get("/v1/portal/history", headers=headers)
    assert resp.status_code == 401
    if expected:
        assert resp.json()["error"]["code"] == expected


async def test_another_agents_withdrawal_after_an_undone_retirement_is_not_this_agent() -> None:
    """A retired to B, B withdrew A's memory, the retirement was undone and A went to C:
    C's history still matches the row through ``author``, but B is not C."""
    app, blue, _red = await _seed()
    note = await _entry(app, "old note", author="old", scope="fleet", fleet_id=blue.id)
    await _row(
        app,
        ActorKind.AGENT_KEY,
        "bee",
        AuditAction.ENTRY_WITHDRAW,
        note.id,
        {"author": "old", "reason": "bee's own words", "via": "succession"},
    )
    (row,) = await _history(app)
    assert row["by"] == "another_agent" and row["agent"] is None
    assert row["summary"] == "A memory by old was withdrawn"
    assert row["reason"] is None
    # "bee" is all hex letters, so a random id can contain it: check the rest of the row.
    assert "bee" not in json.dumps({k: v for k, v in row.items() if k not in ("id", "entry_id")})


async def test_a_cli_operator_named_like_an_inherited_agent_is_not_named() -> None:
    app, _blue, _red = await _seed()
    await _row(app, ActorKind.CLI, "old", AuditAction.AGENT_REVOKE, "carol", {"from": "active"})
    await _row(
        app, ActorKind.CLI, "old", AuditAction.AGENT_RETIRE, "stranger", {"successor": "carol"}
    )
    await _row(app, ActorKind.CLI, "carol", AuditAction.AGENT_KEY_ISSUE, "old", {})
    rows = await _history(app)
    assert {r["about"] for r in rows} <= {"carol", "old"}
    revoke = next(r for r in rows if r["action"] == "agent.revoke")
    assert (revoke["by"], revoke["agent"], revoke["about"]) == ("admin", None, "carol")
    retire = next(r for r in rows if r["action"] == "agent.retire")
    assert retire["about"] == "carol"  # the successor, never the operator
    assert retire["by"] == "admin"


async def test_an_owner_change_shows_no_alias() -> None:
    app, _blue, _red = await _seed()
    await _row(
        app,
        ActorKind.ADMIN_KEY,
        ADMIN_ACTOR,
        AuditAction.AGENT_OWNER_SET,
        "carol",
        {"from": "jane.doe", "to": "secret-owner"},
    )
    (row,) = await _history(app)
    assert row["summary"] == "The owner recorded for it was changed"
    assert "jane.doe" not in json.dumps(row) and "secret-owner" not in json.dumps(row)


async def test_a_role_change_shows_in_the_agents_history() -> None:
    app, _blue, _red = await _seed()
    for action in (AuditAction.AGENT_ROLE_SET, AuditAction.AGENT_ROLE_CLEAR):
        await _row(app, ActorKind.ADMIN_KEY, ADMIN_ACTOR, action, "carol", {"role": "scout"})
    summaries = {r["action"]: r["summary"] for r in await _history(app)}
    assert summaries["agent.role_set"] == "Marked as a scout"
    assert summaries["agent.role_clear"] == "No longer a scout"


async def test_a_move_to_self_names_who_moved_it_and_shows_the_reason_for_own_memories() -> None:
    app, blue, _red = await _seed()
    by_admin = await _entry(app, "carol note the admin moved", scope="self", fleet_id=blue.id)
    by_carol = await _entry(app, "carol note she moved", scope="self", fleet_id=blue.id)
    inherited = await _entry(app, "old note", author="old", scope="self", fleet_id=blue.id)
    for actor_kind, actor, entry, author, reason in (
        (ActorKind.ADMIN_KEY, ADMIN_ACTOR, by_admin, "carol", "meant to be private"),
        (ActorKind.AGENT_KEY, "carol", by_carol, "carol", "a scratch note"),
        (ActorKind.AGENT_KEY, "carol", inherited, "old", "old's scratch"),
    ):
        await _row(
            app,
            actor_kind,
            actor,
            AuditAction.ENTRY_MOVE_TO_SELF,
            entry.id,
            {"author": author, "fleet_id": blue.id, "reason": reason, "earlier_versions": []},
        )
    rows = {r["reason"]: r for r in await _history(app) if r["action"] == "entry.move_to_self"}
    assert rows["meant to be private"]["summary"] == "An admin moved a memory by this agent to self"
    assert rows["meant to be private"]["by"] == "admin"
    assert rows["a scratch note"]["summary"] == "Moved a memory to self"
    assert rows["a scratch note"]["entry_id"] == by_carol.id
    assert rows["old's scratch"]["summary"] == "Moved a memory inherited from old to self"
    assert ADMIN_ACTOR not in json.dumps(rows)


@pytest.mark.parametrize(
    "credential",
    [
        Credential(user_id="admin", agent_name="carol", is_admin=True, key_id="abcdef012345"),
        Credential(user_id="org", agent_name="carol", is_org=True, access_controlled=True),
        Credential(user_id="dev", access_controlled=True),
    ],
    ids=["admin", "org", "no-agent-name"],
)
async def test_the_history_service_itself_refuses_a_key_that_is_not_an_agents(
    credential: Credential,
) -> None:
    """The route checks first; the service holds on its own (ADR 0123)."""
    from hivemind.services.errors import PermissionDenied

    app, _blue, _red = await _seed()
    with pytest.raises(PermissionDenied, match="agent key"):
        await app.access_service.history.list_own(credential, before=None, limit=50)
