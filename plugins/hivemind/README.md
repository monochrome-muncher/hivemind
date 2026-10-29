# Hivemind agent plugin

This plugin connects your AI agent to your organization's Hivemind, a
shared memory that all the organization's agents read and write. With
the plugin installed, the agent checks in with Hivemind at the start of
each session, searches it before starting work, writes down what it
learns and tells you when it needs more rights.

It works with Claude Code, Codex, DeepSeek Harness, Gemini CLI, GitHub
Copilot CLI, Hermes, OpenCode, Pi and Oh My Pi, plus the IDE
agents Cline and KiloCode. Its two skills also work in any other agent
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
  [GitHub Copilot CLI](#github-copilot-cli) ·
  [Hermes](#hermes) ·
  [OpenCode](#opencode) ·
  [Pi](#pi) ·
  [Oh My Pi](#oh-my-pi) ·
  [Cline](#cline) ·
  [KiloCode](#kilocode) ·
  [Any other agent](#any-other-agent)
- [2. Let the agent finish the setup](#2-let-the-agent-finish-the-setup)
- [3. Swap the org key for the agent key](#3-swap-the-org-key-for-the-agent-key)
- [Setting it up by hand](#setting-it-up-by-hand)
- [Reminders to write and rate](#reminders-to-write-and-rate)
- [Incognito sessions](#incognito-sessions)
- [Updating](#updating)
- [When an agent is replaced](#when-an-agent-is-replaced)
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
instead.

Some commands contain a value in angle brackets, like `<name>`. Replace
the whole thing, brackets included, with your own value. Where a command
says `v<version>`, that means a release such as `v2.5.0`;
[CHANGELOG.md](CHANGELOG.md) lists them.

## 1. Install the plugin

Install it in your agent, then carry on with
[step 2](#2-let-the-agent-finish-the-setup).

Use the URLs as shown. On a Git server other than GitHub, keep the
`.git` at the end. DeepSeek Harness and OpenCode also need the `git+`
prefix, KiloCode uses its own `git:` spec, and Hermes needs
`#plugins/hivemind`. Don't paste a link with a trailing slash, or a
`/tree/...` link copied from the browser.

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
install it. A GitHub URL works there as is. A URL from another Git
server must end in `.git`.

A plugin belongs to one profile; if you use DSH in more than one
(terminal, Desktop app), install it in each.

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

Add `--ref v<version>` to stay on one release. This works from
`v2.6.0` on; 2.5.0 and older came out before the extension existed.

### GitHub Copilot CLI

In a terminal:

```sh
copilot plugin marketplace add https://github.com/monochrome-muncher/hivemind
copilot plugin install hivemind@hivemind
```

The plugin brings both skills and the reminders. It does not bring the
connection: Copilot CLI cannot read the whole address from an
environment variable, so you add the MCP server once yourself (see
[Setting it up by hand](#setting-it-up-by-hand)), or let the setup in
step 2 do it. Don't use `copilot plugin install <owner>/<repo>`: Copilot
calls direct installs deprecated, and the repository root is not a
plugin. Checked with Copilot CLI 1.0.95.

### Hermes

In a terminal:

```sh
hermes plugins install monochrome-muncher/hivemind/plugins/hivemind
hermes plugins enable hivemind
```

From a local copy, use `hermes plugins install /path/to/hivemind/plugins/hivemind`.

The short form above works for GitHub only. For another Git server, use:

```sh
hermes plugins install https://<git-server>/<owner>/hivemind.git#plugins/hivemind
```

Hermes scans a plugin before it installs it. It rates Hivemind CAUTION,
lists what it found (mostly false alarms from text in the docs) and asks
"Install anyway? [y/N]". Answer `y` if you trust the source. Without a
terminal, for example when an agent runs the command, Hermes refuses
and says to use `--force`.

Hermes v2026.9.24 (0.21.5) and later add a Hivemind section to every new
session by themselves. Older versions need a short block in
`~/.hermes/SOUL.md` for that. The setup in step 2 adds it either way.

### OpenCode

In a terminal:

```sh
# OpenCode v1
opencode plugin -g git+https://github.com/monochrome-muncher/hivemind.git#v<version>

# OpenCode v2
opencode plugin add git+https://github.com/monochrome-muncher/hivemind.git#v<version>
```

The plugin supports both v1 (1.18.29 and later) and v2; they have
different plugin APIs and it carries both. Without `-g` (v1) it installs
for the current project only; v2's `add` always writes the global config.
You can also add the same entry to the `plugin` list in your OpenCode
config file (`~/.config/opencode/opencode.json` or `.jsonc`). Write
`plugin`, not `plugins`: v1 does not load the latter, and v2 only
migrates a `plugin` entry automatically:

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

On Pi 0.99 and later the plugin sets up the MCP connection.

### Oh My Pi

In a terminal:

```sh
omp plugin marketplace add https://github.com/monochrome-muncher/hivemind
omp plugin install hivemind@hivemind
```

Use the marketplace as shown. A plain `omp install <url>` leaves out the
skills.

### Cline

Cline has no package plugin, so its setup is three files (the agent does
them in step 2, or do them by hand):

1. Add Hivemind to `~/.cline/data/settings/cline_mcp_settings.json` with
   `${env:HIVEMIND_MCP_URL}` and `${env:HIVEMIND_API_KEY}` (Cline expands
   them from the environment; the key is never written into the file).
   The `cline` CLI (3.0.73) does not expand them, so Hivemind stays off
   there; see [the Cline setup notes](skills/hivemind-setup/references/cline.md).
2. Copy both skill folders from [`skills/`](skills/) to
   `~/.cline/skills/`.
3. Copy the plugin's `cline/` hook scripts (`UserPromptSubmit`,
   `TaskStart`, `PostToolUse`, plus the `.ps1` twins on Windows) to
   `~/Documents/Cline/Hooks/`. They carry the reminder (in full on a task's first prompt, short after)
   and the end-of-task write-and-rate nudge.

Export the two variables for the shell that starts Cline (the IDE
extension does not read a shell profile: start it from that terminal).

### KiloCode

KiloCode 7.x is a fork of OpenCode, so the OpenCode plugin works in it
unchanged. Add the Hivemind repository to the `plugin` list in
`~/.config/kilo/kilo.jsonc` (all projects) or a project's `kilo.jsonc`,
then export the two variables and restart KiloCode:

```jsonc
{ "plugin": ["git:https://github.com/monochrome-muncher/hivemind@v<version>#plugins/hivemind/opencode/hivemind.js"] }
```

KiloCode's `git:` spec is `git:<repo>[@ref][#subpath]`: it clones the
repository and loads the file at `#subpath`. Pin the release tag as shown:
a pinned ref is never refreshed, so changing the tag is how it updates.

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
update or to look into a key that stopped working. If your admin replaced
your agent's key, the old one stops working: paste the new one where the
agent key was, the same way as at install, and restart the agent.

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
for the fleet and can pin a few lasting facts to the top of the fleet's
briefing. The agent tells you when it needs a higher level.

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
- **GitHub Copilot CLI**: the key comes from the shell that starts
  `copilot`, and the address goes in `~/.copilot/mcp-config.json`.
  Copilot checks that `url` is a real address before it fills in
  variables, so `"url": "${HIVEMIND_MCP_URL}"` is refused. The command
  below writes the entry; use single quotes so the shell leaves
  `${HIVEMIND_API_KEY}` alone and the key stays out of the file:

  ```sh
  copilot mcp add --transport http hivemind https://hivemind.example.org/mcp \
    --header 'Authorization: Bearer ${HIVEMIND_API_KEY}'
  ```

  Keep the name `hivemind`, because the incognito launcher switches the
  server off by that name. Add the instruction block to
  `~/.copilot/copilot-instructions.md` as described in
  [the setup skill's Copilot notes](skills/hivemind-setup/references/copilot-cli.md).
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
  needed.

- **Oh My Pi**: the shell that starts `omp`. The plugin sets up the
  connection from both variables. Nothing goes in
  `~/.omp/agent/mcp.json`.
- **Cline**: the shell that starts `cline` (the IDE extension does not
  read a shell profile: start it from that terminal). The connection goes
  in `~/.cline/data/settings/cline_mcp_settings.json` and refers to the
  variables, so the key itself never lands in that file:

  ```json
  { "mcpServers": { "hivemind": {
      "type": "streamableHttp",
      "url": "${env:HIVEMIND_MCP_URL}",
      "headers": { "Authorization": "Bearer ${env:HIVEMIND_API_KEY}" },
      "disabled": false } } }
  ```

- **KiloCode**: the shell that starts `kilo` (the IDE extension does not
  read a shell profile: start it from that terminal). The plugin reads
  both variables and sets up the connection. Without the plugin, add this
  to `~/.config/kilo/kilo.jsonc`:

  ```jsonc
  { "mcp": { "hivemind": {
      "type": "remote",
      "url": "{env:HIVEMIND_MCP_URL}",
      "headers": { "Authorization": "Bearer {env:HIVEMIND_API_KEY}" } } } }
  ```

## Reminders to write and rate

Agents tend to forget to write things down. The plugin adds two small
reminders:

- **After the agent's context is compacted**, the startup reminder adds a
  line asking it to write down what it learned and to rate the entries it
  used.
- **At the end of a long session** (20 or more tool calls) in which the
  agent rated nothing and either wrote nothing to Hivemind or read
  entries from it, it is asked once to write what another agent could
  reuse and to rate the entries its work tested. If there is nothing
  new, it simply finishes.

In an incognito session the startup line is replaced by the incognito
text and the end-of-session reminder never appears. The end-of-session one
is also skipped when `HIVEMIND_API_KEY` is not set. What each agent gets:

- **Claude Code and Codex**: both reminders. To turn off the
  end-of-session one, disable the plugin's `Stop` hook in `/hooks`. Codex
  runs them only after you trust the plugin's hooks.
- **Pi, Oh My Pi and OpenCode**: both reminders. To turn off the
  end-of-session one, set `HIVEMIND_STOP_MIN_TOOLS` to a very high
  number. OpenCode sends it as a new prompt, so `opencode run`, which
  exits as soon as the agent goes quiet, never gets it.
- **DeepSeek Harness**: only the end-of-session reminder. DSH already
  keeps Hivemind's instructions through a compaction.
- **Gemini CLI**: only the end-of-session reminder, from an `AfterAgent`
  hook (turn it off with `gemini hooks`). The `SessionStart` hook still
  gives the startup text, but Gemini runs no startup hook after compacting
  its context, so there is no post-compaction line; the instruction block
  in `~/.gemini/GEMINI.md` is what carries Hivemind across a compaction.
- **GitHub Copilot CLI**: only the end-of-session reminder, from the
  plugin's `Stop` hook. Its `SessionStart` hook still gives the startup
  text, but Copilot runs no startup hook after compacting its context, so
  there is no post-compaction line; the instruction block in
  `~/.copilot/copilot-instructions.md` is what carries Hivemind across a
  compaction.
- **Hermes**: neither. Hermes has no hook that can hold the end of a
  session.
- **KiloCode**: both reminders, from the OpenCode plugin it runs (it is an
  OpenCode fork). To turn off the end-of-session one, set
  `HIVEMIND_STOP_MIN_TOOLS` to a very high number.
- **Cline**: the end-of-task one, from the `PostToolUse` hook (a Cline
  "task" is one run of the agent; to turn it off, set
  `HIVEMIND_STOP_MIN_TOOLS` to a very high number). There is no
  post-compaction line: Cline's `PreCompact` hook output is not injected.
  The `UserPromptSubmit` hook gives the full startup text on a task's
  first prompt and a short line naming the skill on each later one, which
  is what carries Hivemind across a compaction.

## Incognito sessions

An incognito session has Hivemind **switched off completely**. The agent
doesn't read from it or write to it, and the server never learns the
session happened.

To start one, put the `hivemind-incognito` launcher in front of the
agent's usual command. The launcher is a file in this repository, so you
need a copy of it (`git clone` it if you installed the plugin another
way):

```sh
/path/to/hivemind/plugins/hivemind/bin/hivemind-incognito claude
```

It also takes `codex`, `copilot`, `dsh`, `gemini`, `hermes`, `pi`, `omp`,
`opencode`, `cline` and `kilocode` (or `kilo`). To make it shorter to type, link it into a folder on your
`PATH` (`ln -s /path/to/hivemind/plugins/hivemind/bin/hivemind-incognito ~/.local/bin/`)
or add an alias such as `alias claude-incognito='hivemind-incognito claude'`.

How well it works depends on the agent:

- **Claude Code, DeepSeek Harness, Gemini CLI (installed as an
  extension), Pi, Oh My Pi, OpenCode and KiloCode**: the Hivemind tools
  aren't loaded at all, and there is nothing to set up.
- **Cline**: the tools aren't loaded as long as the connection uses the
  `${env:…}` references described above. The launcher unsets the two
  variables, so the references stay unexpanded and the server never
  connects. The hooks say the incognito line; the skills stay installed.
- **Codex**: the tools aren't loaded, as long as the connection is in
  `~/.codex/config.toml` (or the project's `.codex/config.toml`) as
  described above.
- **GitHub Copilot CLI**: the tools aren't loaded, as long as the
  connection is the server named `hivemind` described above (the
  launcher starts Copilot with `--disable-mcp-server hivemind`).
- **Hermes**: the tools aren't loaded once you add
  `enabled: ${HIVEMIND_ENABLED}` to the `hivemind` entry in
  `~/.hermes/config.yaml`. Don't set `HIVEMIND_ENABLED` in
  `~/.hermes/.env`, because that file overrides the launcher.
- **Gemini CLI set up by hand, and any other agent**: incognito **by
  restraint**. The tools stay loaded, and the agent is told not to use
  them.

Without the launcher, you can set `HIVEMIND_INCOGNITO=1` yourself before
starting the agent, or ask the agent mid-session to stop using Hivemind.
Both are incognito by restraint too, and the agent tells you so. A
session that started incognito can't switch Hivemind back on; start a new
one. [DEVELOPING.md](DEVELOPING.md#incognito) has the details for each
agent.

## Updating

Update the plugin with the commands below, then **start a new session**.
A session that is already running keeps the instructions it loaded at
the start. [CHANGELOG.md](CHANGELOG.md) says what changed in each
release, so you can tell whether an update matters to you.

Two things don't update with the plugin. The server's own part (the tool
descriptions and what `hive_whoami` says) updates when your admin
upgrades the server. Anything copied into your own files, such as the
`<!-- hivemind:begin -->` block or skills you copied by hand, never
updates by itself: ask the agent to "update the Hivemind setup", or copy
them again.

All of these go in a terminal:

- **Claude Code**: `claude plugin marketplace update hivemind`, then
  `claude plugin update hivemind@hivemind`, then restart Claude Code.
  Updating the marketplace alone only refreshes the list of plugins.
- **Codex**: `codex plugin marketplace upgrade hivemind`, then install it
  again with `codex plugin add hivemind@hivemind` (Codex runs a cached
  copy). `codex plugin list --marketplace hivemind` shows the version.
- **DeepSeek Harness**:
  `dsh plugin --profile <name> update hivemind-agent-plugin`, then
  restart `dsh`. If you added it with `#v<version>`, run `add` again with
  the new version instead. From a local copy, run `git pull` in it. If
  you installed it before 2.0.1, when it was called
  `hivemind-dsh-plugin`, remove it under that name and add it again.
- **Gemini CLI**: `gemini extensions update hivemind`. If you installed it
  with `--ref`, uninstall it and install it again with the new ref. Then
  ask the agent to update the Hivemind setup, which refreshes the block in
  `~/.gemini/GEMINI.md`. If you set Gemini up by hand before the extension
  existed, the setup skill's Gemini notes explain how to switch.
- **GitHub Copilot CLI**: `copilot plugin marketplace update hivemind`,
  then `copilot plugin update hivemind@hivemind`. Then ask the agent to
  update the Hivemind setup, which refreshes the block in
  `~/.copilot/copilot-instructions.md`.
- **Hermes**: `hermes plugins update hivemind`. If you installed a fixed
  ref: `hermes plugins install <source> --force --ref <commit-sha>`
  (the full 40-character commit SHA; a tag or branch name is refused).
- **OpenCode**: change the version in the `plugin` entry and restart. An
  entry without a version never updates: add one, or delete the cached
  copy under `~/.cache/opencode/` and restart.
- **KiloCode**: change the tag in the `plugin` entry
  (`git:…/hivemind@v<version>#…`) and restart. A pinned entry is never
  refreshed, so the tag change is the update; a branch entry updates on
  its own about hourly.
- **Cline**: copy the new plugin's `cline/` hook scripts over the old
  ones in `~/Documents/Cline/Hooks/` and the new `skills/` folders over
  the old copies in `~/.cline/skills/`. The MCP entry does not change
  between releases. Then restart Cline.
- **Pi**: `pi update --extensions`. If you installed a fixed version,
  install again with the new one. From a local copy, run `git pull` in it.
- **Oh My Pi**: `omp plugin marketplace update hivemind`, then
  `omp plugin upgrade` (or `omp plugin install hivemind@hivemind --force`).
- **Any other agent**: copy both `skills/` folders over your old copies.

## When an agent is replaced

If your admin retires an agent in favour of a new one, its key stops working and the new agent owns what it wrote (SPEC §12.1); `hive_whoami` lists them as `inherits_from`. `hive_whoami` also shows `key_last_used_at`, when your key last worked (to the hour; `null` if not recorded yet).

## Removing

First uninstall the plugin, in a terminal:

- **Claude Code**: `claude plugin uninstall hivemind@hivemind`
- **Codex**: `codex plugin remove hivemind@hivemind`, then delete the
  `mcp_servers.hivemind` section from `~/.codex/config.toml`.
- **DeepSeek Harness**:
  `dsh plugin --profile <name> remove hivemind-agent-plugin` (or
  `hivemind-dsh-plugin` if you installed it before 2.0.1).
- **Gemini CLI**: `gemini extensions uninstall hivemind`. If you set it up
  by hand, delete the two skill folders from `~/.gemini/skills/`, and
  `mcpServers.hivemind` and the Hivemind hooks from
  `~/.gemini/settings.json`.
- **GitHub Copilot CLI**: `copilot plugin uninstall hivemind@hivemind`,
  then `copilot mcp remove hivemind`.
- **Hermes**: `hermes plugins remove hivemind`, then delete the
  `hivemind` entry from `~/.hermes/config.yaml`.
- **OpenCode**: remove the entry from the `plugin` list. On v2,
  `opencode plugin remove <package>` does it. v1 has no remove command,
  so edit the config file.
- **KiloCode**: remove the entry from the `plugin` list in
  `kilo.jsonc` (edit the file).
- **Cline**: remove the `hivemind` entry from
  `~/.cline/data/settings/cline_mcp_settings.json`, delete the two skill
  folders from `~/.cline/skills/`, and the hook scripts from
  `~/Documents/Cline/Hooks/`.
- **Pi**: `pi remove <source>`, with the same source you installed from.
- **Oh My Pi**: `omp plugin uninstall hivemind@hivemind`
- **Any other agent**: delete the two skill folders you copied.

Then delete the block between `<!-- hivemind:begin -->` and
`<!-- hivemind:end -->` from the instructions file the setup added it to.
Depending on the agent, that is `CLAUDE.md`, `AGENTS.md`,
`~/.copilot/copilot-instructions.md`, `~/.dsh/AGENTS.md`,
`~/.gemini/GEMINI.md`, `~/.hermes/SOUL.md`,
`~/.config/opencode/AGENTS.md`, `~/.config/kilo/AGENTS.md`,
`~/.pi/agent/AGENTS.md` or
`~/.omp/agent/AGENTS.md`. You may also want to delete your key file and
any connection settings you added by hand.

## For maintainers

[DEVELOPING.md](DEVELOPING.md) explains how the plugin is built: what
each file does, how each agent gets the MCP connection, the skills and
the reminders, how incognito works in each one, and the tests and release
steps that keep it all in step with the server.
