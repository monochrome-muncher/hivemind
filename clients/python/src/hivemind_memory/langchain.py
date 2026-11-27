"""LangChain and LangGraph adapter: ``pip install hivemind-memory[langchain]``."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

try:
    from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
    from langchain_core.tools import BaseTool, StructuredTool
except ImportError as exc:
    raise ImportError(
        "hivemind_memory.langchain needs LangChain: pip install hivemind-memory[langchain]"
    ) from exc

from hivemind_memory.client import Hivemind
from hivemind_memory.conversation import Conversation
from hivemind_memory.formats import ToolDefinition

AgentState = dict[str, Any]


class MessagesAgent(Protocol):
    """An agent whose state carries ``messages``: LangChain's ``create_agent``, LangGraph's graphs."""

    async def ainvoke(self, input: AgentState, /, *args: Any, **kwargs: Any) -> AgentState: ...


def langchain_tools(hive: Hivemind) -> list[BaseTool]:
    """One tool per Hivemind tool. Refusals come back as text, so the model sees the error code."""
    return [_tool(hive, definition) for definition in hive.tools]


def system_message(hive: Hivemind) -> SystemMessage:
    return SystemMessage(content=hive.instructions())


async def ainvoke_with_reminder(
    conversation: Conversation, agent: MessagesAgent, input: AgentState, **kwargs: Any
) -> tuple[AgentState, AgentState | None]:
    """Run the agent; if the end-of-task reminder is due, run it once more with the reminder.

    Every tool call in the agent's messages is counted once, by its id, so a checkpointer that
    returns the whole thread does not count earlier turns again. Returns the task's state and the
    reminder run's state, or ``None`` when no reminder was due. A run that paused for a human
    approval, or ended on tool calls without results, is not reminded and keeps the reminder due.
    """
    state = await agent.ainvoke(input, **kwargs)
    _count_tool_calls(conversation, state)
    if _awaits_tool_results(state):
        return state, None
    reminder = conversation.reminder_due()
    if reminder is None:
        return state, None
    history: Sequence[BaseMessage] = state["messages"]
    reminded = await agent.ainvoke(
        {**state, "messages": [*history, HumanMessage(content=reminder)]}, **kwargs
    )
    _count_tool_calls(conversation, reminded)
    return state, reminded


def _awaits_tool_results(state: Mapping[str, Any]) -> bool:
    messages = state.get("messages") or []
    last = messages[-1] if messages else None
    return "__interrupt__" in state or (isinstance(last, AIMessage) and bool(last.tool_calls))


def _count_tool_calls(conversation: Conversation, state: Mapping[str, Any]) -> None:
    for message in state.get("messages", []):
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                conversation.count_tool_call(call["name"], call.get("id"))


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
