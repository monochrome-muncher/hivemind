"""Unit tests for the OpenAI-compatible embedder (the real ``Embedder`` port).

The embedder is exercised through an injected ``httpx.MockTransport`` —
no network, no real embeddings endpoint. The seam is the ``Embedder``
port: behavior is asserted in terms of the wire request (endpoint,
model, input, dimensions, auth header) and the returned vector.

Expected vectors are literal OpenAI-shape responses
(``{"data": [{"embedding": [...]}]}``), never values recomputed the way
the code does.
"""

from __future__ import annotations

import json

import httpx
import pytest

from hivemind.config import Settings
from hivemind.domain.entry import (
    DEFAULT_PREFIX_TOKENS,
    EntryDraft,
    Kind,
    Source,
    SourceType,
    embeddable_text,
)
from hivemind.embeddings.openai_compat import (
    EmbeddingError,
    OpenAICompatEmbedder,
)
from hivemind.ports import Embedder

BASE_URL = "http://embed.test/v1"
OK_VECTOR = [0.1, 0.2, 0.3, 0.4]


class MockEnv:
    """A recording ``httpx.MockTransport`` environment for one embedder."""

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


def _default_ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"data": [{"object": "embedding", "index": 0, "embedding": OK_VECTOR}]},
    )


def make_embedder(
    env: MockEnv,
    base_url: str = BASE_URL,
    api_key: str = "sk-test",
    model_name: str = "fake-model",
    dim: int = 4,
    prefix_tokens: int = DEFAULT_PREFIX_TOKENS,
    retries: int = 2,
    backoff: float = 0.5,
    sleep=None,
) -> OpenAICompatEmbedder:
    return OpenAICompatEmbedder(
        client=env.client(),
        base_url=base_url,
        api_key=api_key,
        model_name=model_name,
        dim=dim,
        prefix_tokens=prefix_tokens,
        retries=retries,
        backoff=backoff,
        sleep=sleep,
    )


class TestRetries:
    """Bounded retries on transient failures (ADR 0014): timeouts,
    connection/network errors, ``429`` and 5xx responses are retried with
    exponential backoff; deterministic 4xx and client-side validation
    failures fail fast (no retry)."""

    @staticmethod
    def _ok() -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"object": "embedding", "index": 0, "embedding": OK_VECTOR}]},
        )

    @staticmethod
    def _scripted_handler(env: MockEnv, outcomes: list[object]) -> None:
        """Serve ``outcomes`` in order (last repeats); a non-``Response``
        outcome is raised as the transport error/exception."""

        def handler(request: httpx.Request) -> httpx.Response:
            # The current request is already appended by ``MockEnv`` before
            # the handler runs, so the call index is ``len(requests) - 1``.
            outcome = outcomes[min(len(env.requests) - 1, len(outcomes) - 1)]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome  # httpx.Response

        env.handler = handler

    async def test_retries_transient_5xx_then_succeeds(self) -> None:
        env = MockEnv()
        self._scripted_handler(
            env, [httpx.Response(500, text="boom"), httpx.Response(503), self._ok()]
        )
        embedder = make_embedder(env)
        assert await embedder.embed_text("x") == OK_VECTOR
        assert len(env.requests) == 3  # two failures, one success

    async def test_retries_429_then_succeeds(self) -> None:
        env = MockEnv()
        self._scripted_handler(env, [httpx.Response(429, text="slow down"), self._ok()])
        embedder = make_embedder(env)
        assert await embedder.embed_text("x") == OK_VECTOR
        assert len(env.requests) == 2

    async def test_retries_connection_error_then_succeeds(self) -> None:
        env = MockEnv()
        self._scripted_handler(env, [httpx.ConnectError("connection refused"), self._ok()])
        embedder = make_embedder(env)
        assert await embedder.embed_text("x") == OK_VECTOR
        assert len(env.requests) == 2

    async def test_retries_timeout_then_succeeds(self) -> None:
        env = MockEnv()
        self._scripted_handler(env, [httpx.ReadTimeout("read timed out"), self._ok()])
        embedder = make_embedder(env)
        assert await embedder.embed_text("x") == OK_VECTOR
        assert len(env.requests) == 2

    async def test_exhausted_budget_raises_with_status(self) -> None:
        env = MockEnv()
        self._scripted_handler(env, [httpx.Response(500, text="boom")])  # 500 forever
        embedder = make_embedder(env, retries=2)
        with pytest.raises(EmbeddingError) as exc_info:
            await embedder.embed_text("x")
        assert exc_info.value.status == 500
        assert len(env.requests) == 3  # initial attempt + 2 retries

    async def test_default_budget_is_three_attempts(self) -> None:
        """Default ``retries=2``: three 5xx in a row exhaust the budget
        before the 4th (which would have succeeded)."""
        env = MockEnv()
        self._scripted_handler(
            env, [httpx.Response(500), httpx.Response(500), httpx.Response(500), self._ok()]
        )
        embedder = make_embedder(env)  # default retries=2
        with pytest.raises(EmbeddingError):
            await embedder.embed_text("x")
        assert len(env.requests) == 3  # the 4th (ok) attempt never happens

    async def test_non_transient_4xx_fails_fast(self) -> None:
        env = MockEnv()
        self._scripted_handler(env, [httpx.Response(401, text="bad key")])
        embedder = make_embedder(env)
        with pytest.raises(EmbeddingError) as exc_info:
            await embedder.embed_text("x")
        assert exc_info.value.status == 401
        assert len(env.requests) == 1  # no retry

    async def test_validation_failure_fails_fast_without_retry(self) -> None:
        env = MockEnv()  # 4-dim answer, ask for 2 dims -> validation failure
        embedder = make_embedder(env, dim=2)
        with pytest.raises(EmbeddingError) as exc_info:
            await embedder.embed_text("x")
        assert exc_info.value.status is None  # client-side validation, not HTTP
        assert len(env.requests) == 1  # not retried

    async def test_backoff_doubles_per_retry(self) -> None:
        sleeps: list[float] = []

        async def record(delay: float) -> None:
            sleeps.append(delay)

        env = MockEnv()
        self._scripted_handler(env, [httpx.Response(500), httpx.Response(502), self._ok()])
        embedder = make_embedder(env, retries=2, backoff=0.5, sleep=record)
        await embedder.embed_text("x")
        assert sleeps == [0.5, 1.0]  # base * 2**retry_index

    async def test_zero_retries_is_a_single_attempt(self) -> None:
        sleeps: list[float] = []

        async def record(delay: float) -> None:
            sleeps.append(delay)

        env = MockEnv()
        self._scripted_handler(env, [httpx.Response(500, text="boom")])
        embedder = make_embedder(env, retries=0, sleep=record)
        with pytest.raises(EmbeddingError):
            await embedder.embed_text("x")
        assert len(env.requests) == 1
        assert sleeps == []  # no attempt -> no backoff sleep

    async def test_from_settings_wires_the_retries_knob(self) -> None:
        settings = Settings(
            embedding_endpoint="http://e.test/v1",
            embedding_dim=8,
            embedding_retries=4,
        )
        embedder = OpenAICompatEmbedder.from_settings(settings)
        try:
            assert embedder.retries == 4
        finally:
            await embedder.aclose()


