"""MCP tool adapters for Hivemind (SPEC §5.2).

Each ``hive_*`` function is a thin adapter over the services layer:
parse arguments into the domain, call the service, return a JSON-ready
dict. Errors come back as an ``{"error": {"code", "message"}}`` envelope,
not raised, so a failed call is a readable tool result. The functions
take ``McpHivemind`` first so ``server.py`` can bind them and tests can
call them with fakes.
"""

from __future__ import annotations

import asyncio
import logging
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
from hivemind.domain.feedback import Feedback, FeedbackCounts, FeedbackSummary, Verdict
from hivemind.domain.validation import MAX_GET_IDS, InvalidInput, check_pagination
from hivemind.embeddings import EmbeddingError
from hivemind.ports import Credential, Store
from hivemind.services.access import AccessService, resolve_write_scope
from hivemind.services.chain import (
    EntryLinks,
    entries_links,
    entry_links,
    get_visible_entries,
    get_visible_entry,
    supersession_chain,
)
from hivemind.services.errors import PermissionDenied, SupersedeDenied
from hivemind.services.governance import GovernanceService, RelatedEntry, WriteService
from hivemind.services.search import Hit, SearchService
from hivemind.services.wiring import Services

logger = logging.getLogger(__name__)

# Error codes returned in the tool-result error envelope (SPEC §5).
ERR_INVALID_INPUT = "invalid_input"
ERR_NOT_FOUND = "not_found"
ERR_PERMISSION_DENIED = "permission_denied"
ERR_NOT_ACTIVE = "not_active"
ERR_AGENT_UNRESOLVED = "agent_unresolved"
ERR_SUPERSEDE_DENIED = "supersede_denied"
# The embedding provider is down/misconfigured: retry later (MCP-2, PC-2).
ERR_EMBEDDING_UNAVAILABLE = "embedding_unavailable"
# ADR 0048: hive_search falls back to keyword-only hits when the embedder fails.
DEGRADED_KEYWORD_ONLY = "keyword_only"
DEGRADED_NOTE = (
    "the embedding service is unavailable; these hits are keyword matches only, "
    "so related entries that share no words with the query may be missing"
)
ERR_UNAUTHENTICATED = "unauthenticated"  # ADR 0042: the key is gone / not resolvable
ERR_INVALID_VERDICT = "invalid_verdict"
ERR_NAME_CONFLICT = "name_conflict"
ERR_STORE_UNAVAILABLE = "store_unavailable"  # a store call timed out: retry shortly
STORE_UNAVAILABLE_MESSAGE = "the database is busy or unreachable; retry shortly"


@dataclass(frozen=True, slots=True)
class McpHivemind:
    """The object the tool functions operate on: the services, the store
    (for reads the services do not expose), and the acting ``credential``
    (SPEC §8.1, ADR 0008).
    """

    store: Store
    write_service: WriteService
    search_service: SearchService
    governance_service: GovernanceService
    access_service: AccessService
    search_config: SearchConfig
    credential: Credential

    @classmethod
    def from_services(cls, store: Store, services: Services, credential: Credential) -> McpHivemind:
        """Bind the shared services (``build_services``) to ``credential``."""
        return cls(
            store=store,
            write_service=services.write_service,
            search_service=services.search_service,
            governance_service=services.governance_service,
            access_service=services.access_service,
            search_config=services.search_config,
            credential=credential,
        )


# --------------------------------------------------------------------------- #
# (de)serialization + parsing helpers
# --------------------------------------------------------------------------- #


def _entry_dict(entry: Entry) -> dict[str, object]:
    """A full entry as a JSON-serializable dict (body included).
    ``entities_model`` is null when extraction was off or failed."""
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
        "scope": hit.scope,
        "fleet_id": hit.fleet_id,
        "feedback": _counts_dict(hit.feedback),
    }


def _related_dict(related: RelatedEntry) -> dict[str, object]:
    """A nearby existing entry in a write's reply (ADR 0052): compact, like a hit."""
    entry = related.entry
    return {
        "id": entry.id,
        "kind": entry.kind.value,
        "summary": entry.summary,
        "author": entry.author,
        "scope": entry.scope,
        "fleet_id": entry.fleet_id,
        "occurred_at": entry.occurred_at.isoformat(),
        "similarity": round(related.similarity, 3),
    }


