"""Pinned entries (ADR 0058): a privileged agent of a fleet (or an admin)
pins a few fleet entries; every agent of the fleet reads them first."""

from __future__ import annotations

from dataclasses import replace

import httpx

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.domain.audit import ActorKind, AuditAction, AuditFilters, AuditRecord
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.domain.validation import MAX_PINS_PER_FLEET
from hivemind.mcp.app import (
    McpHivemind,
    hive_pin,
    hive_pinned,
    hive_withdraw,
    hive_write,
)
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.services.governance import GovernanceService, WriteService
from hivemind.services.search import SearchService
from tests.fakes import FakeAuthenticator, make_clock, make_embedder, make_search_config


def _agent(name: str, level: TrustLevel, fleet: str = "fleet-a") -> Credential:
    return Credential(
        user_id=name,
        agent_id=name,
        agent_name=name,
        access_controlled=True,
        trust_level=level,
        home_fleet_id=fleet,
    )


LEAD = _agent("lead", TrustLevel.PRIVILEGED)
DEV = _agent("dev", TrustLevel.CONTRIBUTOR)
OTHER_LEAD = _agent("other-lead", TrustLevel.PRIVILEGED, "fleet-b")
OUTSIDER = _agent("outsider", TrustLevel.CONTRIBUTOR, "fleet-b")
ADMIN = Credential(user_id="admin", is_admin=True)


def _surface(store: MemoryStore, credential: Credential) -> McpHivemind:
    clock, embedder, config = make_clock(), make_embedder(), make_search_config()
    return McpHivemind(
        store=store,
        write_service=WriteService(store, embedder),
        search_service=SearchService(store, embedder, config, now_fn=clock),
        governance_service=GovernanceService(store),
        access_service=AccessService(store),
        search_config=config,
        credential=credential,
    )


async def _write(store: MemoryStore, cred: Credential, summary: str, **kw: object) -> str:
    result = await hive_write(_surface(store, cred), kind="decision", summary=summary, **kw)  # type: ignore[arg-type]
    assert "error" not in result, result
    return str(result["id"])


def _pinned_ids(result: dict[str, object]) -> list[str]:
    return [str(p["id"]) for p in result["pins"]]  # type: ignore[attr-defined, index]


async def test_a_privileged_member_pins_and_every_member_reads_it() -> None:
    store = MemoryStore(make_clock())
    entry = await _write(store, DEV, "deploys freeze on Fridays")
    pinned = await hive_pin(_surface(store, LEAD), entry)
    assert pinned["pinned"] is True and pinned["fleet_id"] == "fleet-a"
    assert pinned["pinned_by"] == "lead"
    briefing = await hive_pinned(_surface(store, DEV))
    assert briefing["fleet_id"] == "fleet-a"
    assert _pinned_ids(briefing) == [entry]
    pin = briefing["pins"][0]  # type: ignore[index]
    assert pin["summary"] == "deploys freeze on Fridays" and pin["state"] == "active"
    # Pinning again refreshes the one pin (ADR 0076).
    again = await hive_pin(_surface(store, LEAD), entry)
    assert again["entry_id"] == entry
    assert _pinned_ids(await hive_pinned(_surface(store, DEV))) == [entry]


async def test_only_privileged_members_or_admins_may_pin() -> None:
    store = MemoryStore(make_clock())
    entry = await _write(store, DEV, "a fleet decision")
    denied = await hive_pin(_surface(store, DEV), entry)
    assert denied["error"]["code"] == "permission_denied"  # type: ignore[index]
    # A privileged agent of another fleet reads the entry but may not pin it.
    foreign = await hive_pin(_surface(store, OTHER_LEAD), entry)
    assert foreign["error"]["code"] == "permission_denied"  # type: ignore[index]
    assert (await hive_pin(_surface(store, ADMIN), entry))["pinned"] is True


