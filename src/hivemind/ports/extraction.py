"""The entity extraction seam (optional, ADR 0016)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from hivemind.domain.entry import (
    EntryDraft,
    ExtractedEntity,
)


@runtime_checkable
class Extractor(Protocol):
    """Entity extractor (ADR 0016, SPEC §13): optional (no endpoint = off) and best-effort (a
    failure never blocks the write)."""

    @property
    def model_name(self) -> str:
        """The extractor model identifier (recorded per entry)."""
        ...

    async def extract_entry(self, draft: EntryDraft) -> tuple[ExtractedEntity, ...]:
        """Entity facets from the entry's embedded text (SPEC §13.1)."""
        ...