def _link_dict(entry: Entry) -> dict[str, object]:
    """A linked entry on a read (ADR 0057): compact, with its state."""
    return {
        "id": entry.id,
        "kind": entry.kind.value,
        "summary": entry.summary,
        "author": entry.author,
        "state": entry.state.value,
        "fleet_id": entry.fleet_id,
    }


def _links_dict(links: EntryLinks) -> dict[str, object]:
    return {
        "see_also": [_link_dict(e) for e in links.see_also],
        "linked_from": [_link_dict(e) for e in links.linked_from],
    }


def _counts_dict(counts: FeedbackCounts) -> dict[str, int]:
    helpful, stale, wrong = counts
    return {"helpful": helpful, "stale": stale, "wrong": wrong}


def _feedback_dict(summary: FeedbackSummary) -> dict[str, object]:
    """An entry's feedback as a reader sees it (ADR 0051)."""
    return {
        **_counts_dict((summary.helpful, summary.stale, summary.wrong)),
        "recent": [
            {
                "verdict": fb.verdict.value,
                "note": fb.note,
                "reporter": fb.user,
                "updated_at": fb.updated_at.isoformat() if fb.updated_at else None,
            }
            for fb in summary.recent
        ],
    }


def _embedding_unavailable(exc: EmbeddingError) -> dict[str, object]:
    """A fixed message; the (sanitized) detail is logged server-side."""
    logger.warning("embedding unavailable: %s", exc)
    return _error(ERR_EMBEDDING_UNAVAILABLE, "the embedding service is unavailable; retry later")


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
        if not ref or not ref.strip():
            raise ValueError("source 'ref' must be a non-blank string")
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
    flagged: bool = False,
) -> EntryFilters:
    """Parse the shared filter parameters into an ``EntryFilters`` (SPEC §5.3)."""
    filters = EntryFilters(
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
        flagged=flagged,
    )
    filters.validate()  # ADR 0040: caller input, unlike service-built filters
    return filters


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
    agent: str | None = None,
    see_also: list[str] | None = None,
) -> dict[str, object]:
    """Write a distilled entry into the shared pool (SPEC §4.1, §5.2).

    ``kind`` is one of ``fact|insight|decision``; ``summary`` is required
    (it is the embedded text). ``occurred_at`` is the backdatable
    "memory date" (ISO-8601). Provenance comes from the key, never from
    the caller (ADRs 0012, 0033): ``author`` is the key's registered name
    and, for an agent key, ``agent`` is the key's agent identity. Only a
    legacy v1 key with no agent identity self-reports ``agent`` (no
    fabricated ``unknown`` identity — the write is rejected instead).

    ``supersedes``: every target must be **active**, readable by the
    caller, and reached by the new entry's audience (ADRs 0033, 0034); one
    bad target rejects the whole write with ``supersede_denied``. Only the
    current head of a chain is supersedable — if a target is already
    superseded or withdrawn, re-target the version that supersedes it.

    ``see_also``: up to 5 ids of entries the caller can read, stored as
    links (ADR 0057) that ``hive_get`` shows on both ends. An id the
    caller cannot read rejects the write as ``invalid_input``.

    ``importance``: omit it and the entry lands at the default (3) with
    ``importance_source=default``; supply it and the value is kept with
    ``importance_source=caller`` (ROADMAP §4.5) — the 1..5 range is
    still enforced.

    ``scope``: omit it and the entry lands at the highest scope the
    caller's trust level permits (L1 -> self; L2/L3 -> fleet; legacy
    and admin -> org) — ADR 0011. An explicit out-of-permission scope
    is rejected; a scope that is not one of ``self|fleet|org`` is
    ``invalid_input`` (a typo is not a trust-level problem).
    """
    cred = app.credential
    if scope is not None and scope not in ("self", "fleet", "org"):
        return _error(ERR_INVALID_INPUT, f"scope must be 'self', 'fleet' or 'org', got {scope!r}")
    resolved_agent = cred.agent_id or agent
    if resolved_agent is None:
        return _error(
            ERR_AGENT_UNRESOLVED,
            "agent identity is required: use an agent sub-key or pass 'agent'",
        )
    try:
        parsed_kind = Kind(kind)
        parsed_sources = _parse_sources(sources)
        # Scope and author come from the credential (ADRs 0011-0012).
        resolution = resolve_write_scope(cred, scope)
        resolved_author = cred.agent_name or cred.user_id
        # Importance provenance (ROADMAP §4.5); the range check is in EntryDraft.
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
            see_also=tuple(see_also or ()),
        )
    except PermissionDenied as exc:
        return _error(ERR_PERMISSION_DENIED, str(exc))
    except ValueError as exc:
        return _error(ERR_INVALID_INPUT, str(exc))
    try:
        written = await app.write_service.write_and_relate(draft, writer=cred.visibility())
    except SupersedeDenied as exc:
        return _error(ERR_SUPERSEDE_DENIED, str(exc))
    except EmbeddingError as exc:
        return _embedding_unavailable(exc)
    except ValueError as exc:
        return _error(ERR_INVALID_INPUT, str(exc))
    result = _entry_dict(written.entry)
    result["related"] = [_related_dict(r) for r in written.related]
    return result


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
    flagged: bool = False,
) -> dict[str, object]:
    """Hybrid search over the pool; returns compact hits (no bodies).

    Superseded / withdrawn entries are hidden unless ``include_inactive``
    (SPEC §6.3). Open an interesting hit with ``hive_get``. ``limit``/
    ``offset`` paginate the result (SPEC §5.3).

    If the embedding service is down the hits come from the keyword
    stream alone and the result carries ``degraded: "keyword_only"`` and
    a ``note`` (ADR 0048), instead of an ``embedding_unavailable`` error.

    ``entities`` (ADR 0016, SPEC §13) filters by machine-extracted
    entity names: AND-semantics, case-insensitive; kinds are display-only.

    ``limit`` must be 1..100 and ``offset`` 0..10000; out-of-range values
    are ``invalid_input`` (matching REST's 422). A search reaches at most
    ``2 x candidate_top_k`` ranked hits (SPEC §5.3): paging past that
    returns an empty page.
    """
    try:
        check_pagination(limit, offset)  # SPEC §5.3, the same rule as REST (ADR 0040)
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
            flagged=flagged,
        )
    except ValueError as exc:
        return _error(ERR_INVALID_INPUT, str(exc))
    try:
        result = await app.search_service.search_result(
            query, filters, limit, offset=offset, visibility=app.credential.visibility()
        )
    except InvalidInput as exc:
        return _error(ERR_INVALID_INPUT, str(exc))
    response: dict[str, object] = {
        "count": len(result.hits),
        "hits": [_hit_dict(h) for h in result.hits],
    }
    if result.degraded:
        # ADR 0048: the embedding service is down; these hits are keyword
        # matches only, so a miss here is weaker evidence than usual.
        response["degraded"] = DEGRADED_KEYWORD_ONLY
        response["note"] = DEGRADED_NOTE
    return response


