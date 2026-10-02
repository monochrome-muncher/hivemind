# Hivemind agent plugin

This plugin makes an agent use your organization's Hivemind as its
long-term memory. The agent checks its standing at the start of every
session, searches Hivemind before it works, writes down what it learns,
and tells you when it needs more rights.

## Contents

- [Before you start](#before-you-start)
- [Install](#install):
  [Claude Code](#claude-code) ·
  [Codex](#codex) ·
  [DeepSeek Harness](#deepseek-harness) ·
  [Hermes](#hermes) ·
  [Pi](#pi) ·
  [Oh My Pi](#oh-my-pi) ·
  [OpenCode](#opencode) ·
  [Gemini CLI](#gemini-cli) ·
  [Any other harness](#any-other-harness)
- [Set up with your agent](#set-up-with-your-agent), the easy way
  - [From org key to agent key](#from-org-key-to-agent-key)
- [Configure by hand](#configure-by-hand)
  - [Keep the key private](#keep-the-key-private)
  - [Per harness](#per-harness)
- [Update](#update)
- [Remove](#remove)
- [Incognito sessions](#incognito-sessions)
- [What is in the plugin](#what-is-in-the-plugin)
- [CHANGELOG.md](CHANGELOG.md): what changed for agents in each release

## Before you start

You need two things from your Hivemind admin:

- **The MCP URL**, ending in `/mcp` (the `hivemind-mcp-http` deployment).
- **A key.** Start with the **org key**, which lets a new agent register.
  Once an admin activates the agent, you replace it with that agent's own
  **agent key**. Use one key at a time, never both.

Wherever a command below says `<git-url-of-this-repo>`, use your Git
server's URL for this repository (a GitLab mirror works).

## Install

Install the plugin in your harness, then go to
[Set up with your agent](#set-up-with-your-agent).

### Claude Code

```text
/plugin marketplace add <git-url-of-this-repo>
/plugin install hivemind@hivemind
```

### Codex

```sh
codex plugin marketplace add <git-url-of-this-repo>
```

Then install `hivemind` from the plugin browser (`/plugins`). Codex asks
you to review and trust the plugin's `SessionStart` hook (`/hooks`).

### DeepSeek Harness

DeepSeek Harness (`dsh`) needs `pnpm` on the `PATH`. Give it this
repository's Git URL from its Plugins page, ask the agent to install it,
or run:

```sh
dsh plugin --profile <name> add git+https://<your-git-server>/<owner>/hivemind.git
```

Add `#v<version>` to the URL to pin a release. From a local checkout,
use `dsh plugin --profile <name> add /path/to/this/repo/plugins/hivemind`
instead, which links the checkout into the profile. To install from your
npm mirror, remove `"private": true` from `package.json`, publish it, and
add `hivemind-agent-plugin`.

Check the result with `dsh --profile <name> --dump-config`.

### Hermes

```sh
hermes plugins install <owner>/hivemind/plugins/hivemind   # or /path/to/this/repo/plugins/hivemind
hermes plugins enable hivemind
```

Current Hermes releases do not show the plugin's system-prompt section
(upstream issue NousResearch/hermes-agent#117432). The marked Hivemind
block in `~/.hermes/SOUL.md` is therefore required, and the setup skill
adds it.

### Pi

```sh
pi install git:<your-git-server>/<owner>/hivemind      # or an https:// URL; @v<version> pins
pi install /path/to/this/repo/plugins/hivemind           # from a checkout
```

On **Pi 0.99 and later** the package also registers the Hivemind MCP
server with Pi's built-in MCP client. On older Pi, or with
[pi-mcp-adapter](https://www.npmjs.com/package/pi-mcp-adapter)
installed, the `hive_*` tools come from the adapter's MCP config instead
(see [Per harness](#per-harness)).

### Oh My Pi

Install through the marketplace. A plain `omp install <git-url>` would
miss the skills.

```sh
omp plugin marketplace add <git-url-of-this-repo>
omp plugin install hivemind@hivemind
```

Oh My Pi mounts MCP tools as routes (`xd://mcp__hivemind_hive_*`) rather
than as named tools. The agent finds them in its system prompt.

### OpenCode

Add this repository's Git URL to the `plugin` list in
`~/.config/opencode/opencode.json`, or run `opencode plugin -g <git-url>`
(without `-g` it installs into the current project only):

```json
{ "plugin": ["git+https://<your-git-server>/<owner>/hivemind.git#v<version>"] }
```

Pin a release tag as shown. OpenCode caches a Git plugin on first use and
never refreshes an unpinned entry, so changing the tag is how you update.

### Gemini CLI

There is no plugin yet. Copy both `skills/` folders into
`~/.gemini/skills/`, start Gemini CLI and ask it to set up Hivemind. The
steps are in `skills/hivemind-setup/references/gemini-cli.md`.

### Any other harness

The two skills work in any harness that reads `SKILL.md` files. Copy both
folders under `skills/` into its skills directory (for example
`~/.claude/skills/`, `~/.agents/skills/` for Codex and DeepSeek Harness,
`~/.hermes/skills/` or `~/.pi/agent/skills/`). Then ask the agent to set
up Hivemind.

## Set up with your agent

The easiest way to finish setup is to let the agent do it with you.
Start the harness and say **"Set up Hivemind"**, or run the
`/hivemind-setup` skill (Claude Code lists it as
`/hivemind:hivemind-setup`).

The skill is a conversation. The agent works out which harness it is
running in, checks where you stand, and then asks you what it needs
before each step:

1. **Connect.** It proposes the MCP configuration for your harness and a
   private key file, and shows you each change before making it. It asks
   you to type the key into that file yourself, so the key never appears
   in the chat.
2. **Register.** With the org key, it asks for its name and your alias,
   then registers itself. Names are permanent.
3. **Switch keys.** Once an admin activates the agent, it helps you swap
   in the agent key.
4. **Stay aware.** It adds the startup hook (where the harness has one)
   and a short marked instruction block to your global instructions file,
   so the agent remembers Hivemind after compaction and in new sessions.

After each change it tells you which file it touched and how to undo it.
Restart the harness when it says so, because MCP settings are read at
startup. Ask for the skill again later to update your copies or to check
on a key that stopped working.

### From org key to agent key

1. With the org key set, the agent registers itself (`hive_register`).
2. A Hivemind admin activates it in the admin panel's pending queue and
   picks its trust level and home fleet. The panel shows the agent key
   once, and the admin sends it to you.
3. Replace `HIVEMIND_API_KEY` with the agent key wherever you set it, and
   restart. `hive_whoami` now shows the agent as `active`.

A **lurker** reads its fleet's knowledge but writes only to its own
`self` scope. A **contributor** also writes to the fleet. The agent tells
you when it needs a promotion.

## Configure by hand

If you would rather not use the skill, set two variables and restart the
harness:

```sh
export HIVEMIND_MCP_URL="https://hivemind.example.org/mcp"
export HIVEMIND_API_KEY="hm_…"   # org key first, agent key after activation
```

### Keep the key private

- Put the two lines in a private file you create yourself, such as
  `~/.config/hivemind/<harness>.env` (`chmod 600`, directory `chmod 700`).
  Never put them in a project, a repository or a dotfiles repo.
- Don't paste the key into the agent's chat. Transcripts are stored in
  plain text.
- Use **one agent per harness**, each with its own name and key
  (`<user>-claude-code`, `<user>-codex`, …). A shared variable in the
  shell profile would give every harness the same identity.
- A harness started from a dock, start menu or IDE does not read the
  shell profile. Use its own settings or env file, or start it from a
  terminal.

### Per harness

- **Claude Code**: the `"env"` block of `~/.claude/settings.json`
  (`chmod 600`; the only route for the desktop app and IDE extensions),
  or a private key file loaded by the launching shell. The plugin's
  `.mcp.json` reads both. Claude Code on the web runs in a throwaway VM:
  see the Claude Code reference in hivemind-setup.
- **Codex**: the plugin cannot define the MCP server, because Codex's
  plugin MCP config cannot read the URL and key from the environment.
  Add it to `~/.codex/config.toml` and export `HIVEMIND_API_KEY`:

  ```toml
  [mcp_servers.hivemind]
  url = "https://hivemind.example.org/mcp"
  bearer_token_env_var = "HIVEMIND_API_KEY"
  ```

- **DeepSeek Harness**: put both lines in `~/.dsh/.env`
  (`chmod 600`) and restart. DSH loads it however it is launched, CLI and
  Desktop alike, and keeps the server off while `HIVEMIND_MCP_URL` is
  unset. Two things to know:
  - **Never put `HIVEMIND_*` in a project `.env`.** DSH also loads the
    `.env` of the directory it starts in, ranked above `~/.dsh/.env`, so
    a cloned repository could send your key elsewhere. When that file
    names any `HIVEMIND_*` variable, the bundle uses `~/.dsh/.env` alone
    and prints a warning.
  - **The agent's shell cannot see the key.** DSH strips every variable
    whose name contains `KEY`, `TOKEN`, `SECRET` or `PASSWORD` from the
    processes it starts. Check the connection with `hive_whoami`, not
    `printenv`.
- **Hermes**: the key goes in `~/.hermes/.env` (or
  `~/.hermes/profiles/<name>/.env`; `chmod 600`). The MCP server goes in
  `~/.hermes/config.yaml` with `${VAR}` references, so the key never
  lands in that file. Run `/reload-mcp` after editing either.

  ```yaml
  mcp_servers:
    hivemind:
      url: "${HIVEMIND_MCP_URL}"
      headers:
        Authorization: "Bearer ${HIVEMIND_API_KEY}"
  ```

- **Pi**: the shell that launches `pi`. Pi 0.99 and later need nothing
  else. With pi-mcp-adapter, the server goes in `~/.config/mcp/mcp.json`
  (all projects) or a project's `.mcp.json`; `lifecycle` and
  `directTools` load the tools at startup under their own names:

  ```json
  {
    "mcpServers": {
      "hivemind": {
        "type": "http",
        "url": "${HIVEMIND_MCP_URL}",
        "headers": { "Authorization": "Bearer ${HIVEMIND_API_KEY}" },
        "lifecycle": "eager",
        "directTools": true
      }
    }
  }
  ```

- **Oh My Pi**: the shell that launches `omp`. The server goes in
  `~/.omp/agent/mcp.json`, with the same JSON as Pi above.
- **OpenCode**: the shell that launches `opencode`. The plugin reads both
  variables and registers the server. Without the plugin, add
  `"mcp": {"hivemind": {"type": "remote", "url": "{env:HIVEMIND_MCP_URL}",
  "headers": {"Authorization": "Bearer {env:HIVEMIND_API_KEY}"},
  "oauth": false}}` to `opencode.json`.
- **Gemini CLI**: `mcpServers.hivemind` in `~/.gemini/settings.json` with
  `httpUrl` and an `Authorization` header, and the instruction block in
  `~/.gemini/GEMINI.md` (see
  `skills/hivemind-setup/references/gemini-cli.md`). Gemini CLI turns off
  every MCP server in a folder you have not trusted.

## Update

Agent guidance lives in three places, and each updates differently:

| What | Where it comes from | How it updates |
|---|---|---|
| Tool descriptions, the MCP server instructions, `hive_whoami` | the Hivemind server | By itself, when the server is upgraded |
| The two skills, the `SessionStart` hook, the Hermes/Pi system-prompt section | this plugin | Update the plugin (below), then start a new session |
| Copies you or the agent made: copied skills, the `<!-- hivemind:begin -->` instruction block, a hand-installed hook | your own files | Never by themselves. Re-copy them, or ask the agent to run hivemind-setup's "Update" step |

**Always start a new session after updating**, because a running
conversation keeps the skill text it already loaded.
[CHANGELOG.md](CHANGELOG.md) says what changed in each release, so you
can tell whether an update matters.

| Harness | Update the plugin |
|---|---|
| Claude Code | `claude plugin marketplace update hivemind`, then `claude plugin update hivemind@hivemind`, then restart. Updating the marketplace alone only refreshes the catalog. |
| Codex | `codex plugin marketplace upgrade hivemind`, then reinstall with `codex plugin add hivemind@hivemind` (Codex runs a cached copy). Check with `codex plugin list --marketplace hivemind`. |
| DeepSeek Harness | From the Git URL or npm: `dsh plugin --profile <name> update hivemind-agent-plugin`. A URL pinned with `#v<version>` stays put, so run `add` again with the new tag. From a checkout: `git pull` in it. Then restart `dsh`. Installed before 2.0.1 as `hivemind-dsh-plugin`: remove it under that name and add it again. |
| Hermes | `hermes plugins update hivemind`. Installed at a pinned ref: `hermes plugins install <source> --force --ref <new-ref>`. |
| Pi | From a local path: `git pull` in the checkout. From git or npm: `pi update --extensions`; a pinned tag stays put, so reinstall at the new tag. |
| Oh My Pi | `omp plugin marketplace update hivemind`, then `omp plugin upgrade` (or `omp plugin install hivemind@hivemind --force`). |
| OpenCode | Change the tag in the `plugin` entry to the new release and restart. An unpinned entry never updates: pin it, or delete the cached copy under `~/.cache/opencode/` and restart. |
| Gemini CLI | Copy both `skills/` folders over `~/.gemini/skills/` and refresh the block in `~/.gemini/GEMINI.md` (hivemind-setup's "Update" step does both). |
| Any other harness | Copy both `skills/` folders over your earlier copies. |

## Remove

Uninstall the plugin, then delete the `<!-- hivemind:begin -->` …
`<!-- hivemind:end -->` block the agent added to your instructions file
(`CLAUDE.md`, `AGENTS.md`, `~/.dsh/AGENTS.md`, `~/.hermes/SOUL.md`,
`~/.pi/agent/AGENTS.md`, `~/.omp/agent/AGENTS.md`,
`~/.config/opencode/AGENTS.md` or `~/.gemini/GEMINI.md`). You may also
want to delete your key file and any MCP entry you added by hand.

| Harness | Uninstall |
|---|---|
| Claude Code | `claude plugin uninstall hivemind@hivemind` |
| Codex | `codex plugin remove hivemind@hivemind`, and the `mcp_servers.hivemind` entry in `~/.codex/config.toml` |
| DeepSeek Harness | `dsh plugin --profile <name> remove hivemind-agent-plugin` (`hivemind-dsh-plugin` if installed before 2.0.1) |
| Hermes | `hermes plugins remove hivemind`, and the `hivemind` entry in `~/.hermes/config.yaml` |
| Pi | `pi remove <source>`, using the source you installed from |
| Oh My Pi | `omp plugin uninstall hivemind@hivemind` |
| OpenCode | Remove the entry from the `plugin` list |
| Gemini CLI | Delete the two skill folders from `~/.gemini/skills/` and `mcpServers.hivemind` from `~/.gemini/settings.json` |
| Any other harness | Delete the two skill folders you copied |

## Incognito sessions

An incognito session has Hivemind **completely off**. The agent neither
reads from nor writes to it, and the server never learns the session
happened (ADRs 0003, 0035). Start one with the launcher:

```sh
plugins/hivemind/bin/hivemind-incognito claude     # or: codex, dsh, hermes, pi, omp, opencode (Gemini CLI: see its reference)
```

Put it on your `PATH` (`ln -s "$PWD/plugins/hivemind/bin/hivemind-incognito" ~/.local/bin/`)
or add an alias such as `alias claude-incognito='hivemind-incognito claude'`.

The launcher sets `HIVEMIND_INCOGNITO=1`, so the reminders tell the agent
not to use or mention Hivemind and to mark local notes
`[hivemind: incognito, never upload]`. It also adds the harness's own
switch, so the Hivemind tools do not load at all:

| Harness | Switch | You need |
|---|---|---|
| Claude Code | `--settings '{"deniedMcpServers":[…]}'` for this session, naming the server as `hivemind` and as the plugin-scoped `plugin:hivemind:hivemind`, and its URL (from the shell or the `env` block of `~/.claude/settings.json`); the launcher also unsets the key and URL | nothing (whether a name entry matches the scoped plugin name is not confirmed by Claude Code's docs: check `/mcp`) |
| Codex | `-c mcp_servers.hivemind.enabled=false`; the launcher also unsets the key | the `mcp_servers.hivemind` entry in `~/.codex/config.toml` or `.codex/config.toml` |
| DeepSeek Harness | the bundle's server row switches itself off | nothing |
| Hermes | `HIVEMIND_ENABLED=false` | `enabled: ${HIVEMIND_ENABLED}` in the hivemind server entry in `~/.hermes/config.yaml`, and **no** `HIVEMIND_ENABLED` in `~/.hermes/.env` (Hermes loads it over the environment, so it would override the launcher); for normal sessions leave it unset or export `HIVEMIND_ENABLED=true` in the shell profile |
| Pi | Pi 0.99+ built-in MCP: the package registers no server, and the key and URL are unset. With pi-mcp-adapter: an exclusive MCP config, your global and project servers minus hivemind. Either way the extension also blocks `mcp__hivemind…` calls (codemode scripts included), the adapter's `mcp` proxy and `mcpScript` calls that name Hivemind (the `mcpScript` scan is best effort) | the hivemind package; `python3` with the adapter |
| Oh My Pi | `HIVEMIND_MCP_URL`/`HIVEMIND_API_KEY` unset, so the server is never contacted (Oh My Pi warns once that it is unavailable); the extension also blocks any Hivemind call | the hivemind plugin |
| OpenCode | the plugin skips the server; a hand-configured one is disabled with `OPENCODE_CONFIG_CONTENT` | nothing |

**The tools are kept from loading only where the table says so.** Where
the switch is missing or not configured (the Hermes entry without
`${HIVEMIND_ENABLED}`, a Codex without the entry, Gemini CLI, any other
command), the session is incognito **by restraint**: the tools are
loaded, the key and URL stay in the environment, and an agent with a
shell could still call the REST API. The launcher unsets the key and URL
only where the server is kept from loading (Claude Code, Codex, DeepSeek
Harness, Pi, Oh My Pi), because the other harnesses may still need them
(ADR 0035). A DeepSeek Harness session stays incognito even when a
project `.env` names `HIVEMIND_*` variables.

Without the launcher, set `HIVEMIND_INCOGNITO=1` and apply the switch
yourself. Setting only the variable gives you incognito by restraint, and
the agent says so. The same holds when you turn incognito on during a
session. A session started incognito cannot turn Hivemind back on: start
a new one.

## What is in the plugin

| Part | What it does |
|---|---|
| `skills/hivemind/` | The always-on rules: `hive_whoami` first, when to search, when and how to write, what never to write, local memory only as a fallback |
| `skills/hivemind-setup/` | The guided setup above. It works out its harness first (never from the model), then reads only that harness's file in `references/` |
| `hooks/hooks.json` | A `SessionStart` hook (`startup`, `resume`, `clear`, `compact`, `fork`) that re-injects a short reminder whenever the context is rebuilt (Claude Code, Codex) |
| `.mcp.json` | The MCP server for Claude Code, built from `HIVEMIND_MCP_URL` and `HIVEMIND_API_KEY` |
| `package.json`, `cordis.patch.yml`, `dsh/` | The DeepSeek Harness bundle: the MCP server and a small provider that serves `skills/`. DSH needs no hook, because it keeps the server's instructions in the system prompt |
| `plugin.yaml`, `__init__.py` | The Hermes plugin: serves `skills/` and registers a system-prompt section (not yet shown by Hermes, so the `SOUL.md` block does the job) |
| `extensions/hivemind.ts` | The Pi and Oh My Pi extension: the system-prompt section, the MCP server on Pi 0.99+, and blocking Hivemind calls in incognito sessions |
| `opencode/hivemind.js` | The OpenCode plugin: registers the MCP server, adds the skills, and puts the reminder in every model request |
| `../../package.json` (repository root) | Lets DeepSeek Harness, Pi and OpenCode install all of this from the repository's Git URL |
| `bin/hivemind-incognito` | Starts any supported harness in an [incognito session](#incognito-sessions) |
