# Hivemind setup: OpenCode

Only for **OpenCode** (the `opencode` CLI, TUI or desktop app). If you have
not confirmed that this is your harness, go back to "Which harness am I
in?" in the hivemind-setup skill.

## Connect

The hivemind **OpenCode plugin** does the whole setup itself: it registers
the Hivemind MCP server from `HIVEMIND_MCP_URL` and `HIVEMIND_API_KEY`, adds
the two skills, and puts the reminder into every model request. It supports
both OpenCode v1 and v2 (the two plugin APIs differ; the plugin carries
both). Add the Hivemind repository's Git URL to the plugin list in
`~/.config/opencode/opencode.json` (all projects) or a project's
`opencode.json`, then export the two variables and restart OpenCode:

OpenCode v1 uses the `plugin` key; v2 uses `plugins` and migrates a `plugin`
entry automatically. Write `plugin` — it works on both (v1 does not read
`plugins`; an entry under it is only warned about, not loaded):

```json
{ "plugin": ["git+https://<git-server>/<owner>/hivemind.git#v<version>"] }
```

Pin a release tag as shown: OpenCode caches a Git plugin on first use and
never refreshes an unpinned entry, so changing the tag is how it updates.

Or run `opencode plugin -g git+https://<git-server>/<owner>/hivemind.git#v<version>`
(v1) / `opencode plugin add` with the same URL (v2, which always writes the
global config), which installs it and updates the config (on v1, without `-g` it writes the current project's
`.opencode/opencode.json`, so Hivemind would load only in that project).
**The plugin is the supported route**: it builds the `Authorization`
header from the environment in its own code. With the key unset it does
not register the server at all (no empty `Bearer ` header, no 401 loop),
and the reminder says Hivemind is not connected. Installing a plugin from
a `git+https` URL has open reports of Bun/npm conflicts.

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

OpenCode expands `{env:…}` in a remote server's `headers` (checked with
OpenCode 1.18), so the key never needs to be written into the file.

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
  It also asks you once, when a long session that rated nothing and wrote nothing or read entries goes
  idle, to write and rate before finishing (not in `opencode run`), and
  after a compaction to write what you learned before it.
- **Instruction block:** `~/.config/opencode/AGENTS.md`. OpenCode also
  reads `~/.claude/CLAUDE.md` as a fallback, so a block written for Claude
  Code may load twice; harmless, but do not add a second block to the
  same file.

## Update

Change the tag in the `plugin` entry (`git+https://…/hivemind.git#v<version>`,
under `plugin` on v1 and migrated to `plugins` on v2) to the new release,
then restart OpenCode. An unpinned entry never updates: pin it, or delete
OpenCode's cached copy under `~/.cache/opencode/` and restart.

## Incognito

`hivemind-incognito opencode`: the hivemind plugin skips the server, and a
hand-configured one is disabled via `OPENCODE_CONFIG_CONTENT`. The
launcher leaves `HIVEMIND_API_KEY` in the environment (the session is
incognito because the server is off, not because the key is gone).
