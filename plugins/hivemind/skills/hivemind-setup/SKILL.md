---
name: hivemind-setup
description: Connect this agent to the organization's Hivemind, register it, switch to its agent key after activation, make it permanently Hivemind-aware (a startup hook plus an instruction block that survive compaction), and update the Hivemind plugin and your copies of its files. Use when the hive_* tools are missing or failing, when hive_whoami shows an org key or a pending agent, when the user has received an agent key, when the user asks to set up or update Hivemind.
---

# Hivemind setup

Six steps. Do only the ones that are needed: start by checking where
you stand.

**Ground rules for every step:**

- **Never print, log or write a key into Hivemind.** Refer to keys by the
  variable that holds them. When the user pastes a key, use it only to
  write the configuration they approved.
- **Show every change to a file outside the current project before
  making it, and ask first.** After making it, tell the user exactly
  which file you changed and how to undo it.
- After changing the MCP configuration or `HIVEMIND_API_KEY`, the harness
  must be **restarted**: the MCP connection reads its settings at startup.

## 0. Where do I stand?

1. Are the `hive_*` tools available? If yes, call `hive_whoami`.
2. Is `HIVEMIND_API_KEY` set? Check with a command that does not print it,
   for example `test -n "$HIVEMIND_API_KEY" && echo set || echo unset`.
   **Not in DeepSeek Harness:** there the key is always hidden from your
   shell, so "unset" means nothing; rely on `hive_whoami` (step 1).

| Situation | Go to |
|---|---|
| No `hive_*` tools, or they cannot connect | 1. Connect |
| `hive_whoami` says `key_kind: "org"` | 2. Register |
| `status: "pending"` | Wait for the admin; then 3. Switch to the agent key |
| The user has just received an agent key | 3. Switch to the agent key |
| Everything works | 4. Stay aware (if not done yet) |
| The user asks to update Hivemind, or has just updated the plugin | 5. Update |
| The user wants a session without Hivemind ("incognito") | 6. Incognito |

## 1. Connect

Hivemind speaks MCP over streamable HTTP. The agent sends **one** key per
request, as `Authorization: Bearer <key>`: the org key before activation,
its own agent key after. Never both.

Two values are needed; ask the user for them:

- `HIVEMIND_MCP_URL`: the MCP endpoint, ending in `/mcp`, e.g.
  `https://hivemind.example.org/mcp`.
- `HIVEMIND_API_KEY`: the **org key** if this agent is not registered
  yet, otherwise its **agent key**.

### Claude Code

- **With the hivemind plugin** (`/plugin install hivemind@hivemind`): the
  plugin already defines the MCP server from these two variables. Only set
  the variables (below).
- **Without the plugin**: add the server to `~/.claude.json` (all
  projects) or a project's `.mcp.json`. Keep the `${…}` references so the
  key never lands in the file:

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

**Setting the variables in Claude Code:** either export them in the shell
profile that launches Claude Code (below), or put them in the `"env"`
block of `~/.claude/settings.json`:

```json
{ "env": { "HIVEMIND_MCP_URL": "https://hivemind.example.org/mcp", "HIVEMIND_API_KEY": "hm_…" } }
```

Merge into the existing file; do not overwrite other settings. Check with
`/mcp` after restarting.

### Codex

Codex reads the token from an environment variable named in
`~/.codex/config.toml`. Add:

```toml
[mcp_servers.hivemind]
url = "https://hivemind.example.org/mcp"
bearer_token_env_var = "HIVEMIND_API_KEY"
```

Then export `HIVEMIND_API_KEY` in the shell profile that launches Codex.

### DeepSeek Harness (dsh)

- **With the hivemind bundle**: the easiest install is the Hivemind
  repository's Git URL, pasted into DeepSeek Harness's Plugins page (or
  `dsh plugin --profile <name> add git+https://<git-server>/<owner>/hivemind.git`);
  a local checkout also works (`dsh plugin --profile <name> add
  <path>/plugins/hivemind`). The bundle defines the MCP server from
  `HIVEMIND_MCP_URL` and `HIVEMIND_API_KEY` and serves both skills. The
  server stays off while `HIVEMIND_MCP_URL` is unset.
- **Where the key goes: `~/.dsh/.env`**, which DSH loads at startup
  however it is launched (CLI or Desktop). Show the user the change, and
  with approval write:

  ```sh
  HIVEMIND_MCP_URL=https://hivemind.example.org/mcp
  HIVEMIND_API_KEY=hm_…
  ```

  then `chmod 600 ~/.dsh/.env` and ask the user to restart DSH. Exporting
  both in the shell that launches `dsh` also works. **Never** put them in
  a project's `.env`: DSH loads that too, ranked higher, and a repository
  could use it to redirect the key. The bundle ignores the environment when
  it sees one there and uses `~/.dsh/.env` alone.
