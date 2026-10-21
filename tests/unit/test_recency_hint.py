"""The per-search recency hint (ADR 0073) and the invariants under each hint.

The hint is on unless the server setting ``recency_hint_enabled`` is turned
off (ADR 0101). Off, every hint scores as ``default``, bit for bit. On, ``ignore_age`` drops
the recency factor and the confirmation clock (so I1 does not hold under it,
by construction), and ``prefer_recent`` scores under a lower floor (so I1
holds, with more room).
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

from hivemind.config import PREFER_RECENT_FLOOR, SearchConfig
from hivemind.domain.entry import EntryDraft, Kind
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.domain.recency import RecencyHint, parse_recency_hint
from hivemind.mcp.app import McpHivemind, hive_search
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.retrieval.scoring import entry_score
from hivemind.services.access import AccessService
from hivemind.services.governance import GovernanceService, WriteService
from hivemind.services.read import ReadService
from hivemind.services.search import SearchService
from tests.fakes import FIXED_NOW, make_clock, make_embedder, make_search_config
from tests.unit.test_api_endpoints import make_authenticator, make_hivemind_app, post_entry

NOW = FIXED_NOW
OFF = SearchConfig(recency_hint_enabled=False)
ON = SearchConfig(recency_hint_enabled=True)
HINTS = tuple(RecencyHint)
NON_DEFAULT = (RecencyHint.PREFER_RECENT, RecencyHint.IGNORE_AGE)
QUERY = "gamma rollout note"


def _draft(summary: str, age_days: float, **kwargs: object) -> EntryDraft:
    return EntryDraft(
        kind=kwargs.pop("kind", Kind.FACT),  # type: ignore[arg-type]
        summary=summary,
        author=str(kwargs.pop("author", "alice")),
        agent=str(kwargs.pop("agent", "agent-1")),
        occurred_at=NOW - timedelta(days=age_days),
        **kwargs,  # type: ignore[arg-type]
    )


def _service(store: MemoryStore, config: SearchConfig) -> SearchService:
    return SearchService(store, make_embedder(), config, now_fn=lambda: NOW)


async def _pool() -> MemoryStore:
    """Entries of every kind and age with feedback and confirmations, so a
    hint that leaked into the scoring would show."""
    store = MemoryStore(make_clock())
    ids = []
    for i, (kind, age) in enumerate(
        [
            (Kind.FACT, 1),
            (Kind.INSIGHT, 12),
            (Kind.DECISION, 40),
            (Kind.FACT, 90),
            (Kind.INSIGHT, 250),
            (Kind.DECISION, 700),
        ]
    ):
        summary = f"{QUERY} variant {i}" if i % 2 else f"{QUERY} {'extra ' * i}words"
        entry = await store.create_entry(_draft(summary, age, kind=kind), [0.1 * (i + 1)] * 4)
        ids.append(entry.id)
    for i in (2, 3, 4):
        await store.record_feedback(
            Feedback(
                ids[i],
                f"rater-{i}",
                "a",
                Verdict.HELPFUL,
                note="re-checked",
                updated_at=NOW - timedelta(days=i),
            )
        )
    await store.record_feedback(Feedback(ids[5], "r", "a", Verdict.STALE, note="old"))
    return store


# --- the hint itself --------------------------------------------------------


def test_parse_recency_hint() -> None:
    assert parse_recency_hint(None) is RecencyHint.DEFAULT
    assert parse_recency_hint("prefer_recent") is RecencyHint.PREFER_RECENT
    assert parse_recency_hint("ignore_age") is RecencyHint.IGNORE_AGE
    for bad in ("", "Default", "recent", "none"):
        with pytest.raises(ValueError, match="unknown recency"):
            parse_recency_hint(bad)


def test_the_setting_is_on_by_default() -> None:
    assert SearchConfig().recency_hint_enabled is True


@pytest.mark.parametrize("hint", HINTS)
def test_off_every_hint_scores_as_the_default(hint: RecencyHint) -> None:
    for kind in Kind:
        assert OFF.recency_terms(kind, hint) == OFF.recency_terms(kind, RecencyHint.DEFAULT)
    assert OFF.applied_hint(hint) is RecencyHint.DEFAULT


def test_on_the_default_hint_is_todays_terms() -> None:
    config = replace(ON, recency_by_kind=((Kind.INSIGHT, 180.0),), recency_from_confirmation=True)
    for kind in Kind:
        terms = config.recency_terms(kind, RecencyHint.DEFAULT)
        assert terms.half_life_days == config.half_life_for(kind)
        assert terms.recency_floor == config.recency_floor
        assert terms.use_confirmation is True
    assert config.applied_hint(RecencyHint.DEFAULT) is RecencyHint.DEFAULT


def test_prefer_recent_keeps_the_kind_half_life_and_lowers_the_floor() -> None:
    config = replace(ON, recency_by_kind=((Kind.DECISION, 365.0),), recency_from_confirmation=True)
    decision = config.recency_terms(Kind.DECISION, RecencyHint.PREFER_RECENT)
    fact = config.recency_terms(Kind.FACT, RecencyHint.PREFER_RECENT)
    assert (decision.half_life_days, fact.half_life_days) == (365.0, 30.0)
    assert decision.recency_floor == fact.recency_floor == PREFER_RECENT_FLOOR == 0.5
    assert decision.use_confirmation is True
    # Never weaker than the deployment's own floor.
    low = replace(ON, recency_floor=0.3)
    assert low.recency_terms(Kind.FACT, RecencyHint.PREFER_RECENT).recency_floor == 0.3
    none = replace(ON, recency_floor=None)
    assert none.recency_terms(Kind.FACT, RecencyHint.PREFER_RECENT).recency_floor is None
    assert (
        replace(ON, prefer_recent_floor=None)
        .recency_terms(Kind.FACT, RecencyHint.PREFER_RECENT)
        .recency_floor
        is None
    )


def test_ignore_age_has_no_recency_factor_and_no_confirmation_clock() -> None:
    config = replace(ON, recency_from_confirmation=True)
    terms = config.recency_terms(Kind.FACT, RecencyHint.IGNORE_AGE)
    assert terms.recency_floor is None and terms.use_confirmation is False
    for age in (0.0, 1.0, 400.0, 36_500.0):
        score = entry_score(
            0.5,
            3,
            NOW - timedelta(days=age),
            1.0,
            NOW,
            terms.half_life_days,
            recency_floor=terms.recency_floor,
            social_max=1.2,
        )
        assert score == 0.5 * 0.8  # fused x importance, exactly


def test_a_prefer_recent_floor_outside_the_range_fails_at_config_time() -> None:
    for bad in (0.0, -0.1, 1.1, math.nan):
        with pytest.raises(ValueError, match="prefer_recent_floor"):
            SearchConfig(prefer_recent_floor=bad)


# --- the service ------------------------------------------------------------


async def _ranking(
    store: MemoryStore, config: SearchConfig, hint: RecencyHint, **kwargs: int
) -> list[tuple[str, float]]:
    hits = await _service(store, config).search(QUERY, limit=10, recency=hint, **kwargs)
    return [(h.entry_id, h.score) for h in hits]


@pytest.mark.parametrize("hint", HINTS)
@pytest.mark.parametrize(
    "config",
    [
        OFF,
        replace(OFF, recency_from_confirmation=True),
        replace(OFF, recency_by_kind=((Kind.INSIGHT, 180.0), (Kind.DECISION, 365.0))),
    ],
    ids=["plain", "confirmation", "profile"],
)
async def test_setting_off_no_hint_changes_a_search(
    config: SearchConfig, hint: RecencyHint
) -> None:
    """The hint cannot change a search while the setting is off: hits, order
    and scores are exactly the default's, and the result says default."""
    store = await _pool()
    service = _service(store, config)
    base = await service.search_result(QUERY, limit=10)
    asked = await service.search_result(QUERY, limit=10, recency=hint)
    assert asked == base
    assert asked.recency is RecencyHint.DEFAULT
    assert len(base.hits) == 6


