"""A write reports the nearest existing entries its writer can read
(ADR 0052), on both surfaces, best-effort."""

from __future__ import annotations

import httpx

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import TrustLevel, Visibility
from hivemind.mcp.app import McpHivemind, hive_withdraw, hive_write
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.services.governance import GovernanceService, WriteService
from hivemind.services.search import SearchService
from tests.fakes import FakeAuthenticator, make_clock, make_embedder, make_search_config


def _agent(name: str, fleet: str, level: TrustLevel = TrustLevel.CONTRIBUTOR) -> Credential:
    return Credential(
        user_id=name,
        agent_id=name,
        agent_name=name,
        access_controlled=True,
        trust_level=level,
        home_fleet_id=fleet,
    )


WRITER = _agent("writer", "fleet-a")
PEER = _agent("peer", "fleet-a")
OUTSIDER = _agent("outsider", "fleet-b")


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


async def test_first_write_has_no_related_entries() -> None:
    store = MemoryStore(make_clock())
    result = await hive_write(_surface(store, WRITER), kind="fact", summary="billing on node 20")
    assert result["related"] == []


async def test_a_duplicate_write_reports_the_original_first() -> None:
    store = MemoryStore(make_clock())
    original = await hive_write(_surface(store, PEER), kind="fact", summary="billing on node 20")
    await hive_write(_surface(store, PEER), kind="fact", summary="cookies expire in a day")
    result = await hive_write(_surface(store, WRITER), kind="fact", summary="billing on node 20")
    first = result["related"][0]  # type: ignore[index]
    assert first["id"] == original["id"]
    assert first["similarity"] == 1.0
    assert first["author"] == "peer"
    assert {"kind", "summary", "scope", "fleet_id", "occurred_at"} <= set(first)
    assert result["id"] not in [r["id"] for r in result["related"]]  # type: ignore[union-attr]


async def test_related_is_capped_at_three() -> None:
    store = MemoryStore(make_clock())
    for i in range(5):
        await hive_write(_surface(store, WRITER), kind="fact", summary=f"billing fact {i}")
    result = await hive_write(_surface(store, WRITER), kind="fact", summary="billing fact")
    assert len(result["related"]) == 3  # type: ignore[arg-type]


async def test_related_only_shows_what_the_writer_can_read() -> None:
    store = MemoryStore(make_clock())
    await hive_write(_surface(store, OUTSIDER), kind="fact", summary="billing on node 20")
    await hive_write(_surface(store, PEER), kind="fact", summary="billing on node 20", scope="self")
    result = await hive_write(_surface(store, WRITER), kind="fact", summary="billing on node 20")
    assert result["related"] == []


async def test_related_leaves_out_inactive_entries() -> None:
    store = MemoryStore(make_clock())
    old = await hive_write(_surface(store, WRITER), kind="fact", summary="billing on node 20")
    await hive_withdraw(_surface(store, WRITER), old["id"], reason="wrong")
    result = await hive_write(_surface(store, WRITER), kind="fact", summary="billing on node 20")
    assert result["related"] == []


class _BrokenLookupStore(MemoryStore):
    async def similar_entries(
        self,
        embedding: list[float],
        limit: int,
        *,
        exclude_id: str,
        visibility: Visibility | None = None,
    ) -> list[tuple[str, float]]:
        raise RuntimeError("lookup down")


async def test_a_failed_lookup_does_not_fail_the_write() -> None:
    store = _BrokenLookupStore(make_clock())
    result = await hive_write(_surface(store, WRITER), kind="fact", summary="billing on node 20")
    assert "error" not in result
    assert result["related"] == []
    assert await store.get_entry(str(result["id"])) is not None


async def test_rest_write_reports_related_entries() -> None:
    store = MemoryStore(make_clock())
    original = await hive_write(_surface(store, PEER), kind="fact", summary="billing on node 20")
    app = create_app_for_config(
        Settings(),
        store=store,
        embedder=make_embedder(),
        authenticator=FakeAuthenticator(agent_credentials={"k": WRITER}),
        search_config=make_search_config(),
    )
    transport = httpx.ASGITransport(app=create_app(app))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post(
            "/v1/entries",
            json={"kind": "fact", "summary": "billing on node 20"},
            headers={"X-API-Key": "k"},
        )
    assert resp.status_code == 201
    [related] = resp.json()["related"]
    assert (related["id"], related["similarity"]) == (original["id"], 1.0)
