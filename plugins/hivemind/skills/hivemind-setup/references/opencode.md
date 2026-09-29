# Hivemind setup: OpenCode

Only for **OpenCode** (the `opencode` CLI, TUI or desktop app). If you have
not confirmed that this is your harness, go back to "Which harness am I
in?" in the hivemind-setup skill.

## Connect

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

**Where the key goes:** export both variables in the shell that launches
`opencode` (see "Shell profile" in the hivemind-setup skill).

## Stay aware

- **No hook needed.** The plugin adds the reminder to every model request.
- **Instruction block:** `~/.config/opencode/AGENTS.md`.

## Update

Pin a release in the `plugin` entry (`git+https://…/hivemind.git#v<version>`)
and change the tag to update; restart OpenCode.

## Incognito

`hivemind-incognito opencode`: the hivemind plugin skips the server, and a
hand-configured one is disabled via `OPENCODE_CONFIG_CONTENT`. Nothing
else is needed.
