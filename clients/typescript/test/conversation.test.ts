import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";
import { stepCountIs, tool, jsonSchema } from "ai";
import { MockLanguageModelV4 } from "ai/test";
import { COMPACTED, Conversation, Hivemind, REMINDER } from "../dist/index.js";
import { aiSdkTools, generateTextWithReminder } from "../dist/ai-sdk.js";
import { GOOD_KEY, startFakeHive, type FakeHive } from "./fake-server.js";

let fake: FakeHive;
before(async () => {
  fake = await startFakeHive();
});
after(async () => {
  await fake.close();
});

const connect = () => Hivemind.connect({ url: fake.url, apiKey: GOOD_KEY });

function calls(conversation: Conversation, ...names: string[]): void {
  for (const name of names) conversation.countToolCall(name);
}

describe("conversation reminder rule", () => {
  it("is quiet below the threshold, then due once", async () => {
    await using hive = await connect();
    const conversation = hive.conversation({ minToolCalls: 3 });
    calls(conversation, "bash", "hive_search");
    assert.equal(conversation.reminderDue(), undefined);
    calls(conversation, "bash");
    assert.equal(conversation.reminderDue(), REMINDER);
    assert.equal(conversation.reminderDue(), undefined);
  });

  it("is quiet once something was rated, under any prefix", async () => {
    await using hive = await connect();
    const conversation = hive.conversation({ minToolCalls: 1 });
    calls(conversation, "mcp__hivemind__hive_feedback");
    assert.equal(conversation.reminderDue(), undefined);
  });

  it("is quiet after a write without reads, due after a write with reads", async () => {
    await using hive = await connect();
    const wroteOnly = hive.conversation({ minToolCalls: 1 });
    calls(wroteOnly, "hive_write");
    assert.equal(wroteOnly.reminderDue(), undefined);
    const wroteAndRead = hive.conversation({ minToolCalls: 1 });
    calls(wroteAndRead, "hive_get", "hive_write");
    assert.equal(wroteAndRead.reminderDue(), REMINDER);
  });

  it("takes the threshold from HIVEMIND_STOP_MIN_TOOLS, else 20", async () => {
    await using hive = await connect();
    assert.equal(hive.conversation().minToolCalls, 20);
    process.env.HIVEMIND_STOP_MIN_TOOLS = "7";
    try {
      assert.equal(hive.conversation().minToolCalls, 7);
      process.env.HIVEMIND_STOP_MIN_TOOLS = "seven";
      assert.equal(hive.conversation().minToolCalls, 20);
    } finally {
      delete process.env.HIVEMIND_STOP_MIN_TOOLS;
    }
  });

  it("counts a call id once", async () => {
    await using hive = await connect();
    const conversation = hive.conversation();
    conversation.countToolCall("bash", "c1");
    conversation.countToolCall("bash", "c1");
    conversation.countToolCall("bash");
    assert.equal(conversation.toolCalls, 2);
  });

  it("counts the calls it runs", async () => {
    await using hive = await connect();
    const conversation = hive.conversation();
    const reply = await conversation.runTool("hive_search", { query: "q" });
    assert.equal(JSON.parse(reply).count, 1);
    assert.equal(conversation.toolCalls, 1);
  });

  it("serves the post-compaction line", async () => {
    await using hive = await connect();
    assert.equal(hive.afterCompaction(), COMPACTED);
    assert.match(COMPACTED, /^Context was just compacted/);
  });
});

const usage = {
  inputTokens: { total: 1, noCache: 1, cacheRead: 0, cacheWrite: 0 },
  outputTokens: { total: 1, text: 1, reasoning: 0 },
} as never;

