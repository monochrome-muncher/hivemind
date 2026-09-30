"""Input bounds + name rules (ADR 0040) at every seam: the domain, the
services, REST and MCP. Hermetic: MemoryStore accepts what Postgres would
reject, so these tests pin that the *service layer* refuses it — and that
it does so before the embedder is called."""

from __future__ import annotations

from dataclasses import replace

import pytest

from hivemind.domain.access import TrustLevel
from hivemind.domain.entry import (
    EntryDraft,
    EntryFilters,
    Kind,
    Source,
    SourceType,
    embeddable_text,
)
from hivemind.domain.validation import (
    MAX_BODY_CHARS,
    MAX_LIMIT,
    MAX_OFFSET,
    MAX_REQUEST_BODY_BYTES,
    InvalidInput,
    validate_agent_name,
)
from hivemind.mcp.app import (
    hive_feedback,
    hive_get,
    hive_list,
    hive_register,
    hive_search,
    hive_withdraw,
    hive_write,
)
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.services.governance import WriteService
from hivemind.services.search import SearchService
from tests.fakes import make_clock, make_embedder, make_search_config
from tests.unit.test_api_endpoints import make_client, make_hivemind_app
from tests.unit.test_mcp_app import ALICE, ORG, build_app

KEY = {"X-API-Key": "key-alice"}
ADMIN = {"X-API-Key": "key-admin"}
ORG_KEY = {"X-API-Key": "key-org"}


def draft(**over: object) -> EntryDraft:
    base: dict[str, object] = {"kind": Kind.FACT, "summary": "s", "author": "a", "agent": "g"}
    return EntryDraft(**{**base, **over})  # type: ignore[arg-type]


class CountingEmbedder:
    """Wraps the fake embedder and counts calls."""

    dimension = 4
    model_name = "fake-embedder"

    def __init__(self) -> None:
        self.inner = make_embedder()
        self.calls = 0

    async def embed_text(self, text: str) -> list[float]:
        self.calls += 1
        return await self.inner.embed_text(text)

    async def embed_entry(self, d: EntryDraft) -> list[float]:
        self.calls += 1
        return await self.inner.embed_entry(d)


# -- the domain ---------------------------------------------------------------

BAD_DRAFTS = {
    "nul summary": {"summary": "a\x00b"},
    "nul body": {"body": "a\x00b"},
    "huge body": {"body": "x" * (MAX_BODY_CHARS + 1)},
    "nul tag": {"tags": ("a\x00",)},
    "empty tag": {"tags": ("",)},
    "blank tag": {"tags": ("  ",)},
    "long tag": {"tags": ("t" * 65,)},
    "many tags": {"tags": tuple(f"t{i}" for i in range(33))},
    "nul payload value": {"payload": {"a": "x\x00y"}},
    "nul payload nested key": {"payload": {"a": [{"k\x00": 1}]}},
    "nan payload": {"payload": {"a": float("nan")}},
    "huge payload": {"payload": {"a": "x" * 70_000}},
    "nul source ref": {"sources": (Source(SourceType.URL, "u\x00"),)},
    "many sources": {"sources": tuple(Source(SourceType.URL, f"u{i}") for i in range(33))},
    "many supersedes": {"supersedes": tuple(str(i) for i in range(17))},
    "nul supersedes": {"supersedes": ("a\x00",)},
    "nul agent": {"agent": "g\x00"},
}


@pytest.mark.parametrize("fields", BAD_DRAFTS.values(), ids=BAD_DRAFTS.keys())
def test_a_bad_draft_is_refused(fields: dict[str, object]) -> None:
    with pytest.raises(InvalidInput):
        draft(**fields)


def test_a_draft_at_the_caps_is_accepted() -> None:
    draft(
        body="x" * MAX_BODY_CHARS,
        tags=tuple("t" * 64 for _ in range(32)),
        payload={"a": "x" * 60_000},
    )


@pytest.mark.parametrize(
    "fields",
    [{"author": "a\x00"}, {"tags": ("a\x00",)}, {"entities": ("e\x00",)}, {"scope": "s\x00"}],
)
def test_a_filter_with_nul_is_refused(fields: dict[str, object]) -> None:
    with pytest.raises(InvalidInput):
        EntryFilters(**fields).validate()  # type: ignore[arg-type]


