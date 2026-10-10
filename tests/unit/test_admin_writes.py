"""The admin key writes ``self`` or ``org`` entries only (ADR 0060), so an
admin correction of a fleet entry stays readable to that fleet."""

from __future__ import annotations

import httpx

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.mcp.app import McpHivemind, hive_feedback, hive_get, hive_search, hive_write
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.services.governance import GovernanceService, WriteService
from hivemind.services.search import SearchService
from tests.fakes import FakeAuthenticator, make_clock, make_embedder, make_search_config

DEV = Credential(
    user_id="dev",
    agent_id="dev",
    agent_name="dev",
    access_controlled=True,
    trust_level=TrustLevel.CONTRIBUTOR,
    home_fleet_id="fleet-a",
)
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


async def test_an_admin_correction_of_a_fleet_entry_stays_readable_to_the_fleet() -> None:
    store = MemoryStore(make_clock())
    old = await hive_write(_surface(store, DEV), kind="fact", summary="the build cache is warm")
    admin = _surface(store, ADMIN)

    refused = await hive_write(
        admin,
        kind="fact",
        summary="the build cache is cold",
        scope="fleet",
        supersedes=[str(old["id"])],
        agent="ops",
    )
    assert refused["error"]["code"] == "permission_denied"  # type: ignore[index]

    narrow = await hive_write(
        admin,
        kind="fact",
        summary="the build cache is cold",
        scope="self",
        supersedes=[str(old["id"])],
        agent="ops",
    )
    assert narrow["error"]["code"] == "supersede_denied"  # type: ignore[index]

    fixed = await hive_write(
        admin,
        kind="fact",
        summary="the build cache is cold",
        supersedes=[str(old["id"])],
        agent="ops",
    )
    assert fixed["scope"] == "org" and fixed["author"] == "admin"
    dev = _surface(store, DEV)
    assert (await hive_get(dev, str(fixed["id"])))["summary"] == "the build cache is cold"
    hits = (await hive_search(dev, "build cache"))["hits"]
    assert [h["id"] for h in hits] == [fixed["id"]]  # type: ignore[index]


async def test_rest_refuses_an_admin_fleet_write() -> None:
    app = create_app_for_config(
        Settings(),
        store=MemoryStore(make_clock()),
        embedder=make_embedder(),
        authenticator=FakeAuthenticator(agent_credentials={"k": ADMIN}),
        search_config=make_search_config(),
    )
    transport = httpx.ASGITransport(app=create_app(app))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://t", headers={"X-API-Key": "k"}
    ) as client:
        resp = await client.post(
            "/v1/entries",
            json={"kind": "fact", "summary": "s", "scope": "fleet", "agent": "ops"},
        )
        ok = await client.post("/v1/entries", json={"kind": "fact", "summary": "s", "agent": "ops"})
    assert resp.status_code == 403
    assert (ok.status_code, ok.json()["scope"]) == (201, "org")


# ADR 0066: the admin key has one feedback vote per entry.
AGENT = Credential(
    user_id="ops-agent",
    agent_id="ops-agent",
    agent_name="ops-agent",
    access_controlled=True,
    trust_level=TrustLevel.CONTRIBUTOR,
    home_fleet_id="fleet-a",
)


async def test_the_admin_key_has_one_feedback_vote_whatever_agent_it_reports() -> None:
    store = MemoryStore(make_clock())
    # Another agent's entry: nobody rates its own (ADR 0065).
    entry = await hive_write(_surface(store, AGENT), kind="fact", summary="s")
    admin = _surface(store, ADMIN)
    for label in ("a", "b", "c"):
        result = await hive_feedback(admin, str(entry["id"]), "helpful", agent=label)
        assert result["feedback"]["agent"] == "admin"  # type: ignore[index]
    assert await store.feedback_counts(str(entry["id"])) == (1, 0, 0)
    rows = await store.list_feedback(str(entry["id"]), 10)
    assert [(r.user, r.agent) for r in rows] == [("admin", "admin")]


async def test_the_admin_key_needs_no_agent_to_give_feedback() -> None:
    store = MemoryStore(make_clock())
    # Another agent's entry: nobody rates its own (ADR 0065).
    entry = await hive_write(_surface(store, AGENT), kind="fact", summary="s")
    result = await hive_feedback(_surface(store, ADMIN), str(entry["id"]), "stale")
    assert result["feedback"]["agent"] == "admin"  # type: ignore[index]
    many = await hive_feedback(
        _surface(store, ADMIN), verdict="wrong", entry_ids=[str(entry["id"])]
    )
    assert "error" not in many
    assert await store.feedback_counts(str(entry["id"])) == (0, 0, 1)  # latest verdict wins

    app = create_app_for_config(
        Settings(),
        store=store,
        embedder=make_embedder(),
        authenticator=FakeAuthenticator(agent_credentials={"k": ADMIN}),
        search_config=make_search_config(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url="http://t",
        headers={"X-API-Key": "k"},
    ) as client:
        one = await client.post(f"/v1/entries/{entry['id']}/feedback", json={"verdict": "helpful"})
        batch = await client.post(
            "/v1/feedback", json={"entry_ids": [entry["id"]], "verdict": "helpful", "agent": "x"}
        )
    assert (one.status_code, batch.status_code) == (200, 200)
    assert await store.feedback_counts(str(entry["id"])) == (1, 0, 0)


async def test_an_agent_key_keeps_its_own_feedback_identity() -> None:
    store = MemoryStore(make_clock())
    entry = await hive_write(_surface(store, ADMIN), kind="fact", summary="s", agent="ops")
    await hive_feedback(_surface(store, AGENT), str(entry["id"]), "helpful", agent="other")
    rows = await store.list_feedback(str(entry["id"]), 10)
    assert [r.agent for r in rows] == ["ops-agent"]
