"""A confirmation newer than every flag halves those flags (ADR 0106), at
the service and the settings."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest

from hivemind.config import SearchConfig, load_settings
from hivemind.domain.entry import EntryDraft, Kind
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.retrieval.scoring import feedback_quality
from hivemind.services.feedback import FeedbackService
from hivemind.services.search import SearchService
from tests.fakes import FIXED_NOW, make_clock, make_embedder

ON = SearchConfig()
OFF = SearchConfig(confirmation_halves_flags=False)
# Before the store clock, so a report the service records is the newest.
T0 = FIXED_NOW - timedelta(days=30)


async def _reported(*reports: tuple[str, Verdict, str | None, int]) -> tuple[MemoryStore, str]:
    """One entry with the given (user, verdict, note, minute) reports."""
    store = MemoryStore(make_clock())
    draft = EntryDraft(kind=Kind.FACT, summary="billing runs on node 20", author="a", agent="a")
    entry = await store.create_entry(draft, [0.1] * 4)
    for user, verdict, note, minute in reports:
        await store.record_feedback(
            Feedback(entry.id, user, user, verdict, note, T0 + timedelta(minutes=minute))
        )
    return store, entry.id


async def _hit_score(store: MemoryStore, config: SearchConfig) -> float:
    service = SearchService(store, make_embedder(), config, now_fn=lambda: FIXED_NOW)
    [hit] = await service.search("billing node")
    return hit.score


async def test_a_noted_helpful_after_a_wrong_halves_it() -> None:
    confirmed, _ = await _reported(
        ("r1", Verdict.WRONG, "broke on 22", 1), ("r2", Verdict.HELPFUL, "ran it", 2)
    )
    bare, _ = await _reported(
        ("r1", Verdict.WRONG, "broke on 22", 1), ("r2", Verdict.HELPFUL, None, 2)
    )
    # 1 + (0.2 - 0.5) / (1.5 + 2) against 1 + (0.2 - 1.0) / (2 + 2)
    on, off = await _hit_score(confirmed, ON), await _hit_score(confirmed, OFF)
    assert on / off == pytest.approx((1 - 0.3 / 3.5) / 0.8)
    # A bare helpful is no confirmation: the flag counts in full either way.
    assert await _hit_score(bare, ON) == await _hit_score(bare, OFF) == off


async def test_a_newer_flag_restores_full_weight() -> None:
    store, _ = await _reported(
        ("r1", Verdict.WRONG, "broke on 22", 1),
        ("r2", Verdict.HELPFUL, "ran it", 2),
        ("r3", Verdict.STALE, None, 3),
    )
    assert await _hit_score(store, ON) == await _hit_score(store, OFF)


async def test_a_tie_is_no_confirmation() -> None:
    store, _ = await _reported(
        ("r1", Verdict.WRONG, "broke on 22", 1), ("r2", Verdict.HELPFUL, "ran it", 1)
    )
    assert await _hit_score(store, ON) == await _hit_score(store, OFF)


async def test_without_flags_nothing_changes() -> None:
    store, _ = await _reported(("r1", Verdict.HELPFUL, "ran it", 1))
    assert await _hit_score(store, ON) == await _hit_score(store, OFF)


async def test_the_feedback_reply_reports_the_quality_search_uses() -> None:
    store, entry_id = await _reported(("r1", Verdict.WRONG, "broke on 22", 1))
    service = FeedbackService(store, ON)
    reporter = Credential(user_id="r2", agent_id="a2")
    one = await service.record_feedback(reporter, entry_id, Verdict.HELPFUL, note="ran it")
    expected = feedback_quality(1, 0, 1, **ON.quality_kwargs(confirmed=True))
    assert one.quality == pytest.approx(expected)
    assert await service.quality(entry_id) == pytest.approx(expected)
    [many] = await service.record_feedback_many(
        reporter, [entry_id], Verdict.HELPFUL, note="ran it again"
    )
    assert many.quality == pytest.approx(expected)
    assert await FeedbackService(store, OFF).quality(entry_id) == pytest.approx(0.8)


def test_the_setting_is_on_by_default_and_reads_its_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in list(os.environ):
        if name.startswith("HIVEMIND_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HIVEMIND_STRICT_ENV", "true")
    assert load_settings().search_config().confirmation_halves_flags is True
    monkeypatch.setenv("HIVEMIND_CONFIRMATION_HALVES_FLAGS", "false")
    assert load_settings().search_config().confirmation_halves_flags is False