async def hive_get(
    app: McpHivemind,
    entry_id: str = "",
    include_history: bool = False,
    entry_ids: list[str] | None = None,
) -> dict[str, object]:
    """Fetch a full entry (body included).

    With ``include_history`` the result additionally carries
    ``successors`` (newer versions) and ``superseded`` (older versions it
    replaced) — the supersession chain (SPEC §5.1 ``?history``).

    ``feedback`` carries the entry's verdict counts and its newest
    feedback rows, notes included (ADR 0051). ``see_also`` lists the
    entries it links to and ``linked_from`` the active entries that link
    to it, both limited to what the caller may read (ADR 0057).

    Follows readability (ADR 0033): an entry the caller may not see is
    ``not_found`` — the same answer as an unknown id — and the chain
    leaves out versions the caller may not see.

    ``entry_ids`` instead of ``entry_id`` reads up to 10 entries in one
    call (ADR 0055): ``entries`` in the order asked (repeats once), and
    ``not_found`` for ids that name nothing the caller may read.
    """
    if entry_ids is not None:
        if entry_id or include_history:
            return _error(
                ERR_INVALID_INPUT, "entry_ids cannot be combined with entry_id or include_history"
            )
        return await _hive_get_many(app, entry_ids)
    if not entry_id or not entry_id.strip():
        return _error(ERR_INVALID_INPUT, "entry_id is required (pass a hit's id)")
    entry = await get_visible_entry(app.store, entry_id, app.credential.visibility())
    if entry is None:
        return _error(ERR_NOT_FOUND, f"unknown entry: {entry_id}")
    summary, links = await asyncio.gather(
        app.governance_service.feedback_summary(entry.id),
        entry_links(app.store, entry.id, app.credential.visibility()),
    )
    result = _entry_dict(entry)
    result["feedback"] = _feedback_dict(summary)
    result.update(_links_dict(links))
    if include_history:
        successors, superseded = await supersession_chain(
            app.store, entry, visibility=app.credential.visibility()
        )
        result["successors"] = [_entry_dict(s) for s in successors]
        result["superseded"] = [_entry_dict(s) for s in superseded]
    return result


