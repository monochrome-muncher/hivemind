# Hivemind setup: Antigravity

Only for **Antigravity** (Google Antigravity): the `agy` CLI, the Antigravity App and the
Antigravity IDE, which share one plugin folder (`~/.gemini/config/plugins/`).
Not Gemini CLI, which has its own file. If you have not confirmed that this
is your harness, go back to "Which harness am I in?" in the hivemind-setup
skill. Antigravity's system prompt starts "You are Antigravity".

Checked with the Antigravity CLI 1.5.3 and 1.5.5. Check anything this
file does not state with `agy plugin list` and `/plugin` rather than assuming.

## Connect

**With the hivemind plugin** (the only supported route). The user installs
a copy of the plugin's folder from the Hivemind repository at a release
tag (`<address>` is the repository's Git URL, for example
`https://github.com/<owner>/hivemind`). It is a shell command for them to
run, not you:

```sh
git clone --depth 1 --branch <tag> <address> /tmp/hivemind-<tag>
cp -RL /tmp/hivemind-<tag>/plugins/antigravity /tmp/hivemind-antigravity
agy plugin install /tmp/hivemind-antigravity
rm -rf /tmp/hivemind-<tag> /tmp/hivemind-antigravity
```

The plugin's `skills` is a link to the skills it shares with the other
harnesses, and Antigravity 1.5.8 refuses to install a folder holding a link
that leads out of it ("symlink … points outside"), from a GitHub link too;
`cp -RL` puts real files in its place. Antigravity copies the folder, so
both copies can go afterwards. Do not install the repository root or use
`agy plugin import`: Antigravity converts the Gemini CLI or Claude Code
copy there, with hooks that do not work.

Antigravity keeps no record of that address, so unless it is the public
GitHub repository, with approval save it
as the only line of `~/.config/hivemind/plugin-source` (folder mode 700,
no password or token in it): the hivemind-update skill compares with it.

The plugin (named `hivemind` once installed) brings the MCP server, the
skills, an always-on rule and the two hooks under "Stay aware". It installs **turned off**, because its server
needs two values. The user turns it on in `/plugin` (CLI: select hivemind,
press Space) or under Customizations > Installed (App), and fills in the
Hivemind MCP URL and the key there. Ask the user to type the key
themselves: the field shows it as typed, so it should not pass through this
chat. Antigravity stores both values in `~/.gemini/config/config.json`
(mode 600), not in the environment, so `HIVEMIND_API_KEY` is unset in your
shell even when Hivemind works.

The tools are loaded lazily: the system prompt lists them under
`# hivemind_hivemind`, and you call one with `call_mcp_tool` (`ServerName`
`hivemind_hivemind`, `ToolName` such as `hive_whoami`, `Arguments` an
object), after reading its schema file if you need the arguments. The
server's own instructions are only in a file next to the schemas, so the
skill and the rule carry the rules.

**A workspace can bring its own plugin.** Antigravity also loads plugins
from a project's `.agents/plugins/`, and fills a plugin named `hivemind`
there with the values the user stored for theirs. A cloned repository with
`.agents/plugins/hivemind/` can send the key to a URL of its own (checked
live with 1.5.3). Check that folder before the user trusts a project, and
use `hivemind-incognito agy` in repositories you do not trust.

Verify after a restart with `agy plugin list` (hivemind should be
`enabled`, with 1 MCP server), then call `hive_whoami`.

**Switching the key** (step 3 of the skill): the user opens `/plugin`,
expands hivemind, chooses "Configure MCP servers..." and enters the agent
key, then starts a new session. That is the only place the key lives, so
there is nothing else to clean up.

**Identity:** name this agent `<user>-antigravity`, distinct from the
agents of other harnesses.

## Stay aware

- **Rule:** the plugin's `rules/AGENTS.md` is an always-on rule, the same
  text as the instruction block in step 4 of the skill. Antigravity puts it
  in the system prompt of every session, so no block in `~/.gemini/AGENTS.md`
  is needed. Remove one there if an older setup added it.
- **Hooks:** a `SessionStart` hook puts the `HIVEMIND:` reminder in front of
  the first prompt, and a `Stop` hook asks once, at the end of a long session
  that rated nothing and either wrote nothing or read entries, to write and
  rate before finishing (both checked live with 1.5.3, also in `agy -p`).
  Antigravity has no compaction event, so there is no post-compaction line;
  the rule carries that.

## Incognito

`hivemind-incognito agy` sets `HIVEMIND_INCOGNITO=1` (the hooks switch to
their incognito text) and passes a session-only permission rule that denies
every `hivemind_hivemind` tool call. The server still connects at startup,
with the stored key, because Antigravity has no per-session switch for a
plugin: the server sees a connection, but nothing is read or written. For a
session the server never hears about, the user turns the plugin off in
`/plugin` first and on again afterwards (that state is stored, not per
session).
