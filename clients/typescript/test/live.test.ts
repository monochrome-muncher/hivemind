import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { Hivemind, HivemindError } from "../dist/index.js";
import { aiSdkTools } from "../dist/ai-sdk.js";

const live = process.env.HIVEMIND_LIVE === "1";

describe("live server", { skip: !live && "set HIVEMIND_LIVE=1 to run" }, () => {
  it("writes, finds, reads, refuses self-rating and withdraws", async () => {
    assert.ok(process.env.HIVEMIND_MCP_URL && process.env.HIVEMIND_API_KEY, "HIVEMIND_MCP_URL and HIVEMIND_API_KEY are required");
    await using hive = await Hivemind.connect();
    assert.equal(hive.whoami.key_kind, "agent");

    const summary = `ts client live check ${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const written = await hive.write("fact", summary, { scope: "fleet" });
    const id = String(written.id);
    assert.ok(id);

    const found = await hive.search(summary);
    assert.ok((found.hits as { id: string }[]).some((h) => h.id === id));

    const entry = await hive.get(id);
    assert.equal(entry.summary, summary);

    await assert.rejects(hive.feedback(id, "helpful"), (e: unknown) => {
      assert.ok(e instanceof HivemindError);
      assert.equal(e.code, "permission_denied");
      return true;
    });

    const withdrawn = await hive.withdraw(id, "live test cleanup");
    assert.equal(withdrawn.state, "withdrawn");

    const search = aiSdkTools(hive).hive_search;
    const text = await search?.execute?.({ query: summary }, { toolCallId: "live", messages: [] } as never);
    assert.ok(Array.isArray(JSON.parse(text as string).hits));
  });
});
