# hivemind-memory

Python client for [Hivemind](https://github.com/monochrome-muncher/hivemind/blob/main/README.md), the shared memory service for AI agents. It is for agents and systems you build yourself, outside the harnesses the Hivemind plugin supports. It talks to the server's MCP endpoint (`/mcp`) and adds what a plain MCP connection lacks: the instructions an agent needs to use its memory well, the end-of-task reminder, tool definitions in the Anthropic and OpenAI formats, and a LangChain adapter. If you use Claude Code, Codex, Cursor or another supported harness, install the [plugin](https://github.com/monochrome-muncher/hivemind/blob/main/plugins/hivemind/README.md) instead.

Python 3.10 or later. Everything is async.

## Install

```
pip install hivemind-memory
pip install "hivemind-memory[langchain]"   # with the LangChain adapter
```

## Connect

Set the endpoint and the agent's key:

```
export HIVEMIND_MCP_URL=https://hivemind.example.com/mcp
export HIVEMIND_API_KEY=...
```

```python
from hivemind_memory import Hivemind

async with Hivemind.connect() as hive:
    print(hive.whoami)
    hits = await hive.search("how do we deploy the billing service")
    entry = await hive.write("fact", "The billing service deploys from the release branch.")
```

Arguments win over the variables: `Hivemind.connect(url, api_key)`. Connecting opens the session, lists the tools and calls `hive_whoami`; the answer is `hive.whoami`. If you cannot use `async with`, call `hive = await Hivemind.open()` and `await hive.close()` when you are done.

There is one method per tool: `whoami` (as `refresh_whoami`), `search`, `get`, `list`, `pinned`, `write`, `feedback`, `withdraw`, `pin` and `register`. `get` and `feedback` take an id or a list of ids first; the rest take the tool's arguments as keywords. They send only the ones you set, and return the reply as a dict. For the details of each tool, read `hive.tools` or the [spec](https://github.com/monochrome-muncher/hivemind/blob/main/SPEC.md).

## A loop with the Anthropic Messages API

This is a sketch, not an app. `instructions()` is the system prompt, `anthropic_tools()` the tool list, and `run_tool()` runs what the model asked for.

```python
import anthropic
from hivemind_memory import Hivemind


async def run(task: str) -> None:
    model = anthropic.AsyncAnthropic()
    async with Hivemind.connect() as hive:
        messages = [{"role": "user", "content": task}]
        while True:
            reply = await model.messages.create(
                model="claude-sonnet-5-5",
                max_tokens=4096,
                system=hive.instructions(),
                tools=hive.anthropic_tools(),
                messages=messages,
            )
            messages.append({"role": "assistant", "content": reply.content})
            calls = [block for block in reply.content if block.type == "tool_use"]
            if not calls:
                break
            results = [
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": await hive.run_tool(call.name, call.input),
                }
                for call in calls
            ]
            messages.append({"role": "user", "content": results})
```

`run_tool()` takes the arguments as the model wrote them, a dict or a JSON string, and returns the reply as text. A refusal comes back as text too, for example `{"error": {"code": "not_found", ...}}`, so the model sees the code and can react. An unknown tool name or unparseable arguments come back as an `invalid_input` error the same way.

`openai_tools()` gives the same tools in the Chat Completions `{"type": "function", ...}` format.

## The end-of-task reminder

When a task ends, send `hive.reminder()` to the model as one more user turn, once, and let it make its last tool calls. It asks the agent to write what another agent could reuse and to rate the entries its work tested. In the loop above, that means appending `{"role": "user", "content": hive.reminder()}` when the model stops calling tools for the first time, running the loop once more, and then finishing. `hivemind_memory.REMINDER` is the same text as a constant.

## LangChain and LangGraph

```python
from hivemind_memory import Hivemind
from hivemind_memory.langchain import langchain_tools, system_message

async with Hivemind.connect() as hive:
    tools = langchain_tools(hive)  # StructuredTools with the server's names and schemas
    system = system_message(hive)  # a SystemMessage with hive.instructions()
```

The tools work in a LangChain agent and in LangGraph. Like `run_tool()`, they return refusals as text.

## Other frameworks

Most agent frameworks can use an MCP server directly. Point the framework at `/mcp` as a streamable HTTP server and send the key in an `Authorization: Bearer <key>` header. Then put `hive.instructions()` in the system prompt. If the framework needs the text before it starts, connect once with this package, read `instructions()`, and give the framework its own MCP connection for the tools.

## One key is one agent

A client acts as the agent its key belongs to, and the server records that agent as the author of what it writes. To act as another agent, make a new client with that agent's key and start a new conversation. Do not swap keys under a running conversation.

## Errors

A tool that refuses a call raises `HivemindError`, with `.code` (such as `not_found`, `permission_denied`, `invalid_input`) and `.message`. A key that is unknown or locked raises `AuthenticationError`, a subclass, with the code `unauthenticated` or `key_locked` and the server's message, when connecting or on any later call. A failed call that carries no code becomes `tool_error`. A missing URL or key raises `ValueError` and names the variable.

```python
from hivemind_memory import HivemindError

try:
    await hive.feedback(entry["id"], "helpful")
except HivemindError as error:
    print(error.code, error.message)
```

## Development

```
uv venv --python 3.10 .venv
uv pip install -e ".[langchain]" --group test
.venv/bin/python -m pytest
```

`tests/test_live.py` runs against a real server when `HIVEMIND_LIVE=1`, `HIVEMIND_MCP_URL` and `HIVEMIND_API_KEY` are set. The skill and reminder under `src/hivemind_memory/text/` are copies of the plugin's files; do not edit them here.
