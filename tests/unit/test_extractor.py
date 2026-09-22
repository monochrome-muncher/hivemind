"""Unit tests for the OpenAI-compatible extractor (the real ``Extractor`` port).

The extractor is exercised through an injected ``httpx.MockTransport`` —
no network, no real LLM. The seam is the ``Extractor`` port: behavior
is asserted in terms of the wire request (endpoint, model, messages,
auth header) and the validated facet result.

Expected results are derived from literal OpenAI-chat-shape responses
(``{"choices": [{"message": {"content": "<json array>"}}]}``), never
values the model would "recompute". Validation is all-or-nothing
(SPEC §13.1): any malformed output fails the whole extraction — no
partial salvage.
"""

from __future__ import annotations

import json

import httpx
import pytest

from hivemind.config import Settings
from hivemind.domain.entry import (
    DEFAULT_PREFIX_TOKENS,
    EntityKind,
    EntryDraft,
    ExtractedEntity,
    Kind,
)
from hivemind.embeddings.openai_compat import OpenAICompatEmbedder
from hivemind.extractor import (
    ExtractorError,
    OpenAICompatExtractor,
    build_extractor,
)
from hivemind.ports import Extractor

BASE_URL = "http://chat.test/v1"


def _entities_json(entities: list[dict[str, str]]) -> str:
    return json.dumps(entities)


def _chat_response(content: str, status: int = 200) -> httpx.Response:
    """An OpenAI chat-completions response carrying ``content``."""
    return httpx.Response(
        status,
        json={
            "id": "chatcmpl-1",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": content}},
            ],
        },
    )


class MockEnv:
    """A recording ``httpx.MockTransport`` environment for one extractor."""

    def __init__(self, handler=None) -> None:
        self.handler = handler or _default_ok_handler
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict[str, object]] = []

    def client(self) -> httpx.AsyncClient:
        def transport_handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.content:
                self.bodies.append(json.loads(request.content))
            return self.handler(request)

        return httpx.AsyncClient(transport=httpx.MockTransport(transport_handler))

    @property
    def last_body(self) -> dict[str, object]:
        return self.bodies[-1]

    def script(self, outcomes: list[object]) -> None:
        """Serve ``outcomes`` in order (last repeats); a non-``Response``
        outcome is raised as the transport error/exception."""

        def handler(request: httpx.Request) -> httpx.Response:
            # The current request is already appended by ``client`` before
            # the handler runs, so the call index is ``len(requests) - 1``.
            outcome = outcomes[min(len(self.requests) - 1, len(outcomes) - 1)]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome  # httpx.Response

        self.handler = handler


def _default_ok_handler(request: httpx.Request) -> httpx.Response:
    return _chat_response(_entities_json([{"name": "Postgres", "kind": "system"}]))


def make_draft(summary: str = "Postgres pool exhausted", body: str | None = None) -> EntryDraft:
    return EntryDraft(
        kind=Kind.INSIGHT,
        summary=summary,
        author="alice",
        agent="claude-code",
        body=body,
    )


def make_extractor(
    env: MockEnv,
    base_url: str = BASE_URL,
    api_key: str = "sk-test",
    model_name: str = "qwen3.8-27b",
    prefix_tokens: int = DEFAULT_PREFIX_TOKENS,
    retries: int = 2,
    backoff: float = 0.5,
    sleep=None,
) -> OpenAICompatExtractor:
    return OpenAICompatExtractor(
        client=env.client(),
        base_url=base_url,
        api_key=api_key,
        model_name=model_name,
        prefix_tokens=prefix_tokens,
        retries=retries,
        backoff=backoff,
        sleep=sleep,
    )


