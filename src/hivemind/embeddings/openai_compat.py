"""OpenAI-compatible embedder: the real ``Embedder`` port implementation.

Requests send ``dimensions`` = the deploy-time dim (ADR 0005), which
Matryoshka-capable providers truncate to; every response length is
validated, and an endpoint that cannot honour it fails as
``EmbeddingError``. Transient failures (timeouts, connection errors,
429, 5xx) are retried with backoff (ADR 0014); others fail fast.
"""

from __future__ import annotations

import asyncio
import math
from array import array
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from hivemind.config import Settings
from hivemind.domain.entry import DEFAULT_PREFIX_TOKENS, EntryDraft, embeddable_text
from hivemind.providers import (
    clean_api_key,
    clean_endpoint,
    default_deadline,
    post_with_retries,
)


async def _default_sleep(delay: float) -> None:
    """The production backoff sleep (injectable in tests)."""
    await asyncio.sleep(delay)


class EmbeddingError(Exception):
    """The embeddings endpoint could not produce a vector.

    ``status`` is the HTTP status, or ``None`` for validation failures.
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
        prefix_tokens: int = DEFAULT_PREFIX_TOKENS,
        timeout: float = 10.0,
        retries: int = 2,
        backoff: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        deadline: float | None = None,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        """Create an embedder.

        Args:
            client: An injected ``httpx.AsyncClient``; ``None`` builds
                one owned by the embedder (closed by ``aclose``).
            base_url: Requests go to ``{base_url}/embeddings``.
            api_key: Bearer credential; empty sends no auth header.
            model_name: Recorded per entry (ADR 0005).
            dim: The fixed vector dimension (ADR 0005).
            prefix_tokens: Body prefix bound in words (SPEC §7, ADR 0021).
            timeout: Used only when building the client.
            retries: Retries after the first attempt for transient
                failures; ``0`` disables.
            backoff: Base backoff; retry *i* sleeps ``backoff * 2**i``.
            sleep: Backoff sleep (tests inject a recorder).
            deadline: Wall-clock budget for one embed call, retries
                included (ADR 0041); ``None`` derives it.
            jitter: Returns [0, 1), scaling each backoff to [0.5x, 1x];
                defaults to ``random.random``.
        """
        self._owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=timeout)
        self._client = client
        self._base_url = clean_endpoint(base_url).rstrip("/")
        self._api_key = clean_api_key(api_key)
        self._deadline = (
            deadline if deadline is not None else default_deadline(timeout, retries, backoff)
        )
        self._jitter = jitter
        self._model_name = model_name
        self._dim = dim
        self._prefix_tokens = prefix_tokens
        self._retries = retries
        self._backoff = backoff
        self._sleep = sleep if sleep is not None else _default_sleep

    @classmethod
    def from_settings(cls, settings: Settings) -> OpenAICompatEmbedder:
        """Build an embedder from deployment settings (SPEC.md §7)."""
        return cls(
            client=None,
            base_url=settings.embedding_endpoint,
            api_key=settings.embedding_api_key,
            deadline=settings.embedding_deadline,
            model_name=settings.embedding_model,
            dim=settings.embedding_dim,
            prefix_tokens=settings.embedding_prefix_tokens,
            timeout=settings.embedding_timeout,
            retries=settings.embedding_retries,
        )

    @property
    def retries(self) -> int:
        """The retry budget for transient failures (ADR 0014)."""
        return self._retries

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
        """The exact text an entry is embedded from (SPEC.md §7, ADR 0021)."""
        return embeddable_text(draft.summary, draft.body, self._prefix_tokens)

    async def embed_text(self, text: str) -> list[float]:
        """Embed a query or a standalone text into a fixed-dim vector."""
        return await self._embed(text)

    async def embed_entry(self, draft: EntryDraft) -> list[float]:
        """Embed the text an entry is indexed by (SPEC.md §7)."""
        return await self._embed(self.entry_embeddable_text(draft))

    async def aclose(self) -> None:
        """Close the HTTP client if this embedder built it; idempotent."""
        if self._owns_client:
            self._owns_client = False
            await self._client.aclose()

    async def _embed(self, text: str) -> list[float]:
        """POST one text and return its vector (retry policy: see the
        module docstring)."""
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload: dict[str, Any] = {
            "model": self._model_name,
            "input": text,
            "dimensions": self._dim,
        }
        response = await post_with_retries(
            self._client,
            f"{self._base_url}/embeddings",
            payload=payload,
            headers=headers,
            retries=self._retries,
            backoff=self._backoff,
            sleep=self._sleep,
            deadline=self._deadline,
            label="embeddings request",
            error=lambda message, status: EmbeddingError(message, status=status),
            jitter=self._jitter,
        )
        vector = self._parse_response(response)
        if len(vector) != self._dim:
            raise EmbeddingError(f"expected {self._dim} dims, got {len(vector)}")
        return vector

    def _parse_response(self, response: httpx.Response) -> list[float]:
        """Extract and validate ``data[0].embedding`` from an OpenAI-shaped
        response: JSON, exactly one item (one input text), every value a
        finite number (no bool / str / NaN / Inf)."""
        try:
            body: Any = response.json()
        except ValueError, RecursionError:
            raise EmbeddingError("embeddings response is not JSON") from None
        try:
            data: Any = body["data"]
            if not isinstance(data, list) or len(data) != 1:
                raise EmbeddingError("embeddings response must hold exactly one item")
            item = data[0]
            if "index" in item and (item["index"] != 0 or isinstance(item["index"], bool)):
                raise EmbeddingError("embeddings response item has an unexpected index")
            raw: Any = item["embedding"]
            if not isinstance(raw, list):
                raise EmbeddingError("malformed embeddings response")
        except KeyError, IndexError, TypeError:
            raise EmbeddingError("malformed embeddings response") from None
        values: list[float] = []
        for v in raw:
            if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
                raise EmbeddingError("embeddings response holds a non-finite or non-numeric value")
            values.append(float(v))
        # pgvector stores float32 (ROADMAP 3.13): an out-of-range value
        # fails the write in Postgres, and an all-zero vector has NaN cosine
        # distances. Both are deterministic provider faults: fail fast.
        as_stored = array("f", values)
        if any(math.isinf(v) for v in as_stored):
            raise EmbeddingError("embeddings response holds a value outside the float32 range")
        if not any(as_stored):
            raise EmbeddingError("embeddings response is a zero vector")
        return values
