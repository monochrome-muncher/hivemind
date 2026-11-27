import { generateText, jsonSchema, tool, type ModelMessage, type ToolSet } from "ai";
import type { Hivemind } from "./client.js";
import type { Conversation } from "./conversation.js";

type GenerateTextOptions<TOOLS extends ToolSet> = Parameters<typeof generateText<TOOLS>>[0];
type GenerateTextResult<TOOLS extends ToolSet> = Awaited<ReturnType<typeof generateText<TOOLS>>>;

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

export interface RemindedResult<TOOLS extends ToolSet> {
  /** The task's result. */
  result: GenerateTextResult<TOOLS>;
  /** The reminder run's result, or `undefined` when no reminder was due. */
  reminderResult: GenerateTextResult<TOOLS> | undefined;
}

/**
 * `generateText`, then, if the end-of-task reminder is due, one more `generateText` with the
 * reminder as the next user message. Every tool call of every step is counted once, by its id.
 */
export async function generateTextWithReminder<TOOLS extends ToolSet>(
  conversation: Conversation,
  options: GenerateTextOptions<TOOLS>,
): Promise<RemindedResult<TOOLS>> {
  const result = await generateText(options);
  countToolCalls(conversation, result);
  const reminder = conversation.reminderDue();
  if (reminder === undefined) {
    return { result, reminderResult: undefined };
  }
  const messages: ModelMessage[] = [
    ...history(options),
    ...result.response.messages,
    { role: "user", content: reminder },
  ];
  const { prompt: _prompt, ...rest } = options as GenerateTextOptions<TOOLS> & { prompt?: unknown };
  const reminderResult = await generateText({ ...rest, messages } as GenerateTextOptions<TOOLS>);
  countToolCalls(conversation, reminderResult);
  return { result, reminderResult };
}

function history(options: object): ModelMessage[] {
  const { prompt, messages } = options as { prompt?: string | ModelMessage[]; messages?: ModelMessage[] };
  if (messages) return messages;
  if (typeof prompt === "string") return [{ role: "user", content: prompt }];
  return prompt ?? [];
}

function countToolCalls(conversation: Conversation, result: { steps: readonly { toolCalls: readonly { toolName: string; toolCallId: string }[] }[] }): void {
  for (const step of result.steps) {
    for (const call of step.toolCalls) {
      conversation.countToolCall(call.toolName, call.toolCallId);
    }
  }
}
