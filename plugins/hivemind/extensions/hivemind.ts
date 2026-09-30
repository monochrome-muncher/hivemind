/**
 * Hivemind — Pi extension.
 *
 * Adds a short Hivemind section to the system prompt so the agent keeps
 * treating the organization's Hivemind as its long-term memory across
 * sessions and compaction: the section lives in the system prompt, which
 * compaction never rewrites — the same shape as the DeepSeek Harness MCP
 * instructions and the Claude Code/Codex SessionStart hook.
 *
 * Deliberately static and offline: no network call, so it cannot fail or
 * slow a session down. It only checks whether HIVEMIND_API_KEY is set
 * (the URL may live in the MCP config, as it does for Codex) and never
 * prints the key.
 *
 * The Hivemind MCP server itself is configured in a standard MCP config
 * file that pi-mcp-adapter reads (for example ~/.config/mcp/mcp.json; see
 * the hivemind-setup skill); the key never enters this package.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// The common core is shared, byte for byte, with the Claude Code/Codex
// SessionStart hook (skills/hivemind-setup/scripts/hivemind-session-start.sh)
// and the Hermes plugin (__init__.py). Keep the three in step —
// tests/unit/test_agent_plugin.py checks it.
const CORE =
	"HIVEMIND: your organization's Hivemind is your long-term memory. " +
	"Follow the hivemind skill (load it now if it is not in context). " +
	"Recall with hive_search before non-trivial work; contribute what you " +
	"learn with hive_write, as often as you have something worth " +
	"reusing; prefer Hivemind over local memory files.";

const CONFIGURED =
	"Hivemind is configured. Your first action this session: call " +
	"hive_whoami and act on the result as the hivemind skill describes.";

const NOT_CONFIGURED =
	"HIVEMIND_API_KEY is not set in this environment, so Hivemind is " +
	"probably not connected. If the hive_* tools are missing or fail, " +
	"tell the user once and offer to run the hivemind-setup skill.";

// An incognito session (ADR 0035): the whole section is replaced by this
// sentence, shared byte for byte with the hook and the Hermes plugin.
const INCOGNITO =
	"HIVEMIND: this is an incognito session, so Hivemind is completely off. Do not call any hive_* tool, and do not mention Hivemind or offer to set it up. You may keep local notes; start each one with [hivemind: incognito, never upload] so that no later session uploads it. If hive_* tools are loaded anyway, still do not use them, and tell the user once that the tools are loaded, so this session is incognito only by your own restraint.";

function incognito(): boolean {
	const value = (process.env.HIVEMIND_INCOGNITO ?? "").trim().toLowerCase();
	return ["1", "true", "yes", "on"].includes(value);
}

// The Hivemind MCP tools, whatever prefix the MCP bridge gives them
// (pi-mcp-adapter in Pi, the built-in client in Oh My Pi).
const HIVE_TOOL = /(^|[^a-z])hive_(whoami|search|get|list|write|feedback|withdraw|register)$/;

// Is this tool call a Hivemind call? Pi exposes MCP tools under their own
// (prefixed) names; Oh My Pi mounts them as routes that the model calls by
// writing JSON to a path such as xd://mcp__hivemind_hive_whoami (possibly
// with a trailing slash or a ?query / #fragment), so there the call arrives
// as a `write` whose path names the Hivemind tool. In pi-mcp-adapter's
// default mode the tools sit behind ONE `mcp` proxy tool whose arguments
// name the server and tool ({server: "hivemind"}, {tool:
// "hivemind_hive_search"}), so for that tool the ROUTING fields are
// inspected. Never `args`: a search for "hivemind" on another server, or a
// file that mentions it, is not a Hivemind call.
const ROUTING_FIELDS = ["server", "connect", "instructions", "tool", "describe"];

function isHivemindRoute(value: unknown): boolean {
	if (typeof value !== "string") return false;
	const v = value.trim();
	return /^hivemind$/i.test(v) || /^hivemind[_-]/i.test(v) || HIVE_TOOL.test(v);
}

// pi-mcp-adapter's other surfaces: the per-server `mcp__<server>` wrapper
// namespace, and `mcpScript`, whose `code` calls tools as `tools.<name>(…)`.
// The script scan is best effort (code can build a name at run time); the
// launcher's exclusive config, which drops the server, is the real control.
const SCRIPT_CALL =
	/tools\s*(\.|\[\s*["'`])hivemind|\bhive_(whoami|search|get|list|write|feedback|withdraw|register)\b/i;

function isProxyCallToHivemind(name: string, input: any): boolean {
	if (name === "mcp") return ROUTING_FIELDS.some((f) => isHivemindRoute(input?.[f]));
	if (/^mcp__hivemind(_|$)/i.test(name)) return true;
	if (name === "mcpScript") return SCRIPT_CALL.test(String(input?.code ?? ""));
	return false;
}

function isHivemindCall(event: any): boolean {
	const name = String(event.toolName ?? "");
	if (HIVE_TOOL.test(name)) return true;
	if (isProxyCallToHivemind(name, event.input)) return true;
	const path = String(event.input?.path ?? "").trim();
	if (!/^xd:\/\//i.test(path)) return false;
	const route = path.slice("xd://".length).replace(/[?#].*$/, "").replace(/\/+$/, "");
	return HIVE_TOOL.test(route);
}

function section(): string {
	if (incognito()) return INCOGNITO;
	const state = process.env.HIVEMIND_API_KEY ? CONFIGURED : NOT_CONFIGURED;
	return `${CORE} ${state}`;
}

export default function hivemind(pi: ExtensionAPI) {
	// Pi hands the handler named prompt sections to fill in; Oh My Pi (a Pi
	// fork) hands it the prompt as a string array and takes the new array
	// back as the result. Support both shapes.
	pi.on("before_agent_start", (event: any) => {
		const text = section();
		if (event.systemPromptOptions?.sections) {
			event.systemPromptOptions.sections.hivemind = text;
			return;
		}
		if (Array.isArray(event.systemPrompt)) {
			return { systemPrompt: [...event.systemPrompt, text] };
		}
	});

	// Incognito sessions (ADR 0035) are enforced here, not just requested:
	// a hive_* call is blocked before it reaches the Hivemind server.
	pi.on("tool_call", (event: any) => {
		if (incognito() && isHivemindCall(event)) {
			return {
				block: true,
				reason:
					"This is an incognito session: Hivemind is off. Do not retry; " +
					"a new session without HIVEMIND_INCOGNITO turns it back on.",
			};
		}
	});
}
