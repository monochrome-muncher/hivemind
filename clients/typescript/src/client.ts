import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { Conversation, type ConversationOptions } from "./conversation.js";
import { HivemindError, refusal, rejectedKey } from "./errors.js";
import { toAnthropic, toOpenAI, type AnthropicTool, type OpenAITool, type ToolDefinition } from "./formats.js";
import { COMPACTED, INSTRUCTIONS, REMINDER, VERSION } from "./text.js";
import type {
  BatchFeedbackReply,
  CheckReply,
  Entries,
  Entry,
  EntryDetail,
  EntryKind,
  FeedbackOptions,
  FeedbackReply,
  GetOptions,
  ListOptions,
  ListReply,
  PinOptions,
  PinReply,
  PinnedOptions,
  PinnedReply,
  RegisterOptions,
  RegisterReply,
  Reply,
  SearchOptions,
  SearchReply,
  Verdict,
  WhoAmI,
  WriteOptions,
  WriteReply,
} from "./types.js";

export interface ConnectOptions {
  url?: string;
  apiKey?: string;
}

function setting(value: string | undefined, variable: string): string {
  const resolved = value ?? process.env[variable];
  if (!resolved) {
    throw new Error(`Hivemind needs ${variable} (or the matching connect option)`);
  }
  return resolved;
}

function defined(args: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(args).filter(([, v]) => v !== undefined));
}

function errorText(code: string, message: string): string {
  return JSON.stringify({ error: { code, message } });
}

