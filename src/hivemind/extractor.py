"""OpenAI-compatible entity extractor: the real ``Extractor`` port (ADR 0016, SPEC §13).

Wraps an OpenAI-compatible ``/chat/completions`` HTTP endpoint (OpenAI,
a self-hosted vLLM/Ollama chat model — ADR 0016) with ``httpx``. The
client is injected so unit tests run against ``httpx.MockTransport``
with no network; when no client is given, one is built and owned by the
extractor (close it via ``aclose``).

The request shape follows the OpenAI chat-completions API:

    POST {base_url}/chat/completions
    {"model": "<model>",
     "messages": [{"role": "system", "content": <fixed prompt>},
                  {"role": "user", "content": <entry text>}],
     "temperature": 0}
    -> {"choices": [{"message": {"content": "[{...}, ...]"}}]}

The user message is the *same bounded text the embedder sees* (the
summary plus a bounded body prefix, ``embedding_prefix_chars`` — SPEC
§13.1), so extraction and embedding stay in lockstep.

The model output is validated **all-or-nothing** (SPEC §13.1): the
content must parse as a JSON array of ``{name, kind}`` objects where
``name`` is open vocabulary (trimmed, non-empty, ≤ 128 chars — the
domain's ``ExtractedEntity`` bounds) and ``kind`` is a member of the
closed ``EntityKind`` vocabulary; at most 10 entities; deduped
case-insensitively by lower-cased name. Any malformed output fails the
whole extraction — no partial salvage. The extraction is *best-effort*
in the write path (ADR 0016): a failure never blocks the write; the
entry simply lands without facets.

Transient failures — timeouts, connection/network errors, ``429`` and
5xx responses — are retried within a bounded budget (exponential
backoff; ADR 0014 pattern, ``HIVEMIND_EXTRACTOR_RETRIES``).
Deterministic failures — other 4xx, and client-side validation (bad
JSON, bad schema) — fail fast.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import httpx

from hivemind.config import Settings
from hivemind.domain.entry import EntityKind, EntryDraft, ExtractedEntity, embeddable_text

if TYPE_CHECKING:
    from hivemind.ports import Extractor


# SPEC §13.1: at most 10 entities per entry (the closed-kind vocabulary
# lives in the domain's ``EntityKind``; name bounds live in
# ``ExtractedEntity`` — one source of truth per limit).
_MAX_ENTITIES = 10

# The fixed prompt (ADR 0016): one prompt, one structured response — no
# agent framework, no multi-turn. It commits the model to the exact
# wire shape ``_parse_response`` validates.
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

    ``status`` carries the HTTP status code when the failure was an
    HTTP error; it is ``None`` for validation failures (malformed
    JSON, out-of-schema entities) that happen after a successful
    exchange.
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
        prefix_chars: int = 2048,
        timeout: float = 30.0,
        retries: int = 2,
        backoff: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """Create an extractor.

        Args:
            client: An injected ``httpx.AsyncClient`` (tests use a
                MockTransport transport). When ``None``, a client is
                built from ``base_url`` semantics and owned by the
                extractor (closed by ``aclose``).
            base_url: The endpoint base (e.g. ``http://host:8080/v1``);
                requests go to ``{base_url}/chat/completions``.
            api_key: The ``Authorization: Bearer`` credential; an empty
                string means no auth header is sent.
            model_name: The chat model identifier (recorded per entry
                as ``entities_model``, ADR 0016).
            prefix_chars: The bounded body prefix the extraction text is
                cut at (SPEC §13.1: the same bound the embedder uses,
                ``embedding_prefix_chars``).
            timeout: The request timeout used only when the extractor
                builds its own client.
            retries: The number of retries (after the first attempt)
                allowed for transient failures — timeouts, connection
                errors, ``429`` and 5xx (ADR 0014 pattern). ``0``
                disables retrying; the default is 2 (up to 3 attempts).
            backoff: The base backoff in seconds; retry *i* sleeps
                ``backoff * 2**i`` (0.5s, 1s, ...).
            sleep: The backoff sleep callable; defaults to
                ``asyncio.sleep`` (tests inject a recorder).
        """
        self._owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=timeout)
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model_name = model_name
        self._prefix_chars = prefix_chars
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
            model_name=settings.extractor_model,
            prefix_chars=settings.embedding_prefix_chars,
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
        """Extract entity facets from the text an entry is written from
        (SPEC §13.1: the summary + bounded body prefix — the same text
        the embedder sees)."""
        text = embeddable_text(draft.summary, draft.body, self._prefix_chars)
        return await self._extract(text)

    async def aclose(self) -> None:
        """Close the HTTP client, but only if this extractor built it.

        A caller-injected client stays open and is the caller's to
        close. Safe to call multiple times.
        """
        if self._owns_client:
            self._owns_client = False
            await self._client.aclose()

    async def _extract(self, text: str) -> tuple[ExtractedEntity, ...]:
        """POST one entry's text to the endpoint and return its facets.

        Transient failures — timeouts, connection/network errors,
        ``429`` and 5xx responses — are retried up to ``retries`` times
        with exponential backoff (ADR 0014 pattern). Deterministic
        failures — other 4xx, and client-side validation (bad JSON /
        out-of-schema entities) — fail fast.
        """
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload = self._payload(text)
        last: ExtractorError | None = None
        for attempt in range(self._retries + 1):
            try:
                response = await self._client.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
            except httpx.TimeoutException as exc:
                last = ExtractorError(f"extractor request timed out: {exc}")
            except httpx.NetworkError as exc:
                last = ExtractorError(f"extractor request failed: {exc}")
            except httpx.HTTPError as exc:
                raise ExtractorError(f"extractor request failed: {exc}") from exc
            else:
                if response.status_code >= 500 or response.status_code == 429:
                    last = ExtractorError(
                        f"extractor endpoint returned {response.status_code}",
                        status=response.status_code,
                    )
                elif response.is_error:
                    raise ExtractorError(
                        f"extractor endpoint returned {response.status_code}",
                        status=response.status_code,
                    )
                else:
                    return self._parse_response(response)
            if attempt < self._retries:
                await self._sleep(self._backoff * (2**attempt))
        assert last is not None  # only reachable when the budget is exhausted
        raise last

    def _payload(self, text: str) -> dict[str, Any]:
        """The chat-completions body: a fixed system prompt + the entry's
        bounded text as the user message, zero-temperature for stable
        schema output."""
        return {
            "model": self._model_name,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            "temperature": 0,
        }

    def _parse_response(self, response: httpx.Response) -> tuple[ExtractedEntity, ...]:
        """Validate the chat response all-or-nothing (SPEC §13.1).

        ``choices[0].message.content`` must hold a JSON array of
        ``{name, kind}`` objects that pass the domain's facet bounds;
        any deviation raises ``ExtractorError`` (no partial salvage —
        the whole extraction fails and the write lands without facets).
        """
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ExtractorError("malformed chat response (not JSON)") from exc
        try:
            content: Any = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ExtractorError("malformed chat response (missing content)") from exc
        if not isinstance(content, str):
            raise ExtractorError("extractor returned a non-string response content")
        try:
            items = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ExtractorError(
                f"extractor output is not valid JSON (content: {content[:200]!r})"
            ) from exc
        return self._validate(items)

    def _validate(self, items: Any) -> tuple[ExtractedEntity, ...]:
        """All-or-nothing schema validation (SPEC §13.1): a JSON array of
        at most 10 ``{name, kind}`` objects, with ``name`` open
        vocabulary (domain bounds) and ``kind`` the closed ``EntityKind``
        vocabulary; deduped case-insensitively by lower-cased name."""
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
            if not isinstance(name, str) or not name.strip():
                raise ExtractorError('each entity needs a non-empty string "name"')
            if not isinstance(kind, str):
                raise ExtractorError('each entity needs a string "kind"')
            try:
                entity = ExtractedEntity(name=name, kind=EntityKind(kind))
            except ValueError as exc:
                # Unknown kind, or a name breaking the domain's bounds
                # (≤ 128 chars after trimming) — malformed output.
                raise ExtractorError(f"invalid entity from the extractor: {exc}") from exc
            key = entity.name.lower()  # case-insensitive dedupe (SPEC §13.1)
            if key not in seen:
                seen.add(key)
                out.append(entity)
        return tuple(out)


def build_extractor(settings: Settings) -> Extractor | None:
    """The ``hivemind.extractor.build_extractor`` factory that the
    WriteService construction sites call to obtain the production
    extractor from service settings.

    Returns ``None`` when the extractor endpoint is unset (ADR 0016:
    the *optional* stance — a deployment without a chat model keeps the
    full system at zero LLM-extraction cost); callers treat ``None``
    as "extraction off".
    """
    if not settings.extractor_endpoint:
        return None
    return OpenAICompatExtractor.from_settings(settings)
