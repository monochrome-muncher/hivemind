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
from hivemind.mcp.app import McpHivemind, hive_write
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.services.governance import GovernanceService
from hivemind.services.read import ReadService
from hivemind.services.search import SearchService
from hivemind.services.write import WriteService
from hivemind.services.write_warnings import (
    answers_open_question_warning,
    has_local_path,
    is_entry_link,
    near_duplicate_warning,
)
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


def _surface(store: MemoryStore, credential: Credential, embedder: object = None) -> McpHivemind:
    clock, config = make_clock(), make_search_config()
    embedder = embedder if embedder is not None else make_embedder()
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
    assert "local_path" not in _codes(private)  # a self note may name local paths


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


@pytest.mark.parametrize(
    "ref",
    [_ID, _ID.replace("-", ""), f"entry:{_ID}", f"https://hm.example/v1/entries/{_ID}"],
)
def test_entry_links_are_recognised(ref: str) -> None:
    """The warning and the fleet health grounding share (ADR 0075) use one
    pattern, so a source the warning calls an entry link never counts as
    grounded."""

    assert is_entry_link(Source(SourceType.OTHER, ref))


@pytest.mark.parametrize("ref", ["src/app.py", "https://example.com/a", "session-123", "entry:abc"])
def test_other_refs_are_not_entry_links(ref: str) -> None:

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

    counter = {"n": 0}

    async def codes(**kw: object) -> list[str]:
        counter["n"] += 1
        args = {"kind": "insight", "summary": f"entry {counter['n']}", **kw}
        return _codes(await hive_write(_surface(store, A), **args))  # type: ignore[arg-type]

    assert await codes(sources=[link, real]) == []  # one external source is enough
    assert await codes() == []  # no sources, no links: not a synthesis
    assert await codes(kind="fact", sources=[link]) == []
    assert await codes(sources=[link], scope="self") == []


def test_near_duplicate_warning_fires_at_the_threshold() -> None:
    """The pure warning logic: high similarity warns, low stays quiet."""
    from types import SimpleNamespace

    def rel(sim: float, entry_id: str = "abc") -> object:
        return SimpleNamespace(similarity=sim, entry=SimpleNamespace(id=entry_id))

    (w,) = near_duplicate_warning([rel(0.9)])
    assert w.code == "near_duplicate"
    assert "abc" in w.message and "0.90" in w.message
    # the message offers the three readings, not one fixed remedy
    assert "rate it" in w.message and "supersede" in w.message and "see_also" in w.message
    assert near_duplicate_warning([rel(0.89)]) == ()
    assert near_duplicate_warning([]) == ()
    (w,) = near_duplicate_warning([rel(0.5, "low"), rel(0.95, "high")])
    assert "high" in w.message
    # an entry the draft already links with see_also does not count
    assert near_duplicate_warning([rel(0.95, "linked")], ("linked",)) == ()
    (w,) = near_duplicate_warning([rel(0.97, "linked"), rel(0.92, "other")], ("linked",))
    assert "other" in w.message


async def test_near_duplicate_warns_on_a_near_identical_write() -> None:
    """Through the write path: a near-copy of an existing entry warns, in a
    write and in a check; a dissimilar one does not, and neither does a
    near-copy that names the entry in ``see_also``. A 16-dim fake embedder
    reaches the threshold for near-identical text (the default 4-dim one is
    too noisy)."""
    from tests.fakes import FakeEmbedder

    store = MemoryStore(make_clock())
    surface = _surface(store, A, embedder=FakeEmbedder(dimension=16))
    summary = "billing in the eu region runs on node 20 behind the shared proxy"
    first = await hive_write(surface, kind="fact", summary=summary)  # type: ignore[arg-type]
    copy = summary.replace("node 20", "node 21")
    checked = await hive_write(surface, kind="fact", summary=copy, check=True)  # type: ignore[arg-type]
    assert _codes(checked) == ["near_duplicate"]
    near = await hive_write(surface, kind="fact", summary=copy)  # type: ignore[arg-type]
    assert "near_duplicate" in _codes(near)
    linked = await hive_write(
        surface,
        kind="fact",
        summary=copy,  # type: ignore[arg-type]
        see_also=[first["id"], near["id"]],  # type: ignore[list-item]
        scope="self",
    )
    assert "near_duplicate" not in _codes(linked)
    other = await hive_write(
        surface,
        kind="fact",
        summary="the deploy pipeline uses a separate queue",  # type: ignore[arg-type]
    )
    assert "near_duplicate" not in _codes(other)


