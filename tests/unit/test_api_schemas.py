"""Unit tests for the REST schema models (SPEC.md §5.1).

Seams under test: the pydantic request/response models in
``hivemind.api.schemas`` — validated as data shapes, before any HTTP.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hivemind.api.schemas import (
    CreateEntryRequest,
    EntryOut,
    FeedbackRequest,
    HealthOut,
    HitOut,
    SearchRequest,
    WithdrawRequest,
)


class TestCreateEntryRequest:
    def test_minimal_request_defaults(self) -> None:
        req = CreateEntryRequest(kind="fact", summary="a fact")
        assert req.body is None
        assert req.payload is None
        assert req.sources == []
        assert req.tags == []
        assert req.occurred_at is None
        # ROADMAP §4.5: an omitted importance is *unspecified* (None) at
        # the schema level; the route resolves it to 3 with
        # importance_source=default (the schema no longer hard-codes 3).
        assert req.importance is None
        # ADR 0011: an omitted scope is *unspecified* (None), so the route
        # resolves it to the highest scope the trust level permits. The old
        # default ("org") rejected every L2/L1 write (dogfood finding 1).
        assert req.scope is None
        assert req.supersedes == []
        assert req.agent is None

    def test_rejects_unknown_kind(self) -> None:
        with pytest.raises(ValidationError):
            CreateEntryRequest(kind="opinion", summary="not a v1 kind")  # type: ignore[arg-type]

    def test_full_request(self) -> None:
        req = CreateEntryRequest(
            kind="insight",
            summary="short",
            body="long form",
            payload={"n": 1},
            sources=[{"type": "path", "ref": "/tmp/a.csv"}],
            tags=["x", "y"],
            importance=5,
            supersedes=["id-1"],
            agent="my-agent",
        )
        assert req.body == "long form"
        assert req.payload == {"n": 1}
        assert [s.ref for s in req.sources] == ["/tmp/a.csv"]
        assert req.supersedes == ["id-1"]


class TestSearchRequest:
    def test_query_required(self) -> None:
        with pytest.raises(ValidationError):
            SearchRequest()  # type: ignore[call-arg]

    def test_defaults(self) -> None:
        req = SearchRequest(query="q")
        assert req.include_inactive is False
        assert req.limit is None
        assert req.kind is None
        assert req.tags == []


class TestFeedbackAndWithdrawRequests:
    def test_feedback_rejects_unknown_verdict(self) -> None:
        with pytest.raises(ValidationError):
            FeedbackRequest(verdict="amazing")  # type: ignore[arg-type]

    def test_feedback_accepts_all_verdicts(self) -> None:
        for verdict in ("helpful", "stale", "wrong"):
            assert FeedbackRequest(verdict=verdict).note is None

    def test_withdraw_reason_is_optional(self) -> None:
        assert WithdrawRequest().reason is None
        assert WithdrawRequest(reason="no longer valid").reason == "no longer valid"


class TestResponseShapes:
    def test_compact_hit_has_no_body_field(self) -> None:
        # Progressive disclosure (SPEC.md §6.1): the hit carries no body.
        assert "body" not in HitOut.model_fields
        assert "entry_id" in HitOut.model_fields
        assert "score" in HitOut.model_fields

    def test_full_entry_has_body_field(self) -> None:
        assert "body" in EntryOut.model_fields

    def test_health_out_shape(self) -> None:
        health = HealthOut(status="ok")
        assert health.status == "ok"