class TestEmbedderExtractorLockstep:
    """ADR 0016 / SPEC §13.1: extraction and embedding read the **same**
    bounded text, from the one ``embedding_prefix_tokens`` budget.

    This invariant was silently broken before ADR 0021: the extractor
    honoured the setting, the embedder ignored it (it called the domain
    helper with no bound argument), so the two diverged the moment anyone
    moved the knob off its default. Nothing pinned it. This does.

    The assertion is made on what actually went over the wire, for both
    components built from **one** ``Settings`` object — so a call site
    that drops the bound again fails here.
    """

    @staticmethod
    def _draft() -> EntryDraft:
        return EntryDraft(
            kind=Kind.INSIGHT,
            summary="pool exhausted",
            author="alice",
            agent="claude-code",
            body="one two three four\n\nfive six seven eight nine ten",
        )

    async def test_both_read_identical_text_from_one_settings_object(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if request.url.path.endswith("/embeddings"):
                sent["embedder"] = body["input"]
                return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]}]})
            sent["extractor"] = body["messages"][1]["content"]
            return httpx.Response(200, json={"choices": [{"message": {"content": "[]"}}]})

        shared = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        # Both factories build their own client; hand them this one so the
        # production wiring (``from_settings`` / ``build_extractor``) is what
        # is under test, not a hand-assembled pair.
        monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: shared)

        settings = Settings(
            embedding_endpoint="http://embed.test/v1",
            embedding_dim=4,
            embedding_prefix_tokens=6,
            extractor_endpoint=BASE_URL,
            extractor_model="qwen3.8-27b",
        )
        embedder = OpenAICompatEmbedder.from_settings(settings)
        extractor = build_extractor(settings)
        assert extractor is not None

        draft = self._draft()
        await embedder.embed_entry(draft)
        await extractor.extract_entry(draft)
        await shared.aclose()

        assert sent["embedder"] == sent["extractor"]
        # ...and the shared text is really the configured 6-word bound, so
        # a regression where BOTH sides ignore the setting cannot pass by
        # agreeing on the unbounded text.
        assert sent["embedder"] == "pool exhausted\none two three four\n\nfive six"


class TestBuildExtractor:
    def test_empty_endpoint_is_extraction_off(self) -> None:
        """``build_extractor`` returns ``None`` when the endpoint is unset
        (ADR 0016: optional — a deployment without a chat model keeps the
        full system at zero LLM-extraction cost)."""
        assert build_extractor(Settings(extractor_endpoint="")) is None

    def test_endpoint_set_returns_an_extractor(self) -> None:
        settings = Settings(extractor_endpoint="http://chat.test/v1", extractor_model="m")
        extractor = build_extractor(settings)
        assert isinstance(extractor, OpenAICompatExtractor)
        if extractor is not None:
            assert extractor.model_name == "m"


class TestExtractEntry:
    async def test_happy_path_returns_validated_facets(self) -> None:
        env = MockEnv()
        env.handler = lambda request: _chat_response(
            _entities_json(
                [
                    {"name": "Postgres", "kind": "system"},
                    {"name": "auth-service", "kind": "service"},
                ]
            )
        )
        extractor = make_extractor(env)
        result = await extractor.extract_entry(make_draft())
        assert result == (
            ExtractedEntity(name="Postgres", kind=EntityKind.SYSTEM),
            ExtractedEntity(name="auth-service", kind=EntityKind.SERVICE),
        )

    async def test_request_goes_to_the_chat_completions_endpoint(self) -> None:
        env = MockEnv()
        extractor = make_extractor(env)
        await extractor.extract_entry(make_draft())
        request = env.requests[-1]
        assert str(request.url) == f"{BASE_URL}/chat/completions"
        assert request.method == "POST"
        body = env.last_body
        assert body["model"] == "qwen3.8-27b"
        assert body["temperature"] == 0
        messages = body["messages"]
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

    async def test_user_message_is_the_bounded_embeddable_text(self) -> None:
        """SPEC §13.1: the extractor sees the same text the embedder does
        (summary + bounded body prefix via ``embedding_prefix_tokens`` —
        whitespace-delimited words, ADR 0021)."""
        env = MockEnv()
        extractor = make_extractor(env, prefix_tokens=3)
        draft = make_draft("pool exhausted", body="one two three four five six")
        await extractor.extract_entry(draft)
        user_message = env.last_body["messages"][1]["content"]
        assert user_message == "pool exhausted\none two three"  # 3-word prefix bound

    async def test_auth_header_present_when_key_configured(self) -> None:
        env = MockEnv()
        extractor = make_extractor(env, api_key="sk-test")
        await extractor.extract_entry(make_draft())
        assert env.requests[-1].headers.get("Authorization") == "Bearer sk-test"

    async def test_auth_header_absent_without_key(self) -> None:
        env = MockEnv()
        extractor = make_extractor(env, api_key="")
        await extractor.extract_entry(make_draft())
        assert "Authorization" not in env.requests[-1].headers

    async def test_empty_array_is_a_valid_empty_result(self) -> None:
        """An empty JSON array means "no entities found" — a valid result,
        not a failure (the entry lands with no facets, but the extraction
        succeeded)."""
        env = MockEnv()
        env.handler = lambda request: _chat_response("[]")
        extractor = make_extractor(env)
        assert await extractor.extract_entry(make_draft()) == ()