def _related(sim: float, entry_id: str, *tags: str, active: bool = True) -> object:
    from types import SimpleNamespace

    from hivemind.domain.entry import EntryState

    state = EntryState.ACTIVE if active else EntryState.WITHDRAWN
    return SimpleNamespace(
        similarity=sim, entry=SimpleNamespace(id=entry_id, tags=tags, state=state)
    )


def test_answers_open_question_fires_at_the_floor_on_either_tag() -> None:
    for tag in ("open-question", "anomaly"):
        (w,) = answers_open_question_warning([_related(0.70, "q1", tag)])  # type: ignore[list-item]
        assert w.code == "answers_open_question"
        assert "q1" in w.message and "0.70" in w.message
        assert "supersedes" in w.message and "see_also" in w.message


def test_answers_open_question_stays_quiet_when_it_should() -> None:
    quiet = answers_open_question_warning
    assert quiet([]) == ()
    # below the floor
    assert quiet([_related(0.69, "q", "open-question")]) == ()  # type: ignore[list-item]
    # not an open question
    assert quiet([_related(0.95, "f", "billing")]) == ()  # type: ignore[list-item]
    # inactive neighbour
    assert quiet([_related(0.95, "q", "open-question", active=False)]) == ()  # type: ignore[list-item]
    # already named in see_also or supersedes (any uuid spelling)
    rel = [_related(0.95, "0F8FAD5B-D9CB-469F-A165-70867728950E", "open-question")]
    assert quiet(rel, linked=("0f8fad5bd9cb469fa16570867728950e",)) == ()  # type: ignore[arg-type]
    # the draft is itself a question or an anomaly
    for tag in ("open-question", "anomaly"):
        assert quiet([_related(0.95, "q", "open-question")], ("x", tag)) == ()  # type: ignore[list-item]


def test_answers_open_question_names_the_closest_and_counts_the_rest() -> None:
    rel = [
        _related(0.80, "mid", "open-question"),
        _related(0.92, "top", "anomaly"),
        _related(0.75, "low", "open-question"),
        _related(0.99, "linked", "open-question"),
        _related(0.97, "plain", "fact"),
    ]
    (w,) = answers_open_question_warning(rel, linked=("linked",))  # type: ignore[arg-type]
    assert "top" in w.message and "0.92" in w.message and "+2 more" in w.message
    (w,) = answers_open_question_warning(rel[:1])  # type: ignore[arg-type]
    assert "more" not in w.message


async def test_answers_open_question_through_the_write_path() -> None:
    """A write that restates an open question's subject warns, in a write and
    in a check; naming the question in see_also or supersedes does not, and a
    question tagged open-question itself does not."""
    from tests.fakes import FakeEmbedder

    store = MemoryStore(make_clock())
    surface = _surface(store, A, embedder=FakeEmbedder(dimension=16))
    text = "why does billing in the eu region stall on node 20 behind the shared proxy"
    question = await hive_write(  # type: ignore[arg-type]
        surface, kind="insight", summary=text, tags=["open-question"]
    )
    answer = text.replace("why does", "because").replace("stall", "stalls")
    checked = await hive_write(surface, kind="fact", summary=answer, check=True)  # type: ignore[arg-type]
    assert "answers_open_question" in _codes(checked)
    assert str(question["id"]) in str(checked["warnings"])
    written = await hive_write(surface, kind="fact", summary=answer)  # type: ignore[arg-type]
    assert "answers_open_question" in _codes(written)
    linked = await hive_write(
        surface,
        kind="fact",
        summary=answer,
        see_also=[question["id"]],  # type: ignore[list-item]
        check=True,
    )
    assert "answers_open_question" not in _codes(linked)
    another = await hive_write(  # type: ignore[arg-type]
        surface, kind="insight", summary=answer, tags=["anomaly"], check=True
    )
    assert "answers_open_question" not in _codes(another)
    superseding = await hive_write(
        surface,
        kind="fact",
        summary=answer,
        supersedes=[question["id"]],  # type: ignore[list-item]
    )
    assert "answers_open_question" not in _codes(superseding)
