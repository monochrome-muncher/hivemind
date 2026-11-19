# hivemind-memory

A client for [Hivemind](https://github.com/monochrome-muncher/hivemind/blob/main/README.md), the shared memory service for AI agents. Use it when you build an agent, or a system around one, outside the harnesses the Hivemind plugin supports (the [plugin README](https://github.com/monochrome-muncher/hivemind/blob/main/plugins/hivemind/README.md) lists them).

It talks to the server's MCP endpoint (`/mcp`) and gives you three things: one method per `hive_*` tool, the tool definitions in the shape your model API wants, and the text an agent needs to use Hivemind well (the server instructions, the Hivemind skill, and the end-of-task reminder).

Node 22 or later, ESM only, types included.

## Install

```sh
npm install hivemind-memory
```

## Connect

```ts
import { Hivemind } from "hivemind-memory";

await using hive = await Hivemind.connect();
console.log(hive.whoami);
```

`await using` closes the connection at the end of the block. It needs TypeScript 5.2 or later to compile it, or Node 24; in plain JavaScript on Node 22, call `await hive.close()` in a `finally` block instead.

`connect()` reads `HIVEMIND_MCP_URL` (the `/mcp` endpoint) and `HIVEMIND_API_KEY`. You can pass `{ url, apiKey }` instead, and an option wins over the variable. If neither is set, it throws an error that names the variable. `connect()` calls `hive_whoami` and keeps the reply as `hive.whoami`; `await hive.refreshWhoami()` asks again. Without `await using`, call `await hive.close()` when you are done.

Each tool has a method: `search`, `get`, `list`, `pinned`, `write`, `feedback`, `withdraw`, `pin`, `register`. Option keys are snake_case, exactly as the tool takes them, and only the values you set are sent. Each returns the reply as a plain object. What each option means and what comes back is in the tool descriptions, `hive.tools`, which are also what the model reads; the [spec](https://github.com/monochrome-muncher/hivemind/blob/main/SPEC.md#52-mcp-tools-the-agents-mental-model--ten-verbs) has the same in one place.

```ts
const written = await hive.write("fact", "The staging DB is reset every Monday", { scope: "fleet" });
const found = await hive.search("when is staging reset?", { limit: 5 });
```

## A loop with the Anthropic Messages API

`instructions()` is the system prompt, `anthropicTools()` the tool list, and `runTool()` runs what the model asked for. When the model stops calling tools, the loop sends the end-of-task reminder once and lets the model answer it.

```ts
import Anthropic from "@anthropic-ai/sdk";
import { Hivemind } from "hivemind-memory";

async function run(task: string): Promise<void> {
  const anthropic = new Anthropic();
  await using hive = await Hivemind.connect();
  const messages: Anthropic.MessageParam[] = [{ role: "user", content: task }];
  let reminded = false;
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
      if (reminded) return;
      messages.push({ role: "user", content: hive.reminder() });
      reminded = true;
      continue;
    }
    const results: Anthropic.ToolResultBlockParam[] = [];
    for (const call of calls) {
      results.push({ type: "tool_result", tool_use_id: call.id, content: await hive.runTool(call.name, call.input as object) });
    }
    messages.push({ role: "user", content: results });
  }
}

await run("Find out when the staging database is reset.");
```

`runTool` returns the reply as JSON text and never throws on an error reply, so the model sees the refusal and can react. `openaiTools()` gives the same tools in the OpenAI function-calling shape. Neither needs the Anthropic or OpenAI package.

## The end-of-task reminder

When a task ends, send `hive.reminder()` once as a user turn and let the agent answer, as the loop above does. It asks the agent to write what others could reuse and to rate the entries its work tested. The same text is exported as `REMINDER`.

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

Set `stopWhen`: by default `generateText` stops after one step, so the model would call a tool and never see the result. Errors reach the model as text, as with `runTool`. For the end-of-task reminder, run `generateText` once more with the returned `response.messages` and `hive.reminder()` as the next user message.

## Other frameworks

If your framework supports MCP, point it at the `/mcp` URL with the header `Authorization: Bearer <key>`, and add `hive.instructions()` to the system prompt. The instructions are what teach the agent how to use the tools.

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

## Development

```sh
npm ci
npm test
```

`npm test` builds the package and runs the unit tests against a fake MCP server. `test/live.test.ts` runs against a real server when `HIVEMIND_LIVE=1`, `HIVEMIND_MCP_URL` and `HIVEMIND_API_KEY` are set. The skill and reminder under `text/` are copies of the plugin's files; do not edit them here.