def test_the_embedded_text_is_bounded_by_characters_too() -> None:
    text = embeddable_text("s", "x" * 90_000)  # one "word", 90k characters
    assert len(text) < 25_000


# -- names --------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["alice", "Alice", "a", "john-claude-infra", "agent.v2_x", "A1", "9lives", "x" * 63]
)
def test_valid_agent_names_stay_valid(name: str) -> None:
    assert validate_agent_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "", " ", "  admin", "admin ", "ad min", "-x", ".x", "_x", "x/y", "x?y", "x#y", "x%41", "..",
        "a\x00b", "a\nb", "a​b", "\uff41\uff44\uff4d\uff49\uff4e", "\u0430dmin", "é", "x" * 64,
        "admin", "Admin", "ADMIN", "Org", "DEV", "Shared",
    ],
)  # fmt: skip
def test_invalid_or_reserved_agent_names_are_refused(name: str) -> None:
    with pytest.raises(InvalidInput):
        validate_agent_name(name)


async def test_register_enforces_the_name_rules_at_the_service_seam() -> None:
    service = AccessService(MemoryStore(make_clock()))
    for bad in ("Admin", "admin​", "", "a\x00", "x" * 200):
        with pytest.raises(InvalidInput):
            await service.register(bad, ORG)
    with pytest.raises(InvalidInput):
        await service.register("ok", ORG, owner_alias="a\x00")


async def test_fleet_names_reject_hidden_and_blank_text() -> None:
    service = AccessService(MemoryStore(make_clock()))
    admin = Credential(user_id="admin", is_admin=True)
    for bad in ("", "  ", "a\x00", "a​b", "a\nb", "x" * 129):
        with pytest.raises(InvalidInput):
            await service.create_fleet(bad, admin)
    assert (await service.create_fleet("Data Eng (EU)", admin)).name == "Data Eng (EU)"


# -- search: validated before the embedder -------------------------------------


async def test_search_refuses_bad_input_before_calling_the_embedder() -> None:
    embedder = CountingEmbedder()
    service = SearchService(MemoryStore(make_clock()), embedder, make_search_config())  # type: ignore[arg-type]
    for kwargs in (
        {"query": "a\x00"},
        {"query": "q" * 2001},
        {"query": "q", "limit": -1},
        {"query": "q", "limit": 0},
        {"query": "q", "limit": MAX_LIMIT + 1},
        {"query": "q", "offset": -1},
        {"query": "q", "offset": MAX_OFFSET + 1},
    ):
        with pytest.raises(InvalidInput):
            await service.search(**kwargs)  # type: ignore[arg-type]
    assert embedder.calls == 0


# -- REST -----------------------------------------------------------------------

ENTRY_BAD = {
    "nul summary": {"summary": "a\x00b"},
    "nul body": {"body": "a\x00b"},
    "huge body": {"body": "x" * (MAX_BODY_CHARS + 1)},
    "nul tag": {"tags": ["a\x00"]},
    "empty tag": {"tags": [""]},
    "many tags": {"tags": [f"t{i}" for i in range(33)]},
    "nul payload": {"payload": {"a": "x\x00"}},
    "nul source": {"sources": [{"type": "url", "ref": "u\x00"}]},
}


