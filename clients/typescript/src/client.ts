import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { AuthenticationError, HivemindError, errorFromEnvelope } from "./errors.js";
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

function isUnauthorized(error: unknown): boolean {
  return typeof error === "object" && error !== null && (error as { code?: unknown }).code === 401;
}

export class Hivemind implements AsyncDisposable {
  private constructor(
    private readonly client: Client,
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
    const transport = new StreamableHTTPClientTransport(new URL(url), {
      requestInit: { headers: { Authorization: `Bearer ${apiKey}` } },
    });
    try {
      await client.connect(transport);
      const listed = await client.listTools();
      const tools = listed.tools.map((t) => ({
        name: t.name,
        description: t.description ?? "",
        inputSchema: t.inputSchema as Record<string, unknown>,
      }));
      const hive = new Hivemind(client, tools, client.getInstructions() ?? "", {});
      await hive.refreshWhoami();
      return hive;
    } catch (error) {
      await client.close().catch(() => undefined);
      if (isUnauthorized(error)) {
        throw new AuthenticationError("unauthenticated", "the server rejected the API key (HTTP 401)");
      }
      throw error;
    }
  }

  async close(): Promise<void> {
    await this.client.close();
  }

  async [Symbol.asyncDispose](): Promise<void> {
    await this.close();
  }

  private async callText(name: string, args: Record<string, unknown>): Promise<string> {
    const result = await this.client.callTool({ name, arguments: args });
    const first = (result.content as { type: string; text?: string }[] | undefined)?.[0];
    const text = first?.type === "text" ? (first.text ?? "") : "";
    if (result.isError) {
      try {
        JSON.parse(text);
        return text;
      } catch {
        return errorText("tool_error", text || `${name} failed`);
      }
    }
    return text;
  }

  async call(name: string, args: Record<string, unknown> = {}): Promise<Reply> {
    const text = await this.callText(name, args);
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      throw new HivemindError("invalid_reply", `${name} answered with text that is not JSON`);
    }
    const envelope = (parsed as { error?: { code?: string; message?: string } } | null)?.error;
    if (envelope && typeof envelope === "object") {
      throw errorFromEnvelope(envelope.code ?? "tool_error", envelope.message ?? "");
    }
    return parsed as Reply;
  }

  /** For model loops: the reply JSON text, and an error envelope as text rather than a throw. */
  async runTool(name: string, args: object | string = {}): Promise<string> {
    if (!this.tools.some((t) => t.name === name)) {
      return errorText("invalid_input", `unknown tool ${name}`);
    }
    let parsed: unknown = args;
    if (typeof args === "string") {
      try {
        parsed = args.trim() === "" ? {} : JSON.parse(args);
      } catch {
        return errorText("invalid_input", `arguments for ${name} are not valid JSON`);
      }
    }
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return errorText("invalid_input", `arguments for ${name} must be a JSON object`);
    }
    return this.callText(name, parsed as Record<string, unknown>);
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
