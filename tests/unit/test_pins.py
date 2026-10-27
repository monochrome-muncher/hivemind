"""Pinned entries (ADRs 0058, 0096): a contributor or privileged agent of a
fleet (or an admin) pins a few fleet entries; every agent of the fleet reads
them first."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import httpx

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.domain.audit import ActorKind, AuditAction, AuditFilters, AuditRecord
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.domain.validation import MAX_PINS_PER_AGENT, MAX_PINS_PER_FLEET
from hivemind.mcp.app import (
    McpHivemind,
    hive_feedback,
    hive_pin,
    hive_pinned,
    hive_withdraw,
    hive_write,
)
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.services.governance import GovernanceService
from hivemind.services.pins import PinService
from hivemind.services.read import ReadService
from hivemind.services.search import SearchService
from hivemind.services.write import WriteService
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
LURKER = _agent("lurker", TrustLevel.LURKER)
DEV2 = _agent("dev2", TrustLevel.CONTRIBUTOR)
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
        read_service=ReadService(store, GovernanceService(store), config),
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


async def test_contributors_and_privileged_members_and_admins_may_pin() -> None:
    store = MemoryStore(make_clock())
    entry = await _write(store, DEV, "a fleet decision")
    mine = await hive_pin(_surface(store, DEV), entry)
    assert mine["pinned"] is True and mine["pinned_by"] == "dev"
    assert (await hive_pin(_surface(store, ADMIN), entry))["pinned"] is True


async def test_lurkers_and_other_fleets_may_not_pin_or_unpin() -> None:
    store = MemoryStore(make_clock())
    entry = await _write(store, DEV, "a fleet decision")
    await hive_pin(_surface(store, DEV), entry)
    # A contributor of another fleet cannot even read the entry.
    for cred, code in (
        (LURKER, "permission_denied"),
        (OUTSIDER, "not_found"),
        (OTHER_LEAD, "permission_denied"),  # privileged: reads every fleet
    ):
        for unpin in (False, True):
            denied = await hive_pin(_surface(store, cred), entry, unpin=unpin)
            assert denied["error"]["code"] == code, (cred.user_id, unpin)  # type: ignore[index]
    assert _pinned_ids(await hive_pinned(_surface(store, DEV))) == [entry]


async def test_a_member_unpins_someone_elses_pin() -> None:
    store = MemoryStore(make_clock())
    entry = await _write(store, LEAD, "outdated rule")
    await hive_pin(_surface(store, LEAD), entry)
    removed = await hive_pin(_surface(store, DEV), entry, unpin=True)
    assert removed["removed"] is True
    assert (await hive_pinned(_surface(store, LEAD)))["pins"] == []
    again = await _write(store, DEV, "dev's own")
    await hive_pin(_surface(store, DEV), again)
    assert (await hive_pin(_surface(store, LEAD), again, unpin=True))["removed"] is True


async def test_an_agent_holds_a_bounded_number_of_pins() -> None:
    store = MemoryStore(make_clock())
    dev = _surface(store, DEV)
    ids = [await _write(store, DEV, f"decision {i}") for i in range(MAX_PINS_PER_AGENT + 2)]
    for entry in ids[:MAX_PINS_PER_AGENT]:
        assert (await hive_pin(dev, entry))["pinned"] is True
    over = await hive_pin(dev, ids[MAX_PINS_PER_AGENT])
    assert over["error"]["code"] == "invalid_input"  # type: ignore[index]
    assert f"you already hold {MAX_PINS_PER_AGENT} of this fleet's pins" in over["error"]["message"]  # type: ignore[index]
    # Re-pinning one of one's own pins is a refresh, within the cap.
    assert (await hive_pin(dev, ids[0]))["pinned"] is True
    # Another agent has its own count, and taking over a pin counts for the taker.
    other = _surface(store, DEV2)
    assert (await hive_pin(other, ids[MAX_PINS_PER_AGENT]))["pinned"] is True
    assert (await hive_pin(other, ids[0]))["pinned"] is True  # dev now holds two
    assert (await hive_pin(dev, ids[MAX_PINS_PER_AGENT + 1]))["pinned"] is True
    # dev holds three again; refreshing dev2's pin would make it four.
    assert (await hive_pin(dev, ids[MAX_PINS_PER_AGENT]))["error"]["code"] == "invalid_input"  # type: ignore[index]
    # Unpinning frees a slot, whoever made the pin.
    assert (await hive_pin(other, ids[1], unpin=True))["removed"] is True
    assert (await hive_pin(dev, ids[MAX_PINS_PER_AGENT]))["pinned"] is True


async def test_the_admin_is_exempt_from_the_agent_cap_but_not_the_fleet_cap() -> None:
    store = MemoryStore(make_clock())
    admin = _surface(store, ADMIN)
    ids = [await _write(store, DEV, f"decision {i}") for i in range(MAX_PINS_PER_FLEET + 1)]
    for entry in ids[:MAX_PINS_PER_FLEET]:
        assert (await hive_pin(admin, entry))["pinned"] is True
    full = await hive_pin(admin, ids[-1])
    assert "the fleet already has" in full["error"]["message"]  # type: ignore[index]
    # A full fleet refuses a member with the fleet message, not the agent one.
    member = await hive_pin(_surface(store, DEV), ids[-1])
    assert "the fleet already has" in member["error"]["message"]  # type: ignore[index]
    assert member["error"]["code"] == "invalid_input"  # type: ignore[index]
    # Unpinning frees a slot for a member.
    assert (await hive_pin(admin, ids[0], unpin=True))["removed"] is True
    assert (await hive_pin(_surface(store, DEV), ids[-1]))["pinned"] is True


async def test_a_move_onto_a_new_version_counts_once_for_the_agent() -> None:
    store = MemoryStore(make_clock())
    dev = _surface(store, DEV)
    first = [await _write(store, DEV, f"rule {i}") for i in range(MAX_PINS_PER_AGENT)]
    for entry in first:
        await hive_pin(dev, entry)
    new = await _write(store, LEAD, "rule 0, revised", supersedes=[first[0]])
    moved = await hive_pin(dev, new)  # replaces dev's own pin: still three
    assert moved["pinned"] is True
    assert len((await hive_pinned(dev))["pins"]) == MAX_PINS_PER_AGENT  # type: ignore[arg-type]
    # A lead's pin moved onto a new version is dev's to hold afterwards: dev is full.
    lead_pin = await _write(store, LEAD, "lead rule")
    await hive_pin(_surface(store, LEAD), lead_pin)
    newer = await _write(store, LEAD, "lead rule, revised", supersedes=[lead_pin])
    refused = await hive_pin(dev, newer)
    assert refused["error"]["code"] == "invalid_input"  # type: ignore[index]
    assert "you already hold" in refused["error"]["message"]  # type: ignore[index]


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


async def test_a_superseded_version_cannot_be_pinned_but_its_successor_can() -> None:
    store = MemoryStore(make_clock())
    old = await _write(store, LEAD, "rule v1")
    new = await _write(store, LEAD, "rule v2", supersedes=[old])
    refused = await hive_pin(_surface(store, LEAD), old)
    assert refused["error"]["code"] == "not_active"  # type: ignore[index]
    assert _pinned_ids(await hive_pinned(_surface(store, DEV))) == []
    assert (await hive_pin(_surface(store, LEAD), new))["pinned"] is True


async def test_an_org_entry_cannot_be_pinned_by_anyone() -> None:
    store = MemoryStore(make_clock())
    org = await _write(store, ADMIN, "org-wide note", scope="org", agent="admin-bot")
    for cred in (LEAD, ADMIN):
        refused = await hive_pin(_surface(store, cred), org)
        assert refused["error"]["code"] == "invalid_input", cred.user_id  # type: ignore[index]
        assert "only fleet entries" in refused["error"]["message"]  # type: ignore[index]
    assert _pinned_ids(await hive_pinned(_surface(store, DEV))) == []


async def test_unpin_reports_whether_it_was_pinned() -> None:
    store = MemoryStore(make_clock())
    entry = await _write(store, LEAD, "pinned for a while")
    lead = _surface(store, LEAD)
    await hive_pin(lead, entry)
    assert (await hive_pin(lead, entry, unpin=True))["removed"] is True
    assert (await hive_pin(lead, entry, unpin=True))["removed"] is False
    assert (await hive_pinned(_surface(store, DEV)))["pins"] == []


async def test_a_pin_follows_supersession() -> None:
    store = MemoryStore(make_clock())
    lead = _surface(store, LEAD)
    old = await _write(store, LEAD, "use runner pool A")
    await hive_pin(lead, old)
    new = await _write(store, DEV, "use runner pool B", supersedes=[old])
    briefing = await hive_pinned(_surface(store, DEV))
    pin = briefing["pins"][0]  # type: ignore[index]
    assert (pin["id"], pin["pinned_id"]) == (new, old)
    assert pin["summary"] == "use runner pool B"


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
        authenticator=FakeAuthenticator(
            agent_credentials={"lead": LEAD, "dev": DEV, "lurker": LURKER}
        ),
        search_config=make_search_config(),
    )
    transport = httpx.ASGITransport(app=create_app(app))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        denied = await client.put(f"/v1/entries/{entry}/pin", headers={"X-API-Key": "lurker"})
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
    await hive_pin(_surface(store, LURKER), entry)
    await hive_pin(_surface(store, LURKER), entry, unpin=True)
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


async def test_a_verdict_right_after_a_pin_flags_it_when_the_store_clock_runs_ahead() -> None:
    """ADR 0079: feedback is stamped by the store's clock, the one pins use,
    so a pod whose clock lags the database cannot date a verdict before the
    pin it follows. The store clock here runs ahead of any wall clock."""
    clock = make_clock(datetime(2100, 1, 1, tzinfo=UTC))
    store = MemoryStore(clock)
    entry = await _write(store, LEAD, "use runner pool A")
    await hive_pin(_surface(store, LEAD), entry)
    clock.advance_days(1)
    result = await hive_feedback(_surface(store, DEV), entry_id=entry, verdict="stale")
    assert "error" not in result, result
    assert (await hive_pinned(_surface(store, DEV)))["pins"][0]["change_reason"] == "flagged"  # type: ignore[index]


async def test_a_repin_after_a_verdict_clears_it_when_the_store_clock_lags() -> None:
    """ADR 0079, the other way round: a pod whose clock runs ahead of the
    database cannot date a verdict after the re-pin that acknowledged it."""
    clock = make_clock(datetime(2000, 1, 1, tzinfo=UTC))
    store = MemoryStore(clock)
    entry = await _write(store, LEAD, "use runner pool A")
    await hive_pin(_surface(store, LEAD), entry)
    clock.advance_days(1)
    result = await hive_feedback(_surface(store, DEV), entry_id=entry, verdict="wrong")
    assert "error" not in result, result
    clock.advance_days(1)
    await hive_pin(_surface(store, LEAD), entry)
    assert (await hive_pinned(_surface(store, DEV)))["pins"][0]["changed_since_pin"] is False  # type: ignore[index]


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


# -- withdrawn entries leave the briefing (ADR 0096) --------------------------


async def test_a_contributors_pin_is_audited_as_an_agent_key() -> None:
    store = MemoryStore(make_clock())
    entry = await _write(store, DEV, "a fleet decision")
    await hive_pin(_surface(store, DEV2), entry)
    await hive_pin(_surface(store, DEV2), entry, unpin=True)
    rows = list(reversed(await _audit(store)))
    assert [(r.action, r.actor_kind, r.actor) for r in rows] == [
        (AuditAction.ENTRY_PIN, ActorKind.AGENT_KEY, "dev2"),
        (AuditAction.ENTRY_UNPIN, ActorKind.AGENT_KEY, "dev2"),
    ]
    assert rows[1].detail == {"fleet_id": "fleet-a", "pin_ids": [entry]}  # no reason


async def test_withdrawing_a_pinned_entry_unpins_it_and_audits_the_withdrawer() -> None:
    store = MemoryStore(make_clock())
    entry = await _write(store, DEV, "temporary rule")
    keep = await _write(store, DEV, "lasting rule")
    await hive_pin(_surface(store, LEAD), entry)
    await hive_pin(_surface(store, LEAD), keep)
    assert (await hive_withdraw(_surface(store, DEV), entry))["state"] == "withdrawn"
    assert _pinned_ids(await hive_pinned(_surface(store, DEV))) == [keep]
    # The pin row is gone, not just hidden: it no longer counts toward the caps.
    assert [p.entry_id for p in await store.list_pins("fleet-a")] == [keep]
    unpins = [r for r in await _audit(store) if r.action is AuditAction.ENTRY_UNPIN]
    [row] = unpins
    assert (row.actor_kind, row.actor, row.target) == (ActorKind.AGENT_KEY, "dev", entry)
    assert row.detail == {"fleet_id": "fleet-a", "pin_ids": [entry], "reason": "withdrawn"}


async def test_withdrawing_an_unpinned_entry_writes_no_unpin_row() -> None:
    store = MemoryStore(make_clock())
    entry = await _write(store, DEV, "never pinned")
    await hive_withdraw(_surface(store, DEV), entry)
    assert [r for r in await _audit(store) if r.action is AuditAction.ENTRY_UNPIN] == []


async def test_withdrawing_the_version_a_pin_followed_removes_that_pin() -> None:
    store = MemoryStore(make_clock())
    old = await _write(store, LEAD, "use runner pool A")
    await hive_pin(_surface(store, LEAD), old)
    new = await _write(store, DEV, "use runner pool B", supersedes=[old])
    assert _pinned_ids(await hive_pinned(_surface(store, DEV))) == [new]
    await hive_withdraw(_surface(store, DEV), new)
    assert (await hive_pinned(_surface(store, DEV)))["pins"] == []
    assert await store.list_pins("fleet-a") == []
    [row] = [r for r in await _audit(store) if r.action is AuditAction.ENTRY_UNPIN]
    assert row.target == new
    assert row.detail == {"fleet_id": "fleet-a", "pin_ids": [old], "reason": "withdrawn"}


async def test_an_admin_withdrawal_unpins_under_the_admin_key() -> None:
    store = MemoryStore(make_clock())
    entry = await _write(store, DEV, "a rule an admin retracts")
    await hive_pin(_surface(store, LEAD), entry)
    admin = Credential(user_id="admin", is_admin=True, key_id="0123456789ab")
    assert (await hive_withdraw(_surface(store, admin), entry))["state"] == "withdrawn"
    rows = await _audit(store)
    unpin = next(r for r in rows if r.action is AuditAction.ENTRY_UNPIN)
    assert (unpin.actor_kind, unpin.actor) == (ActorKind.ADMIN_KEY, "admin:0123456789ab")
    assert unpin.detail["reason"] == "withdrawn"
    assert any(r.action is AuditAction.ENTRY_WITHDRAW for r in rows)


async def test_a_portal_withdrawal_unpins_too() -> None:
    store = MemoryStore(make_clock())
    entry = await _write(store, DEV, "withdrawn from the portal")
    await hive_pin(_surface(store, LEAD), entry)
    await GovernanceService(store).withdraw_own(DEV, entry, "obsolete")
    assert await store.list_pins("fleet-a") == []
    [row] = [r for r in await _audit(store) if r.action is AuditAction.ENTRY_UNPIN]
    assert row.detail["reason"] == "withdrawn"


async def test_the_briefing_skips_a_withdrawn_entry_left_from_before() -> None:
    """A pin row left from before withdrawing unpinned (or after a failed
    unpin) is not shown."""
    store = MemoryStore(make_clock())
    entry = await _write(store, DEV, "retracted rule")
    keep = await _write(store, DEV, "lasting rule")
    await store.pin_entry("fleet-a", entry, "lead", 10)
    await store.pin_entry("fleet-a", keep, "lead", 10)
    await store.withdraw_entry(entry, "wrong", by_user="dev")  # the store alone: pin stays
    assert [p.entry_id for p in await store.list_pins("fleet-a")] == [keep, entry]
    assert _pinned_ids(await hive_pinned(_surface(store, DEV))) == [keep]


async def test_a_withdrawer_who_moved_fleets_still_unpins_other_authors_versions() -> None:
    """ADR 0096: the walk that finds the pins to remove reads as anyone, so a
    withdrawer whose home fleet is no longer the entry's still finds the
    older pinned version another author wrote."""
    store = MemoryStore(make_clock())
    old = await _write(store, LEAD, "use runner pool A")
    await hive_pin(_surface(store, LEAD), old)
    new = await _write(store, DEV, "use runner pool B", supersedes=[old])
    entry = await store.get_entry(new)
    assert entry is not None
    moved = _agent("dev", TrustLevel.CONTRIBUTOR, "fleet-b")  # cannot read fleet-a any more
    await store.withdraw_entry(new, None, by_user="dev")
    removed = await PinService(store).unpin_withdrawn(moved, entry)
    assert removed == [old]
    assert await store.list_pins("fleet-a") == []


async def test_a_leftover_withdrawn_pin_is_swept_on_the_next_pin_and_frees_the_cap() -> None:
    store = MemoryStore(make_clock())
    dev = _surface(store, DEV)
    ids = [await _write(store, DEV, f"rule {i}") for i in range(MAX_PINS_PER_AGENT + 1)]
    for entry in ids[:MAX_PINS_PER_AGENT]:
        await hive_pin(dev, entry)
    # Withdrawn at the store alone: the pin row stays, as from before ADR 0096.
    await store.withdraw_entry(ids[0], "wrong", by_user="dev")
    assert len(await store.list_pins("fleet-a")) == MAX_PINS_PER_AGENT
    assert (await hive_pin(dev, ids[-1]))["pinned"] is True  # not "you already hold 3"
    assert {p.entry_id for p in await store.list_pins("fleet-a")} == {*ids[1:]}
    [row] = [r for r in await _audit(store) if r.action is AuditAction.ENTRY_UNPIN]
    assert (row.actor, row.target) == ("dev", ids[0])
    assert row.detail == {"fleet_id": "fleet-a", "pin_ids": [ids[0]], "reason": "withdrawn"}