async def test_the_default_hint_is_bit_identical_with_the_setting_on_or_off() -> None:
    """No hint, or ``default``, scores the same whether the setting is on."""
    store = await _pool()
    off = await _service(store, OFF).search_result(QUERY, limit=10)
    on = await _service(store, ON).search_result(QUERY, limit=10, recency=RecencyHint.DEFAULT)
    assert on == off


async def test_a_hinted_search_leaves_the_next_default_search_alone() -> None:
    """The hint is an argument of one call: it is not kept on the service or
    the config, so another caller's search is unaffected."""
    store = await _pool()
    service = _service(store, ON)
    before = await service.search_result(QUERY, limit=10)
    for hint in NON_DEFAULT:
        await service.search_result(QUERY, limit=10, recency=hint)
        assert await service.search_result(QUERY, limit=10) == before
    assert service._config == ON


async def test_the_hints_change_scores_when_the_setting_is_on() -> None:
    store = await _pool()
    default = await _ranking(store, ON, RecencyHint.DEFAULT)
    prefer = await _ranking(store, ON, RecencyHint.PREFER_RECENT)
    ignore = await _ranking(store, ON, RecencyHint.IGNORE_AGE)
    assert len({tuple(s for _, s in r) for r in (default, prefer, ignore)}) == 3
    assert {i for i, _ in default} == {i for i, _ in prefer} == {i for i, _ in ignore}


