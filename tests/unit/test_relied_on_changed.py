"""The ``relied_on_changed`` list filter (ADR 0098) through both surfaces on
``MemoryStore``: what an agent rated helpful and that has since changed.
The per-store semantics are pinned in ``test_store_contract.py``."""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.mcp.app import McpHivemind, hive_feedback, hive_list, hive_withdraw, hive_write
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.services.governance import GovernanceService, WriteService
from hivemind.services.read import ReadService
from hivemind.services.search import SearchService
from tests.fakes import FakeAuthenticator, FixedClock, make_clock, make_embedder, make_search_config


class TickingClock(FixedClock):
    """Every reading is a second later: events order strictly, as in real time."""

    def __call__(self) -> datetime:
        self._now += timedelta(seconds=1)
        return self._now


def _store() -> MemoryStore:
    return MemoryStore(TickingClock(make_clock()()))


def _agent(name: str, level: TrustLevel = TrustLevel.CONTRIBUTOR) -> Credential:
    return Credential(
        user_id=name,
        agent_id=name,
        agent_name=name,
        access_controlled=True,
        trust_level=level,
        home_fleet_id="fleet-a",
    )


AUTHOR, ME, OTHER = _agent("author"), _agent("me"), _agent("other")
ME_DEMOTED = _agent("me", TrustLevel.UNTRUSTED)
ORG = Credential(user_id="org", is_org=True)
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


async def _entry(store: MemoryStore, summary: str, **kw: object) -> str:
    out = await hive_write(_surface(store, AUTHOR), kind="fact", summary=summary, **kw)  # type: ignore[arg-type]
    return str(out["id"])


async def _rate(store: MemoryStore, who: Credential, entry_id: str, verdict: str) -> None:
    await hive_feedback(_surface(store, who), entry_id, verdict=verdict, note="checked")


async def _relied(store: MemoryStore, who: Credential) -> dict[str, dict[str, object]]:
    out = await hive_list(_surface(store, who), relied_on_changed=True)
    return {str(e["id"]): e for e in out["entries"]}  # type: ignore[attr-defined]


def _rest(store: MemoryStore, key: str, credential: Credential) -> httpx.AsyncClient:
    app = create_app_for_config(
        Settings(),
        store=store,
        embedder=make_embedder(),
        authenticator=FakeAuthenticator(agent_credentials={key: credential}),
        search_config=make_search_config(),
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url="http://t",
        headers={"X-API-Key": key},
    )


async def test_a_superseded_entry_is_listed_with_its_state_and_counts() -> None:
    store = _store()
    old = await _entry(store, "pool is 20")
    await _rate(store, ME, old, "helpful")
    new = await _entry(store, "pool is 90", supersedes=[old])
    listed = await _relied(store, ME)
    assert set(listed) == {old}
    assert listed[old]["state"] == "superseded" and listed[old]["superseded_by"] == new
    assert listed[old]["feedback"] == {"helpful": 1, "stale": 0, "wrong": 0}


async def test_a_withdrawn_entry_is_listed() -> None:
    store = _store()
    gone = await _entry(store, "to be withdrawn")
    await _rate(store, ME, gone, "helpful")
    await hive_withdraw(_surface(store, AUTHOR), gone, reason="obsolete")
    assert (await _relied(store, ME))[gone]["state"] == "withdrawn"


async def test_a_later_flag_lists_the_entry_and_shows_the_report_counts() -> None:
    store = _store()
    built_on = await _entry(store, "endpoint is /v1")
    await _rate(store, ME, built_on, "helpful")
    assert await _relied(store, ME) == {}
    await _rate(store, OTHER, built_on, "wrong")
    listed = await _relied(store, ME)
    assert listed[built_on]["state"] == "active"
    assert listed[built_on]["feedback"] == {"helpful": 1, "stale": 0, "wrong": 1}