async def _hive_get_many(app: McpHivemind, entry_ids: list[str]) -> dict[str, object]:
    ids = list(dict.fromkeys(entry_ids))
    if not ids:
        return _error(ERR_INVALID_INPUT, "entry_ids must name at least one entry")
    if len(ids) > MAX_GET_IDS:
        return _error(ERR_INVALID_INPUT, f"entry_ids may name at most {MAX_GET_IDS} entries")
    found = await get_visible_entries(app.store, ids, app.credential.visibility())
    entries = [found[eid] for eid in ids if eid in found]
    visibility = app.credential.visibility()
    # A fixed number of store reads for the whole batch (ADR 0059).
    entry_ids = [e.id for e in entries]
    summaries, links = await asyncio.gather(
        app.governance_service.feedback_summaries(entry_ids),
        entries_links(app.store, entry_ids, visibility),
    )
    return {
        "entries": [
            {
                **_entry_dict(e),
                "feedback": _feedback_dict(summaries[e.id]),
                **_links_dict(links[e.id]),
            }
            for e in entries
        ],
        "not_found": [eid for eid in ids if eid not in found],
    }


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
    flagged: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, object]:
    """List / filter entries without a query (filter only, paginated).

    ``entities`` (ADR 0016, SPEC §13) filters by machine-extracted entity
    names: AND-semantics, case-insensitive; kinds are display-only.

    ``flagged`` keeps only entries reported stale or wrong at least once
    (ADR 0054): the queue of entries waiting to be superseded.

    ``limit`` must be 1..100 and ``offset`` 0..10000; out-of-range values
    are ``invalid_input`` (matching REST's 422).
    """
    try:
        check_pagination(limit, offset)  # SPEC §5.3, the same rule as REST (ADR 0040)
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
            flagged=flagged,
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
    except InvalidInput as exc:
        return _error(ERR_INVALID_INPUT, str(exc))
    except ValueError as exc:
        return _error(ERR_NOT_ACTIVE, str(exc))
    return _entry_dict(entry)


async def hive_pin(app: McpHivemind, entry_id: str, unpin: bool = False) -> dict[str, object]:
    """Pin an active fleet entry to its fleet's briefing, or ``unpin`` it
    (ADR 0058). A privileged agent of the entry's fleet or an admin may; a
    fleet holds at most 10 pins. An entry the caller may not read is
    ``not_found``."""
    try:
        if unpin:
            removed = await app.governance_service.unpin(app.credential, entry_id)
            return {"entry_id": entry_id, "pinned": False, "removed": removed}
        pin = await app.governance_service.pin(app.credential, entry_id)
    except PermissionDenied as exc:
        return _error(ERR_PERMISSION_DENIED, str(exc))
    except LookupError as exc:
        return _error(ERR_NOT_FOUND, str(exc))
    except InvalidInput as exc:
        return _error(ERR_INVALID_INPUT, str(exc))
    except ValueError as exc:
        return _error(ERR_NOT_ACTIVE, str(exc))
    return {
        "entry_id": pin.entry_id,
        "pinned": True,
        "fleet_id": pin.fleet_id,
        "pinned_by": pin.pinned_by,
        "pinned_at": pin.pinned_at.isoformat(),
    }


async def hive_pinned(app: McpHivemind, fleet_id: str | None = None) -> dict[str, object]:
    """A fleet's pinned entries, newest pin first (ADR 0058): your home
    fleet's unless ``fleet_id`` names another you may read. Each pin shows
    the current version of the pinned entry; ``pinned_id`` is the version
    that was pinned. Either id unpins it."""
    try:
        pinned = await app.governance_service.pinned(app.credential, fleet_id)
    except InvalidInput as exc:
        return _error(ERR_INVALID_INPUT, str(exc))
    return {
        "fleet_id": fleet_id or app.credential.home_fleet_id,
        "pins": [
            {
                "id": p.entry.id,
                "pinned_id": p.pin.entry_id,
                "kind": p.entry.kind.value,
                "summary": p.entry.summary,
                "author": p.entry.author,
                "state": p.entry.state.value,
                "pinned_by": p.pin.pinned_by,
                "pinned_at": p.pin.pinned_at.isoformat(),
            }
            for p in pinned
        ],
    }


