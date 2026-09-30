"""Unit tests for the supersession-chain service (SPEC.md §5.1/§5.2).

The chain walk (``supersession_chain``) is the shared deep module REST
``?history`` and MCP ``hive_get`` both use. These tests pin its
behavior against a hand-built chain.
"""

from __future__ import annotations

from hivemind.services.chain import supersession_chain
from tests.fakes import make_store


async def _build_chain(store) -> tuple:
    """a -> b -> c  (c supersedes b, b supersedes a).

    Re-fetches the stored rows: the local objects returned by
    ``create_entry`` are stale (a later supersession mutates the stored
    copy, not the earlier return value) — the chain service's contract
    is a freshly-fetched entry (as REST/MCP both supply).
    """
    a = await store.create_entry(_draft("v1"))
    b = await store.create_entry(_draft("v2", supersedes=(a.id,)))
    c = await store.create_entry(_draft("v3", supersedes=(b.id,)))
    return (
        await store.get_entry(a.id),
        await store.get_entry(b.id),
        await store.get_entry(c.id),
    )


def _draft(summary, **kw):
    from hivemind.domain.entry import EntryDraft, Kind

    return EntryDraft(
        kind=kw.pop("kind", Kind.FACT),
        summary=summary,
        author=kw.pop("author", "alice"),
        agent="agent-1",
        **kw,
    )


async def test_middle_entry_exposes_both_sides() -> None:
    store = make_store()
    a, b, c = await _build_chain(store)
    successors, superseded = await supersession_chain(store, b)
    assert [e.id for e in successors] == [c.id]
    assert [e.id for e in superseded] == [a.id]


async def test_head_entry_has_no_successors() -> None:
    store = make_store()
    a, b, c = await _build_chain(store)
    successors, superseded = await supersession_chain(store, c)
    assert successors == []
    # c is the head: its superseded set is the older versions it replaced.
    assert {e.id for e in superseded} == {a.id, b.id}


async def test_a_corrupt_superseded_by_cycle_is_bounded_and_deduped() -> None:
    """A corrupt chain (a -> b -> a) must not loop the forward walk and
    must not return the same entry twice. A real chain can never do this
    (supersession requires an active target, ADR 0034), so the guard is
    for storage corruption, not for legal data."""
    import dataclasses

    store = make_store()
    a = await store.create_entry(_draft("v1"))
    b = await store.create_entry(_draft("v2", supersedes=(a.id,)))
    # Corrupt: point b's superseded_by back at a.
    store._entries[b.id] = dataclasses.replace(store._entries[b.id], superseded_by=a.id)
    successors, _ = await supersession_chain(store, await store.get_entry(a.id))
    assert [e.id for e in successors] == [b.id]


async def test_tail_entry_has_no_superseded() -> None:
    store = make_store()
    a, b, c = await _build_chain(store)
    successors, superseded = await supersession_chain(store, a)
    assert [e.id for e in successors] == [b.id, c.id]
    assert superseded == []


async def test_unrelated_entry_has_empty_chain() -> None:
    store = make_store()
    lone = await store.create_entry(_draft("lone fact"))
    successors, superseded = await supersession_chain(store, lone)
    assert successors == []
    assert superseded == []


async def test_reverse_walk_asks_for_predecessors_not_the_pool() -> None:
    """PERF-1/STORE-2: the reverse walk is one ``list_predecessors`` call
    per hop (O(chain depth)), never a scan of ``list_entries`` over the
    whole pool."""
    store = make_store()
    # A long chain plus unrelated bystanders the walk must never read.
    prev = await store.create_entry(_draft("v0"))
    for i in range(1, 6):
        prev = await store.create_entry(_draft(f"v{i}", supersedes=(prev.id,)))
    for i in range(20):
        await store.create_entry(_draft(f"bystander {i}"))
    head = await store.get_entry(prev.id)

    calls = {"predecessors": 0}
    real = store.list_predecessors

    async def counting(ids):
        calls["predecessors"] += 1
        return await real(ids)

    async def forbidden(*args, **kwargs):
        raise AssertionError("the chain walk must not scan the pool")

    store.list_predecessors = counting  # type: ignore[method-assign]
    store.list_entries = forbidden  # type: ignore[method-assign]
    _, superseded = await supersession_chain(store, head)

    assert len(superseded) == 5
    # depth 5 + one final empty frontier probe
    assert calls["predecessors"] <= 6


async def test_hop_cap_bounds_the_reverse_walk() -> None:
    store = make_store()
    prev = await store.create_entry(_draft("v0"))
    for i in range(1, 6):
        prev = await store.create_entry(_draft(f"v{i}", supersedes=(prev.id,)))
    head = await store.get_entry(prev.id)
    _, superseded = await supersession_chain(store, head, max_hops=2)
    assert len(superseded) == 2


async def test_invisible_middle_version_stays_out_but_does_not_cut_the_walk() -> None:
    """ADR 0033: the walk passes through a version the reader cannot see
    (so older visible ones are still reached) but never returns it."""
    from hivemind.domain.access import TrustLevel, Visibility

    store = make_store()
    a = await store.create_entry(_draft("v1", scope="org"))
    b = await store.create_entry(_draft("v2", scope="self", author="bob", supersedes=(a.id,)))
    c = await store.create_entry(_draft("v3", scope="self", author="bob", supersedes=(b.id,)))
    reader = Visibility(level=TrustLevel.CONTRIBUTOR, name="alice")
    _, superseded = await supersession_chain(store, await store.get_entry(c.id), visibility=reader)
    assert [e.id for e in superseded] == [a.id]
    everything = await supersession_chain(store, await store.get_entry(c.id))
    assert {e.id for e in everything[1]} == {a.id, b.id}