async def test_a_flag_before_the_rating_and_unrated_entries_are_not_listed() -> None:
    store = _store()
    flagged_first = await _entry(store, "flagged first")
    await _rate(store, OTHER, flagged_first, "stale")
    await _rate(store, ME, flagged_first, "helpful")
    unrated = await _entry(store, "never rated")
    await _rate(store, OTHER, unrated, "wrong")
    assert await _relied(store, ME) == {}


async def test_an_entry_the_caller_can_no_longer_read_is_not_listed() -> None:
    store = _store()
    entry = await _entry(store, "shared at first")
    await _rate(store, ME, entry, "helpful")
    await _rate(store, OTHER, entry, "wrong")
    assert set(await _relied(store, ME)) == {entry}
    assert await _relied(store, ME_DEMOTED) == {}  # demoted: nothing is readable


async def test_no_agent_identity_is_an_error_on_both_surfaces() -> None:
    store = _store()
    out = await hive_list(_surface(store, ORG), relied_on_changed=True)
    assert out["error"]["code"] == "agent_unresolved"  # type: ignore[index]
    async with _rest(store, "org-k", ORG) as client:
        resp = await client.get("/v1/entries", params={"relied_on_changed": "true"})
    assert resp.status_code == 422


async def test_the_admin_key_is_keyed_as_admin_like_its_feedback() -> None:
    store = _store()
    entry = await _entry(store, "admin relied on this")
    await _rate(store, ADMIN, entry, "helpful")
    await _rate(store, OTHER, entry, "stale")
    assert set(await _relied(store, ADMIN)) == {entry}


async def test_rest_lists_the_same_entries_with_their_state() -> None:
    store = _store()
    old = await _entry(store, "pool is 20")
    await _rate(store, ME, old, "helpful")
    await _entry(store, "pool is 90", supersedes=[old])
    async with _rest(store, "k", ME) as client:
        resp = await client.get("/v1/entries", params={"relied_on_changed": "true"})
    assert [(e["id"], e["state"]) for e in resp.json()] == [(old, "superseded")]


async def test_include_body_returns_full_entries_with_the_counts() -> None:
    store = _store()
    old = await _entry(store, "pool is 20", body="the long form")
    await _rate(store, ME, old, "helpful")
    await _entry(store, "pool is 90", supersedes=[old])
    out = await hive_list(_surface(store, ME), relied_on_changed=True, include_body=True)
    [item] = out["entries"]  # type: ignore[misc]
    assert item["id"] == old and item["body"] == "the long form"
    assert item["state"] == "superseded" and item["feedback"]["helpful"] == 1


async def test_an_explicit_include_inactive_false_still_lists_inactive_entries() -> None:
    store = _store()
    old = await _entry(store, "pool is 20")
    await _rate(store, ME, old, "helpful")
    await _entry(store, "pool is 90", supersedes=[old])
    out = await hive_list(_surface(store, ME), relied_on_changed=True, include_inactive=False)
    assert [e["id"] for e in out["entries"]] == [old]  # type: ignore[attr-defined]


async def test_the_list_pages_with_limit_and_offset() -> None:
    store = _store()
    ids = []
    for n in range(3):
        entry = await _entry(store, f"fact {n}")
        await _rate(store, ME, entry, "helpful")
        await _rate(store, OTHER, entry, "wrong")
        ids.append(entry)
    surface = _surface(store, ME)
    first = await hive_list(surface, relied_on_changed=True, limit=2)
    second = await hive_list(surface, relied_on_changed=True, limit=2, offset=2)
    got = [e["id"] for e in first["entries"]] + [e["id"] for e in second["entries"]]  # type: ignore[attr-defined]
    assert got == ids[::-1]  # newest entry first, across both pages
    assert first["count"] == 2 and second["count"] == 1


async def test_rating_again_after_the_change_clears_the_entry() -> None:
    store = _store()
    entry = await _entry(store, "endpoint is /v1")
    await _rate(store, ME, entry, "helpful")
    await _rate(store, OTHER, entry, "wrong")
    assert set(await _relied(store, ME)) == {entry}
    await _rate(store, ME, entry, "helpful")  # re-checked: it holds
    assert await _relied(store, ME) == {}
