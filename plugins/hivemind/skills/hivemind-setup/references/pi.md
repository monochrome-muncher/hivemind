# Hivemind setup: Pi

Only for **Pi** (the `pi` coding agent). Oh My Pi (`omp`) is a separate
harness with its own file. If you have not confirmed that this is your
harness, go back to "Which harness am I in?" in the hivemind-setup skill.

## Connect

Install the hivemind **Pi package** first: it provides the two skills, the
system-prompt reminder and, on Pi 0.99 and later, the MCP connection.
Install it from the Hivemind repository's Git URL (`pi install
git:<git-server>/<owner>/hivemind`, or `pi install
https://<git-server>/<owner>/hivemind`; append `@v<version>` to pin a
release), or from a checkout (`pi install /path/to/hivemind/plugins/hivemind`).
Check the version with `pi --version`; there are two routes.

**Pi 0.99 and later (built-in MCP; recommended).** Pi has its own MCP
client, and the package registers the Hivemind server with it from
`HIVEMIND_MCP_URL` and `HIVEMIND_API_KEY`, with its tools declared by
name (`mcp__hivemind__hive_whoami`, …). Only set the two variables
(below) and restart Pi. Nothing else to configure; `/mcp` shows the
server. A `hivemind` entry in `~/.pi/agent/mcp.json` takes precedence
over the package's registration, so remove a hand-written one unless you
need it.
Without the package, add the server to `~/.pi/agent/mcp.json` yourself
(the key stays in the environment):

```json
{
  "mcpServers": {
    "hivemind": {
      "url": "${HIVEMIND_MCP_URL}",
      "headers": { "Authorization": "Bearer ${HIVEMIND_API_KEY}" },
      "exposure": "direct"
    }
  }
}
```

**Older Pi, or Pi with pi-mcp-adapter installed** (the adapter replaces
Pi's built-in MCP client). The connection comes from the standard MCP
config files that **pi-mcp-adapter** (`pi install npm:pi-mcp-adapter`)
reads, and the package registers nothing when one of them defines
`hivemind`. `${VAR}` references are expanded at connection time, so the
key never lands in the file:

- **User-global** (all projects): `~/.config/mcp/mcp.json` (or
  `~/.agents/mcp.json`):

  ```json
  {
    "mcpServers": {
      "hivemind": {
        "type": "http",
        "url": "${HIVEMIND_MCP_URL}",
        "headers": { "Authorization": "Bearer ${HIVEMIND_API_KEY}" },
        "lifecycle": "eager",
        "directTools": true
      }
    }
  }
  ```

  `lifecycle` and `directTools` are pi-mcp-adapter settings. Its defaults
  are **lazy** (the server connects on the first call) and **through one
  `mcp` proxy tool** (`mcp({ tool: "hivemind_hive_whoami" })`,
  `mcp({ search: "hive" })`), so with the defaults the `hive_*` tools do
  not show up in your tool list even though Hivemind is configured.
  `eager` connects at startup and `directTools: true` registers the tools
  by name. After editing the file, reload Pi's tools (`/reload`) or
  restart.
- **Project**: the same file as `.mcp.json` at the repository root (no
  key in it: only `${VAR}` references; the adapter asks the user to
  approve a project-scoped server).

On Pi 0.99 and later the adapter is no longer needed: to switch, remove
it (`pi remove npm:pi-mcp-adapter`) and the `hivemind` entry from its
config, then restart.

**Do not conclude the tools are missing just because they are not
listed.** Before sending the user to "Connect", try calling `hive_whoami`
(directly, or through the adapter's proxy: `mcp({ tool:
"hivemind_hive_whoami" })`).

**Where the key goes:** a private key file (hivemind-setup, "Key file",
`~/.config/hivemind/pi.env`, mode 600) loaded **only for Pi** with a
function in the shell profile: `pi() { ( . ~/.config/hivemind/pi.env;
command pi "$@" ); }`, so other harnesses on the machine keep their own
agents. (Pi as the only harness: sourcing it from the profile is fine.)
Never a key in a project `.mcp.json`.

**Identity:** name this agent `<user>-pi`, distinct from the agents of
other harnesses.

## Stay aware

- **No hook needed.** The Pi package adds a Hivemind section to the system
  prompt, which compaction never removes.
- **Instruction block:** `~/.pi/agent/AGENTS.md`.
- **Skills without the package:** copy both skill folders into
  `~/.pi/agent/skills/`.

## Update

Installed from a local path: `git pull` in that checkout (Pi loads it in
place). Installed from git or npm: `pi update --extensions`; a pinned tag
stays put, so reinstall at the new tag.

## Incognito

`hivemind-incognito pi` removes `HIVEMIND_API_KEY` and `HIVEMIND_MCP_URL`
from the session's environment. With the built-in MCP client the package
then registers no server (it also skips registering whenever
`HIVEMIND_INCOGNITO` is set). With pi-mcp-adapter installed (the launcher
looks for it in Pi's `settings.json`), it starts Pi with an exclusive MCP
config instead: the user's global and project servers, minus hivemind
(the merged file is private and deleted when Pi exits; this needs
`python3`). Either way the package's extension also blocks Hivemind
calls: `mcp__hivemind…` tools (including calls made from `codemode`
scripts), the adapter's `mcp` proxy tool when its routing fields
(`server`, `connect`, `instructions`, `tool`, `describe`) name the
Hivemind server or one of its tools (never its `args`), and `mcpScript`
code that calls `tools.hivemind_…` or a `hive_*` name. The `mcpScript`
check is best effort (code can build a tool name at run time).
