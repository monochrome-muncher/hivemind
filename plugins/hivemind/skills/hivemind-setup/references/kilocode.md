# Hivemind setup: KiloCode

Only for **KiloCode** (the `kilo` CLI, or the Kilo Code VS Code / JetBrains
extension). If you have not confirmed that this is your harness, go back to
"Which harness am I in?" in the hivemind-setup skill.

## Connect

KiloCode 7.x is a fork of OpenCode, so the hivemind **OpenCode plugin**
works in it unchanged. The plugin registers the Hivemind MCP server from
`HIVEMIND_MCP_URL` and `HIVEMIND_API_KEY`, adds the two skills, and puts
the reminder into every model request. Add the Hivemind repository to the
`plugin` list in `~/.config/kilo/kilo.jsonc` (all projects) or a project's
`kilo.jsonc`, then export the two variables and restart KiloCode:

```jsonc
{
  "plugin": [
    "git:https://<git-server>/<owner>/hivemind@v<version>#plugins/hivemind/opencode/hivemind.js"
  ]
}
```

KiloCode's `git:` spec is `git:<repo>[@ref][#subpath]`: it clones the
repository and loads the file at `#subpath`. Pin a release tag (`@v<version>`)
as shown: a pinned ref is never refreshed, so changing the tag is how it
updates; a branch ref is re-checked about hourly.

The `plugin` key is the v1 plugin API, which KiloCode keeps and which gives
the full feature set (the server, the reminder, the end-of-session and
post-compaction nudges). The `plugins` key is the v2 API; it loads the same
file's `setup`, which registers only the skills, so prefer `plugin`.

**Where the key goes:** a private key file (hivemind-setup, "Key file",
`~/.config/hivemind/kilocode.env`, mode 600) loaded **only for KiloCode**
with a function in the shell profile: `kilo() { (
. ~/.config/hivemind/kilocode.env; command kilo "$@" ); }`, so other
harnesses on the machine keep their own agents. The VS Code / JetBrains
extension started from a dock or start menu does not read the profile:
start it from a terminal that has the two variables exported.

**Identity:** name this agent `<user>-kilocode`, distinct from the agents
of other harnesses.

## Stay aware

- **No hook needed.** The plugin adds the reminder to every model request.
  It also asks you once, when a long session that rated nothing and wrote nothing or read entries ends, to
  write and rate before finishing, and after a compaction to write what you
  learned before it.
- **Instruction block:** `~/.config/kilo/AGENTS.md`.

## Update

Change the tag in the `plugin` entry (`git:…/hivemind@v<version>#…`) to the
new release, then restart KiloCode. A pinned entry is never refreshed, so
the tag change is the update; a branch entry updates on its own about
hourly.

## Incognito

`hivemind-incognito kilo` (or `kilocode`): a hand-configured server is
disabled for the run via `KILO_CONFIG_CONTENT`, and the plugin skips the
server it would register. The launcher leaves `HIVEMIND_API_KEY` in the
environment (the session is incognito because the server is off, not
because the key is gone).
