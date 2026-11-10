"""UUID helpers shared by several areas."""

from __future__ import annotations

from hivemind.domain.validation import canonical_uuid, is_valid_uuid


def valid_uuids(ids: list[str] | tuple[str, ...]) -> list[str]:
    """Keep only the IDs that parse as UUIDs (others simply don't exist)."""
    return [i for i in ids if is_valid_uuid(i)]


def canonical_uuids(ids: list[str]) -> list[str]:
    """The distinct well-formed IDs in Postgres's canonical text form, in order
    (Postgres reads upper-case and hyphen-less spellings as the same uuid)."""
    return list(dict.fromkeys(c for i in ids if (c := canonical_uuid(i)) is not None))
