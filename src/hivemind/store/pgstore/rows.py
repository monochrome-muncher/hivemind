"""Row mappers and JSON encoders and decoders for the asyncpg records."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import asyncpg

from hivemind.domain.access import Agent, AgentStatus, Fleet, TrustLevel
from hivemind.domain.agent_cleanup import AgentCleanupRecord, RestoredEntry
from hivemind.domain.audit import ActorKind, AuditAction, AuditRecord
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
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.domain.fleet_merge import DroppedPin, FleetMergeRecord
from hivemind.domain.pin import Pin
from hivemind.domain.succession import Succession


def encode_sources(sources: tuple[Source, ...]) -> list[dict[str, str]]:
    """``sources`` is a jsonb column: a JSON list of {type, ref} objects."""
    return [{"type": source.type.value, "ref": source.ref} for source in sources]


def encode_entities(entities: tuple[ExtractedEntity, ...]) -> list[dict[str, str]]:
    """``entities`` is a jsonb list of {name, kind}; lower-cased names go on ``entity_names``."""
    return [{"name": e.name, "kind": e.kind.value} for e in entities]


def row_to_feedback(row: asyncpg.Record) -> Feedback:
    return Feedback(
        entry_id=str(row["entry_id"]),
        user=row["user"],
        agent=row["agent"],
        verdict=Verdict(row["verdict"]),
        note=row["note"],
        updated_at=row["updated_at"],
    )


def ages(rows: list[asyncpg.Record], kind: str) -> tuple[float, ...]:
    return tuple(float(r["age"]) for r in rows if r["kind"] == kind)


def row_to_pin(row: asyncpg.Record) -> Pin:
    return Pin(
        fleet_id=row["fleet_id"],
        entry_id=str(row["entry_id"]),
        pinned_by=row["pinned_by"],
        pinned_at=row["pinned_at"],
    )


def _decode_sources(raw: Any) -> tuple[Source, ...]:
    """Decode the jsonb ``sources`` column into ``Source`` objects."""
    if not raw:
        return ()
    items = _jsonb_value(raw)
    return tuple(Source(type=SourceType(item["type"]), ref=item["ref"]) for item in items)


def _decode_entities(raw: Any) -> tuple[ExtractedEntity, ...]:
    """Decode the jsonb ``entities`` column."""
    if not raw:
        return ()
    items = _jsonb_value(raw)
    return tuple(
        ExtractedEntity(name=item["name"], kind=EntityKind(item["kind"])) for item in items
    )


def row_to_entry(row: asyncpg.Record) -> Entry:
    payload = row["payload"]  # jsonb: native dict (or None)
    return Entry(
        id=str(row["id"]),
        kind=Kind(row["kind"]),
        summary=row["summary"],
        author=row["author"],
        agent=row["agent"],
        occurred_at=row["occurred_at"],
        created_at=row["created_at"],
        body=row["body"],
        payload=payload,
        sources=_decode_sources(row["sources"]),
        tags=tuple(row["tags"]) if row["tags"] else (),
        importance=row["importance"],
        importance_source=ImportanceSource(row["importance_source"]),
        scope=row["scope"],
        fleet_id=str(row["fleet_id"]) if row["fleet_id"] else None,
        embedding_model=row["embedding_model"],
        state=EntryState(row["state"]),
        superseded_by=str(row["superseded_by"]) if row["superseded_by"] else None,
        withdrawn_reason=row["withdrawn_reason"],
        redacted_at=row["redacted_at"],
        withdrawn_at=row["withdrawn_at"],
        entities=_decode_entities(row["entities"]),
        entities_model=row["entities_model"],
    )


def row_to_fleet(row: asyncpg.Record) -> Fleet:
    """Map a ``fleets`` row to the domain ``Fleet``."""
    return Fleet(
        id=str(row["id"]),
        name=row["name"],
        created_at=row["created_at"],
        merged_into=str(row["merged_into"]) if row["merged_into"] is not None else None,
        merged_at=row["merged_at"],
    )


def _jsonb_value(raw: Any, expected: type[list[Any]] | type[dict[str, Any]] = list) -> Any:
    """A jsonb column as native Python (asyncpg decodes it; the text branch is for raw-text
    drivers)."""
    return raw if isinstance(raw, expected) else json.loads(raw)


def _decode_dropped(raw: Any) -> tuple[DroppedPin, ...]:
    """Decode a jsonb list written by ``dropped_json``."""
    return tuple(
        DroppedPin(
            d["fleet_id"], d["entry_id"], d["pinned_by"], datetime.fromisoformat(d["pinned_at"])
        )
        for d in _jsonb_value(raw)
    )


def dropped_json(drops: Sequence[DroppedPin]) -> str:
    """The jsonb for pins a merge, move or cleanup unpinned."""
    return json.dumps(
        [
            {
                "fleet_id": d.fleet_id,
                "entry_id": d.entry_id,
                "pinned_by": d.pinned_by,
                "pinned_at": d.pinned_at.isoformat(),
            }
            for d in drops
        ]
    )


def row_to_merge(row: asyncpg.Record) -> FleetMergeRecord:
    """Map a ``fleet_merges`` row to the domain record."""
    return FleetMergeRecord(
        id=str(row["id"]),
        source_fleet_id=str(row["source_fleet_id"]),
        target_fleet_id=str(row["target_fleet_id"]),
        merged_by=row["merged_by"],
        merged_at=row["merged_at"],
        moved_agents=tuple(row["moved_agents"]),
        dropped_pins=_decode_dropped(row["dropped_pins"]),
        undone_at=row["undone_at"],
        kind=row["kind"],
    )


def row_to_cleanup(row: asyncpg.Record) -> AgentCleanupRecord:
    """Map an ``agent_cleanups`` row to the domain record."""
    return AgentCleanupRecord(
        id=str(row["id"]),
        agent=row["agent"],
        since=row["since"],
        until=row["until"],
        cleaned_by=row["cleaned_by"],
        cleaned_at=row["cleaned_at"],
        withdrawn_ids=tuple(str(i) for i in row["withdrawn_ids"]),
        restored=tuple(
            RestoredEntry(r["entry_id"], r["superseded_by"], r.get("head", ""))
            for r in _jsonb_value(row["restored"])
        ),
        ratings_set_aside=int(row["ratings_set_aside"]),
        unpinned_pins=_decode_dropped(row["unpinned_pins"]),
        revoked=bool(row["revoked"]),
        self_service=bool(row["self_service"]),
        undone_at=row["undone_at"],
    )


def row_to_succession(row: asyncpg.Record) -> Succession:
    return Succession(
        retired_agent=row["retired_agent"],
        successor=row["successor"],
        by=row["by"],
        at=row["at"],
    )


def row_to_agent(row: asyncpg.Record) -> Agent:
    """Map an ``agents`` row to the domain ``Agent``."""
    return Agent(
        name=row["name"],
        status=AgentStatus(row["status"]),
        trust_level=TrustLevel(row["trust_level"]),
        home_fleet_id=str(row["home_fleet_id"]) if row["home_fleet_id"] else None,
        owner_alias=row["owner_alias"],
        created_at=row["created_at"],
        activated_at=row["activated_at"],
        # Only the agent list selects these; the other agent queries do not.
        last_used_at=row.get("last_used_at"),
        key_created_at=row.get("key_created_at"),
        key_locked_at=row.get("key_locked_at"),
    )


def row_to_audit(row: asyncpg.Record) -> AuditRecord:
    """Map an ``audit_log`` row to the domain ``AuditRecord``."""
    detail = row["detail"]
    return AuditRecord(
        id=str(row["id"]),
        occurred_at=row["occurred_at"],
        actor_kind=ActorKind(row["actor_kind"]),
        actor=row["actor"],
        action=AuditAction(row["action"]),
        target=row["target"],
        detail=_jsonb_value(detail, dict),
    )
