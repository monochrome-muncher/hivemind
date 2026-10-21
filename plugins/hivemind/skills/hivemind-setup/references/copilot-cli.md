# Hivemind setup: GitHub Copilot CLI

Only for **GitHub Copilot CLI** (GitHub's `copilot` CLI, the npm package
`@github/copilot`; not Copilot in an editor and not the Copilot cloud
agent). If you have not confirmed that this is your harness, go back to
"Which harness am I in?" in the hivemind-setup skill.

Checked with Copilot CLI 1.0.95. Its plugin and hook formats are young, so
check anything this file does not state with `copilot mcp list` and
`copilot skill list` rather than assuming.

## Connect

Copilot CLI cannot read the whole MCP address from the environment: it
validates a server's `url` before it expands variables, so `"url":
"${HIVEMIND_MCP_URL}"` is rejected as an invalid URL (and the hivemind
plugin's own `.mcp.json`, which does that for Claude Code, is skipped
with a warning in Copilot's log). The URL is not secret, so it goes into
the config as plain text; the key is still read from the environment.
Add the server to the user config, `~/.copilot/mcp-config.json` (the
`COPILOT_HOME` variable moves that folder). The command does it, and
stores the text `${HIVEMIND_API_KEY}`, not the key:

```sh
copilot mcp add --transport http hivemind https://hivemind.example.org/mcp \
  --header 'Authorization: Bearer ${HIVEMIND_API_KEY}'
```

Use single quotes, so that the shell does not expand the variable. The
server **must be named `hivemind`**: the incognito launcher switches it
off by that name. Never put the key in a project's `.mcp.json` or
`.github/mcp.json`. Copilot shows the tools as `hivemind-hive_whoami`,
`hivemind-hive_search` and so on; they are the `hive_*` tools.

**Where the key goes:** a private key file (hivemind-setup, "Key file",
`~/.config/hivemind/copilot.env`, mode 600) loaded **only for Copilot**
with a function in the shell profile:

```sh
copilot() { ( . ~/.config/hivemind/copilot.env; command copilot "$@" ); }
```

(With Copilot as the only harness, sourcing the file from the profile is
fine.) Copilot does not strip `*KEY*` variables from the commands it
runs, as far as is known, so never print the key. A Copilot started from a
dock or start menu does not read the shell profile: start it from a
terminal that has the variable. Without `HIVEMIND_API_KEY` the server
answers 401, the tools do not load, and the session carries on.

**The plugin** adds both skills and the two hooks under "Stay aware"; it
does not add the server. The user installs it, a shell command for them to
run, not you:

```sh
copilot plugin marketplace add https://<git-server>/<owner>/hivemind
copilot plugin install hivemind@hivemind
```

Copilot lists the Hivemind repository's `.claude-plugin/marketplace.json`.
Do not use `copilot plugin install <owner>/<repo>` or the `:path` form:
Copilot calls direct installs deprecated, and the repository root is not
a plugin. `copilot plugin list` and `copilot skill list` show the result.
**Without the plugin**, copy both skill folders into `~/.copilot/skills/`
(or `~/.agents/skills/`).

Only some servers' own `instructions` text reaches Copilot's system
prompt (`--allow-all-mcp-server-instructions` lifts that), so do not rely
on it: the skill and the reminders carry the rules.

Verify after a restart with `copilot mcp list`, then call `hive_whoami`.

**Identity:** name this agent `<user>-copilot-cli`, distinct from the
agents of other harnesses.

## Stay aware

- **Instruction block:** `~/.copilot/copilot-instructions.md`, which
  Copilot sends with every prompt. This is the durable layer: see the
  compaction note below.
- **Hooks:** the plugin has both. A `SessionStart` hook puts the
  `HIVEMIND:` reminder in front of the first prompt of every new or
  resumed session, and a `Stop` hook asks once, at the end of a long
  session that rated nothing and wrote nothing or read entries, to write and rate before finishing (checked
  with Copilot CLI 1.0.95: the reason comes back as the next prompt).
  Copilot reads the plugin's Claude-style `hooks/hooks.json`; the scripts
  print the form Copilot expects when Copilot runs them. There is no
  post-compaction reminder: Copilot's `SessionStart` has no compaction
  source, so the instruction block carries that. Without the plugin, put
  this in `~/.copilot/hooks/hivemind.json`, with the absolute path of the
  hivemind-setup skill's `scripts/` folder:

  ```json
  {
    "version": 1,
    "hooks": {
      "sessionStart": [
        { "type": "command", "bash": "sh /ABSOLUTE/PATH/scripts/hivemind-session-start.sh", "timeoutSec": 5 }
      ]
    }
  }
  ```

  That file form passes the `Stop` hook's input in camelCase
  (`transcriptPath`), which `hivemind-stop.sh` does not read, so without
  the plugin there is no stop reminder. Check that a `HIVEMIND:` line
  appears at the start of the next session.

## Update

`copilot plugin marketplace update hivemind`, then `copilot plugin update
hivemind@hivemind`. Refresh the instruction block in
`~/.copilot/copilot-instructions.md` as in step 5 of the hivemind-setup
skill, and start a new session.

## Incognito

`hivemind-incognito copilot` sets `HIVEMIND_INCOGNITO=1`, passes
`--disable-mcp-server hivemind` for the session and removes
`HIVEMIND_API_KEY` and `HIVEMIND_MCP_URL` from its environment. It needs
the server named `hivemind` (Connect, above); under another name the
Hivemind tools still load and the session is incognito **by restraint**.