@pytest.mark.parametrize("hint", HINTS)
async def test_pages_of_one_hint_add_up_to_the_whole_ranking(hint: RecencyHint) -> None:
    """Pagination: the same hint on every page gives one consistent order."""
    store = await _pool()
    whole = [i for i, _ in await _ranking(store, ON, hint)]
    paged: list[str] = []
    for offset in range(0, 6, 2):
        hits = await _service(store, ON).search(QUERY, limit=2, offset=offset, recency=hint)
        paged += [h.entry_id for h in hits]
    assert paged == whole


async def test_i1_old_never_beats_new_by_default_even_with_the_setting_on() -> None:
    """I1: the default hint, setting on, a 400-day entry with 200 helpful
    votes and a confirmation does not outrank a 1-day unrated one."""
    store = MemoryStore(make_clock())
    old = await store.create_entry(_draft(QUERY, 400), [0.1] * 4)
    new = await store.create_entry(_draft(QUERY, 1), [0.1] * 4)
    for i in range(200):
        await store.record_feedback(
            Feedback(
                old.id,
                f"r{i}",
                "a",
                Verdict.HELPFUL,
                note="ok",
                updated_at=NOW - timedelta(hours=1),
            )
        )
    config = replace(ON, recency_from_confirmation=True)
    for hint in (None, RecencyHint.DEFAULT):
        kw = {} if hint is None else {"recency": hint}
        hits = await _service(store, config).search(QUERY, limit=5, **kw)  # type: ignore[arg-type]
        assert [h.entry_id for h in hits] == [new.id, old.id]


async def test_i1_holds_under_prefer_recent() -> None:
    store = MemoryStore(make_clock())
    old = await store.create_entry(_draft(QUERY, 400), [0.1] * 4)
    new = await store.create_entry(_draft(QUERY, 1), [0.1] * 4)
    for i in range(200):
        await store.record_feedback(
            Feedback(
                old.id,
                f"r{i}",
                "a",
                Verdict.HELPFUL,
                note="ok",
                updated_at=NOW - timedelta(hours=1),
            )
        )
    config = replace(ON, recency_from_confirmation=True)
    hits = await _service(store, config).search(QUERY, limit=5, recency=RecencyHint.PREFER_RECENT)
    assert [h.entry_id for h in hits] == [new.id, old.id]


