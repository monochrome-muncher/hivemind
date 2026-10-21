"""Check mode (``check`` on ``hive_write``, ``?check=true`` on
``POST /v1/entries``): every validation of a write, the related entries and
warnings, and nothing stored. Also the richer ``related`` items."""

from __future__ import annotations

import httpx

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.domain.entry import EntryDraft, EntryFilters, ExtractedEntity
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.mcp.app import McpHivemind, hive_write
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.services.governance import GovernanceService
from hivemind.services.read import ReadService
from hivemind.services.search import SearchService
from hivemind.services.write import WriteService
from tests.fakes import (
    FakeAuthenticator,
    FakeExtractor,
    make_clock,
    make_embedder,
    make_search_config,
)


def _agent(name: str, fleet: str) -> Credential:
    return Credential(
        user_id=name,
        agent_id=name,
        agent_name=name,
        access_controlled=True,
        trust_level=TrustLevel.CONTRIBUTOR,
        home_fleet_id=fleet,
    )


WRITER = _agent("writer", "fleet-a")
PEER = _agent("peer", "fleet-a")


class _CountingStore(MemoryStore):
    quality_calls = 0

    async def quality_counts(self, entry_ids: list[str]) -> dict[str, tuple[int, int, int]]:
        self.quality_calls += 1
        return await super().quality_counts(entry_ids)


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


async def _count(store: MemoryStore) -> int:
    return await store.count_entries(EntryFilters())


async def test_check_stores_nothing_and_reports_related_and_warnings() -> None:
    store = MemoryStore(make_clock())
    original = await hive_write(
        _surface(store, PEER), kind="fact", summary="billing on node 20", tags=["billing"]
    )
    result = await hive_write(
        _surface(store, WRITER),
        kind="fact",
        summary="billing on node 20",
        body="see /home/w/x",
        check=True,
    )
    assert result["checked"] is True
    assert "id" not in result
    assert [r["id"] for r in result["related"]] == [original["id"]]  # type: ignore[index, union-attr]
    assert [w["code"] for w in result["warnings"]] == ["local_path"]  # type: ignore[index, union-attr]
    assert await _count(store) == 1


async def test_check_leaves_out_the_entries_it_supersedes_like_a_write() -> None:
    """A real write's ``related`` cannot hold its supersession targets (they
    are superseded by then); a check must not hold them either, or it tells
    the writer to withdraw over the entry it is replacing."""
    store = MemoryStore(make_clock())
    surface = _surface(store, WRITER)
    old = await hive_write(surface, kind="fact", summary="billing runs on node 20")
    other = await hive_write(surface, kind="fact", summary="billing runs on node 18 in staging")
    args = {"kind": "fact", "summary": "billing runs on node 22", "supersedes": [old["id"]]}
    checked = await hive_write(surface, check=True, **args)  # type: ignore[arg-type]
    real = await hive_write(surface, **args)  # type: ignore[arg-type]
    checked_ids = [r["id"] for r in checked["related"]]  # type: ignore[index, union-attr]
    assert old["id"] not in checked_ids
    assert other["id"] in checked_ids
    assert checked_ids == [r["id"] for r in real["related"]]  # type: ignore[index, union-attr]


async def test_check_raises_what_a_write_would() -> None:
    store = MemoryStore(make_clock())
    surface = _surface(store, WRITER)
    for kwargs, code in [
        ({"supersedes": ["00000000-0000-0000-0000-000000000000"]}, "supersede_denied"),
        ({"see_also": ["00000000-0000-0000-0000-000000000000"]}, "invalid_input"),
        ({"scope": "galaxy"}, "invalid_input"),
        ({"summary": "x" * 500}, "invalid_input"),
    ]:
        args = {"kind": "fact", "summary": "s", **kwargs}
        checked = await hive_write(surface, check=True, **args)  # type: ignore[arg-type]
        real = await hive_write(surface, **args)  # type: ignore[arg-type]
        assert checked == real, kwargs
        assert checked["error"]["code"] == code
    assert await _count(store) == 0


