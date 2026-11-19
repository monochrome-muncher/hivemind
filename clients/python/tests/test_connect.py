from __future__ import annotations

import pytest

from hivemind_memory import AuthenticationError, Hivemind
from tests.conftest import (
    GOOD_KEY,
    LOCKED_DETAIL,
    LOCKED_KEY,
    SERVER_INSTRUCTIONS,
    Connected,
    HttpServer,
    build_fake_server,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HIVEMIND_MCP_URL", raising=False)
    monkeypatch.delenv("HIVEMIND_API_KEY", raising=False)


def test_missing_url_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVEMIND_API_KEY", "k")
    with pytest.raises(ValueError, match="HIVEMIND_MCP_URL"):
        Hivemind.connect()


def test_missing_key_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVEMIND_MCP_URL", "http://x/mcp")
    with pytest.raises(ValueError, match="HIVEMIND_API_KEY"):
        Hivemind.connect()


async def test_arguments_win_over_the_environment(
    monkeypatch: pytest.MonkeyPatch, http_server: HttpServer
) -> None:
    async with http_server() as url:
        monkeypatch.setenv("HIVEMIND_MCP_URL", "http://127.0.0.1:1/mcp")
        monkeypatch.setenv("HIVEMIND_API_KEY", "wrong")
        async with Hivemind.connect(url, GOOD_KEY) as hive:
            assert hive.whoami["key_kind"] == "agent"


async def test_environment_is_used_when_there_are_no_arguments(
    monkeypatch: pytest.MonkeyPatch, http_server: HttpServer
) -> None:
    async with http_server() as url:
        monkeypatch.setenv("HIVEMIND_MCP_URL", url)
        monkeypatch.setenv("HIVEMIND_API_KEY", GOOD_KEY)
        async with Hivemind.connect() as hive:
            assert hive.whoami["name"] == "fake-agent"


async def test_open_and_close(http_server: HttpServer) -> None:
    async with http_server() as url:
        hive = await Hivemind.open(url, GOOD_KEY)
        assert (await hive.search("x"))["count"] == 1
        await hive.close()
        await hive.close()


async def test_a_401_raises_authentication_error(http_server: HttpServer) -> None:
    async with http_server() as url:
        with pytest.raises(AuthenticationError):
            async with Hivemind.connect(url, "not-the-key"):
                pass


async def test_a_locked_key_raises_key_locked_with_the_servers_message(
    http_server: HttpServer,
) -> None:
    async with http_server() as url:
        with pytest.raises(AuthenticationError) as caught:
            async with Hivemind.connect(url, LOCKED_KEY):
                pass
    assert (caught.value.code, caught.value.message) == ("key_locked", LOCKED_DETAIL)


@pytest.mark.parametrize("code", ["unauthenticated", "key_locked"])
async def test_an_unusable_key_in_whoami_raises_authentication_error(code: str) -> None:
    with pytest.raises(AuthenticationError) as caught:
        async with Hivemind._open_target(build_fake_server(whoami_refusal=code)):
            pass
    assert caught.value.code == code


async def test_connect_keeps_whoami_tools_and_instructions(connected: Connected) -> None:
    async with connected() as hive:
        assert dict(hive.whoami) == {"key_kind": "agent", "name": "fake-agent"}
        assert [tool["name"] for tool in hive.tools] == ["hive_whoami", "hive_search", "hive_get"]
        assert hive.instructions().startswith(SERVER_INSTRUCTIONS)


async def test_an_error_in_the_body_is_not_wrapped_in_an_exception_group(
    connected: Connected,
) -> None:
    with pytest.raises(KeyError):
        async with connected():
            raise KeyError("mine")