async def test_only_active_fleet_entries_can_be_pinned() -> None:
    store = MemoryStore(make_clock())
    own_note = await _write(store, LEAD, "my own note", scope="self")
    result = await hive_pin(_surface(store, LEAD), own_note)
    assert result["error"]["code"] == "invalid_input"  # type: ignore[index]
    gone = await _write(store, LEAD, "soon withdrawn")
    await hive_withdraw(_surface(store, LEAD), gone)
    inactive = await hive_pin(_surface(store, LEAD), gone)
    assert inactive["error"]["code"] == "not_active"  # type: ignore[index]
    missing = await hive_pin(_surface(store, LEAD), "00000000-0000-0000-0000-000000000000")
    assert missing["error"]["code"] == "not_found"  # type: ignore[index]


async def test_a_fleet_holds_a_bounded_number_of_pins() -> None:
    store = MemoryStore(make_clock())
    lead = _surface(store, LEAD)
    ids = [await _write(store, LEAD, f"decision {i}") for i in range(MAX_PINS_PER_FLEET + 1)]
    for entry in ids[:MAX_PINS_PER_FLEET]:
        assert (await hive_pin(lead, entry))["pinned"] is True
    full = await hive_pin(lead, ids[-1])
    assert full["error"]["code"] == "invalid_input"  # type: ignore[index]
    assert (await hive_pin(lead, ids[0], unpin=True))["removed"] is True
    assert (await hive_pin(lead, ids[-1]))["pinned"] is True


async def test_unpin_reports_whether_it_was_pinned() -> None:
    store = MemoryStore(make_clock())
    entry = await _write(store, LEAD, "pinned for a while")
    lead = _surface(store, LEAD)
    await hive_pin(lead, entry)
    assert (await hive_pin(lead, entry, unpin=True))["removed"] is True
    assert (await hive_pin(lead, entry, unpin=True))["removed"] is False
    assert (await hive_pinned(_surface(store, DEV)))["pins"] == []


async def test_a_pin_follows_supersession_and_shows_withdrawal() -> None:
    store = MemoryStore(make_clock())
    lead = _surface(store, LEAD)
    old = await _write(store, LEAD, "use runner pool A")
    await hive_pin(lead, old)
    new = await _write(store, DEV, "use runner pool B", supersedes=[old])
    briefing = await hive_pinned(_surface(store, DEV))
    pin = briefing["pins"][0]  # type: ignore[index]
    assert (pin["id"], pin["pinned_id"]) == (new, old)
    assert pin["summary"] == "use runner pool B"

    gone = await _write(store, LEAD, "temporary rule")
    await hive_pin(lead, gone)
    await hive_withdraw(lead, gone)
    states = {p["id"]: p["state"] for p in (await hive_pinned(lead))["pins"]}  # type: ignore[attr-defined]
    assert states[gone] == "withdrawn"


async def test_the_version_a_pin_shows_unpins_it_and_does_not_pin_twice() -> None:
    """Readers see the newest version's id once a pinned entry is
    superseded, so that id must work for unpinning, and pinning it again
    must not add a second pin that shows the same entry twice."""
    store = MemoryStore(make_clock())
    lead = _surface(store, LEAD)
    old = await _write(store, LEAD, "use runner pool A")
    await hive_pin(lead, old)
    new = await _write(store, DEV, "use runner pool B", supersedes=[old])

    again = await hive_pin(lead, new)
    assert again["entry_id"] == new  # the old pin moved to the version it shows
    briefing = await hive_pinned(lead)
    assert _pinned_ids(briefing) == [new]
    assert briefing["pins"][0]["pinned_id"] == new  # type: ignore[index]

    assert (await hive_pin(lead, new, unpin=True))["removed"] is True
    assert (await hive_pinned(lead))["pins"] == []
    assert (await hive_pin(lead, new, unpin=True))["removed"] is False


async def test_two_pins_that_lead_to_one_version_show_it_once() -> None:
    store = MemoryStore(make_clock())
    lead = _surface(store, LEAD)
    a = await _write(store, LEAD, "rule A")
    b = await _write(store, LEAD, "rule B")
    await hive_pin(lead, a)
    await hive_pin(lead, b)
    merged = await _write(store, LEAD, "rules A and B", supersedes=[a, b])
    assert _pinned_ids(await hive_pinned(lead)) == [merged]
    assert (await hive_pin(lead, merged, unpin=True))["removed"] is True
    assert (await hive_pinned(lead))["pins"] == []


