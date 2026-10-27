"""``upstream_changed`` on a search hit (SPEC §6.1, ADR 0081): the pure rule,
the service on ``MemoryStore``, both surfaces, and that it never ranks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hivemind.domain.access import TrustLevel, Visibility
from hivemind.domain.entry import EntryDraft, EntryFilters, EntryState, Kind
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.domain.upstream import (
    MAX_UPSTREAM_CHANGED,
    UpstreamChange,
    UpstreamChangeKind,
    UpstreamLink,
    upstream_changed,
)
from hivemind.mcp.app import hive_search
from hivemind.memstore import MemoryStore
from hivemind.services.search import SearchService
from tests.fakes import FIXED_NOW, FixedClock, make_clock, make_embedder, make_search_config
from tests.unit.test_readable_feedback import WRITER, _rest_client, _surface

T0 = datetime(2026, 9, 1, tzinfo=UTC)
VEC = [0.1, 0.1, 0.1, 0.1]


def _min(n: int) -> datetime:
    return T0 + timedelta(minutes=n)


def _link(to_id: str = "t1", **kw: object) -> UpstreamLink:
    fields: dict[str, object] = {
        "from_id": "f",
        "to_id": to_id,
        "linked_at": _min(10),
        "state": EntryState.ACTIVE,
    }
    fields.update(kw)
    return UpstreamLink(**fields)  # type: ignore[arg-type]


def _kinds(links: list[UpstreamLink], kind: Kind = Kind.INSIGHT, author: str = "me") -> list[str]:
    return [f"{c.id}:{c.change.value}" for c in upstream_changed(kind, author, links)]


# --- the pure rule ------------------------------------------------------------------


def test_an_unchanged_target_is_not_reported() -> None:
    assert _kinds([_link()]) == []
    assert _kinds([]) == []


def test_a_withdrawn_target_is_reported() -> None:
    assert _kinds([_link(state=EntryState.WITHDRAWN)]) == ["t1:withdrawn"]


def test_a_target_superseded_by_someone_else_after_the_link_is_reported() -> None:
    link = _link(state=EntryState.SUPERSEDED, superseded_at=_min(11), superseded_author="other")
    assert _kinds([link]) == ["t1:superseded"]


def test_supersession_by_the_linking_author_or_before_the_link_is_not_reported() -> None:
    mine = _link(state=EntryState.SUPERSEDED, superseded_at=_min(11), superseded_author="me")
    before = _link(state=EntryState.SUPERSEDED, superseded_at=_min(9), superseded_author="other")
    tie = _link(state=EntryState.SUPERSEDED, superseded_at=_min(10), superseded_author="other")
    assert _kinds([mine, before, tie]) == []


def test_a_flag_after_the_link_is_reported_and_one_before_is_not() -> None:
    assert _kinds([_link(newest_flag_at=_min(11))]) == ["t1:flagged"]
    assert _kinds([_link(newest_flag_at=_min(9))]) == []
    assert _kinds([_link(newest_flag_at=_min(10))]) == []


def test_a_confirmation_after_the_flag_clears_it() -> None:
    flagged = _link(newest_flag_at=_min(11), newest_noted_helpful_at=_min(12))
    assert _kinds([flagged]) == []
    # A confirmation older than the flag, or a tie, does not.
    assert _kinds([_link(newest_flag_at=_min(11), newest_noted_helpful_at=_min(10))]) == [
        "t1:flagged"
    ]
    assert _kinds([_link(newest_flag_at=_min(11), newest_noted_helpful_at=_min(11))]) == [
        "t1:flagged"
    ]


def test_a_foreign_supersession_by_the_author_still_lets_a_flag_show() -> None:
    link = _link(
        state=EntryState.SUPERSEDED,
        superseded_at=_min(11),
        superseded_author="me",
        newest_flag_at=_min(12),
    )
    assert _kinds([link]) == ["t1:flagged"]


def test_only_insights_and_decisions_get_the_marker() -> None:
    links = [_link(state=EntryState.WITHDRAWN)]
    assert _kinds(links, Kind.FACT) == []
    assert _kinds(links, Kind.INSIGHT) == ["t1:withdrawn"]
    assert _kinds(links, Kind.DECISION) == ["t1:withdrawn"]


def test_at_most_three_newest_first_with_withdrawals_ahead() -> None:
    links = [
        _link("a", newest_flag_at=_min(11)),
        _link("b", newest_flag_at=_min(15)),
        _link("c", newest_flag_at=_min(13)),
        _link("d", newest_flag_at=_min(12)),
        _link("w", state=EntryState.WITHDRAWN),
    ]
    assert MAX_UPSTREAM_CHANGED == 3
    assert _kinds(links) == ["w:withdrawn", "b:flagged", "c:flagged"]


def test_ties_break_on_the_target_id() -> None:
    links = [_link("z", newest_flag_at=_min(11)), _link("a", newest_flag_at=_min(11))]
    assert _kinds(links) == ["a:flagged", "z:flagged"]


# --- the service on MemoryStore -----------------------------------------------------


async def _put(
    store: MemoryStore,
    summary: str,
    *,
    author: str = "writer",
    kind: Kind = Kind.INSIGHT,
    **kw: object,
) -> str:
    draft = EntryDraft(kind=kind, summary=summary, author=author, agent="a", **kw)  # type: ignore[arg-type]
    return (await store.create_entry(draft, VEC)).id


def _service(store: MemoryStore) -> SearchService:
    return SearchService(store, make_embedder(), make_search_config(), now_fn=lambda: FIXED_NOW)


async def _marker(
    store: MemoryStore, query: str = "billing node build", visibility: Visibility | None = None
) -> dict[str, tuple[UpstreamChange, ...]]:
    hits = await _service(store).search(query, visibility=visibility)
    return {h.summary: h.upstream_changed for h in hits}


async def _built_on(clock: FixedClock, store: MemoryStore, **kw: object) -> tuple[str, str]:
    """A base entry and an insight (by ``writer``) that links to it."""
    base = await _put(store, "billing node base", kind=Kind.FACT, author="ann", scope="org")
    clock.advance_days(1)
    built = await _put(store, "billing node build", see_also=(base,), **kw)
    clock.advance_days(1)
    return base, built


async def test_withdrawing_a_target_marks_the_entry_built_on_it() -> None:
    clock = make_clock()
    store = MemoryStore(clock)
    base, _ = await _built_on(clock, store)
    assert (await _marker(store))["billing node build"] == ()
    await store.withdraw_entry(base, "wrong", by_user="ann")
    assert (await _marker(store))["billing node build"] == (
        UpstreamChange(base, UpstreamChangeKind.WITHDRAWN),
    )


async def test_supersession_by_someone_else_marks_but_the_authors_own_does_not() -> None:
    clock = make_clock()
    store = MemoryStore(clock)
    base, _ = await _built_on(clock, store)
    await _put(store, "billing node base v2", kind=Kind.FACT, author="writer", supersedes=(base,))
    assert (await _marker(store))["billing node build"] == ()

    other_clock = make_clock()
    other = MemoryStore(other_clock)
    base2, _ = await _built_on(other_clock, other)
    await _put(other, "billing node base v2", kind=Kind.FACT, author="ann", supersedes=(base2,))
    assert (await _marker(other))["billing node build"] == (
        UpstreamChange(base2, UpstreamChangeKind.SUPERSEDED),
    )


async def test_a_target_superseded_before_the_link_is_not_marked() -> None:
    clock = make_clock()
    store = MemoryStore(clock)
    old = await _put(store, "billing node old", kind=Kind.FACT, author="ann")
    clock.advance_days(1)
    await _put(store, "billing node new", kind=Kind.FACT, author="ann", supersedes=(old,))
    clock.advance_days(1)
    await _put(store, "billing node build", see_also=(old,))
    assert (await _marker(store))["billing node build"] == ()


async def test_a_flag_marks_and_a_later_confirmation_clears_it() -> None:
    clock = make_clock()
    store = MemoryStore(clock)
    base, _ = await _built_on(clock, store)
    when = clock()
    await store.record_feedback(Feedback(base, "u1", "a", Verdict.STALE, None, when))
    assert (await _marker(store))["billing node build"] == (
        UpstreamChange(base, UpstreamChangeKind.FLAGGED),
    )
    await store.record_feedback(
        Feedback(base, "u2", "a", Verdict.HELPFUL, "ran it", when + timedelta(minutes=1))
    )
    assert (await _marker(store))["billing node build"] == ()


async def test_a_flag_before_the_link_does_not_mark() -> None:
    clock = make_clock()
    store = MemoryStore(clock)
    base = await _put(store, "billing node base", kind=Kind.FACT, author="ann")
    await store.record_feedback(Feedback(base, "u1", "a", Verdict.WRONG, None, clock()))
    clock.advance_days(1)
    await _put(store, "billing node build", see_also=(base,))
    assert (await _marker(store))["billing node build"] == ()


async def test_a_decision_gets_the_marker() -> None:
    clock = make_clock()
    store = MemoryStore(clock)
    base, _ = await _built_on(clock, store, kind=Kind.DECISION)
    await store.withdraw_entry(base, None, by_user="ann")
    hits = await _service(store).search("billing node build")
    [built] = [h for h in hits if h.summary == "billing node build"]
    assert built.kind is Kind.DECISION
    assert built.upstream_changed == (UpstreamChange(base, UpstreamChangeKind.WITHDRAWN),)


async def test_a_fact_never_gets_the_marker() -> None:
    clock = make_clock()
    store = MemoryStore(clock)
    base, _ = await _built_on(clock, store, kind=Kind.FACT)
    await store.withdraw_entry(base, None, by_user="ann")
    assert (await _marker(store))["billing node build"] == ()


async def test_a_target_the_reader_cannot_read_is_never_reported() -> None:
    clock = make_clock()
    store = MemoryStore(clock)
    secret = await _put(store, "billing node secret", kind=Kind.FACT, author="ann", scope="self")
    shown = await _put(store, "billing node shown", kind=Kind.FACT, author="ann", scope="org")
    clock.advance_days(1)
    # The writer may link a self entry only if it can read it: the author itself writes.
    await _put(store, "billing node build", author="ann", see_also=(secret, shown))
    clock.advance_days(1)
    await store.withdraw_entry(secret, None, by_user="ann")
    await store.withdraw_entry(shown, None, by_user="ann")
    stranger = Visibility(TrustLevel.CONTRIBUTOR, "stranger", home_fleet_id="fleet-x")
    assert [c.id for c in (await _marker(store, visibility=stranger))["billing node build"]] == [
        shown
    ]
    author = Visibility(TrustLevel.CONTRIBUTOR, "ann", home_fleet_id="fleet-x")
    assert {c.id for c in (await _marker(store, visibility=author))["billing node build"]} == {
        secret,
        shown,
    }


async def test_a_page_costs_one_batched_read() -> None:
    clock = make_clock()
    store = MemoryStore(clock)
    for i in range(3):
        base = await _put(store, f"billing node base {i}", kind=Kind.FACT, author="ann")
        await _put(store, f"billing node build {i}", see_also=(base,))
    calls: list[list[str]] = []
    real = store.upstream_links

    async def counting(ids: list[str], **kw: object) -> object:
        calls.append(ids)
        return await real(ids, **kw)  # type: ignore[arg-type]

    store.upstream_links = counting  # type: ignore[method-assign,assignment]
    await _service(store).search("billing node build")
    assert len(calls) == 1 and len(calls[0]) >= 3


async def test_a_failing_read_costs_the_marker_not_the_search() -> None:
    clock = make_clock()
    store = MemoryStore(clock)
    base, _ = await _built_on(clock, store)
    await store.withdraw_entry(base, None, by_user="ann")

    async def boom(ids: list[str], **kw: object) -> object:
        raise RuntimeError("down")

    store.upstream_links = boom  # type: ignore[method-assign,assignment]
    hits = await _service(store).search("billing node build")
    assert any(h.summary == "billing node build" for h in hits)
    assert all(h.upstream_changed == () for h in hits)


async def test_the_marker_never_changes_a_score_or_the_order() -> None:
    """Same store, same candidates: with the marker read and without it."""
    clock = make_clock()
    store = MemoryStore(clock)
    base, _ = await _built_on(clock, store)
    await store.withdraw_entry(base, None, by_user="ann")
    marked = await _service(store).search("billing node build", EntryFilters(include_inactive=True))
    assert any(h.upstream_changed for h in marked)

    async def none(ids: list[str], **kw: object) -> dict[str, list[UpstreamLink]]:
        return {}

    store.upstream_links = none  # type: ignore[method-assign,assignment]
    bare = await _service(store).search("billing node build", EntryFilters(include_inactive=True))
    assert [(h.entry_id, h.score) for h in marked] == [(h.entry_id, h.score) for h in bare]


# --- surfaces -----------------------------------------------------------------------


async def _withdrawn_upstream() -> tuple[MemoryStore, str]:
    clock = make_clock()
    store = MemoryStore(clock)
    base = await _put(store, "billing node base", kind=Kind.FACT, author="ann", scope="org")
    clock.advance_days(1)
    await _put(store, "billing node build", author="ann", scope="org", see_also=(base,))
    await store.withdraw_entry(base, None, by_user="ann")
    return store, base


async def test_mcp_hit_lists_the_change_and_omits_it_when_empty() -> None:
    store, base = await _withdrawn_upstream()
    await _put(store, "billing node standalone", kind=Kind.FACT, author="ann", scope="org")
    reply = await hive_search(_surface(store, WRITER), query="billing node build")
    by_summary = {h["summary"]: h for h in reply["hits"]}  # type: ignore[index,union-attr]
    assert set(by_summary) == {"billing node build", "billing node standalone"}
    assert by_summary["billing node build"]["upstream_changed"] == [
        {"id": base, "change": "withdrawn"}
    ]
    assert "upstream_changed" not in by_summary["billing node standalone"]


async def test_mcp_plain_hit_has_no_upstream_field() -> None:
    clock = make_clock()
    store = MemoryStore(clock)
    await _put(store, "billing node build", scope="org")
    reply = await hive_search(_surface(store, WRITER), query="billing node build")
    [hit] = reply["hits"]  # type: ignore[index,misc]
    assert "upstream_changed" not in hit


async def test_rest_hit_lists_the_change_and_is_null_when_empty() -> None:
    store, base = await _withdrawn_upstream()
    async with _rest_client(store, WRITER) as client:
        hits = (await client.post("/v1/search", json={"query": "billing node build"})).json()
    built = next(h for h in hits if h["summary"] == "billing node build")
    assert built["upstream_changed"] == [{"id": base, "change": "withdrawn"}]

    plain = MemoryStore(make_clock())
    await _put(plain, "billing node build", scope="org")
    async with _rest_client(plain, WRITER) as client:
        [hit] = (await client.post("/v1/search", json={"query": "billing node build"})).json()
    assert hit["upstream_changed"] is None