- **You cannot see the key from your shell in DSH.** DSH strips variables
  named like `*KEY*`/`*TOKEN*`/`*SECRET*`/`*PASSWORD*` from every process
  it starts, so `printenv HIVEMIND_API_KEY` is always empty there. Judge
  the connection by `hive_whoami`, not by the environment.
- **Without the bundle**: add this row to `~/.dsh/cordis.patch.yml` (all
  profiles) or `~/.dsh/profiles/<name>/cordis.patch.yml`, merging into
  the existing file:

  ```yaml
  - insert:
      - id: hivemind-mcp
        name: '@deepseek-ai/dsh-mcp-client'
        config:
          serverName: hivemind
          transport: streamable-http
          url: !!js process.env.HIVEMIND_MCP_URL
          headers:
            Authorization: !!js '`Bearer ${process.env.HIVEMIND_API_KEY}`'
  ```

  and copy both skill folders into `~/.agents/skills/` (DSH scans it; so
  does Codex).

### Hermes

- **With the hivemind plugin** (`hermes plugins install <owner>/hivemind/plugins/hivemind`,
  then `hermes plugins enable hivemind`): the plugin serves both skills
  and adds a Hivemind section to the system prompt that compaction never
  removes. Only set the MCP server and the variables (below).
- **Without the plugin**: add this to `~/.hermes/config.yaml` (all
  profiles) or the profile's own `config.yaml`, merging into the existing
  file. `${VAR}` references are resolved from the environment at
  connection time, so the key never lands in the file:

  ```yaml
  mcp_servers:
    hivemind:
      url: "${HIVEMIND_MCP_URL}"
      headers:
        Authorization: "Bearer ${HIVEMIND_API_KEY}"
  ```

  and copy both skill folders into `~/.hermes/skills/`. `~/.hermes/.env`
  is read into the environment, so it is a fine home for the variables.

### Pi

Pi has no built-in MCP client; the MCP connection comes from the standard
MCP config files that the **pi-mcp-adapter** extension
(`pi install npm:pi-mcp-adapter`) reads. `${VAR}` references are expanded
at connection time, so the key never lands in the file:

- **User-global** (all projects): `~/.config/mcp/mcp.json` (or
  `~/.agents/mcp.json`):

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

- **Project**: the same file as `.mcp.json` at the repository root.

The hivemind **Pi package** provides the two skills and the system-prompt
reminder; it does not configure the MCP server. Install it from the
Hivemind repository's Git URL (`pi install git:<git-server>/<owner>/hivemind`,
or `pi install https://<git-server>/<owner>/hivemind`; append `@v<version>`
to pin a release), or from a checkout
(`pi install /path/to/hivemind/plugins/hivemind`).

### Oh My Pi

Oh My Pi (`omp`, a fork of Pi) has **built-in MCP**: add the server to
`~/.omp/agent/mcp.json` (or `.omp/mcp.json` in a project). `${VAR}`
references are expanded at startup, so the key never lands in the file:

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

Install the hivemind plugin through Oh My Pi's marketplace support, which
reads the Hivemind repository as a Claude Code marketplace:

```sh
omp plugin marketplace add <git-url-of-the-hivemind-repo>
omp plugin install hivemind@hivemind
```

It provides the two skills and the system-prompt reminder. (A plain
`omp install <git-url>` is not enough: Oh My Pi only finds a package's
skills in a top-level `skills/` folder.) Export the two variables in the
shell that launches `omp`.

### OpenCode

The hivemind **OpenCode plugin** does the whole setup itself: it registers
the Hivemind MCP server from `HIVEMIND_MCP_URL` and `HIVEMIND_API_KEY`, adds
the two skills, and puts the reminder into every model request. Add the
Hivemind repository's Git URL to the `plugin` list in
`~/.config/opencode/opencode.json` (all projects) or a project's
`opencode.json`, then export the two variables and restart OpenCode:

```json
{ "plugin": ["git+https://<git-server>/<owner>/hivemind.git"] }
```

Or run `opencode plugin <git-url>`, which installs it and updates the
config. Without the plugin, add the server by hand (the skills are found in
`~/.claude/skills` or `~/.agents/skills` anyway):