class TestValidation:
    """All-or-nothing validation (SPEC §13.1): any malformed output fails
    the whole extraction — no partial salvage. Validation failures are
    client-side, so they fail fast (no retry)."""

    async def test_malformed_json_fails_fast(self) -> None:
        env = MockEnv()
        env.handler = lambda request: _chat_response("not a json array")
        extractor = make_extractor(env)
        with pytest.raises(ExtractorError) as exc_info:
            await extractor.extract_entry(make_draft())
        assert exc_info.value.status is None  # validation, not HTTP
        assert len(env.requests) == 1  # not retried

    async def test_non_array_json_fails(self) -> None:
        env = MockEnv()
        env.handler = lambda request: _chat_response('{"name": "Postgres"}')
        extractor = make_extractor(env)
        with pytest.raises(ExtractorError):
            await extractor.extract_entry(make_draft())
        assert len(env.requests) == 1

    async def test_more_than_ten_entities_fails(self) -> None:
        env = MockEnv()
        entities = [{"name": f"e{i}", "kind": "concept"} for i in range(11)]
        env.handler = lambda request: _chat_response(_entities_json(entities))
        extractor = make_extractor(env)
        with pytest.raises(ExtractorError):
            await extractor.extract_entry(make_draft())
        assert len(env.requests) == 1

    async def test_ten_entities_is_the_cap(self) -> None:
        """Exactly 10 entities is valid (the cap, not over it)."""
        env = MockEnv()
        entities = [{"name": f"e{i}", "kind": "concept"} for i in range(10)]
        env.handler = lambda request: _chat_response(_entities_json(entities))
        extractor = make_extractor(env)
        assert len(await extractor.extract_entry(make_draft())) == 10

    async def test_invalid_kind_fails(self) -> None:
        env = MockEnv()
        env.handler = lambda request: _chat_response(
            _entities_json([{"name": "Postgres", "kind": "place"}])
        )
        extractor = make_extractor(env)
        with pytest.raises(ExtractorError):
            await extractor.extract_entry(make_draft())
        assert len(env.requests) == 1

    async def test_name_over_128_chars_fails(self) -> None:
        env = MockEnv()
        env.handler = lambda request: _chat_response(
            _entities_json([{"name": "x" * 129, "kind": "concept"}])
        )
        extractor = make_extractor(env)
        with pytest.raises(ExtractorError):
            await extractor.extract_entry(make_draft())
        assert len(env.requests) == 1

    async def test_name_of_128_chars_is_valid(self) -> None:
        env = MockEnv()
        name = "x" * 128
        env.handler = lambda request: _chat_response(
            _entities_json([{"name": name, "kind": "concept"}])
        )
        extractor = make_extractor(env)
        assert (await extractor.extract_entry(make_draft()))[0].name == name

    async def test_non_object_item_fails(self) -> None:
        env = MockEnv()
        env.handler = lambda request: _chat_response(_entities_json(["Postgres"]))
        extractor = make_extractor(env)
        with pytest.raises(ExtractorError):
            await extractor.extract_entry(make_draft())

    async def test_missing_fields_fail(self) -> None:
        env = MockEnv()
        env.handler = lambda request: _chat_response(_entities_json([{"name": "Postgres"}]))
        extractor = make_extractor(env)
        with pytest.raises(ExtractorError):
            await extractor.extract_entry(make_draft())

    async def test_dedupes_case_insensitive_by_lowercased_name(self) -> None:
        env = MockEnv()
        env.handler = lambda request: _chat_response(
            _entities_json(
                [
                    {"name": "Postgres", "kind": "system"},
                    {"name": "POSTGRES", "kind": "system"},
                    {"name": "postgres", "kind": "system"},
                ]
            )
        )
        extractor = make_extractor(env)
        result = await extractor.extract_entry(make_draft())
        assert len(result) == 1
        assert result[0].name == "Postgres"  # the first occurrence wins


