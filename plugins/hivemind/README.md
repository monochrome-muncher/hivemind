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
| `bin/hivemind-incognito` | Starts any supported harness in an **incognito session**: Hivemind completely off for that one session |

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

## Incognito sessions

An incognito session has Hivemind **completely off**: the agent neither
reads from nor writes to it, and the Hivemind server never learns the
session happened (ADRs 0003, 0035). Start one with the launcher:

```sh
plugins/hivemind/bin/hivemind-incognito claude     # or: codex, dsh, hermes, pi
```

Put it on your `PATH` (e.g. `ln -s "$PWD/plugins/hivemind/bin/hivemind-incognito" ~/.local/bin/`)
or add aliases such as `alias claude-incognito='hivemind-incognito claude'`.

It does two things:

1. Sets `HIVEMIND_INCOGNITO=1`, so the plugin's reminders switch to their
   incognito text: don't use or mention Hivemind, and mark any local
   notes `[hivemind: incognito, never upload]` so no later session
   uploads them.
2. Adds the harness's own switch, so the Hivemind tools do not load at all:

| Harness | Switch | You need |
|---|---|---|
| Claude Code | `--settings '{"deniedMcpServers":[…]}'` for this session | nothing |
| Codex | `-c mcp_servers.hivemind.enabled=false` | the `[mcp_servers.hivemind]` entry in `~/.codex/config.toml` |
| DeepSeek Harness | the bundle's server row switches itself off | nothing |
| Hermes | `HIVEMIND_ENABLED=false` | `enabled: ${HIVEMIND_ENABLED}` in the hivemind server entry in `~/.hermes/config.yaml`, plus `HIVEMIND_ENABLED=true` in `~/.hermes/.env` for normal sessions |
| Pi | an exclusive MCP config: your global and project servers, minus hivemind | `python3` |

Without the launcher, set `HIVEMIND_INCOGNITO=1` and apply the switch from
the table yourself. Setting only the variable also works, but then the
tools are still loaded and the session is incognito only because the
agent obeys; the agent says so. Turning incognito on *during* a session
works the same way (the tools stay loaded), and a session started
incognito cannot turn Hivemind back on: start a new one.

## Updating

Hivemind's agent guidance lives in three places, and each updates
differently:

| What | Where it comes from | How it updates |
|---|---|---|
| Tool descriptions, the MCP server instructions, `hive_whoami` | the Hivemind server | Automatically, when the server is upgraded. Nothing to do on the agent side. |
| The two skills, the `SessionStart` hook, the Hermes/Pi system-prompt section | this plugin | Update the plugin (commands below), then start a new session. |
| Copies you or the agent made: skills copied into a skills folder, the `<!-- hivemind:begin -->` instruction block, a hand-installed hook | your own files | Not updated by anything. Re-copy them, or ask the agent to run **hivemind-setup**'s "Update" step, which compares and refreshes them. |

**Always start a new session after updating.** A running conversation keeps
the skill text it already loaded.

| Harness | Update the plugin |
|---|---|
| Claude Code | `claude plugin marketplace update hivemind`, then `claude plugin update hivemind@hivemind`, then restart. (Updating the marketplace alone only refreshes the catalog.) |
| Codex | `codex plugin marketplace upgrade hivemind`, then reinstall with `codex plugin add hivemind@hivemind` (Codex runs a cached copy), check with `codex plugin list --marketplace hivemind`, then start a new session. |
| DeepSeek Harness | Installed from a checkout: `git pull` in the checkout (the profile links it). Installed from npm: `dsh plugin --profile <name> update hivemind-dsh-plugin`. Then restart `dsh`. |
| Hermes | `hermes plugins update hivemind` (a `git pull` of the installed plugin). A plugin installed at a pinned ref needs `hermes plugins install <source> --force --ref <new-ref>` instead. |
| Pi | Installed from a local path: `git pull` in that checkout (Pi loads it in place). Installed from git or npm: `pi update --extensions`; a pinned tag stays put, so reinstall at the new tag. |
| Skills only | Copy both `skills/` folders over your earlier copies. |

### What changed for agents, by release

Check this before deciding whether an update matters. "Server" rows take
effect when the Hivemind server is upgraded; "plugin" rows need the plugin
update above.

| Release | Server | Plugin | Your copies (instruction block, hand-installed hook) |
|---|---|---|---|
| 1.1.1 | No agent-facing change. Server fix: the store and authenticator no longer leak Postgres connections (a race when the first requests arrive concurrently opened extra pools; each migration run left a connection open). | Unchanged (version bump only). | Unchanged. |
| 1.1.0 | Search hits (MCP `hive_search`, REST `POST /v1/search`) carry `scope` and `fleet_id`, so a foreign entry is recognisable without opening it (ADR 0036). | `hivemind` skill: foreign entries — use, don't relay; link instead of copy; ask before bringing them home (§3a); incognito sessions (§7) and the `[hivemind: incognito, never upload]` marker that later sessions never upload. The reminders (hook, Hermes, Pi) have an incognito variant; the DeepSeek Harness row switches off in incognito sessions; new `bin/hivemind-incognito` launcher; hivemind-setup gains step 6, Incognito. | **Changed:** the instruction block gains an incognito clause. Ask the agent to run hivemind-setup's "Update" step, or re-copy the block from step 4. A hand-installed hook should copy the new `hivemind-session-start.sh`. |
| 1.0.0 | `hive_write`: `supersedes` targets must be **active** — superseding an already-superseded or withdrawn entry is now `supersede_denied` (ADR 0034; re-target the current head). Typed errors everywhere: out-of-range `trust_level` → 422, unknown `home_fleet_id` → 404, a scope typo → 422/`invalid_input`, negative MCP `limit`/`offset` → `invalid_input`, blank `sources[].ref` refused on both surfaces. | `hivemind` skill: only the current head of a chain is supersedable; on a `supersede_denied` for a non-head target, fetch `?history` and re-target the current version. | Unchanged. |
| 1.0.0-rc.6 | `hive_write` description: keep machine-local paths out of fleet entries (make them repo-relative, or put the local detail in a `self` entry). | `hivemind` skill: the local-paths rule — what counts as local, rewrite before dropping, still write the finding to the fleet, local specifics in a separate `self` note. | Unchanged. |
| 1.0.0-rc.5 | Tool descriptions: `hive_write` has no `author` parameter and states the supersession rule; `hive_get`, `hive_feedback` and `hive_withdraw` say invisible entries answer `not_found` (ADR 0033). | `hivemind` skill: who may supersede what, lurkers flag fleet entries with `hive_feedback` instead, `not_found` may mean "not visible to you". | Unchanged. |

## Remove

Uninstall the plugin, and delete any `<!-- hivemind:begin -->` …
`<!-- hivemind:end -->` block the agent added to `CLAUDE.md` or
`AGENTS.md` (including `~/.dsh/AGENTS.md`, `~/.hermes/SOUL.md` and
`~/.pi/agent/AGENTS.md`). For DeepSeek Harness:
`dsh plugin --profile <name> remove hivemind-dsh-plugin`. For Hermes:
`hermes plugins remove hivemind`. For Pi: `pi remove <source>` (the
source you installed it from).
