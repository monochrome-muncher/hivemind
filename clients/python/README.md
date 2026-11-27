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
import asyncio

from hivemind_memory import Hivemind


async def main() -> None:
    async with Hivemind.connect() as hive:
        print(hive.whoami)
        hits = await hive.search("how do we deploy the billing service")
        print(hits["count"], [hit["summary"] for hit in hits["hits"]])
        entry = await hive.write("fact", "The billing service deploys from the release branch.")
        print(entry["id"])


asyncio.run(main())
```

Arguments win over the variables: `Hivemind.connect(url, api_key)`. Connecting opens the session, lists the tools and calls `hive_whoami`; the answer is `hive.whoami`. If you cannot use `async with`, call `hive = await Hivemind.open()` and `await hive.close()` when you are done.

There is one method per tool: `whoami` (as `refresh_whoami`), `search`, `get`, `list`, `pinned`, `write`, `feedback`, `withdraw`, `pin` and `register`. `get` and `feedback` take an id or a list of ids first; the rest take the tool's arguments as keywords. They send only the ones you set, and return the reply as a dict typed with the shapes in `hivemind_memory.replies` (`SearchReply`, `EntryDetail`, `WriteReply` and so on), so an editor or a type checker knows its keys. `get` and `feedback` return the batch shape (`Entries`, `BatchFeedbackReply`) for a list of ids, and `write(..., check=True)` returns `CheckReply`. A newer server may add keys the types do not list yet. `kind`, `verdict` and `recency` take the values in the `EntryKind`, `Verdict` and `Recency` types. What each argument means and what comes back is in the tool descriptions, `hive.tools`, which are also what the model reads; the [spec](https://github.com/monochrome-muncher/hivemind/blob/main/SPEC.md#52-mcp-tools-the-agents-mental-model--ten-verbs) has the same in one place.

## A loop with the Anthropic Messages API

`instructions()` is the system prompt, `anthropic_tools()` the tool list, and the conversation's `run_tool()` runs what the model asked for. When the model stops calling tools, the loop asks the conversation whether the end-of-task reminder is due, and if so sends it and lets the model answer it.

```python
import asyncio

import anthropic
from hivemind_memory import Hivemind


async def run(task: str) -> None:
    model = anthropic.AsyncAnthropic()
    async with Hivemind.connect() as hive:
        conversation = hive.conversation()
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
                reminder = conversation.reminder_due()
                if reminder is None:
                    return
                messages.append({"role": "user", "content": reminder})
                continue
            results = [
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": await conversation.run_tool(call.name, call.input),
                }
                for call in calls
            ]
            messages.append({"role": "user", "content": results})


asyncio.run(run("Find out how the billing service deploys."))
```

`run_tool()` (on the conversation or on `hive`) takes the arguments as the model wrote them, a dict or a JSON string, and returns the reply as text. A refusal comes back as text too, for example `{"error": {"code": "not_found", ...}}`, so the model sees the code and can react. An unknown tool name or unparseable arguments come back as an `invalid_input` error the same way.

`openai_tools()` gives the same tools in the Chat Completions `{"type": "function", "function": {...}}` format, and `openai_responses_tools()` in the flat format of the OpenAI Responses API.

## The end-of-task reminder

The reminder asks the agent to write what another agent could reuse and to rate the entries its work tested. Send it as one more user turn when a task ends and let the agent make its last tool calls, as the loop above does, but only when `reminder_due()` returns it. It follows the plugin's Stop hook: once per conversation, after at least 20 tool calls (set `HIVEMIND_STOP_MIN_TOOLS`, or `hive.conversation(min_tool_calls=...)`), and only if the agent rated nothing yet and either wrote nothing or read entries. So a short question costs no extra turn, and an agent that already rated is not asked again.

Start one `hive.conversation()` per conversation. Run the Hivemind tools through its `run_tool()`, and call `conversation.count_tool_call(name, call_id)` for every other tool call your agent makes, so the count is the whole conversation's; a call id counted before is not counted again. `hive.reminder()` and `hivemind_memory.REMINDER` are the text alone.

## After compaction

If your app compacts or summarizes a conversation's context, add `hive.after_compaction()` (`hivemind_memory.COMPACTED`) to the next system prompt or user turn. It asks the agent to write and rate what it learned before the detail is lost, as the plugin does after a compaction.

## LangChain and LangGraph

```python
from hivemind_memory import Hivemind
from hivemind_memory.langchain import langchain_tools, system_message

async with Hivemind.connect() as hive:
    tools = langchain_tools(hive)  # StructuredTools with the server's names and schemas
    system = system_message(hive)  # a SystemMessage with hive.instructions()
```

The tools work in a LangChain agent and in LangGraph. They are async only, so call them with `ainvoke` or run them in an async agent; `invoke` raises `NotImplementedError`. Like `run_tool()`, they return refusals as text.

For the end-of-task reminder, run the agent through `ainvoke_with_reminder`. It runs the agent, counts every tool call in its messages once by its id (so a checkpointer that returns the whole thread is fine), and if the reminder is due, runs it once more with the reminder as the next message. It works with any agent whose state holds `messages`, such as LangChain's `create_agent` and LangGraph graphs.

```python
from langchain.agents import create_agent
from hivemind_memory.langchain import ainvoke_with_reminder

conversation = hive.conversation()
agent = create_agent(
    model,
    tools=[*langchain_tools(hive), *my_tools],
    system_prompt=hive.instructions(),
)
state, reminded = await ainvoke_with_reminder(conversation, agent, {"messages": [("user", task)]})
answer = state["messages"][-1].content  # reminded is None, or the state after the reminder
```

## Claude Agent SDK

The Claude Agent SDK runs Claude Code underneath, so it can load the Hivemind plugin itself. The agent then gets what Claude Code gets: the SessionStart and Stop hooks, the skills and the MCP server. You do not need this package for that. Clone the repository, set `HIVEMIND_MCP_URL` and `HIVEMIND_API_KEY` in the environment, and point the `plugins` option at the clone's `plugins/hivemind` folder. Use an absolute path or one relative to `cwd`: the SDK does not expand `~`, and it skips a path that does not exist without an error.

```python
from claude_agent_sdk import ClaudeAgentOptions, query

options = ClaudeAgentOptions(plugins=[{"type": "local", "path": "/opt/hivemind/plugins/hivemind"}])
async for message in query(prompt=task, options=options):
    print(message)
```

The `init` system message lists `hivemind` under `plugins` and `plugin:hivemind:hivemind` under `mcp_servers`; check it there if the tools are missing. Checked with `claude-agent-sdk` 0.2.189 for Python.

## Other frameworks

Most agent frameworks can use an MCP server directly. Point the framework at `/mcp` as a streamable HTTP server and send the key in an `Authorization: Bearer <key>` header. Then put `hivemind_memory.INSTRUCTIONS` in the system prompt: it is the same text as `hive.instructions()` and needs no connection. Many frameworks do not show the server's own instructions to the model, so add it even when the framework reads them.

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

Network and HTTP failures (an unreachable server, a 503 while the database is busy) raise the errors of `httpx2` and the MCP SDK unchanged, from the methods and from `run_tool()` alike.

## Development

```
uv venv --python 3.10 .venv
uv pip install -e ".[langchain]" --group test
.venv/bin/python -m pytest
```

`tests/test_live.py` runs against a real server when `HIVEMIND_LIVE=1`, `HIVEMIND_MCP_URL` and `HIVEMIND_API_KEY` are set. The instructions, skill and reminder under `src/hivemind_memory/text/` are generated from the plugin's files by `make sync-client-text`; do not edit them here.
