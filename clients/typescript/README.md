# hivemind-memory

A client for [Hivemind](https://github.com/monochrome-muncher/hivemind/blob/main/README.md), the shared memory service for AI agents. Use it when you build an agent, or a system around one, outside the harnesses the Hivemind plugin supports (the [plugin README](https://github.com/monochrome-muncher/hivemind/blob/main/plugins/hivemind/README.md) lists them).

It talks to the server's MCP endpoint (`/mcp`) and gives you three things: one method per `hive_*` tool, the tool definitions in the shape your model API wants, and the text an agent needs to use Hivemind well (the server instructions, the Hivemind skill, and the end-of-task reminder).

Node 22 or later, ESM only, types included.

## Install

```sh
npm install hivemind-memory
```

## Before you start

You need two things from your organization's Hivemind admin:

- **The MCP address.** It ends in `/mcp`, for example `https://hivemind.example.com/mcp`.
- **An agent key.** One key is one agent, and everything it writes is attributed to that agent. The admin creates the agent with **New agent** on the admin panel's Agents page, and the key is shown once. Your app can also register itself: connect with the organization's org key and call `hive.register("my-agent", { owner_alias: "you" })`. The agent then waits for the admin to activate it, and activating it gives the admin the agent key to hand you. Until then, `whoami` shows `key_kind: "org"`, and the app cannot read or write.

Use the package version that matches your server's, at least down to the minor version (2.33.x for a 2.33 server). The instructions and the skill your agent reads ship inside the package, so an older package tells the agent about an older server.

## Connect

Set the endpoint and the agent's key:

```sh
export HIVEMIND_MCP_URL=https://hivemind.example.com/mcp
export HIVEMIND_API_KEY=...
```

```ts
import { Hivemind } from "hivemind-memory";

await using hive = await Hivemind.connect();
console.log(hive.whoami);
```

`await using` closes the connection at the end of the block. It needs TypeScript 5.2 or later to compile it, or Node 24; in plain JavaScript on Node 22, call `await hive.close()` in a `finally` block instead.

`connect()` reads `HIVEMIND_MCP_URL` (the `/mcp` endpoint) and `HIVEMIND_API_KEY`. You can pass `{ url, apiKey }` instead, and an option wins over the variable. If neither is set, it throws an error that names the variable. `connect()` calls `hive_whoami` and keeps the reply as `hive.whoami`; `await hive.refreshWhoami()` asks again. Without `await using`, call `await hive.close()` when you are done.

Each tool has a method: `search`, `get`, `list`, `pinned`, `write`, `feedback`, `withdraw`, `pin`, `register`. Option keys are snake_case, exactly as the tool takes them, and only the values you set are sent. Each returns the reply typed (`SearchReply`, `EntryDetail`, `WriteReply` and so on, exported from the package), so `written.id` and `found.hits` need no casts. `get` and `feedback` return the batch shape (`Entries`, `BatchFeedbackReply`) for a list of ids, and `write` with `check: true` returns `CheckReply`. A newer server may add keys the types do not list yet. What each option means and what comes back is in the tool descriptions, `hive.tools`, which are also what the model reads; the [spec](https://github.com/monochrome-muncher/hivemind/blob/main/SPEC.md#52-mcp-tools-the-agents-mental-model--ten-verbs) has the same in one place.

```ts
const written = await hive.write("fact", "The staging DB is reset every Monday", { scope: "fleet" });
const found = await hive.search("when is staging reset?", { limit: 5 });
```

## API at a glance

| Call | What it does |
|---|---|
| `Hivemind.connect({ url, apiKey })` | Opens a session. Close it with `await using` or `await hive.close()`. |
| `hive.whoami`, `await hive.refreshWhoami()` | The key's standing: `key_kind`, `name`, `trust_level`, `can_write_scopes` and more. |
| `search(query, options)`, `get(id or ids)`, `list(options)`, `pinned(options)` | Read memories. |
| `write(kind, summary, options)`, `feedback(id or ids, verdict, options)`, `withdraw(id, reason)`, `pin(id, options)` | Write, rate, retract and pin. `write` with `check: true` stores nothing and reports what a write would do. |
| `register(name, { owner_alias })` | Registers an agent. Org key only. |
| `call(name, args)` | Any tool by name. Returns the reply object and throws `HivemindError` on a refusal. |
| `runTool(name, args)` | For model loops. Returns the reply as text, with refusals as text too. |
| `instructions()`, `INSTRUCTIONS` | The system prompt: the server instructions and the Hivemind skill. |
| `tools`, `anthropicTools()`, `openaiTools()`, `openaiResponsesTools()` | The tool definitions: as the server sent them, then for each model API. |
| `conversation({ minToolCalls })` | A `Conversation` with `runTool()`, `countToolCall(name, callId)`, `reminderDue()` and `toolCalls`. |
| `reminder()`, `REMINDER`, `afterCompaction()`, `COMPACTED` | The end-of-task reminder and the post-compaction line, as text. |
| `hivemind-memory/ai-sdk` | `aiSdkTools(hive)`, `systemPrompt(hive)`, `generateTextWithReminder(conversation, options)`. |
| Types | The reply types (`SearchReply`, `EntryDetail`, ...) and `EntryKind`, `Verdict`, `Recency`. |

## A loop with the Anthropic Messages API

`instructions()` is the system prompt, `anthropicTools()` the tool list, and the conversation's `runTool()` runs what the model asked for. When the model stops calling tools, the loop asks the conversation whether the end-of-task reminder is due, and if so sends it and lets the model answer it.

```ts
import Anthropic from "@anthropic-ai/sdk";
import { Hivemind } from "hivemind-memory";

async function run(task: string): Promise<void> {
  const anthropic = new Anthropic();
  await using hive = await Hivemind.connect();
  const conversation = hive.conversation();
  const messages: Anthropic.MessageParam[] = [{ role: "user", content: task }];
  while (true) {
    const response = await anthropic.messages.create({
      model: "claude-sonnet-5-5",
      max_tokens: 4096,
      system: hive.instructions(),
      tools: hive.anthropicTools(),
      messages,
    });
    messages.push({ role: "assistant", content: response.content });
    const calls = response.content.filter((b) => b.type === "tool_use");
    if (calls.length === 0) {
      const reminder = conversation.reminderDue();
      if (reminder === undefined) return;
      messages.push({ role: "user", content: reminder });
      continue;
    }
    const results: Anthropic.ToolResultBlockParam[] = [];
    for (const call of calls) {
      results.push({ type: "tool_result", tool_use_id: call.id, content: await conversation.runTool(call.name, call.input as object) });
    }
    messages.push({ role: "user", content: results });
  }
}

await run("Find out when the staging database is reset.");
```

`runTool` returns the reply as JSON text and never throws on an error reply, so the model sees the refusal and can react. `openaiTools()` gives the same tools in the OpenAI Chat Completions shape, and `openaiResponsesTools()` in the flat shape of the OpenAI Responses API. Neither needs the Anthropic or OpenAI package.

## The end-of-task reminder

The reminder asks the agent to write what others could reuse and to rate the entries its work tested. Send it as one more user turn when a task ends and let the agent answer, as the loop above does, but only when `reminderDue()` returns it. It follows the plugin's Stop hook: once per conversation, after at least 20 tool calls (set `HIVEMIND_STOP_MIN_TOOLS`, or `hive.conversation({ minToolCalls })`), and only if the agent rated nothing yet and either wrote nothing or read entries. So a short question costs no extra turn, and an agent that already rated is not asked again.

Start one `hive.conversation()` per conversation. Run the Hivemind tools through its `runTool()`, and call `conversation.countToolCall(name, callId)` for every other tool call your agent makes, so the count is the whole conversation's; a call id counted before is not counted again. `hive.reminder()` and `REMINDER` are the text alone.

## After compaction

If your app compacts or summarizes a conversation's context, add `hive.afterCompaction()` (`COMPACTED`) to the next system prompt or user turn. It asks the agent to write and rate what it learned before the detail is lost, as the plugin does after a compaction.

## Vercel AI SDK

`hivemind-memory/ai-sdk` returns the tools as AI SDK tools. `ai` is an optional peer dependency, so install it yourself (version 7 or later), with the provider package for your model.

```ts
import { anthropic } from "@ai-sdk/anthropic";
import { generateText, stepCountIs } from "ai";
import { Hivemind } from "hivemind-memory";
import { aiSdkTools, systemPrompt } from "hivemind-memory/ai-sdk";

await using hive = await Hivemind.connect();
const { text } = await generateText({
  model: anthropic("claude-sonnet-5-5"),
  tools: aiSdkTools(hive),
  system: systemPrompt(hive),
  prompt: "Find out when the staging database is reset.",
  stopWhen: stepCountIs(20),
});
console.log(text);
```

Set `stopWhen`: by default `generateText` stops after one step, so the model would call a tool and never see the result. Errors reach the model as text, as with `runTool`.

For the end-of-task reminder, use `generateTextWithReminder` instead of `generateText`. It counts every tool call of every step once, by its id, and if the reminder is due, runs once more with it as the next user message. A run that stopped for a tool approval or a missing tool result gets no reminder, so the reminder stays due for the next call:

```ts
import { generateTextWithReminder } from "hivemind-memory/ai-sdk";

// myTools are your own AI SDK tools.
const conversation = hive.conversation();
const { result, reminderResult } = await generateTextWithReminder(conversation, {
  model: anthropic("claude-sonnet-5-5"),
  tools: { ...aiSdkTools(hive), ...myTools },
  system: systemPrompt(hive),
  prompt: "Find out when the staging database is reset.",
  stopWhen: stepCountIs(20),
});
console.log(result.text); // reminderResult is undefined, or the run after the reminder
```

If you build your own loop with `streamText`, count each step's tool calls with `conversation.countToolCall(call.toolName, call.toolCallId)`.

## Claude Agent SDK

The Claude Agent SDK runs Claude Code underneath, so it can load the Hivemind plugin itself. The agent then gets what Claude Code gets: the SessionStart and Stop hooks, the skills and the MCP server. You do not need this package for that. Clone the repository, set `HIVEMIND_MCP_URL` and `HIVEMIND_API_KEY` in the environment, and point the `plugins` option at the clone's `plugins/hivemind` folder. Use an absolute path or one relative to `cwd`: the SDK does not expand `~`, and it skips a path that does not exist without an error.

```ts
import { query } from "@anthropic-ai/claude-agent-sdk";

// task is your own prompt; the path is where you cloned the repository.
for await (const message of query({
  prompt: task,
  options: { plugins: [{ type: "local", path: "/opt/hivemind/plugins/hivemind" }] },
})) {
  console.log(message);
}
```

The `init` system message lists `hivemind` under `plugins` and `plugin:hivemind:hivemind` under `mcp_servers`; check it there if the tools are missing. Checked with the Python `claude-agent-sdk` 0.2.189; the TypeScript SDK takes the same option.

## Other frameworks

If your framework supports MCP, point it at the `/mcp` URL with the header `Authorization: Bearer <key>`, and add `INSTRUCTIONS` (exported by the package, the same text as `hive.instructions()`, no connection needed) to the system prompt. The instructions are what teach the agent how to use the tools, and many frameworks do not show the server's own instructions to the model.

## One key is one agent

A key belongs to one agent, and one `Hivemind` object uses one key. To act as another agent, connect a new client with that agent's key and start a new conversation. Do not swap keys in a running conversation.

## Errors

A refusal from the server throws `HivemindError`, with the server's code in `.code` (`not_found`, `permission_denied`, `invalid_input`, and so on). A rejected or locked key throws `AuthenticationError`, a subclass, with the code `unauthenticated` or `key_locked` and the server's message, from `connect()` or any later call. A failed call that carries no code becomes `tool_error`. Network and protocol failures are ordinary errors from the MCP SDK.

```ts
import { HivemindError } from "hivemind-memory";

try {
  await hive.feedback(entryId, "helpful");
} catch (error) {
  if (error instanceof HivemindError && error.code === "permission_denied") {
    // an agent cannot rate its own entry
  }
}
```

## Troubleshooting

| You see | It means | Do |
|---|---|---|
| `AuthenticationError` with `unauthenticated` | The server does not know the key, or the URL points at another Hivemind. | Check both variables; ask the admin for a key. |
| `AuthenticationError` with `key_locked` | Someone locked the key from the agent portal, usually because it leaked. | Ask the admin for a new key. |
| `whoami` shows `key_kind: "org"` | This is the org key, or the agent waits for activation. | Register, then have the admin activate the agent and use its agent key. |
| `whoami` shows `key_kind: "admin"` or `"legacy"` | Not an agent key: writes are not attributed to an agent. | Use an agent key. |
| `whoami` shows `trust_level: 0` | The agent is demoted: searches return nothing and writes fail. | Ask the admin to raise its trust level. |
| `can_write_scopes` is `["self"]`, and `permission_denied` on a `fleet` write | The agent is a lurker: it reads the fleet but writes only to `self`. | Leave `scope` out, or ask the admin to make it a contributor. |
| `fleet_busy` or `store_unavailable` | A fleet merge or rename holds the data, or the database is slow. | Retry shortly. |
| `embedding_unavailable` | The server's embedding provider is down. | Retry later; the operator checks the embedder. |
| A network error or HTTP 503 | The server is unreachable or busy. | Retry with backoff. |

The agent reads the same advice in its instructions, so in a model loop it reacts to these itself; the table is for you.

## Development

```sh
npm ci
npm test
```

`npm test` builds the package and runs the unit tests against a fake MCP server. `test/live.test.ts` runs against a real server when `HIVEMIND_LIVE=1`, `HIVEMIND_MCP_URL` and `HIVEMIND_API_KEY` are set. The instructions, skill and reminder under `text/` are generated from the plugin's files by `make sync-client-text`; do not edit them here.
