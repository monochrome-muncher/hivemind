# Hivemind setup: Gemini CLI

Only for **Gemini CLI** (Google's `gemini` CLI). If you have not
confirmed that this is your harness, go back to "Which harness am I in?"
in the hivemind-setup skill. There is no Hivemind plugin or extension for
Gemini CLI yet: the setup is an MCP entry, the two skills copied into
place, and an instruction block. (A `gemini-extension.json` bundle was
considered and left out: the extension manifest schema is documented, but
whether `${VAR}` expands inside an extension server's `headers` is not,
and extensions run with a sanitized environment, so the key would have to
go through the extension's own settings. Revisit when that is confirmed.)

Facts below that Google's documentation does not state are marked
UNCONFIRMED; check them with `/mcp` after a restart rather than assuming.

## Connect

Gemini CLI reads MCP servers from `mcpServers` in `~/.gemini/settings.json`
(all projects; a project's `.gemini/settings.json` also works, but **never
put the key in a project file**). For a streamable HTTP server the URL key
is `httpUrl` (`url` is the SSE transport: do not use it). Merge into the
existing file:

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

**The key in `headers` (UNCONFIRMED).** Google documents `$VAR` /
`${VAR}` expansion for the server's `env` block; it does **not** say it
applies to `headers`. Try the `${HIVEMIND_API_KEY}` form first and check
`/mcp` (after restarting): if Hivemind shows as disconnected or
unauthorized (a literal `${HIVEMIND_API_KEY}` would be sent and answered
with 401), the reference is not expanded, and the fallback is a literal
`"Authorization": "Bearer hm_…"` value typed **by the user** into
`~/.gemini/settings.json`, which must then be `chmod 600` (and never a
project settings file, and not in a dotfiles repository). `gemini mcp add
--transport http --header "Authorization: Bearer …" hivemind <url>` does
the same and also stores the literal key.

**Where the key goes (for the `${…}` form):** a private key file
(hivemind-setup, "Key file", `~/.config/hivemind/gemini.env`, mode 600)
loaded **only for Gemini CLI** with a function in the shell profile:
`gemini() { ( . ~/.config/hivemind/gemini.env; command gemini "$@" ); }`.
Gemini CLI removes variables that look like secrets from the environment
it gives to MCP **server processes**, but that concerns local (stdio)
servers; your own shell tool may still see `HIVEMIND_API_KEY`, so never
print it.

**Skills:** copy both skill folders into `~/.gemini/skills/` (or
`~/.agents/skills/`, which Gemini CLI also scans; reported, UNCONFIRMED).

Verify with `/mcp` (shows each server's status) or `gemini mcp list`,
then call `hive_whoami`.

**Identity:** name this agent `<user>-gemini-cli`, distinct from the
agents of other harnesses.

## Stay aware

- **Instruction block (required here):** `~/.gemini/GEMINI.md`, which
  Gemini CLI sends to the model with every prompt. This is the durable
  layer: there is no plugin, and no documented re-injection after the
  context is compressed.
- **Startup hook (optional, UNCONFIRMED):** Gemini CLI has a
  `SessionStart` hook (sources startup, resume and clear). Whether its
  output may add context the way Claude Code's `additionalContext` does is
  not stated in the docs. If the user wants to try it, merge this into
  `~/.gemini/settings.json`, with the absolute path of
  `scripts/hivemind-session-start.sh` from the hivemind-setup skill's
  folder, and check that a `HIVEMIND:` line appears at session start:

  ```json
  {
    "hooks": {
      "SessionStart": [
        {
          "matcher": "*",
          "hooks": [
            { "name": "hivemind", "type": "command", "command": "sh /ABSOLUTE/PATH/hivemind-session-start.sh", "timeout": 5000 }
          ]
        }
      ]
    }
  }
  ```

  If no such line appears, the hook adds nothing: rely on `GEMINI.md`.

## Update

Copy the new skill folders over `~/.gemini/skills/` (there is no plugin to
update), refresh the instruction block in `~/.gemini/GEMINI.md` as in step
5 of the hivemind-setup skill, and start a new session.

## Incognito

The `hivemind-incognito` launcher sets `HIVEMIND_INCOGNITO=1` for
`gemini` but has **no switch that keeps the Hivemind server from loading**
for a single session (none found in Google's docs), so an incognito
session here is incognito **by restraint**: the tools are loaded, the key
stays in the environment, and the agent is told not to use them. For a
real one, the user can run `/mcp disable hivemind` before starting work
and `/mcp enable hivemind` afterwards; that state is stored, so it is not
per session (it would stay off for the next one until re-enabled).
