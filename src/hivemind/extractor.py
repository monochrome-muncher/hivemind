"""OpenAI-compatible entity extractor: the real ``Extractor`` port (ADR 0016, SPEC §13).

One zero-temperature ``/chat/completions`` call over the embedder's bounded text (SPEC §13.1),
output validated all-or-nothing. Retries as in ``providers`` (ADR 0014); the write path treats any
failure as "no facets" (ADR 0016).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import httpx

from hivemind.config import Settings
from hivemind.domain.entry import (
    DEFAULT_PREFIX_TOKENS,
    EntityKind,
    EntryDraft,
    ExtractedEntity,
    embeddable_text,
)
from hivemind.providers import (
    clean_api_key,
    clean_endpoint,
    default_deadline,
    post_with_retries,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from hivemind.ports import Extractor


# SPEC §13.1 (kinds and name bounds live in the domain).
_MAX_ENTITIES = 10

# Bounds on untrusted model output: larger is refused before parsing; logs carry a short snippet.
_MAX_CONTENT_CHARS = 64_000
_SNIPPET_CHARS = 80

_FENCE_OPEN_RE = re.compile(r"[A-Za-z0-9_-]*[ \t]*\r?\n")


def _unwrap(content: str) -> str:
    """Strip one leading ``<think>`` block and one markdown code fence; nothing else is salvaged.

    No backtracking regex: untrusted output could stall the event loop."""
    text = content.lstrip()
    if text.startswith("<think>"):
        end = text.find("</think>")
        if end != -1:
            text = text[end + len("</think>") :]
    text = text.strip()
    if len(text) >= 6 and text.startswith("```") and text.endswith("```"):
        inner = text[3:-3]
        opening = _FENCE_OPEN_RE.match(inner)
        if opening:
            return inner[opening.end() :]
    return text


def _clean_name(name: str) -> str:
    """Replace control characters and lone surrogates with a space and collapse whitespace.

    Names are untrusted output stored in Postgres and shown to agents. Format characters (ZWJ, soft
    hyphen) are kept."""
    kept = (" " if unicodedata.category(ch) in ("Cc", "Cs") else ch for ch in name)
    return " ".join("".join(kept).split())


# Fixed prompt (ADR 0016); the shape it commits the model to is what ``_validate`` checks.
_SYSTEM_PROMPT = """\
You are an entity-extraction tool for an organization's shared memory \
pool. You receive the text of one memory entry and identify the \
salient entities it references.

Respond with ONLY a JSON array of objects — no markdown, no prose, no \
explanation. Each object has exactly two fields:

  "name"  the entity's name (open vocabulary; short, readable, at most \
          128 characters)
  "kind"  exactly one of: person, organization, system, service, \
          artifact, concept