class TestEmbedText:
    async def test_happy_path_returns_the_embedding_vector(self) -> None:
        env = MockEnv()
        embedder = make_embedder(env)
        vec = await embedder.embed_text("churn cohorts")
        assert vec == OK_VECTOR

    async def test_request_goes_to_the_embeddings_endpoint(self) -> None:
        env = MockEnv()
        embedder = make_embedder(env)
        await embedder.embed_text("churn cohorts")
        request = env.requests[-1]
        assert str(request.url) == f"{BASE_URL}/embeddings"
        assert request.method == "POST"
        assert env.last_body == {
            "model": "fake-model",
            "input": "churn cohorts",
            "dimensions": 4,  # make_embedder's default dim
        }

    async def test_auth_header_present_when_key_configured(self) -> None:
        env = MockEnv()
        embedder = make_embedder(env, api_key="sk-test")
        await embedder.embed_text("x")
        assert env.requests[-1].headers.get("Authorization") == "Bearer sk-test"

    async def test_auth_header_absent_without_key(self) -> None:
        env = MockEnv()
        embedder = make_embedder(env, api_key="")
        await embedder.embed_text("x")
        assert "Authorization" not in env.requests[-1].headers

    async def test_dimension_mismatch_raises(self) -> None:
        env = MockEnv()  # answers with a 4-dim vector
        embedder = make_embedder(env, dim=2)
        with pytest.raises(EmbeddingError) as exc_info:
            await embedder.embed_text("x")
        # Validation failures carry no HTTP status.
        assert exc_info.value.status is None

    async def test_http_error_raises_with_status(self) -> None:
        env = MockEnv(handler=lambda request: httpx.Response(500, text="boom"))
        embedder = make_embedder(env)
        with pytest.raises(EmbeddingError) as exc_info:
            await embedder.embed_text("x")
        assert exc_info.value.status == 500


class TestEmbedEntry:
    def _long_body_draft(self) -> EntryDraft:
        return EntryDraft(
            kind=Kind.INSIGHT,
            summary="cohort churn write-up",
            author="alice",
            agent="agent-1",
            # 3000 whitespace-delimited words — longer than the default
            # 2000-word prefix budget (ADR 0021).
            body=" ".join(f"w{i}" for i in range(3000)),
        )

    async def test_sends_the_truncated_embeddable_text(self) -> None:
        env = MockEnv()
        embedder = make_embedder(env)
        draft = self._long_body_draft()
        await embedder.embed_entry(draft)
        # The request carries exactly the domain's embeddable text
        # (summary + bounded body prefix), never the full body.
        expected = embeddable_text(draft.summary, draft.body)
        assert env.last_body["input"] == expected
        assert len(expected) < len(draft.body or "")
        # The port method agrees with what was actually sent.
        assert embedder.entry_embeddable_text(draft) == expected