@pytest.mark.parametrize("fresh_age", (0.0, 0.5, 1.0, 1.69))
@pytest.mark.parametrize("old_age", (10.0, 30.0, 400.0))
@pytest.mark.parametrize("helpful", (0, 5, 1000))
def test_i1_arithmetic_under_prefer_recent(fresh_age: float, old_age: float, helpful: int) -> None:
    """I1 at equal match and importance, through the terms the service uses."""
    from hivemind.retrieval.scoring import feedback_quality

    terms = ON.recency_terms(Kind.FACT, RecencyHint.PREFER_RECENT)

    def score(age: float, helpful_votes: int) -> float:
        return entry_score(
            1.0,
            3,
            NOW - timedelta(days=age),
            feedback_quality(helpful_votes, 0, 0),
            NOW,
            terms.half_life_days,
            recency_floor=terms.recency_floor,
            social_max=ON.social_max,
            confirmed_at=NOW - timedelta(minutes=1) if helpful else None,
        )

    assert score(fresh_age, 0) >= score(old_age, helpful)


async def test_i1_is_given_up_under_ignore_age_only_with_the_setting_and_the_hint() -> None:
    """Age is ignored on the caller's explicit request: a well-rated old entry
    outranks a newer unrated one. Without the setting, or without the hint,
    the new one still wins."""
    store = MemoryStore(make_clock())
    old = await store.create_entry(_draft(QUERY, 400), [0.1] * 4)
    new = await store.create_entry(_draft(QUERY, 1), [0.1] * 4)
    for i in range(10):
        await store.record_feedback(Feedback(old.id, f"r{i}", "a", Verdict.HELPFUL, note="ok"))
    ignore = RecencyHint.IGNORE_AGE
    reached = [
        (ON, ignore),  # the only combination that does
        (ON, RecencyHint.DEFAULT),
        (ON, RecencyHint.PREFER_RECENT),
        (OFF, ignore),
        (OFF, RecencyHint.DEFAULT),
    ]
    firsts = {}
    for config, hint in reached:
        hits = await _service(store, config).search(QUERY, limit=5, recency=hint)
        firsts[(config.recency_hint_enabled, hint)] = hits[0].entry_id
    assert firsts[(True, ignore)] == old.id
    assert {k: v for k, v in firsts.items() if k != (True, ignore)} == {
        (True, RecencyHint.DEFAULT): new.id,
        (True, RecencyHint.PREFER_RECENT): new.id,
        (False, ignore): new.id,
        (False, RecencyHint.DEFAULT): new.id,
    }


async def test_ignore_age_ranks_by_match_alone_when_importance_and_social_are_equal() -> None:
    store = MemoryStore(make_clock())
    old = await store.create_entry(_draft(QUERY, 700), [0.1] * 4)
    new = await store.create_entry(_draft(QUERY, 1), [0.1] * 4)
    hits = await _service(store, ON).search(QUERY, limit=5, recency=RecencyHint.IGNORE_AGE)
    by_id = {h.entry_id: h.score for h in hits}
    assert by_id[old.id] == by_id[new.id]


@pytest.mark.parametrize("hint", HINTS)
async def test_i2_authors_agents_and_raters_do_not_reach_the_score(hint: RecencyHint) -> None:
    """I2 under each hint: two pools that differ only in who wrote and rated
    score alike."""

    async def scores(author: str, agent: str, rater: str) -> list[float]:
        clock = make_clock()
        store = MemoryStore(clock)
        for i in range(3):
            entry = await store.create_entry(
                _draft(f"{QUERY} number {i}" + " pad" * i, 20 * i + 1, author=author, agent=agent),
                [0.1 + 0.2 * i, 0.3, 0.2, 0.1],
            )
            clock.advance_days(1.0 / 86_400.0)  # created_at breaks keyword ties, not the id
            await store.record_feedback(
                Feedback(entry.id, rater, "a", Verdict.HELPFUL, note="ok", updated_at=NOW)
            )
        return [s for _, s in await _ranking(store, ON, hint)]

    assert await scores("alice", "agent-1", "r1") == await scores("zed", "bot-9", "q7")