function scriptedModel(steps: ({ call: string; input: object } | { text: string })[]) {
  const prompts: unknown[] = [];
  const model = new MockLanguageModelV4({
    doGenerate: async (options) => {
      prompts.push(options.prompt);
      const step = steps.shift();
      assert.ok(step, "the model was called more often than scripted");
      if ("text" in step) {
        return { content: [{ type: "text", text: step.text }], finishReason: { unified: "stop", raw: "stop" }, usage, warnings: [] };
      }
      return {
        content: [{ type: "tool-call", toolCallId: `c${prompts.length}`, toolName: step.call, input: JSON.stringify(step.input) }],
        finishReason: { unified: "tool-calls", raw: "tool_use" },
        usage,
        warnings: [],
      };
    },
  });
  return { model, prompts };
}

const appTool = tool({ description: "an app tool", inputSchema: jsonSchema({ type: "object" }), execute: async () => "ok" });

describe("generateTextWithReminder", () => {
  it("runs once more with the reminder when it is due", async () => {
    await using hive = await connect();
    const conversation = hive.conversation({ minToolCalls: 2 });
    const { model, prompts } = scriptedModel([
      { call: "hive_search", input: { query: "q" } },
      { call: "app_tool", input: {} },
      { text: "the answer" },
      { text: "nothing new" },
    ]);
    const { result, reminderResult } = await generateTextWithReminder(conversation, {
      model,
      prompt: "the task",
      tools: { ...aiSdkTools(hive), app_tool: appTool },
      stopWhen: stepCountIs(5),
    });
    assert.equal(result.text, "the answer");
    assert.equal(reminderResult?.text, "nothing new");
    assert.equal(conversation.toolCalls, 2);
    assert.match(JSON.stringify(prompts.at(-1)), /HIVEMIND: before finishing/);
    assert.match(JSON.stringify(prompts.at(-1)), /the task/);
  });

  it("carries every step of the task into the reminder run, in order", async () => {
    await using hive = await connect();
    const conversation = hive.conversation({ minToolCalls: 1 });
    const { model, prompts } = scriptedModel([
      { call: "app_tool", input: {} },
      { text: "the answer" },
      { text: "nothing new" },
    ]);
    await generateTextWithReminder(conversation, {
      model,
      prompt: "the task",
      tools: { app_tool: appTool },
      stopWhen: stepCountIs(5),
    });
    const sent = (prompts.at(-1) as { role: string; content: unknown }[]).map((m) => JSON.stringify(m));
    const at = (pattern: RegExp) => sent.findIndex((m) => pattern.test(m));
    const order = [/"role":"user".*the task/, /"role":"assistant".*tool-call/, /"role":"tool".*tool-result/, /"role":"assistant".*the answer/, /HIVEMIND: before finishing/].map(at);
    assert.ok(order.every((i) => i >= 0), `missing a message in ${sent.join("\n")}`);
    assert.deepEqual(order, [...order].sort((a, b) => a - b));
  });

  it("does not remind a run that ended waiting for a tool approval", async () => {
    await using hive = await connect();
    const conversation = hive.conversation({ minToolCalls: 1 });
    const { model } = scriptedModel([{ call: "app_tool", input: {} }]);
    const { reminderResult } = await generateTextWithReminder(conversation, {
      model,
      prompt: "the task",
      tools: { app_tool: tool({ description: "needs approval", inputSchema: jsonSchema({ type: "object" }), needsApproval: true, execute: async () => "ok" }) },
      stopWhen: stepCountIs(5),
    });
    assert.equal(reminderResult, undefined);
    assert.equal(conversation.toolCalls, 1);
    assert.equal(conversation.reminderDue(), REMINDER);
  });

  it("stops after the task when the reminder is not due", async () => {
    await using hive = await connect();
    const conversation = hive.conversation({ minToolCalls: 5 });
    const { model } = scriptedModel([{ call: "hive_search", input: { query: "q" } }, { text: "the answer" }]);
    const { result, reminderResult } = await generateTextWithReminder(conversation, {
      model,
      prompt: "the task",
      tools: aiSdkTools(hive),
      stopWhen: stepCountIs(5),
    });
    assert.equal(result.text, "the answer");
    assert.equal(reminderResult, undefined);
  });
});
