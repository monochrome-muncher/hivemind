"""Fleet phases (ADR 0143): the domain rules, the admin service and routes, and the
``fleet_phase`` hint in ``hive_whoami`` and ``hive_pinned``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import FleetMerged, TrustLevel, UnknownFleet
from hivemind.domain.audit import AuditAction, AuditFilters
from hivemind.domain.fleet_phase import MAX_PHASE_DAYS, FleetPhase, PhaseName
from hivemind.domain.validation import InvalidInput
from hivemind.mcp.app import McpHivemind, hive_pin, hive_pinned, hive_whoami, hive_write
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService, PermissionDenied
from tests.fakes import (
    FakeAuthenticator,
    make_clock,
    make_embedder,
    make_search_config,
    mcp_surface,
)

NOW = datetime(2026, 11, 12, 9, 30, tzinfo=UTC)
ADMIN = Credential(user_id="admin", is_admin=True, key_id="abcdef012345")
ORG = Credential(user_id="org", is_org=True, access_controlled=True)

EXPLORE_HINT = (
    "Your fleet is exploring until 2026-11-20. On open-ended work, try explore: true, "
    "write approaches someone could test as their own entries tagged alternative, saying "
    "how to test them, write dead ends "
    "and open questions, and when the stakes are low try the less-tested of two rivals "
    "and report what you found."
)
CONSOLIDATE_HINT = (
    "Your fleet is consolidating until 2026-11-20. Before new work, settle what is open: "
    "test alternative and Conflict: entries and supersede or flag them, distill clusters "
    "of three or more entries, and fix flagged entries."
)


def _in(days: float) -> datetime:
    return NOW + timedelta(days=days)


# -- the domain rules -----------------------------------------------------------


def test_a_phase_starts_for_a_future_moment_within_the_limit() -> None:
    phase = FleetPhase.starting(PhaseName.EXPLORE, _in(8), NOW)
    assert (phase.name, phase.until) == (PhaseName.EXPLORE, _in(8))
    assert FleetPhase.starting(PhaseName.CONSOLIDATE, _in(MAX_PHASE_DAYS), NOW).until == _in(30)


@pytest.mark.parametrize("until", [NOW, NOW - timedelta(seconds=1), _in(-3)])
def test_an_until_that_is_not_in_the_future_is_refused(until: datetime) -> None:
    with pytest.raises(InvalidInput, match="in the future"):
        FleetPhase.starting(PhaseName.EXPLORE, until, NOW)


def test_an_until_more_than_thirty_days_ahead_is_refused() -> None:
    with pytest.raises(InvalidInput, match="at most 30 days"):
        FleetPhase.starting(PhaseName.EXPLORE, _in(30) + timedelta(minutes=1, seconds=1), NOW)


def test_a_minute_of_clock_skew_is_allowed_at_the_limit() -> None:
    ahead = _in(30) + timedelta(minutes=1)
    assert FleetPhase.starting(PhaseName.EXPLORE, ahead, NOW).until == ahead


def test_a_naive_until_means_utc_and_an_offset_is_converted() -> None:
    naive = FleetPhase.starting(PhaseName.EXPLORE, datetime(2026, 11, 20, 12), NOW)
    assert naive.until == datetime(2026, 11, 20, 12, tzinfo=UTC)
    plus_two = datetime(2026, 11, 20, 14, tzinfo=timezone(timedelta(hours=2)))
    assert FleetPhase.starting(PhaseName.EXPLORE, plus_two, NOW).until == datetime(
        2026, 11, 20, 12, tzinfo=UTC
    )


def test_a_phase_is_in_force_until_its_end() -> None:
    phase = FleetPhase(PhaseName.EXPLORE, _in(1))
    assert phase.in_force(NOW)
    assert phase.in_force(_in(1) - timedelta(seconds=1))
    assert not phase.in_force(_in(1))
    assert not phase.in_force(_in(2))


def test_the_hints_are_fixed_text_with_the_end_date() -> None:
    until = datetime(2026, 11, 20, 23, 59, tzinfo=UTC)
    assert FleetPhase(PhaseName.EXPLORE, until).hint == EXPLORE_HINT
    assert FleetPhase(PhaseName.CONSOLIDATE, until).hint == CONSOLIDATE_HINT
    assert FleetPhase(PhaseName.EXPLORE, until).as_dict() == {
        "phase": "explore",
        "until": "2026-11-20T23:59:00+00:00",
        "hint": EXPLORE_HINT,
    }


# -- the admin service ----------------------------------------------------------


class World:
    def __init__(self) -> None:
        self.store = MemoryStore(make_clock())
        self.access = AccessService(self.store, FakeAuthenticator(), clock=lambda: NOW)

    async def fleet(self, name: str = "platform") -> str:
        return (await self.access.create_fleet(name, ADMIN)).id

    async def audit(self) -> list[tuple[str, dict[str, object]]]:
        rows = await self.store.list_audit(AuditFilters(), 50)
        return [(r.action.value, dict(r.detail)) for r in reversed(rows) if "phase" in r.action]


async def test_the_admin_sets_a_phase_and_it_is_audited() -> None:
    world = World()
    fleet_id = await world.fleet()
    fleet = await world.access.set_fleet_phase(fleet_id, PhaseName.EXPLORE, _in(8), ADMIN)
    assert fleet.phase == FleetPhase(PhaseName.EXPLORE, _in(8))
    assert await world.audit() == [
        ("fleet.phase_set", {"phase": "explore", "until": _in(8).isoformat()})
    ]
    rows = await world.store.list_audit(AuditFilters(action=AuditAction.FLEET_PHASE_SET), 5)
    assert [r.target for r in rows] == [fleet_id]


async def test_setting_replaces_the_current_phase() -> None:
    world = World()
    fleet_id = await world.fleet()
    await world.access.set_fleet_phase(fleet_id, PhaseName.EXPLORE, _in(8), ADMIN)
    fleet = await world.access.set_fleet_phase(fleet_id, PhaseName.CONSOLIDATE, _in(3), ADMIN)
    assert fleet.phase == FleetPhase(PhaseName.CONSOLIDATE, _in(3))
    assert [action for action, _ in await world.audit()] == ["fleet.phase_set", "fleet.phase_set"]


async def test_clearing_ends_the_phase_and_audits_what_it_ended() -> None:
    world = World()
    fleet_id = await world.fleet()
    await world.access.set_fleet_phase(fleet_id, PhaseName.EXPLORE, _in(8), ADMIN)
    cleared = await world.access.clear_fleet_phase(fleet_id, ADMIN)
    assert cleared.phase is None
    assert (await world.audit())[-1] == (
        "fleet.phase_clear",
        {"phase": "explore", "until": _in(8).isoformat()},
    )
    audited = await world.audit()
    again = await world.access.clear_fleet_phase(fleet_id, ADMIN)
    assert again.phase is None
    assert await world.audit() == audited


async def test_clearing_an_ended_phase_writes_no_row() -> None:
    world = World()
    fleet_id = await world.fleet()
    await world.store.set_fleet_phase(fleet_id, FleetPhase(PhaseName.EXPLORE, NOW))
    cleared = await world.access.clear_fleet_phase(fleet_id, ADMIN)
    assert cleared.phase is None
    assert await world.audit() == []


async def test_a_refused_phase_changes_and_audits_nothing() -> None:
    world = World()
    fleet_id = await world.fleet()
    with pytest.raises(InvalidInput):
        await world.access.set_fleet_phase(fleet_id, PhaseName.EXPLORE, _in(-1), ADMIN)
    with pytest.raises(InvalidInput):
        await world.access.set_fleet_phase(fleet_id, PhaseName.EXPLORE, _in(31), ADMIN)
    with pytest.raises(UnknownFleet):
        await world.access.set_fleet_phase("no-such-fleet", PhaseName.EXPLORE, _in(2), ADMIN)
    assert (await world.store.get_fleet(fleet_id)).phase is None  # type: ignore[union-attr]
    assert await world.audit() == []


@pytest.mark.parametrize(
    "who", [ORG, Credential(user_id="a", agent_name="a", access_controlled=True)]
)
async def test_only_the_admin_key_may_set_or_clear(who: Credential) -> None:
    world = World()
    fleet_id = await world.fleet()
    with pytest.raises(PermissionDenied):
        await world.access.set_fleet_phase(fleet_id, PhaseName.EXPLORE, _in(2), who)
    with pytest.raises(PermissionDenied):
        await world.access.clear_fleet_phase(fleet_id, who)
    assert await world.audit() == []


async def test_a_merged_fleet_refuses_a_phase() -> None:
    world = World()
    source, target = await world.fleet("old"), await world.fleet("new")
    await world.access.merges.merge(source, ADMIN, target_fleet_id=target)
    with pytest.raises(FleetMerged):
        await world.access.set_fleet_phase(source, PhaseName.EXPLORE, _in(2), ADMIN)
    with pytest.raises(FleetMerged):
        await world.access.clear_fleet_phase(source, ADMIN)
    assert await world.audit() == []


# -- hive_whoami and hive_pinned ------------------------------------------------


def _agent(fleet_id: str | None, level: TrustLevel = TrustLevel.CONTRIBUTOR) -> Credential:
    return Credential(
        user_id="alice",
        agent_id="alice",
        agent_name="alice",
        access_controlled=True,
        trust_level=level,
        home_fleet_id=fleet_id,
    )


def _surface(world: World, credential: Credential) -> McpHivemind:
    return mcp_surface(world.store, credential, access=world.access)


async def _agent_in(world: World, fleet_id: str) -> Credential:
    await world.store.register_agent("alice", "owner")
    await world.store.activate_agent(
        "alice", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=fleet_id
    )
    return _agent(fleet_id)


async def test_whoami_carries_the_phase_in_force_and_no_key_without_one() -> None:
    world = World()
    fleet_id = await world.fleet()
    alice = await _agent_in(world, fleet_id)
    assert "fleet_phase" not in await hive_whoami(_surface(world, alice))

    until = datetime(2026, 11, 20, 8, tzinfo=UTC)
    await world.access.set_fleet_phase(fleet_id, PhaseName.EXPLORE, until, ADMIN)
    assert (await hive_whoami(_surface(world, alice)))["fleet_phase"] == {
        "phase": "explore",
        "until": until.isoformat(),
        "hint": EXPLORE_HINT,
    }
    await world.access.set_fleet_phase(fleet_id, PhaseName.CONSOLIDATE, until, ADMIN)
    phase = (await hive_whoami(_surface(world, alice)))["fleet_phase"]
    assert phase["hint"] == CONSOLIDATE_HINT  # type: ignore[index]

    await world.access.clear_fleet_phase(fleet_id, ADMIN)
    assert "fleet_phase" not in await hive_whoami(_surface(world, alice))


async def test_an_ended_phase_is_absent_from_whoami_and_pinned() -> None:
    world = World()
    fleet_id = await world.fleet()
    alice = await _agent_in(world, fleet_id)
    await world.store.set_fleet_phase(fleet_id, FleetPhase(PhaseName.EXPLORE, NOW))
    assert "fleet_phase" not in await hive_whoami(_surface(world, alice))
    assert "fleet_phase" not in await hive_pinned(_surface(world, alice))
    fleet = await world.store.get_fleet(fleet_id)
    assert fleet is not None and fleet.phase_in_force(NOW) is None


async def test_keys_without_a_home_fleet_see_no_phase() -> None:
    world = World()
    fleet_id = await world.fleet()
    await world.access.set_fleet_phase(fleet_id, PhaseName.EXPLORE, _in(2), ADMIN)
    assert "fleet_phase" not in await hive_whoami(_surface(world, ADMIN))
    assert "fleet_phase" not in await hive_whoami(_surface(world, _agent(None)))


async def test_pinned_carries_the_phase_for_the_callers_own_fleet() -> None:
    world = World()
    fleet_id = await world.fleet()
    other_id = await world.fleet("other")
    alice = await _agent_in(world, fleet_id)
    surface = _surface(world, alice)
    entry = await hive_write(surface, kind="decision", summary="freeze on Fridays")
    await hive_pin(surface, str(entry["id"]))
    assert "fleet_phase" not in await hive_pinned(surface)

    until = datetime(2026, 11, 20, 8, tzinfo=UTC)
    await world.access.set_fleet_phase(fleet_id, PhaseName.CONSOLIDATE, until, ADMIN)
    await world.access.set_fleet_phase(other_id, PhaseName.EXPLORE, until, ADMIN)
    briefing = await hive_pinned(surface)
    assert briefing["fleet_phase"] == {
        "phase": "consolidate",
        "until": until.isoformat(),
        "hint": CONSOLIDATE_HINT,
    }
    assert [p["id"] for p in briefing["pins"]] == [entry["id"]]  # type: ignore[attr-defined]
    named = await hive_pinned(surface, fleet_id=fleet_id)
    assert named["fleet_phase"] == briefing["fleet_phase"]
    # Another fleet's phase is not the caller's to read.
    assert "fleet_phase" not in await hive_pinned(surface, fleet_id=other_id)


# -- the REST routes ------------------------------------------------------------

ADMIN_H = {"X-API-Key": "hm_admin"}


def _client(store: MemoryStore, agents: dict[str, Credential] | None = None) -> httpx.AsyncClient:
    app = create_app_for_config(
        Settings(),
        store=store,
        embedder=make_embedder(),
        authenticator=FakeAuthenticator(agent_credentials=agents),
        search_config=make_search_config(),
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url="http://t"
    )


def _soon(days: int) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


async def test_rest_sets_and_clears_a_phase_and_the_fleet_lists_it() -> None:
    store = MemoryStore(make_clock())
    fleet = await store.create_fleet("platform")
    until = _soon(5)
    async with _client(store) as client:
        put = await client.put(
            f"/v1/admin/fleets/{fleet.id}/phase",
            json={"phase": "consolidate", "until": until},
            headers=ADMIN_H,
        )
        listed = await client.get("/v1/admin/fleets", headers=ADMIN_H)
        cleared = await client.delete(f"/v1/admin/fleets/{fleet.id}/phase", headers=ADMIN_H)
        after = await client.get("/v1/admin/fleets", headers=ADMIN_H)
    assert put.status_code == 200
    body = put.json()
    assert (body["id"], body["phase"]) == (fleet.id, "consolidate")
    assert datetime.fromisoformat(body["phase_until"]) == datetime.fromisoformat(until)
    assert listed.json()[0]["phase"] == "consolidate"
    assert cleared.status_code == 200
    assert (cleared.json()["phase"], cleared.json()["phase_until"]) == (None, None)
    assert (after.json()[0]["phase"], after.json()[0]["phase_until"]) == (None, None)


@pytest.mark.parametrize(
    "body",
    [
        {"phase": "explore", "until": _soon(-1)},
        {"phase": "explore", "until": _soon(31)},
        {"phase": "sprint", "until": _soon(3)},
        {"phase": "explore", "until": "tomorrow"},
        {"phase": "explore"},
    ],
)
async def test_rest_refuses_a_bad_phase_with_422(body: dict[str, str]) -> None:
    store = MemoryStore(make_clock())
    fleet = await store.create_fleet("platform")
    async with _client(store) as client:
        resp = await client.put(f"/v1/admin/fleets/{fleet.id}/phase", json=body, headers=ADMIN_H)
    assert resp.status_code == 422
    stored = await store.get_fleet(fleet.id)
    assert stored is not None and stored.phase is None


async def test_rest_errors_for_unknown_merged_and_unauthorised() -> None:
    store = MemoryStore(make_clock())
    old, new = await store.create_fleet("old"), await store.create_fleet("new")
    await store.merge_fleets(old.id, target_fleet_id=new.id, merged_by="admin:1")
    dev = Credential(user_id="dev", agent_name="dev", access_controlled=True)
    body = {"phase": "explore", "until": _soon(3)}
    async with _client(store, {"dev-key": dev}) as client:
        unknown = await client.put("/v1/admin/fleets/nope/phase", json=body, headers=ADMIN_H)
        merged = await client.put(f"/v1/admin/fleets/{old.id}/phase", json=body, headers=ADMIN_H)
        merged_clear = await client.delete(f"/v1/admin/fleets/{old.id}/phase", headers=ADMIN_H)
        agent = await client.put(
            f"/v1/admin/fleets/{new.id}/phase", json=body, headers={"X-API-Key": "dev-key"}
        )
        agent_clear = await client.delete(
            f"/v1/admin/fleets/{new.id}/phase", headers={"X-API-Key": "dev-key"}
        )
        anonymous = await client.put(f"/v1/admin/fleets/{new.id}/phase", json=body)
    assert unknown.status_code == 404
    assert (merged.status_code, merged.json()["error"]["code"]) == (409, "fleet_merged")
    assert merged_clear.status_code == 409
    assert (agent.status_code, agent_clear.status_code, anonymous.status_code) == (403, 403, 401)


async def test_rest_whoami_and_pins_carry_the_phase_only_while_in_force() -> None:
    store = MemoryStore(make_clock())
    fleet = await store.create_fleet("platform")
    alice = _agent(fleet.id)
    until = datetime.now(UTC) + timedelta(days=4)
    async with _client(store, {"alice-key": alice}) as client:
        alice_h = {"X-API-Key": "alice-key"}
        bare_whoami = await client.get("/v1/whoami", headers=alice_h)
        bare_pins = await client.get("/v1/pins", headers=alice_h)
        await store.set_fleet_phase(fleet.id, FleetPhase(PhaseName.EXPLORE, until))
        whoami = await client.get("/v1/whoami", headers=alice_h)
        pins = await client.get("/v1/pins", headers=alice_h)
        await store.set_fleet_phase(
            fleet.id, FleetPhase(PhaseName.EXPLORE, datetime.now(UTC) - timedelta(seconds=1))
        )
        ended_whoami = await client.get("/v1/whoami", headers=alice_h)
        ended_pins = await client.get("/v1/pins", headers=alice_h)
        ended_fleets = await client.get("/v1/admin/fleets", headers=ADMIN_H)
    assert set(bare_whoami.json()) == {
        "key_kind",
        "name",
        "status",
        "trust_level",
        "trust_level_name",
        "home_fleet_id",
        "home_fleet_name",
        "can_read",
        "can_write_scopes",
        "inherits_from",
        "key_last_used_at",
    }
    assert set(bare_pins.json()) == {"fleet_id", "pins"}
    expected = FleetPhase(PhaseName.EXPLORE, until).as_dict()
    assert whoami.json()["fleet_phase"]["hint"] == expected["hint"]
    assert whoami.json()["fleet_phase"]["phase"] == "explore"
    assert pins.json()["fleet_phase"] == whoami.json()["fleet_phase"]
    assert pins.json()["pins"] == []
    assert "fleet_phase" not in ended_whoami.json()
    assert "fleet_phase" not in ended_pins.json()
    assert ended_fleets.json()[0]["phase"] is None


async def test_the_rest_and_mcp_replies_agree() -> None:
    store = MemoryStore(make_clock())
    world = World()
    world.store = store
    world.access = AccessService(store, FakeAuthenticator())
    fleet = await store.create_fleet("platform")
    alice = await _agent_in(world, fleet.id)
    until = datetime.now(UTC) + timedelta(days=4)
    await store.set_fleet_phase(fleet.id, FleetPhase(PhaseName.CONSOLIDATE, until))
    async with _client(store, {"alice-key": alice}) as client:
        rest = (await client.get("/v1/whoami", headers={"X-API-Key": "alice-key"})).json()
        rest_pins = (await client.get("/v1/pins", headers={"X-API-Key": "alice-key"})).json()
    mcp = await hive_whoami(_surface(world, alice))
    mcp_pins = await hive_pinned(_surface(world, alice))
    assert rest["fleet_phase"] == mcp["fleet_phase"] == mcp_pins["fleet_phase"]
    assert rest_pins["fleet_phase"] == mcp["fleet_phase"]


# -- usage report and Prometheus ------------------------------------------------


async def test_the_usage_report_and_gauge_list_phases_in_force() -> None:
    store = MemoryStore(make_clock())
    exploring = await store.create_fleet("exploring")
    ended = await store.create_fleet("ended")
    await store.create_fleet("plain")
    until = datetime.now(UTC) + timedelta(days=2)
    await store.set_fleet_phase(exploring.id, FleetPhase(PhaseName.EXPLORE, until))
    await store.set_fleet_phase(
        ended.id, FleetPhase(PhaseName.CONSOLIDATE, datetime.now(UTC) - timedelta(minutes=1))
    )
    async with _client(store) as client:
        report = (await client.get("/v1/metrics", headers=ADMIN_H)).json()
        scrape = (await client.get("/metrics")).text
    phases = report["fleets"]["phase_by_fleet"]
    assert list(phases) == ["exploring"]
    assert phases["exploring"]["phase"] == "explore"
    assert datetime.fromisoformat(phases["exploring"]["until"]) == until
    gauge = [
        (s.labels, s.value)
        for family in text_string_to_metric_families(scrape)
        for s in family.samples
        if s.name == "hivemind_fleet_phase"
    ]
    assert gauge == [({"fleet": "exploring", "phase": "explore"}, 1.0)]


async def test_rest_reads_a_non_utc_until_as_the_utc_date_in_the_hint() -> None:
    store = MemoryStore(make_clock())
    fleet = await store.create_fleet("platform")
    end = (datetime.now(UTC) + timedelta(days=3)).replace(hour=1, minute=0, second=0, microsecond=0)
    local = end.astimezone(timezone(timedelta(hours=-5)))
    assert local.date() != end.date()
    async with _client(store, {"alice-key": _agent(fleet.id)}) as client:
        put = await client.put(
            f"/v1/admin/fleets/{fleet.id}/phase",
            json={"phase": "explore", "until": local.isoformat()},
            headers=ADMIN_H,
        )
        whoami = await client.get("/v1/whoami", headers={"X-API-Key": "alice-key"})
    assert put.status_code == 200
    phase = whoami.json()["fleet_phase"]
    assert datetime.fromisoformat(phase["until"]) == end
    assert f"until {end.date().isoformat()}." in phase["hint"]
