import assert from "node:assert/strict";
import { after, afterEach, before, beforeEach, describe, it } from "node:test";
import { readFileSync } from "node:fs";
import { AuthenticationError, Hivemind, HivemindError, REMINDER } from "../dist/index.js";
import { aiSdkTools, systemPrompt } from "../dist/ai-sdk.js";
import { GOOD_KEY, INSTRUCTIONS, LOCKED_DETAIL, LOCKED_KEY, startFakeHive, type FakeHive } from "./fake-server.js";

const ENV = ["HIVEMIND_MCP_URL", "HIVEMIND_API_KEY"] as const;

let fake: FakeHive;
before(async () => {
  fake = await startFakeHive();
});
after(() => fake.close());

let saved: Record<string, string | undefined>;
beforeEach(() => {
  saved = Object.fromEntries(ENV.map((k) => [k, process.env[k]]));
  for (const k of ENV) delete process.env[k];
  fake.calls.length = 0;
});
afterEach(() => {
  for (const k of ENV) {
    if (saved[k] === undefined) delete process.env[k];
    else process.env[k] = saved[k];
  }
});

async function connect(): Promise<Hivemind> {
  return Hivemind.connect({ url: fake.url, apiKey: GOOD_KEY });
}

describe("connect", () => {
  it("opens a session and keeps whoami", async () => {
    await using hive = await connect();
    assert.equal(hive.whoami.key_kind, "agent");
    assert.equal(hive.tools.length, 5);
    assert.equal(fake.calls[0]?.name, "hive_whoami");
  });

  it("reads the environment", async () => {
    process.env.HIVEMIND_MCP_URL = fake.url;
    process.env.HIVEMIND_API_KEY = GOOD_KEY;
    await using hive = await Hivemind.connect();
    assert.equal(hive.whoami.name, "fake-agent");
  });

  it("lets options win over the environment", async () => {
    process.env.HIVEMIND_MCP_URL = "http://127.0.0.1:1/mcp";
    process.env.HIVEMIND_API_KEY = "hm_wrong";
    await using hive = await connect();
    assert.equal(hive.whoami.key_kind, "agent");
  });

  it("names the missing variable", async () => {
    await assert.rejects(Hivemind.connect({ apiKey: GOOD_KEY }), /HIVEMIND_MCP_URL/);
    await assert.rejects(Hivemind.connect({ url: fake.url }), /HIVEMIND_API_KEY/);
  });

  it("throws AuthenticationError on HTTP 401", async () => {
    await assert.rejects(Hivemind.connect({ url: fake.url, apiKey: "hm_bad" }), AuthenticationError);
  });

  it("throws key_locked with the server's message for a locked key", async () => {
    await assert.rejects(Hivemind.connect({ url: fake.url, apiKey: LOCKED_KEY }), (e: unknown) => {
      assert.ok(e instanceof AuthenticationError);
      assert.equal(e.code, "key_locked");
      assert.equal(e.message, LOCKED_DETAIL);
      return true;
    });
  });
});

describe("calls", () => {
  it("call throws HivemindError with the code", async () => {
    await using hive = await connect();
    await assert.rejects(hive.get("nope"), (e: unknown) => {
      assert.ok(e instanceof HivemindError && !(e instanceof AuthenticationError));
      assert.equal(e.code, "not_found");
      assert.equal(e.message, "no such entry");
      return true;
    });
  });

  it("runTool returns error text instead of throwing", async () => {
    await using hive = await connect();
    const text = await hive.runTool("hive_get", { entry_id: "nope" });
    assert.equal(JSON.parse(text).error.code, "not_found");
  });

  it("runTool takes a JSON string and answers unknown tools and bad JSON as invalid_input", async () => {
    await using hive = await connect();
    assert.equal(JSON.parse(await hive.runTool("hive_search", '{"query":"x"}')).count, 1);
    assert.equal(JSON.parse(await hive.runTool("hive_nothing", {})).error.code, "invalid_input");
    assert.equal(JSON.parse(await hive.runTool("hive_search", "{oops")).error.code, "invalid_input");
  });

  it("wraps a failed call outside the envelope as tool_error", async () => {
    await using hive = await connect();
    const text = await hive.runTool("hive_get", { entry_id: "crash" });
    assert.deepEqual(JSON.parse(text).error, { code: "tool_error", message: "Error executing tool hive_get" });
    await assert.rejects(hive.get("crash"), (e: unknown) => e instanceof HivemindError && e.code === "tool_error");
  });

  it("joins the text blocks of a reply and refuses a reply that is not an object", async () => {
    await using hive = await connect();
    assert.deepEqual(await hive.get("split"), { id: "split" });
    await assert.rejects(hive.get("list"), (e: unknown) => e instanceof HivemindError && e.code === "invalid_reply");
  });

  it("typed methods send only defined arguments", async () => {
    await using hive = await connect();
    fake.calls.length = 0;
    await hive.search("deploys", { limit: 3, kind: undefined, tags: ["a"] });
    await hive.write("fact", "s", { body: undefined, importance: 4 });
    await hive.feedback(["a", "b"], "helpful", { note: undefined });
    assert.deepEqual(fake.calls, [
      { name: "hive_search", args: { query: "deploys", limit: 3, tags: ["a"] } },
      { name: "hive_write", args: { kind: "fact", summary: "s", importance: 4 } },
      { name: "hive_feedback", args: { entry_ids: ["a", "b"], verdict: "helpful" } },
    ]);
  });

  it("refreshWhoami calls the tool again", async () => {
    await using hive = await connect();
    await hive.refreshWhoami();
    assert.equal(fake.calls.filter((c) => c.name === "hive_whoami").length, 2);
  });
});

describe("text and formats", () => {
  it("composes instructions from the server text and the skill body", async () => {
    await using hive = await connect();
    const skill = readFileSync(new URL("../text/skill.md", import.meta.url), "utf8");
    const body = skill.replace(/^---\n[\s\S]*?\n---\n/, "").trim();
    assert.equal(hive.instructions(), `${INSTRUCTIONS}\n\n${body}`);
    assert.ok(!hive.instructions().includes("name: hivemind"));
  });

  it("serves the reminder", async () => {
    await using hive = await connect();
    assert.equal(hive.reminder(), REMINDER);
    assert.match(REMINDER, /^HIVEMIND: before finishing/);
    assert.ok(!REMINDER.endsWith("\n"));
  });

  it("builds Anthropic and OpenAI tool definitions", async () => {
    await using hive = await connect();
    const [a] = hive.anthropicTools();
    assert.deepEqual(Object.keys(a ?? {}).sort(), ["description", "input_schema", "name"]);
    const [o] = hive.openaiTools();
    assert.equal(o?.type, "function");
    assert.equal(o?.function.name, hive.tools[0]?.name);
    assert.deepEqual(o?.function.parameters, hive.tools[0]?.inputSchema);
  });
});

describe("ai-sdk adapter", () => {
  it("keys tools by name and executes through runTool", async () => {
    await using hive = await connect();
    const tools = aiSdkTools(hive);
    assert.deepEqual(Object.keys(tools).sort(), hive.tools.map((t) => t.name).sort());
    const search = tools.hive_search;
    assert.ok(search?.execute);
    const out = await search.execute({ query: "q" }, { toolCallId: "1", messages: [] } as never);
    assert.equal(JSON.parse(out as string).count, 1);
    const err = await tools.hive_get?.execute?.({ entry_id: "x" }, { toolCallId: "2", messages: [] } as never);
    assert.equal(JSON.parse(err as string).error.code, "not_found");
    assert.equal(systemPrompt(hive), hive.instructions());
  });
});
