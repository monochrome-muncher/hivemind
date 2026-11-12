"""Match strength on search hits (ADR 0148): which streams found a hit, its cosine similarity, and
the reply note when no hit is a close match."""

from __future__ import annotations

from dataclasses import replace

import pytest

from hivemind.config import SearchConfig
from hivemind.domain.entry import EntryDraft, Kind
from hivemind.embeddings import EmbeddingError
from hivemind.memstore import MemoryStore
from hivemind.ports import VectorMatch
from hivemind.services.search import SearchResult, SearchService
from tests.fakes import make_clock, make_embedder, make_search_config

CLOSE = 0.6


async def _entry(store: MemoryStore, summary: str) -> str:
    draft = EntryDraft(kind=Kind.FACT, summary=summary, author="alice", agent="a1")
    entry = await store.create_entry(draft, await make_embedder().embed_text(summary))
    return entry.id


def _streams(store: MemoryStore, keyword: list[str], vector: list[VectorMatch]) -> None:
    async def search_keyword(*args: object, **kwargs: object) -> list[str]:
        return keyword

    async def search_vector(*args: object, **kwargs: object) -> list[VectorMatch]:
        return vector

    store.search_keyword = search_keyword  # type: ignore[method-assign]
    store.search_vector = search_vector  # type: ignore[method-assign]


async def _search(
    store: MemoryStore, config: SearchConfig | None = None, embedder: object | None = None
) -> SearchResult:
    service = SearchService(
        store,
        embedder or make_embedder(),  # type: ignore[arg-type]
        config or make_search_config(close_match_similarity=CLOSE),
        now_fn=make_clock(),
    )
    return await service.search_result("anything")


async def test_hits_say_which_streams_found_them_and_carry_the_similarity() -> None:
    store = MemoryStore(make_clock())
    both, keyword_only, vector_only = [await _entry(store, s) for s in ("a b", "c d", "e f")]
    _streams(
        store,
        [both, keyword_only],
        [VectorMatch(both, 0.8149), VectorMatch(vector_only, 0.5)],
    )
    hits = {h.entry_id: h for h in (await _search(store)).hits}
    assert {i: h.match for i, h in hits.items()} == {
        both: "both",
        keyword_only: "keyword",
        vector_only: "vector",
    }
    assert hits[both].similarity == 0.81
    assert hits[vector_only].similarity == 0.5
    assert hits[keyword_only].similarity is None


async def test_a_degraded_search_has_keyword_matches_and_no_similarity() -> None:
    class Down:
        async def embed_text(self, text: str) -> list[float]:
            raise EmbeddingError("down")

    store = MemoryStore(make_clock())
    await _entry(store, "anything goes")
    result = await _search(store, embedder=Down())
    assert result.degraded
    assert [(h.match, h.similarity) for h in result.hits] == [("keyword", None)]
    assert not result.loose


async def test_no_close_hit_makes_the_result_loose() -> None:
    store = MemoryStore(make_clock())
    first, second = [await _entry(store, s) for s in ("a b", "c d")]
    _streams(store, [second], [VectorMatch(first, 0.59), VectorMatch(second, 0.3)])
    assert (await _search(store)).loose


async def test_the_bar_is_judged_on_the_similarity_as_shown() -> None:
    store = MemoryStore(make_clock())
    only = await _entry(store, "a b")
    _streams(store, [], [VectorMatch(only, 0.596)])
    result = await _search(store)
    assert result.hits[0].similarity == 0.6
    assert not result.loose

    _streams(store, [], [VectorMatch(only, 0.594)])
    result = await _search(store)
    assert result.hits[0].similarity == 0.59
    assert result.loose


async def test_one_close_hit_is_enough() -> None:
    store = MemoryStore(make_clock())
    first, second = [await _entry(store, s) for s in ("a b", "c d")]
    _streams(store, [], [VectorMatch(first, CLOSE), VectorMatch(second, 0.3)])
    assert not (await _search(store)).loose


async def test_keyword_only_hits_are_loose_in_a_healthy_search() -> None:
    store = MemoryStore(make_clock())
    only = await _entry(store, "a b")
    _streams(store, [only], [])
    assert (await _search(store)).loose


async def test_an_empty_result_is_not_loose() -> None:
    store = MemoryStore(make_clock())
    _streams(store, [], [])
    assert not (await _search(store)).loose


async def test_a_zero_bar_turns_the_note_off() -> None:
    store = MemoryStore(make_clock())
    only = await _entry(store, "a b")
    _streams(store, [], [VectorMatch(only, 0.1)])
    assert not (await _search(store, make_search_config(close_match_similarity=0.0))).loose


@pytest.mark.parametrize("bad", [-0.1, 1.0, float("nan")])
def test_the_close_match_bar_must_be_in_zero_to_one(bad: float) -> None:
    with pytest.raises(ValueError, match="close_match_similarity"):
        make_search_config(close_match_similarity=bad)


async def test_the_mcp_reply_carries_the_loose_note_but_not_beside_a_degraded_one() -> None:
    from hivemind.mcp.app import CLOSEST_LOOSE, DEGRADED_NOTE, LOOSE_NOTE, _search_reply
    from tests.unit.test_mcp_server import make_app

    app = make_app()
    loose = await _loose_result()
    reply = _search_reply(app, loose)
    assert (reply["closest"], reply["note"]) == (CLOSEST_LOOSE, LOOSE_NOTE)

    degraded = SearchResult(loose.hits, degraded=True, loose=False)
    reply = _search_reply(app, degraded)
    assert reply["note"] == DEGRADED_NOTE
    assert "closest" not in reply

    assert "closest" not in _search_reply(app, SearchResult(loose.hits))


async def _loose_result() -> SearchResult:
    store = MemoryStore(make_clock())
    only = await _entry(store, "a b")
    _streams(store, [], [VectorMatch(only, 0.2)])
    return await _search(store)


@pytest.mark.parametrize(
    "options",
    [
        {"version_group_min_similarity": 0.1},
        {"explore": True},
        {"limit": 1, "offset": 1},
    ],
    ids=["version grouping", "explore", "later page"],
)
async def test_match_and_similarity_survive_the_reordering_and_paging_options(
    options: dict[str, object],
) -> None:
    store = MemoryStore(make_clock())
    ids = [await _entry(store, s) for s in ("a b", "c d", "e f")]
    _streams(store, ids[:2], [VectorMatch(ids[1], 0.9), VectorMatch(ids[2], 0.7)])

    async def shown(config: SearchConfig, **kwargs: object) -> dict[str, tuple[str, float | None]]:
        service = SearchService(store, make_embedder(), config, now_fn=make_clock())
        result = await service.search_result("anything", **kwargs)  # type: ignore[arg-type]
        return {h.entry_id: (h.match, h.similarity) for h in result.hits}

    config = make_search_config(close_match_similarity=CLOSE)
    baseline = await shown(config)
    assert baseline == {ids[0]: ("keyword", None), ids[1]: ("both", 0.9), ids[2]: ("vector", 0.7)}

    config_option = {k: v for k, v in options.items() if k == "version_group_min_similarity"}
    call_options = {k: v for k, v in options.items() if k != "version_group_min_similarity"}
    varied = await shown(replace(config, **config_option), **call_options)
    assert varied and varied.items() <= baseline.items()
