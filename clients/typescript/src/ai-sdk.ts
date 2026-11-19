import { jsonSchema, tool, type ToolSet } from "ai";
import type { Hivemind } from "./client.js";

/** The hive_* tools as AI SDK tools; an error reply reaches the model as text, not as an exception. */
export function aiSdkTools(hive: Hivemind): ToolSet {
  return Object.fromEntries(
    hive.tools.map((t) => [
      t.name,
      tool({
        description: t.description,
        inputSchema: jsonSchema(t.inputSchema as Parameters<typeof jsonSchema>[0]),
        execute: (input: unknown) => hive.runTool(t.name, (input ?? {}) as object),
      }),
    ]),
  );
}

export function systemPrompt(hive: Hivemind): string {
  return hive.instructions();
}
