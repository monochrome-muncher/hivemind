# Hivemind setup: Cursor

Only for **Cursor** (the Cursor editor from Anysphere, and its
`cursor-agent` CLI). If you have not confirmed that this is your harness,
go back to "Which harness am I in?" in the hivemind-setup skill.

Checked live in November 2026 with the Cursor CLI 2026.11.05 in its
local-model mode (`cursor-agent-local --authless`, which needs no Cursor
account), against a test model and a test Hivemind server. There the MCP
server, both skills, the `sessionStart` hook, the marketplace install and
incognito worked. The plugin's rule did not reach the model (a user rule
did), and no `stop` hook ran. The editor and the signed-in `cursor-agent`
were not checked; their formats are as documented. If something does not
behave as written, check it in Cursor (`cursor-agent mcp list` for the
server) rather than assuming.

## Connect

Cursor reads MCP servers from the user file `~/.cursor/mcp.json` and
expands `${env:NAME}` in a server's `command`, `args`, `env`, `url` and
`headers`, so both the address and the key can come from the environment.
Add the server to `~/.cursor/mcp.json` (keep any servers already there):

```json
{
  "mcpServers": {
    "hivemind": {
      "url": "${env:HIVEMIND_MCP_URL}",
      "headers": {
        "Authorization": "Bearer ${env:HIVEMIND_API_KEY}"
      }
    }
  }
}
```

The server **must be named `hivemind`**. The file stores the text
`${env:HIVEMIND_API_KEY}`, never the key. Never put the key in a project's
`.cursor/mcp.json`: a project file can be committed, and a cloned project
can define a `hivemind` server of its own that sends the key to a URL it
chooses, so read that file before you trust a folder.

The plugin does not add the server. Cursor sets a plugin's MCP variables
from a team admin's dashboard, so each user configures the server
themselves as above.

**Where the key goes:** a private key file (hivemind-setup, "Key file",
`~/.config/hivemind/cursor.env`, mode 600) loaded **only for Cursor** with
a function in the shell profile:

```sh
cursor() { ( . ~/.config/hivemind/cursor.env; command cursor "$@" ); }
cursor-agent() { ( . ~/.config/hivemind/cursor.env; command cursor-agent "$@" ); }
```

(With Cursor as the only harness, sourcing the file from the profile is
fine.) The Cursor app started from a dock or start menu most likely does
not see shell variables, and then `${env:...}` resolves to nothing and the
server answers 401: start it from a terminal that has the variables, for
example `cursor .`. Without `HIVEMIND_API_KEY` the tools do not load and the
session carries on. Never print the key.

The CLI reads the same `~/.cursor/mcp.json` and expands `${env:...}`
there (checked live). It reaches MCP tools through its `CallMcpTool` tool,
with server `hivemind` and tool name `hive_whoami` and so on. After a
restart, run `cursor-agent mcp list` and check that `hivemind` is there,
then call `hive_whoami`. In the
editor, Settings, MCP shows the server and its tools.

**The plugin** adds both skills, an always-on rule and a `sessionStart`
hook (see "Stay aware"). Cursor installs plugins from the Cursor
Marketplace (a public listing is reviewed, and Hivemind has none yet), from
a team marketplace (an admin uses "Import from Repo" with the Git URL of
the Hivemind repository, which has a `.cursor-plugin/marketplace.json`),
from a marketplace listed in the user file
`~/.cursor/plugins/local-marketplaces.json`, or from a local folder.

For one user, the marketplace file is the route that was checked live.
Create `~/.cursor/plugins/local-marketplaces.json` (or add the entry to the
`marketplaces` list if the file exists):

```json
{
  "version": 1,
  "marketplaces": [
    {
      "name": "hivemind",
      "gitUrl": "https://github.com/monochrome-muncher/hivemind.git",
      "gitRef": "main",
      "plugins": [{ "name": "hivemind", "enabled": true }]
    }
  ]
}
```

