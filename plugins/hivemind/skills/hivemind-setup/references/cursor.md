# Hivemind setup: Cursor

Only for **Cursor** (the Cursor editor from Anysphere, and its
`cursor-agent` CLI). If you have not confirmed that this is your harness,
go back to "Which harness am I in?" in the hivemind-setup skill.

Checked live in November 2026 with the Cursor CLI 2026.11.05, signed in
with a free account, and in its local-model mode (`cursor-agent-local
--authless`, no account), against a test Hivemind server. The MCP server,
both skills, the `sessionStart` hook and incognito worked in both. The
plugin's own rule and `stop` hook did not run in either, so setup adds a
user rule and a user `stop` hook (see "Stay aware"), and those worked. The
editor was not checked; it shares the CLI's plugin, rule and hook formats.
If something does not behave as written, check it in Cursor
(`cursor-agent mcp list` for the server) rather than assuming.

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
there (checked live). It reaches MCP tools through a meta tool
(`CallDynamicTool` signed in, `CallMcpTool` in local-model mode) with
`hivemind` and a tool name such as `hive_whoami`. After a
restart, run `cursor-agent mcp list` and check that `hivemind` is there,
then call `hive_whoami`. In the
editor, Settings, MCP shows the server and its tools.

**The plugin** adds the skills and a `sessionStart` hook (see "Stay
aware"). Cursor installs plugins from the Cursor Marketplace (a public
listing is reviewed, and Hivemind has none yet), from a team marketplace
(an admin uses "Import from Repo" with the Git URL of the Hivemind
repository, which has a `.cursor-plugin/marketplace.json`), or from the
local folder `~/.cursor/plugins/local/`. For one user, the local folder is
the route (checked live). Cursor refuses a symlink there that points
outside that folder, so the clone goes inside it, in a folder starting
with a dot (Cursor skips those), and a relative link names the plugin.
These are shell commands for the user to run, not you:

```sh
mkdir -p ~/.cursor/plugins/local
cd ~/.cursor/plugins/local
git clone https://github.com/monochrome-muncher/hivemind .hivemind-src
ln -s .hivemind-src/plugins/hivemind hivemind
```

(Use the address of your organization's copy of the repository if it has
one.) Then restart Cursor, or run "Developer: Reload Window". On an
Enterprise plan an admin must allow local plugin imports.

In local-model mode (`--authless`) Cursor ignores that folder and reads
`~/.cursor/plugins/local-marketplaces.json` instead (signed in, it ignores
that file). There, add the repository as a marketplace (checked live;
Cursor fetches it again at each start):

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

**Without the plugin**, copy the three skill folders into `~/.cursor/skills/`.
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

- **Rule:** the plugin ships `cursor/rules/hivemind.mdc` with
  `alwaysApply: true`, but in the live check Cursor never put a plugin's
  rule in the model's context, while a user rule in `~/.cursor/rules/`
  was there in every conversation. So create `~/.cursor/rules/hivemind.mdc`
  (with or without the plugin) with this frontmatter and the block from
  step 4 of the hivemind-setup skill ("The instruction block") as its
  body:

  ```
  ---
  description: Hivemind shared memory
  alwaysApply: true
  ---
  ```

  This is the durable layer: Cursor's `sessionStart` has no compaction
  source, so nothing re-runs a hook after a compaction, and the rule is
  what carries Hivemind across one. If the plugin's rule does load one
  day, the sentence appears twice, which does no harm. In the editor,
  pasting the block into Cursor's User Rules in Settings works as well.
- **Hook:** the plugin's `cursor/hooks.json` runs
  `hivemind-session-start.sh` on `sessionStart`. The script prints
  `{"additional_context": "..."}` when the hook passes it `--cursor`, and Cursor adds that text to the
  conversation's start (checked live: the text arrives in the first
  message). The plugin points Cursor at this Cursor-only file,
  not at `hooks/hooks.json`, which is in the Claude Code format. Check that
  a `HIVEMIND:` line appears at the start of a new conversation.
- **Stop reminder:** a plugin's `stop` hook never ran in the live check,
  but a user one does, in an interactive session when signed in (not with
  `-p`, and not in local-model mode). With the plugin in the local folder,
  add this to the user file `~/.cursor/hooks.json` (merge it into the
  `hooks` there if the file exists):

  ```json
  {
    "version": 1,
    "hooks": {
      "stop": [
        {
          "command": "sh \"$HOME/.cursor/plugins/local/hivemind/skills/hivemind-setup/scripts/hivemind-stop.sh\" --cursor"
        }
      ]
    }
  }
  ```

  At most once per session, after a substantial session that wrote or
  rated nothing, the script answers with a `followup_message`, which
  Cursor sends as the next message: a `HIVEMIND:` reminder to write what
  another agent could reuse. Without the plugin there is no script to
  point at, so there is no stop reminder; write as you go (the hivemind
  skill says when).

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
