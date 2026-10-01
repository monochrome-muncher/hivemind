"""OpenAI-compatible entity extractor: the real ``Extractor`` port (ADR 0016, SPEC §13).

One zero-temperature ``/chat/completions`` call with a fixed system
prompt; the user message is the same bounded text the embedder sees
(SPEC §13.1). The output is validated **all-or-nothing** (see
``_validate``). Transient failures (timeouts, connection errors, 429,
5xx) are retried with backoff (ADR 0014 pattern); other failures fail
fast. The write path treats any failure as "no facets" (ADR 0016).
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

# Bounds on untrusted model output (PC-11): a response larger than this
# is refused before parsing; logs carry at most a short snippet of it.
_MAX_CONTENT_CHARS = 64_000
_SNIPPET_CHARS = 80

_FENCE_OPEN_RE = re.compile(r"[A-Za-z0-9_-]*[ \t]*\r?\n")


def _unwrap(content: str) -> str:
    """Strip one leading ``<think>...</think>`` block and one markdown
    code fence around the JSON; nothing else is salvaged.

    No backtracking regex: model output is untrusted and could stall the
    event loop."""
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
    """Replace C0/C1 control characters (NUL, newlines, ...) and lone
    surrogates with a space and collapse whitespace: facet names are
    untrusted model output, stored in Postgres and shown to other agents.
    Format characters (ZWJ/ZWNJ, soft hyphen) are legitimate text and kept."""
    kept = (" " if unicodedata.category(ch) in ("Cc", "Cs") else ch for ch in name)
    return " ".join("".join(kept).split())


# The fixed prompt (ADR 0016), committing the model to the shape
# ``_parse_response`` validates.
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
    """The production backoff sleep (injectable in tests)."""
    await asyncio.sleep(delay)


class ExtractorError(Exception):
    """The extractor endpoint could not produce validated facets.

    ``status`` is the HTTP status, or ``None`` for validation failures.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class OpenAICompatExtractor:
    """An ``Extractor`` backed by an OpenAI-compatible ``/chat/completions``
    endpoint."""

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

        Args:
            client: An injected ``httpx.AsyncClient``; ``None`` builds
                one owned by the extractor (closed by ``aclose``).
            base_url: Requests go to ``{base_url}/chat/completions``.
            api_key: Bearer credential; empty sends no auth header.
            model_name: Recorded per entry as ``entities_model``.
            prefix_tokens: Body prefix bound in words, as the embedder's
                (ADR 0021).
            timeout: Used only when building the client.
            retries: Retries after the first attempt for transient
                failures; ``0`` disables.
            backoff: Base backoff; retry *i* sleeps ``backoff * 2**i``.
            sleep: Backoff sleep (tests inject a recorder).
            deadline: Wall-clock budget for one extraction, retries
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
        self._prefix_tokens = prefix_tokens
        self._retries = retries
        self._backoff = backoff
        self._sleep = sleep if sleep is not None else _default_sleep

    @classmethod
    def from_settings(cls, settings: Settings) -> OpenAICompatExtractor:
        """Build an extractor from deployment settings (ADR 0016, SPEC §13)."""
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
        """The retry budget for transient failures (ADR 0014 pattern)."""
        return self._retries

    @property
    def model_name(self) -> str:
        """The extractor model identifier (recorded per entry, ADR 0016)."""
        return self._model_name

    @property
    def base_url(self) -> str:
        """The endpoint base requests are sent to (e.g. ``http://h/v1``)."""
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
        """POST one entry's text and return its facets (retry policy: see
        the module docstring)."""
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
        """The chat-completions body (zero temperature for stable output)."""
        return {
            "model": self._model_name,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            "temperature": 0,
        }

    def _parse_response(self, response: httpx.Response) -> tuple[ExtractedEntity, ...]:
        """Parse ``choices[0].message.content`` and validate it; any
        deviation raises ``ExtractorError``."""
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
        """All-or-nothing (SPEC §13.1): a JSON array of at most 10
        ``{name, kind}`` objects within the domain's bounds, deduped
        case-insensitively by name."""
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
    """The production extractor, or ``None`` (extraction off) when the
    endpoint is unset (ADR 0016)."""
    if not settings.extractor_endpoint:
        return None
    return OpenAICompatExtractor.from_settings(settings)