async def test_another_fleets_pins_are_empty_to_a_contributor() -> None:
    store = MemoryStore(make_clock())
    entry = await _write(store, LEAD, "fleet-a only")
    await hive_pin(_surface(store, LEAD), entry)
    assert (await hive_pinned(_surface(store, OUTSIDER), "fleet-a"))["pins"] == []
    assert _pinned_ids(await hive_pinned(_surface(store, OTHER_LEAD), "fleet-a")) == [entry]
    assert (await hive_pinned(_surface(store, OUTSIDER)))["pins"] == []  # own fleet: none
    assert (await hive_pinned(_surface(store, ADMIN)))["pins"] == []  # no home fleet


async def test_pins_are_newest_first() -> None:
    store = MemoryStore(make_clock())
    lead = _surface(store, LEAD)
    first = await _write(store, LEAD, "first")
    second = await _write(store, LEAD, "second")
    await hive_pin(lead, first)
    await hive_pin(lead, second)
    assert _pinned_ids(await hive_pinned(lead)) == [second, first]


async def test_rest_pin_unpin_and_list() -> None:
    store = MemoryStore(make_clock())
    entry = await _write(store, LEAD, "rest-pinned decision")
    app = create_app_for_config(
        Settings(),
        store=store,
        embedder=make_embedder(),
        authenticator=FakeAuthenticator(agent_credentials={"lead": LEAD, "dev": DEV}),
        search_config=make_search_config(),
    )
    transport = httpx.ASGITransport(app=create_app(app))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        denied = await client.put(f"/v1/entries/{entry}/pin", headers={"X-API-Key": "dev"})
        pinned = await client.put(f"/v1/entries/{entry}/pin", headers={"X-API-Key": "lead"})
        listed = await client.get("/v1/pins", headers={"X-API-Key": "dev"})
        removed = await client.delete(f"/v1/entries/{entry}/pin", headers={"X-API-Key": "lead"})
        after = await client.get("/v1/pins", headers={"X-API-Key": "dev"})
    assert denied.status_code == 403
    assert pinned.status_code == 200 and pinned.json()["fleet_id"] == "fleet-a"
    assert [p["id"] for p in listed.json()["pins"]] == [entry]
    assert removed.json() == {"entry_id": entry, "removed": True}
    assert after.json()["pins"] == []


# -- pin integrity (ADR 0076): audit rows and changed_since_pin ---------------


async def _audit(store: MemoryStore) -> list[AuditRecord]:
    return await store.list_audit(AuditFilters(), 50)


async def test_pin_and_unpin_write_audit_rows_without_a_key() -> None:
    store = MemoryStore(make_clock())
    entry = await _write(store, DEV, "deploys freeze on Fridays")
    lead = _surface(store, LEAD)
    await hive_pin(lead, entry)
    await hive_pin(lead, entry, unpin=True)
    await hive_pin(lead, entry, unpin=True)  # nothing to remove: not audited
    rows = list(reversed(await _audit(store)))
    assert [r.action for r in rows] == [AuditAction.ENTRY_PIN, AuditAction.ENTRY_UNPIN]
    for row in rows:
        assert (row.actor_kind, row.actor, row.target) == (ActorKind.AGENT_KEY, "lead", entry)
    assert rows[0].detail == {"fleet_id": "fleet-a"}
    assert rows[1].detail == {"fleet_id": "fleet-a", "pin_ids": [entry]}


async def test_an_admin_pin_is_audited_under_the_admin_key_fingerprint() -> None:
    store = MemoryStore(make_clock())
    entry = await _write(store, DEV, "an admin-pinned decision")
    admin = Credential(user_id="admin", is_admin=True, key_id="0123456789ab")
    await hive_pin(_surface(store, admin), entry)
    [row] = await _audit(store)
    assert (row.actor_kind, row.actor) == (ActorKind.ADMIN_KEY, "admin:0123456789ab")


