"""The narrow importance factor at the service and the settings (ADR 0088)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from hivemind.config import SearchConfig, load_settings
from hivemind.domain.entry import EntryDraft, ImportanceSource, Kind
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.memstore import MemoryStore
from hivemind.services.search import SearchService
from tests.fakes import FIXED_NOW, make_clock, make_embedder

QUERY = "gamma rollout note"


def _draft(importance: int) -> EntryDraft:
    return EntryDraft(
        kind=Kind.FACT,
        summary=QUERY,
        author="alice",
        agent="agent-1",
        occurred_at=FIXED_NOW - timedelta(days=1),
        importance=importance,
        importance_source=ImportanceSource.CALLER,
    )


async def _pool() -> tuple[MemoryStore, str, str]:
    """Two equally relevant entries: importance 5 unrated, importance 3 with
    several helpful reports (quality at the 1.2 ceiling)."""
    store = MemoryStore(make_clock())
    vec = [0.5] * 4
    five = await store.create_entry(_draft(5), vec)
    three = await store.create_entry(_draft(3), vec)
    for i in range(30):
        await store.record_feedback(
            Feedback(three.id, f"rater-{i}", "a", Verdict.HELPFUL, note="re-checked")
        )
    return store, five.id, three.id


async def _order(store: MemoryStore, config: SearchConfig) -> list[str]:
    service = SearchService(store, make_embedder(), config, now_fn=lambda: FIXED_NOW)
    return [h.entry_id for h in await service.search(QUERY, limit=10)]


async def test_the_setting_flips_equally_relevant_importance_five_against_peer_confirmed() -> None:
    store, five, three = await _pool()
    assert await _order(store, SearchConfig(narrow_importance=False)) == [five, three]
    assert await _order(store, SearchConfig(narrow_importance=True)) == [three, five]


async def test_equal_importance_ranks_the_same_either_way() -> None:
    store = MemoryStore(make_clock())
    a = await store.create_entry(_draft(3), [0.5] * 4)
    b = await store.create_entry(_draft(3), [0.5] * 4)
    off = await _order(store, SearchConfig(narrow_importance=False))
    assert off == await _order(store, SearchConfig(narrow_importance=True))
    assert set(off) == {a.id, b.id}


def test_the_setting_is_on_by_default_and_reads_its_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    for name in list(os.environ):
        if name.startswith("HIVEMIND_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HIVEMIND_STRICT_ENV", "true")
    assert load_settings().search_config().narrow_importance is True
    monkeypatch.setenv("HIVEMIND_NARROW_IMPORTANCE", "false")
    assert load_settings().search_config().narrow_importance is False