(Use the address of your organization's copy of the repository if it has
one.) Cursor clones the repository at start and caches the plugin under
`~/.cursor/plugins/cache/hivemind/`. On an Enterprise plan an admin may
have to allow local plugins.

The local folder route is documented but was not checked live (the
local-model CLI ignores it). These are shell commands for the user to run,
not you:

```sh
git clone https://github.com/monochrome-muncher/hivemind ~/src/hivemind
mkdir -p ~/.cursor/plugins/local
ln -s ~/src/hivemind/plugins/hivemind ~/.cursor/plugins/local/hivemind
```

Then restart Cursor, or run "Developer: Reload Window".

**Without the plugin**, copy both skill folders into `~/.cursor/skills/`.
Then you have no rule and no hook, so add the instruction block as under
"Stay aware".

**Identity:** name this agent `<user>-cursor`, distinct from the agents of
other harnesses.

## Remote and cloud sessions

Cursor's cloud agents run on a remote machine without the user's home
directory. They are not supported here: do not write `~/.cursor` there or
put a key into the repository. Use Hivemind from Cursor on the user's own
machine.

## Stay aware

- **Rule:** the plugin's `cursor/rules/hivemind.mdc` has `alwaysApply:
  true`, so Cursor should put its one sentence in the context of every
  conversation. This is the durable layer: Cursor's `sessionStart` has no
  compaction source, so nothing re-runs a hook after a compaction, and the
  rule is what carries Hivemind across one. In the live check the plugin's
  rule did not reach the model, while a user rule in `~/.cursor/rules/`
  did. So also create `~/.cursor/rules/hivemind.mdc` (with or without the
  plugin) with this frontmatter and the block from step 4 of the
  hivemind-setup skill ("The instruction block") as its body:

  ```
  ---
  description: Hivemind shared memory
  alwaysApply: true
  ---
  ```

  If the plugin's rule does load too, the sentence appears twice, which
  does no harm. In the editor, pasting the block into Cursor's User Rules
  in Settings works as well.
- **Hook:** the plugin's `cursor/hooks.json` runs
  `hivemind-session-start.sh` on `sessionStart`. The script prints
  `{"additional_context": "..."}` when the hook passes it `--cursor`, and Cursor adds that text to the
  conversation's start (checked live: the text arrives in the first
  message). The plugin points Cursor at this Cursor-only file,
  not at `hooks/hooks.json`, which is in the Claude Code format. Check that
  a `HIVEMIND:` line appears at the start of a new conversation.
- **No stop reminder.** In the live check Cursor never ran a `stop` hook,
  and its hook input carries no transcript path, so `hivemind-stop.sh`
  cannot count tool calls there. Cursor's `stop` hook could only send a
  `followup_message`, which arrives as a new user message anyway. Nothing
  nudges the agent to write at the end of a long session, so write as you
  go (the hivemind skill says when).

## Update

With the marketplace file, Cursor fetches the branch again at each start
(checked live), so restart Cursor and start a new conversation. With a
local folder, run `git pull` in the clone first.
Refresh the instruction block or your own rule as in step 5 of the
hivemind-setup skill.

## Incognito

Cursor has no documented per-session MCP switch. `hivemind-incognito
cursor` (or `cursor-agent`) sets `HIVEMIND_INCOGNITO=1`, so the
`sessionStart` hook says the incognito line, and removes `HIVEMIND_API_KEY`
and `HIVEMIND_MCP_URL` from the session's environment, so the
`${env:...}` references in `~/.cursor/mcp.json` resolve to nothing and the
server cannot authenticate. The always-on rule still loads, and a server
configured another way (a literal key or URL in a config file) keeps
working: that session is incognito **by restraint**, and the launcher
prints a note saying so. Start the app from that terminal (`hivemind-incognito
cursor .`), because an app that is already running keeps its own
environment.
