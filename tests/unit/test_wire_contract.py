"""The wire contract (ADR 0084): the REST and MCP surfaces serialize the
same shapes, and this file owns the canonical field tables.

Each table is ``field -> value`` for a fully-populated fixture; the tests
assert that both surfaces' serializers produce exactly those fields and
values (the REST models carry a fixed set of extra None-valued write/read
fields, listed per shape). A wire-shape change edits a table here, and
fails if the two surfaces drift.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from hivemind.api.schemas import EntryOut, FeedbackSummaryOut, HitOut, LinkOut
from hivemind.domain.entry import (
    EntityKind,
    Entry,
    EntryState,
    ExtractedEntity,
    ImportanceSource,
    Kind,
    Source,
    SourceType,
)
from hivemind.domain.feedback import Correction, Feedback, FeedbackSummary, Verdict
from hivemind.domain.upstream import UpstreamChange, UpstreamChangeKind
from hivemind.services.search import Hit, SimilarEntry
from hivemind.wire import (
    entry_dict,
    feedback_dict,
    hit_dict,
    link_dict,
    list_item_dict,
)

DT_OCCURRED = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
DT_CREATED = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
DT_CONFIRMED = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)
DT_REPORT = datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)

ENTRY = Entry(
    id="11111111-1111-4111-8111-111111111111",
    kind=Kind.FACT,
    summary="the summary",
    author="author-a",
    agent="agent-a",
    occurred_at=DT_OCCURRED,
    created_at=DT_CREATED,
    body="the body",
    payload={"k": "v"},
    sources=(Source(type=SourceType.URL, ref="https://example.com/x"),),
    tags=("tag-1",),
    importance=4,
    importance_source=ImportanceSource.CALLER,
    scope="fleet",
    fleet_id="fleet-1",
    state=EntryState.ACTIVE,
    superseded_by=None,
    withdrawn_reason=None,
    entities=(ExtractedEntity(name="Entity E", kind=EntityKind.PERSON),),
    entities_model="model-x",
)

# --- canonical tables -------------------------------------------------------

# Full entry, body included. Both surfaces carry identical fields.
ENTRY_TABLE: dict[str, object] = {
    "id": ENTRY.id,
    "kind": "fact",
    "summary": "the summary",
    "author": "author-a",
    "agent": "agent-a",
    "body": "the body",
    "payload": {"k": "v"},
    "sources": [{"type": "url", "ref": "https://example.com/x"}],
    "tags": ["tag-1"],
    "importance": 4,
    "importance_source": "caller",
    "scope": "fleet",
    "fleet_id": "fleet-1",
    "state": "active",
    "superseded_by": None,
    "withdrawn_reason": None,
    "entities": [{"name": "Entity E", "kind": "person"}],
    "entities_model": "model-x",
    "occurred_at": "2026-10-01T12:00:00+00:00",
    "created_at": "2026-10-02T12:00:00+00:00",
}
# The REST model additionally carries the read/write-only fields, all None
# on a plain get.
ENTRY_REST_EXTRAS = frozenset(
    {"history", "feedback", "related", "warnings", "see_also", "linked_from"}
)

# Compact list item (hive_list / GET /v1/entries): no body, payload,
# sources, importance, entities.
LIST_ITEM_TABLE: dict[str, object] = {
    "id": ENTRY.id,
    "kind": "fact",
    "summary": "the summary",
    "tags": ["tag-1"],
    "author": "author-a",
    "agent": "agent-a",
    "occurred_at": "2026-10-01T12:00:00+00:00",
    "created_at": "2026-10-02T12:00:00+00:00",
    "scope": "fleet",
    "fleet_id": "fleet-1",
    "state": "active",
    "superseded_by": None,
}

FEEDBACK_SUMMARY = FeedbackSummary(
    helpful=2,
    stale=1,
    wrong=0,
    recent=(
        Feedback(
            entry_id=ENTRY.id,
            user="reporter-r",
            agent="agent-r",
            verdict=Verdict.HELPFUL,
            note="a note",
            updated_at=DT_REPORT,
        ),
    ),
    last_confirmed_at=DT_CONFIRMED,
)

FEEDBACK_TABLE: dict[str, object] = {
    "helpful": 2,
    "stale": 1,
    "wrong": 0,
    "recent": [
        {
            "verdict": "helpful",
            "note": "a note",
            "reporter": "reporter-r",
            "updated_at": "2026-10-04T12:00:00+00:00",
        }
    ],
    "last_confirmed_at": "2026-10-03T12:00:00+00:00",
}

LINK_TABLE: dict[str, object] = {
    "id": ENTRY.id,
    "kind": "fact",
    "summary": "the summary",
    "author": "author-a",
    "state": "active",
    "fleet_id": "fleet-1",
}

HIT = Hit(
    entry_id="22222222-2222-4222-8222-222222222222",
    kind=Kind.INSIGHT,
    summary="hit summary",
    tags=("tag-h",),
    author="author-h",
    agent="agent-h",
    occurred_at=DT_OCCURRED,
    score=0.87,
    scope="org",
    fleet_id=None,
    feedback=(3, 1, 2),
    last_confirmed_at=DT_CONFIRMED,
    contested=True,
    correction=Correction(
        verdict=Verdict.STALE,
        note="correction note",
        reporter="reporter-c",
        updated_at=DT_REPORT,
    ),
    supersedes=2,
    state=EntryState.ACTIVE,
    similar=(
        SimilarEntry(entry_id="33333333-3333-4333-8333-333333333333", occurred_at=DT_CREATED),
    ),
    upstream_changed=(
        UpstreamChange(
            id="44444444-4444-4444-8444-444444444444", change=UpstreamChangeKind.SUPERSEDED
        ),
    ),
)

# Search hits: same fields on both surfaces, one deliberate naming
# divergence — the MCP hit is keyed by ``id`` (agents fetch with
# hive_get(id=...)), the REST hit by ``entry_id``.
HIT_TABLE_MCP: dict[str, object] = {
    "id": HIT.entry_id,
    "kind": "insight",
    "summary": "hit summary",
    "tags": ["tag-h"],
    "author": "author-h",
    "agent": "agent-h",
    "occurred_at": "2026-10-01T12:00:00+00:00",
    "score": 0.87,
    "scope": "org",
    "fleet_id": None,
    "feedback": {"helpful": 3, "stale": 1, "wrong": 2},
    "last_confirmed_at": "2026-10-03T12:00:00+00:00",
    "contested": True,
    "correction": {
        "verdict": "stale",
        "note": "correction note",
        "reporter": "reporter-c",
        "updated_at": "2026-10-04T12:00:00+00:00",
    },
    "supersedes": 2,
    "state": "active",
    "similar": [
        {"id": "33333333-3333-4333-8333-333333333333", "occurred_at": "2026-10-02T12:00:00+00:00"}
    ],
    "upstream_changed": [{"id": "44444444-4444-4444-8444-444444444444", "change": "superseded"}],
}
HIT_TABLE_REST = {**HIT_TABLE_MCP, "entry_id": HIT.entry_id}
del HIT_TABLE_REST["id"]


# --- the contract -----------------------------------------------------------


def _norm(value):
    """pydantic's JSON mode writes UTC datetimes with a ``Z`` suffix; the
    canonical tables use ``isoformat()`` (``+00:00``). Normalise for the
    comparison — the two are the same instant on the wire."""
    if isinstance(value, str) and value.endswith("Z") and value[:4].isdigit():
        return value[:-1] + "+00:00"
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_norm(v) for v in value]
    return value


def test_full_entry_matches_the_contract_on_both_surfaces() -> None:
    assert entry_dict(ENTRY) == ENTRY_TABLE
    rest = EntryOut.from_entry(ENTRY).model_dump(mode="json")
    assert set(rest) == set(ENTRY_TABLE) | ENTRY_REST_EXTRAS
    for extra in ENTRY_REST_EXTRAS:
        assert rest[extra] is None
    assert _norm({k: v for k, v in rest.items() if k in ENTRY_TABLE}) == ENTRY_TABLE


def test_list_item_matches_the_contract() -> None:
    assert list_item_dict(ENTRY) == LIST_ITEM_TABLE
    # The REST list answer is EntryOut (the full shape, body included);
    # the compact shape is the MCP list item only.
    rest = EntryOut.from_entry(ENTRY).model_dump(mode="json")
    assert _norm({k: rest[k] for k in LIST_ITEM_TABLE}) == LIST_ITEM_TABLE


def test_feedback_summary_matches_the_contract_on_both_surfaces() -> None:
    assert feedback_dict(FEEDBACK_SUMMARY) == FEEDBACK_TABLE
    assert (
        _norm(FeedbackSummaryOut.from_summary(FEEDBACK_SUMMARY).model_dump(mode="json"))
        == FEEDBACK_TABLE
    )


def test_link_matches_the_contract_on_both_surfaces() -> None:
    assert link_dict(ENTRY) == LINK_TABLE
    assert _norm(LinkOut.from_entry(ENTRY).model_dump(mode="json")) == LINK_TABLE


def test_hit_matches_the_contract_on_both_surfaces() -> None:
    assert hit_dict(HIT) == HIT_TABLE_MCP
    assert _norm(HitOut.from_hit(HIT).model_dump(mode="json")) == HIT_TABLE_REST


def test_hit_omits_the_optional_fields_when_empty() -> None:
    bare = Hit(
        entry_id=HIT.entry_id,
        kind=HIT.kind,
        summary=HIT.summary,
        tags=HIT.tags,
        author=HIT.author,
        agent=HIT.agent,
        occurred_at=HIT.occurred_at,
        score=HIT.score,
    )
    mcp = hit_dict(bare)
    assert mcp == {
        "id": HIT.entry_id,
        "kind": "insight",
        "summary": "hit summary",
        "tags": ["tag-h"],
        "author": "author-h",
        "agent": "agent-h",
        "occurred_at": "2026-10-01T12:00:00+00:00",
        "score": 0.87,
        "scope": "fleet",
        "fleet_id": None,
    }
    rest = HitOut.from_hit(bare).model_dump(mode="json")
    assert rest["entry_id"] == HIT.entry_id
    for optional in (
        "feedback",
        "last_confirmed_at",
        "contested",
        "correction",
        "supersedes",
        "state",
        "similar",
        "upstream_changed",
    ):
        assert rest[optional] is None


def test_hit_score_is_rounded_to_four_significant_figures_on_both_surfaces() -> None:
    raw = replace(HIT, score=0.01600102405288303)
    assert hit_dict(raw)["score"] == 0.016
    assert HitOut.from_hit(raw).score == 0.016
    assert hit_dict(replace(HIT, score=0.0118349))["score"] == 0.01183
    assert hit_dict(replace(HIT, score=1.23456))["score"] == 1.235
    assert raw.score == 0.01600102405288303  # the hit itself, and so the ranking, is untouched


def test_hit_feedback_is_left_out_only_when_all_counts_are_zero() -> None:
    for counts in ((0, 0, 0),):
        zero = replace(HIT, feedback=counts)
        assert "feedback" not in hit_dict(zero)
        assert HitOut.from_hit(zero).feedback is None
    for counts in ((1, 0, 0), (0, 1, 0), (0, 0, 1)):
        some = replace(HIT, feedback=counts)
        assert hit_dict(some)["feedback"] == dict(
            zip(("helpful", "stale", "wrong"), counts, strict=True)
        )
        assert HitOut.from_hit(some).feedback is not None
