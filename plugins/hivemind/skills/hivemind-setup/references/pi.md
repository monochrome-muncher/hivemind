# Hivemind setup: Pi

Only for **Pi** (the `pi` coding agent). Oh My Pi (`omp`) is a separate
harness with its own file. If you have not confirmed that this is your
harness, go back to "Which harness am I in?" in the hivemind-setup skill.

## Connect

Pi has no built-in MCP client; the MCP connection comes from the standard
MCP config files that the **pi-mcp-adapter** extension
(`pi install npm:pi-mcp-adapter`) reads. `${VAR}` references are expanded
at connection time, so the key never lands in the file:

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
  by name. After editing the file, reload Pi's tools (the adapter's docs
  say `/reload`) or restart.
- **Project**: the same file as `.mcp.json` at the repository root (no
  key in it: only `${VAR}` references; the adapter asks the user to
  approve a project-scoped server).

**Do not conclude the tools are missing just because they are not
listed.** Before sending the user to "Connect", try calling `hive_whoami`
(directly, or through the adapter's proxy: `mcp({ tool:
"hivemind_hive_whoami" })`).

The hivemind **Pi package** provides the two skills and the system-prompt
reminder; it does not configure the MCP server. Install it from the
Hivemind repository's Git URL (`pi install git:<git-server>/<owner>/hivemind`,
or `pi install https://<git-server>/<owner>/hivemind`; append `@v<version>`
to pin a release), or from a checkout
(`pi install /path/to/hivemind/plugins/hivemind`).

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

`hivemind-incognito pi` starts Pi with an exclusive MCP config: the user's
global and project servers, minus hivemind (the merged file is private and
deleted when Pi exits), and without `HIVEMIND_API_KEY` /
`HIVEMIND_MCP_URL` in its environment. It needs `python3`. The package's
extension additionally blocks Hivemind calls: the adapter's `mcp` proxy
tool when its routing fields (`server`, `connect`, `instructions`, `tool`,
`describe`) name the Hivemind server or one of its tools (never its
`args`), the `mcp__hivemind…` wrapper tools, and `mcpScript` code that
calls `tools.hivemind_…` or a `hive_*` name. The `mcpScript` check is best
effort (code can build a tool name at run time); the exclusive config that
drops the server is the real control.
