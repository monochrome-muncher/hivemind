# Developing the Hivemind agent plugin

This file is for people and agents who change the plugin. If you only want
to install or use it, read [README.md](README.md) instead.

## Contents

- [What is in the plugin](#what-is-in-the-plugin)
- [How each harness gets each piece](#how-each-harness-gets-each-piece)
- [Harness quirks](#harness-quirks)
- [Incognito](#incognito)
- [Tests](#tests)
- [Releasing](#releasing)

## What is in the plugin

| Part | What it does |
|---|---|
| `skills/hivemind/` | The always-on rules: `hive_whoami` first, when to search, when and how to write, what never to write, local memory only as a fallback |
| `skills/hivemind-setup/` | The guided setup. Harness-neutral itself: it works out its harness first (never from the model), then reads only that harness's file in `references/` |
| `skills/hivemind-setup/scripts/hivemind-session-start.sh` | The reminder text the startup hook prints, including its incognito variant |
| `hooks/hooks.json` | A `SessionStart` hook (`startup`, `resume`, `clear`, `compact`, `fork`) that re-injects the reminder whenever the context is rebuilt (Claude Code, Codex), and a `Stop` hook (Claude Code, Codex). GitHub Copilot CLI reads the same file (its Claude-style hooks format) |
| `skills/hivemind-setup/scripts/hivemind-stop.sh` | The `Stop` hook ([below](#the-stop-hook-and-the-post-compaction-line)) |
| `.mcp.json` | The MCP server for Claude Code, built from `HIVEMIND_MCP_URL` and `HIVEMIND_API_KEY`. GitHub Copilot CLI rejects it (see its quirk below) |
| `package.json`, `cordis.patch.yml`, `dsh/` | The DeepSeek Harness bundle: the MCP server row, a small provider (`dsh/hivemind-skills.js`) that serves `skills/`, and the Stop reminder (`dsh/hivemind-reminders.js`) |
| `plugin.yaml`, `__init__.py` | The Hermes plugin: serves `skills/` and registers a system-prompt section |
| `extensions/hivemind.ts` | The Pi and Oh My Pi extension: the system-prompt section, the MCP server on Pi 0.99+, and blocking Hivemind calls in incognito sessions |
| `opencode/hivemind.js` | The OpenCode plugin (which KiloCode 7.x, an OpenCode fork, loads too): registers the MCP server, adds the skills, and puts the reminder in every model request |
| `cline/` | The Cline hooks: `UserPromptSubmit` (the stay-aware line on every prompt), `TaskStart` and `PostToolUse` (the end-of-task write-and-rate nudge), each with a `.ps1` twin for Windows |
| `bin/hivemind-incognito` | The incognito launcher ([below](#incognito)) |
| `CHANGELOG.md` | What changed for agents in each release |
| `../../package.json` (repository root) | Same package name as `plugins/hivemind/package.json`. Lets DeepSeek Harness, Pi and OpenCode install the plugin from the repository's Git URL (OpenCode loads `exports["./server"]`, else `main`) |
| `../../gemini-extension.json`, `../../skills`, `../../hooks/hooks.json` (repository root) | The Gemini CLI extension, installed with `gemini extensions install <git-url>`: Gemini reads these three at the root of what it installs. `skills` is a link to `skills/` here; the hooks run the two scripts above as `SessionStart` and `AfterAgent` hooks |
| `../../.claude-plugin/`, `../../.agents/plugins/` (repository root) | The marketplaces Claude Code, Codex and Oh My Pi install from. GitHub Copilot CLI reads the root `.claude-plugin/marketplace.json` and the plugin's `.claude-plugin/plugin.json` too, so it needs no manifest of its own |

## How each harness gets each piece

Every harness needs three things: the MCP server, the two skills, and a
reminder that survives compaction and new sessions.

| Harness | MCP server | Skills | Reminder |
|---|---|---|---|
| Claude Code | `.mcp.json` in the plugin | the plugin | `SessionStart` hook |
| Codex | the user's `~/.codex/config.toml` (plugin MCP config cannot read the URL and key from the environment) | the plugin | `SessionStart` hook, which the user must trust in `/hooks` |
| DeepSeek Harness | the bundle's row in `cordis.patch.yml` | `dsh/hivemind-skills.js` | none needed: DSH keeps the server's instructions in the system prompt |
| Hermes | the user's `~/.hermes/config.yaml` | the plugin | the plugin's system-prompt section (Hermes 0.21.5+), else the `SOUL.md` instruction block (see below) |
| Pi | the extension registers it with Pi 0.99+ built-in MCP; with pi-mcp-adapter, the adapter's config | the package | the extension's system-prompt section |
| Oh My Pi | the plugin's `.mcp.json` (server `hivemind:hivemind`; a hand `hivemind` entry in `~/.omp/agent/mcp.json` replaces it) | the plugin | the extension's system-prompt section |
| OpenCode | `opencode/hivemind.js` (skipped if a hand-configured `hivemind` server exists or no key is set; supports v1 and v2) | the plugin | added to every model request |
| KiloCode | `opencode/hivemind.js` via the `plugin` key (KiloCode is an OpenCode fork; the v1 key gives the full feature set, the v2 `plugins` key only the skills) | the plugin | added to every model request |
| Cline | the user's `~/.cline/data/settings/cline_mcp_settings.json` with `${env:…}` references (Cline has no package plugin) | copied to `~/.cline/skills/` | the `UserPromptSubmit` hook in `~/Documents/Cline/Hooks/`, plus the `PostToolUse` end-of-task nudge |
| Gemini CLI | the extension's `gemini-extension.json` (a `hivemind` entry in `~/.gemini/settings.json` replaces it) | the extension | the `GEMINI.md` instruction block, plus the extension's `SessionStart` hook |
| GitHub Copilot CLI | the user's `~/.copilot/mcp-config.json` (the whole `url` cannot come from the environment; the key can) | the plugin | the plugin's `SessionStart` hook, plus the `~/.copilot/copilot-instructions.md` instruction block |

Everything the setup skill tells an agent to do for one harness lives in
that harness's file under `skills/hivemind-setup/references/`. The skill
itself names no harness-specific paths, and a test enforces this.

## Harness quirks

- **DeepSeek Harness** loads a `.env` from the directory it starts in,
  ranked above `~/.dsh/.env`. The bundle therefore ignores the environment
  whenever that project file names any `HIVEMIND_*` variable, uses
  `~/.dsh/.env` alone, and prints a warning. DSH also strips variables
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
- **Pi** supports two MCP routes for now: the built-in client (Pi 0.99+),
  where the extension registers the server unless an adapter config
  already defines `hivemind`, and the older pi-mcp-adapter. pi-mcp-adapter
  5 also connects servers that extensions register (behind its `mcp`
  proxy, ignoring `exposure`), so the extension sends no `exposure` when
  the adapter is installed. Checked live with Pi 1.0.0 and pi-mcp-adapter
  5.0.0. The adapter route can be dropped later.
- **Oh My Pi** loads the plugin's `.mcp.json` as the server
  `hivemind:hivemind`, expanding `${VAR}` in the URL and the header, and
  mounts MCP tools as routes (`xd://mcp__hivemind_hivemind_hive_*`, or
  `xd://mcp__hivemind_hive_*` for a hand entry, which then replaces the
  plugin's) rather than as named tools; the agent finds them in its system
  prompt. Checked with Oh My Pi 18.9.1.
  A plain `omp install <git-url>` misses the skills, so it installs
  through the marketplace.
- **OpenCode** caches a Git plugin on first use and never refreshes an
  unpinned entry, so users pin a release tag. v1 (1.18.29+) and v2 have
  different plugin APIs, so `opencode/hivemind.js` default-exports one
  plain object that carries both: v1 calls `server()` (an object default
  export is supported from 1.18.29; older v1 releases expect a function
  export) and v2 validates the default export as `{ id, setup }` and
  ignores `server()`. The two implementations are separate — nothing
  translates hooks: v1's `config` hook / `experimental.chat.system.transform`
  / `chat.message` / `tool.execute.after` / `event` become v2's
  `ctx.mcp.transform` + `ctx.skill.transform` / `ctx.session.hook("context")`
  (`event.system` takes `{type, text}` parts) / — / `ctx.tool.hook(
  "execute.after")` / `ctx.event.subscribe`. v2 MCP servers use `disabled`
  (v1 `enabled`); v2 config uses the `plugins` key and migrates a v1
  `plugin` entry, while v1 only warns about `plugins` and does not load
  it; v2 has no `session.idle` — an execution ends with
  `session.execution.succeeded` / `failed` / `interrupted` (v1's idle
  fired on each of them) and compaction ends with
  `session.compaction.ended`. Verified against the v2 source (`@opencode`
  packages) and v1.18.29.
- **KiloCode** 7.x is a fork of OpenCode (`Kilo-Org/kilocode`): the same
  plugin file loads in it, and its v1 plugin API is the OpenCode v1 one
  (same hooks; the `plugin` config key). Its v1 `event` hook receives v2
  events (`session.execution.*`, `session.compaction.ended`), which is why
  the stop and post-compaction nudges work there. KiloCode has its own
  `git:` plugin spec — `git:<repo>[@ref][#subpath]` — that clones the
  repository and loads the file at `#subpath`; a pinned ref is never
  refreshed, a branch ref is re-checked about hourly. The v2 `plugins`
  key loads a reduced plugin context (no `mcp`, `session`, `tool` or
  `event` domains — only `skill`, `agent`, `command` and `integration`
  transforms), so the same file's `setup` registers only the skills there.
  Its MCP config is the OpenCode v2 shape (`mcp.servers`, `disabled`), and
  `KILO_CONFIG_CONTENT` is the highest-precedence inline config overlay
  (the incognito launcher uses it). Global config is `~/.config/kilo/`
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
  enabled by default. `UserPromptSubmit` fires on every prompt (the
  per-prompt reminder is what carries Hivemind across a compaction, whose
  `PreCompact` hook output is not injected — it is wired as a deferred
  hook in the extension's hooks adapter). Verified against `cline/cline`
  main (extension source: `envExpansion.ts`, `skill-directories.ts`,
  `hooks-adapter.ts`, `disk.ts`, `name-transform.ts`).
- **Gemini CLI** installs an extension from a Git URL by cloning the whole
  repository and reading `gemini-extension.json`, `skills/` and
  `hooks/hooks.json` at its root (hence the root files; there is no
  option for a subfolder). It expands `${HIVEMIND_*}` from the launching
  environment in the server's URL and header, and `${extensionPath}` in
  hook commands. It turns off every MCP server in a folder the user has
  not trusted. Checked with Gemini CLI 0.63. On a Windows checkout
  without symlink support the `skills` link is a plain file, so the
  skills would have to be copied by hand.

- **GitHub Copilot CLI** (checked with 1.0.95, headless against a mock
  OpenAI-compatible model: `COPILOT_PROVIDER_BASE_URL`, `COPILOT_MODEL`,
  `COPILOT_PROVIDER_API_KEY` and `copilot -p ... --allow-all-tools`; no
  GitHub login is needed there).
  - *Install.* `copilot plugin marketplace add <repo-url>` reads the
    root `.claude-plugin/marketplace.json`, and `copilot plugin install
    hivemind@hivemind` installs `plugins/hivemind` through
    `.claude-plugin/plugin.json`: both skills, and the hooks from
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
  - *Skills.* Copilot parses `SKILL.md` frontmatter with a strict YAML
    parser: an unquoted `: ` in `description` skipped the `hivemind` skill
    entirely until the fix after 2.7.0 (`copilot skill list` reports
    "failed to parse YAML frontmatter"). `test_skill_descriptions_are_strict_yaml`
    pins it.

## The Stop hook and the post-compaction line

Two nudges that get knowledge written before it is lost: in Claude Code
and Codex from `hooks/hooks.json`, in Pi and Oh My Pi from the extension
and the OpenCode plugin (which KiloCode runs as well), in DeepSeek
Harness the Stop reminder from `dsh/hivemind-reminders.js` (below). Gemini CLI gets the Stop reminder
from the extension's `AfterAgent` hook (`../../hooks/hooks.json`).
Hermes does not have them. GitHub Copilot CLI gets the Stop reminder from
the plugin's `Stop` hook (the shared `hooks/hooks.json`), and no
post-compaction line. Cline gets the end-of-task nudge from the
`PostToolUse` hook in `cline/` (it counts the task's tool calls in a temp
file, reset by `TaskStart`, and never nudges a subagent task); it has no
post-compaction line.

The script counts four transcript formats:

- Claude Code's `"type": "tool_use"` records.
- Codex's `function_call`, `custom_tool_call` and `local_shell_call`
  items. Checked with Codex 0.162.1 against a mock model: it blocks once,
  Codex continues with the reason as the next prompt, and an MCP
  `hive_write`, recorded as `"name":"hive_write","namespace":"mcp__hivemind"`,
  keeps it quiet. Codex runs plugin hooks only after the user trusts them
  in `/hooks`.
- The distinct `functionResponse` ids in Gemini CLI's JSONL chat log,
  which rewrites a call's record as it progresses. Checked with Gemini CLI
  0.63 against a mock model: `AfterAgent` takes the same `block` output
  and sends the reason as the next prompt, and `mcp_hivemind_hive_write`
  keeps it quiet. Gemini's `SessionStart` has no compaction source, so it
  gets no post-compaction line.

- Copilot CLI's `events.jsonl`, with one `tool.execution_start` event per
  call; a Hivemind write shows as `"name":"hivemind-hive_write"`.
  Checked with Copilot CLI 1.0.95 against a mock model (`Stop` input:
  `session_id`, `transcript_path`, `stop_hook_active`, as for Claude
  Code): the hook blocks once, the reason becomes the next prompt, and a
  `hive_write` keeps it quiet. Copilot writes `events.jsonl` a moment
  after the events happen (it was not there yet when a sub-second session
  stopped), so the count can trail by a call or two: fine for a 20-call
  threshold, and the script fails open when the file is missing.

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
  2. `HIVEMIND_API_KEY` is set (same test as the session-start script);
  3. `stop_hook_active` is not true;
  4. no marker for this `session_id` in `$XDG_RUNTIME_DIR/hivemind-stop-reminder/`
     (when that variable names a real directory the user owns), else
     `${TMPDIR:-/tmp}/hivemind-stop-reminder-<uid>/` (written only when it
     blocks, so it never fires twice). The folder is private: created 0700,
     and refused (no reminder) unless it is a real directory owned by the
     user and not group or other writable;
  5. the transcript (grep only, no jq, python or node) has no `hive_write`
     or `hive_feedback` tool call and at least 20 tool uses
     (`HIVEMIND_STOP_MIN_TOOLS` overrides the 20). Lines that carry tool
     definitions (Claude Code's `prompt_snapshot` records list every tool
     with its schema) are skipped, so a definition never counts as a call.

  Any error, missing file or unparsable input fails open. Tests:
  `tests/unit/test_stop_hook.py`.
- **Pi and Oh My Pi.** The extension (`extensions/hivemind.ts`) gives the
  same two nudges with the same texts, under the same conditions (not
  incognito, a key set, `HIVEMIND_STOP_MIN_TOOLS`), counting the calls
  the model made in `tool_execution_start` (a call a script made for it,
  with a `parentToolCallId`, is not counted) and noting a `hive_write` or
  `hive_feedback` by any route (named tool, the adapter's `mcp` proxy, a
  script's code, an Oh My Pi `xd://` route). Once per session (reset on
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
  nothing, sends the reminder once with `client.session.promptAsync`,
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
  `agent/turn-stopping` of a long turn that wrote nothing, steers the agent
  once with the reminder as a user message (as DSH's own Claude Code hooks
  bridge does with a `Stop` block). It stays silent while
  `mcp__hivemind__hive_write` is not visible to the agent, which covers
  incognito and an unset URL (the server row is off then); agents
  announced by `subagent/start` are neither counted nor reminded. No
  post-compaction sentence: DSH keeps the server's instructions in the
  system prompt, and compaction is a session event, not a plugin hook.
  DSH's Claude Code hooks bridge cannot run `hivemind-stop.sh` instead:
  it passes an empty `transcript_path`. Checked live with DSH 0.2.0-rc.2
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
| Pi | Pi 0.99+ built-in MCP: the package registers no server, and the key and URL are unset. With pi-mcp-adapter: an exclusive MCP config, the servers of your global MCP config files minus hivemind (project servers are left out: the adapter would start them without its approval prompt). Either way the extension also blocks `mcp__hivemind…` calls (codemode scripts included), the adapter's `mcp` proxy and `mcpScript` calls that name Hivemind (the `mcpScript` scan is best effort) | the hivemind package; `python3` with the adapter |
| Oh My Pi | `HIVEMIND_MCP_URL`/`HIVEMIND_API_KEY` unset, so the server is never contacted (Oh My Pi warns once that it is unavailable); the extension also blocks any Hivemind call | the hivemind plugin |
| GitHub Copilot CLI | `--disable-mcp-server hivemind` for the session (it never contacts the server); the launcher also unsets the key and URL | the server named `hivemind` in `~/.copilot/mcp-config.json` |
| Gemini CLI | `HIVEMIND_MCP_URL`/`HIVEMIND_API_KEY` unset, so the extension's server is never contacted (Gemini notes an MCP issue) | the hivemind extension, and no hand entry in `settings.json` |
| OpenCode | the plugin skips the server; a hand-configured one is disabled with `OPENCODE_CONFIG_CONTENT` | nothing |
| KiloCode | the plugin (the OpenCode one) skips the server; a hand-configured one is disabled with `KILO_CONFIG_CONTENT` (`mcp.servers.hivemind.disabled`) | nothing |
| Cline | `HIVEMIND_MCP_URL`/`HIVEMIND_API_KEY` unset, so the `${env:…}` references in `cline_mcp_settings.json` stay unexpanded and the server never connects; the hooks see `HIVEMIND_INCOGNITO` and say the incognito line (the skills stay installed) | the server entry with `${env:…}` references |

Where the switch is missing or not configured (the Hermes entry without
`${HIVEMIND_ENABLED}`, a Codex without the entry, a hand-configured Gemini CLI, a Cline
whose server entry does not use `${env:…}`, any other
command), the session is incognito **by restraint**: the tools are
loaded, the key and URL stay in the environment, and an agent with a
shell could still call the REST API. The launcher unsets the key and URL
only where the server is kept from loading (Claude Code, Codex, Copilot CLI,
DeepSeek Harness, Pi, Oh My Pi, Gemini CLI, Cline), because the other harnesses may still need them
(ADR 0035). A DeepSeek Harness session also stays incognito when a
project `.env` names `HIVEMIND_*` variables: incognito is on if the
process environment or `~/.dsh/.env` says so, and no project `.env` can
turn it off. A key set in the `env` block of `~/.claude/settings.json` is
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

## Tests

`tests/unit/test_agent_plugin.py` keeps the plugin, the server and the
docs in step. Among other things it checks that:

- every MCP tool is named in the `hivemind` skill, which stays under 10 KB;
- all manifests agree on the name and version;
- every harness has a reference file routed from the setup skill, and a
  mention in README.md, and the setup skill itself stays harness-neutral;
- the hook, the Hermes section, the Pi extension, the OpenCode plugin and
  the DSH row all switch to incognito, and the launcher applies each
  harness's switch;
- CHANGELOG.md has a row for the current version, and README.md links it.

Run it with `uv run pytest tests/unit/test_agent_plugin.py`. Some tests
need `node` or `python3` and skip without them.

## Releasing

A release bumps the version in every manifest and adds a row to
[CHANGELOG.md](CHANGELOG.md) saying what changed on the server, in the
plugin, and in copies users made themselves. The full release steps are
in the repository's [AGENTS.md](../../AGENTS.md).
