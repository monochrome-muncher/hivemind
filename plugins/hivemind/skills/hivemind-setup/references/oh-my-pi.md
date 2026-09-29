# Hivemind setup: Oh My Pi

Only for **Oh My Pi** (`omp`, a fork of Pi). If you have not confirmed
that this is your harness, go back to "Which harness am I in?" in the
hivemind-setup skill.

## Connect

Oh My Pi has **built-in MCP**: add the server to `~/.omp/agent/mcp.json`
(or `.omp/mcp.json` in a project). `${VAR}` references are expanded at
startup, so the key never lands in the file:

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

Install the hivemind plugin through Oh My Pi's marketplace support, which
reads the Hivemind repository as a Claude Code marketplace:

```sh
omp plugin marketplace add <git-url-of-the-hivemind-repo>
omp plugin install hivemind@hivemind
```

It provides the two skills and the system-prompt reminder. (A plain
`omp install <git-url>` is not enough: Oh My Pi only finds a package's
skills in a top-level `skills/` folder.)

**Where the key goes:** export both variables in the shell that launches
`omp` (see "Shell profile" in the hivemind-setup skill).

Oh My Pi mounts MCP tools as routes (`xd://mcp__hivemind_hive_*`) rather
than as named tools; they are listed in your system prompt.

## Stay aware

- **No hook needed.** The plugin's extension adds a Hivemind section to
  the system prompt, which compaction never removes.
- **Instruction block:** `~/.omp/agent/AGENTS.md`.

## Update

`omp plugin marketplace update hivemind`, then `omp plugin upgrade` (or
`omp plugin install hivemind@hivemind --force`).

## Incognito

`hivemind-incognito omp` unsets `HIVEMIND_MCP_URL` and `HIVEMIND_API_KEY`,
so the server is never contacted (Oh My Pi warns once that it is
unavailable); the extension also blocks any Hivemind call. It needs the
hivemind plugin.
