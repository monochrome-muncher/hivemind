# Hivemind agent plugin

Makes an agent harness use your organization's Hivemind as its long-term
memory: it checks its standing at the start of every session, recalls
before it works, contributes what it learns to its fleet, and tells its
user when it lacks the rights to do so.

Works as a **Claude Code** plugin and a **Codex** plugin. The two skills
also work on their own in any harness that reads `SKILL.md` skills.

| Part | What it does |
|---|---|
| `skills/hivemind/` | The always-on rules: `hive_whoami` first, when to recall, when and how to write, what never to write, local memory as a fallback only |
| `skills/hivemind-setup/` | Connecting, registering, switching to the agent key after activation, and making the agent permanently Hivemind-aware |
| `hooks/hooks.json` | A `SessionStart` hook (`startup`, `resume`, `clear`, `compact`) that re-injects a short Hivemind reminder whenever the context is rebuilt |
| `.mcp.json` | The MCP server for Claude Code, built from `HIVEMIND_MCP_URL` and `HIVEMIND_API_KEY` |

## What you need

- The URL of your Hivemind MCP endpoint (the `hivemind-mcp-http`
  deployment), ending in `/mcp`.
- A key: the **org key** to register a new agent, later replaced by that
  agent's own **agent key**. One key at a time, never both.

## Install

The repository root is a marketplace for both harnesses. Use your Git
server's URL for this repository (a GitLab mirror works).

**Claude Code**

```text
/plugin marketplace add <git-url-of-this-repo>
/plugin install hivemind@hivemind
```

**Codex**

```sh
codex plugin marketplace add <git-url-of-this-repo>
```

then install `hivemind` from the plugin browser (`/plugins`). Codex asks
you to review and trust the plugin's `SessionStart` hook (`/hooks`).

## Configure

Set two variables, then restart the harness.

```sh
export HIVEMIND_MCP_URL="https://hivemind.example.org/mcp"
export HIVEMIND_API_KEY="hm_…"   # org key first, agent key after activation
```

- **Claude Code**: the shell profile that launches it, or the `"env"`
  block of `~/.claude/settings.json`. The plugin's `.mcp.json` reads both.
- **Codex**: the plugin does not define the MCP server, because Codex's
  plugin MCP config cannot read the URL and key from the environment. Add
  it to `~/.codex/config.toml` and export `HIVEMIND_API_KEY`:

  ```toml
  [mcp_servers.hivemind]
  url = "https://hivemind.example.org/mcp"
  bearer_token_env_var = "HIVEMIND_API_KEY"
  ```

Or ask the agent to run the **hivemind-setup** skill, which walks through
the same steps and asks before changing any file.

## From org key to agent key

1. With the org key set, the agent registers itself (`hive_register`),
   after asking you for its name and your alias. Names are permanent.
2. A Hivemind admin activates it in the admin panel's pending queue,
   choosing its trust level and home fleet. The panel shows the agent key
   once; the admin sends it to you.
3. Replace `HIVEMIND_API_KEY` with the agent key wherever you set it, and
   restart. `hive_whoami` now shows the agent as `active`.

A **lurker** reads its fleet's knowledge but writes only to its own
`self` scope; a **contributor** also writes to the fleet. The agent tells
you when it needs a promotion.

## Without the plugin

Copy both folders under `skills/` into your harness's skills directory
(for example `~/.claude/skills/`, or `~/.agents/skills/` for Codex), set
up the MCP connection as above, then ask the agent to run
**hivemind-setup**'s "Stay aware" step. It adds the startup hook and a
marked instruction block to your global instructions file, showing you
each change first.

## Remove

Uninstall the plugin, and delete any `<!-- hivemind:begin -->` …
`<!-- hivemind:end -->` block the agent added to `CLAUDE.md` or
`AGENTS.md`.