async def test_check_does_not_run_the_extractor() -> None:
    class Spy(FakeExtractor):
        calls = 0

        async def extract_entry(self, draft: EntryDraft) -> tuple[ExtractedEntity, ...]:
            self.calls += 1
            return await super().extract_entry(draft)

    store = MemoryStore(make_clock())
    extractor = Spy()
    embedder, config, clock = make_embedder(), make_search_config(), make_clock()
    app = McpHivemind(
        store=store,
        write_service=WriteService(store, embedder, extractor),
        search_service=SearchService(store, embedder, config, now_fn=clock),
        governance_service=GovernanceService(store),
        read_service=ReadService(store, GovernanceService(store), config),
        access_service=AccessService(store),
        search_config=config,
        credential=WRITER,
    )
    await hive_write(app, kind="fact", summary="Postgres on node 20", check=True)
    assert extractor.calls == 0
    assert await _count(store) == 0
    await hive_write(app, kind="fact", summary="Postgres on node 20")
    assert extractor.calls == 1


async def test_related_carries_tags_feedback_and_same_author() -> None:
    store = _CountingStore(make_clock())
    mine = await hive_write(
        _surface(store, WRITER), kind="fact", summary="billing on node 20", tags=["billing", "ops"]
    )
    theirs = await hive_write(_surface(store, PEER), kind="fact", summary="billing on node 20")
    for user, verdict in [("u1", Verdict.HELPFUL), ("u2", Verdict.HELPFUL), ("u3", Verdict.STALE)]:
        await store.record_feedback(Feedback(str(theirs["id"]), user, "ag", verdict, None))
    store.quality_calls = 0
    result = await hive_write(_surface(store, WRITER), kind="fact", summary="billing on node 20")
    by_id = {r["id"]: r for r in result["related"]}  # type: ignore[union-attr]
    assert by_id[mine["id"]]["same_author"] is True
    assert by_id[mine["id"]]["tags"] == ["billing", "ops"]
    assert by_id[mine["id"]]["feedback"] == {"helpful": 0, "stale": 0, "wrong": 0}
    assert by_id[theirs["id"]]["same_author"] is False
    assert by_id[theirs["id"]]["feedback"] == {"helpful": 2, "stale": 1, "wrong": 0}
    assert store.quality_calls == 1  # one batched read, not one per entry


async def test_rest_check_returns_200_without_an_entry() -> None:
    store = MemoryStore(make_clock())
    original = await hive_write(
        _surface(store, PEER), kind="fact", summary="billing on node 20", tags=["t"]
    )
    app = create_app_for_config(
        Settings(),
        store=store,
        embedder=make_embedder(),
        authenticator=FakeAuthenticator(agent_credentials={"k": WRITER}),
        search_config=make_search_config(),
    )
    transport = httpx.ASGITransport(app=create_app(app))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        headers = {"X-API-Key": "k"}
        body = {"kind": "fact", "summary": "billing on node 20"}
        checked = await client.post("/v1/entries?check=true", json=body, headers=headers)
        bad = await client.post(
            "/v1/entries?check=true",
            json={**body, "supersedes": [str(original["id"])[::-1]]},
            headers=headers,
        )
        real = await client.post("/v1/entries", json=body, headers=headers)
    assert checked.status_code == 200
    data = checked.json()
    # the check's summary is identical to `original`, so it reports a
    # near-duplicate (ADR 0052's `related` now carries that signal).
    assert data["checked"] is True and [w["code"] for w in data["warnings"]] == ["near_duplicate"]
    assert "id" not in data
    [related] = data["related"]
    assert related["id"] == original["id"]
    assert related["tags"] == ["t"] and related["same_author"] is False
    assert related["feedback"] == {"helpful": 0, "stale": 0, "wrong": 0}
    assert bad.status_code == 403
    assert real.status_code == 201
    assert await _count(store) == 2


LURKER = Credential(
    user_id="lurker",
    agent_id="lurker",
    agent_name="lurker",
    access_controlled=True,
    trust_level=TrustLevel.UNTRUSTED,
    home_fleet_id="fleet-a",
)
ADMIN = Credential(user_id="root", agent_id="root", agent_name="root", is_admin=True)


