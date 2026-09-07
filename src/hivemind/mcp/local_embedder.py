"""A self-contained, deterministic embedder for the dev stdio server.

Real deployments use the operator-configured OpenAI-compatible endpoint
(SPEC §7, ADR 0005) — that integration lives in the embeddings lane and
is out of scope here. For the v1 dev stdio server (``main`` in
``server.py``) we need a fully local, dependency-free ``Embedder`` so the
server runs with no network access and no external model. This one hashes
tokens into a fixed-dimension vector (the same trick as the test fake),
which is "good enough" for a dev pool and keeps the code path honest.
"""

from __future__ import annotations

import hashlib

from hivemind.domain.entry import EntryDraft, embeddable_text


class LocalEmbedder:
    """Deterministic token-hash embedder (no I/O, no external model)."""

    def __init__(self, dimension: int = 256) -> None:
        self._dimension = dimension

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
        return embeddable_text(draft.summary, draft.body)
