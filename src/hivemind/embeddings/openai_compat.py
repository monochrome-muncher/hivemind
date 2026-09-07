"""OpenAI-compatible embedder: the real ``Embedder`` port implementation.

Wraps an OpenAI-compatible ``/embeddings`` HTTP endpoint (OpenAI,
self-hosted vLLM, Ollama — ADR 0005) with ``httpx``. The client is
injected so unit tests run against ``httpx.MockTransport`` with no
network; when no client is given, one is built and owned by the
embedder (close it via ``aclose``).

The request shape follows the OpenAI embeddings API:

    POST {base_url}/embeddings
    {"model": "<model>", "input": "<text>"}
    -> {"data": [{"embedding": [f, ...]}], ...}

Embedding model and dimension are recorded per entry (ADR 0005); the
dimension is a deploy-time decision and validated on every response.
"""

from __future__ import annotations

from typing import Any

import httpx

from hivemind.config import Settings
from hivemind.domain.entry import EntryDraft, embeddable_text


class EmbeddingError(Exception):
    """The embeddings endpoint could not produce a vector.

    ``status`` carries the HTTP status code when the failure was an
    HTTP error; it is ``None`` for validation failures (dimension
    mismatch, malformed body) that happen after a successful exchange.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class OpenAICompatEmbedder:
    """An ``Embedder`` backed by an OpenAI-compatible ``/embeddings`` endpoint."""

    def __init__(
        self,
        client: httpx.AsyncClient | None,
        base_url: str,
        api_key: str,
        model_name: str,
        dim: int,
        *,
        timeout: float = 10.0,
    ) -> None:
        """Create an embedder.

        Args:
            client: An injected ``httpx.AsyncClient`` (tests use a
                MockTransport transport). When ``None``, a client is
                built from ``base_url`` semantics and owned by the
                embedder (closed by ``aclose``).
            base_url: The endpoint base (e.g. ``http://host:8001/v1``);
                requests go to ``{base_url}/embeddings``.
            api_key: The ``Authorization: Bearer`` credential; an empty
                string means no auth header is sent.
            model_name: The embedding model identifier (recorded per
                entry, ADR 0005).
            dim: The fixed vector dimension (deploy-time decision,
                ADR 0005); responses with a different length are
                rejected.
            timeout: The request timeout used only when the embedder
                builds its own client.
        """
        self._owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=timeout)
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model_name = model_name
        self._dim = dim

    @classmethod
    def from_settings(cls, settings: Settings) -> OpenAICompatEmbedder:
        """Build an embedder from deployment settings (SPEC.md §7)."""
        return cls(
            client=None,
            base_url=settings.embedding_endpoint,
            api_key=settings.embedding_api_key,
            model_name=settings.embedding_model,
            dim=settings.embedding_dim,
        )

    @property
    def dimension(self) -> int:
        """The fixed vector dimension (deploy-time decision, ADR 0005)."""
        return self._dim

    @property
    def model_name(self) -> str:
        """The embedding model identifier (recorded per entry, ADR 0005)."""
        return self._model_name

    @property
    def base_url(self) -> str:
        """The endpoint base requests are sent to (e.g. ``http://h/v1``)."""
        return self._base_url

    def entry_embeddable_text(self, draft: EntryDraft) -> str:
        """The exact text an entry is embedded from (SPEC.md §7): the
        summary plus a bounded prefix of the body."""
        return embeddable_text(draft.summary, draft.body)

    async def embed_text(self, text: str) -> list[float]:
        """Embed a query or a standalone text into a fixed-dim vector."""
        return await self._embed(text)

    async def embed_entry(self, draft: EntryDraft) -> list[float]:
        """Embed the text an entry is indexed by (SPEC.md §7)."""
        return await self._embed(self.entry_embeddable_text(draft))

    async def aclose(self) -> None:
        """Close the HTTP client, but only if this embedder built it.

        A caller-injected client stays open and is the caller's to
        close. Safe to call multiple times.
        """
        if self._owns_client:
            self._owns_client = False
            await self._client.aclose()

    async def _embed(self, text: str) -> list[float]:
        """POST one text to the endpoint and return its vector."""
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload: dict[str, str] = {"model": self._model_name, "input": text}
        try:
            response = await self._client.post(
                f"{self._base_url}/embeddings",
                json=payload,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"embeddings request failed: {exc}") from exc

        if response.is_error:
            raise EmbeddingError(
                f"embeddings endpoint returned {response.status_code}",
                status=response.status_code,
            )
        vector = self._parse_response(response)
        if len(vector) != self._dim:
            raise EmbeddingError(
                f"expected {self._dim} dims, got {len(vector)}"
            )
        return vector

    def _parse_response(self, response: httpx.Response) -> list[float]:
        """Extract ``data[0].embedding`` from an OpenAI-shaped response."""
        body: Any = response.json()
        try:
            raw: Any = body["data"][0]["embedding"]
            values = [float(v) for v in raw]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise EmbeddingError("malformed embeddings response") from exc
        return values