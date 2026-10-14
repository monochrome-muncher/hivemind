"""Write-time warnings (SPEC.md §4.1): advisory, computed from the draft and
the targets the write path loaded. Pure cases first, then both surfaces."""

from __future__ import annotations

import httpx
import pytest

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.domain.entry import Source, SourceType
from hivemind.domain.health import is_entry_ref
from hivemind.mcp.app import McpHivemind, hive_write
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.services.governance import GovernanceService, WriteService
from hivemind.services.read import ReadService
from hivemind.services.search import SearchService
from hivemind.services.write_warnings import has_local_path, is_entry_link
from tests.fakes import FakeAuthenticator, make_clock, make_embedder, make_search_config


@pytest.mark.parametrize(
    "text",
    [
        "config lives in /home/alice/.config/app",
        "see /Users/bob/work/repo",
        "run ~/bin/tool first",
        "C:\\Users\\carol\\AppData",
        "c:\\users\\carol",
        "/home/dashboard",
        "C:/Users/frank/x and C:\\\\Users\\\\gina\\\\x",
        "(/home/erin/proj) is stale",
        "path:\n~/.ssh/config",
    ],
)
def test_local_paths_are_detected(text: str) -> None:
    assert has_local_path(text)


@pytest.mark.parametrize(
    "text",
    [
        "https://github.com/home/repo and https://example.com/Users/list",
        "file:///home/alice/x",
        "src/home/config.py and ./home/x",
        "the /home/ directory and /Users/ root",
        "approx ~5 minutes, ~/ alone",
        "use C:\\Program Files\\tool",
        "a/b~/c",
        "GET /users/42 and POST /users/login",
        "/HOME/dave/x",
        "",
    ],
)
def test_conservative_misses(text: str) -> None:
    assert not has_local_path(text)


def _agent(name: str, fleet: str, level: TrustLevel = TrustLevel.CONTRIBUTOR) -> Credential:
    return Credential(
        user_id=name,
        agent_id=name,
        agent_name=name,
        access_controlled=True,
        trust_level=level,
        home_fleet_id=fleet,
    )


A = _agent("a", "fleet-a")
A2 = _agent("a2", "fleet-a")
B = _agent("b", "fleet-b")
LEAD = _agent("lead", "fleet-a", TrustLevel.PRIVILEGED)


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


async def _write(store: MemoryStore, cred: Credential, **kw: object) -> dict[str, object]:
    kw.setdefault("summary", "billing runs on node 20")
    kw.setdefault("kind", "fact")
    return await hive_write(_surface(store, cred), **kw)  # type: ignore[arg-type]


def _codes(result: dict[str, object]) -> list[str]:
    return [w["code"] for w in result["warnings"]]  # type: ignore[index, union-attr]


async def test_a_clean_write_has_no_warnings() -> None:
    result = await _write(MemoryStore(make_clock()), A)
    assert result["warnings"] == []


async def test_local_path_warns_on_fleet_entries_only() -> None:
    store = MemoryStore(make_clock())
    fleet = await _write(store, A, body="checkout at /home/a/repo")
    assert _codes(fleet) == ["local_path"]
    assert fleet["warnings"][0]["message"]  # type: ignore[index]
    summary = await _write(store, A, summary="fix in ~/repo/x.py", scope="fleet")
    assert _codes(summary) == ["local_path"]
    private = await _write(store, A, body="checkout at /home/a/repo", scope="self")
    assert private["warnings"] == []


async def test_foreign_link_when_a_fleet_entry_relays_another_fleets_entry() -> None:
    store = MemoryStore(make_clock())
    foreign = await _write(store, B, summary="b-only fact")
    own = await _write(store, A, summary="a fact")
    linked = await _write(store, LEAD, summary="relay", see_also=[foreign["id"]])
    assert _codes(linked) == ["foreign_link"]
    # a same-fleet link, and a self entry linking out, are fine
    assert (await _write(store, LEAD, summary="x", see_also=[own["id"]]))["warnings"] == []
    # superseding a foreign fleet's entry is refused outright (ADR 0033)
    refused = await _write(store, LEAD, summary="z", supersedes=[foreign["id"]])
    assert refused["error"]["code"] == "supersede_denied"  # type: ignore[index]
    private = await _write(store, LEAD, summary="y", scope="self", see_also=[foreign["id"]])
    assert private["warnings"] == []


async def test_backdated_successor_and_sources_dropped() -> None:
    store = MemoryStore(make_clock())
    old = await _write(
        store,
        A,
        occurred_at="2026-05-01T00:00:00+00:00",
        sources=[{"type": "url", "ref": "https://x/1"}],
    )
    result = await _write(store, A, supersedes=[old["id"]], occurred_at="2026-04-01T00:00:00+00:00")
    assert _codes(result) == ["backdated_successor", "sources_dropped"]


async def test_carrying_a_source_forward_keeps_quiet() -> None:
    store = MemoryStore(make_clock())
    src = {"type": "url", "ref": "https://x/1"}
    old = await _write(store, A, occurred_at="2026-05-01T00:00:00+00:00", sources=[src])
    result = await _write(
        store, A, supersedes=[old["id"]], occurred_at="2026-05-02T00:00:00+00:00", sources=[src]
    )
    assert result["warnings"] == []


