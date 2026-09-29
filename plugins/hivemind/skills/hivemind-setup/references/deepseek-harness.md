# Hivemind setup: DeepSeek Harness

Only for **DeepSeek Harness** (the `dsh` CLI or DSH Desktop). This is a
harness, not a model: a DeepSeek model running in OpenCode, Pi, Claude
Code or any other harness is **not** in DeepSeek Harness, and nothing in
this file applies to it. If you have not confirmed that this is your
harness, go back to "Which harness am I in?" in the hivemind-setup skill.

## Connect

- **With the hivemind bundle**: the easiest install is the Hivemind
  repository's Git URL, pasted into DeepSeek Harness's Plugins page (or
  `dsh plugin --profile <name> add git+https://<git-server>/<owner>/hivemind.git`);
  a local checkout also works (`dsh plugin --profile <name> add
  <path>/plugins/hivemind`). The bundle defines the MCP server from
  `HIVEMIND_MCP_URL` and `HIVEMIND_API_KEY` and serves both skills. The
  server stays off while `HIVEMIND_MCP_URL` is unset.
- **Where the key goes: `~/.dsh/.env`**, which DSH loads at startup
  however it is launched (CLI or Desktop). Show the user the change, and
  with approval write:

  ```sh
  HIVEMIND_MCP_URL=https://hivemind.example.org/mcp
  HIVEMIND_API_KEY=hm_…
  ```

  then `chmod 600 ~/.dsh/.env` and ask the user to restart DSH. Exporting
  both in the shell that launches `dsh` also works. **Never** put them in
  a project's `.env`: DSH loads that too, ranked higher, and a repository
  could use it to redirect the key. The bundle ignores the environment when
  it sees one there and uses `~/.dsh/.env` alone.
- **You cannot see the key from your shell.** DSH strips variables named
  like `*KEY*`/`*TOKEN*`/`*SECRET*`/`*PASSWORD*` from every process it
  starts, so `printenv HIVEMIND_API_KEY` is always empty here. Judge the
  connection by `hive_whoami`, not by the environment.
- **Without the bundle**: add this row to `~/.dsh/cordis.patch.yml` (all
  profiles) or `~/.dsh/profiles/<name>/cordis.patch.yml`, merging into
  the existing file:

  ```yaml
  - insert:
      - id: hivemind-mcp
        name: '@deepseek-ai/dsh-mcp-client'
        config:
          serverName: hivemind
          transport: streamable-http
          url: !!js process.env.HIVEMIND_MCP_URL
          headers:
            Authorization: !!js '`Bearer ${process.env.HIVEMIND_API_KEY}`'
  ```

  and copy both skill folders into `~/.agents/skills/` (DSH scans it).

## Stay aware

- **No hook needed.** DSH puts the Hivemind MCP server's instructions into
  the system prompt, which compaction never removes. (DSH's bridge for
  Claude Code hooks runs `SessionStart` only once, at session start, and
  its text does not survive compaction, so it is not the right tool here.)
- **Instruction block:** `~/.dsh/AGENTS.md`, which DSH keeps as a durable
  baseline.

## Update

Installed from a checkout: `git pull` in the checkout (the profile links
it). Installed from npm: `dsh plugin --profile <name> update
hivemind-dsh-plugin`. Then restart `dsh`.

## Incognito

`hivemind-incognito dsh`: the bundle's server row switches itself off
when `HIVEMIND_INCOGNITO` is set. Nothing else is needed.
