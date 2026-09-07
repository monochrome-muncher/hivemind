"""MCP tool adapters for Hivemind (SPEC §5.2).

The six plain tool functions (``hive_write``, ``hive_search``,
``hive_get``, ``hive_list``, ``hive_withdraw``, ``hive_feedback``) are
the agent-facing verbs. Each is a thin, typed adapter over the services
layer: it parses its arguments into the domain (``EntryDraft``,
``EntryFilters``, ``Verdict``), calls the matching service, and maps the
result to a JSON-serializable dict. Errors are surfaced as a small
``{"error": {"code": ..., "message": ...}}`` envelope rather than
raised, so a failed call degrades to a readable tool result instead of
an MCP transport error.

The functions take ``McpHivemind`` as their first argument so the
MCP server can bind them into closures (see ``server.py``) while unit
tests can call them directly with fakes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from hivemind.config import SearchConfig
from hivemind.domain.entry import (
    Entry,
    EntryDraft,
    EntryFilters,
    Kind,
    Source,
    SourceType,
)
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.ports import Credential, Store
from hivemind.services.governance import (
    GovernanceService,
    PermissionDenied,
    WriteService,
)
from hivemind.services.search import Hit, SearchService

# Error codes returned in the tool-result error envelope (SPEC §5).
ERR_INVALID_INPUT = "invalid_input"
ERR_NOT_FOUND = "not_found"
ERR_PERMISSION_DENIED = "permission_denied"
ERR_NOT_ACTIVE = "not_active"
ERR_AGENT_UNRESOLVED = "agent_unresolved"

# Supersession chains in a corrupt pool could loop; bound both walks.
_MAX_SUPERSEDE_HOPS = 256
_REVERSE_SCAN_PAGE = 200


@dataclass(frozen=True, slots=True)
class McpHivemind:
    """The object the six tool functions operate on.

    Holds the three services plus the store (for ``hive_get`` /
    ``hive_list``, which the services do not expose) and the acting
    ``credential`` (the agent identity that all writes / governance act
    as, per SPEC §8.1 / ADR 0008).
    """

    store: Store
    write_service: WriteService
    search_service: SearchService
    governance_service: GovernanceService
    search_config: SearchConfig
    credential: Credential


# --------------------------------------------------------------------------- #
# (de)serialization + parsing helpers
# --------------------------------------------------------------------------- #


def _entry_dict(entry: Entry) -> dict[str, object]:
    """A full entry as a JSON-serializable dict (body included)."""
    return {
        "id": entry.id,
        "kind": entry.kind.value,
        "summary": entry.summary,
        "author": entry.author,
        "agent": entry.agent,
        "body": entry.body,
        "payload": entry.payload,
        "sources": [{"type": s.type.value, "ref": s.ref} for s in entry.sources],
        "tags": list(entry.tags),
        "importance": entry.importance,
        "scope": entry.scope,
        "state": entry.state.value,
        "superseded_by": entry.superseded_by,
        "withdrawn_reason": entry.withdrawn_reason,
        "occurred_at": entry.occurred_at.isoformat(),
        "created_at": entry.created_at.isoformat(),
    }


def _hit_dict(hit: Hit) -> dict[str, object]:
    """A compact search hit (no body — progressive disclosure, SPEC §6.1)."""
    return {
        "id": hit.entry_id,
        "kind": hit.kind.value,
        "summary": hit.summary,
        "tags": list(hit.tags),
        "author": hit.author,
        "agent": hit.agent,
        "occurred_at": hit.occurred_at.isoformat(),
        "score": hit.score,
    }


def _error(code: str, message: str) -> dict[str, object]:
    return {"error": {"code": code, "message": message}}


def _parse_dt(value: str | None, field_name: str) -> datetime | None:
    """Parse an optional ISO-8601 timestamp; naive values are treated as UTC."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp, got {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _parse_kind(value: str | None) -> Kind | None:
    if value is None:
        return None
    try:
        return Kind(value)
    except ValueError as exc:
        raise ValueError(f"kind must be one of fact|insight|decision, got {value!r}") from exc


def _parse_sources(raw: list[dict[str, str]] | None) -> tuple[Source, ...]:
    if not raw:
        return ()
    parsed: list[Source] = []
    for item in raw:
        try:
            source_type = SourceType(item.get("type", ""))
        except ValueError as exc:
            raise ValueError(
                f"source type must be one of path|url|session|other, got {item.get('type')!r}"
            ) from exc
        ref = item.get("ref")
        if not ref:
            raise ValueError("source 'ref' must be a non-empty string")
        parsed.append(Source(type=source_type, ref=ref))
    return tuple(parsed)


