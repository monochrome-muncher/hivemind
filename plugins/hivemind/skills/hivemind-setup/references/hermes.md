# Hivemind setup: Hermes

Only for **Hermes** (Nous Research's `hermes` agent). If you have not
confirmed that this is your harness, go back to "Which harness am I in?"
in the hivemind-setup skill.

## Connect

- **With the hivemind plugin** (`hermes plugins install
  <owner>/hivemind/plugins/hivemind`, then `hermes plugins enable
  hivemind`): the plugin serves both skills (as `hivemind:hivemind` and
  `hivemind:hivemind-setup`; namespaced plugin skills are not in the
  skill index) and registers a Hivemind system-prompt section. **Do not
  rely on that section**: current Hermes releases never render
  registered sections (upstream issue NousResearch/hermes-agent#117432,
  closed "not planned"; UNCONFIRMED whether a later release fixed it).
  The marked block in `SOUL.md` (Stay aware, below) is **required**. The
  MCP server is still configured by hand (below).
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

- **Where the key goes:** Hermes's own env file, which is specific to
  Hermes, so its agent identity stays separate from other harnesses:
  `~/.hermes/.env` for the default profile, or
  `~/.hermes/profiles/<name>/.env` for a named profile. Ask the user to
  add the two variables there themselves (hivemind-setup, "Key file": the
  same `read` command, appending `HIVEMIND_API_KEY=…` lines without
  `export`), then `chmod 600` the file. Never a project-level file.
- **Skills without the plugin, or so they appear in the skill index:**
  copy both skill folders into `~/.hermes/skills/` (in addition to, or
  instead of, the namespaced plugin skills).
- After editing the MCP config or the env file, `/reload-mcp` reloads the
  servers without a full restart; if `hive_whoami` still shows the old
  key, restart Hermes.

**Identity:** name this agent `<user>-hermes`, distinct from the agents of
other harnesses.

## Stay aware

- **The instruction block is required here**, not optional: put it in
  `~/.hermes/SOUL.md` (the always-loaded file for the profile; the only
  global always-loaded instructions file in Hermes) as part of Connect.
  Verify it reached you: start a new session and quote the line in the
  block that starts `Hivemind`; if you cannot, the file is not being
  loaded, tell the user.
- **No hook.** The plugin's system-prompt section is kept for Hermes
  versions that render it, but do not claim it works.

## Update

`hermes plugins update hivemind` (a `git pull` of the installed plugin). A
plugin installed at a pinned ref needs `hermes plugins install <source>
--force --ref <new-ref>` instead.

## Incognito

`hivemind-incognito hermes` sets `HIVEMIND_ENABLED=false`. It needs
`enabled: ${HIVEMIND_ENABLED}` in the hivemind entry of
`~/.hermes/config.yaml`. **Never put `HIVEMIND_ENABLED` in the Hermes env
file** (`~/.hermes/.env` or a profile's `.env`): Hermes loads that file
over the environment, so a value there overrides the launcher and the
server stays on in incognito sessions. For normal sessions leave it unset
(Hermes then keeps the server on, logging a warning about the unexpanded
`${HIVEMIND_ENABLED}`), or export `HIVEMIND_ENABLED=true` in the shell
profile to silence it. The launcher leaves `HIVEMIND_API_KEY` in place
(Hermes may still resolve it), so this session is incognito by the server
being disabled, and the key is still in the environment.
