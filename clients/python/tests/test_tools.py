from __future__ import annotations

import json
from typing import Any

import pytest

import hivemind_memory
from hivemind_memory import AuthenticationError, HivemindError
from hivemind_memory.text import skill_body
from tests.conftest import Connected


async def test_call_returns_the_parsed_reply(connected: Connected) -> None:
    async with connected() as hive:
        reply = await hive.call("hive_search", {"query": "pgvector"})
        assert reply["hits"][0]["summary"] == "pgvector"


async def test_call_raises_the_error_code(connected: Connected) -> None:
    async with connected() as hive:
        with pytest.raises(HivemindError) as caught:
            await hive.call("hive_get", {"entry_id": "missing"})
        assert caught.value.code == "not_found"
        assert caught.value.message == "no such entry"
        assert not isinstance(caught.value, AuthenticationError)


async def test_run_tool_returns_the_reply_text(connected: Connected) -> None:
    async with connected() as hive:
        reply = await hive.run_tool("hive_search", {"query": "q"})
        assert json.loads(reply)["count"] == 1


async def test_run_tool_takes_a_json_string(connected: Connected) -> None:
    async with connected() as hive:
        reply = await hive.run_tool("hive_search", '{"query": "q", "limit": 2}')
        assert json.loads(reply)["limit"] == 2


async def test_run_tool_returns_a_refusal_as_text(connected: Connected) -> None:
    async with connected() as hive:
        reply = await hive.run_tool("hive_get", {"entry_id": "missing"})
        assert json.loads(reply) == {"error": {"code": "not_found", "message": "no such entry"}}


async def test_run_tool_answers_an_unknown_tool_with_invalid_input(connected: Connected) -> None:
    async with connected() as hive:
        reply = json.loads(await hive.run_tool("hive_delete_everything", {}))
        assert reply["error"]["code"] == "invalid_input"


async def test_a_failed_call_outside_the_envelope_reaches_the_model_as_tool_error(
    connected: Connected,
) -> None:
    async with connected() as hive:
        reply = json.loads(await hive.run_tool("hive_get", {"entry_id": "crash"}))
        assert reply["error"]["code"] == "tool_error"
        assert reply["error"]["message"] == "Error executing tool hive_get"


async def test_call_raises_tool_error_for_a_failed_call_outside_the_envelope(
    connected: Connected,
) -> None:
    async with connected() as hive:
        with pytest.raises(HivemindError) as caught:
            await hive.get("crash")
        assert caught.value.code == "tool_error"


@pytest.mark.parametrize("arguments", ["{not json", "[1, 2]"])
async def test_run_tool_answers_bad_arguments_with_invalid_input(
    connected: Connected, arguments: str
) -> None:
    async with connected() as hive:
        reply = json.loads(await hive.run_tool("hive_search", arguments))
        assert reply["error"]["code"] == "invalid_input"


CALLS: list[tuple[str, tuple[Any, ...], dict[str, Any], str, dict[str, Any]]] = [
    ("search", ("q",), {}, "hive_search", {"query": "q"}),
    (
        "search",
        ("q",),
        {"limit": 3, "flagged": False},
        "hive_search",
        {"query": "q", "limit": 3, "flagged": False},
    ),
    ("get", ("e1",), {}, "hive_get", {"entry_id": "e1"}),
    ("get", (["a", "b"],), {}, "hive_get", {"entry_ids": ["a", "b"]}),
    ("list", (), {"kind": "fact"}, "hive_list", {"kind": "fact"}),
    ("pinned", (), {}, "hive_pinned", {}),
    (
        "write",
        ("fact", "s"),
        {"tags": ["t"]},
        "hive_write",
        {"kind": "fact", "summary": "s", "tags": ["t"]},
    ),
    (
        "feedback",
        ("e1", "helpful"),
        {},
        "hive_feedback",
        {"verdict": "helpful", "entry_id": "e1"},
    ),
    (
        "feedback",
        (["a", "b"], "stale"),
        {"note": "moved"},
        "hive_feedback",
        {"verdict": "stale", "entry_ids": ["a", "b"], "note": "moved"},
    ),
    ("withdraw", ("e1",), {"reason": "r"}, "hive_withdraw", {"entry_id": "e1", "reason": "r"}),
    ("withdraw", ("e1",), {}, "hive_withdraw", {"entry_id": "e1"}),
    ("pin", ("e1",), {"unpin": True}, "hive_pin", {"entry_id": "e1", "unpin": True}),
    ("register", ("bot",), {}, "hive_register", {"name": "bot"}),
]


@pytest.mark.parametrize(("method", "args", "kwargs", "tool", "sent"), CALLS)
async def test_typed_methods_send_only_the_arguments_given(
    connected: Connected,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    tool: str,
    sent: dict[str, Any],
) -> None:
    async with connected() as hive:
        calls: list[tuple[str, dict[str, Any]]] = []

        async def record(name: str, arguments: Any = None) -> dict[str, Any]:
            calls.append((name, dict(arguments or {})))
            return {}

        monkeypatch.setattr(hive, "call", record)
        await getattr(hive, method)(*args, **kwargs)
        assert calls == [(tool, sent)]


async def test_whoami_can_be_refreshed(connected: Connected) -> None:
    async with connected() as hive:
        before = hive.whoami
        assert (await hive.refresh_whoami())["key_kind"] == "agent"
        assert hive.whoami is not before
        assert hive.whoami["name"] == "fake-agent"


async def test_instructions_are_the_shipped_server_text_then_the_skill_without_front_matter(
    connected: Connected,
) -> None:
    async with connected() as hive:
        text = hive.instructions()
        assert text is hivemind_memory.INSTRUCTIONS
        assert text.startswith("Hivemind is this organization's shared long-term memory")
        assert text.endswith("\n\n" + skill_body())
        assert "name: hivemind" not in text
        assert "hivemind-setup" not in text
        assert skill_body().startswith("# Hivemind")


async def test_reminder_is_the_constant(connected: Connected) -> None:
    async with connected() as hive:
        assert hive.reminder() is hivemind_memory.REMINDER
    assert hivemind_memory.REMINDER.startswith("HIVEMIND: before finishing")
    assert not hivemind_memory.REMINDER.endswith("\n")


async def test_anthropic_tools(connected: Connected) -> None:
    async with connected() as hive:
        tools = hive.anthropic_tools()
        search = next(tool for tool in tools if tool["name"] == "hive_search")
        assert set(search) == {"name", "description", "input_schema"}
        assert search["description"] == "Search the pool."
        assert search["input_schema"]["required"] == ["query"]


async def test_openai_tools(connected: Connected) -> None:
    async with connected() as hive:
        tools = hive.openai_tools()
        search = next(tool for tool in tools if tool["function"]["name"] == "hive_search")
        assert search["type"] == "function"
        assert set(search["function"]) == {"name", "description", "parameters"}
        assert search["function"]["parameters"]["required"] == ["query"]


async def test_tool_formats_are_copies(connected: Connected) -> None:
    async with connected() as hive:
        hive.anthropic_tools()[0]["input_schema"].clear()
        hive.openai_tools()[0]["function"]["parameters"].clear()
        assert hive.tools[0]["input_schema"]
