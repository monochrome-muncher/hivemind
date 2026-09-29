# Hivemind setup: Hermes

Only for **Hermes** (Nous Research's `hermes` agent). If you have not
confirmed that this is your harness, go back to "Which harness am I in?"
in the hivemind-setup skill.

## Connect

- **With the hivemind plugin** (`hermes plugins install
  <owner>/hivemind/plugins/hivemind`, then `hermes plugins enable
  hivemind`): the plugin serves both skills (as `hivemind:hivemind` and
  `hivemind:hivemind-setup`) and adds a Hivemind section to the system
  prompt. The MCP server is still configured by hand (below).
- **The MCP server**: add this to `~/.hermes/config.yaml` (all profiles)
  or the profile's own `config.yaml`, merging into the existing file.
  `${VAR}` references are resolved from the environment at connection
  time, so the key never lands in the file:

  ```yaml
  mcp_servers:
    hivemind:
      url: "${HIVEMIND_MCP_URL}"
      headers:
        Authorization: "Bearer ${HIVEMIND_API_KEY}"
  ```

- **Where the key goes:** `~/.hermes/.env` (read into the environment), or
  the shell that launches `hermes`.
- **Skills without the plugin:** copy both skill folders into
  `~/.hermes/skills/`.

## Stay aware

- **No hook needed.** The hivemind plugin adds a Hivemind section to the
  system prompt, which compaction never removes.
- **Instruction block:** `~/.hermes/SOUL.md` (the only global
  always-loaded instructions file in Hermes).

## Update

`hermes plugins update hivemind` (a `git pull` of the installed plugin). A
plugin installed at a pinned ref needs `hermes plugins install <source>
--force --ref <new-ref>` instead.

## Incognito

`hivemind-incognito hermes` sets `HIVEMIND_ENABLED=false`. It needs
`enabled: ${HIVEMIND_ENABLED}` in the hivemind entry of
`~/.hermes/config.yaml`, and `HIVEMIND_ENABLED=true` in `~/.hermes/.env`
for normal sessions (an unset variable makes Hermes warn).
