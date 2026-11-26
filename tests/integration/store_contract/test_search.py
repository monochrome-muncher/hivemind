"""The search streams (SPEC §6.2) on both adapters."""

from __future__ import annotations

import math
import uuid

import pytest
from tests.integration.store_contract.helpers import Adapter, _vis, draft, vector_ids, write

from hivemind.domain.access import TrustLevel, Visibility
from hivemind.domain.entry import EntryFilters


async def test_keyword_search_matches_any_term_and_respects_visibility(adapter: Adapter) -> None:
    jwt = await write(adapter, "tokens are signed with JWT", author="w")
    await write(adapter, "unrelated cooking recipe", author="w")
    private = await write(adapter, "JWT rotation notes", scope="self", author="w")
    ids = await adapter.store.search_keyword("JWT expiry", EntryFilters(), 10)
    assert set(ids) == {jwt.id, private.id}
    seen = await adapter.store.search_keyword(
        "JWT", EntryFilters(), 10, visibility=_vis(TrustLevel.CONTRIBUTOR)
    )
    assert seen == [jwt.id]


async def test_vector_search_ranks_nearest_first(adapter: Adapter) -> None:
    near = await write(adapter, "near", hot=1)
    far = await write(adapter, "far", hot=2)
    probe = adapter.vec(1)
    probe[2] = 0.5
    await adapter.store.create_entry(draft("no vector"), None, None)  # never a vector hit
    assert await vector_ids(adapter.store, probe, EntryFilters(), 10) == [near.id, far.id]


async def test_both_search_streams_apply_visibility(adapter: Adapter) -> None:
    """Every entry matches the query on both streams, so only visibility can
    leave one out."""
    home, other = str(uuid.uuid4()), str(uuid.uuid4())
    org = await write(adapter, "cohort churn org", scope="org", author="w")
    mine = await write(adapter, "cohort churn mine", scope="self", author="reader")
    await write(adapter, "cohort churn theirs", scope="self", author="w")
    in_home = await write(adapter, "cohort churn home", scope="fleet", fleet_id=home, author="w")
    in_other = await write(adapter, "cohort churn other", scope="fleet", fleet_id=other, author="w")

    async def seen(v: Visibility) -> tuple[set[str], set[str]]:
        keyword = await adapter.store.search_keyword("cohort", EntryFilters(), 10, visibility=v)
        vector = await vector_ids(adapter.store, adapter.vec(0), EntryFilters(), 10, visibility=v)
        return set(keyword), set(vector)

    member = {org.id, mine.id, in_home.id}
    assert await seen(_vis(TrustLevel.UNTRUSTED, fleet=home)) == (set(), set())
    assert await seen(_vis(TrustLevel.LURKER, fleet=home)) == (member, member)
    assert await seen(_vis(TrustLevel.PRIVILEGED, fleet=home)) == (
        member | {in_other.id},
        member | {in_other.id},
    )


async def test_similar_entries_rank_nearest_first_with_cosine_similarity(
    adapter: Adapter,
) -> None:
    """ADR 0052: nearest active entries first, each with its cosine similarity
    (an identical vector scores 1.0), cut to ``limit``."""
    same = await write(adapter, "Auth uses JWT", hot=0)
    far = await write(adapter, "Cookies", hot=3)
    pairs = await adapter.store.similar_entries(adapter.vec(0), 10)
    ids = [eid for eid, _ in pairs]
    assert ids == [same.id, far.id]
    assert pairs[0][1] == pytest.approx(1.0, abs=1e-4)
    assert pairs[1][1] < pairs[0][1]
    assert len(await adapter.store.similar_entries(adapter.vec(0), 1)) == 1


async def test_similar_entries_score_a_zero_vector_as_zero_not_nan(adapter: Adapter) -> None:
    """Cosine to a zero vector is NaN in pgvector; both stores say 0.0, as
    ``pairwise_similarity`` does."""
    near = await write(adapter, "near", hot=0)
    zero = await adapter.store.create_entry(draft("zero norm"), [0.0] * adapter.dim, "test-model")
    pairs = await adapter.store.similar_entries(adapter.vec(0), 10)
    assert [eid for eid, _ in pairs] == [near.id, zero.id]
    assert pairs[1][1] == 0.0
    assert all(math.isfinite(sim) for _, sim in pairs)