async def test_a_refused_pin_or_unpin_writes_no_audit_row() -> None:
    store = MemoryStore(make_clock())
    entry = await _write(store, DEV, "a fleet decision")
    await hive_pin(_surface(store, DEV), entry)
    await hive_pin(_surface(store, DEV), entry, unpin=True)
    assert await _audit(store) == []


async def test_unpinning_by_the_shown_version_is_audited_once() -> None:
    store = MemoryStore(make_clock())
    lead = _surface(store, LEAD)
    old = await _write(store, LEAD, "use runner pool A")
    await hive_pin(lead, old)
    new = await _write(store, DEV, "use runner pool B", supersedes=[old])
    await hive_pin(lead, new, unpin=True)
    rows = [r for r in await _audit(store) if r.action is AuditAction.ENTRY_UNPIN]
    assert len(rows) == 1 and rows[0].target == new
    assert rows[0].detail == {"fleet_id": "fleet-a", "pin_ids": [old]}


async def test_a_fresh_pin_is_not_changed_since_pin() -> None:
    store = MemoryStore(make_clock())
    entry = await _write(store, LEAD, "use runner pool A")
    await hive_pin(_surface(store, LEAD), entry)
    pin = (await hive_pinned(_surface(store, DEV)))["pins"][0]  # type: ignore[index]
    assert pin["changed_since_pin"] is False and pin["change_reason"] is None


async def test_a_superseded_pin_is_changed_since_pin() -> None:
    store = MemoryStore(make_clock())
    lead = _surface(store, LEAD)
    old = await _write(store, LEAD, "use runner pool A")
    await hive_pin(lead, old)
    await _write(store, DEV, "use runner pool B", supersedes=[old])
    pin = (await hive_pinned(_surface(store, DEV)))["pins"][0]  # type: ignore[index]
    assert pin["changed_since_pin"] is True and pin["change_reason"] == "superseded"


async def test_a_stale_or_wrong_verdict_after_the_pin_flags_it() -> None:
    clock = make_clock()
    store = MemoryStore(clock)
    entry = await _write(store, LEAD, "use runner pool A")
    before = Feedback(entry, "dev", "dev", Verdict.STALE, updated_at=clock())
    await store.record_feedback(before)  # a verdict from before the pin
    clock.advance_days(1)
    await hive_pin(_surface(store, LEAD), entry)
    pin = (await hive_pinned(_surface(store, DEV)))["pins"][0]  # type: ignore[index]
    assert pin["changed_since_pin"] is False

    clock.advance_days(1)
    await store.record_feedback(Feedback(entry, "ops", "ops", Verdict.HELPFUL, updated_at=clock()))
    pin = (await hive_pinned(_surface(store, DEV)))["pins"][0]  # type: ignore[index]
    assert pin["changed_since_pin"] is False  # applause is not a change
    await store.record_feedback(Feedback(entry, "ops2", "ops2", Verdict.WRONG, updated_at=clock()))
    pin = (await hive_pinned(_surface(store, DEV)))["pins"][0]  # type: ignore[index]
    assert pin["changed_since_pin"] is True and pin["change_reason"] == "flagged"


async def test_superseded_wins_over_flagged_and_the_rest_listing_carries_both() -> None:
    clock = make_clock()
    store = MemoryStore(clock)
    old = await _write(store, LEAD, "use runner pool A")
    await hive_pin(_surface(store, LEAD), old)
    steady = await _write(store, LEAD, "freeze on Fridays")
    await hive_pin(_surface(store, LEAD), steady)
    clock.advance_days(1)
    await store.record_feedback(Feedback(old, "ops", "ops", Verdict.STALE, updated_at=clock()))
    await _write(store, DEV, "use runner pool B", supersedes=[old])
    app = create_app_for_config(
        Settings(),
        store=store,
        embedder=make_embedder(),
        authenticator=FakeAuthenticator(agent_credentials={"dev": DEV}),
        search_config=make_search_config(),
    )
    transport = httpx.ASGITransport(app=create_app(app))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        listed = await client.get("/v1/pins", headers={"X-API-Key": "dev"})
    by_pinned = {p["pinned_id"]: p for p in listed.json()["pins"]}
    assert by_pinned[old]["changed_since_pin"] is True
    assert by_pinned[old]["change_reason"] == "superseded"
    assert by_pinned[steady]["changed_since_pin"] is False
    assert by_pinned[steady]["change_reason"] is None


