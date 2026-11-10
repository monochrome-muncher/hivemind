"""A local, deterministic token-hash embedder for the dev stdio server (no network, no model).

Real deployments use the configured OpenAI-compatible endpoint (SPEC §7, ADR 0005).
"""

from __future__ import annotations

import hashlib

from hivemind.domain.entry import DEFAULT_PREFIX_TOKENS, EntryDraft, embeddable_text


class LocalEmbedder:
    """Deterministic token-hash embedder (no I/O, no external model)."""

    def __init__(self, dimension: int = 256, prefix_tokens: int = DEFAULT_PREFIX_TOKENS) -> None:
        self._dimension = dimension
        self._prefix_tokens = prefix_tokens

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def model_name(self) -> str:
        return "local-hash"

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self._dimension
        for token in text.lower().split():
            h = hashlib.sha256(token.encode()).digest()
            for i in range(self._dimension):
                byte = h[i % len(h)]
                sign = 1.0 if byte % 2 == 0 else -1.0
                vec[i] += sign * (byte / 255.0)
        norm = (sum(v * v for v in vec) ** 0.5) or 1.0
        return [v / norm for v in vec]

    async def embed_text(self, text: str) -> list[float]:
        return self._vector(text)

    async def embed_entry(self, draft: EntryDraft) -> list[float]:
        return self._vector(self.entry_embeddable_text(draft))

    def entry_embeddable_text(self, draft: EntryDraft) -> str:
        # Same prefix budget as production (ADR 0021).
        return embeddable_text(draft.summary, draft.body, self._prefix_tokens)
