# Incognito sessions: one signal, the harness's own off switch, and marked local notes

Builds on ADR 0003 (the switch is client-side) and renames its "kill switch"
to **incognito session**.

## Context

ADR 0003 decided that turning Hivemind off for a session is a client-side
act the server never sees, but nothing implemented it. Once the agent
plugin existed, a session with the Hivemind tools switched off was worse
than useless:

* every stay-aware layer (the `SessionStart` hook, the Hermes and Pi
  system-prompt sections, the instruction block, the skill) read missing
  tools as "not connected" and told the agent to nag the user and offer
  setup;
* the skill's "fall back to local memory, move it into Hivemind later"
  rule would upload what the user had chosen not to record, in the next
  normal session.

Each harness has its own way to keep one MCP server from loading for a
single session, and some only have in-session toggles that persist.

## Decision

1. **"Incognito" means completely off**: no reads and no writes. A
   "read but don't record" mode was considered and not taken.

2. **One signal: `HIVEMIND_INCOGNITO`** (`1`/`true`/`yes`/`on`) in the
   launch environment. The hook, the Hermes and Pi sections replace their
   reminder with one fixed incognito sentence (identical across the
   three, pinned by a test); the DeepSeek Harness bundle's server row is
   disabled by it.

3. **The harness's own switch keeps the tools from loading**, applied by a
   launcher shipped in the plugin, `bin/hivemind-incognito <harness>`:
   Claude Code gets `--settings` with `deniedMcpServers`; Codex gets
   `-c mcp_servers.hivemind.enabled=false` (only when that server is
   configured, since the override otherwise creates an invalid entry);
   Hermes gets `HIVEMIND_ENABLED=false` for a config that gates the server
   on it; Pi gets an exclusive MCP config with the server removed. The
   variable alone still works, but then the session is incognito only by
   the agent's restraint, and the agent tells the user so.

4. **Local notes stay allowed and are marked.** Each starts with
   `[hivemind: incognito, never upload]`, and no later session moves a
   marked note into Hivemind. Ordinary fallback notes still migrate.

5. **Chosen when the session starts.** A mid-session "go incognito" is
   honoured by the agent but the tools stay loaded; a session started
   incognito cannot switch Hivemind back on. Both are stated plainly.

6. **The server stays unaware** (ADR 0003): no session registry, no
   audit of incognito sessions.

## Consequences

* The instruction block changed; users with a hand-added block refresh it
  (hivemind-setup's Update step).
* Hermes needs a one-time config change (`enabled: ${HIVEMIND_ENABLED}`
  plus `HIVEMIND_ENABLED=true` for normal sessions) before the launcher can
  switch its server off.
* An incognito Pi session loads only the global and current-project MCP
  configs the launcher merges; other config sources are skipped.
