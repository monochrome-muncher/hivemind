"""The canonical wire mappers (ADR 0084): domain objects -> JSON-ready
dicts, in one neutral module (no FastAPI, no MCP).

The MCP surface imports these; the REST pydantic models (api/schemas.py)
are the HTTP response models, and ``tests/unit/test_wire_contract.py``
pins that the two sides agree field-for-field. A wire-shape change
touches a table in that test, this module, and the matching model.
"""

from __future__ import annotations

from hivemind.domain.entry import Entry
from hivemind.domain.feedback import FeedbackCounts, FeedbackSummary
from hivemind.services.chain import EntryLinks
from hivemind.services.governance import RelatedEntry
from hivemind.services.search import Hit
from hivemind.services.write_warnings import WriteWarning


def entry_dict(entry: Entry) -> dict[str, object]:
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


def list_item_dict(entry: Entry) -> dict[str, object]:
    """A compact ``hive_list`` item (no body, payload or sources; open it with
    ``hive_get``). No feedback counts: listing does not load them."""
    return {
        "id": entry.id,
        "kind": entry.kind.value,
        "summary": entry.summary,
        "tags": list(entry.tags),
        "author": entry.author,
        "agent": entry.agent,
        "occurred_at": entry.occurred_at.isoformat(),
        "created_at": entry.created_at.isoformat(),
        "scope": entry.scope,
        "fleet_id": entry.fleet_id,
        "state": entry.state.value,
        "superseded_by": entry.superseded_by,
    }


def hit_dict(hit: Hit) -> dict[str, object]:
    """A compact search hit (no body — progressive disclosure, SPEC §6.1)."""
    out: dict[str, object] = {
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
        "feedback": counts_dict(hit.feedback),
    }
    if hit.last_confirmed_at is not None:  # omitted when there is none (SPEC §4.2)
        out["last_confirmed_at"] = hit.last_confirmed_at.isoformat()
    if hit.contested:  # the evidence fields are omitted when empty (SPEC §6.1)
        out["contested"] = True
    if hit.correction is not None:
        out["correction"] = {
            "verdict": hit.correction.verdict.value,
            "note": hit.correction.note,
            "reporter": hit.correction.reporter,
            "updated_at": hit.correction.updated_at.isoformat(),
        }
    if hit.supersedes:
        out["supersedes"] = hit.supersedes
    if hit.state is not None:
        out["state"] = hit.state.value
    if hit.similar:
        out["similar"] = [
            {"id": m.entry_id, "occurred_at": m.occurred_at.isoformat()} for m in hit.similar
        ]
    if hit.upstream_changed:
        out["upstream_changed"] = [
            {"id": c.id, "change": c.change.value} for c in hit.upstream_changed
        ]
    return out


def related_dict(related: RelatedEntry) -> dict[str, object]:
    """A nearby existing entry in a write's reply (ADR 0052): compact, like a hit,
    with its tags, feedback counts and whether the writer wrote it."""
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
        "tags": list(entry.tags),
        "feedback": counts_dict(related.feedback),
        "same_author": related.same_author,
    }


def warning_dict(warning: WriteWarning) -> dict[str, str]:
    return {"code": warning.code, "message": warning.message}


def link_dict(entry: Entry) -> dict[str, object]:
    """A linked entry on a read (ADR 0057): compact, with its state."""
    return {
        "id": entry.id,
        "kind": entry.kind.value,
        "summary": entry.summary,
        "author": entry.author,
        "state": entry.state.value,
        "fleet_id": entry.fleet_id,
    }


def links_dict(links: EntryLinks) -> dict[str, object]:
    return {
        "see_also": [link_dict(e) for e in links.see_also],
        "linked_from": [link_dict(e) for e in links.linked_from],
    }


def counts_dict(counts: FeedbackCounts) -> dict[str, int]:
    helpful, stale, wrong = counts
    return {"helpful": helpful, "stale": stale, "wrong": wrong}


def feedback_dict(summary: FeedbackSummary) -> dict[str, object]:
    """An entry's feedback as a reader sees it (ADR 0051)."""
    out: dict[str, object] = {
        **counts_dict((summary.helpful, summary.stale, summary.wrong)),
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
    if summary.last_confirmed_at is not None:  # omitted when there is none (SPEC §4.2)
        out["last_confirmed_at"] = summary.last_confirmed_at.isoformat()
    return out
