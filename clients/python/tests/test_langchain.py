from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from hivemind_memory.langchain import ainvoke_with_reminder, langchain_tools, system_message
from tests.conftest import Connected


async def test_one_tool_per_server_tool_with_its_schema(connected: Connected) -> None:
    async with connected() as hive:
        tools = {tool.name: tool for tool in langchain_tools(hive)}
        assert set(tools) == {"hive_whoami", "hive_search", "hive_get"}
        assert tools["hive_search"].description == "Search the pool."
        assert tools["hive_search"].args["query"]["type"] == "string"


async def test_invoking_a_tool_returns_the_reply_text(connected: Connected) -> None:
    async with connected() as hive:
        search = next(t for t in langchain_tools(hive) if t.name == "hive_search")
        reply = await search.ainvoke({"query": "pgvector"})
        assert json.loads(reply)["hits"][0]["summary"] == "pgvector"


async def test_a_refusal_reaches_the_model_as_text(connected: Connected) -> None:
    async with connected() as hive:
        get = next(t for t in langchain_tools(hive) if t.name == "hive_get")
        reply = await get.ainvoke({"entry_id": "missing"})
        assert json.loads(reply)["error"]["code"] == "not_found"


async def test_system_message_carries_the_instructions(connected: Connected) -> None:
    async with connected() as hive:
        message = system_message(hive)
        assert isinstance(message, SystemMessage)
        assert message.content == hive.instructions()


class _ScriptedAgent:
    """Stands in for a compiled agent: answers each run with the scripted new messages."""

    def __init__(self, *runs: list[BaseMessage]) -> None:
        self.runs = list(runs)
        self.inputs: list[dict[str, Any]] = []

    async def ainvoke(self, input: dict[str, Any], /, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.inputs.append(input)
        return {"messages": [*input["messages"], *self.runs.pop(0)]}


def _calling(*names: str) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": {}, "id": name} for name in names]
    )


async def test_ainvoke_with_reminder_runs_once_more_when_due(connected: Connected) -> None:
    async with connected() as hive:
        conversation = hive.conversation(min_tool_calls=2)
        agent = _ScriptedAgent(
            [_calling("hive_search", "app_tool"), AIMessage(content="the answer")],
            [AIMessage(content="nothing new")],
        )
        task = {"messages": [HumanMessage(content="the task")]}
        state, reminded = await ainvoke_with_reminder(conversation, agent, task)
        assert state["messages"][-1].content == "the answer"
        assert reminded is not None
        assert reminded["messages"][-1].content == "nothing new"
        assert agent.inputs[1]["messages"][-1].content == hive.reminder()
        assert conversation.tool_calls == 2


async def test_ainvoke_with_reminder_stops_when_not_due(connected: Connected) -> None:
    async with connected() as hive:
        conversation = hive.conversation(min_tool_calls=5)
        agent = _ScriptedAgent([AIMessage(content="the answer")])
        task = {"messages": [HumanMessage(content="the task")]}
        _state, reminded = await ainvoke_with_reminder(conversation, agent, task)
        assert reminded is None
        assert len(agent.inputs) == 1


async def test_ainvoke_with_reminder_counts_a_returned_thread_once(connected: Connected) -> None:
    async with connected() as hive:
        conversation = hive.conversation(min_tool_calls=3)
        agent = _ScriptedAgent([_calling("hive_feedback", "app_tool")], [AIMessage(content="ok")])
        first, _ = await ainvoke_with_reminder(conversation, agent, {"messages": []})
        await ainvoke_with_reminder(conversation, agent, first)
        assert conversation.tool_calls == 2
