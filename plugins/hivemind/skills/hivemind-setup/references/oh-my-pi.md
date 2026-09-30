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

**Where the key goes:** a private key file (hivemind-setup, "Key file",
`~/.config/hivemind/omp.env`, mode 600) loaded **only for Oh My Pi** with
a function in the shell profile: `omp() { ( . ~/.config/hivemind/omp.env;
command omp "$@" ); }`, so other harnesses on the machine keep their own
agents. (Oh My Pi as the only harness: sourcing it from the profile is
fine.) Never a key in a project `.omp/mcp.json`.

**Identity:** name this agent `<user>-omp`, distinct from the agents of
other harnesses.

**Duplicate-server risk (UNCONFIRMED).** Oh My Pi's marketplace plugins
can load a plugin's MCP servers too, and the hivemind plugin ships a
`.mcp.json` with the same `${HIVEMIND_*}` references at its root. After
installing, list the MCP servers (`/mcp` or the equivalent): if Hivemind
appears twice (two connections, duplicate `hive_*` routes), keep one:
delete the hand-written `hivemind` entry from `~/.omp/agent/mcp.json`, or
tell the user not to add both. Also check the installed version: some
`omp` releases (18.1.5 and later, reported) hide the skills of
user-scope marketplace plugins unless `claude-plugins` is in
`enabledProviders`; if the skills are missing, check that setting.

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
unavailable; twice if the server is registered twice, see above); the
extension also blocks any Hivemind call, including the `xd://` routes
with a trailing slash or a `?query`. It needs the hivemind plugin.
