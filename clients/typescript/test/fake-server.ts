import { createServer, type IncomingMessage, type Server as HttpServer } from "node:http";
import type { AddressInfo } from "node:net";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";

export const GOOD_KEY = "hm_good";
export const LOCKED_KEY = "hm_locked";
export const LOCKED_DETAIL = "this key is locked; ask an admin for a new one";
export const INSTRUCTIONS = "Fake server instructions.";

export interface Call {
  name: string;
  args: Record<string, unknown>;
}

const objectSchema = (properties: Record<string, unknown>, required: string[] = []) => ({
  type: "object",
  properties,
  required,
});

const TOOLS = [
  { name: "hive_whoami", description: "who", inputSchema: objectSchema({}) },
  { name: "hive_search", description: "search", inputSchema: objectSchema({ query: { type: "string" } }, ["query"]) },
  { name: "hive_get", description: "get", inputSchema: objectSchema({ entry_id: { type: "string" } }) },
  { name: "hive_write", description: "write", inputSchema: objectSchema({ kind: { type: "string" } }) },
  { name: "hive_feedback", description: "feedback", inputSchema: objectSchema({ verdict: { type: "string" } }) },
];

function reply(body: unknown, isError = false) {
  return { content: [{ type: "text", text: JSON.stringify(body) }], isError };
}

export interface FakeHive {
  url: string;
  calls: Call[];
  close(): Promise<void>;
}

function bearer(req: IncomingMessage): string | undefined {
  return req.headers.authorization?.replace(/^Bearer /, "");
}

function build(calls: Call[]): Server {
  const server = new Server(
    { name: "fake-hivemind", version: "0.0.0" },
    { capabilities: { tools: {} }, instructions: INSTRUCTIONS },
  );
  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));
  server.setRequestHandler(CallToolRequestSchema, async ({ params }) => {
    const args = (params.arguments ?? {}) as Record<string, unknown>;
    calls.push({ name: params.name, args });
    switch (params.name) {
      case "hive_whoami":
        return reply({ key_kind: "agent", name: "fake-agent" });
      case "hive_search":
        return reply({ count: 1, hits: [{ id: "e1", summary: String(args.query) }] });
      case "hive_get":
        if (args.entry_id === "crash") {
          return { content: [{ type: "text", text: "Error executing tool hive_get" }], isError: true };
        }
        if (args.entry_id === "split") {
          return { content: [{ type: "text", text: '{"id":' }, { type: "text", text: '"split"}' }] };
        }
        if (args.entry_id === "list") {
          return reply([1, 2]);
        }
        return reply({ error: { code: "not_found", message: "no such entry" } });
      case "hive_write":
        return reply({ id: "e2", kind: args.kind });
      default:
        return reply({ ok: true });
    }
  });
  return server;
}

export async function startFakeHive(): Promise<FakeHive> {
  const calls: Call[] = [];
  const http: HttpServer = createServer((req, res) => {
    const key = bearer(req);
    if (key !== GOOD_KEY) {
      const error = key === LOCKED_KEY ? "key_locked" : "unauthorized";
      const detail = key === LOCKED_KEY ? LOCKED_DETAIL : "a key is required";
      res.writeHead(401, { "content-type": "application/json" }).end(JSON.stringify({ error, detail }));
      return;
    }
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", async () => {
      const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : undefined;
      const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined, enableJsonResponse: true });
      const server = build(calls);
      res.on("close", () => void server.close());
      await server.connect(transport);
      await transport.handleRequest(req, res, body);
    });
  });
  await new Promise<void>((resolve) => http.listen(0, "127.0.0.1", resolve));
  const { port } = http.address() as AddressInfo;
  return {
    url: `http://127.0.0.1:${port}/mcp`,
    calls,
    close: () => new Promise<void>((resolve) => http.close(() => resolve())),
  };
}
