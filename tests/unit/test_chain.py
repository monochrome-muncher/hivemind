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
        author="alice",
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