class TestPrefixTokenBudget:
    """ADR 0021: the body budget is ``prefix_tokens`` whitespace-delimited
    words, threaded from ``Settings.embedding_prefix_tokens``.

    Before ADR 0021 the embedder ignored the setting entirely (it called
    the domain helper with no bound argument), so the knob moved entity
    extraction and did nothing at all to embedding. These tests pin that
    the knob reaches the wire.
    """

    @staticmethod
    def _draft(body: str) -> EntryDraft:
        return EntryDraft(
            kind=Kind.INSIGHT,
            summary="pool exhausted",
            author="alice",
            agent="agent-1",
            body=body,
        )

    async def test_configured_budget_bounds_the_embedded_text(self) -> None:
        env = MockEnv()
        embedder = make_embedder(env, prefix_tokens=3)
        await embedder.embed_entry(self._draft("one two three four five six"))
        assert env.last_body["input"] == "pool exhausted\none two three"

    async def test_budget_counts_words_not_characters(self) -> None:
        """One very long word is ONE prefix token: a char budget would have
        cut it, a word budget does not."""
        env = MockEnv()
        embedder = make_embedder(env, prefix_tokens=1)
        await embedder.embed_entry(self._draft("x" * 5000))
        assert env.last_body["input"] == "pool exhausted\n" + "x" * 5000

    async def test_body_spacing_survives_the_cut(self) -> None:
        """The cut preserves the body's own whitespace up to the cut point
        (newlines and paragraph breaks are not collapsed)."""
        env = MockEnv()
        embedder = make_embedder(env, prefix_tokens=4)
        await embedder.embed_entry(self._draft("alpha beta\n\ngamma   delta epsilon"))
        assert env.last_body["input"] == "pool exhausted\nalpha beta\n\ngamma   delta"

    def test_from_settings_threads_the_configured_budget(self) -> None:
        """``from_settings`` must pass the setting through — the bug ADR
        0021 fixes was exactly this call site omitting it."""
        embedder = OpenAICompatEmbedder.from_settings(
            Settings(embedding_endpoint=BASE_URL, embedding_prefix_tokens=2)
        )
        assert (
            embedder.entry_embeddable_text(self._draft("one two three"))
            == "pool exhausted\none two"
        )


class TestDimensionsField:
    """The deploy-time dimension (ADR 0005) rides on the OpenAI ``dimensions`` field."""

    async def test_request_sends_the_configured_dimension(self) -> None:
        dim = 512
        env = MockEnv(
            handler=lambda request: httpx.Response(
                200,
                json={"data": [{"object": "embedding", "index": 0, "embedding": [0.1] * dim}]},
            )
        )
        embedder = make_embedder(env, dim=dim)
        await embedder.embed_text("x")
        assert env.last_body["dimensions"] == 512

    async def test_a_provider_that_ignores_dimensions_is_rejected(self) -> None:
        """A provider that ignores ``dimensions`` returns the native dim; the response check fails."""
        env = MockEnv()  # answers with a 4-dim vector
        embedder = make_embedder(env, dim=512)
        with pytest.raises(EmbeddingError) as exc_info:
            await embedder.embed_text("x")
        assert "expected 512 dims, got 4" in str(exc_info.value)


class TestFactoryAndProtocol:
    async def test_from_settings_reads_the_embedding_knobs(self) -> None:
        settings = Settings(
            embedding_endpoint="http://e.test/v1",
            embedding_api_key="k2",
            embedding_model="model-x",
            embedding_dim=8,
        )
        embedder = OpenAICompatEmbedder.from_settings(settings)
        try:
            assert embedder.dimension == 8
            assert embedder.model_name == "model-x"
            assert embedder.base_url == "http://e.test/v1"
        finally:
            await embedder.aclose()  # the embedder owns the client it built

    async def test_satisfies_the_embedder_port(self) -> None:
        env = MockEnv()
        embedder = make_embedder(env)
        assert isinstance(embedder, Embedder)

    async def test_aclose_keeps_injected_client_open(self) -> None:
        """A caller-injected client is not closed by ``aclose``."""
        env = MockEnv()
        client = env.client()
        embedder = OpenAICompatEmbedder(
            client=client,
            base_url=BASE_URL,
            api_key="",
            model_name="m",
            dim=4,
        )
        await embedder.aclose()
        vec = await embedder.embed_text("x")  # still works
        assert vec == OK_VECTOR

    def test_source_domain_imports_are_the_frozen_ones(self) -> None:
        """Sanity: the domain types used above are the frozen ones."""
        assert Source(type=SourceType.URL, ref="http://x").ref == "http://x"
