"""``entry_draft``: the credential-derived parts of a write, shared by REST
``POST /v1/entries`` and MCP ``hive_write``."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hivemind.domain.access import TrustLevel
from hivemind.domain.entry import ImportanceSource, Kind, Source, SourceType
from hivemind.ports import Credential
from hivemind.services.access import resolve_write_scope
from hivemind.services.drafts import DEFAULT_IMPORTANCE, entry_draft

AGENT = Credential(
    user_id="scout",
    agent_id="scout",
    agent_name="scout",
    access_controlled=True,
    trust_level=TrustLevel.CONTRIBUTOR,
    home_fleet_id="fleet-a",
)
LEGACY = Credential(user_id="alice")


def _draft(credential: Credential, **overrides: object):  # type: ignore[no-untyped-def]
    args: dict[str, object] = {
        "agent": "claude-code",
        "kind": Kind.FACT,
        "summary": "s",
        "body": None,
        "payload": None,
        "sources": (),
        "tags": [],
        "occurred_at": None,
        "importance": None,
        "supersedes": [],
        "see_also": [],
    }
    args.update(overrides)
    return entry_draft(credential, resolve_write_scope(credential), **args)  # type: ignore[arg-type]


def test_omitted_importance_is_the_default() -> None:
    draft = _draft(AGENT)
    assert draft.importance == DEFAULT_IMPORTANCE == 3
    assert draft.importance_source is ImportanceSource.DEFAULT


def test_supplied_importance_is_the_callers() -> None:
    draft = _draft(AGENT, importance=3)
    assert draft.importance == 3
    assert draft.importance_source is ImportanceSource.CALLER


def test_out_of_range_importance_is_refused() -> None:
    with pytest.raises(ValueError, match=r"importance must be 1\.\.5"):
        _draft(AGENT, importance=6)


def test_author_is_the_keys_name_and_scope_the_resolution() -> None:
    draft = _draft(AGENT, agent="scout")
    assert (draft.author, draft.agent) == ("scout", "scout")
    assert (draft.scope, draft.fleet_id) == ("fleet", "fleet-a")
    legacy = _draft(LEGACY)
    assert (legacy.author, legacy.agent, legacy.scope) == ("alice", "claude-code", "org")


def test_request_fields_are_carried_over() -> None:
    when = datetime(2026, 1, 2, tzinfo=UTC)
    source = Source(type=SourceType.URL, ref="https://example.test")
    draft = _draft(
        AGENT,
        kind=Kind.DECISION,
        summary="decided",
        body="why",
        payload={"k": 1},
        sources=(source,),
        tags=["a", "b"],
        occurred_at=when,
        supersedes=["x"],
        see_also=["y"],
    )
    assert draft.kind is Kind.DECISION
    assert (draft.summary, draft.body, draft.payload) == ("decided", "why", {"k": 1})
    assert draft.sources == (source,)
    assert (draft.tags, draft.supersedes, draft.see_also) == (("a", "b"), ("x",), ("y",))
    assert draft.occurred_at == when
