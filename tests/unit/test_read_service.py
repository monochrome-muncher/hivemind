"""Unit tests for the ReadService (ADR 0083): the entry-read path is
serviced — visibility-checked read by id, batch read, filtered list, each
with its feedback and see-also links, composed in one place.

Pins the ADR 0033 semantics (invisible ≡ not found), the ADR 0055/0059
batch rules (dedupe, cap, a fixed number of store reads), and the ADR 0040
filter validation at the service seam.
"""

from __future__ import annotations

import pytest

from hivemind.domain.access import TrustLevel
from hivemind.domain.entry import EntryDraft, EntryFilters, Kind
from hivemind.domain.validation import MAX_GET_IDS, InvalidInput
from hivemind.ports import Credential
from hivemind.services.read import EntryDetail, ReadService
from hivemind.services.write import WriteService
from tests.fakes import make_embedder, make_search_config, make_store


def agent(name: str, fleet: str, level: TrustLevel) -> Credential:
    return Credential(
        user_id=name,
        agent_id=name,
        agent_name=name,
        access_controlled=True,
        trust_level=level,
        home_fleet_id=fleet,
    )


WRITER = agent("writer", "fleet-a", TrustLevel.CONTRIBUTOR)
OUTSIDER = agent("outsider", "fleet-b", TrustLevel.LURKER)
ADMIN = Credential(user_id="admin", is_admin=True)


async def _write(
    store,
    credential: Credential,
    summary: str = "s",
    *,
    supersedes: tuple[str, ...] = (),
) -> str:
    draft = EntryDraft(
        kind=Kind.FACT,
        summary=summary,
        author=credential.user_id,
        agent="a",
        scope="fleet",
        fleet_id=credential.home_fleet_id,
        supersedes=supersedes,
    )
    written = await WriteService(store, make_embedder()).write_and_relate(
        draft, writer=credential.visibility()
    )
    return written.entry.id


def _service(store, governance) -> ReadService:
    return ReadService(store, governance, make_search_config())


# -- entry() ----------------------------------------------------------------


async def test_entry_is_none_for_an_unknown_id() -> None:
    store = make_store()
    service = _service(store, None)
    assert await service.entry("nope", visibility=ADMIN.visibility()) is None


async def test_entry_is_none_for_an_invisible_entry() -> None:
    store = make_store()
    entry_id = await _write(store, WRITER)
    service = _service(store, None)
    assert await service.entry(entry_id, visibility=OUTSIDER.visibility()) is None


async def test_entry_carries_feedback_and_links() -> None:
    store = make_store()
    entry_id = await _write(store, WRITER)
    from hivemind.services.governance import GovernanceService

    service = _service(store, GovernanceService(store))
    detail = await service.entry(entry_id, visibility=ADMIN.visibility())
    assert isinstance(detail, EntryDetail)
    assert detail.entry.id == entry_id
    assert detail.feedback is not None
    assert detail.links is not None
    assert detail.successors == ()
    assert detail.superseded == ()


async def test_entry_history_fills_the_chain() -> None:
    store = make_store()
    from hivemind.services.governance import GovernanceService

    v1 = await _write(store, WRITER, summary="v1")
    v2 = await _write(store, WRITER, summary="v2", supersedes=(v1,))
    service = _service(store, GovernanceService(store))
    detail = await service.entry(v1, visibility=ADMIN.visibility(), include_history=True)
    assert [e.id for e in detail.successors] == [v2]
    assert detail.superseded == ()


# -- entries() ---------------------------------------------------------------


async def test_entries_returns_found_in_order_and_not_found() -> None:
    store = make_store()
    from hivemind.services.governance import GovernanceService

    a = await _write(store, WRITER, summary="a")
    b = await _write(store, WRITER, summary="b")
    service = _service(store, GovernanceService(store))
    batch = await service.entries([b, a, "missing"], visibility=ADMIN.visibility())
    assert [d.entry.id for d in batch.entries] == [b, a]
    assert batch.not_found == ("missing",)


async def test_entries_dedupes() -> None:
    store = make_store()
    from hivemind.services.governance import GovernanceService

    a = await _write(store, WRITER)
    service = _service(store, GovernanceService(store))
    batch = await service.entries([a, a], visibility=ADMIN.visibility())
    assert [d.entry.id for d in batch.entries] == [a]
    assert batch.not_found == ()


@pytest.mark.parametrize("ids", [[], [f"id{i}" for i in range(MAX_GET_IDS + 1)]])
async def test_entries_rejects_empty_and_over_cap(ids: list[str]) -> None:
    store = make_store()
    service = _service(store, None)
    with pytest.raises(InvalidInput):
        await service.entries(ids, visibility=ADMIN.visibility())


async def test_entries_hides_invisible_entries_in_not_found() -> None:
    store = make_store()
    from hivemind.services.governance import GovernanceService

    entry_id = await _write(store, WRITER)
    service = _service(store, GovernanceService(store))
    batch = await service.entries([entry_id], visibility=OUTSIDER.visibility())
    assert batch.entries == ()
    assert batch.not_found == (entry_id,)


# -- list() ------------------------------------------------------------------


async def test_list_validates_the_filters() -> None:
    store = make_store()
    service = _service(store, None)
    # ADR 0040: caller-built filters are bound-checked at the seam.
    with pytest.raises(InvalidInput):
        await service.list(
            EntryFilters(author="a" * 300),
            limit=10,
            offset=0,
            visibility=ADMIN.visibility(),
        )


async def test_list_applies_the_default_limit_when_omitted() -> None:
    store = make_store()
    for i in range(7):
        await _write(store, WRITER, summary=f"s{i}")
    service = _service(store, None)
    # make_search_config()'s default_limit is 5; 7 entries do not all fit.
    entries = await service.list(
        EntryFilters(), limit=None, offset=0, visibility=ADMIN.visibility()
    )
    assert len(entries) == 5


async def test_list_respects_an_explicit_limit_and_offset() -> None:
    store = make_store()
    for i in range(5):
        await _write(store, WRITER, summary=f"s{i}")
    service = _service(store, None)
    entries = await service.list(EntryFilters(), limit=2, offset=1, visibility=ADMIN.visibility())
    assert len(entries) == 2
