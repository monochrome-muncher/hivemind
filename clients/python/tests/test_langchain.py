from __future__ import annotations

import json

from langchain_core.messages import SystemMessage

from hivemind_memory.langchain import langchain_tools, system_message
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
