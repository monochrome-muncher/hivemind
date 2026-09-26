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

export default function hivemind(pi: ExtensionAPI) {
	pi.on("before_agent_start", (event) => {
		const state = process.env.HIVEMIND_API_KEY ? CONFIGURED : NOT_CONFIGURED;
		event.systemPromptOptions.sections.hivemind = `${CORE} ${state}`;
	});
}