@pytest.mark.parametrize("fields", ENTRY_BAD.values(), ids=ENTRY_BAD.keys())
async def test_rest_write_refuses_bad_entries_with_422_and_no_embedding(
    fields: dict[str, object],
) -> None:
    app = make_hivemind_app()
    embedder = CountingEmbedder()
    app = replace(app, write_service=WriteService(app.store, embedder))  # type: ignore[arg-type]
    async with make_client(app) as client:
        resp = await client.post(
            "/v1/entries", json={"kind": "fact", "summary": "s", **fields}, headers=KEY
        )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "invalid_entry"
    assert embedder.calls == 0


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("POST", "/v1/search", {"query": "a\x00"}),
        ("POST", "/v1/search", {"query": "q", "tags": ["a\x00"]}),
        ("POST", "/v1/search", {"query": "q", "limit": -1}),
        ("POST", "/v1/search", {"query": "q", "limit": 0}),
        ("POST", "/v1/search", {"query": "q", "limit": MAX_LIMIT + 1}),
        ("POST", "/v1/search", {"query": "q", "offset": -1}),
        ("POST", "/v1/search", {"query": "q", "offset": 2**63}),
        ("GET", "/v1/entries?limit=101", None),
        ("GET", "/v1/entries?limit=99999999999999999999", None),
        ("GET", "/v1/entries?offset=10001", None),
        ("GET", "/v1/entries?offset=-1", None),
        ("GET", "/v1/entries?author=x%00y", None),
        ("GET", "/v1/entries?tags=a%00", None),
        ("GET", "/v1/entries?entities=a%00", None),
        ("GET", "/v1/entries?scope=a%00", None),
        ("GET", "/v1/admin/audit-log?actor=x%00", None),
        ("GET", "/v1/admin/audit-log?limit=1001", None),
        ("POST", "/v1/entries/some-id/withdraw", {"reason": "r\x00"}),
        ("POST", "/v1/entries/some-id/withdraw", {"reason": "r" * 2001}),
        ("POST", "/v1/entries/some-id/feedback", {"verdict": "helpful", "note": "n\x00"}),
        ("POST", "/v1/entries/some-id/feedback", {"verdict": "helpful", "note": "n" * 2001}),
        ("POST", "/v1/admin/agents/a%00b/activate", {"home_fleet_id": "f"}),
        ("POST", "/v1/admin/agents/x/activate", {"home_fleet_id": "f\x00"}),
        ("PATCH", "/v1/admin/agents/a%00b", {"trust_level": 1}),
        ("POST", "/v1/admin/agents/a%00b/revoke", None),
    ],
)
async def test_rest_refuses_bad_reads_and_governance_input_with_422(
    method: str, url: str, body: object
) -> None:
    async with make_client(make_hivemind_app()) as client:
        resp = await client.request(method, url, json=body, headers=ADMIN)
    assert resp.status_code == 422, resp.text


async def test_rest_paging_at_the_bounds_is_fine() -> None:
    async with make_client(make_hivemind_app()) as client:
        ok = await client.get(f"/v1/entries?limit={MAX_LIMIT}&offset={MAX_OFFSET}", headers=KEY)
        s = await client.post(
            "/v1/search", json={"query": "q", "limit": MAX_LIMIT, "offset": 0}, headers=KEY
        )
    assert ok.status_code == 200
    assert s.status_code == 200


async def test_rest_unknown_entry_id_with_nul_is_a_404_not_a_500() -> None:
    async with make_client(make_hivemind_app()) as client:
        resp = await client.get("/v1/entries/a%00b", headers=KEY)
    assert resp.status_code == 404


@pytest.mark.parametrize("name", ["Admin", "ADMIN", "admin​", "", "a\x00b", "x" * 100, "a/b"])
async def test_rest_register_refuses_bad_names_as_422_not_409(name: str) -> None:
    async with make_client(make_hivemind_app()) as client:
        resp = await client.post("/v1/agents", json={"name": name}, headers=ORG_KEY)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "invalid_input"


async def test_rest_register_still_accepts_a_good_name_and_409s_a_taken_one() -> None:
    app = make_hivemind_app()
    fleet = await app.store.create_fleet("f")
    async with make_client(app) as client:
        ok = await client.post("/v1/agents", json={"name": "bob"}, headers=ORG_KEY)
        assert ok.status_code == 201
        await app.store.activate_agent(
            "bob", trust_level=TrustLevel.PRIVILEGED, home_fleet_id=fleet.id
        )
        taken = await client.post("/v1/agents", json={"name": "bob"}, headers=ORG_KEY)
    assert taken.status_code == 409


async def test_rest_fleet_names_are_validated() -> None:
    async with make_client(make_hivemind_app()) as client:
        bad = await client.post("/v1/admin/fleets", json={"name": "a\x00"}, headers=ADMIN)
        ok = await client.post("/v1/admin/fleets", json={"name": "Data Eng"}, headers=ADMIN)
    assert bad.status_code == 422
    assert ok.status_code == 201


