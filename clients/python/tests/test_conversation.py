from __future__ import annotations

import json

import pytest

import hivemind_memory
from hivemind_memory import Conversation
from tests.conftest import Connected


def _calls(conversation: Conversation, *names: str) -> None:
    for name in names:
        conversation.count_tool_call(name)


async def test_quiet_below_the_threshold_then_due_once(connected: Connected) -> None:
    async with connected() as hive:
        conversation = hive.conversation(min_tool_calls=3)
        _calls(conversation, "bash", "hive_search")
        assert conversation.reminder_due() is None
        _calls(conversation, "bash")
        assert conversation.reminder_due() == hivemind_memory.REMINDER
        assert conversation.reminder_due() is None


async def test_quiet_once_something_was_rated_under_any_prefix(connected: Connected) -> None:
    async with connected() as hive:
        conversation = hive.conversation(min_tool_calls=1)
        _calls(conversation, "mcp__hivemind__hive_feedback")
        assert conversation.reminder_due() is None


async def test_quiet_after_a_write_without_reads_due_with_reads(connected: Connected) -> None:
    async with connected() as hive:
        wrote_only = hive.conversation(min_tool_calls=1)
        _calls(wrote_only, "hive_write")
        assert wrote_only.reminder_due() is None
        wrote_and_read = hive.conversation(min_tool_calls=1)
        _calls(wrote_and_read, "hive_get", "hive_write")
        assert wrote_and_read.reminder_due() == hivemind_memory.REMINDER


async def test_the_threshold_comes_from_the_environment_else_20(
    connected: Connected, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with connected() as hive:
        monkeypatch.delenv("HIVEMIND_STOP_MIN_TOOLS", raising=False)
        assert hive.conversation().min_tool_calls == 20
        monkeypatch.setenv("HIVEMIND_STOP_MIN_TOOLS", "7")
        assert hive.conversation().min_tool_calls == 7
        monkeypatch.setenv("HIVEMIND_STOP_MIN_TOOLS", "seven")
        assert hive.conversation().min_tool_calls == 20


async def test_it_counts_the_calls_it_runs(connected: Connected) -> None:
    async with connected() as hive:
        conversation = hive.conversation()
        reply = await conversation.run_tool("hive_search", {"query": "q"})
        assert json.loads(reply)["count"] == 1
        assert conversation.tool_calls == 1


async def test_the_post_compaction_line(connected: Connected) -> None:
    async with connected() as hive:
        assert hive.after_compaction() is hivemind_memory.COMPACTED
        assert hivemind_memory.COMPACTED.startswith("Context was just compacted")


async def test_a_call_id_is_counted_once(connected: Connected) -> None:
    async with connected() as hive:
        conversation = hive.conversation()
        conversation.count_tool_call("bash", "c1")
        conversation.count_tool_call("bash", "c1")
        conversation.count_tool_call("bash")
        assert conversation.tool_calls == 2
