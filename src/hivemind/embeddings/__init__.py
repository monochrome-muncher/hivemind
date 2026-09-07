"""Embedding adapters: the ``Embedder`` port's production implementation.

The OpenAI-compatible endpoint is the only supported provider in v1
(ADR 0005): it covers OpenAI, a self-hosted vLLM, or Ollama through a
single deploy-time endpoint knob.
"""

from hivemind.embeddings.openai_compat import (
    EmbeddingError,
    OpenAICompatEmbedder,
)

__all__ = [
    "EmbeddingError",
    "OpenAICompatEmbedder",
]