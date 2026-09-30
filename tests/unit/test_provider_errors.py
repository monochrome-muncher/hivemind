"""Provider-error hardening (embedder + extractor + config), PC-1..PC-12.

Hermetic: ``httpx.MockTransport``; the header-leak case raises the same
``LocalProtocolError`` text httpx/h11 produce for a key ending in ``\\n``.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from hivemind.api.main import create_app_for_config
from hivemind.config import SearchConfig, Settings, configure_logging, redact_url
from hivemind.domain.entry import EntryDraft, Kind
from hivemind.embeddings import EmbeddingError, OpenAICompatEmbedder
from hivemind.extractor import ExtractorError, OpenAICompatExtractor
from hivemind.mcp.app import hive_search, hive_write
from hivemind.memstore import MemoryStore
from tests.fakes import make_clock, make_search_config
from tests.unit.test_api_endpoints import make_authenticator, make_client
from tests.unit.test_mcp_app import ALICE, build_app

KEY = "sk-SECRETKEYVALUE"
BASE = "http://embed.test/v1"


async def _nosleep(_: float) -> None:
    return None


def _embedder(handler, *, key: str = "k", dim: int = 3, **kw) -> OpenAICompatEmbedder:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kw.setdefault("sleep", _nosleep)
    return OpenAICompatEmbedder(client, BASE, key, "m", dim, **kw)


def _ok(vec=(0.1, 0.2, 0.3)) -> httpx.Response:
    return httpx.Response(200, json={"data": [{"index": 0, "embedding": list(vec)}]})


def _extractor(handler, **kw) -> OpenAICompatExtractor:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kw.setdefault("sleep", _nosleep)
    return OpenAICompatExtractor(client, "http://x.test/v1", "k", "m", **kw)


def _chat(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _draft() -> EntryDraft:
    return EntryDraft(kind=Kind.INSIGHT, summary="s", author="a", agent="b")


def _leaky(req: httpx.Request) -> httpx.Response:
    raise httpx.LocalProtocolError(f"Illegal header value b'Bearer {KEY}\\n'")


class _LeakyEmbedder:
    dimension = 4
    model_name = "x"

    async def embed_text(self, text: str) -> list[float]:
        raise EmbeddingError(f"boom {KEY}")

    async def embed_entry(self, draft: EntryDraft) -> list[float]:
        raise EmbeddingError(f"boom {KEY}")

    def entry_embeddable_text(self, draft: EntryDraft) -> str:
        return draft.summary


class TestKeyNeverLeaks:
    async def test_newline_key_is_stripped_and_works(self) -> None:
        seen: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req.headers["authorization"])
            return _ok()

        await _embedder(handler, key=KEY + "\n").embed_text("x")
        assert seen == [f"Bearer {KEY}"]

    def test_settings_strip_and_reject_control_chars(self) -> None:
        assert Settings(embedding_api_key=f"  {KEY}\r\n").embedding_api_key == KEY
        with pytest.raises(ValueError) as exc:
            Settings(extractor_api_key="a\x00b")
        assert "a\x00b" not in str(exc.value)

    def test_repr_hides_secrets(self) -> None:
        text = repr(Settings(embedding_api_key=KEY, database_url="postgresql://u:PW123@h/db"))
        assert KEY not in text and "PW123" not in text

    async def test_embedder_transport_error_is_class_name_only(self, caplog) -> None:
        caplog.set_level(logging.DEBUG)
        with pytest.raises(EmbeddingError) as exc:
            await _embedder(_leaky).embed_text("x")
        assert KEY not in str(exc.value)
        assert KEY not in caplog.text
        assert exc.value.__cause__ is None

    async def test_extractor_transport_error_is_sanitized(self, caplog) -> None:
        caplog.set_level(logging.DEBUG)
        with pytest.raises(ExtractorError) as exc:
            await _extractor(_leaky).extract_entry(_draft())
        assert KEY not in str(exc.value) and KEY not in caplog.text

    async def test_rest_502_body_is_generic_on_write_and_search(self, caplog) -> None:
        app = create_app_for_config(
            Settings(),
            store=MemoryStore(make_clock()),
            embedder=_LeakyEmbedder(),
            authenticator=make_authenticator(),
            search_config=make_search_config(),
        )
        caplog.set_level(logging.DEBUG)
        async with make_client(app) as client:
            headers = {"X-API-Key": "key-alice-user"}
            for path, body in (
                ("/v1/search", {"query": "q"}),
                ("/v1/entries", {"kind": "fact", "summary": "s", "agent": "a"}),
            ):
                r = await client.post(path, json=body, headers=headers)
                assert r.status_code == 502, r.text
                assert r.json()["error"]["code"] == "embedding_unavailable"
                assert KEY not in r.text

    async def test_mcp_search_and_write_return_a_typed_error(self) -> None:
        clock = make_clock()
        app = build_app(MemoryStore(clock), ALICE, clock)
        app.search_service._embedder = _LeakyEmbedder()  # type: ignore[attr-defined]
        app.write_service._embedder = _LeakyEmbedder()  # type: ignore[attr-defined]
        for result in (
            await hive_search(app, query="q"),
            await hive_write(app, kind="fact", summary="s"),
        ):
            assert result["error"]["code"] == "embedding_unavailable", result
            assert KEY not in json.dumps(result)


class TestResponseValidation:
    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(200, text="<html>captive portal</html>"),
            httpx.Response(301, headers={"location": "https://x/"}),
            httpx.Response(200, json={"data": []}),
            httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3]}] * 2}),
            httpx.Response(200, json={"data": [{"index": 1, "embedding": [0.1, 0.2, 0.3]}]}),
            httpx.Response(200, content=b'{"data":[{"embedding":[NaN,1,2]}]}'),
            httpx.Response(200, content=b'{"data":[{"embedding":[Infinity,1,2]}]}'),
            httpx.Response(200, json={"data": [{"embedding": [True, False, True]}]}),
            httpx.Response(200, json={"data": [{"embedding": ["1", "2", "3"]}]}),
            httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]}),
            httpx.Response(200, json={"data": [{"embedding": "abc"}]}),
            httpx.Response(200, content=b"[" * 100_000),
        ],
    )
    async def test_malformed_body_is_an_embedding_error(self, response) -> None:
        with pytest.raises(EmbeddingError):
            await _embedder(lambda req: response).embed_text("x")

    async def test_redirect_is_not_retried(self) -> None:
        calls: list[int] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(301)

        with pytest.raises(EmbeddingError):
            await _embedder(handler).embed_text("x")
        assert len(calls) == 1


class TestRetryPolicy:
    async def test_remote_protocol_error_is_retried(self) -> None:
        calls: list[int] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                raise httpx.RemoteProtocolError("Server disconnected")
            return _ok()

        assert await _embedder(handler).embed_text("x") == [0.1, 0.2, 0.3]
        assert len(calls) == 2

    async def test_4xx_other_than_429_is_not_retried(self) -> None:
        calls: list[int] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(400)

        with pytest.raises(EmbeddingError):
            await _embedder(handler).embed_text("x")
        assert len(calls) == 1

    async def test_retry_after_is_honoured_and_capped(self) -> None:
        sleeps: list[float] = []

        async def rec(d: float) -> None:
            sleeps.append(d)

        answers = [
            httpx.Response(429, headers={"retry-after": "3"}),
            httpx.Response(429, headers={"retry-after": "9999"}),
            _ok(),
        ]
        e = _embedder(lambda req: answers.pop(0), sleep=rec, backoff=0.1, jitter=lambda: 1.0)
        await e.embed_text("x")
        assert sleeps == [3.0, 10.0]

    async def test_backoff_is_jittered(self) -> None:
        sleeps: list[float] = []

        async def rec(d: float) -> None:
            sleeps.append(d)

        answers = [httpx.Response(500), _ok()]
        e = _embedder(lambda req: answers.pop(0), sleep=rec, backoff=1.0, jitter=lambda: 0.0)
        await e.embed_text("x")
        assert sleeps == [0.5]

    async def test_negative_retries_make_one_attempt(self) -> None:
        calls: list[int] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(500)

        with pytest.raises(EmbeddingError):
            await _embedder(handler, retries=-1).embed_text("x")
        assert len(calls) == 1

    async def test_overall_deadline_bounds_the_call(self) -> None:
        async def slow_sleep(_: float) -> None:
            await asyncio.sleep(5)

        e = _embedder(lambda req: httpx.Response(500), sleep=slow_sleep, deadline=0.05)
        with pytest.raises(EmbeddingError, match="deadline"):
            await asyncio.wait_for(e.embed_text("x"), 2)


class TestSettingsValidation:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"embedding_retries": -1},
            {"extractor_retries": -1},
            {"embedding_dim": 0},
            {"embedding_dim": 2001},
            {"rrf_k": -1},
            {"candidate_top_k": 0},
            {"default_limit": 0},
            {"weight_keyword": -0.1},
            {"weight_vector": float("nan")},
            {"half_life_days": float("inf")},
            {"half_life_days": float("nan")},
            {"quality_min": 2.0, "quality_max": 1.0},
            {"pool_min_size": 5, "pool_max_size": 2},
            {"pool_max_size": 0},
            {"embedding_timeout": 0},
            {"extractor_timeout": -1},
            {"embedding_deadline": 0},
            {"embedding_prefix_tokens": -1},
            {"embedding_deadline": 5, "embedding_timeout": 10},
            {"extractor_deadline": 5},
            {"embedding_endpoint": "http://h/v1\nx"},
            {"embedding_endpoint": "localhost:8001/v1"},
            {"extractor_endpoint": "http://x/v1", "extractor_model": ""},
        ],
    )
    def test_invalid_value_fails_at_config_time(self, kwargs) -> None:
        with pytest.raises(ValueError):
            Settings(**kwargs)

    def test_search_config_rejects_bad_values_directly(self) -> None:
        with pytest.raises(ValueError):
            SearchConfig(rrf_k=-1)

    def test_defaults_are_valid(self) -> None:
        Settings()

    def test_compat_values_stay_valid(self) -> None:
        # 0 = summary only (domain/entry.py); 1/(0+rank) is valid RRF.
        Settings(embedding_prefix_tokens=0, rrf_k=0)
        SearchConfig(rrf_k=0)

    def test_deadline_defaults_to_the_documented_worst_case(self) -> None:
        from hivemind.providers import default_deadline

        # DEPLOY.md §5: 3 x 10s + 0.5s + 1.0s; 3 x 30s + 0.5s + 1.0s
        assert default_deadline(10.0, 2) == 31.5
        assert default_deadline(30.0, 2) == 91.5
        assert default_deadline(30.0, 0) == 30.0
        # raising TIMEOUT / RETRIES needs no deadline change
        s = Settings(extractor_timeout=60.0, extractor_retries=4)
        assert s.extractor_deadline is None

    def test_endpoint_whitespace_is_stripped(self) -> None:
        assert Settings(embedding_endpoint="http://h/v1\n").embedding_endpoint == "http://h/v1"

    def test_validation_errors_never_echo_the_value(self) -> None:
        # a PRINTABLE secret: an assertion on a NUL would be vacuous
        with pytest.raises(ValueError) as exc:
            Settings(embedding_retries="sk-PRINTABLESECRET")  # type: ignore[arg-type]
        assert "sk-PRINTABLESECRET" not in str(exc.value)
        with pytest.raises(ValueError) as exc:
            Settings(embedding_api_key="sk-PRINTABLESECRET\x00x")
        assert "sk-PRINTABLESECRET" not in str(exc.value)
        with pytest.raises(ValueError) as exc:
            Settings(embedding_api_key="sk-PRINTABLE\u00e9")
        assert "sk-PRINTABLE" not in str(exc.value)

    def test_endpoint_errors_never_echo_userinfo(self) -> None:
        with pytest.raises(ValueError) as exc:
            Settings(embedding_endpoint="user:hunter2@host:8001/v1")
        assert "hunter2" not in str(exc.value)

    def test_key_over_plain_http_to_remote_host_warns(self, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            Settings(embedding_endpoint="http://api.example.com/v1", embedding_api_key=KEY)
        assert "plain http" in caplog.text and KEY not in caplog.text

    def test_loopback_http_with_key_does_not_warn(self, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            Settings(embedding_endpoint="http://localhost:8001/v1", embedding_api_key=KEY)
        assert "plain http" not in caplog.text


class TestLogging:
    def test_redact_url(self) -> None:
        assert redact_url("https://bob:hunter2@emb.example/v1") == "https://emb.example/v1"
        assert redact_url("http://h/v1") == "http://h/v1"

    @pytest.mark.parametrize(
        "url",
        [
            "https://bob:hunter2@emb.example/v1",
            "user:hunter2@host:8001/v1",
            "http://user:pa/ss@host/v1",
            "http://user:hunter2@host:8001",
        ],
    )
    def test_redact_url_hides_userinfo_robustly(self, url) -> None:
        assert "hunter2" not in redact_url(url) and "user:" not in redact_url(url)
        assert "ss@" not in redact_url(url)

    def test_startup_lines_redact_the_endpoint(self, monkeypatch, caplog) -> None:
        import hivemind.api.main as api_main
        import hivemind.mcp.http as mcp_http
        import hivemind.mcp.server as mcp_server
        import hivemind.store as store_mod

        settings = Settings(embedding_endpoint="https://bob:hunter2@emb.example/v1")

        class Stop(Exception):
            pass

        def boom(*a, **k):
            raise Stop

        for mod in (api_main, mcp_http, mcp_server):
            monkeypatch.setattr(mod, "load_settings", lambda: settings)
            monkeypatch.setattr(mod, "configure_logging", lambda level: None)
        monkeypatch.setattr(api_main, "create_app_from_settings", boom)
        monkeypatch.setattr(store_mod, "build_store", boom)
        monkeypatch.setenv("HIVEMIND_MCP_KEY", "hm_x")
        caplog.set_level(logging.INFO)
        for run in (api_main.run, mcp_http.main_http, mcp_server.main_pg):
            with pytest.raises(Stop):
                run()
        assert "starting hivemind" in caplog.text
        assert "hunter2" not in caplog.text

    def test_httpx_loggers_are_quieted(self) -> None:
        configure_logging("INFO")
        assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
        assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


class TestClientSideFailures:
    async def test_non_ascii_key_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError) as exc:
            _embedder(lambda r: _ok(), key="sk-\u00e9abc")
        assert "sk-" not in str(exc.value)

    @pytest.mark.parametrize(
        "exc", [UnicodeEncodeError("ascii", "x", 0, 1, "bad"), httpx.InvalidURL("bad")]
    )
    async def test_encode_and_url_errors_map_to_the_typed_error(self, exc) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise exc

        with pytest.raises(EmbeddingError):
            await _embedder(handler).embed_text("x")

    async def test_endpoint_with_trailing_newline_works(self) -> None:
        seen: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(str(req.url))
            return _ok()

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        e = OpenAICompatEmbedder(client, BASE + "\n", "k", "m", 3)
        await e.embed_text("x")
        assert seen == [BASE + "/embeddings"]


class TestExtractorRobustness:
    @pytest.mark.parametrize(
        "content",
        [
            "```json\n" + "\n" * 5000,
            "```\n" + "\n" * 5000,
            "```\n" + " \n" * 5000 + "x",
            "<think>" + "\n" * 50_000,
            "<think>" + "</think>" * 5000 + "\n" * 5000,
            " \n" * 30_000,
        ],
    )
    async def test_unwrapping_is_linear_time(self, content) -> None:
        import time

        start = time.perf_counter()
        with pytest.raises(ExtractorError):
            await _extractor(lambda r: _chat(content)).extract_entry(_draft())
        assert time.perf_counter() - start < 0.5

    async def test_dedupe_is_lower_of_the_collapsed_name(self) -> None:
        # str.lower() is the store / filter key (ADR 0016); casefold would
        # merge "Stra\u00dfe" and "STRASSE", which the filter never would.
        body = json.dumps(
            [{"name": "Stra\u00dfe", "kind": "system"}, {"name": "STRASSE", "kind": "system"}]
        )
        assert len(await _extractor(lambda r: _chat(body)).extract_entry(_draft())) == 2

    async def test_format_characters_are_kept(self) -> None:
        body = json.dumps([{"name": "a\u200db\u00adc", "kind": "system"}])
        (e,) = await _extractor(lambda r: _chat(body)).extract_entry(_draft())
        assert e.name == "a\u200db\u00adc"

    async def test_nul_and_control_chars_are_cleaned(self) -> None:
        body = json.dumps([{"name": "a\x00b\nIGNORE  \x07it", "kind": "system"}])
        (e,) = await _extractor(lambda r: _chat(body)).extract_entry(_draft())
        assert e.name == "a b IGNORE it"

    async def test_name_only_control_chars_is_rejected(self) -> None:
        body = json.dumps([{"name": "\x00\n", "kind": "system"}])
        with pytest.raises(ExtractorError):
            await _extractor(lambda r: _chat(body)).extract_entry(_draft())

    async def test_dedupe_ignores_whitespace_and_case(self) -> None:
        body = json.dumps([{"name": "A  b", "kind": "system"}, {"name": "a b", "kind": "system"}])
        assert len(await _extractor(lambda r: _chat(body)).extract_entry(_draft())) == 1

    @pytest.mark.parametrize(
        "content",
        [
            '```json\n[{"name": "pg", "kind": "system"}]\n```',
            '<think>hmm\n[1]</think>\n[{"name": "pg", "kind": "system"}]',
            '<think>x</think>```\n[{"name": "pg", "kind": "system"}]\n```',
        ],
    )
    async def test_fence_and_think_are_tolerated(self, content) -> None:
        (e,) = await _extractor(lambda r: _chat(content)).extract_entry(_draft())
        assert e.name == "pg"

    async def test_deep_nesting_is_an_extractor_error(self) -> None:
        with pytest.raises(ExtractorError):
            await _extractor(lambda r: _chat("[" * 50_000)).extract_entry(_draft())

    async def test_oversized_output_is_refused(self) -> None:
        with pytest.raises(ExtractorError):
            await _extractor(lambda r: _chat("x" * 200_000)).extract_entry(_draft())

    async def test_bad_output_is_reported_only_as_a_short_escaped_snippet(self) -> None:
        with pytest.raises(ExtractorError) as exc:
            await _extractor(lambda r: _chat("line1\nFORGED " + "y" * 500)).extract_entry(_draft())
        assert "\n" not in str(exc.value) and len(str(exc.value)) < 200

    async def test_non_json_body_is_an_extractor_error(self) -> None:
        with pytest.raises(ExtractorError):
            await _extractor(lambda r: httpx.Response(200, text="<html>")).extract_entry(_draft())
