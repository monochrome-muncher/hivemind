"""The search similarity threshold (ADR 0062): config, and what it does to
a search through the service.

The vector stream keeps only entries above the threshold; the keyword
stream is untouched, so a search finds nothing only when neither stream
does, and that miss is counted (ADR 0056). Off (``None``) by default, and
off is today's behaviour: any positive similarity (ADR 0061).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from hivemind.config import SearchConfig, Settings
from hivemind.domain.access import TrustLevel, Visibility
from hivemind.domain.entry import EntryDraft, EntryFilters, Kind, SearchCount
from hivemind.memstore import MemoryStore
from hivemind.services.search import SearchService
from tests.fakes import FIXED_NOW, make_clock, make_search_config

# Unit vectors, so the cosine similarity to QUERY is the first component.
QUERY = [1.0, 0.0, 0.0, 0.0]
CLOSE = [0.9, 0.43589, 0.0, 0.0]  # similarity 0.9
LOOSE = [0.3, 0.95394, 0.0, 0.0]  # similarity 0.3


class _FixedEmbedder:
    """Embeds every query as ``QUERY``."""

    dimension = 4
    model_name = "fixed"

    async def embed_text(self, text: str) -> list[float]:
        return list(QUERY)

    async def embed_entry(self, draft: EntryDraft) -> list[float]:
        return list(QUERY)


async def _store() -> tuple[MemoryStore, dict[str, str]]:
    store = MemoryStore(make_clock())
    ids = {}
    for name, vector in (("close", CLOSE), ("loose", LOOSE)):
        draft = EntryDraft(kind=Kind.FACT, summary=f"{name} entry", author="a", agent="a")
        ids[name] = (await store.create_entry(draft, vector)).id
    return store, ids


def _service(store: MemoryStore, threshold: float | None) -> SearchService:
    config = replace(make_search_config(), vector_min_similarity=threshold)
    return SearchService(store, _FixedEmbedder(), config, now_fn=lambda: FIXED_NOW)


class TestConfig:
    def test_off_by_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HIVEMIND_VECTOR_MIN_SIMILARITY", raising=False)
        monkeypatch.chdir(tmp_path)
        assert SearchConfig().vector_min_similarity is None
        assert Settings().search_config().vector_min_similarity is None

    def test_an_operator_sets_it_through_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HIVEMIND_VECTOR_MIN_SIMILARITY", "0.45")
        assert Settings().search_config().vector_min_similarity == 0.45

    @pytest.mark.parametrize("spelling", ["", "none", "None", " NONE "])
    def test_empty_and_none_mean_off(
        self, spelling: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HIVEMIND_VECTOR_MIN_SIMILARITY", spelling)
        assert Settings().search_config().vector_min_similarity is None

    @pytest.mark.parametrize("bad", ["1", "1.5", "-0.1", "nan", "null"])
    def test_a_value_outside_zero_to_one_fails_at_startup(
        self, bad: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HIVEMIND_VECTOR_MIN_SIMILARITY", bad)
        with pytest.raises(ValidationError):
            Settings()

    @pytest.mark.parametrize("bad", [1.0, 1.5, -0.1, float("nan"), float("inf")])
    def test_search_config_rejects_the_same_values(self, bad: float) -> None:
        with pytest.raises(ValueError, match="vector_min_similarity"):
            SearchConfig(vector_min_similarity=bad)

    def test_zero_is_allowed_and_equals_off(self) -> None:
        assert SearchConfig(vector_min_similarity=0.0).vector_min_similarity == 0.0


class TestThroughTheService:
    async def test_off_keeps_every_positive_match(self) -> None:
        store, ids = await _store()
        hits = await _service(store, None).search("qqq")
        assert [h.entry_id for h in hits] == [ids["close"], ids["loose"]]

    async def test_zero_is_the_same_as_off(self) -> None:
        store, _ = await _store()
        off = [h.entry_id for h in await _service(store, None).search("qqq")]
        zero = [h.entry_id for h in await _service(store, 0.0).search("qqq")]
        assert off == zero

    async def test_the_threshold_drops_vector_matches_at_or_below_it(self) -> None:
        store, ids = await _store()
        hits = await _service(store, 0.5).search("qqq")
        assert [h.entry_id for h in hits] == [ids["close"]]
        assert await _service(store, 0.95).search("qqq") == []

    async def test_keyword_matches_still_come_back(self) -> None:
        """The threshold is on the vector stream only: an entry below it
        that shares a query word still reaches the results."""
        store, ids = await _store()
        hits = await _service(store, 0.95).search("loose")
        assert [h.entry_id for h in hits] == [ids["loose"]]

    async def test_a_search_emptied_by_the_threshold_counts_as_empty(self) -> None:
        store, _ = await _store()
        reader = Visibility(TrustLevel.CONTRIBUTOR, "reader")
        await _service(store, 0.5).search_result("qqq", visibility=reader)
        await _service(store, 0.95).search_result("qqq", visibility=reader)
        assert await store.search_counts() == [SearchCount(None, searches=2, empty=1)]

    async def test_the_threshold_is_strict(self) -> None:
        """At exactly the threshold an entry is left out, as at 0 (ADR 0061)."""
        store = MemoryStore(make_clock())
        draft = EntryDraft(kind=Kind.FACT, summary="edge", author="a", agent="a")
        await store.create_entry(draft, [0.5, 0.5, 0.5, 0.5])
        similarity = 0.5  # exactly: both vectors have norm 1
        assert await store.search_vector(QUERY, EntryFilters(), 5, min_similarity=similarity) == []
