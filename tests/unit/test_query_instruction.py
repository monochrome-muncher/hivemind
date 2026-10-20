"""The query instruction (ADR 0097): config, and where it does and does not
reach.

Set, the search query sent to the vector stream is embedded as
``Instruct: {task}\\nQuery:{query}``. The keyword stream keeps the raw query,
and nothing that embeds an entry (a write, its ``related`` entries) ever
carries the instruction. Off (empty) is today's behaviour.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from hivemind.config import SearchConfig, Settings
from hivemind.domain.access import TrustLevel, Visibility
from hivemind.domain.entry import EntryDraft, EntryFilters, Kind
from hivemind.domain.validation import MAX_QUERY_INSTRUCTION_CHARS
from hivemind.memstore import MemoryStore
from hivemind.services.wiring import build_services
from tests.fakes import FIXED_NOW, FakeEmbedder, make_clock, make_search_config

TASK = "Given an agent's task, find memory entries"
WRAPPED = f"Instruct: {TASK}\nQuery:deploy kaniko"


class RecordingEmbedder(FakeEmbedder):
    """Records every text embedded, by the call that embedded it."""

    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []
        self.entries: list[str] = []

    async def embed_text(self, text: str) -> list[float]:
        self.texts.append(text)
        return await super().embed_text(text)

    async def embed_entry(self, draft: EntryDraft) -> list[float]:
        self.entries.append(self.entry_embeddable_text(draft))
        return await super().embed_entry(draft)


class RecordingStore(MemoryStore):
    """Records the query text the keyword stream receives."""

    def __init__(self) -> None:
        super().__init__(make_clock())
        self.keyword_queries: list[str] = []

    async def search_keyword(  # type: ignore[no-untyped-def, override]
        self, query: str, *args, **kwargs
    ) -> list[str]:
        self.keyword_queries.append(query)
        return await super().search_keyword(query, *args, **kwargs)


def _services(instruction: str) -> tuple[RecordingStore, RecordingEmbedder, object]:
    store = RecordingStore()
    embedder = RecordingEmbedder()
    config = replace(make_search_config(), query_instruction=instruction)
    return store, embedder, build_services(store, embedder, config)


class TestConfig:
    def test_off_by_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HIVEMIND_QUERY_INSTRUCTION", raising=False)
        monkeypatch.chdir(tmp_path)
        assert SearchConfig().query_instruction == ""
        assert Settings().search_config().query_instruction == ""

    def test_an_operator_sets_it_through_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HIVEMIND_QUERY_INSTRUCTION", f"  {TASK} ")
        assert Settings().search_config().query_instruction == TASK

    def test_empty_means_off(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HIVEMIND_QUERY_INSTRUCTION", "  ")
        assert Settings().search_config().query_instruction == ""

    @pytest.mark.parametrize("bad", ["two\nlines", "carriage\rreturn", "x" * 501])
    def test_a_bad_value_fails_at_startup(
        self, bad: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HIVEMIND_QUERY_INSTRUCTION", bad)
        with pytest.raises(ValidationError):
            Settings()

    @pytest.mark.parametrize(
        "bad", ["a\nb", "a\rb", "a\x00b", "x" * (MAX_QUERY_INSTRUCTION_CHARS + 1)]
    )
    def test_search_config_rejects_the_same_values(self, bad: str) -> None:
        with pytest.raises(ValueError, match="query_instruction"):
            SearchConfig(query_instruction=bad)

    def test_the_longest_allowed_value_is_accepted(self) -> None:
        longest = "x" * MAX_QUERY_INSTRUCTION_CHARS
        assert SearchConfig(query_instruction=longest).query_instruction == longest

    def test_the_wrapped_query_has_no_space_after_the_colon(self) -> None:
        config = SearchConfig(query_instruction=TASK)
        assert config.embedded_query("q") == f"Instruct: {TASK}\nQuery:q"
        assert SearchConfig().embedded_query("q") == "q"


class TestThroughTheServices:
    async def test_the_vector_stream_gets_the_wrapped_query(self) -> None:
        _store, embedder, services = _services(TASK)
        await services.search_service.search("deploy kaniko")  # type: ignore[attr-defined]
        assert embedder.texts == [WRAPPED]

    async def test_unset_the_vector_stream_gets_the_raw_query(self) -> None:
        _store, embedder, services = _services("")
        await services.search_service.search("deploy kaniko")  # type: ignore[attr-defined]
        assert embedder.texts == ["deploy kaniko"]

    @pytest.mark.parametrize("instruction", ["", TASK])
    async def test_the_keyword_stream_always_gets_the_raw_query(self, instruction: str) -> None:
        store, _embedder, services = _services(instruction)
        await services.search_service.search("deploy kaniko")  # type: ignore[attr-defined]
        assert store.keyword_queries == ["deploy kaniko"]

    async def test_a_write_and_its_related_entries_never_carry_it(self) -> None:
        _store, embedder, services = _services(TASK)
        draft = EntryDraft(
            kind=Kind.FACT,
            summary="Deploys use kaniko",
            author="alice",
            agent="a",
            occurred_at=FIXED_NOW,
        )
        await services.write_service.write_and_relate(draft)  # type: ignore[attr-defined]
        second = replace(draft, summary="Deploys also use kaniko cache")
        written = await services.write_service.write_and_relate(second)  # type: ignore[attr-defined]
        assert written.related  # the nearest-entry lookup ran on the stored vector
        assert embedder.texts == []  # nothing went through the query path
        assert len(embedder.entries) == 2
        assert not any("Instruct:" in text for text in embedder.entries)

    async def test_explore_and_paging_searches_wrap_too(self) -> None:
        _store, embedder, services = _services(TASK)
        reader = Visibility(TrustLevel.CONTRIBUTOR, "reader")
        await services.search_service.search(  # type: ignore[attr-defined]
            "deploy kaniko", EntryFilters(), explore=True, visibility=reader
        )
        assert embedder.texts == [WRAPPED]
