"""The parts of a new entry that come from the caller's key, not its request.

The one place REST ``POST /v1/entries`` and MCP ``hive_write`` stamp the author, scope and
importance provenance, so the two surfaces cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from hivemind.domain.entry import EntryDraft, ImportanceSource, Kind, Source
from hivemind.domain.validation import MAX_IDENTITY_CHARS, check_text
from hivemind.ports import Credential
from hivemind.services.access import WriteResolution

# The importance an entry gets when the writer gives none (ROADMAP §4.5).
DEFAULT_IMPORTANCE = 3


def entry_draft(
    credential: Credential,
    resolution: WriteResolution,
    *,
    agent: str,
    kind: Kind,
    summary: str,
    body: str | None,
    payload: dict[str, Any] | None,
    sources: tuple[Source, ...],
    tags: Sequence[str],
    occurred_at: datetime | None,
    importance: int | None,
    supersedes: Sequence[str],
    see_also: Sequence[str],
) -> EntryDraft:
    """A validated draft for ``credential``'s write (SPEC §4.1). ``author`` is the key's
    registered name (ADRs 0012, 0033), never the caller's say; omitted importance gets the default
    with ``importance_source=default``."""
    if credential.agent_id is None:
        # A self-reported agent is free text every reader sees; a key's own identity may be a legacy
        # name.
        check_text(agent, "agent", MAX_IDENTITY_CHARS)
    if importance is None:
        importance, importance_source = DEFAULT_IMPORTANCE, ImportanceSource.DEFAULT
    else:
        importance_source = ImportanceSource.CALLER
    return EntryDraft(
        kind=kind,
        summary=summary,
        author=credential.agent_name or credential.user_id,
        agent=agent,
        body=body,
        payload=payload,
        sources=sources,
        tags=tuple(tags),
        occurred_at=occurred_at,
        importance=importance,
        importance_source=importance_source,
        scope=resolution.scope,
        fleet_id=resolution.fleet_id,
        supersedes=tuple(supersedes),
        see_also=tuple(see_also),
    )