@pytest.mark.parametrize("hint", HINTS)
async def test_i5_the_social_cap_holds_under_every_hint(hint: RecencyHint) -> None:
    """I5: 1000 helpful votes and a fresh confirmation lift an entry by at
    most ``social_max`` over the same entry unrated, under each hint."""

    async def score(*, rated: bool) -> float:
        store = MemoryStore(make_clock())
        entry = await store.create_entry(_draft(QUERY, 300), [0.1] * 4)
        if rated:
            for i in range(1000):
                await store.record_feedback(
                    Feedback(
                        entry.id,
                        f"r{i}",
                        "a",
                        Verdict.HELPFUL,
                        note="ok",
                        updated_at=NOW - timedelta(hours=1),
                    )
                )
        config = replace(ON, recency_from_confirmation=True)
        return (await _service(store, config).search(QUERY, limit=1, recency=hint))[0].score

    assert await score(rated=True) / await score(rated=False) <= ON.social_max + 1e-12


async def test_i3_and_i4_with_prefer_recent_and_ignore_age() -> None:
    """The confirmation clock works under prefer_recent (a flag newer than it
    cancels it, I4) and does not exist under ignore_age."""

    async def score(hint: RecencyHint, *, clock: bool, flag: bool) -> float:
        store = MemoryStore(make_clock())
        entry = await store.create_entry(_draft(QUERY, 730), [0.1] * 4)
        await store.record_feedback(
            Feedback(
                entry.id, "c", "a", Verdict.HELPFUL, note="ok", updated_at=NOW - timedelta(days=2)
            )
        )
        if flag:
            await store.record_feedback(
                Feedback(
                    entry.id, "f", "a", Verdict.STALE, note="no", updated_at=NOW - timedelta(days=1)
                )
            )
        config = replace(ON, recency_from_confirmation=clock, social_max=3.0)
        return (await _service(store, config).search(QUERY, limit=1, recency=hint))[0].score

    prefer, ignore = RecencyHint.PREFER_RECENT, RecencyHint.IGNORE_AGE
    assert await score(prefer, clock=True, flag=False) > await score(
        prefer, clock=False, flag=False
    )
    assert await score(prefer, clock=True, flag=True) == await score(prefer, clock=False, flag=True)
    assert await score(ignore, clock=True, flag=False) == await score(
        ignore, clock=False, flag=False
    )


# --- the surfaces -----------------------------------------------------------


def _api(enabled: bool) -> tuple[object, httpx.AsyncClient]:
    from hivemind.api.deps import create_app
    from hivemind.api.main import create_app_for_config
    from hivemind.config import Settings

    app = create_app_for_config(
        Settings(),
        store=MemoryStore(make_clock()),
        embedder=make_embedder(),
        authenticator=make_authenticator(),
        search_config=replace(make_search_config(), recency_hint_enabled=enabled),
    )
    transport = httpx.ASGITransport(app=create_app(app))
    return app, httpx.AsyncClient(transport=transport, base_url="http://hivemind.test")


async def test_rest_null_recency_is_the_default() -> None:
    _, client = _api(True)
    headers = {"X-API-Key": "key-alice"}
    async with client:
        await post_entry(client, "key-alice", QUERY, agent="a")
        resp = await client.post(
            "/v1/search", json={"query": QUERY, "recency": None}, headers=headers
        )
    assert resp.status_code == 200 and "X-Hivemind-Recency" not in resp.headers


@pytest.mark.parametrize("enabled", [False, True])
async def test_rest_rejects_an_unknown_recency_either_way(enabled: bool) -> None:
    _, client = _api(enabled)
    async with client:
        resp = await client.post(
            "/v1/search",
            json={"query": QUERY, "recency": "latest"},
            headers={"X-API-Key": "key-alice"},
        )
    assert resp.status_code == 422