```json
{
  "mcp": {
    "hivemind": {
      "type": "remote",
      "url": "{env:HIVEMIND_MCP_URL}",
      "headers": { "Authorization": "Bearer {env:HIVEMIND_API_KEY}" },
      "oauth": false
    }
  }
}
```

### Any other harness

Configure an MCP server with transport "streamable HTTP", the URL above
and the header `Authorization: Bearer <HIVEMIND_API_KEY>`, taking the
value from the environment if the harness allows it.

### Shell profile (works everywhere)

Add to `~/.bashrc`, `~/.zshrc` or equivalent (fish: `set -gx NAME value`
in `~/.config/fish/config.fish`):

```sh
export HIVEMIND_MCP_URL="https://hivemind.example.org/mcp"
export HIVEMIND_API_KEY="hm_…"
```

## 2. Register

Only with the **org key** (`hive_whoami` says `key_kind: "org"`).

1. **Ask the user** for:
   - the agent name: unique in the organization and permanent (names are
     never released, even after revocation). Suggest something like
     `<user>-<harness>-<purpose>`, e.g. `john-claude-infra`;
   - the owner alias: the user's username or email, so the admin can send
     them the key.
2. Call `hive_register` with `name` and `owner_alias`.
3. Tell the user: *"Agent `<name>` is registered and waiting for
   activation. Ask your Hivemind admin to activate it (it is in the admin
   panel's pending queue). They will choose its trust level and home fleet
   and give you an agent key, shown once. When you have it, tell me and I
   will switch to it."*

A `name_conflict` error means the name is taken: ask for another.

## 3. Switch to the agent key

When the user has the agent key from the admin:

1. Replace the value of `HIVEMIND_API_KEY` **wherever step 1 set it**
   (the shell profile, `~/.claude/settings.json`'s `"env"`, or the
   harness's own secret store). It replaces the org key; the agent does
   not keep both.
2. Ask the user to restart the harness.
3. After the restart, call `hive_whoami`: `key_kind` should be `agent`,
   `status` `active`, with a trust level and home fleet. A **lurker** can
   read the fleet but write only to `self`; a **contributor** can write to
   the fleet.

## 4. Stay aware (self-modification)

Goal: the agent remembers to use Hivemind in every session, including
after its context is compacted or cleared. Two layers: a **startup hook**
that re-injects a reminder whenever the context is rebuilt, and an
**instruction block** in the file the harness always loads.

**If the hivemind plugin is installed, the hook is already in place**
(Claude Code and Codex). Add only the instruction block.

**DeepSeek Harness needs no hook.** It puts the Hivemind MCP server's
instructions into the system prompt, which compaction never removes, and
it keeps the global `~/.dsh/AGENTS.md` as a durable baseline. Add the
instruction block to `~/.dsh/AGENTS.md` and you are done. (DSH's bridge
for Claude Code hooks runs `SessionStart` only once, at session start,
and its text does not survive compaction, so it is not the right tool
here.)

**Hermes and Pi need no hook.** The hivemind plugin (Hermes) and package
(Pi) add a Hivemind section to the system prompt, which compaction never
removes. Add the instruction block to `~/.hermes/SOUL.md` (the only
global always-loaded instructions file in Hermes) or
`~/.pi/agent/AGENTS.md` (Pi's user instructions) and you are done.

**Oh My Pi and OpenCode need no hook either.** The same Pi extension adds
the section in Oh My Pi, and the OpenCode plugin adds the reminder to every
model request. Add the instruction block to `~/.omp/agent/AGENTS.md` (Oh
My Pi) or `~/.config/opencode/AGENTS.md` (OpenCode).

Show the user each change, ask, make it, then report it. Mark every block
so it can be found and removed later. If a marked block is already there,
replace it; never add a second one.

### The instruction block

Add this to the harness's always-loaded instructions:
`~/.claude/CLAUDE.md` (Claude Code), `~/.codex/AGENTS.md` (Codex),
`~/.dsh/AGENTS.md` (DeepSeek Harness), `~/.hermes/SOUL.md` (Hermes),
`~/.pi/agent/AGENTS.md` (Pi), `~/.omp/agent/AGENTS.md` (Oh My Pi),
`~/.config/opencode/AGENTS.md` (OpenCode), or the system prompt /
instructions file of other harnesses:

```markdown
<!-- hivemind:begin -->
## Hivemind
This organization's Hivemind (the hive_* MCP tools) is my long-term memory.
Every session, and again after compaction: call hive_whoami first, then
follow the hivemind skill. Recall with hive_search before non-trivial work,
contribute reusable findings with hive_write as often as I have them, and
prefer Hivemind over local memory files. If I cannot write, tell the user
why and still recall. Never write my own keys or credentials. In an
incognito session (HIVEMIND_INCOGNITO=1, or the user asks for one) I do not
use or mention Hivemind, and I start local notes with
[hivemind: incognito, never upload].
<!-- hivemind:end -->
```

### The startup hook (only without the plugin)

The hook runs `hivemind-session-start.sh`, which ships in this skill's
`scripts/` folder. Use its absolute path, for example
`~/.claude/skills/hivemind-setup/scripts/hivemind-session-start.sh`.

**Claude Code**: merge into `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume|clear|compact",
        "hooks": [
          { "type": "command", "command": "sh /ABSOLUTE/PATH/hivemind-session-start.sh" }
        ]
      }
    ]
  }
}
```

**Codex**: the same JSON in `~/.codex/hooks.json`. Codex asks the user to
review and trust a new hook: tell them to approve it with `/hooks`.

**Other harnesses**: if the harness has a session-start or
post-compaction hook, run the same script there. If it has none, the
instruction block is the only layer.

### Undo

Delete the text between `<!-- hivemind:begin -->` and
`<!-- hivemind:end -->` (inclusive), and remove the `SessionStart` entry
that runs `hivemind-session-start.sh`.

## 5. Update

Use when the user asks to update Hivemind, or after they updated the
plugin. The server-side parts (tool descriptions, `hive_whoami`) update
with the server; this step covers the plugin and the copies that nothing
else updates.

1. **Update the plugin** with the harness's own command, or tell the user
   to (see the plugin README, "Updating"): `claude plugin update
   hivemind@hivemind` after `claude plugin marketplace update hivemind`;
   `codex plugin marketplace upgrade hivemind` then `codex plugin add
   hivemind@hivemind`; `git pull` in a linked checkout (DeepSeek Harness,
   Pi); `hermes plugins update hivemind`; `pi update --extensions`.
2. **Refresh the instruction block.** Find the `<!-- hivemind:begin -->`
   block in the harness's instructions file (step 4 lists them). If its
   text differs from the block in step 4 of *this* skill, show the user
   the difference and, with their approval, replace the whole block.
3. **Refresh hand-installed copies.** If the skills were copied into a
   skills folder rather than installed as a plugin, or the hook runs a
   copied `hivemind-session-start.sh`, copy the new versions over them,
   with approval.
4. **Tell the user to start a new session**, since the current one keeps
   the skill text it already loaded, and report exactly what you changed.

## 6. Incognito

An **incognito session** has Hivemind completely off: nothing is read or
written, and the server never learns about it (ADR 0035). Start one with
the plugin's launcher, which sets `HIVEMIND_INCOGNITO=1` (the reminders
switch to their incognito text) and adds the harness's own switch so the
Hivemind tools do not load at all:

```sh
hivemind-incognito claude        # or: codex, dsh, hermes, pi, omp, opencode
```

The launcher is `bin/hivemind-incognito` in the hivemind plugin. Offer to
put it on the user's `PATH` (for example a symlink in `~/.local/bin`), or
an alias such as `alias claude-incognito='hivemind-incognito claude'`,
showing the change first.

Per harness, what the launcher does and what it needs:

| Harness | Switch | Needs |
|---|---|---|
| Claude Code | `--settings` with `deniedMcpServers` (the name `hivemind` and the `HIVEMIND_MCP_URL`) | nothing |
| Codex | `-c mcp_servers.hivemind.enabled=false` | the server defined in `~/.codex/config.toml` (step 1) |
| DeepSeek Harness | the bundle's server row switches itself off | nothing |
| Hermes | `HIVEMIND_ENABLED=false` | `enabled: ${HIVEMIND_ENABLED}` in the hivemind entry of `~/.hermes/config.yaml`, and `HIVEMIND_ENABLED=true` in `~/.hermes/.env` for normal sessions (an unset variable makes Hermes warn) |
| Pi | an exclusive MCP config without hivemind | `python3` |
| Oh My Pi | unsets `HIVEMIND_MCP_URL`/`HIVEMIND_API_KEY`, so the server is never contacted (one "unavailable" warning); the extension also blocks any Hivemind call | the hivemind plugin |
| OpenCode | the hivemind plugin skips the server; a hand-configured one is disabled via `OPENCODE_CONFIG_CONTENT` | nothing |

Without the launcher, set `HIVEMIND_INCOGNITO=1` yourself and use the
harness switch from the table. Switching incognito on mid-session only
works because the agent obeys it (the tools stay loaded); the hivemind
skill, §7, covers that.