async def test_an_agent_key_with_a_fingerprint_is_audited_by_name() -> None:
    """Production agent credentials carry a ``key_id``; the actor must still
    be the agent name, never ``admin:<fingerprint>`` (ADR 0076)."""
    keyed = replace(LEAD, key_id="0123456789ab")
    assert keyed.audit_actor() == "lead"
    store = MemoryStore(make_clock())
    entry = await _write(store, DEV, "a keyed pin")
    await hive_pin(_surface(store, keyed), entry)
    [row] = await _audit(store)
    assert (row.actor_kind, row.actor) == (ActorKind.AGENT_KEY, "lead")


async def test_repinning_acknowledges_a_change_and_moves_to_the_front() -> None:
    clock = make_clock()
    store = MemoryStore(clock)
    lead = _surface(store, LEAD)
    old = await _write(store, LEAD, "use runner pool A")
    other = await _write(store, LEAD, "freeze on Fridays")
    await hive_pin(lead, old)
    clock.advance_days(1)
    await hive_pin(lead, other)
    new = await _write(store, DEV, "use runner pool B", supersedes=[old])
    clock.advance_days(1)
    pins = (await hive_pinned(lead))["pins"]  # type: ignore[index]
    assert [(p["id"], p["changed_since_pin"]) for p in pins] == [(other, False), (new, True)]

    repinned = await hive_pin(lead, new)  # moves the old pin to the version it shows
    assert repinned["entry_id"] == new and repinned["pinned_by"] == "lead"
    pins = (await hive_pinned(lead))["pins"]  # type: ignore[index]
    assert [(p["id"], p["pinned_id"], p["changed_since_pin"]) for p in pins] == [
        (new, new, False),
        (other, other, False),
    ]
    assert pins[0]["change_reason"] is None
    pin_rows = [r for r in await _audit(store) if r.action is AuditAction.ENTRY_PIN]
    assert pin_rows[0].detail == {"fleet_id": "fleet-a", "from": old}  # newest first


async def test_repinning_a_flagged_entry_clears_the_flag() -> None:
    clock = make_clock()
    store = MemoryStore(clock)
    lead = _surface(store, LEAD)
    entry = await _write(store, LEAD, "use runner pool A")
    await hive_pin(lead, entry)
    clock.advance_days(1)
    await store.record_feedback(Feedback(entry, "ops", "ops", Verdict.STALE, updated_at=clock()))
    assert (await hive_pinned(lead))["pins"][0]["change_reason"] == "flagged"  # type: ignore[index]
    clock.advance_days(1)
    await hive_pin(lead, entry)
    assert (await hive_pinned(lead))["pins"][0]["changed_since_pin"] is False  # type: ignore[index]


async def test_a_note_on_a_helpful_verdict_is_not_a_flag() -> None:
    clock = make_clock()
    store = MemoryStore(clock)
    entry = await _write(store, LEAD, "use runner pool A")
    await hive_pin(_surface(store, LEAD), entry)
    clock.advance_days(1)
    fb = Feedback(entry, "ops", "ops", Verdict.HELPFUL, note="worked", updated_at=clock())
    await store.record_feedback(fb)
    assert (await hive_pinned(_surface(store, DEV)))["pins"][0]["changed_since_pin"] is False  # type: ignore[index]


async def test_a_revoked_reporters_verdict_does_not_flag() -> None:
    clock = make_clock()
    store = MemoryStore(clock)
    await store.register_agent("ghost", None)
    entry = await _write(store, LEAD, "use runner pool A")
    await hive_pin(_surface(store, LEAD), entry)
    clock.advance_days(1)
    await store.record_feedback(
        Feedback(entry, "ghost", "ghost", Verdict.STALE, updated_at=clock())
    )
    await store.revoke_agent("ghost")
    assert (await hive_pinned(_surface(store, DEV)))["pins"][0]["changed_since_pin"] is False  # type: ignore[index]
