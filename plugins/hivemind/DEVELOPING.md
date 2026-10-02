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
| `hooks/hooks.json` | A `SessionStart` hook (`startup`, `resume`, `clear`, `compact`, `fork`) that re-injects the reminder whenever the context is rebuilt (Claude Code, Codex) |
| `.mcp.json` | The MCP server for Claude Code, built from `HIVEMIND_MCP_URL` and `HIVEMIND_API_KEY` |
| `package.json`, `cordis.patch.yml`, `dsh/` | The DeepSeek Harness bundle: the MCP server row, and a small provider (`dsh/hivemind-skills.js`) that serves `skills/` |
| `plugin.yaml`, `__init__.py` | The Hermes plugin: serves `skills/` and registers a system-prompt section |
| `extensions/hivemind.ts` | The Pi and Oh My Pi extension: the system-prompt section, the MCP server on Pi 0.99+, and blocking Hivemind calls in incognito sessions |
| `opencode/hivemind.js` | The OpenCode plugin: registers the MCP server, adds the skills, and puts the reminder in every model request |
| `bin/hivemind-incognito` | The incognito launcher ([below](#incognito)) |
| `CHANGELOG.md` | What changed for agents in each release |
| `../../package.json` (repository root) | Same package name as `plugins/hivemind/package.json`. Lets DeepSeek Harness, Pi and OpenCode install the plugin from the repository's Git URL |
| `../../.claude-plugin/`, `../../.agents/plugins/` (repository root) | The marketplaces Claude Code, Codex and Oh My Pi install from |

## How each harness gets each piece

Every harness needs three things: the MCP server, the two skills, and a
reminder that survives compaction and new sessions.

| Harness | MCP server | Skills | Reminder |
|---|---|---|---|
| Claude Code | `.mcp.json` in the plugin | the plugin | `SessionStart` hook |
| Codex | the user's `~/.codex/config.toml` (plugin MCP config cannot read the URL and key from the environment) | the plugin | `SessionStart` hook, which the user must trust in `/hooks` |
| DeepSeek Harness | the bundle's row in `cordis.patch.yml` | `dsh/hivemind-skills.js` | none needed: DSH keeps the server's instructions in the system prompt |
| Hermes | the user's `~/.hermes/config.yaml` | the plugin | the `SOUL.md` instruction block (the plugin's section is not rendered, see below) |
| Pi | the extension registers it with Pi 0.99+ built-in MCP; with pi-mcp-adapter, the adapter's config | the package | the extension's system-prompt section |
| Oh My Pi | the user's `~/.omp/agent/mcp.json` | the plugin | the extension's system-prompt section |
| OpenCode | `opencode/hivemind.js` (skipped if a hand-configured `hivemind` server exists or no key is set) | the plugin | added to every model request |
| Gemini CLI | the user's `~/.gemini/settings.json` | copied by hand | the `GEMINI.md` instruction block |

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
- **Hermes** registers the plugin's system-prompt section but current
  releases never render it (upstream issue
  NousResearch/hermes-agent#117432, closed "not planned"). That is why the
  `SOUL.md` block is required. Hermes also loads `~/.hermes/.env` over the
  process environment, which matters for incognito.
- **Pi** supports two MCP routes for now: the built-in client (Pi 0.99+),
  where the extension registers the server unless an adapter config
  already defines `hivemind`, and the older pi-mcp-adapter. The adapter
  route can be dropped later.
- **Oh My Pi** mounts MCP tools as routes (`xd://mcp__hivemind_hive_*`)
  rather than as named tools; the agent finds them in its system prompt.
  A plain `omp install <git-url>` misses the skills, so it installs
  through the marketplace.
- **OpenCode** caches a Git plugin on first use and never refreshes an
  unpinned entry, so users pin a release tag.
- **Gemini CLI** has no plugin yet and turns off every MCP server in a
  folder the user has not trusted.

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
| Pi | Pi 0.99+ built-in MCP: the package registers no server, and the key and URL are unset. With pi-mcp-adapter: an exclusive MCP config, your global and project servers minus hivemind. Either way the extension also blocks `mcp__hivemind…` calls (codemode scripts included), the adapter's `mcp` proxy and `mcpScript` calls that name Hivemind (the `mcpScript` scan is best effort) | the hivemind package; `python3` with the adapter |
| Oh My Pi | `HIVEMIND_MCP_URL`/`HIVEMIND_API_KEY` unset, so the server is never contacted (Oh My Pi warns once that it is unavailable); the extension also blocks any Hivemind call | the hivemind plugin |
| OpenCode | the plugin skips the server; a hand-configured one is disabled with `OPENCODE_CONFIG_CONTENT` | nothing |

Where the switch is missing or not configured (the Hermes entry without
`${HIVEMIND_ENABLED}`, a Codex without the entry, Gemini CLI, any other
command), the session is incognito **by restraint**: the tools are
loaded, the key and URL stay in the environment, and an agent with a
shell could still call the REST API. The launcher unsets the key and URL
only where the server is kept from loading (Claude Code, Codex, DeepSeek
Harness, Pi, Oh My Pi), because the other harnesses may still need them
(ADR 0035). A DeepSeek Harness session also stays incognito when a
project `.env` names `HIVEMIND_*` variables: incognito is on if the
process environment or `~/.dsh/.env` says so, and no project `.env` can
turn it off. A key set in the `env` block of `~/.claude/settings.json` is
applied by Claude Code itself, so it stays visible to an incognito
session's shell.

Setting only `HIVEMIND_INCOGNITO=1`, or turning incognito on during a
session, is incognito by restraint, and the agent says so. A session
started incognito cannot turn Hivemind back on.

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
