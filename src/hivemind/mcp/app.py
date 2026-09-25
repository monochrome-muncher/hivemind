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
    ImportanceSource,
    Kind,
    Source,
    SourceType,
)
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.ports import Credential, Store
from hivemind.services.access import AccessService, resolve_write_scope
from hivemind.services.chain import supersession_chain
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


@dataclass(frozen=True, slots=True)
class McpHivemind:
    """The object the tool functions operate on.

    Holds the services, the store (for ``hive_get`` / ``hive_list`` / the
    access-plane verbs, which the services do not expose), the acting
    ``credential`` (the agent identity all writes / governance act as,
    per SPEC §8.1 / ADR 0008), and the ``access_service`` (the
    registration / fleet / trust verbs, ADRs 0011-0012).
    """

    store: Store
    write_service: WriteService
    search_service: SearchService
    governance_service: GovernanceService
    access_service: AccessService
    search_config: SearchConfig
    credential: Credential


# --------------------------------------------------------------------------- #
# (de)serialization + parsing helpers
# --------------------------------------------------------------------------- #


def _entry_dict(entry: Entry) -> dict[str, object]:
    """A full entry as a JSON-serializable dict (body included).

    Carries the machine-extracted entity facets (ADR 0016, SPEC §13):
    ``entities`` (name + kind, display-only in v1) and ``entities_model``
    (the extractor provenance; null when extraction was off or failed).
    Also carries ``importance_source`` (ROADMAP §4.5): whether the
    writer supplied ``importance`` or it fell out of the default.
    """
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
        "importance_source": entry.importance_source.value,
        "scope": entry.scope,
        "fleet_id": entry.fleet_id,
        "state": entry.state.value,
        "superseded_by": entry.superseded_by,
        "withdrawn_reason": entry.withdrawn_reason,
        "entities": [{"name": e.name, "kind": e.kind.value} for e in entry.entities],
        "entities_model": entry.entities_model,
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
    entities: list[str] | None,
    scope: str | None,
    author: str | None,
    agent: str | None,
    occurred_from: str | None,
    occurred_to: str | None,
    created_from: str | None,
    created_to: str | None,
    include_inactive: bool,
) -> EntryFilters:
    """Parse the shared filter parameters into an ``EntryFilters`` (SPEC §5.3).

    ``entities`` (ADR 0016, SPEC §13) filters by machine-extracted entity
    names: AND-semantics, case-insensitive (the store layer matches on
    lower-cased names; kinds are display-only, not filterable in v1).
    """
    return EntryFilters(
        kind=_parse_kind(kind),
        tags=tuple(tags or ()),
        entities=tuple(entities or ()),
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

    Delegates to the shared ``supersession_chain`` service (SPEC.md
    §5.1 ``?history`` / §5.2 ``hive_get``) so REST and MCP walk the
    chain identically.
    """
    return await supersession_chain(app.store, entry)


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
    importance: int | None = None,
    scope: str | None = None,
    supersedes: list[str] | None = None,
    author: str | None = None,
    agent: str | None = None,
) -> dict[str, object]:
    """Write a distilled entry into the shared pool (SPEC §4.1, §5.2).

    ``kind`` is one of ``fact|insight|decision``; ``summary`` is required
    (it is the embedded text). ``occurred_at`` is the backdatable
    "memory date" (ISO-8601). Provenance falls back to the acting
    credential when ``author``/``agent`` are omitted (SPEC §8.1); a
    plain user key must self-report the agent instance (no fabricated
    ``unknown`` identity — the write is rejected instead).

    ``importance``: omit it and the entry lands at the default (3) with
    ``importance_source=default``; supply it and the value is kept with
    ``importance_source=caller`` (ROADMAP §4.5) — the 1..5 range is
    still enforced.

    ``scope``: omit it and the entry lands at the highest scope the
    caller's trust level permits (L1 -> self; L2/L3 -> fleet; legacy
    and admin -> org) — ADR 0011. An explicit out-of-permission scope
    is rejected.
    """
    cred = app.credential
    resolved_agent = agent or cred.agent_id
    if resolved_agent is None:
        return _error(
            ERR_AGENT_UNRESOLVED,
            "agent identity is required: use an agent sub-key or pass 'agent'",
        )
    try:
        parsed_kind = Kind(kind)
        parsed_sources = _parse_sources(sources)
        # The write-scope is resolved from the credential (ADRs 0011-0012):
        # the default is the highest scope the trust level permits, an
        # out-of-permission scope is rejected, and the entry's ``author``
        # is the agent's registered name (verified server-side, ADR 0012).
        resolution = resolve_write_scope(cred, scope)
        resolved_author = author or (cred.agent_name or cred.user_id)
        # ROADMAP §4.5: an omitted importance resolves to the default
        # (3) with provenance `default`; a supplied value keeps its
        # provenance `caller` (the 1..5 range is still enforced by
        # ``EntryDraft.__post_init__``).
        if importance is None:
            resolved_importance = 3
            importance_source = ImportanceSource.DEFAULT
        else:
            resolved_importance = importance
            importance_source = ImportanceSource.CALLER
        draft = EntryDraft(
            kind=parsed_kind,
            summary=summary,
            author=resolved_author,
            agent=resolved_agent,
            body=body,
            payload=payload,
            sources=parsed_sources,
            tags=tuple(tags or ()),
            occurred_at=_parse_dt(occurred_at, "occurred_at"),
            importance=resolved_importance,
            importance_source=importance_source,
            scope=resolution.scope,
            fleet_id=resolution.fleet_id,
            supersedes=tuple(supersedes or ()),
        )
    except PermissionDenied as exc:
        return _error(ERR_PERMISSION_DENIED, str(exc))
    except ValueError as exc:
        return _error(ERR_INVALID_INPUT, str(exc))
    try:
        entry = await app.write_service.write(draft)
    except ValueError as exc:
        return _error(ERR_INVALID_INPUT, str(exc))
    return _entry_dict(entry)


async def hive_search(
    app: McpHivemind,
    query: str,
    limit: int | None = None,
    offset: int | None = None,
    kind: str | None = None,
    tags: list[str] | None = None,
    entities: list[str] | None = None,
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
    (SPEC §6.3). Open an interesting hit with ``hive_get``. ``limit``/
    ``offset`` paginate the result (SPEC §5.3).

    ``entities`` (ADR 0016, SPEC §13) filters by machine-extracted
    entity names: AND-semantics, case-insensitive; kinds are display-only.
    """
    try:
        filters = _build_filters(
            kind=kind,
            tags=tags,
            entities=entities,
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
    hits = await app.search_service.search(
        query, filters, limit, offset=offset, visibility=app.credential.visibility()
    )
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
    if not entry_id or not entry_id.strip():
        return _error(ERR_INVALID_INPUT, "entry_id is required (pass a hit's id)")
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
    entities: list[str] | None = None,
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
    """List / filter entries without a query (filter only, paginated).

    ``entities`` (ADR 0016, SPEC §13) filters by machine-extracted entity
    names: AND-semantics, case-insensitive; kinds are display-only.
    """
    try:
        filters = _build_filters(
            kind=kind,
            tags=tags,
            entities=entities,
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
    entries = await app.store.list_entries(
        filters,
        limit=effective_limit,
        offset=offset,
        visibility=app.credential.visibility(),
    )
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
    agent: str | None = None,
) -> dict[str, object]:
    """Report ``helpful|stale|wrong`` on an entry the caller relied on.

    One row per (entry, user, agent); the latest verdict wins (SPEC §4.2).
    The acting credential supplies the reporter identity; a plain user key
    self-reports the agent instance via ``agent`` (SPEC §8.1).
    """
    if not entry_id or not entry_id.strip():
        return _error(ERR_INVALID_INPUT, "entry_id is required (pass a hit's id)")
    try:
        parsed_verdict = Verdict(verdict)
    except ValueError:
        return _error("invalid_verdict", f"verdict must be helpful|stale|wrong, got {verdict!r}")
    try:
        outcome = await app.governance_service.record_feedback(
            app.credential, entry_id, parsed_verdict, note, agent=agent
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


async def hive_register(
    app: McpHivemind,
    name: str,
    owner_alias: str | None = None,
) -> dict[str, object]:
    """Register (or re-register) an agent (ADR 0012).

    Gated on the **org key only** (SPEC §5.2: the MCP verb is the agent's
    first contact with Hivemind; the REST surface also accepts an admin
    key for human-driven registration). Creates a ``pending`` agent
    (level 0, no fleet). Re-registering a pending name is idempotent;
    an *active* name is a conflict (the name stays reserved — pick a new
    one, ADR 0012). The agent is dormant until an admin activates it
    (sets its trust level + home fleet and issues its key).
    """
    try:
        agent = await app.access_service.register(
            name, app.credential, owner_alias=owner_alias, org_only=True
        )
    except PermissionDenied as exc:
        return _error(ERR_PERMISSION_DENIED, str(exc))
    except ValueError as exc:
        return _error("name_conflict", str(exc))
    return {
        "name": agent.name,
        "status": agent.status.value,
        "trust_level": agent.trust_level.value,
        "home_fleet_id": agent.home_fleet_id,
    }


async def hive_whoami(app: McpHivemind) -> dict[str, object]:
    """The calling key's standing (ADR 0030): what kind of key it is, the
    agent's name, status, trust level and home fleet, and what it may
    read and write. Any valid key may ask — the org key included, which
    is how an agent learns it still has to register."""
    standing = await app.access_service.whoami(app.credential)
    return standing.as_dict()
