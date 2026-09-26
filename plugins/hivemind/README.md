# Hivemind agent plugin

Makes an agent harness use your organization's Hivemind as its long-term
memory: it checks its standing at the start of every session, recalls
before it works, contributes what it learns to its fleet, and tells its
user when it lacks the rights to do so.

Works as a **Claude Code** plugin, a **Codex** plugin, a **DeepSeek
Harness** bundle, a **Hermes** plugin and a **Pi** package. The two skills
also work on their own in any harness that reads `SKILL.md` skills.

| Part | What it does |
|---|---|
| `skills/hivemind/` | The always-on rules: `hive_whoami` first, when to recall, when and how to write, what never to write, local memory as a fallback only |
| `skills/hivemind-setup/` | Connecting, registering, switching to the agent key after activation, and making the agent permanently Hivemind-aware |
| `hooks/hooks.json` | A `SessionStart` hook (`startup`, `resume`, `clear`, `compact`) that re-injects a short Hivemind reminder whenever the context is rebuilt (Claude Code, Codex) |
| `.mcp.json` | The MCP server for Claude Code, built from `HIVEMIND_MCP_URL` and `HIVEMIND_API_KEY` |
| `package.json`, `cordis.patch.yml`, `dsh/` | The DeepSeek Harness bundle: the same MCP server, plus a small provider that serves `skills/` |
| `plugin.yaml`, `__init__.py` | The Hermes native plugin: serves `skills/` and adds a Hivemind section to the system prompt (the stay-aware layer; compaction never removes it) |
| `extensions/hivemind.ts` | The Pi package extension: the same system-prompt section, added on `before_agent_start` |

## What you need

- The URL of your Hivemind MCP endpoint (the `hivemind-mcp-http`
  deployment), ending in `/mcp`.
- A key: the **org key** to register a new agent, later replaced by that
  agent's own **agent key**. One key at a time, never both.

## Install

The repository root is a marketplace for Claude Code and Codex. Use your
Git server's URL for this repository (a GitLab mirror works).

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

**DeepSeek Harness** (`dsh`, which needs `pnpm` on the `PATH`)

```sh
dsh plugin --profile <name> add /path/to/this/repo/plugins/hivemind
```

This links the checkout into the profile. (To distribute it through your
npm mirror instead, remove `"private": true` from `package.json`, publish
it, and `dsh plugin --profile <name> add hivemind-dsh-plugin`.) Check the
layer with `dsh --profile <name> --dump-config`, then run
`dsh --profile <name>`. DSH needs no startup hook: it keeps the Hivemind
server's instructions in the system prompt, which compaction never
removes.

**Hermes** (install the plugin from this checkout; use your Git server's
URL for `<owner>/hivemind`)

```sh
hermes plugins install <owner>/hivemind/plugins/hivemind
hermes plugins enable hivemind
```

From a local checkout: `hermes plugins install /path/to/this/repo/plugins/hivemind`.
The plugin serves both skills and adds a Hivemind section to the system
prompt, which compaction never removes — no startup hook needed. The MCP
server is configured separately (below).

**Pi**

```sh
pi install /path/to/this/repo/plugins/hivemind
```

The package provides the two skills and a system-prompt section that keeps
the agent Hivemind-aware across sessions and compaction. Pi has no built-in
MCP client: the `hive_*` tools come from the standard MCP config files that
the [pi-mcp-adapter](https://www.npmjs.com/package/pi-mcp-adapter)
extension reads (`pi install npm:pi-mcp-adapter`), configured below.

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

- **DeepSeek Harness**: the shell that launches `dsh`. The bundle reads
  both; the MCP server stays off while `HIVEMIND_MCP_URL` is unset.
- **Hermes**: the shell that launches `hermes`, or `~/.hermes/.env`
  (read into the environment). The MCP server goes into
  `~/.hermes/config.yaml` with `${VAR}` references, so the key never
  lands in the file:

  ```yaml
  mcp_servers:
    hivemind:
      url: "${HIVEMIND_MCP_URL}"
      headers:
        Authorization: "Bearer ${HIVEMIND_API_KEY}"
  ```

- **Pi**: the shell that launches `pi`. The MCP server goes into
  `~/.config/mcp/mcp.json` (all projects) or a project's `.mcp.json`,
  read by pi-mcp-adapter, with the same `${VAR}` references:

  ```json
  {
    "mcpServers": {
      "hivemind": {
        "type": "http",
        "url": "${HIVEMIND_MCP_URL}",
        "headers": { "Authorization": "Bearer ${HIVEMIND_API_KEY}" }
      }
    }
  }
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
(for example `~/.claude/skills/`, `~/.agents/skills/` for Codex and
DeepSeek Harness, `~/.hermes/skills/` for Hermes, or `~/.pi/agent/skills/`
for Pi), set
up the MCP connection as above, then ask the agent to run
**hivemind-setup**'s "Stay aware" step. It adds the startup hook (where
the harness has one) and a marked instruction block to your global
instructions file, showing you each change first.

## Remove

Uninstall the plugin, and delete any `<!-- hivemind:begin -->` …
`<!-- hivemind:end -->` block the agent added to `CLAUDE.md` or
`AGENTS.md` (including `~/.dsh/AGENTS.md`, `~/.hermes/SOUL.md` and
`~/.pi/agent/AGENTS.md`). For DeepSeek Harness:
`dsh plugin --profile <name> remove hivemind-dsh-plugin`. For Hermes:
`hermes plugins remove hivemind`. For Pi: `pi remove <source>` (the
source you installed it from).
