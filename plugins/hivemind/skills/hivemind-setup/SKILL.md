---
name: hivemind-setup
description: Connect this agent to the organization's Hivemind, register it, switch to its agent key after activation, and make it permanently Hivemind-aware (a startup hook plus an instruction block that survive compaction). Use when the hive_* tools are missing or failing, when hive_whoami shows an org key or a pending agent, when the user has received an agent key, or when the user asks to set up Hivemind.
---

# Hivemind setup

Four steps. Do only the ones that are missing: start by checking where
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

| Situation | Go to |
|---|---|
| No `hive_*` tools, or they cannot connect | 1. Connect |
| `hive_whoami` says `key_kind: "org"` | 2. Register |
| `status: "pending"` | Wait for the admin; then 3. Switch to the agent key |
| The user has just received an agent key | 3. Switch to the agent key |
| Everything works | 4. Stay aware (if not done yet) |

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

- **With the hivemind bundle** (`dsh plugin --profile <name> add
  <path-or-package>`): the bundle defines the MCP server from
  `HIVEMIND_MCP_URL` and `HIVEMIND_API_KEY` and serves both skills. Only
  set the variables, in the shell that launches `dsh`. The server stays
  off while `HIVEMIND_MCP_URL` is unset.
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
     `<user>-<harness>-<purpose>`, e.g. `chris-claude-infra`;
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

Show the user each change, ask, make it, then report it. Mark every block
so it can be found and removed later.

### The instruction block

Add this to the harness's always-loaded instructions:
`~/.claude/CLAUDE.md` (Claude Code), `~/.codex/AGENTS.md` (Codex),
`~/.dsh/AGENTS.md` (DeepSeek Harness), or the system prompt / instructions
file of other harnesses:

```markdown
<!-- hivemind:begin -->
## Hivemind
This organization's Hivemind (the hive_* MCP tools) is my long-term memory.
Every session, and again after compaction: call hive_whoami first, then
follow the hivemind skill. Recall with hive_search before non-trivial work,
contribute reusable findings with hive_write as often as I have them, and
prefer Hivemind over local memory files. If I cannot write, tell the user
why and still recall. Never write my own keys or credentials.
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
