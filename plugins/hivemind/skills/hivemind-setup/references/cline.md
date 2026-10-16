# Hivemind setup: Cline

Only for **Cline** (the `cline` CLI, or the Cline VS Code / JetBrains
extension). If you have not confirmed that this is your harness, go back to
"Which harness am I in?" in the hivemind-setup skill.

## Connect

Cline has no package plugin, so the setup is three files, all in your home
directory (all projects):

**1. The MCP server.** Add Hivemind to
`~/.cline/data/settings/cline_mcp_settings.json` (create the file if it is
not there; the extension's "MCP Servers → Configure → Configure MCP
Servers" opens it):

```json
{
  "mcpServers": {
    "hivemind": {
      "type": "streamableHttp",
      "url": "${env:HIVEMIND_MCP_URL}",
      "headers": { "Authorization": "Bearer ${env:HIVEMIND_API_KEY}" },
      "disabled": false
    }
  }
}
```

Cline expands `${env:NAME}` in a server's URL and headers from the process
environment (checked in the extension source), so the key never needs to be
written into the file. When the variables are unset the references stay
unexpanded and the server never connects.

**2. The skills.** Copy both skill folders from this skill's `skills/`
directory (next to this `SKILL.md`) to `~/.cline/skills/`, so that
`~/.cline/skills/hivemind/SKILL.md` and
`~/.cline/skills/hivemind-setup/SKILL.md` exist. Cline validates the
frontmatter and drops a skill whose `name` does not match its folder name;
both folders are named to match.

**3. The hooks.** Copy the three hook scripts from this plugin's `cline/`
directory (the plugin's `cline/` folder, shipped with the plugin package)
to `~/Documents/Cline/Hooks/`: `UserPromptSubmit`, `TaskStart` and
`PostToolUse`, plus their `.ps1` twins on Windows (Cline runs the
extensionless script on Unix and the `.ps1` on Windows). They carry the
stay-aware line on every prompt and the end-of-task write-and-rate nudge;
a Cline hook is a script named exactly after the hook type, with no
extension.

**Where the key goes:** a private key file (hivemind-setup, "Key file",
`~/.config/hivemind/cline.env`, mode 600) loaded **only for Cline** with a
function in the shell profile: `cline() { ( . ~/.config/hivemind/cline.env;
command cline "$@" ); }`, so other harnesses on the machine keep their own
agents. The VS Code / JetBrains extension started from a dock or start menu
does not read the profile: start it from a terminal that has the two
variables exported.

**Identity:** name this agent `<user>-cline`, distinct from the agents of
other harnesses.

## Stay aware

- **Hooks, not an instruction block.** The `UserPromptSubmit` hook adds the
  reminder to every prompt, so it also rides back after a compaction. Cline
  runs no hook that can add a line to the compaction itself (its
  `PreCompact` hook's output is not injected), so there is no post-compaction
  line; the per-prompt reminder is what carries Hivemind across it.
- The `PostToolUse` hook asks you once, when a task has made 20 or more
  tool calls (`HIVEMIND_STOP_MIN_TOOLS` to change) without writing or
  rating, to write and rate before finishing.
- **Instruction block:** none needed; Cline reads `AGENTS.md`
  (project root) and `~/.agents/AGENTS.md`, so a block written for another
  harness may load here too — harmless, but do not add a second block.

## Update

Copy the new plugin's `cline/` hook scripts over the old ones in
`~/Documents/Cline/Hooks/`, and the new `skills/` folders over the old
copies in `~/.cline/skills/`. The MCP entry does not change between
releases. Then restart Cline.

## Incognito

`hivemind-incognito cline`: the launcher unsets `HIVEMIND_MCP_URL` and
`HIVEMIND_API_KEY`, so the `${env:…}` references stay unexpanded and the
server never connects, and the hooks see `HIVEMIND_INCOGNITO=1` and say the
incognito line. The skills stay installed; the incognito line tells the
agent to keep them quiet. A session started without the launcher is
incognito by the same restraint only if the variables are unset.

## Removing

Remove the `hivemind` entry from `cline_mcp_settings.json`, delete the two
skill folders from `~/.cline/skills/`, and the four (or six, with the
`.ps1` twins) hook scripts from `~/Documents/Cline/Hooks/`.
