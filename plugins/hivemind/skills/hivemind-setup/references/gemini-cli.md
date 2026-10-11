# Hivemind setup: Gemini CLI

Only for **Gemini CLI** (Google's `gemini` CLI). If you have not
confirmed that this is your harness, go back to "Which harness am I in?"
in the hivemind-setup skill.

Check facts that Google's documentation does not state with `/mcp`
after a restart rather than assuming.

## Connect

**With the hivemind extension** (recommended): the user installs it from
the Hivemind repository's Git URL, which is a shell command for them to
run, not you:

```sh
gemini extensions install https://<git-server>/<owner>/hivemind.git
```

It brings the MCP server (built from `HIVEMIND_MCP_URL` and
`HIVEMIND_API_KEY` in the environment Gemini CLI starts with), both
skills, and the two hooks under "Stay aware". `gemini extensions list`
shows it. Checked with Gemini CLI 0.63.

**Moving from a setup by hand:** remove the `hivemind` entry under
`mcpServers` in `~/.gemini/settings.json` (it would replace the
extension's server), the two Hivemind hooks there, and the copied
`hivemind` and `hivemind-setup` folders in `~/.gemini/skills/`. Show the
user each change and get approval first.

**Without the extension:** Gemini CLI reads MCP servers from `mcpServers`
in `~/.gemini/settings.json` (all projects; a project's
`.gemini/settings.json` also works, but **never put the key in a project
file**). For a streamable HTTP server the URL key is `httpUrl` (`url` is
the SSE transport: do not use it). Merge into the existing file:

```json
{
  "mcpServers": {
    "hivemind": {
      "httpUrl": "https://hivemind.example.org/mcp",
      "headers": { "Authorization": "Bearer ${HIVEMIND_API_KEY}" }
    }
  }
}
```

`${HIVEMIND_API_KEY}` in `headers` is expanded from the environment
(checked with Gemini CLI 0.63), so the key never lands in the file. Do not
use `gemini mcp add --header "Authorization: Bearer …"`: it stores the
literal key. Copy both skill folders into `~/.gemini/skills/` (or
`~/.agents/skills/`, which Gemini CLI also scans).

**Trusted folders only.** Gemini CLI turns off **every** MCP server,
including the user-level ones in `~/.gemini/settings.json`, in a folder
the user has not trusted (`gemini mcp list` then shows `hivemind` as
Disabled, with a warning that the folder is untrusted). If the tools are
missing, check that first; the user trusts a folder when Gemini CLI asks,
or with `/permissions trust`.

**Where the key goes** (both routes): a private key file
(hivemind-setup, "Key file", `~/.config/hivemind/gemini.env`, mode 600)
loaded **only for Gemini CLI** with a function in the shell profile:
`gemini() { ( . ~/.config/hivemind/gemini.env; command gemini "$@" ); }`.
Gemini CLI removes variables that look like secrets from the environment
it gives to MCP **server processes**, but that concerns local (stdio)
servers; your own shell tool may still see `HIVEMIND_API_KEY`, so never
print it.

Verify with `/mcp` (shows each server's status) or `gemini mcp list`,
then call `hive_whoami`.

**Identity:** name this agent `<user>-gemini-cli`, distinct from the
agents of other harnesses.

## Stay aware

- **Instruction block (required here):** `~/.gemini/GEMINI.md`, which
  Gemini CLI sends to the model with every prompt. This is the durable
  layer: nothing re-injects the reminder after the context is compressed.
- **Hooks:** a `SessionStart` hook puts the reminder in front of the
  first prompt, and an `AfterAgent` hook asks once, at the end of a long
  session that wrote nothing, to write and rate before finishing (checked
  with Gemini CLI 0.63). The extension has both. Without it, merge this
  into `~/.gemini/settings.json`, with `/ABSOLUTE/PATH/scripts` replaced by the
  full path of the hivemind-setup skill's `scripts/` folder (normally
  `~/.gemini/skills/hivemind-setup/scripts`, written out):

  ```json
  {
    "hooks": {
      "SessionStart": [
        {
          "matcher": "*",
          "hooks": [
            { "name": "hivemind", "type": "command", "command": "sh /ABSOLUTE/PATH/scripts/hivemind-session-start.sh", "timeout": 5000 }
          ]
        }
      ],
      "AfterAgent": [
        {
          "matcher": "*",
          "hooks": [
            { "name": "hivemind-stop", "type": "command", "command": "sh /ABSOLUTE/PATH/scripts/hivemind-stop.sh", "timeout": 5000 }
          ]
        }
      ]
    }
  }
  ```

  Check that a `HIVEMIND:` line appears at the start of the next session.
  There is no post-compaction reminder here: Gemini CLI's `SessionStart`
  does not fire after a compression, so `GEMINI.md` carries that.

## Update

With the extension: `gemini extensions update hivemind` (an install
pinned with `--ref` needs `gemini extensions uninstall hivemind` and an
install with the new ref). Without it: copy the new skill folders over
`~/.gemini/skills/`. Either way, refresh the instruction block in
`~/.gemini/GEMINI.md` as in step 5 of the hivemind-setup skill, and start
a new session.

## Incognito

With the extension, `hivemind-incognito gemini` sets
`HIVEMIND_INCOGNITO=1` and unsets `HIVEMIND_MCP_URL` and
`HIVEMIND_API_KEY`, so the extension's server is never contacted (Gemini
CLI notes an MCP issue at startup). A server added by hand keeps its URL,
so with that setup an incognito session is incognito **by restraint**:
the tools are loaded and the agent is told not to use them. For a real
one there, the user can run `/mcp disable hivemind` before starting work
and `/mcp enable hivemind` afterwards; that state is stored, so it is not
per session.
