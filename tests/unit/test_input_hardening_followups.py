"""Verifier follow-ups to ADR 0040: legacy names stay manageable, payload
depth, lone surrogates, datetime range, the exact body cap, probe paths,
fleet-name characters and the embedded-character ceiling."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from hivemind.domain.entry import (
    EntryDraft,
    EntryFilters,
    Kind,
    Source,
    SourceType,
    embeddable_text,
)
from hivemind.domain.validation import (
    MAX_REQUEST_BODY_BYTES,
    InvalidInput,
    validate_fleet_name,
)
from hivemind.mcp.app import hive_list, hive_search, hive_write
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.governance import WriteService
from tests.fakes import make_clock, make_embedder
from tests.unit.test_api_endpoints import make_client, make_hivemind_app
from tests.unit.test_mcp_app import ALICE, build_app

KEY = {"X-API-Key": "key-alice"}
ADMIN = {"X-API-Key": "key-admin"}
LEGACY = "L" * 300  # registered before ADR 0040, when names were unbounded


def draft(**over: object) -> EntryDraft:
    base: dict[str, object] = {"kind": Kind.FACT, "summary": "s", "author": "a", "agent": "g"}
    return EntryDraft(**{**base, **over})  # type: ignore[arg-type]


class CountingEmbedder:
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


# -- F1: a legacy name must stay manageable ------------------------------------


async def test_a_legacy_long_agent_name_does_not_break_metrics_or_admin_routes() -> None:
    app = make_hivemind_app()
    fleet = await app.store.create_fleet("f")
    await app.store.register_agent(LEGACY, None)
    async with make_client(app) as client:
        metrics = await client.get("/v1/metrics", headers=ADMIN)
        activate = await client.post(
            f"/v1/admin/agents/{LEGACY}/activate",
            json={"trust_level": 1, "home_fleet_id": fleet.id},
            headers=ADMIN,
        )
        patch = await client.patch(
            f"/v1/admin/agents/{LEGACY}", json={"trust_level": 2}, headers=ADMIN
        )
        revoke = await client.post(f"/v1/admin/agents/{LEGACY}/revoke", headers=ADMIN)
        metrics_after = await client.get("/v1/metrics", headers=ADMIN)
    assert metrics.status_code == 200, metrics.text
    assert activate.status_code == patch.status_code == revoke.status_code == 200
    assert metrics_after.status_code == 200


async def test_a_legacy_long_author_can_still_write() -> None:
    app = make_hivemind_app()
    app.authenticator.add_key(  # type: ignore[attr-defined]
        "key-legacy", Credential(user_id=LEGACY, agent_id="g", agent_name=LEGACY)
    )
    async with make_client(app) as client:
        resp = await client.post(
            "/v1/entries",
            json={"kind": "fact", "summary": "s"},
            headers={"X-API-Key": "key-legacy"},
        )
    assert resp.status_code == 201, resp.text


# -- F2: payload depth -----------------------------------------------------------


def _nested(depth: int) -> dict:
    node: dict = {"leaf": 1}
    for _ in range(depth):
        node = {"n": node}
    return node


def test_payload_nesting_is_capped_and_compact_size_is_measured() -> None:
    draft(payload=_nested(30))
    with pytest.raises(InvalidInput):
        draft(payload=_nested(40))
    with pytest.raises(InvalidInput):
        draft(payload=_nested(300))
    draft(payload={"a": "x" * (65_536 - 8)})  # {"a":"..."} is 8 bytes of overhead
    with pytest.raises(InvalidInput):
        draft(payload={"a": "x" * (65_536 - 7)})


async def test_a_deep_payload_is_refused_on_rest_and_mcp_before_embedding() -> None:
    app = make_hivemind_app()
    embedder = CountingEmbedder()
    app = replace(app, write_service=WriteService(app.store, embedder))  # type: ignore[arg-type]
    async with make_client(app) as client:
        resp = await client.post(
            "/v1/entries",
            json={"kind": "fact", "summary": "s", "payload": _nested(300)},
            headers=KEY,
        )
        listing = await client.get("/v1/entries", headers=KEY)
    assert resp.status_code == 422
    assert embedder.calls == 0
    assert listing.status_code == 200
    mcp = build_app(MemoryStore(make_clock()), ALICE, make_clock())
    result = await hive_write(mcp, "fact", "s", payload=_nested(300))
    assert result["error"]["code"] == "invalid_input"  # type: ignore[index]


# -- F3: lone surrogates -----------------------------------------------------------


@pytest.mark.parametrize("bad", ["a\ud800b", "\udfff"])
def test_lone_surrogates_are_refused_like_nul(bad: str) -> None:
    with pytest.raises(InvalidInput):
        draft(tags=(bad,))
    with pytest.raises(InvalidInput):
        draft(summary=bad)
    with pytest.raises(InvalidInput):
        draft(payload={"a": bad})
    with pytest.raises(InvalidInput):
        draft(sources=(Source(SourceType.URL, bad),))
    with pytest.raises(InvalidInput):
        EntryFilters(author=bad).validate()


# -- F4: datetime range --------------------------------------------------------------

BAD_DATES = ["0001-01-01T00:00:00+23:00", "0001-01-01T00:00:00", "9999-12-31T23:59:59-23:00"]


@pytest.mark.parametrize("value", BAD_DATES)
async def test_out_of_range_datetimes_are_422_on_rest_and_invalid_input_on_mcp(value: str) -> None:
    async with make_client(make_hivemind_app()) as client:
        listing = await client.get("/v1/entries", params={"created_from": value}, headers=KEY)
        search = await client.post(
            "/v1/search", json={"query": "q", "occurred_to": value}, headers=KEY
        )
        write = await client.post(
            "/v1/entries",
            json={"kind": "fact", "summary": "s", "occurred_at": value},
            headers=KEY,
        )
    assert listing.status_code == search.status_code == write.status_code == 422
    mcp = build_app(MemoryStore(make_clock()), ALICE, make_clock())
    for result in (
        await hive_search(mcp, "q", occurred_from=value),
        await hive_list(mcp, created_to=value),
        await hive_write(mcp, "fact", "s", occurred_at=value),
    ):
        assert result["error"]["code"] == "invalid_input", result  # type: ignore[index]


def test_sane_datetimes_are_accepted() -> None:
    EntryFilters(
        occurred_from=datetime(1999, 1, 1, tzinfo=UTC), created_to=datetime(2100, 1, 1)
    ).validate()
    draft(occurred_at=datetime(1950, 6, 1, tzinfo=UTC))


# -- F5 / F6: the body cap and the probes ---------------------------------------------

_PAD_JSON = b'{"kind":"fact","summary":"s"}'


async def test_a_body_of_exactly_the_cap_is_accepted_and_one_more_byte_is_not() -> None:
    exact = _PAD_JSON + b" " * (MAX_REQUEST_BODY_BYTES - len(_PAD_JSON))
    headers = {**KEY, "Content-Type": "application/json"}

    async def chunked(data: bytes):
        for i in range(0, len(data), 65_536):
            yield data[i : i + 65_536]

    async with make_client(make_hivemind_app()) as client:
        ok = await client.post("/v1/entries", content=exact, headers=headers)
        ok_chunked = await client.post("/v1/entries", content=chunked(exact), headers=headers)
        over = await client.post("/v1/entries", content=exact + b" ", headers=headers)
        over_chunked = await client.post(
            "/v1/entries", content=chunked(exact + b" "), headers=headers
        )
    assert ok.status_code == ok_chunked.status_code == 201, (ok.text, ok_chunked.text)
    assert over.status_code == over_chunked.status_code == 413


async def test_probes_need_no_key_with_or_without_a_trailing_slash() -> None:
    async with make_client(make_hivemind_app()) as client:
        assert (await client.get("/v1/liveness")).status_code == 200
        assert (await client.get("/v1/health")).status_code == 200
        for path in ("/v1/health/", "/v1/liveness/"):
            assert (await client.get(path)).status_code != 401  # Starlette's redirect, as before


# -- F7: fleet-name characters --------------------------------------------------------


@pytest.mark.parametrize("name", ["\u0645\u06cc\u200c\u062e\u0648", "a\u200db", "Data Eng"])
def test_zwnj_and_zwj_are_allowed_in_fleet_names(name: str) -> None:
    assert validate_fleet_name(name) == name


@pytest.mark.parametrize("name", ["a​b", "a‮b", "a⁦b", "a⁠b", "﻿a", "a‎b"])
def test_invisible_and_bidi_characters_are_refused_in_fleet_names(name: str) -> None:
    with pytest.raises(InvalidInput):
        validate_fleet_name(name)


# -- F8: the embedded-character ceiling scales with the knob ----------------------------


def test_the_embedded_char_ceiling_scales_with_the_prefix_token_knob() -> None:
    body = "x" * 45_000  # one whitespace-free "word"
    assert len(embeddable_text("s", body)) < 25_000
    assert len(embeddable_text("s", body, prefix_tokens=5_000)) > 44_000


async def test_a_surrogate_in_a_constrained_field_is_a_422_without_an_input_echo() -> None:
    body = b'{"kind":"fact","summary":"s","sources":[{"type":"url","ref":"\\udc00"}]}'
    async with make_client(make_hivemind_app()) as client:
        resp = await client.post(
            "/v1/entries", content=body, headers={**KEY, "Content-Type": "application/json"}
        )
    assert resp.status_code == 422
    assert all("input" not in error for error in resp.json()["detail"])
