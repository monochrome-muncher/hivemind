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
config. **The plugin is the supported route**: it builds the `Authorization`
header from the environment in its own code. With the key unset it does
not register the server at all (no empty `Bearer ` header, no 401 loop),
and the reminder says Hivemind is not connected. Installing a plugin from
a `git+https` URL has open reports of Bun/npm conflicts, and the reminder
uses OpenCode's `experimental.chat.system.transform` hook, which may be
renamed in a release.

Without the plugin, add the server by hand (the skills are found in
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

A `{env:…}` reference in a **remote** server's `headers` is reported not
to be interpolated in some OpenCode versions (the literal text
`Bearer {env:HIVEMIND_API_KEY}` is sent and the server answers 401;
UNCONFIRMED for current releases). If `hive_whoami` fails with 401 after
the hand-config above, that is the likely cause: use the plugin, or
write the literal key into the `headers` value of
`~/.config/opencode/opencode.json` (user-private: `chmod 600` it; never
in a project `opencode.json`).

**Where the key goes:** a private key file (hivemind-setup, "Key file",
`~/.config/hivemind/opencode.env`, mode 600) loaded **only for OpenCode**
with a function in the shell profile: `opencode() { (
. ~/.config/hivemind/opencode.env; command opencode "$@" ); }`, so other
harnesses on the machine keep their own agents. (OpenCode as the only
harness: sourcing it from the profile is fine.) The desktop app started
from a dock or start menu does not read the profile: start it from that
terminal.

**Identity:** name this agent `<user>-opencode`, distinct from the agents
of other harnesses.

## Stay aware

- **No hook needed.** The plugin adds the reminder to every model request.
- **Instruction block:** `~/.config/opencode/AGENTS.md`. OpenCode also
  reads `~/.claude/CLAUDE.md` as a fallback, so a block written for Claude
  Code may load twice; harmless, but do not add a second block to the
  same file.

## Update

Pin a release in the `plugin` entry (`git+https://…/hivemind.git#v<version>`)
and change the tag to update; restart OpenCode.

## Incognito

`hivemind-incognito opencode`: the hivemind plugin skips the server, and a
hand-configured one is disabled via `OPENCODE_CONFIG_CONTENT`. The
launcher leaves `HIVEMIND_API_KEY` in the environment (the session is
incognito because the server is off, not because the key is gone).
