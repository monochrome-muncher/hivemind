"""The parts of a new entry that come from the caller's key, not its request.

REST ``POST /v1/entries`` and MCP ``hive_write`` both turn a write request
into an ``EntryDraft``; this is the one place that stamps the author,
applies the resolved scope and records the importance provenance, so the
two surfaces cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from hivemind.domain.entry import EntryDraft, ImportanceSource, Kind, Source
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
    """A validated draft for ``credential``'s write.

    ``author`` is the key's registered name (ADRs 0012, 0033), never the
    caller's say; ``agent`` is the resolved agent identity; ``resolution``
    comes from ``resolve_write_scope``. Omitted importance lands at the
    default with ``importance_source=default``, a supplied one is kept
    with ``caller`` (its 1..5 range is checked by ``EntryDraft``). Raises
    ``ValueError`` / ``InvalidInput`` for an invalid draft (SPEC §4.1).
    """
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