def _build_filters(
    *,
    kind: str | None,
    tags: list[str] | None,
    scope: str | None,
    author: str | None,
    agent: str | None,
    occurred_from: str | None,
    occurred_to: str | None,
    created_from: str | None,
    created_to: str | None,
    include_inactive: bool,
) -> EntryFilters:
    """Parse the shared filter parameters into an ``EntryFilters`` (SPEC §5.3)."""
    return EntryFilters(
        kind=_parse_kind(kind),
        tags=tuple(tags or ()),
        scope=scope,
        author=author,
        agent=agent,
        occurred_from=_parse_dt(occurred_from, "occurred_from"),
        occurred_to=_parse_dt(occurred_to, "occurred_to"),
        created_from=_parse_dt(created_from, "created_from"),
        created_to=_parse_dt(created_to, "created_to"),
        include_inactive=include_inactive,
    )


async def _supersession_chain(app: McpHivemind, entry: Entry) -> tuple[list[Entry], list[Entry]]:
    """Return ``(successors, superseded)`` for an entry's supersession chain.

    ``successors`` are the newer versions reachable by following
    ``superseded_by`` forward. ``superseded`` are the older versions this
    entry replaced. Both walks are bounded so a corrupt chain cannot loop.
    """
    successors: list[Entry] = []
    cursor = entry
    for _ in range(_MAX_SUPERSEDE_HOPS):
        next_id = cursor.superseded_by
        if next_id is None:
            break
        nxt = await app.store.get_entry(next_id)
        if nxt is None:
            break
        successors.append(nxt)
        cursor = nxt

    # The v1 stores have no reverse index, so walk backwards with a single
    # bounded paginated scan that builds a ``superseded_by -> [entries]`` map.
    superseded: list[Entry] = []
    reverse: dict[str, list[Entry]] = {}
    offset = 0
    while True:
        batch = await app.store.list_entries(
            EntryFilters(include_inactive=True),
            limit=_REVERSE_SCAN_PAGE,
            offset=offset,
        )
        if not batch:
            break
        for e in batch:
            if e.superseded_by is not None:
                reverse.setdefault(e.superseded_by, []).append(e)
        if len(batch) < _REVERSE_SCAN_PAGE:
            break
        offset += _REVERSE_SCAN_PAGE

    frontier = [entry]
    seen = {entry.id}
    for _ in range(_MAX_SUPERSEDE_HOPS):
        if not frontier:
            break
        next_frontier: list[Entry] = []
        for node in frontier:
            for pred in reverse.get(node.id, ()):
                if pred.id in seen:
                    continue
                superseded.append(pred)
                seen.add(pred.id)
                next_frontier.append(pred)
        frontier = next_frontier
    return successors, superseded


# --------------------------------------------------------------------------- #
# Tool functions (SPEC §5.2)
# --------------------------------------------------------------------------- #


async def hive_write(
    app: McpHivemind,
    kind: str,
    summary: str,
    body: str | None = None,
    payload: dict[str, Any] | None = None,
    sources: list[dict[str, str]] | None = None,
    tags: list[str] | None = None,
    occurred_at: str | None = None,
    importance: int = 3,
    scope: str = "org",
    supersedes: list[str] | None = None,
    author: str | None = None,
    agent: str | None = None,
) -> dict[str, object]:
    """Write a distilled entry into the shared pool (SPEC §4.1, §5.2).

    ``kind`` is one of ``fact|insight|decision``; ``summary`` is required
    (it is the embedded text). ``occurred_at`` is the backdatable
    "memory date" (ISO-8601). Provenance falls back to the acting
    credential when ``author``/``agent`` are omitted (SPEC §8.1).
    """
    cred = app.credential
    try:
        parsed_kind = Kind(kind)
        parsed_sources = _parse_sources(sources)
        draft = EntryDraft(
            kind=parsed_kind,
            summary=summary,
            author=author or cred.user_id,
            agent=agent or (cred.agent_id or "unknown"),
            body=body,
            payload=payload,
            sources=parsed_sources,
            tags=tuple(tags or ()),
            occurred_at=_parse_dt(occurred_at, "occurred_at"),
            importance=importance,
            scope=scope,
            supersedes=tuple(supersedes or ()),
        )
    except ValueError as exc:
        return _error(ERR_INVALID_INPUT, str(exc))
    try:
        entry = await app.write_service.write(draft)
    except LookupError as exc:
        return _error("supersede_target_missing", str(exc))
    except ValueError as exc:
        return _error(ERR_INVALID_INPUT, str(exc))
    return _entry_dict(entry)


