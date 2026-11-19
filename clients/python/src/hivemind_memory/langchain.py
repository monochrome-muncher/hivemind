"""LangChain and LangGraph adapter: ``pip install hivemind-memory[langchain]``."""

from __future__ import annotations

from typing import Any

try:
    from langchain_core.messages import SystemMessage
    from langchain_core.tools import BaseTool, StructuredTool
except ImportError as exc:
    raise ImportError(
        "hivemind_memory.langchain needs LangChain: pip install hivemind-memory[langchain]"
    ) from exc

from hivemind_memory.client import Hivemind
from hivemind_memory.formats import ToolDefinition


def langchain_tools(hive: Hivemind) -> list[BaseTool]:
    """One tool per Hivemind tool. Refusals come back as text, so the model sees the error code."""
    return [_tool(hive, definition) for definition in hive.tools]


def system_message(hive: Hivemind) -> SystemMessage:
    return SystemMessage(content=hive.instructions())


def _tool(hive: Hivemind, definition: ToolDefinition) -> BaseTool:
    name = definition["name"]

    async def run(**arguments: Any) -> str:
        sent = {key: value for key, value in arguments.items() if value is not None}
        return await hive.run_tool(name, sent)

    return StructuredTool(
        name=name,
        description=definition["description"],
        args_schema=definition["input_schema"],
        coroutine=run,
    )