async def hive_feedback(
    app: McpHivemind,
    entry_id: str = "",
    verdict: str = "",
    note: str | None = None,
    agent: str | None = None,
    entry_ids: list[str] | None = None,
) -> dict[str, object]:
    """Report ``helpful|stale|wrong`` on an entry the caller relied on.

    One row per (entry, user, agent); the latest verdict wins (SPEC §4.2).
    The acting credential supplies the reporter identity; a plain user key
    self-reports the agent instance via ``agent`` (SPEC §8.1).

    ``entry_ids`` instead of ``entry_id`` gives one verdict and note to up
    to 16 entries at once (ADR 0053): all must be readable, or nothing is
    recorded.
    """
    if entry_ids is not None:
        if entry_id:
            return _error(ERR_INVALID_INPUT, "pass entry_id or entry_ids, not both")
        return await _hive_feedback_many(app, entry_ids, verdict, note, agent)
    if not entry_id or not entry_id.strip():
        return _error(ERR_INVALID_INPUT, "entry_id is required (pass a hit's id)")
    try:
        parsed_verdict = Verdict(verdict)
    except ValueError:
        return _error(ERR_INVALID_VERDICT, f"verdict must be helpful|stale|wrong, got {verdict!r}")
    if not (app.credential.agent_id or agent):
        # The one condition the service's ValueError stands for (SPEC §8.1).
        return _error(
            ERR_AGENT_UNRESOLVED,
            "the caller's agent identity must be resolved before recording feedback",
        )
    try:
        outcome = await app.governance_service.record_feedback(
            app.credential, entry_id, parsed_verdict, note, agent=agent
        )
    except LookupError as exc:
        return _error(ERR_NOT_FOUND, str(exc))
    except ValueError as exc:  # any other rejected input is just that
        return _error(ERR_INVALID_INPUT, str(exc))
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


async def _hive_feedback_many(
    app: McpHivemind,
    entry_ids: list[str],
    verdict: str,
    note: str | None,
    agent: str | None,
) -> dict[str, object]:
    try:
        parsed_verdict = Verdict(verdict)
    except ValueError:
        return _error(ERR_INVALID_VERDICT, f"verdict must be helpful|stale|wrong, got {verdict!r}")
    if not (app.credential.agent_id or agent):
        return _error(
            ERR_AGENT_UNRESOLVED,
            "the caller's agent identity must be resolved before recording feedback",
        )
    try:
        outcomes = await app.governance_service.record_feedback_many(
            app.credential, entry_ids, parsed_verdict, note, agent=agent
        )
    except LookupError as exc:
        return _error(ERR_NOT_FOUND, str(exc))
    except ValueError as exc:
        return _error(ERR_INVALID_INPUT, str(exc))
    return {
        "verdict": parsed_verdict.value,
        "note": note,
        "results": [{"entry_id": o.feedback.entry_id, "quality": o.quality} for o in outcomes],
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
    (level 0, no fleet). Registering a name again with the **same**
    ``owner_alias`` answers with its current status (``already_registered``
    plus a ``message``: pending / active → ask the admin for the key /
    revoked); a name owned by another alias is a ``name_conflict`` that
    says nothing more (ADR 0039). The agent is dormant until an admin
    activates it (sets its trust level + home fleet and issues its key).
    """
    try:
        registration = await app.access_service.register(
            name, app.credential, owner_alias=owner_alias, org_only=True
        )
    except PermissionDenied as exc:
        return _error(ERR_PERMISSION_DENIED, str(exc))
    except InvalidInput as exc:  # a bad or reserved name is not a conflict (ADR 0040)
        return _error(ERR_INVALID_INPUT, str(exc))
    except ValueError as exc:
        return _error(ERR_NAME_CONFLICT, str(exc))
    agent = registration.agent
    return {
        "name": agent.name,
        "status": agent.status.value,
        "trust_level": agent.trust_level.value,
        "home_fleet_id": agent.home_fleet_id,
        "already_registered": registration.already_registered,
        "message": registration.message,
    }


async def hive_whoami(app: McpHivemind) -> dict[str, object]:
    """The calling key's standing (ADR 0030): what kind of key it is, the
    agent's name, status, trust level and home fleet, and what it may
    read and write. Any valid key may ask — the org key included, which
    is how an agent learns it still has to register."""
    standing = await app.access_service.whoami(app.credential)
    return standing.as_dict()
