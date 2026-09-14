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
) -> OpenAICompatEmbedder:
    return OpenAICompatEmbedder(
        client=env.client(),
        base_url=base_url,
        api_key=api_key,
        model_name=model_name,
        dim=dim,
    )


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
            body="x" * 3000,  # longer than the 2048-char prefix
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