Rules:
- Return at most 10 entities.
- Do not repeat an entity (names match case-insensitively).
- If the entry references no entities, respond with an empty array: []
"""


async def _default_sleep(delay: float) -> None:
    await asyncio.sleep(delay)


class ExtractorError(Exception):
    """No validated facets; ``status`` is the HTTP status, ``None`` for validation failures."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class OpenAICompatExtractor:
    """An ``Extractor`` over an OpenAI-compatible ``/chat/completions`` endpoint."""

    def __init__(
        self,
        client: httpx.AsyncClient | None,
        base_url: str,
        api_key: str,
        model_name: str,
        *,
        prefix_tokens: int = DEFAULT_PREFIX_TOKENS,
        timeout: float = 30.0,
        retries: int = 2,
        backoff: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        deadline: float | None = None,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        """Create an extractor.

        ``client=None`` builds one the extractor owns (``aclose`` closes it; ``timeout`` applies
        only then). An empty ``api_key`` sends no auth header. ``model_name`` is recorded as
        ``entities_model``. ``prefix_tokens`` is a word bound as the embedder's (ADR 0021). Retry
        *i* sleeps ``backoff * 2**i`` scaled by ``jitter`` ([0, 1) to [0.5x, 1x]). ``deadline=None``
        derives it (ADR 0041).
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
        self._prefix_tokens = prefix_tokens
        self._retries = retries
        self._backoff = backoff
        self._sleep = sleep if sleep is not None else _default_sleep

    @classmethod
    def from_settings(cls, settings: Settings) -> OpenAICompatExtractor:
        return cls(
            client=None,
            base_url=settings.extractor_endpoint,
            api_key=settings.extractor_api_key,
            deadline=settings.extractor_deadline,
            model_name=settings.extractor_model,
            prefix_tokens=settings.embedding_prefix_tokens,
            timeout=settings.extractor_timeout,
            retries=settings.extractor_retries,
        )

    @property
    def retries(self) -> int:
        return self._retries

    @property
    def model_name(self) -> str:
        """The extractor model identifier (recorded per entry, ADR 0016)."""
        return self._model_name

    @property
    def base_url(self) -> str:
        return self._base_url

    async def extract_entry(self, draft: EntryDraft) -> tuple[ExtractedEntity, ...]:
        """Extract entity facets from the entry's embeddable text (SPEC §13.1)."""
        text = embeddable_text(draft.summary, draft.body, self._prefix_tokens)
        return await self._extract(text)

    async def aclose(self) -> None:
        """Close the HTTP client if this extractor built it; idempotent."""
        if self._owns_client:
            self._owns_client = False
            await self._client.aclose()

    async def _extract(self, text: str) -> tuple[ExtractedEntity, ...]:
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload = self._payload(text)
        response = await post_with_retries(
            self._client,
            f"{self._base_url}/chat/completions",
            payload=payload,
            headers=headers,
            retries=self._retries,
            backoff=self._backoff,
            sleep=self._sleep,
            deadline=self._deadline,
            label="extractor request",
            error=lambda message, status: ExtractorError(message, status=status),
            # Best-effort (ADR 0016): a failure never blocks the write.
            give_up_level=logging.WARNING,
            jitter=self._jitter,
        )
        return self._parse_response(response)

    def _payload(self, text: str) -> dict[str, Any]:
        """The chat-completions body; zero temperature for stable output."""
        return {
            "model": self._model_name,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            "temperature": 0,
        }

    def _parse_response(self, response: httpx.Response) -> tuple[ExtractedEntity, ...]:
        try:
            body = response.json()
        except ValueError, RecursionError:
            raise ExtractorError("malformed chat response (not JSON)") from None
        try:
            content: Any = body["choices"][0]["message"]["content"]
        except KeyError, IndexError, TypeError:
            raise ExtractorError("malformed chat response (missing content)") from None
        if not isinstance(content, str):
            raise ExtractorError("extractor returned a non-string response content")
        if len(content) > _MAX_CONTENT_CHARS:
            raise ExtractorError("extractor output is too large")
        content = _unwrap(content)
        try:
            items = json.loads(content)
        except ValueError, RecursionError:
            # A short, repr-escaped snippet only: the model echoes
            # entry-derived text, and a log line must not be forgeable.
            raise ExtractorError(
                f"extractor output is not valid JSON (content: {content[:_SNIPPET_CHARS]!r})"
            ) from None
        return self._validate(items)

    def _validate(self, items: Any) -> tuple[ExtractedEntity, ...]:
        """All-or-nothing (SPEC §13.1): at most 10 ``{name, kind}`` objects within the domain's
        bounds, deduped by name."""
        if not isinstance(items, list):
            raise ExtractorError("extractor output must be a JSON array of {name, kind} objects")
        if len(items) > _MAX_ENTITIES:
            raise ExtractorError(
                f"extractor returned {len(items)} entities; the cap is {_MAX_ENTITIES}"
            )
        seen: set[str] = set()
        out: list[ExtractedEntity] = []
        for item in items:
            if not isinstance(item, dict):
                raise ExtractorError('each entity must be an object with "name" and "kind" fields')
            name, kind = item.get("name"), item.get("kind")
            if isinstance(name, str):
                name = _clean_name(name)
            if not isinstance(name, str) or not name:
                raise ExtractorError('each entity needs a non-empty string "name"')
            if not isinstance(kind, str):
                raise ExtractorError('each entity needs a string "kind"')
            try:
                entity = ExtractedEntity(name=name, kind=EntityKind(kind))
            except ValueError as exc:
                # Unknown kind, or a name outside the domain's bounds.
                raise ExtractorError(f"invalid entity from the extractor: {exc}") from exc
            key = entity.name.lower()  # case-insensitive dedupe (SPEC §13.1, the store key)
            if key not in seen:
                seen.add(key)
                out.append(entity)
        return tuple(out)


def build_extractor(settings: Settings) -> Extractor | None:
    """The production extractor, or ``None`` (off) when the endpoint is unset (ADR 0016)."""
    if not settings.extractor_endpoint:
        return None
    return OpenAICompatExtractor.from_settings(settings)
