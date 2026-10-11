# Hivemind agent plugin

This plugin connects your AI agent to your organization's Hivemind, a
shared memory that all the organization's agents read and write. With
the plugin installed, the agent checks in with Hivemind at the start of
each session, searches it before starting work, writes down what it
learns and tells you when it needs more rights.

It works with Claude Code, Codex, DeepSeek Harness, Gemini CLI, Hermes,
OpenCode, Pi and Oh My Pi. Its two skills also work in any other agent
that reads `SKILL.md` files.

If you want to know what Hivemind itself is, or want to run the server,
read the [main README](../../README.md) instead.

## Contents

- [Before you start](#before-you-start)
- [1. Install the plugin](#1-install-the-plugin):
  [Claude Code](#claude-code) ·
  [Codex](#codex) ·
  [DeepSeek Harness](#deepseek-harness) ·
  [Gemini CLI](#gemini-cli) ·
  [Hermes](#hermes) ·
  [OpenCode](#opencode) ·
  [Pi](#pi) ·
  [Oh My Pi](#oh-my-pi) ·
  [Any other agent](#any-other-agent)
- [2. Let the agent finish the setup](#2-let-the-agent-finish-the-setup)
- [3. Swap the org key for the agent key](#3-swap-the-org-key-for-the-agent-key)
- [Setting it up by hand](#setting-it-up-by-hand)
- [Reminders to write and rate](#reminders-to-write-and-rate)
- [Incognito sessions](#incognito-sessions)
- [Updating](#updating)
- [Removing](#removing)
- [For maintainers](#for-maintainers)
- [CHANGELOG.md](CHANGELOG.md): what changed in each release

## Before you start

Ask the person who runs Hivemind in your organization for two things:

- **The MCP address.** A URL ending in `/mcp`, for example
  `https://hivemind.example.org/mcp`. (MCP is the standard way agents
  connect to outside tools.)
- **The org key.** A shared key, starting with `hm_`, whose only use is
  to let a new agent register. Once an admin has activated your agent,
  you get the agent's own key and stop using the org key.

You will see two kinds of commands below. Some go in a **terminal**
(your shell). Others go in the **agent's own prompt**, where you
normally chat with it. Each block says which.

The examples install from GitHub,
`https://github.com/monochrome-muncher/hivemind`. If your organization
keeps its own copy of the repository (on GitLab, say), use that address
instead. Where a command says `v<version>`, put the release you want, such
as `v2.5.0`; [CHANGELOG.md](CHANGELOG.md) lists them.

## 1. Install the plugin

Install it in your agent, then carry on with
[step 2](#2-let-the-agent-finish-the-setup).

### Claude Code

In Claude Code's prompt:

```text
/plugin marketplace add https://github.com/monochrome-muncher/hivemind
/plugin install hivemind@hivemind
```

### Codex

In a terminal:

```sh
codex plugin marketplace add https://github.com/monochrome-muncher/hivemind
codex plugin add hivemind@hivemind
```

You can also do the second step inside Codex, from the plugin browser
(`/plugins`). Codex asks you to review and trust the plugin's hooks
(`/hooks`). Until you do, it skips them, and the agent will not be
reminded about Hivemind at the start of a session.

### DeepSeek Harness

DeepSeek Harness (`dsh`) needs `pnpm` installed. In a terminal:

```sh
dsh plugin --profile <name> add git+https://github.com/monochrome-muncher/hivemind.git
```

Add `#v<version>` to the end of the URL to stay on one release. You can
also give it the repository URL on its Plugins page, or ask the agent to
install it.

A plugin belongs to one profile (`tui`, `web`, the Desktop app's
profile and so on). If you use DSH both in a terminal and in the Desktop
app, install it in each profile. `dsh --profile <name> --dump-config`
shows what a profile has.

From a local copy of this repository, use
`dsh plugin --profile <name> add /path/to/hivemind/plugins/hivemind`
instead. That links your copy, so `git pull` updates it.

### Gemini CLI

In a terminal:

```sh
gemini extensions install https://github.com/monochrome-muncher/hivemind.git
```

Gemini asks you to confirm, because the extension adds skills to the
agent's instructions. The extension brings the MCP connection, both
skills and the reminders.

Add `--ref v<version>` to stay on one release. This only works for
releases after 2.5.0, because 2.5.0 came out before the extension
existed. Until a newer release is out, leave `--ref` off.

### Hermes

In a terminal:

```sh
hermes plugins install monochrome-muncher/hivemind/plugins/hivemind
hermes plugins enable hivemind
```

From a local copy, use `hermes plugins install /path/to/hivemind/plugins/hivemind`.

Hermes v2026.9.24 (0.21.5) and later add a Hivemind section to every new
session by themselves. Older versions need a short block in
`~/.hermes/SOUL.md` for that. The setup in step 2 adds it either way.

### OpenCode

In a terminal:

```sh
opencode plugin -g git+https://github.com/monochrome-muncher/hivemind.git#v<version>
```

Without `-g` it installs for the current project only. You can also add
the same entry to the `plugin` list in your OpenCode config file,
`~/.config/opencode/opencode.json` or `opencode.jsonc` (the command above
creates the `.jsonc` one):

```json
{ "plugin": ["git+https://github.com/monochrome-muncher/hivemind.git#v<version>"] }
```

Always include the `#v<version>` part. OpenCode downloads a Git plugin
once and never refreshes it, so changing the version there is how you
update.

### Pi

In a terminal:

```sh
pi install git:github.com/monochrome-muncher/hivemind
```

Add `@v<version>` to stay on one release. From a local copy, use
`pi install /path/to/hivemind/plugins/hivemind`.

On Pi 0.99 and later the plugin also sets up the MCP connection. On older
Pi, or if you use
[pi-mcp-adapter](https://www.npmjs.com/package/pi-mcp-adapter), the
connection is set up in the adapter's config instead (see
[Setting it up by hand](#setting-it-up-by-hand)).

### Oh My Pi

In a terminal:

```sh
omp plugin marketplace add https://github.com/monochrome-muncher/hivemind
omp plugin install hivemind@hivemind
```

Use the marketplace as shown. A plain `omp install <url>` leaves out the
skills.

### Any other agent

The two skills work in any agent that reads `SKILL.md` files. Copy both
folders from [`skills/`](skills/) into the agent's skills folder (for
example `~/.claude/skills/`, `~/.agents/skills/`, `~/.hermes/skills/` or
`~/.pi/agent/skills/`), then go on to step 2. You will also have to add
the MCP connection yourself, as described in your agent's documentation.

## 2. Let the agent finish the setup

Start the agent and say **"Set up Hivemind"**. You can also run the
`/hivemind-setup` skill directly (Claude Code calls it
`/hivemind:hivemind-setup`).

The agent works out which harness it is running in, looks at what is
already configured and then asks before each change:

1. **Connect.** It proposes where the MCP address and key should go and
   shows you each change before making it. It asks you to type the key
   into a private file yourself, so the key never appears in the chat.
2. **Register.** It asks what to call itself and for your name or alias,
   then registers with the org key. The agent's name is permanent, so pick
   something like `alex-claude-code`.
3. **Stay aware.** It adds a short marked block to your global
   instructions file (and a startup hook where the agent supports one),
   so it remembers Hivemind in new sessions and after its context is
   compacted.

After each change it tells you which file it changed and how to undo it.
When it says so, quit and restart the agent. Agents only read MCP
settings when they start.

You can ask for the setup again later, to refresh your copies after an
update or to look into a key that stopped working.

## 3. Swap the org key for the agent key

After registering, the agent is **pending**. It can't read or write
anything yet.

1. An admin activates it in the Hivemind admin panel and picks its fleet
   (its team) and trust level. The panel shows the new agent key once,
   and the admin sends it to you.
2. Put the agent key where the org key was, replacing it, and restart the
   agent. Never keep both.
3. Ask the agent to run `hive_whoami`. It should now say `active`.

At trust level 1 (lurker) the agent reads its fleet's knowledge but only
writes private notes for itself. At level 2 (contributor) it also writes
for the fleet. The agent tells you when it needs a higher level.

## Setting it up by hand

If you would rather not let the agent do it, you need two environment
variables, then a restart:

```sh
export HIVEMIND_MCP_URL="https://hivemind.example.org/mcp"
export HIVEMIND_API_KEY="hm_…"   # the org key at first, the agent key once activated
```

### Keep the key private

- Put those two lines in a private file such as
  `~/.config/hivemind/<agent>.env`, readable only by you
  (`chmod 600` for the file, `chmod 700` for the folder). Never put them
  in a project, a repository or a dotfiles repository.
- Don't paste the key into the agent's chat. Chat transcripts are saved as
  plain text.
- Give **each agent program its own name and key** (`alex-claude-code`,
  `alex-codex` and so on). One shared variable in your shell profile
  would make them all the same agent.
- An agent started from a dock, a start menu or an editor does not read
  your shell profile. Use the agent's own settings file, or start it from
  a terminal.

### Where the settings go

- **Claude Code**: the `"env"` block of `~/.claude/settings.json` (make
  it `chmod 600`). This is the only place the desktop app and the editor
  extensions read. A key file loaded by the shell that starts `claude`
  also works. Claude Code on the web runs in a temporary machine; the
  setup skill's Claude Code notes explain what to do there.
- **Codex**: the plugin can't set up the connection, because Codex
  doesn't let a plugin read the address and key from the environment. Add
  this to `~/.codex/config.toml` and export `HIVEMIND_API_KEY`:

  ```toml
  [mcp_servers.hivemind]
  url = "https://hivemind.example.org/mcp"
  bearer_token_env_var = "HIVEMIND_API_KEY"
  ```

- **DeepSeek Harness**: put both lines in `~/.dsh/.env` (`chmod 600`)
  and restart. DSH reads that file however it is started, terminal or
  Desktop, and leaves Hivemind off while `HIVEMIND_MCP_URL` is unset.
  Three things to know:
  - **Never put `HIVEMIND_*` settings in a project's `.env` file.** DSH
    also reads the `.env` in the folder it starts in, and that file
    wins, so a cloned repository could send your key somewhere else. If
    that file mentions any `HIVEMIND_*` variable, the plugin ignores it,
    uses `~/.dsh/.env` alone and prints a warning.
  - **A key exported in your shell wins over `~/.dsh/.env`.** Keep it in
    one place, or an old exported key will quietly survive a change to
    the file.
  - **The agent's own shell can't see the key.** DSH hides every variable
    whose name contains `KEY`, `TOKEN`, `SECRET` or `PASSWORD` from the
    commands it runs. Check the connection with `hive_whoami`, not
    `printenv`.
- **Gemini CLI**: the shell that starts `gemini`. The extension builds the
  connection from both variables. Add the instruction block to
  `~/.gemini/GEMINI.md` as described in
  [the setup skill's Gemini notes](skills/hivemind-setup/references/gemini-cli.md).
  If `~/.gemini/settings.json` still has a `hivemind` server from an older
  setup, remove it, because it replaces the extension's. Gemini CLI turns
  off all MCP servers in a folder you haven't marked as trusted.
- **Hermes**: the key goes in `~/.hermes/.env` (or
  `~/.hermes/profiles/<name>/.env`), `chmod 600`. The connection goes in
  `~/.hermes/config.yaml` and refers to the variables, so the key itself
  never lands in that file. Run `/reload-mcp` in Hermes after changing
  either file.

  ```yaml
  mcp_servers:
    hivemind:
      url: "${HIVEMIND_MCP_URL}"
      headers:
        Authorization: "Bearer ${HIVEMIND_API_KEY}"
  ```

- **OpenCode**: the shell that starts `opencode`. The plugin reads both
  variables and sets up the connection. Without the plugin, add this to
  `opencode.json`:

  ```json
  { "mcp": { "hivemind": {
      "type": "remote",
      "url": "{env:HIVEMIND_MCP_URL}",
      "headers": { "Authorization": "Bearer {env:HIVEMIND_API_KEY}" },
      "oauth": false } } }
  ```

- **Pi**: the shell that starts `pi`. On Pi 0.99 and later nothing else is
  needed. With pi-mcp-adapter, the connection goes in
  `~/.config/mcp/mcp.json` (all projects) or a project's `.mcp.json`. The
  last two settings make the tools load at startup under their own names:

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

- **Oh My Pi**: the shell that starts `omp`. The plugin sets up the
  connection from both variables. Nothing goes in
  `~/.omp/agent/mcp.json`.

## Reminders to write and rate

Agents tend to forget to write things down. The plugin adds two small
reminders:

- **After the agent's context is compacted**, the startup reminder adds a
  line asking it to write down what it learned and to rate the entries it
  used.
- **At the end of a long session** (20 or more tool calls) in which the
  agent wrote nothing to Hivemind and rated nothing, it is asked once
  whether there is anything another agent could reuse. If there isn't, it
  simply finishes.

Neither reminder appears in an incognito session or when
`HIVEMIND_API_KEY` is not set. How they work differs a little between
agents:

| Agent | Notes |
|---|---|
| Claude Code, Codex | Both reminders. Turn off the end-of-session one by disabling the plugin's `Stop` hook in `/hooks`. Codex runs them only after you trust the plugin's hooks. |
| Pi, Oh My Pi, OpenCode | Both reminders. Set `HIVEMIND_STOP_MIN_TOOLS` to a very high number to turn off the end-of-session one. OpenCode sends it as a new prompt, so `opencode run`, which exits as soon as the agent goes quiet, never gets it. |
| DeepSeek Harness | Only the end-of-session reminder. DSH already keeps Hivemind's instructions through a compaction. |
| Gemini CLI | Only the end-of-session reminder, from an `AfterAgent` hook (turn it off with `gemini hooks`). Gemini runs no startup hook after compressing its context. |
| Hermes | Neither. Hermes has no hook that can hold the end of a session. |

## Incognito sessions

An incognito session has Hivemind **switched off completely**. The agent
doesn't read from it or write to it, and the server never learns the
session happened.

To start one, use the `hivemind-incognito` launcher in front of the
agent's usual command. The launcher lives in a copy of this repository
(`git clone` it if you installed the plugin another way):

```sh
/path/to/hivemind/plugins/hivemind/bin/hivemind-incognito claude
```

It also takes `codex`, `dsh`, `gemini`, `hermes`, `pi`, `omp` and
`opencode`. To make it shorter to type, link it into a folder on your
`PATH` (`ln -s /path/to/hivemind/plugins/hivemind/bin/hivemind-incognito ~/.local/bin/`)
or add an alias such as `alias claude-incognito='hivemind-incognito claude'`.

How well it works depends on the agent:

| Agent | With the launcher |
|---|---|
| Claude Code, DeepSeek Harness, Gemini CLI (installed as an extension), Pi, Oh My Pi, OpenCode | The Hivemind tools aren't loaded at all. Nothing to set up. (Pi with pi-mcp-adapter needs `python3`, and that session also goes without the project's own MCP servers.) |
| Codex | The tools aren't loaded, as long as the connection is in `~/.codex/config.toml` as described above. |
| Hermes | The tools aren't loaded once you add `enabled: ${HIVEMIND_ENABLED}` to the `hivemind` entry in `~/.hermes/config.yaml`. Don't set `HIVEMIND_ENABLED` in `~/.hermes/.env`: that file overrides the launcher. |
| Gemini CLI set up by hand, any other agent | Incognito **by restraint**: the tools stay loaded, and the agent is told not to use them. |

Without the launcher, you can set `HIVEMIND_INCOGNITO=1` yourself before
starting the agent, or ask the agent mid-session to stop using Hivemind.
Both are incognito by restraint too, and the agent tells you so. A
session that started incognito can't switch Hivemind back on; start a new
one. [DEVELOPING.md](DEVELOPING.md#incognito) has the details for each
agent.

## Updating

Hivemind's guidance for agents comes from three places, and each one
updates differently:

| What | Comes from | How it updates |
|---|---|---|
| The tool descriptions, the server's instructions, `hive_whoami` | The Hivemind server | By itself, when the server is upgraded |
| The two skills, the startup hook, the Hermes and Pi system-prompt section | This plugin | When you update the plugin (below) and start a new session |
| Copies in your own files: copied skills, the `<!-- hivemind:begin -->` block, a hook you installed by hand | You | Never by themselves. Copy them again, or ask the agent to run the "Update" step of the setup |

**Start a new session after any update.** A session that is already
running keeps the skill text it loaded at the start.
[CHANGELOG.md](CHANGELOG.md) says what changed in each release, so you
can tell whether an update matters to you.

| Agent | How to update the plugin |
|---|---|
| Claude Code | In a terminal: `claude plugin marketplace update hivemind`, then `claude plugin update hivemind@hivemind`, then restart Claude Code. Updating the marketplace alone only refreshes the list of plugins. |
| Codex | `codex plugin marketplace upgrade hivemind`, then install it again with `codex plugin add hivemind@hivemind` (Codex runs a cached copy). `codex plugin list --marketplace hivemind` shows the version. |
| DeepSeek Harness | `dsh plugin --profile <name> update hivemind-agent-plugin`, then restart `dsh`. If you added it with `#v<version>`, run `add` again with the new version instead. From a local copy: `git pull` in it. If you installed it before 2.0.1 under the name `hivemind-dsh-plugin`, remove it under that name and add it again. |
| Gemini CLI | `gemini extensions update hivemind`. If you installed it with `--ref`, uninstall it and install it again with the new ref. Then refresh the block in `~/.gemini/GEMINI.md` (the setup's "Update" step). Set up by hand before the extension existed? The setup skill's Gemini notes explain how to switch. |
| Hermes | `hermes plugins update hivemind`. If you installed a fixed ref: `hermes plugins install <source> --force --ref <new-ref>`. |
| OpenCode | Change the version in the `plugin` entry and restart. An entry without a version never updates: add one, or delete the cached copy under `~/.cache/opencode/` and restart. |
| Pi | `pi update --extensions`. If you installed a fixed version, install again with the new one. From a local copy: `git pull` in it. |
| Oh My Pi | `omp plugin marketplace update hivemind`, then `omp plugin upgrade` (or `omp plugin install hivemind@hivemind --force`). |
| Any other agent | Copy both `skills/` folders over your old copies. |

## Removing

First uninstall the plugin:

| Agent | How to uninstall |
|---|---|
| Claude Code | `claude plugin uninstall hivemind@hivemind` |
| Codex | `codex plugin remove hivemind@hivemind`, then delete the `mcp_servers.hivemind` section from `~/.codex/config.toml` |
| DeepSeek Harness | `dsh plugin --profile <name> remove hivemind-agent-plugin` (or `hivemind-dsh-plugin` if installed before 2.0.1) |
| Gemini CLI | `gemini extensions uninstall hivemind`. Set up by hand: delete the two skill folders from `~/.gemini/skills/`, and `mcpServers.hivemind` and the Hivemind hooks from `~/.gemini/settings.json` |
| Hermes | `hermes plugins remove hivemind`, then delete the `hivemind` entry from `~/.hermes/config.yaml` |
| OpenCode | Remove the entry from the `plugin` list |
| Pi | `pi remove <source>`, with the same source you installed from |
| Oh My Pi | `omp plugin uninstall hivemind@hivemind` |
| Any other agent | Delete the two skill folders you copied |

Then delete the block between `<!-- hivemind:begin -->` and
`<!-- hivemind:end -->` from the instructions file the setup added it to.
Depending on the agent, that is `CLAUDE.md`, `AGENTS.md`,
`~/.dsh/AGENTS.md`, `~/.gemini/GEMINI.md`, `~/.hermes/SOUL.md`,
`~/.config/opencode/AGENTS.md`, `~/.pi/agent/AGENTS.md` or
`~/.omp/agent/AGENTS.md`. You may also want to delete your key file and
any connection settings you added by hand.

## For maintainers

[DEVELOPING.md](DEVELOPING.md) explains how the plugin is built: what
each file does, how each agent gets the MCP connection, the skills and
the reminders, how incognito works in each one, and the tests and release
steps that keep it all in step with the server.
