"""Embedding adapters: the ``Embedder`` port's production implementation.

The OpenAI-compatible endpoint is the only supported provider in v1
(ADR 0005): it covers OpenAI, a self-hosted vLLM, or Ollama through a
single deploy-time endpoint knob.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hivemind.embeddings.openai_compat import (
    EmbeddingError,
    OpenAICompatEmbedder,
)

if TYPE_CHECKING:
    from hivemind.config import Settings
    from hivemind.ports import Embedder


def build_embedder(settings: Settings) -> Embedder:
    """The ``hivemind.embeddings.build_embedder(settings)`` factory that
    the API's lazy builder (``hivemind.api.main._build_embedder``) calls
    to obtain the production embedder from service settings."""
    return OpenAICompatEmbedder.from_settings(settings)


__all__ = [
    "EmbeddingError",
    "OpenAICompatEmbedder",
    "build_embedder",
]