class TestRetries:
    """Bounded retries on transient failures (ADR 0014 pattern): timeouts,
    connection errors, ``429`` and 5xx are retried with exponential
    backoff; deterministic 4xx and validation failures fail fast."""

    async def test_retries_transient_5xx_then_succeeds(self) -> None:
        env = MockEnv()
        env.script([httpx.Response(500, text="boom"), _chat_response("[]")])
        extractor = make_extractor(env)
        assert await extractor.extract_entry(make_draft()) == ()
        assert len(env.requests) == 2

    async def test_retries_429_then_succeeds(self) -> None:
        env = MockEnv()
        env.script([httpx.Response(429, text="slow down"), _chat_response("[]")])
        extractor = make_extractor(env)
        assert await extractor.extract_entry(make_draft()) == ()
        assert len(env.requests) == 2

    async def test_retries_timeout_then_succeeds(self) -> None:
        env = MockEnv()
        env.script([httpx.ReadTimeout("read timed out"), _chat_response("[]")])
        extractor = make_extractor(env)
        assert await extractor.extract_entry(make_draft()) == ()
        assert len(env.requests) == 2

    async def test_retries_connection_error_then_succeeds(self) -> None:
        env = MockEnv()
        env.script([httpx.ConnectError("refused"), _chat_response("[]")])
        extractor = make_extractor(env)
        assert await extractor.extract_entry(make_draft()) == ()
        assert len(env.requests) == 2

    async def test_non_transient_4xx_fails_fast(self) -> None:
        env = MockEnv()
        env.script([httpx.Response(400, text="bad request")])
        extractor = make_extractor(env)
        with pytest.raises(ExtractorError) as exc_info:
            await extractor.extract_entry(make_draft())
        assert exc_info.value.status == 400
        assert len(env.requests) == 1  # no retry

    async def test_exhausted_budget_raises_with_status(self) -> None:
        env = MockEnv()
        env.script([httpx.Response(500, text="boom")])  # 500 forever
        extractor = make_extractor(env, retries=2)
        with pytest.raises(ExtractorError) as exc_info:
            await extractor.extract_entry(make_draft())
        assert exc_info.value.status == 500
        assert len(env.requests) == 3  # initial attempt + 2 retries

    async def test_zero_retries_is_a_single_attempt(self) -> None:
        env = MockEnv()
        env.script([httpx.Response(500, text="boom")])
        extractor = make_extractor(env, retries=0)
        with pytest.raises(ExtractorError):
            await extractor.extract_entry(make_draft())
        assert len(env.requests) == 1

    async def test_backoff_doubles_per_retry(self) -> None:
        sleeps: list[float] = []

        async def record(delay: float) -> None:
            sleeps.append(delay)

        env = MockEnv()
        env.script([httpx.Response(503), httpx.Response(502), _chat_response("[]")])
        extractor = make_extractor(env, retries=2, backoff=0.5, sleep=record)
        await extractor.extract_entry(make_draft())
        assert sleeps == [0.5, 1.0]


class TestFactoryAndProtocol:
    async def test_from_settings_reads_the_extractor_knobs(self) -> None:
        settings = Settings(
            extractor_endpoint="http://e.test/v1",
            extractor_api_key="k2",
            extractor_model="model-x",
            extractor_retries=4,
        )
        extractor = OpenAICompatExtractor.from_settings(settings)
        try:
            assert extractor.model_name == "model-x"
            assert extractor.base_url == "http://e.test/v1"
            assert extractor.retries == 4
        finally:
            await extractor.aclose()  # the extractor owns the client it built

    async def test_satisfies_the_extractor_port(self) -> None:
        env = MockEnv()
        extractor = make_extractor(env)
        assert isinstance(extractor, Extractor)

    async def test_aclose_keeps_injected_client_open(self) -> None:
        """A caller-injected client is not closed by ``aclose``."""
        env = MockEnv()
        extractor = OpenAICompatExtractor(
            client=env.client(),
            base_url=BASE_URL,
            api_key="",
            model_name="m",
        )
        await extractor.aclose()
        assert await extractor.extract_entry(make_draft())  # still works
