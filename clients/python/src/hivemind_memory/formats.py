"""Tool definitions as plain data for the Anthropic Messages and OpenAI Chat Completions formats."""

from __future__ import annotations

import copy
from typing import Any, TypedDict


class ToolDefinition(TypedDict):
    name: str
    description: str
    input_schema: dict[str, Any]


ToolDefinitions = list[ToolDefinition]
ToolSpecs = list[dict[str, Any]]


def anthropic_tools(tools: ToolDefinitions) -> ToolSpecs:
    return [copy.deepcopy(dict(tool)) for tool in tools]


def openai_tools(tools: ToolDefinitions) -> ToolSpecs:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": copy.deepcopy(tool["input_schema"]),
            },
        }
        for tool in tools
    ]
