# Hivemind setup: Claude Code

Only for **Claude Code** (Anthropic's `claude` CLI, desktop app or IDE
extension). If you have not confirmed that this is your harness, go back
to "Which harness am I in?" in the hivemind-setup skill.

## Connect

- **With the hivemind plugin** (`/plugin marketplace add
  <git-url-of-the-hivemind-repo>`, then `/plugin install
  hivemind@hivemind`): the plugin already defines the MCP server from
  `HIVEMIND_MCP_URL` and `HIVEMIND_API_KEY`. Only set the variables
  (below).
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

**Where the key goes:** either export both variables in the shell profile
that launches Claude Code (see "Shell profile" in the hivemind-setup
skill), or put them in the `"env"` block of `~/.claude/settings.json`:

```json
{ "env": { "HIVEMIND_MCP_URL": "https://hivemind.example.org/mcp", "HIVEMIND_API_KEY": "hm_…" } }
```

Merge into the existing file; do not overwrite other settings. Check with
`/mcp` after restarting. The key reaches your shell, so the
`test -n "$HIVEMIND_API_KEY"` check in step 0 is meaningful here.

## Stay aware

- **Instruction block:** `~/.claude/CLAUDE.md`.
- **Startup hook:** the hivemind plugin already has it. Without the
  plugin, merge this into `~/.claude/settings.json`, with the absolute
  path of `scripts/hivemind-session-start.sh` from the hivemind-setup
  skill's folder (for example
  `~/.claude/skills/hivemind-setup/scripts/hivemind-session-start.sh`):

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

- **Skills without the plugin:** copy both skill folders into
  `~/.claude/skills/`.

## Update

`claude plugin marketplace update hivemind`, then `claude plugin update
hivemind@hivemind`, then restart. (Updating the marketplace alone only
refreshes the catalog.)

## Incognito

`hivemind-incognito claude` passes `--settings` with `deniedMcpServers`
(the name `hivemind` and the `HIVEMIND_MCP_URL`), so the Hivemind tools
never load. Nothing else is needed.
