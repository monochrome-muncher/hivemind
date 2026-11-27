import type { Hivemind } from "./client.js";
import { REMINDER } from "./text.js";

export const MIN_TOOL_CALLS_ENV = "HIVEMIND_STOP_MIN_TOOLS";
export const DEFAULT_MIN_TOOL_CALLS = 20;

// A Hivemind tool under any prefix a framework gives it (mcp__hivemind__hive_write, ...).
const WROTE = /(^|[^a-z])hive_write$/;
const RATED = /(^|[^a-z])hive_feedback$/;
const READ = /(^|[^a-z])hive_(search|get)$/;

function minToolCalls(): number {
  const value = process.env[MIN_TOOL_CALLS_ENV] ?? "";
  return /^[0-9]+$/.test(value) ? Number(value) : DEFAULT_MIN_TOOL_CALLS;
}

export interface ConversationOptions {
  minToolCalls?: number;
}

/**
 * Counts one conversation's tool calls and says when the end-of-task reminder is due.
 *
 * The rule is the plugin's Stop hook: once per conversation, after at least `minToolCalls` tool
 * calls (`HIVEMIND_STOP_MIN_TOOLS`, default 20), when nothing was rated yet and either nothing
 * was written or entries were read. Start one per conversation with `hive.conversation()`.
 */
export class Conversation {
  readonly minToolCalls: number;
  toolCalls = 0;
  private readonly counted = new Set<string>();
  private wrote = false;
  private rated = false;
  private read = false;
  private reminded = false;

  constructor(
    readonly hive: Hivemind,
    options: ConversationOptions = {},
  ) {
    this.minToolCalls = options.minToolCalls ?? minToolCalls();
  }

  /** `hive.runTool`, counted. */
  runTool(name: string, args: object | string = {}): Promise<string> {
    this.countToolCall(name);
    return this.hive.runTool(name, args);
  }

  /**
   * Counts a tool call this conversation made outside `runTool`, such as the app's own tools.
   * A call counted before under the same `callId` is not counted again.
   */
  countToolCall(name: string, callId?: string): void {
    if (callId !== undefined) {
      if (this.counted.has(callId)) return;
      this.counted.add(callId);
    }
    this.toolCalls += 1;
    this.wrote ||= WROTE.test(name);
    this.rated ||= RATED.test(name);
    this.read ||= READ.test(name);
  }

  /** The reminder when it is due, once; otherwise `undefined`. Send it as one more user turn. */
  reminderDue(): string | undefined {
    const quiet = this.rated || (this.wrote && !this.read);
    if (this.reminded || quiet || this.toolCalls < this.minToolCalls) {
      return undefined;
    }
    this.reminded = true;
    return REMINDER;
  }
}
