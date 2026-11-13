# Developing the Hivemind agent plugin

This file is for people and agents who change the plugin. If you only want
to install or use it, read [README.md](README.md) instead.

## Contents

- [What is in the plugin](#what-is-in-the-plugin)
- [How each harness gets each piece](#how-each-harness-gets-each-piece)
- [Harness quirks](#harness-quirks)
- [The Stop hook and the post-compaction line](#the-stop-hook-and-the-post-compaction-line)
- [Incognito](#incognito)
- [Key profiles](#key-profiles)
- [The drift check](#the-drift-check)
- [Tests](#tests)
- [Releasing](#releasing)

## What is in the plugin

| Part | What it does |
|---|---|
| `skills/hivemind/` | The always-on rules: `hive_whoami` first, when to search, when and how to write, what never to write, local memory only as a fallback |
| `skills/hivemind-setup/` | The guided setup. Harness-neutral itself: it works out its harness first (never from the model), then reads only that harness's file in `references/` |
| `skills/hivemind-update/` | The update skill: checks for drift from the latest release, then follows its harness's file in `references/` to update the plugin and refresh the copies setup wrote outside it. Harness-neutral itself, like the setup skill |
| `skills/hivemind-switch/` | The key-profile skill: who am I, which profiles exist, how to add one and switch, and the harnesses where a switch is persistent (ADR 0150) |
| `skills/hivemind-update/scripts/hivemind-drift.sh` | The drift check ([below](#the-drift-check)) |
| `skills/hivemind-setup/scripts/hivemind-session-start.sh` | The reminder text the startup hook prints, including its incognito variant |
| `hooks/hooks.json` | A `SessionStart` hook (`startup`, `resume`, `clear`, `compact`, `fork`) that re-injects the reminder whenever the context is rebuilt (Claude Code, Codex), and a `Stop` hook (Claude Code, Codex). GitHub Copilot CLI and Junie read the same file (its Claude-style hooks format) |
| `skills/hivemind-setup/scripts/hivemind-stop.sh` | The `Stop` hook ([below](#the-stop-hook-and-the-post-compaction-line)) |
| `.mcp.json` | The MCP server for Claude Code, built from `HIVEMIND_MCP_URL` and `HIVEMIND_API_KEY`. GitHub Copilot CLI rejects it (see its quirk below) |
| `package.json`, `cordis.patch.yml`, `dsh/` | The DeepSeek Harness bundle: the MCP server row, a small provider (`dsh/hivemind-skills.js`) that serves `skills/`, and the Stop reminder (`dsh/hivemind-reminders.js`) |
| `plugin.yaml`, `__init__.py` | The Hermes plugin: serves `skills/` and registers a system-prompt section |
| `extensions/hivemind.ts` | The Pi and Oh My Pi extension: the system-prompt section, the MCP server on Pi 0.99+, and blocking Hivemind calls in incognito sessions |
| `opencode/hivemind.js` | The OpenCode plugin (which KiloCode 7.x, an OpenCode fork, loads too): registers the MCP server, adds the skills, and puts the reminder in every model request |
| `guidelines/hivemind.md` | The Junie guidelines file: the `STAY_AWARE_CORE` sentence plus one line telling Junie's MCP tool picker to always select the `hive_*` tools. Nothing else reads this folder |
| `junie/incognito.md` | The incognito text (`INCOGNITO_TEXT`) that `hivemind-incognito junie` passes to Junie as extra guidelines |
| `cline/` | The Cline hooks: `UserPromptSubmit` (the stay-aware line: in full on a task's first prompt, short on later ones), `TaskStart` and `PostToolUse` (the end-of-task write-and-rate nudge), each with a `.ps1` twin for Windows |
| `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json` | The plugin manifests for Claude Code (also read by GitHub Copilot CLI and Junie) and Codex, which point Codex at `skills/` and `hooks/hooks.json` |
| `.cursor-plugin/plugin.json`, `cursor/hooks.json`, `cursor/rules/hivemind.mdc` | The Cursor plugin: the manifest points Cursor at `skills/`, at the Cursor-only hooks file (so it never parses the Claude-format `hooks/hooks.json`) and at the rules folder; the hook runs the startup script on `sessionStart`; the always-on rule holds the startup sentence, though Cursor did not load it in the live check, so setup also writes a user rule. No `mcp.json`: the user configures the server |
| `bin/hivemind-incognito` | The incognito launcher ([below](#incognito)) |
| `bin/hivemind-as` | The key-profile launcher ([below](#key-profiles)) |
| `CHANGELOG.md` | What changed for agents in each release |
| `../../package.json` (repository root) | Same package name as `plugins/hivemind/package.json`. Lets DeepSeek Harness, Pi and OpenCode install the plugin from the repository's Git URL (OpenCode loads `exports["./server"]`, else `main`) |
| `../../gemini-extension.json`, `../../skills`, `../../hooks/hooks.json` (repository root) | The Gemini CLI extension, installed with `gemini extensions install <git-url>`: Gemini reads these three at the root of what it installs. `skills` is a link to `skills/` here; the hooks run the two scripts above as `SessionStart` and `AfterAgent` hooks |
| `../antigravity/` | The Antigravity plugin, a folder of its own (it installs as `hivemind`): `plugin.json`, `mcp_config.json` (the server, with the URL and key as install-time variables), `hooks.json` (the two scripts above, with `--antigravity`), `rules/AGENTS.md` (the instruction block's text, always on), `assets/logo.svg`, and `skills`, a link to `skills/` here. It lives apart because Antigravity reads `hooks.json` at the plugin root, a name GitHub Copilot CLI also reads in this folder |
| `../../.claude-plugin/`, `../../.agents/plugins/` (repository root) | The marketplaces Claude Code, Codex and Oh My Pi install from. GitHub Copilot CLI reads the root `.claude-plugin/marketplace.json` and the plugin's `.claude-plugin/plugin.json` too, so it needs no manifest of its own. Cursor reads the root `.cursor-plugin/marketplace.json` and the plugin's `.cursor-plugin/plugin.json` |

## How each harness gets each piece

Every harness needs three things: the MCP server, the skills, and a
reminder that survives compaction and new sessions.

| Harness | MCP server | Skills | Reminder |
|---|---|---|---|
| Claude Code | `.mcp.json` in the plugin | the plugin | `SessionStart` hook |
| Codex | the user's `~/.codex/config.toml` (plugin MCP config cannot read the URL and key from the environment) | the plugin | `SessionStart` hook, which the user must trust in `/hooks` |
| DeepSeek Harness | the bundle's row in `cordis.patch.yml` | `dsh/hivemind-skills.js` | none needed across a compaction: DSH keeps the server's instructions in the system prompt; the end-of-session nudge is `dsh/hivemind-reminders.js` |
| Hermes | the user's `~/.hermes/config.yaml` | the plugin | the plugin's system-prompt section (Hermes 0.21.5+), else the `SOUL.md` instruction block (see below) |
| Pi | the extension registers it with the built-in MCP client (Pi 0.99+) | the package | the extension's system-prompt section |
| Oh My Pi | the plugin's `.mcp.json` (server `hivemind:hivemind`; a hand `hivemind` entry in `~/.omp/agent/mcp.json` replaces it) | the plugin | the extension's system-prompt section |
| OpenCode | `opencode/hivemind.js` (skipped if a hand-configured `hivemind` server exists or no key is set; supports v1 and v2) | the plugin | added to every model request |
| KiloCode | `opencode/hivemind.js` via the `plugin` key (KiloCode is an OpenCode fork; the v1 key gives the full feature set, the v2 `plugins` key only the skills) | the plugin | added to every model request |
| Cline | the user's `~/.cline/data/settings/cline_mcp_settings.json` with `${env:…}` references (Cline has no package plugin) | copied to `~/.cline/skills/` | the `UserPromptSubmit` hook in `~/Documents/Cline/Hooks/`, plus the `PostToolUse` end-of-task nudge |
| Gemini CLI | the extension's `gemini-extension.json` (a `hivemind` entry in `~/.gemini/settings.json` replaces it) | the extension | the `GEMINI.md` instruction block, plus the extension's `SessionStart` hook |
| Antigravity | `../antigravity/mcp_config.json`, with the URL and key filled in when the user turns the plugin on (stored in `~/.gemini/config/config.json`) | the plugin | the plugin's always-on rule, plus its `SessionStart` hook |
| GitHub Copilot CLI | the user's `~/.copilot/mcp-config.json` (the whole `url` cannot come from the environment; the key can) | the plugin | the plugin's `SessionStart` hook, plus the `~/.copilot/copilot-instructions.md` instruction block |
| Cursor | the user's `~/.cursor/mcp.json` with `${env:…}` references (plugin MCP variables are set by team admins, so the plugin ships none) | the plugin (a local plugin in `~/.cursor/plugins/local/hivemind`) | the user rule `~/.cursor/rules/hivemind.mdc` that setup writes (the plugin's own rule never loaded in the live check), plus the plugin's `sessionStart` hook and the user `stop` hook in `~/.cursor/hooks.json` |
| Junie | the user's `~/.junie/mcp/mcp.json` with the literal URL and key (Junie expands no `${VAR}`) | the plugin (installed from the Claude Code marketplace) | the plugin's `guidelines/hivemind.md` (always in the guidelines), plus the `Stop` hook |

Everything the setup skill tells an agent to do for one harness lives in
that harness's file under `skills/hivemind-setup/references/`, and how
to update it in the file of the same name under
`skills/hivemind-update/references/`. Neither skill names
harness-specific paths itself, and tests enforce this. A test also
checks that every instruction block and skills folder a setup file
writes is one its update file passes to the drift check.

## Harness quirks

- **DeepSeek Harness** loads a `.env` from the directory it starts in,
  ranked above `~/.dsh/.env`. The bundle therefore ignores the environment
  whenever that project file names any `HIVEMIND_*` variable, uses
  `~/.dsh/.env` alone, and prints a warning. It finds the names with a
  lenient regex over the raw text (`KEY=value`, `KEY: value`, `export`,
  a leading BOM, any case), because node's `util.parseEnv` skips
  `KEY: value` lines and keeps a BOM in the first key, which other dotenv
  loaders accept. DSH also strips variables
  whose names contain `KEY`, `TOKEN`, `SECRET` or `PASSWORD` from the
  processes it starts, so `printenv HIVEMIND_API_KEY` is empty there even
  when Hivemind works. To distribute the bundle through an npm mirror,
  remove `"private": true` from `package.json` and publish it as
  `hivemind-agent-plugin` (named `hivemind-dsh-plugin` before 2.0.1).
- **Hermes** renders a plugin's system-prompt section from v2026.9.24
  (0.21.5): once per new session, calling the content callable with a
  read-only session-info mapping, and skipping (with a warning) a section
  that raises. 0.19 has no `register_system_prompt_section`, and 0.21.3
  never rendered it (NousResearch/hermes-agent#117432), so the `SOUL.md`
  block stays required there. Hermes parses `plugin.yaml` with a YAML
  loader and skips the whole plugin when that fails (an unquoted `: ` in
  the description did so from 2.3.1 to 2.5.0). Checked with v2026.9.24:
  both skills register and the section renders. Hermes also loads
  `~/.hermes/.env` over the process environment, which matters for
  incognito.
- **Pi** uses the built-in MCP client (Pi 0.99+): the extension registers
  the server with it, with direct exposure, unless a hand-written
  `hivemind` entry in `~/.pi/agent/mcp.json` takes precedence (Pi's rule).
  Checked live with Pi 1.0.0.
- **Oh My Pi** loads the plugin's `.mcp.json` as the server
  `hivemind:hivemind`, expanding `${VAR}` in the URL and the header, and
  mounts MCP tools as routes (`xd://mcp__hivemind_hivemind_hive_*`, or
  `xd://mcp__hivemind_hive_*` for a hand entry, which then replaces the
  plugin's) rather than as named tools; the agent finds them in its system
  prompt. Checked with Oh My Pi 18.9.1.
  A plain `omp install` of the repository installs the root package: the
  extension and, through the root `skills` link, the skills, but no MCP
  server (the package route has no `.mcp.json`, and Oh My Pi has no
  `registerMcpServer`), so it installs through the marketplace. Checked
  with 18.15.0 from a local copy of the repository root; a GitHub URL
  could not be fetched from the test container.
- **OpenCode** caches a Git plugin on first use and never refreshes an
  unpinned entry, so users pin a release tag. v1 (1.18.29+) and v2 have
  different plugin APIs, so `opencode/hivemind.js` default-exports one
  plain object that carries both: v1 calls `server()` (an object default
  export is supported from 1.18.29; older v1 releases expect a function
  export) and v2 validates the default export as `{ id, setup }` and
  ignores `server()`. The two implementations are separate, and nothing
  translates hooks: v1's `config` hook / `experimental.chat.system.transform`
  / `chat.message` / `tool.execute.after` / `event` become v2's
  `ctx.mcp.transform` + `ctx.skill.transform` / `ctx.session.hook("context")`
  (`event.system` takes `{type, text}` parts) / nothing / `ctx.tool.hook(
  "execute.after")` / `ctx.event.subscribe`. v2 MCP servers use `disabled`
  (v1 `enabled`); v2 config uses the `plugins` key and migrates a v1
  `plugin` entry, while v1 only warns about `plugins` and does not load
  it; v2 has no `session.idle`: an execution ends with
  `session.execution.succeeded` / `failed` / `interrupted` (v1's idle
  fired on each of them) and compaction ends with
  `session.compaction.ended`. Verified against the v2 source (`@opencode`
  packages) and v1.18.29.
- **KiloCode** 7.x is a fork of OpenCode (`Kilo-Org/kilocode`): the same
  plugin file loads in it, and its v1 plugin API is the OpenCode v1 one
  (same hooks; the `plugin` config key). Its v1 `event` hook receives v2
  events (`session.execution.*`, `session.compaction.ended`), which is why
  the stop and post-compaction nudges work there. KiloCode has its own
  `git:` plugin spec, `git:<repo>[@ref][#subpath]`, that clones the
  repository and loads its package entry point (the root `package.json`
  `exports["./server"]`); a `#subpath` must be a folder with its own
  `package.json`, so the documented spec has none. Only a full commit SHA
  is never re-fetched; a tag or branch is re-checked about hourly. Skills
  from a plugin outside the project load only when `skill_path_origins`
  marks their folder trusted, which the plugin's `config` hook does when
  `KILO=1` (KiloCode sets it; OpenCode does not), so a `SKILL.md` must not
  contain `{env:`, `{file:` or `` !` `` tokens, which KiloCode expands or
  runs in trusted skills. The v2 `plugins`
  key loads a reduced plugin context (no `mcp`, `session`, `tool` or
  `event` domains; only `skill`, `agent`, `command` and `integration`
  transforms), so the same file's `setup` registers only the skills there.
  It accepts a hand-configured server in either OpenCode shape (v1
  `mcp.hivemind` with `enabled`, v2 `mcp.servers` with `disabled`). The
  plugin yields to an existing `hivemind` entry (a cloned project's config
  can define one), but when its URL differs from `HIVEMIND_MCP_URL` it
  prints one stderr warning per process naming both hosts, never the key.
  `KILO_CONFIG_CONTENT` is the
  highest-precedence inline config overlay. The incognito launcher's
  overlay sets both forms: on KiloCode 7.8.18 the v2 form alone left a
  hand-configured server of either shape on (checked live). Global
  config is `~/.config/kilo/`
  (`kilo.jsonc`, `AGENTS.md`), and it also reads `opencode.json(c)`
  config files. Verified against `Kilo-Org/kilocode` 7.8.12.
- **Cline** has no package plugin for the VS Code extension (the SDK
  plugin system runs in the CLI/SDK; the extension uses file hooks). It
  expands `${env:NAME}` in an MCP server's URL and headers from the
  process environment, and leaves an unset reference unexpanded (with a
  warning), so an incognito session with the variables unset never
  connects. The `cline` CLI (3.0.73, `@cline/core`) does neither: its MCP
  settings schema needs a real URL and takes headers as written, and it
  runs `UserPromptSubmit` without using its output (checked live against
  a mock model; `PostToolUse` and `TaskStart` work there). MCP tools appear
  as `<server>__<tool>` (`hivemind__hive_write`).
  Skills are SKILL.md folders in `~/.cline/skills/` (global) or
  `.cline/skills/`, `.clinerules/skills/`, `.claude/skills/`,
  `.agents/skills/` (project); the frontmatter `name` must match the
  folder name or the skill is dropped. File hooks are scripts named
  exactly after the hook type in `~/Documents/Cline/Hooks/` (global) or
  `.clinerules/hooks/` (project): no extension on Unix, `.ps1` on Windows
  (`.ps1` is ignored on Unix). A hook gets the payload JSON on stdin
  (top-level `taskId`; `postToolUse.toolName` the tool; a subagent carries a non-empty
  `parent_agent_id`, or `sessionContext.parentAgentId`) and prints
  `{ cancel, contextModification, errorMessage }` on stdout; hooks are
  enabled by default. `UserPromptSubmit` fires on every prompt: the
  first prompt of a task gets the full stay-aware line and later prompts a
  96-byte form (a sixth "full line sent" field in the per-task state file
  `TaskStart` resets and `PostToolUse` keeps; 3- and 5-field files from
  older releases still read). No hook signals a compaction (`PreCompact`
  output is not injected: it is wired as a deferred hook in the
  extension's hooks adapter), so the full line does not return after one. Verified against `cline/cline`
  main (extension source: `envExpansion.ts`, `skill-directories.ts`,
  `hooks-adapter.ts`, `disk.ts`, `name-transform.ts`).
- **Gemini CLI** installs an extension from a Git URL by cloning the whole
  repository and reading `gemini-extension.json`, `skills/` and
  `hooks/hooks.json` at its root (hence the root files; there is no
  option for a subfolder). It expands `${HIVEMIND_*}` from the launching
  environment in the server's URL and header, and `${extensionPath}` in
  hook commands. It turns off every MCP server in a folder the user has
  not trusted. Checked with Gemini CLI 0.63. In a trusted folder, with
  `experimental.extensionConfig` on (the default), it reads the
  project's top-level `.env` as the extension's workspace settings, and
  those take precedence over the process environment when it expands
  the extension's `${…}` (checked live with 0.65.0 and 0.66.0: a `.env`
  with `HIVEMIND_MCP_URL` got the bearer key). The extension cannot opt
  out, so the docs tell users to set `extensionConfig` to `false`, which
  makes the expansion read the process environment only. On a Windows
  checkout without symlink support the `skills` link is a plain file, so the
  skills would have to be copied by hand.

- **Antigravity** (checked live with the CLI, `agy` 1.5.3 and 1.5.5, headless
  against a mock Gemini model: `modelProvider: "gemini"` in
  `~/.gemini/antigravity-cli/settings.json`, `GEMINI_API_KEY` and
  `GOOGLE_GEMINI_BASE_URL`; no Google sign-in is needed there).
  - *Install.* `agy plugin install <github-url>/tree/<ref>/plugins/antigravity`
    clones the repository and installs that folder at that ref, into
    `~/.gemini/config/plugins/hivemind` (the App and the IDE read the same
    folder), copying the `skills` link as a real folder. Without the
    subfolder it installs the repository root, converting the Gemini CLI
    extension there, and from `plugins/hivemind` it converts the Claude
    Code plugin; both conversions keep the Claude and Gemini hook shapes,
    which Antigravity reads as two broken hook groups, so the docs name
    the full link. `agy plugin validate --strict plugins/antigravity`
    passes. On a Windows checkout without symlink support the `skills`
    link is a plain file, so install from a Linux or macOS clone or the
    GitHub link. Only github.com links work (1.5.5: any other host fails
    with "failed to parse GitHub URL", and a `git+` URL is taken as a
    folder); from another Git server the docs clone the tag and install
    the folder, which Antigravity copies. Neither route leaves a record of
    the address (`config.json` and `import_manifest.json` hold none), so
    the drift check relies on `~/.config/hivemind/plugin-source`.
  - *MCP server.* A server that declares `variables` installs the plugin
    turned off. Turning it on in `/plugin` (or the App) asks for each
    variable and stores the values in plain text in
    `~/.gemini/config/config.json` (mode 600) under
    `plugins.hivemind.mcp.hivemind_hivemind.variables`; `agy plugin enable`
    refuses until then. Reinstalling keeps them, uninstalling deletes
    them. The server is `hivemind_hivemind`. Its tools are lazy: the
    system prompt lists their names, the schemas and the server's
    `instructions` go to files under `~/.gemini/antigravity-cli/mcp/`,
    and the model calls `call_mcp_tool` with `ServerName`, `ToolName` and
    an `Arguments` object (a JSON string is sent as is and the server
    answers 400). So the server's instructions do not reach the model,
    which is why the plugin ships the rule.
  - *Workspace plugins.* Antigravity also loads plugins from a trusted
    workspace's `.agents/plugins/`, and fills one named `hivemind` with the
    stored variables: a test plugin there whose `serverUrl` pointed at a
    local listener received `Authorization: Bearer hm_…`. Nothing in the
    plugin can prevent that, so the README and the setup notes tell users
    to check that folder before trusting a project.
  - *Hooks.* `hooks.json` at the plugin root, one named group with flat
    handler arrays for `SessionStart` and `Stop`. Hooks run with the plugin
    root as working directory and get `PLUGIN_ROOT`, `PLUGIN_DATA` and
    `ANTIGRAVITY_CONVERSATION_ID`, but not the plugin's variables, so
    `HIVEMIND_API_KEY` is unset there; `--antigravity` makes both scripts
    skip that check (the plugin is off until the key is filled in). Input
    is camelCase (`conversationId`, `transcriptPath`). `SessionStart`
    takes `{"injectSteps":[{"systemMessage":{"systemMessage":...}}]}`,
    which reaches the model as a system message before the first turn.
    `Stop` takes `{"decision":"block","reason":...}` (the hooks reference
    says `"continue"`; 1.5.3 re-enters the loop with either) and sends the
    reason as a system message. Both fire in `agy -p`. There is no
    compaction event.
  - *Rule.* `rules/AGENTS.md` is put into every system prompt as a user
    rule while the plugin is on.
- **Junie** (JetBrains' coding agent; checked with Junie CLI 26.9.7 on
  Linux against a mock OpenAI-compatible model and a local
  `hivemind-mcp-http`).
  - *Install.* Junie reads Claude-style marketplaces, so the root
    `.claude-plugin/marketplace.json` works unchanged: in the TUI,
    `/extensions marketplace add <git-url-or-path>` (an https GitHub URL,
    `owner/repo`, SSH, other git hosts, a local path or a direct
    `marketplace.json` URL are accepted), then `/extensions`, search
    `hivemind`, Install, "User level". It lists the Features as "skills,
    mcp, hooks" and copies the plugin to
    `~/.junie/extensions/<marketplace>/hivemind/`. Checked with a local
    path; GitHub URLs are documented but were not tried. A Git extension
    updates only when the user picks Update in the Installed tab; a
    local-directory one refreshes on start. No Junie manifest is needed.
  - *MCP server.* The user config is `~/.junie/mcp/mcp.json` (a project
    `.junie/mcp/mcp.json` must never hold the key). **Junie expands no
    `${VAR}` in `url` or `headers`**: a `${HIVEMIND_MCP_URL}` url went to
    localhost:80 and a `Bearer ${HIVEMIND_API_KEY}` header got 401. So the
    file holds the literal URL and key, mode 600 in a 700 folder, and the
    setup skill writes it with the key-hygiene rules (it shows the change
    first and prefers that the user writes the key). The server must be
    named `hivemind`. When a user `hivemind` entry exists the extension's
    own `.mcp.json` is shown as "mcp (ignored due to conflict)"; without
    one it tries localhost:80 and fails harmlessly (no key sent). The
    model sees `mcp_hivemind_hive_whoami`; Junie's UI shows
    `hivemind/hive_whoami`.
  - *Capability filter.* Before each step Junie makes a separate model
    call that picks which MCP tools the main agent gets for the task; a
    tool it leaves out is not offered at all. The filter follows the
    capability instructions in the guidelines, and gets the extension's
    guidelines. So `guidelines/hivemind.md` ends with "Required MCP server:
    hivemind. Always select all of its hive_* tools for every task."
  - *Guidelines.* An extension may ship a `guidelines/` folder: its files
    go into the main agent's guidelines and into the capability filter's
    prompt. This is the always-on layer for the stay-aware text, because
    Junie ignores `additionalContext` from `SessionStart` (the hook runs,
    its text never reaches the model). `hivemind-session-start.sh` is left
    as it is. The file is `STAY_AWARE_CORE` plus the filter sentence, and
    the reminder tests check it byte for byte.
  - *Hooks.* Junie runs the extension's `hooks/hooks.json` (Claude
    format, `${CLAUDE_PLUGIN_ROOT}` expanded). `Stop` supports
    `{"decision":"block","reason":"..."}` (a retry with the reason as
    feedback) but does not run for chat-type tasks. Junie sets no
    `JUNIE_*` or `CLAUDE_*` variable in the hook's environment.
  - *Stop input.* No `transcript_path`: `{"hook_event_name":"Stop",
    "session_id":"session-261108-043543-1i6y","cwd":…,"project_path":…,
    "stop_hook_active":false,"last_assistant_message":…}`. The session log
    is `$HOME/.junie/sessions/<session_id>/events.jsonl`; each tool use is
    a line with `"kind":"<X>BlockUpdatedEvent"` (X seen: `Mcp`,
    `Terminal`) and `"status":"IN_PROGRESS"`, later again with
    `"status":"COMPLETED"`; a final `ResultBlockUpdatedEvent` has no
    status. An MCP call carries `"toolName":"hivemind/hive_whoami"`.
  - *Incognito.* `junie --mcp-default-locations=false` skips both
    `mcp.json` files, so the extension's `.mcp.json` tries
    `${HIVEMIND_MCP_URL}` literally and fails to connect, and no hive tools
    load (the user's other MCP servers go too).
    `--ide-guidelines=<file>` prepends the file's content to the
    guidelines.
  - *Not verified.* The Junie plugin inside JetBrains IDEs (it has an MCP
    settings page in the same JSON format and reads `.junie/AGENTS.md`;
    whether it loads CLI extensions is unknown) and the JetBrains AI
    Assistant chat (MCP under Settings | Tools | AI Assistant | Model
    Context Protocol, plus project rules; no plugins or hooks).
  - *Mock model for headless runs.* A custom profile
    `~/.junie/models/<id>.json` with `baseUrl` (the full endpoint, e.g.
    `.../v1/chat/completions`), `apiType` (`OpenAICompletion`,
    `OpenAIResponses`, ...), `apiKey` and `id`; then `junie --model
    custom:<id> --output-format json "task"`. The model ends a task with
    the `submit` tool. Junie's JVM reads `user.home`, which normally equals
    `$HOME`.

- **GitHub Copilot CLI** (checked with 1.0.95, headless against a mock
  OpenAI-compatible model: `COPILOT_PROVIDER_BASE_URL`, `COPILOT_MODEL`,
  `COPILOT_PROVIDER_API_KEY` and `copilot -p ... --allow-all-tools`; no
  GitHub login is needed there).
  - *Install.* `copilot plugin marketplace add <repo-url>` reads the
    root `.claude-plugin/marketplace.json`, and `copilot plugin install
    hivemind@hivemind` installs `plugins/hivemind` through
    `.claude-plugin/plugin.json`: the skills, and the hooks from
    `hooks/hooks.json` (Claude-style `SessionStart` and `Stop`; Copilot
    sets `CLAUDE_PLUGIN_ROOT`, `COPILOT_PLUGIN_ROOT` and `PLUGIN_ROOT`
    for them). A local-path marketplace is loaded live, not copied.
    `copilot plugin install <owner>/<repo>` fails (no `plugin.json` at
    the root), `<owner>/<repo>:plugins/hivemind` works but Copilot warns
    that direct installs are deprecated, so the docs name the marketplace.
  - *MCP server.* Copilot validates a server's `url` as a URL before
    expanding variables: `"${HIVEMIND_MCP_URL}"`, `$HIVEMIND_MCP_URL`,
    `${env:...}` and `${SCHEME}://host` are all "Invalid url", and the
    rest of that file's servers go with it. A variable inside the host or
    path (`http://${HOST}/mcp`) and `${VAR}` in `headers` do expand. So
    the shared `.mcp.json` (Claude Code, Oh My Pi) is skipped with a
    warning in Copilot's log, and the server comes from the user's own
    `~/.copilot/mcp-config.json` entry (`copilot mcp add` stores the
    literal `${HIVEMIND_API_KEY}`), which the setup skill writes. Tools
    appear as `<server>-hive_*`. A server's own `instructions` reach the
    system prompt only for allowlisted servers
    (`--allow-all-mcp-server-instructions`), so the skill and the
    reminders carry the rules.
  - *SessionStart.* Copilot reads only a top-level `additionalContext`
    in the hook's JSON and drops `hookSpecificOutput`, so
    `hivemind-session-start.sh` prints the flat form when `COPILOT_CLI`
    is set (Copilot sets it for hooks). The text arrives as its own user
    message before the first prompt. `source` is `new`, `resume` or
    `startup`: no compaction source, hence no post-compaction line, and
    nothing re-injects the text after a compaction.
  - *Stop.* The hook gets `transcript_path` (the session's
    `events.jsonl`). In `copilot -p` it runs before the session's tool
    calls are written there (checked with 1.0.104 and 1.0.105: three lines
    and no `tool.execution_start` at hook time), so the stop reminder never
    fires headless. The interactive check was with 1.0.95.
  - *Skills.* Copilot parses `SKILL.md` frontmatter with a strict YAML
    parser: an unquoted `: ` in `description` skipped the `hivemind` skill
    entirely until the fix after 2.7.0 (`copilot skill list` reports
    "failed to parse YAML frontmatter"). `test_skill_descriptions_are_strict_yaml`
    pins it.

- **Cursor** (checked live with the CLI 2026.11.05, signed in with a free
  account and in local-model mode: `cursor-agent-local --authless` from
  `downloads.cursor.com/lab/<version>/linux/x64/agent-cli-local-package.tar.gz`
  with `CURSOR_LOCAL_AGENT_BASE_URL` and `CURSOR_LOCAL_AGENT_API_KEY`
  pointing at the mock model. Signing in needs `cursor.com` and
  `*.cursor.sh`. The editor was not checked. Cursor's session log, with a
  `cli.hook.executed` event per hook run, is under
  `$TMPDIR/cursor-agent-logs-<uid>/`).
  - *Install.* Signed in, Cursor loads `~/.cursor/plugins/local/<name>/`
    and rejects a symlink whose target is outside that folder, so the
    clone goes in `~/.cursor/plugins/local/.hivemind-src` (dot folders are
    skipped) with a relative link `hivemind -> .hivemind-src/plugins/hivemind`.
    The authless profile turns user-local plugins (and `--plugin-dir`)
    off and the local marketplace file on:
    `~/.cursor/plugins/local-marketplaces.json`
    (`{"version":1,"marketplaces":[{"name","gitUrl","gitRef","plugins":[{"name","enabled":true}]}]}`),
    cloned at each start and cached by commit under
    `~/.cursor/plugins/cache/<marketplace>/<plugin>/<commit>/` (a plain
    copy, not a clone; a new `gitRef` replaces it, checked with
    2026.11.11), so the drift check reads the address from that file.
    Signed in, that file is ignored. The root
    `.cursor-plugin/marketplace.json` (`name`, `owner`, `plugins[].source`
    as a path) serves both it and team marketplaces; Cursor looks for
    `<source>/.cursor-plugin/plugin.json`, then `.claude-plugin/plugin.json`.
    A public Marketplace listing is reviewed and Hivemind has none.
  - *Manifest.* A path field replaces folder discovery for that
    component. Cursor would parse `hooks/hooks.json` by default (it
    converts the Claude format), so the manifest sets `hooks` to
    `./cursor/hooks.json`, `rules` to `./cursor/rules/` and `skills` to
    `./skills/`. There is no `mcp.json`.
  - *MCP server.* The user's `~/.cursor/mcp.json` entry reads
    `${env:HIVEMIND_MCP_URL}` and `${env:HIVEMIND_API_KEY}`; the CLI
    expands both (checked live). Tools are called through a meta tool:
    `CallDynamicTool` `{namespace, toolName, arguments}` signed in,
    `CallMcpTool` `{server, toolName, arguments}` in local-model mode.
    Not confirmed: whether the app, started from a dock, sees shell
    variables (probably not, so users start it with `cursor .`).
  - *SessionStart.* Cursor's `sessionStart` takes
    `{"additional_context": "..."}`; `hivemind-session-start.sh` prints
    that when the plugin's hook passes `--cursor`. It does not go by
    `CURSOR_VERSION` (set for every Cursor hook), because Cursor's
    variables may reach a Claude Code started in Cursor's terminal.
    Checked live: the text arrives in the first user message inside
    `<hooks_context>`. The hook input has `transcript_path: null`. There
    is no compaction source, so the always-on rule is the layer that
    survives a compaction.
  - *Rules.* The plugin's rule (through the `rules` field or the default
    `rules/` folder) never reached the model, signed in or not, while a
    user rule in `~/.cursor/rules/` did, so setup writes
    `~/.cursor/rules/hivemind.mdc`. The plugin's rule stays in case the
    editor or a later version loads it.
  - *Stop.* A plugin `stop` hook never ran (no `cli.hook.executed` event),
    so the plugin has none. A user `stop` hook in `~/.cursor/hooks.json`
    runs in an interactive session when signed in (not with `-p`, not in
    local-model mode), with `transcript_path`
    (`~/.cursor/projects/<slug>/agent-transcripts/<id>/<id>.jsonl`, Claude-like
    `tool_use` records) and `loop_count`. Setup points it at
    `hivemind-stop.sh --cursor` in the local plugin folder, which answers
    `{"followup_message": ...}`; Cursor sends that as the next message and
    the following stop has `loop_count: 1`, which the script skips.

## The Stop hook and the post-compaction line

Two nudges that get knowledge written before it is lost: in Claude Code
and Codex from `hooks/hooks.json`, in Pi and Oh My Pi from the extension
and the OpenCode plugin (which KiloCode runs as well), in DeepSeek
Harness the Stop reminder from `dsh/hivemind-reminders.js` (below). Gemini CLI gets the Stop reminder
from the extension's `AfterAgent` hook (`../../hooks/hooks.json`).
Antigravity gets the Stop reminder from `../antigravity/hooks.json`,
and no post-compaction line.
Hermes does not have them. Cursor gets the Stop reminder from a user `stop` hook that setup adds (its plugin hooks never see a stop), and no post-compaction line. Junie gets the Stop reminder from the same plugin hook. GitHub Copilot CLI gets the Stop reminder from
the plugin's `Stop` hook (the shared `hooks/hooks.json`), and no
post-compaction line. Cline gets the end-of-task nudge from the
`PostToolUse` hook in `cline/` (it counts the task's tool calls in a temp
file, reset by `TaskStart`, and never nudges a subagent task); it has no
post-compaction line.

The script counts seven transcript formats:

- Claude Code's `"type": "tool_use"` records.
- Codex's `function_call`, `custom_tool_call` and `local_shell_call`
  items. Checked with Codex 0.162.1 against a mock model: it blocks once,
  Codex continues with the reason as the next prompt, and an MCP
  `hive_write`, recorded as `"name":"hive_write","namespace":"mcp__hivemind"`,
  keeps it quiet. Codex runs plugin hooks only after the user trusts them
  in `/hooks`. Codex 0.163.0 writes the same transcript format and
  `hive_write` record (its hooks were not re-run).
- The distinct `functionResponse` ids in Gemini CLI's JSONL chat log,
  which rewrites a call's record as it progresses. Checked with Gemini CLI
  0.64.0 against a mock model: `SessionStart` sends the reminder,
  `AfterAgent` blocks once and takes the same `block` output
  and sends the reason as the next prompt, and `mcp_hivemind_hive_write`
  keeps it quiet. Gemini's `SessionStart` has no compaction source, so it
  gets no post-compaction line.

- Antigravity's `transcript_full.jsonl`, one line per step, with a step's
  calls in `"tool_calls":[{"name":...,"args":...}]`; a lazily loaded
  Hivemind tool shows as `"name":"call_mcp_tool"` with
  `"ToolName":"hive_write"`, so the checks also match `ToolName`. Checked
  with 1.5.3 against a mock model: the hook blocks once, and three
  `hive_feedback` calls keep it quiet.

- Copilot CLI's `events.jsonl`, with one `tool.execution_start` event per
  call; a Hivemind write shows as `"name":"hivemind-hive_write"`.
  Checked with Copilot CLI 1.0.98 against a mock model (`SessionStart`
  sends a flat `additionalContext`; `Stop` input: `session_id`,
  `transcript_path`, `stop_hook_active`, as for Claude
  Code): the hook blocks once, the reason becomes the next prompt, and a
  `hive_write` keeps it quiet. Copilot writes `events.jsonl` a moment
  after the events happen (it was not there yet when a sub-second session
  stopped), so the count can trail by a call or two: fine for a 20-call
  threshold, and the script fails open when the file is missing.

- Junie's `~/.junie/sessions/<session_id>/events.jsonl`. Its `Stop` input
  has no `transcript_path` (see the Junie quirk), so the script, seeing a
  `project_path` field and no `transcript_path`, reads that file (refusing
  one that is not a regular file) and counts the lines whose `kind` is
  `<X>BlockUpdatedEvent` and whose `status` is `COMPLETED` (each tool use
  logs an `IN_PROGRESS` and a `COMPLETED` line). A Hivemind call shows as
  `"toolName":"hivemind/hive_write"`, so `hive_write`, `hive_feedback`,
  `hive_search` and `hive_get` are matched on `toolName`. Checked with
  Junie CLI 26.9.7. `SessionStart` runs but Junie ignores its output.

- Cursor's `agent-transcripts/<id>/<id>.jsonl`, counted as Claude Code's
  `"type": "tool_use"` records. With `--cursor`, a Hivemind call is a
  `CallDynamicTool` or `CallMcpTool` record whose `input` has
  `"toolName":"hive_write"` (and so on), so only those records count as
  Hivemind calls (a `GetDynamicTools` lookup names a tool too, and does
  not). A stop with `loop_count` above 0 is skipped. Checked with Cursor
  CLI 2026.11.05 signed in: the reminder arrives once as the next
  message, and the agent answers it.

- **Post-compaction line.** When the `SessionStart` source is `compact`
  (read from the hook's stdin JSON), `hivemind-session-start.sh` appends
  one sentence asking the agent to write what it learned before the
  compaction and rate the entries its work tested. Other sources and the
  incognito text are unchanged. It is not part of `STAY_AWARE_CORE`, so
  the always-on byte budget does not count it.
- **Stop hook.** `hivemind-stop.sh` blocks a stop once per session with a
  two-sentence reason (`{"decision":"block","reason":...}` on stdout,
  exit 0, as the Claude Code hooks reference documents; input fields
  `session_id`, `transcript_path`, `stop_hook_active`). It exits 0 with no
  output, so the stop goes through, unless all of these hold:
  1. not incognito (same test as the session-start script);
  2. `HIVEMIND_API_KEY` is set (same test as the session-start script;
     skipped with `--antigravity`, whose hooks never see the key);
  3. `stop_hook_active` is not true;
  4. no marker for this `session_id` in `$XDG_RUNTIME_DIR/hivemind-stop-reminder/`
     (when that variable names a real directory the user owns), else
     `${TMPDIR:-/tmp}/hivemind-stop-reminder-<uid>/` (written only when it
     blocks, so it never fires twice). The folder is private: created 0700,
     and refused (no reminder) unless it is a real directory owned by the
     user and not group or other writable;
  5. the transcript (grep only, no jq, python or node) shows no
     `hive_feedback` call, and either no `hive_write` call or a
     `hive_search` or `hive_get` call (a session that wrote but read
     entries and rated none is still asked to rate), and at least 20 tool uses
     (`HIVEMIND_STOP_MIN_TOOLS` overrides the 20). Lines that carry tool
     definitions (Claude Code's `prompt_snapshot` records list every tool
     with its schema) are skipped, so a definition never counts as a call.

  Any error, missing file or unparsable input fails open. Tests:
  `tests/unit/test_stop_hook.py`.
- **Pi and Oh My Pi.** The extension (`extensions/hivemind.ts`) gives the
  same two nudges with the same texts, under the same conditions (not
  incognito, a key set, `HIVEMIND_STOP_MIN_TOOLS`), counting the calls
  the model made in `tool_execution_start` (a call a script made for it,
  with a `parentToolCallId`, is not counted) and noting a `hive_write`,
  `hive_feedback`, `hive_search` or `hive_get` by any route (named tool, a
  codemode script's call, an Oh My Pi `xd://` route). Once per session (reset on
  `session_start`), Pi's `agent_before_settle` appends the reminder as a
  visible `custom_message` and asks for one more model request; Oh My
  Pi's `session_stop` returns the Claude Code-style block. After
  `session_compact` the next system-prompt section carries the
  post-compaction sentence once. Checked live with Pi 1.1.0 and Oh My Pi
  18.9.1 against a mock model: three shell calls get the reminder as the
  next prompt, and a `hive_write` (Pi `mcp__hivemind__hive_write`, Oh My
  Pi `xd://mcp__hivemind_hivemind_hive_write`) keeps it quiet. The
  compaction sentence is tested only in `tests/unit/test_stop_hook.py`.
- **OpenCode.** OpenCode has no hook that can hold a stop, so the plugin
  (`opencode/hivemind.js`) counts each session's calls in
  `tool.execute.after` (MCP tools are named `hivemind_hive_write`) and, on
  the `session.idle` event of a long top-level session that wrote
  nothing, or read entries and rated none, sends the reminder once with `client.session.promptAsync`,
  using the agent and model of the session's last message (from
  `chat.message`). A subagent's session (one with a `parentID`) is left
  alone. After `session.compacted`, the next
  `experimental.chat.system.transform` for that session adds the
  post-compaction sentence once. Checked live with OpenCode 1.19.0
  (`opencode serve`, which the TUI also uses) against a mock model: three
  shell calls get the reminder as the next prompt, and a
  `hivemind_hive_write` keeps it quiet. `opencode run` exits at the first
  idle, so the reminder never runs there.
- **DeepSeek Harness.** The bundle's `dsh/hivemind-reminders.js` counts
  the main agent's calls in `tools/post-execute` and, in
  `agent/turn-stopping` of a long turn that wrote nothing (or read entries
  and rated none), steers the agent
  once with the reminder as a user message (as DSH's own Claude Code hooks
  bridge does with a `Stop` block). It stays silent while
  `mcp__hivemind__hive_write` is not visible to the agent, which covers
  incognito and an unset URL (the server row is off then); agents
  announced by `subagent/start` are neither counted nor reminded. No
  post-compaction sentence: DSH keeps the server's instructions in the
  system prompt, and compaction is a session event, not a plugin hook.
  DSH's Claude Code hooks bridge (not installed by default
  since 0.2.1) cannot run `hivemind-stop.sh` instead: it passes an empty
  `transcript_path`. Checked live with DSH 0.2.0-rc.2
  and 0.2.1-alpha.2 (`dsh headless`) against a mock model: three shell
  calls get the reminder once, and a `hive_write`, incognito or a
  subagent's calls keep it quiet.

## Incognito

An incognito session has Hivemind completely off, and the server never
learns the session happened (ADRs 0003, 0035). `bin/hivemind-incognito`
does two things:

1. Sets `HIVEMIND_INCOGNITO=1`, so the reminders switch to their incognito
   text: don't use or mention Hivemind, and mark any local notes
   `[hivemind: incognito, never upload]` so no later session uploads them.
2. Adds the harness's own switch, so the Hivemind tools do not load:

| Harness | Switch | Needs |
|---|---|---|
| Claude Code | `--settings '{"deniedMcpServers":[…]}'` for this session, naming the server as `hivemind` and as the plugin-scoped `plugin:hivemind:hivemind`, and its URL (from the shell or the `env` block of `~/.claude/settings.json`); the launcher also unsets the key and URL. The scoped name or the URL is what blocks the plugin's server; `hivemind` alone covers only a hand-added one | nothing |
| Codex | `-c mcp_servers.hivemind.enabled=false`; the launcher also unsets the key | the `mcp_servers.hivemind` entry in `~/.codex/config.toml` or `.codex/config.toml` |
| DeepSeek Harness | the bundle's server row switches itself off | nothing |
| Hermes | `HIVEMIND_ENABLED=false` | `enabled: ${HIVEMIND_ENABLED}` in the hivemind server entry in `~/.hermes/config.yaml`, and **no** `HIVEMIND_ENABLED` in `~/.hermes/.env` (Hermes loads it over the environment, so it would override the launcher); for normal sessions leave it unset or export `HIVEMIND_ENABLED=true` in the shell profile |
| Pi | the package registers no server, and the key and URL are unset; a hand-written `mcp.json` entry that reads `${HIVEMIND_MCP_URL}` has no URL. The extension also blocks `mcp__hivemind…` calls (codemode scripts included) and the Oh My Pi `xd://` routes | the hivemind package |
| Oh My Pi | `HIVEMIND_API_KEY` unset and `HIVEMIND_MCP_URL=off` (not unset: Oh My Pi runs on Bun, which fills an unset or empty variable from the project's `.env`), so the server is never contacted (Oh My Pi warns once that it is unavailable); the extension also blocks any Hivemind call | the hivemind plugin |
| GitHub Copilot CLI | `--disable-mcp-server hivemind` for the session (it never contacts the server); the launcher also unsets the key and URL | the server named `hivemind` in `~/.copilot/mcp-config.json` |
| Cursor | no per-session MCP switch: `HIVEMIND_MCP_URL`/`HIVEMIND_API_KEY` unset, so the `${env:…}` references in `~/.cursor/mcp.json` resolve empty and the server cannot authenticate; the hook prints the incognito line in Cursor's JSON shape, and the always-on rule still loads (the launcher prints a note) | the server entry with `${env:…}` references |
| Gemini CLI | `HIVEMIND_MCP_URL`/`HIVEMIND_API_KEY` set empty (not unset: Gemini fills unset variables from the project's `.env`), so the extension's server is never contacted (Gemini notes an MCP issue) | the hivemind extension, no hand entry in `settings.json`, and `"experimental": {"extensionConfig": false}` (otherwise a trusted project's `.env` still wins) |
| OpenCode | the plugin skips the server; a hand-configured one is disabled with `OPENCODE_CONFIG_CONTENT`, or, when that variable is already set (never merged in sh), the key and URL are unset and a note is printed | nothing |
| KiloCode | the plugin (the OpenCode one) skips the server; a hand-configured one is disabled with `KILO_CONFIG_CONTENT` (`mcp.hivemind.enabled: false` and `mcp.servers.hivemind.disabled`), or, when that variable is already set, the key and URL are unset and a note is printed | nothing |
| Antigravity (`agy`) | `--settings '{"permissions":{"deny":["mcp(hivemind_hivemind/*)"]}}'`, a session-only rule that refuses every Hivemind call (checked with 1.5.3: six `call_mcp_tool` calls refused, no stop reminder). The server still connects at startup with the stored key, so the server sees a connection; Antigravity has no per-session switch for one plugin | the hivemind plugin |
| Junie | `--mcp-default-locations=false` (skips the user's and the project's `mcp.json`, so the hand `hivemind` entry is not read; the extension's own `.mcp.json` then tries `${HIVEMIND_MCP_URL}` literally and cannot connect) and `--ide-guidelines=<plugin>/junie/incognito.md`; the launcher also unsets the key and URL. The flag also turns off the user's other MCP servers for the session | the plugin installed, the server in `~/.junie/mcp/mcp.json` |
| Cline | `HIVEMIND_MCP_URL`/`HIVEMIND_API_KEY` unset, so the `${env:…}` references in `cline_mcp_settings.json` stay unexpanded and the server never connects; the hooks see `HIVEMIND_INCOGNITO` and say the incognito line (the skills stay installed) | the server entry with `${env:…}` references |

Where the switch is missing or not configured (the Hermes entry without
`${HIVEMIND_ENABLED}`, a Codex without the entry, a hand-configured Gemini CLI, a Cline
whose server entry does not use `${env:…}`, a Cursor server configured another way, any other
command), the session is incognito **by restraint**: the tools are
loaded, the key and URL stay in the environment, and an agent with a
shell could still call the REST API. The launcher unsets the key and URL
only where the server is kept from loading (Claude Code, Copilot CLI,
DeepSeek Harness, Junie, Pi, Oh My Pi, Gemini CLI, Cline; for Cursor, where unsetting them is the whole switch and the session is incognito by restraint; for Codex, the key only; for OpenCode and KiloCode only when their config overlay variable is already set),
because the other harnesses may still need them (ADR 0035). A DeepSeek Harness session also stays incognito when a
project `.env` names `HIVEMIND_*` variables: incognito is on if the
process environment or `~/.dsh/.env` says so, and no project `.env` can
turn it off. It fails closed: a project `.env` that names
`HIVEMIND_INCOGNITO` at all, even as `0`, makes the session incognito. A key set in the `env` block of `~/.claude/settings.json` is
applied by Claude Code itself, so it stays visible to an incognito
session's shell.

Setting only `HIVEMIND_INCOGNITO=1`, or turning incognito on during a
session, is incognito by restraint, and the agent says so. A session
started incognito cannot turn Hivemind back on.

Copilot CLI's switch was checked live with 1.0.95: with it the model sees no
`hivemind-hive_*` tool, the Hivemind server gets no request, the startup
hook prints the incognito text and the stop hook stays silent.

The Claude Code switch was checked live with Claude Code 2.1.287 against
a local listener: denying `plugin:hivemind:hivemind` or the URL stopped
every request to the endpoint, while denying `hivemind` alone did not.

## Key profiles

`bin/hivemind-as <profile> <harness>` starts a harness as one of the
user's agents (ADR 0150). Per harness:

| Harness | How the profile reaches it |
|---|---|
| Claude Code | `--settings ~/.config/hivemind/claude.<profile>.json` (an `env` block). An `env` key in `~/.claude/settings.json` beats the launching shell, and `--settings` beats both and a project's settings (checked with 2.1.323) |
| Hermes | `-p <profile>`: Hermes sets `HERMES_HOME` to that profile and loads its `.env` over the environment; `default` passes `-p default`, which overrides a saved `hermes profile use` |
| Junie, Antigravity | refused: the key lives in their own config, so a switch is persistent (the skill has the steps) |
| DeepSeek Harness | the key file is exported, except in a folder whose `.env` names `HIVEMIND_*`: the bundle then reads `~/.dsh/.env` only, so any profile but `default` is refused |
| every other harness, and `code`/`codium` for the editor extensions | `~/.config/hivemind/<stem>.<profile>.env` sourced and both variables exported (`cursor-agent` uses `cursor`, `kilo` uses `kilocode`) |

The launcher unsets the inherited key and URL before loading a profile,
fails when the file leaves either unset, refuses a file other users can
read, and sets `HIVEMIND_PROFILE`. `default` loads `<stem>.env` when it
exists and otherwise changes nothing. `auto` takes the profile from the
user's `~/.config/hivemind/profiles` (longest folder that holds the
current one), never from the project.

## The drift check

`hivemind-drift.sh` compares what is installed with the latest release
and changes nothing. By default it reads the newest `vX.Y.Z` tag with
`git ls-remote`, makes a shallow clone of it into a temporary folder
(`--keep DIR` keeps it, `--latest DIR` uses a checkout instead) and
removes the clone at exit. It never runs anything from the clone, and
it uses only git, so any git server or path works. The repository is
`--repo`; else, with `--source-from FILE`, the first git address in
that file whose repository name contains `hivemind` and that git can
reach when there are several (a harness's config can also name the
Hivemind MCP server), dropping a `git+`
or `git:` prefix, a `#fragment` and an `@ref`, so OpenCode and KiloCode
plugin entries work (each harness's update file names its record, where
it was checked; a record with no such address, such as a GitHub
`owner/name` shorthand, is named on the `Latest:` line and the search
goes on); else the `origin` (or first) remote of the clone that holds
the installed plugin, but only when that clone is the Hivemind
repository (a plugin inside a dotfiles repository does not count); else,
for a cached copy at `<plugins>/cache/<marketplace>/<plugin>/<version>`,
the remote of the clone at `<plugins>/marketplaces/<marketplace>` (Claude
Code's layout, checked live with a Git-URL marketplace), with the same
check; else the first non-comment line of `${XDG_CONFIG_HOME:-~/.config}/hivemind/plugin-source`;
else the public GitHub URL. The `Latest:` line names the source and why,
with any `user:password@` removed from the URL, and git's own error
output is dropped for the same reason. The address always follows `--`
on git's command line, so one that starts with `-` is never read as an
option. It reports:

- the installed plugin: `--installed`, else the one around the script
  (three folders up, when that folder has a manifest with a version).
  Its version against the release's, then each skill folder and the
  always-on files it has (`guidelines/hivemind.md`,
  `cursor/rules/hivemind.mdc`, Antigravity's `rules/AGENTS.md`) against
  the release's;
- `--block FILE`: the marked block in that file against the block in
  step 4 of the release's setup skill, ignoring carriage returns and
  trailing blanks, with a unified diff when they differ. No block is
  reported but is not drift: whether one belongs there depends on the
  harness;
- `--skills DIR` and `--copy FILE=PATH`: hand-copied skill folders and
  files (Cline's hooks) against the release's.

Exit status 0 means no drift, 1 drift, 2 that it could not compare.
`--print-block` prints the release's block for the agent to write. It
needs `sh`, `git`, `diff`, `cmp`, `sed` and `awk`; native Windows without
Git Bash compares by hand. Tests: `tests/unit/test_drift_check.py`
(a local checkout and a local git repository stand in for the release).

## Tests

`tests/unit/test_agent_plugin.py` keeps the plugin, the server and the
docs in step. Among other things it checks that:

- every MCP tool is named in the `hivemind` skill, which stays under 10 KB;
- all manifests agree on the name and version;
- every harness has a reference file routed from the setup skill, an
  update file routed from the update skill, and a mention in README.md,
  and both skills stay harness-neutral;
- the hook, the Hermes section, the Pi extension, the OpenCode plugin and
  the DSH row all switch to incognito, and the launcher applies each
  harness's switch;
- every copy of each reminder text (startup, incognito, stop, compaction,
  configured, not configured and Cline's short line) matches one canonical
  text, read as its own runtime would. To change a reminder, change the
  canonical text in `REMINDER_COPIES` and every file it lists;
- CHANGELOG.md has a row for the current version, and README.md links it.

Run it with `uv run pytest tests/unit/test_agent_plugin.py`. Some tests
need `node` or `python3` and skip without them.

`make hermes-scan` runs Hermes's own install scan over the plugin and
fails when Hermes would refuse the install even with `--force`; CI runs it
on every pull request. One line in `references/junie.md` did that from
2.19.0 to 2.22.0 and nothing noticed. The scan fetches Hermes at the
commit pinned in the Makefile (`HERMES_REF`); move the pin to each new
Hermes release, since a new rule can block an unchanged plugin.

## Releasing

A release bumps the version in every manifest and adds a row to
[CHANGELOG.md](CHANGELOG.md) saying what changed on the server, in the
plugin, and in copies users made themselves. The full release steps are
in the repository's [AGENTS.md](../../AGENTS.md).
