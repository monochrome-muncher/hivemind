"""Pinned entries (ADR 0058): a privileged agent of a fleet (or an admin)
pins a few fleet entries; every agent of the fleet reads them first."""

from __future__ import annotations

import httpx

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
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
    # Pinning again is a no-op.
    again = await hive_pin(_surface(store, LEAD), entry)
    assert again["pinned_at"] == pinned["pinned_at"]


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
    assert again["entry_id"] == old  # the existing pin, unchanged
    assert _pinned_ids(await hive_pinned(lead)) == [new]

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