async def test_similar_entries_exclude_the_entry_and_inactive_ones(adapter: Adapter) -> None:
    old = await write(adapter, "v1", hot=3)
    new = await write(adapter, "v2", hot=3, supersedes=(old.id,))
    other = await write(adapter, "other", hot=4)
    pairs = await adapter.store.similar_entries(adapter.vec(3), 5, exclude_id=new.id)
    assert [eid for eid, _ in pairs] == [other.id]
    assert -1.0 <= pairs[0][1] <= 1.0


async def test_similar_entries_without_an_excluded_id_returns_every_active_match(
    adapter: Adapter,
) -> None:
    old = await write(adapter, "old", hot=3)
    new = await write(adapter, "new", hot=3, supersedes=(old.id,))
    other = await write(adapter, "other", hot=3)
    pairs = await adapter.store.similar_entries(adapter.vec(3), 5)
    assert {eid for eid, _ in pairs} == {new.id, other.id}


async def test_similar_entries_apply_the_readers_visibility(adapter: Adapter) -> None:
    """Related entries on a write are the writer's own view: another agent's
    ``self`` entries and another fleet's entries never show up as related."""
    home, other = str(uuid.uuid4()), str(uuid.uuid4())
    org = await write(adapter, "org note", hot=3, author="w")
    mine = await write(adapter, "my note", hot=3, scope="self", author="reader")
    theirs = await write(adapter, "their note", hot=3, scope="self", author="w")
    in_home = await write(adapter, "home fleet", hot=3, scope="fleet", fleet_id=home, author="w")
    in_other = await write(adapter, "other fleet", hot=3, scope="fleet", fleet_id=other, author="w")

    async def related(v: Visibility | None) -> set[str]:
        pairs = await adapter.store.similar_entries(adapter.vec(3), 10, visibility=v)
        return {eid for eid, _ in pairs}

    assert await related(None) == {org.id, mine.id, theirs.id, in_home.id, in_other.id}
    assert await related(_vis(TrustLevel.UNTRUSTED, fleet=home)) == set()
    member = {org.id, mine.id, in_home.id}
    assert await related(_vis(TrustLevel.LURKER, fleet=home)) == member
    assert await related(_vis(TrustLevel.CONTRIBUTOR, fleet=home)) == member
    assert await related(_vis(TrustLevel.PRIVILEGED, fleet=home)) == member | {in_other.id}


async def test_keyword_ties_break_newest_first(adapter: Adapter) -> None:
    first = await write(adapter, "shared word alpha")
    second = await write(adapter, "shared word alpha")
    assert await adapter.store.search_keyword("alpha", EntryFilters(), 10) == [
        second.id,
        first.id,
    ]


async def test_vector_search_leaves_out_entries_with_no_positive_similarity(
    adapter: Adapter,
) -> None:
    near = await adapter.store.create_entry(draft("near"), adapter.vec(5), "test-model")
    opposite = [0.0] * adapter.dim
    opposite[5] = -1.0
    await adapter.store.create_entry(draft("opposite"), opposite, "test-model")
    orthogonal = [0.0] * adapter.dim
    orthogonal[6] = 1.0
    await adapter.store.create_entry(draft("orthogonal"), orthogonal, "test-model")
    assert await vector_ids(adapter.store, adapter.vec(5, base=0.0), EntryFilters(), 10) == [
        near.id
    ]


async def test_vector_search_leaves_out_entries_at_or_below_min_similarity(
    adapter: Adapter,
) -> None:
    """The search similarity threshold (ADR 0062): ``min_similarity``
    raises ADR 0061's floor of 0, the same way on both adapters."""
    close = [0.0] * adapter.dim
    close[5], close[6] = 0.8, 0.6  # similarity 0.8 to the probe
    loose = [0.0] * adapter.dim
    loose[5], loose[6] = 0.3, 0.9539392  # similarity 0.3
    near = await adapter.store.create_entry(draft("close"), close, "test-model")
    far = await adapter.store.create_entry(draft("loose"), loose, "test-model")
    probe = adapter.vec(5, base=0.0)
    search = adapter.store.search_vector
    matches = await search(probe, EntryFilters(), 10, min_similarity=0.0)
    assert [m.entry_id for m in matches] == [near.id, far.id]
    assert [m.similarity for m in matches] == pytest.approx([0.8, 0.3], abs=1e-3)
    assert [m.entry_id for m in await search(probe, EntryFilters(), 10, min_similarity=0.5)] == [
        near.id
    ]
    assert await search(probe, EntryFilters(), 10, min_similarity=0.9) == []