async def test_rest_setting_off_ignores_the_hint_and_sets_no_header() -> None:
    _, client = _api(False)
    headers = {"X-API-Key": "key-alice"}
    async with client:
        await post_entry(client, "key-alice", QUERY, agent="a")
        plain = await client.post("/v1/search", json={"query": QUERY}, headers=headers)
        bodies = []
        for hint in HINTS:
            resp = await client.post(
                "/v1/search", json={"query": QUERY, "recency": hint.value}, headers=headers
            )
            assert resp.status_code == 200 and "X-Hivemind-Recency" not in resp.headers
            bodies.append(resp.json())

    # The app reads the real clock, so scores drift by a hair between calls.
    def shape(body: list[dict]) -> list[dict]:
        return [{k: v for k, v in hit.items() if k != "score"} for hit in body]

    for body in bodies:
        assert shape(body) == shape(plain.json())
        assert [h["score"] for h in body] == pytest.approx([h["score"] for h in plain.json()])


async def test_rest_setting_on_names_the_applied_hint_in_a_header() -> None:
    _, client = _api(True)
    headers = {"X-API-Key": "key-alice"}
    async with client:
        await post_entry(client, "key-alice", QUERY, agent="a")
        seen = {}
        for hint in HINTS:
            resp = await client.post(
                "/v1/search", json={"query": QUERY, "recency": hint.value}, headers=headers
            )
            assert resp.status_code == 200
            seen[hint] = resp.headers.get("X-Hivemind-Recency")
        absent = await client.post("/v1/search", json={"query": QUERY}, headers=headers)
    assert seen == {
        RecencyHint.DEFAULT: None,
        RecencyHint.PREFER_RECENT: "prefer_recent",
        RecencyHint.IGNORE_AGE: "ignore_age",
    }
    assert "X-Hivemind-Recency" not in absent.headers


def _mcp(enabled: bool, store: MemoryStore) -> McpHivemind:
    embedder = make_embedder()
    config = replace(make_search_config(), recency_hint_enabled=enabled)
    return McpHivemind(
        store=store,
        write_service=WriteService(store, embedder),
        search_service=SearchService(store, embedder, config, now_fn=lambda: NOW),
        governance_service=GovernanceService(store),
        read_service=ReadService(store, GovernanceService(store), config),
        access_service=AccessService(store),
        search_config=config,
        credential=Credential(user_id="alice", agent_id="agent-1"),
    )


@pytest.mark.parametrize("enabled", [False, True])
async def test_mcp_rejects_an_unknown_recency_either_way(enabled: bool) -> None:
    app = _mcp(enabled, MemoryStore(make_clock()))
    result = await hive_search(app, query=QUERY, recency="latest")
    assert result["error"]["code"] == "invalid_input"
    assert "recency" in str(result["error"]["message"])


async def test_mcp_names_the_hint_only_when_it_was_applied() -> None:
    store = MemoryStore(make_clock())
    await store.create_entry(_draft(QUERY, 3), [0.1] * 4)
    off, on = _mcp(False, store), _mcp(True, store)
    plain = await hive_search(off, query=QUERY)
    for hint in HINTS:
        assert await hive_search(off, query=QUERY, recency=hint.value) == plain
    assert "recency" not in plain
    assert "recency" not in await hive_search(on, query=QUERY)
    assert "recency" not in await hive_search(on, query=QUERY, recency="default")
    assert (await hive_search(on, query=QUERY, recency="prefer_recent"))["recency"] == (
        "prefer_recent"
    )
    assert (await hive_search(on, query=QUERY, recency="ignore_age"))["recency"] == "ignore_age"


def test_the_env_var_reaches_the_search_config(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hivemind.config import load_settings

    for name in list(__import__("os").environ):
        if name.startswith("HIVEMIND_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("HIVEMIND_STRICT_ENV", "true")
    assert load_settings().search_config().recency_hint_enabled is True
    monkeypatch.setenv("HIVEMIND_RECENCY_HINT", "false")
    assert load_settings().search_config().recency_hint_enabled is False
    monkeypatch.setenv("HIVEMIND_RECENCY_HINT", "true")
    assert load_settings().search_config().recency_hint_enabled is True


def test_make_hivemind_app_default_has_the_hint_on() -> None:
    assert make_hivemind_app().search_config.recency_hint_enabled is True