async def test_check_and_write_agree_on_permission_denied_over_mcp() -> None:
    for cred in (LURKER, ADMIN):
        store = MemoryStore(make_clock())
        args = {"kind": "fact", "summary": "s", "scope": "fleet"}
        checked = await hive_write(_surface(store, cred), check=True, **args)  # type: ignore[arg-type]
        real = await hive_write(_surface(store, cred), **args)  # type: ignore[arg-type]
        assert checked == real
        assert checked["error"]["code"] == "permission_denied"  # type: ignore[index]
        assert await _count(store) == 0


def _client(store: MemoryStore, credential: Credential) -> httpx.AsyncClient:
    app = create_app_for_config(
        Settings(),
        store=store,
        embedder=make_embedder(),
        authenticator=FakeAuthenticator(agent_credentials={"k": credential}),
        search_config=make_search_config(),
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url="http://t",
        headers={"X-API-Key": "k"},
    )


_MISSING = "00000000-0000-0000-0000-000000000000"


async def test_rest_check_and_write_fail_alike() -> None:
    cases = [
        (LURKER, {"scope": "fleet"}, 403),
        (ADMIN, {"scope": "fleet"}, 403),
        (WRITER, {"scope": "org"}, 403),
        (WRITER, {"supersedes": [_MISSING]}, 403),
        (WRITER, {"see_also": [_MISSING]}, 422),
        (WRITER, {"summary": "x" * 500}, 422),
        (WRITER, {"occurred_at": "1800-01-01T00:00:00Z"}, 422),
    ]
    for cred, extra, status in cases:
        store = MemoryStore(make_clock())
        body = {"kind": "fact", "summary": "s", **extra}
        async with _client(store, cred) as client:
            checked = await client.post("/v1/entries?check=true", json=body)
            real = await client.post("/v1/entries", json=body)
        assert (checked.status_code, checked.json()) == (real.status_code, real.json()), extra
        assert checked.status_code == status, extra
        assert await _count(store) == 0


async def test_a_successful_check_leaves_targets_and_links_untouched() -> None:
    store = MemoryStore(make_clock())
    old = await hive_write(_surface(store, WRITER), kind="fact", summary="old fact")
    linked = await hive_write(_surface(store, WRITER), kind="fact", summary="other fact")
    result = await hive_write(
        _surface(store, WRITER),
        kind="fact",
        summary="new fact",
        supersedes=[str(old["id"])],
        see_also=[str(linked["id"])],
        check=True,
    )
    assert result["checked"] is True
    entry = await store.get_entry(str(old["id"]))
    assert entry is not None and entry.state.value == "active" and entry.superseded_by is None
    assert await _count(store) == 2
    other = await store.get_entry(str(linked["id"]))
    assert other is not None
    async with _client(store, WRITER) as client:
        got = (await client.get(f"/v1/entries/{linked['id']}")).json()
    assert got["linked_from"] == []


class _BrokenCountsStore(MemoryStore):
    async def quality_counts(self, entry_ids: list[str]) -> dict[str, tuple[int, int, int]]:
        raise RuntimeError("counts down")


async def test_failed_feedback_counts_keep_the_related_list() -> None:
    store = _BrokenCountsStore(make_clock())
    original = await hive_write(_surface(store, PEER), kind="fact", summary="billing on node 20")
    result = await hive_write(_surface(store, WRITER), kind="fact", summary="billing on node 20")
    [related] = result["related"]  # type: ignore[misc]
    assert related["id"] == original["id"]
    assert related["feedback"] == {"helpful": 0, "stale": 0, "wrong": 0}


async def test_openapi_documents_the_check_reply_beside_the_plain_entry() -> None:
    async with _client(MemoryStore(make_clock()), WRITER) as client:
        spec = (await client.get("/openapi.json")).json()
    op = spec["paths"]["/v1/entries"]["post"]["responses"]
    assert op["201"]["content"]["application/json"]["schema"]["$ref"].endswith("/EntryOut")
    assert op["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/CheckOut")
