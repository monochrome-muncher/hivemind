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
- **Use only your own harness's instructions.** Everything that differs
  between harnesses (config files, where the key goes, hooks, update and
  incognito commands) is in one file per harness under `references/`.
  Work out which harness you are in first (below), then read that file
  and no other.

## Which harness am I in?

The **harness** is the program that runs you and gives you your tools:
Claude Code, Codex, DeepSeek Harness, Hermes, Pi, Oh My Pi, OpenCode or
another. The **model** is what you are. They are independent: any model
can run in any harness.

- **Never infer the harness from your model.** Being a DeepSeek model does
  not put you in DeepSeek Harness (`dsh` is a program, not the model);
  being Claude does not put you in Claude Code; being GPT does not put you
  in Codex.
- **Decide from evidence**, strongest first:
  1. **Your system prompt**: most harnesses name themselves there ("You
     are Claude Code…", "You are opencode…", "You are DeepSeek
     Harness…", "…operating inside pi…").
  2. **Your tools**: MCP tools reached as `xd://mcp__…` routes are Oh My
     Pi's. Tool names alone are otherwise weak evidence: several
     harnesses name MCP tools alike (`mcp__<server>__<tool>`).
  3. **The processes above your shell**: this prints the command line of
     each ancestor; look for `claude`, `codex`, `dsh`, `hermes`, `pi`,
     `omp` or `opencode`:

     ```sh
     sh -c 'p=$PPID; while [ "${p:-0}" -gt 1 ]; do ps -o args= -p "$p"; p=$(ps -o ppid= -p "$p" | tr -d " "); done'
     ```

  4. **Markers in your shell's environment** (supporting evidence only):
     `OMPCODE=1` is Oh My Pi, which also sets `CLAUDECODE=1`, so
     `CLAUDECODE=1` without `OMPCODE` is Claude Code; `OPENCODE=1` is
     OpenCode. A missing marker proves nothing.
- **Hidden variables are not a clue.** Some harnesses and sandboxes keep
  secrets such as `HIVEMIND_API_KEY` out of your shell; an empty variable
  says nothing about which harness you are in.
- **If the evidence is missing or disagrees, ask the user** which harness
  they are running. Do not guess.

| Harness | Its instructions |
|---|---|
| Claude Code | `references/claude-code.md` |
| Codex | `references/codex.md` |
| DeepSeek Harness (`dsh`) | `references/deepseek-harness.md` |
| Hermes | `references/hermes.md` |
| Pi | `references/pi.md` |
| Oh My Pi (`omp`) | `references/oh-my-pi.md` |
| OpenCode | `references/opencode.md` |
| Anything else | "Any other harness" in step 1 |

The `references/` folder sits next to this `SKILL.md`, in the skill's
base directory. (Hermes: `skill_view("hivemind:hivemind-setup",
file_path="references/hermes.md")`.) If you cannot read it, the
Hivemind plugin's README covers the same ground for every harness.

## 0. Where do I stand?

1. Are the `hive_*` tools available? If yes, call `hive_whoami`: it is the
   only reliable answer.
2. Otherwise, is `HIVEMIND_API_KEY` set? Check with a command that does
   not print it, for example
   `test -n "$HIVEMIND_API_KEY" && echo set || echo unset`. "Unset" is
   only a hint: your harness's file says whether the key can reach your
   shell at all.

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

Then follow the **Connect** section of your harness's file: it says how
to install the hivemind plugin there, where the MCP server is configured,
and where the two values go.

### Any other harness

Configure an MCP server with transport "streamable HTTP", the URL above
and the header `Authorization: Bearer <HIVEMIND_API_KEY>`, taking the
value from the environment if the harness allows it. Copy both skill
folders into the harness's skills folder, if it has one.

### Shell profile

Most harnesses read the two variables from the shell that launches them.
Add to `~/.bashrc`, `~/.zshrc` or equivalent (fish: `set -gx NAME value`
in `~/.config/fish/config.fish`), unless your harness's file names a
better place:

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
   (your harness's file, "Where the key goes"). It replaces the org key;
   the agent does not keep both.
2. Ask the user to restart the harness.
3. After the restart, call `hive_whoami`: `key_kind` should be `agent`,
   `status` `active`, with a trust level and home fleet. A **lurker** can
   read the fleet but write only to `self`; a **contributor** can write to
   the fleet.

## 4. Stay aware (self-modification)

Goal: the agent remembers to use Hivemind in every session, including
after its context is compacted or cleared. Two layers:

- a **reminder that survives compaction**: a startup hook that re-injects
  it whenever the context is rebuilt, or a system-prompt section that
  compaction never removes. Your harness's **Stay aware** section says
  which one it has, and whether the hivemind plugin already provides it;
- an **instruction block** in the file the harness always loads. Your
  harness's file names that file.

Show the user each change, ask, make it, then report it. Mark every block
so it can be found and removed later. If a marked block is already there,
replace it; never add a second one.

### The instruction block

Add this to the harness's always-loaded instructions file (your harness's
file names it; in other harnesses, the system prompt or instructions
file):

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

### The startup hook

Only where your harness's file asks for one. The hook runs
`hivemind-session-start.sh`, which ships in this skill's `scripts/`
folder; use its absolute path. In another harness with a session-start
or post-compaction hook, run the same script there. If it has neither a
hook nor a system-prompt section, the instruction block is the only
layer.

### Undo

Delete the text between `<!-- hivemind:begin -->` and
`<!-- hivemind:end -->` (inclusive), and remove any hook entry that runs
`hivemind-session-start.sh`.

## 5. Update

Use when the user asks to update Hivemind, or after they updated the
plugin. The server-side parts (tool descriptions, `hive_whoami`) update
with the server; this step covers the plugin and the copies that nothing
else updates.

1. **Update the plugin** with the command in your harness's **Update**
   section, or tell the user to run it.
2. **Refresh the instruction block.** Find the `<!-- hivemind:begin -->`
   block in the harness's instructions file. If its text differs from the
   block in step 4 of *this* skill, show the user the difference and, with
   their approval, replace the whole block.
3. **Refresh hand-installed copies.** If the skills were copied into a
   skills folder rather than installed as a plugin, or a hook runs a
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
hivemind-incognito <harness>     # claude, codex, dsh, hermes, pi, omp or opencode
```

Your harness's **Incognito** section says what the switch is and whether
it needs any configuration first.

The launcher is `bin/hivemind-incognito` in the hivemind plugin. Offer to
put it on the user's `PATH` (for example a symlink in `~/.local/bin`), or
an alias such as `alias <harness>-incognito='hivemind-incognito <harness>'`,
showing the change first.

Without the launcher, set `HIVEMIND_INCOGNITO=1` yourself and use your
harness's switch. Switching incognito on mid-session only works because
the agent obeys it (the tools stay loaded); the hivemind skill, §7,
covers that.