async def hive_search(
    app: McpHivemind,
    query: str,
    limit: int | None = None,
    kind: str | None = None,
    tags: list[str] | None = None,
    scope: str | None = None,
    author: str | None = None,
    agent: str | None = None,
    occurred_from: str | None = None,
    occurred_to: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    include_inactive: bool = False,
) -> dict[str, object]:
    """Hybrid search over the pool; returns compact hits (no bodies).

    Superseded / withdrawn entries are hidden unless ``include_inactive``
    (SPEC §6.3). Open an interesting hit with ``hive_get``.
    """
    try:
        filters = _build_filters(
            kind=kind,
            tags=tags,
            scope=scope,
            author=author,
            agent=agent,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            created_from=created_from,
            created_to=created_to,
            include_inactive=include_inactive,
        )
    except ValueError as exc:
        return _error(ERR_INVALID_INPUT, str(exc))
    hits = await app.search_service.search(query, filters, limit)
    return {"count": len(hits), "hits": [_hit_dict(h) for h in hits]}


async def hive_get(
    app: McpHivemind,
    entry_id: str,
    include_history: bool = False,
) -> dict[str, object]:
    """Fetch a full entry (body included).

    With ``include_history`` the result additionally carries
    ``successors`` (newer versions) and ``superseded`` (older versions it
    replaced) — the supersession chain (SPEC §5.1 ``?history``).
    """
    entry = await app.store.get_entry(entry_id)
    if entry is None:
        return _error(ERR_NOT_FOUND, f"unknown entry: {entry_id}")
    result = _entry_dict(entry)
    if include_history:
        successors, superseded = await _supersession_chain(app, entry)
        result["successors"] = [_entry_dict(s) for s in successors]
        result["superseded"] = [_entry_dict(s) for s in superseded]
    return result


async def hive_list(
    app: McpHivemind,
    kind: str | None = None,
    tags: list[str] | None = None,
    scope: str | None = None,
    author: str | None = None,
    agent: str | None = None,
    occurred_from: str | None = None,
    occurred_to: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    include_inactive: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, object]:
    """List / filter entries without a query (filter only, paginated)."""
    try:
        filters = _build_filters(
            kind=kind,
            tags=tags,
            scope=scope,
            author=author,
            agent=agent,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            created_from=created_from,
            created_to=created_to,
            include_inactive=include_inactive,
        )
    except ValueError as exc:
        return _error(ERR_INVALID_INPUT, str(exc))
    effective_limit = limit if limit is not None else app.search_config.default_limit
    entries = await app.store.list_entries(filters, limit=effective_limit, offset=offset)
    return {"count": len(entries), "entries": [_entry_dict(e) for e in entries]}


async def hive_withdraw(
    app: McpHivemind,
    entry_id: str,
    reason: str | None = None,
) -> dict[str, object]:
    """Withdraw an entry (retract without replacing).

    Only the author or an admin may withdraw (SPEC §4.1, §8.1). The
    acting credential on ``McpHivemind`` decides the authorization.
    """
    try:
        entry = await app.governance_service.withdraw(app.credential, entry_id, reason)
    except PermissionDenied as exc:
        return _error(ERR_PERMISSION_DENIED, str(exc))
    except LookupError as exc:
        return _error(ERR_NOT_FOUND, str(exc))
    except ValueError as exc:
        return _error(ERR_NOT_ACTIVE, str(exc))
    return _entry_dict(entry)


async def hive_feedback(
    app: McpHivemind,
    entry_id: str,
    verdict: str,
    note: str | None = None,
) -> dict[str, object]:
    """Report ``helpful|stale|wrong`` on an entry the caller relied on.

    One row per (entry, user, agent); the latest verdict wins (SPEC §4.2).
    The acting credential supplies the reporter identity.
    """
    try:
        parsed_verdict = Verdict(verdict)
    except ValueError:
        return _error("invalid_verdict", f"verdict must be helpful|stale|wrong, got {verdict!r}")
    try:
        outcome = await app.governance_service.record_feedback(
            app.credential, entry_id, parsed_verdict, note
        )
    except LookupError as exc:
        return _error(ERR_NOT_FOUND, str(exc))
    except ValueError as exc:
        return _error(ERR_AGENT_UNRESOLVED, str(exc))
    fb: Feedback = outcome.feedback
    return {
        "entry_id": entry_id,
        "verdict": parsed_verdict.value,
        "note": note,
        "quality": outcome.quality,
        "feedback": {
            "entry_id": fb.entry_id,
            "user": fb.user,
            "agent": fb.agent,
            "verdict": fb.verdict.value,
            "note": fb.note,
            "updated_at": fb.updated_at.isoformat() if fb.updated_at else None,
        },
    }