function parsed(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

/** Keeps why the server answered 401: the MCP SDK reports only the status. */
class Rejection {
  private seen: Promise<HivemindError> | undefined;

  readonly fetch = async (url: string | URL, init?: RequestInit): Promise<Response> => {
    const response = await fetch(url, init);
    if (response.status === 401) {
      this.seen = rejectedKey(response.clone());
    }
    return response;
  };

  async throwIfSeen(): Promise<void> {
    if (this.seen) {
      throw await this.seen;
    }
  }

  forget(): void {
    this.seen = undefined;
  }
}

export class Hivemind implements AsyncDisposable {
  private constructor(
    private readonly client: Client,
    private readonly rejection: Rejection,
    readonly tools: readonly ToolDefinition[],
    private current: WhoAmI,
  ) {}

  /** The key's standing from the last hive_whoami. */
  get whoami(): WhoAmI {
    return this.current;
  }

  static async connect(options: ConnectOptions = {}): Promise<Hivemind> {
    const url = setting(options.url, "HIVEMIND_MCP_URL");
    const apiKey = setting(options.apiKey, "HIVEMIND_API_KEY");
    const client = new Client({ name: "hivemind-memory", version: VERSION });
    const rejection = new Rejection();
    const transport = new StreamableHTTPClientTransport(new URL(url), {
      requestInit: { headers: { Authorization: `Bearer ${apiKey}` } },
      fetch: rejection.fetch,
    });
    try {
      await client.connect(transport);
      const listed = await client.listTools();
      const tools = listed.tools.map((t) => ({
        name: t.name,
        description: t.description ?? "",
        inputSchema: t.inputSchema as Record<string, unknown>,
      }));
      const hive = new Hivemind(client, rejection, tools, {} as WhoAmI);
      await hive.refreshWhoami();
      return hive;
    } catch (error) {
      await client.close().catch(() => undefined);
      await rejection.throwIfSeen();
      throw error;
    }
  }

  async close(): Promise<void> {
    await this.client.close();
  }

  async [Symbol.asyncDispose](): Promise<void> {
    await this.close();
  }

  /** The reply text; a failed call that is not already the error envelope is wrapped in one. */
  private async callText(name: string, args: Record<string, unknown>): Promise<string> {
    let result: Awaited<ReturnType<Client["callTool"]>>;
    try {
      result = await this.client.callTool({ name, arguments: args });
    } catch (error) {
      await this.rejection.throwIfSeen();
      throw error;
    }
    this.rejection.forget();
    const blocks = (result.content ?? []) as { type: string; text?: string }[];
    const text = blocks.map((b) => (b.type === "text" ? (b.text ?? "") : "")).join("");
    if (result.isError && !refusal(parsed(text))) {
      return errorText("tool_error", text || `${name} failed`);
    }
    return text;
  }

  async call(name: string, args: Record<string, unknown> = {}): Promise<Reply> {
    const reply = parsed(await this.callText(name, args));
    const error = refusal(reply);
    if (error) {
      throw error;
    }
    if (typeof reply !== "object" || reply === null || Array.isArray(reply)) {
      throw new HivemindError("invalid_reply", `${name} did not answer with a JSON object`);
    }
    return reply as Reply;
  }

  /** For model loops: the reply JSON text, and an error envelope as text rather than a throw. */
  async runTool(name: string, args: object | string = {}): Promise<string> {
    if (!this.tools.some((t) => t.name === name)) {
      return errorText("invalid_input", `unknown tool ${name}`);
    }
    let input: unknown = args;
    if (typeof args === "string") {
      input = args.trim() === "" ? {} : parsed(args);
      if (input === undefined) {
        return errorText("invalid_input", `arguments for ${name} are not valid JSON`);
      }
    }
    if (typeof input !== "object" || input === null || Array.isArray(input)) {
      return errorText("invalid_input", `arguments for ${name} must be a JSON object`);
    }
    return this.callText(name, input as Record<string, unknown>);
  }

  async refreshWhoami(): Promise<WhoAmI> {
    this.current = (await this.call("hive_whoami", {})) as unknown as WhoAmI;
    return this.current;
  }

  private typed<T>(name: string, args: Record<string, unknown>): Promise<T> {
    return this.call(name, defined(args)) as Promise<unknown> as Promise<T>;
  }

  search(query: string, options: SearchOptions = {}): Promise<SearchReply> {
    return this.typed("hive_search", { query, ...options });
  }

  /** One entry by id, or up to 10 at once when given a list of ids. */
  get(entryId: string, options?: GetOptions): Promise<EntryDetail>;
  get(entryIds: string[]): Promise<Entries>;
  get(entryId: string | string[], options: GetOptions = {}): Promise<EntryDetail | Entries> {
    const target = typeof entryId === "string" ? { entry_id: entryId } : { entry_ids: entryId };
    return this.typed("hive_get", { ...target, ...options });
  }

  list(options: ListOptions = {}): Promise<ListReply> {
    return this.typed("hive_list", { ...options });
  }

  pinned(options: PinnedOptions = {}): Promise<PinnedReply> {
    return this.typed("hive_pinned", { ...options });
  }

  /** Writes an entry; with `check: true` it stores nothing and reports what a write would. */
  write(kind: EntryKind, summary: string, options: WriteOptions & { check: true }): Promise<CheckReply>;
  write(kind: EntryKind, summary: string, options?: WriteOptions & { check?: false }): Promise<WriteReply>;
  write(kind: EntryKind, summary: string, options?: WriteOptions): Promise<WriteReply | CheckReply>;
  write(kind: EntryKind, summary: string, options: WriteOptions = {}): Promise<WriteReply | CheckReply> {
    return this.typed("hive_write", { kind, summary, ...options });
  }

  /** One verdict for an entry, or for up to 16 entries when given a list of ids. */
  feedback(entryId: string, verdict: Verdict, options?: FeedbackOptions): Promise<FeedbackReply>;
  feedback(entryIds: string[], verdict: Verdict, options?: FeedbackOptions): Promise<BatchFeedbackReply>;
  feedback(
    entryId: string | string[],
    verdict: Verdict,
    options: FeedbackOptions = {},
  ): Promise<FeedbackReply | BatchFeedbackReply> {
    const target = typeof entryId === "string" ? { entry_id: entryId } : { entry_ids: entryId };
    return this.typed("hive_feedback", { ...target, verdict, ...options });
  }

  /** Retracts an entry without replacing it; the reply is the withdrawn entry. */
  withdraw(entryId: string, reason?: string): Promise<Entry> {
    return this.typed("hive_withdraw", { entry_id: entryId, reason });
  }

  pin(entryId: string, options: PinOptions = {}): Promise<PinReply> {
    return this.typed("hive_pin", { entry_id: entryId, ...options });
  }

  register(name: string, options: RegisterOptions = {}): Promise<RegisterReply> {
    return this.typed("hive_register", { name, ...options });
  }

  /** The system-prompt text for an agent: the server's instructions, then the hivemind skill. */
  instructions(): string {
    return INSTRUCTIONS;
  }

  /** The end-of-task text. `Conversation.reminderDue()` says when to send it. */
  reminder(): string {
    return REMINDER;
  }

  /** The line to add after the app compacts or summarizes a conversation's context. */
  afterCompaction(): string {
    return COMPACTED;
  }

  /** Starts counting a new conversation's tool calls, for the end-of-task reminder. */
  conversation(options: ConversationOptions = {}): Conversation {
    return new Conversation(this, options);
  }

  anthropicTools(): AnthropicTool[] {
    return toAnthropic(this.tools);
  }

  openaiTools(): OpenAITool[] {
    return toOpenAI(this.tools);
  }
}
