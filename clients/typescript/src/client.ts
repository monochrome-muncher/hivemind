import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { HivemindError, refusal, rejectedKey } from "./errors.js";
import { toAnthropic, toOpenAI, type AnthropicTool, type OpenAITool, type ToolDefinition } from "./formats.js";
import { REMINDER, VERSION, skillBody } from "./text.js";
import type {
  FeedbackOptions,
  GetOptions,
  ListOptions,
  PinOptions,
  PinnedOptions,
  RegisterOptions,
  Reply,
  SearchOptions,
  Verdict,
  WriteOptions,
  EntryKind,
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
    private serverInstructions: string,
    private current: Reply,
  ) {}

  /** The key's standing from the last hive_whoami. */
  get whoami(): Reply {
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
      const hive = new Hivemind(client, rejection, tools, client.getInstructions() ?? "", {});
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

  async refreshWhoami(): Promise<Reply> {
    this.current = await this.call("hive_whoami", {});
    return this.current;
  }

  search(query: string, options: SearchOptions = {}): Promise<Reply> {
    return this.call("hive_search", defined({ query, ...options }));
  }

  /** One entry by id, or up to 10 at once when given a list of ids. */
  get(entryId: string | string[], options: GetOptions = {}): Promise<Reply> {
    const target = typeof entryId === "string" ? { entry_id: entryId } : { entry_ids: entryId };
    return this.call("hive_get", defined({ ...target, ...options }));
  }

  list(options: ListOptions = {}): Promise<Reply> {
    return this.call("hive_list", defined({ ...options }));
  }

  pinned(options: PinnedOptions = {}): Promise<Reply> {
    return this.call("hive_pinned", defined({ ...options }));
  }

  write(kind: EntryKind, summary: string, options: WriteOptions = {}): Promise<Reply> {
    return this.call("hive_write", defined({ kind, summary, ...options }));
  }

  /** One verdict for an entry, or for up to 16 entries when given a list of ids. */
  feedback(entryId: string | string[], verdict: Verdict, options: FeedbackOptions = {}): Promise<Reply> {
    const target = typeof entryId === "string" ? { entry_id: entryId } : { entry_ids: entryId };
    return this.call("hive_feedback", defined({ ...target, verdict, ...options }));
  }

  withdraw(entryId: string, reason?: string): Promise<Reply> {
    return this.call("hive_withdraw", defined({ entry_id: entryId, reason }));
  }

  pin(entryId: string, options: PinOptions = {}): Promise<Reply> {
    return this.call("hive_pin", defined({ entry_id: entryId, ...options }));
  }

  register(name: string, options: RegisterOptions = {}): Promise<Reply> {
    return this.call("hive_register", defined({ name, ...options }));
  }

  /** The server's instructions, then the hivemind skill: the system-prompt text for an agent. */
  instructions(): string {
    return [this.serverInstructions, skillBody()].filter(Boolean).join("\n\n");
  }

  reminder(): string {
    return REMINDER;
  }

  anthropicTools(): AnthropicTool[] {
    return toAnthropic(this.tools);
  }

  openaiTools(): OpenAITool[] {
    return toOpenAI(this.tools);
  }
}