async def test_fresh_consolidation_needs_two_targets_and_a_day_gap() -> None:
    store = MemoryStore(make_clock())
    one = await _write(store, A, occurred_at="2026-05-01T00:00:00+00:00", summary="one")
    two = await _write(store, A, occurred_at="2026-05-03T00:00:00+00:00", summary="two")
    three = await _write(store, A, occurred_at="2026-05-03T00:00:00+00:00", summary="three")
    single = await _write(store, A, supersedes=[three["id"]], summary="s")  # now: not a merge
    assert "fresh_consolidation" not in _codes(single)
    fresh = await _write(store, A, supersedes=[one["id"], two["id"]], summary="merged")
    assert _codes(fresh) == ["fresh_consolidation"]
    four = await _write(store, A, occurred_at="2026-05-03T00:00:00+00:00", summary="four")
    five = await _write(store, A, occurred_at="2026-05-03T00:00:00+00:00", summary="five")
    near = await _write(
        store,
        A,
        supersedes=[four["id"], five["id"]],
        occurred_at="2026-05-03T20:00:00+00:00",
        summary="merged2",
    )
    assert near["warnings"] == []


@pytest.mark.parametrize("check", [False, True])
async def test_a_warning_never_blocks(check: bool) -> None:
    store = MemoryStore(make_clock())
    result = await _write(store, A, body="in /home/a/x", check=check)
    assert "error" not in result
    assert _codes(result) == ["local_path"]


async def test_rest_reports_warnings_on_a_write() -> None:
    store = MemoryStore(make_clock())
    app = create_app_for_config(
        Settings(),
        store=store,
        embedder=make_embedder(),
        authenticator=FakeAuthenticator(agent_credentials={"k": A2}),
        search_config=make_search_config(),
    )
    transport = httpx.ASGITransport(app=create_app(app))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post(
            "/v1/entries",
            json={"kind": "fact", "summary": "see ~/notes/x"},
            headers={"X-API-Key": "k"},
        )
    assert resp.status_code == 201
    assert [w["code"] for w in resp.json()["warnings"]] == ["local_path"]


_ID = "0b6f2c1e-5d34-4a8e-9c1b-7e2a4f6d8b10"


@pytest.mark.parametrize("ref", [_ID, f"entry:{_ID}", f"https://hm.example/v1/entries/{_ID}"])
def test_entry_links_are_recognised(ref: str) -> None:
    from hivemind.domain.entry import Source, SourceType

    assert is_entry_link(Source(SourceType.OTHER, ref))


@pytest.mark.parametrize("ref", ["src/app.py", "https://example.com/a", "session-123", "entry:abc"])
def test_other_refs_are_not_entry_links(ref: str) -> None:
    from hivemind.domain.entry import Source, SourceType

    assert not is_entry_link(Source(SourceType.URL, ref))


async def test_no_primary_source_on_a_synthesis_of_entries() -> None:
    store = MemoryStore(make_clock())
    base = await _write(store, A, summary="base")
    link = {"type": "other", "ref": f"entry:{base['id']}"}
    # entry-only sources, and empty sources with links, both warn
    only_links = await _write(store, A, kind="insight", sources=[link])  # type: ignore[call-arg]
    assert _codes(only_links) == ["no_primary_source"]
    empty_linked = await hive_write(
        _surface(store, A),
        kind="decision",
        summary="d",
        see_also=[base["id"]],  # type: ignore[arg-type]
    )
    assert _codes(empty_linked) == ["no_primary_source"]
    merged = await hive_write(
        _surface(store, A),
        kind="insight",
        summary="m",
        supersedes=[base["id"]],  # type: ignore[arg-type]
    )
    assert "no_primary_source" in _codes(merged)


async def test_no_primary_source_misses() -> None:
    store = MemoryStore(make_clock())
    base = await _write(store, A, summary="base")
    link = {"type": "other", "ref": f"entry:{base['id']}"}
    real = {"type": "url", "ref": "https://example.com/doc"}

    async def codes(**kw: object) -> list[str]:
        args = {"kind": "insight", "summary": "s", **kw}
        return _codes(await hive_write(_surface(store, A), **args))  # type: ignore[arg-type]

    assert await codes(sources=[link, real]) == []  # one external source is enough
    assert await codes() == []  # no sources, no links: not a synthesis
    assert await codes(kind="fact", sources=[link]) == []
    assert await codes(sources=[link], scope="self") == []


@pytest.mark.parametrize(
    "ref",
    [
        "0f8fad5b-d9cb-469f-a165-70867728950e",
        "0f8fad5bd9cb469fa16570867728950e",
        "https://hivemind.example.com/v1/entries/0f8fad5b-d9cb-469f-a165-70867728950e",
    ],
)
def test_entry_link_matches_the_grounding_metric(ref: str) -> None:
    """The warning and the fleet health grounding share (ADR 0075) use one
    pattern, so a source the warning calls an entry link never counts as
    grounded."""
    source = Source(type=SourceType.OTHER, ref=ref)
    assert is_entry_link(source) and is_entry_ref(ref)
