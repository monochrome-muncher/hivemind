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
        "headers": { "Authorization": "Bearer ${HIVEMIND_API_KEY}" }
      }
    }
  }
  ```

- **Project**: the same file as `.mcp.json` at the repository root.

The hivemind **Pi package** provides the two skills and the system-prompt
reminder; it does not configure the MCP server. Install it from the
Hivemind repository's Git URL (`pi install git:<git-server>/<owner>/hivemind`,
or `pi install https://<git-server>/<owner>/hivemind`; append `@v<version>`
to pin a release), or from a checkout
(`pi install /path/to/hivemind/plugins/hivemind`).

**Where the key goes:** export both variables in the shell that launches
`pi` (see "Shell profile" in the hivemind-setup skill).

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
global and project servers, minus hivemind. It needs `python3`.
