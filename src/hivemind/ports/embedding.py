"""The embedding seam."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from hivemind.domain.entry import (
    EntryDraft,
    embeddable_text,
)


@runtime_checkable
class Embedder(Protocol):
    """Embedding provider: text in, fixed-dimension vector out."""

    @property
    def dimension(self) -> int:
        """The fixed vector dimension (deploy-time decision, ADR 0005)."""
        ...

    @property
    def model_name(self) -> str:
        """The embedding model identifier (recorded per entry)."""
        ...

    async def embed_text(self, text: str) -> list[float]:
        """Embed a query or a standalone text."""
        ...

    async def embed_entry(self, draft: EntryDraft) -> list[float]:
        """Embed the text an entry is indexed by (SPEC.md §7)."""
        ...

    def entry_embeddable_text(self, draft: EntryDraft) -> str:
        """The exact text an entry is embedded from."""
        ...


def entry_embeddable_text(draft: EntryDraft) -> str:
    """The embedded text at the default budget (SPEC §7, ADR 0021), for embedders without
    ``Settings``."""
    return embeddable_text(draft.summary, draft.body)