# -- REST: the body guard ---------------------------------------------------------


async def test_an_oversized_body_is_413_by_content_length() -> None:
    big = b'{"kind":"fact","summary":"s","body":"' + b"x" * MAX_REQUEST_BODY_BYTES + b'"}'
    async with make_client(make_hivemind_app()) as client:
        resp = await client.post(
            "/v1/entries", content=big, headers={**KEY, "Content-Type": "application/json"}
        )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"


async def test_a_streamed_oversized_body_is_413_without_content_length() -> None:
    async def chunks():
        for _ in range(MAX_REQUEST_BODY_BYTES // 65_536 + 2):
            yield b"x" * 65_536

    async with make_client(make_hivemind_app()) as client:
        resp = await client.post(
            "/v1/entries", content=chunks(), headers={**KEY, "Content-Type": "application/json"}
        )
    assert resp.status_code == 413, resp.text


async def test_no_key_wins_over_a_malformed_body() -> None:
    async with make_client(make_hivemind_app()) as client:
        resp = await client.post(
            "/v1/entries", content=b'{"kind":', headers={"Content-Type": "application/json"}
        )
        oversized = await client.post(
            "/v1/entries",
            content=b"x" * (MAX_REQUEST_BODY_BYTES + 1),
            headers={"Content-Type": "application/json"},
        )
        health = await client.get("/v1/health")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_api_key"
    assert oversized.status_code == 401
    assert health.status_code == 200


# -- MCP ------------------------------------------------------------------------


async def test_mcp_refuses_bad_input_with_invalid_input() -> None:
    app = build_app(MemoryStore(make_clock()), ALICE, make_clock())
    cases = [
        await hive_write(app, "fact", "a\x00b"),
        await hive_write(app, "fact", "s", body="x" * (MAX_BODY_CHARS + 1)),
        await hive_write(app, "fact", "s", tags=[""]),
        await hive_write(app, "fact", "s", payload={"a": "\x00"}),
        await hive_write(app, "fact", "s", sources=[{"type": "url", "ref": "u\x00"}]),
        await hive_search(app, "a\x00"),
        await hive_search(app, "q", limit=MAX_LIMIT + 1),
        await hive_search(app, "q", limit=-1),
        await hive_search(app, "q", offset=MAX_OFFSET + 1),
        await hive_search(app, "q", tags=["a\x00"]),
        await hive_list(app, limit=MAX_LIMIT + 1),
        await hive_list(app, limit=10**12),
        await hive_list(app, offset=10**30),
        await hive_list(app, author="a\x00"),
        await hive_withdraw(app, "some-id", reason="r\x00"),
        await hive_feedback(app, "some-id", "helpful", note="n" * 2001),
    ]
    for result in cases:
        assert result["error"]["code"] == "invalid_input", result  # type: ignore[index]


async def test_mcp_get_with_nul_id_is_not_found() -> None:
    app = build_app(MemoryStore(make_clock()), ALICE, make_clock())
    assert (await hive_get(app, "a\x00b"))["error"]["code"] == "not_found"  # type: ignore[index]


@pytest.mark.parametrize(
    "name", ["Admin", "admin ", "", "a\x00b", "x" * 100, "\uff41\uff44\uff4d\uff49\uff4e"]
)
async def test_mcp_register_reports_invalid_names_as_invalid_input(name: str) -> None:
    app = build_app(MemoryStore(make_clock()), ORG, make_clock())
    result = await hive_register(app, name)
    assert result["error"]["code"] == "invalid_input", result  # type: ignore[index]


async def test_mcp_register_keeps_name_conflict_for_taken_names() -> None:
    store = MemoryStore(make_clock())
    app = build_app(store, ORG, make_clock())
    assert (await hive_register(app, "bob"))["status"] == "pending"
    fleet = await store.create_fleet("f")
    await store.activate_agent("bob", trust_level=TrustLevel.PRIVILEGED, home_fleet_id=fleet.id)
    assert (await hive_register(app, "bob"))["error"]["code"] == "name_conflict"  # type: ignore[index]
