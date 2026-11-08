# Hivemind setup: Junie

Only for **Junie** (JetBrains' coding agent, the `junie` CLI; the notes on
the Junie plugin in JetBrains IDEs and on the JetBrains AI Assistant chat
are at the end). If you have not confirmed that this is your harness, go
back to "Which harness am I in?" in the hivemind-setup skill. Your system
prompt says "You are Junie, an autonomous programmer developed by
JetBrains".

Checked with Junie CLI 26.9.7. Junie is young: check anything this file
does not state with `/extensions` (the Installed tab) and the MCP view in
the Junie TUI rather than assuming.

## Connect

Junie reads Claude-style marketplaces, so it installs the same hivemind
plugin as Claude Code. The user installs it, in the Junie TUI, not you:

```text
/extensions marketplace add https://<git-server>/<owner>/hivemind
/extensions
```

Then search `hivemind`, choose Install and "User level". (A local path to
a clone of the repository and an `owner/repo` shorthand work too.) The
plugin is copied to `~/.junie/extensions/<marketplace>/hivemind/`. It
brings both skills and the two hooks under "Stay aware", and a
`guidelines/` file. Its `.mcp.json` is the Claude Code one, which reads
`${HIVEMIND_MCP_URL}`: **Junie does not expand `${VAR}` in a server's
`url` or `headers`**, so that server cannot connect (it tries
`localhost:80`, harmlessly, and sends no key). The server comes from the
user's own file instead.

**The server entry.** Junie reads MCP servers from `~/.junie/mcp/mcp.json`
(user level). This file has to hold the **literal** address and key:

```json
{"mcpServers": {"hivemind": {"url": "https://hivemind.example.org/mcp",
  "headers": {"Authorization": "Bearer hm_…"}}}}
```

The server **must be named `hivemind`**: the incognito launcher and the
guidelines refer to that name. Once the entry exists, Junie shows the
plugin's own server as "mcp (ignored due to conflict)", which is right.
Never put the key in a project's `.junie/mcp/mcp.json`: a project file is
for the repository, not for secrets.

**Where the key goes:** `~/.junie/mcp/mcp.json`, `chmod 600` the file and
`chmod 700 ~/.junie/mcp` (and `~/.junie`). Because Junie cannot read the
key from the environment, this is the one file that holds it. Show the
user the change first, and prefer that they write the key into it
themselves, without it passing through this chat. If they keep the key in
a private key file (hivemind-setup, "Key file", `~/.config/hivemind/junie.env`,
mode 600), this command builds the entry from it without printing the
key (it keeps any other servers already in the file):

```sh
mkdir -p ~/.junie/mcp && chmod 700 ~/.junie ~/.junie/mcp
python3 - <<'PY'
import json, os, re, shlex
env = {}
for line in open(os.path.expanduser("~/.config/hivemind/junie.env")):
    m = re.match(r"\s*(?:export\s+)?(\w+)=(.*)", line)
    if m:
        env[m[1]] = shlex.split(m[2])[0] if m[2].strip() else ""
path = os.path.expanduser("~/.junie/mcp/mcp.json")
try:
    cfg = json.load(open(path))
except (OSError, ValueError):
    cfg = {}
cfg.setdefault("mcpServers", {})["hivemind"] = {
    "url": env["HIVEMIND_MCP_URL"],
    "headers": {"Authorization": "Bearer " + env["HIVEMIND_API_KEY"]},
}
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as fh:
    json.dump(cfg, fh, indent=2)
os.chmod(path, 0o600)
PY
```

To switch to the agent key later, run the same command again after
updating the key file. Restart Junie after any change. Without a restart
the old key stays loaded; without a key the server answers 401, the
tools do not load, and the session carries on.

Junie shows the tools as `hivemind/hive_whoami` and so on; the model sees
them as `mcp_hivemind_hive_whoami`. They are the `hive_*` tools.

**Without the plugin** there are no skills, guidelines or hooks from
Hivemind (this file has not checked where Junie loads hand-copied skills
from), so install the plugin; the MCP entry above is needed either way.

Verify after a restart: call `hive_whoami`.

**Identity:** name this agent `<user>-junie`, distinct from the agents of
other harnesses.

## Stay aware

- **Guidelines:** the plugin ships `guidelines/hivemind.md`, which Junie
  puts in the main agent's guidelines on every task. It carries the
  `HIVEMIND:` stay-aware line, so it is the durable layer and survives a
  compaction. It also tells Junie's tool picker to always select the
  `hive_*` tools: before each step, Junie asks a separate model call
  which MCP tools the task needs, and a tool it leaves out is not offered
  at all. If the `hive_*` tools are missing from a task although the
  server is connected, check that the plugin is installed and that
  `guidelines/hivemind.md` is in the extension folder.
- **Hooks:** the plugin's `hooks/hooks.json` runs in Junie. The
  `SessionStart` hook runs, but Junie ignores the text it prints, so it
  adds nothing here (this is why the guidelines carry the line). The
  `Stop` hook works: at the end of a long task that rated nothing and
  either wrote nothing or read entries, it asks once to write and rate
  before finishing, and Junie continues with that as feedback. It reads
  Junie's session log, `~/.junie/sessions/<id>/events.jsonl`. Stop hooks
  do not run for chat-type tasks, so a chat gets no reminder.
- **Instruction block:** not needed. If you want one anyway, put it in the
  project's `.junie/AGENTS.md`; do not put a key in it.

Check that your guidelines show the `HIVEMIND:` line.

## Update

A Git extension is updated only when the user picks Update in the
Installed tab of `/extensions` (an extension from a local directory
refreshes when Junie starts). Then start a new session. If the Hivemind
address or key changed, edit `~/.junie/mcp/mcp.json` (or run the command
above again) and restart.

## Incognito

`hivemind-incognito junie` sets `HIVEMIND_INCOGNITO=1`, starts Junie with
`--mcp-default-locations=false` (it skips `~/.junie/mcp/mcp.json` and the
project's MCP file, so the hand-written `hivemind` entry is never read and
the plugin's own server cannot connect) and with
`--ide-guidelines=<plugin>/junie/incognito.md` (the incognito text, since
Junie ignores a `SessionStart` hook's output), and removes
`HIVEMIND_API_KEY` and `HIVEMIND_MCP_URL` from its environment. Tell the
user that the flag also turns off their **other** MCP servers for that
session. Under another server name the Hivemind tools can still load, and
the session is incognito **by restraint**.

## The Junie plugin in a JetBrains IDE, and the AI Assistant chat

Not verified; set these up by hand and say so.

- **Junie in a JetBrains IDE** (the plugin that shows as `idea`, `pycharm`
  and so on in the process list) has an MCP settings page that takes the
  same JSON as `mcp.json` above (literal URL and key), and reads
  `.junie/AGENTS.md`. Whether it loads the CLI's extensions, skills and
  hooks is unknown, so do not assume the guidelines or reminders are
  there: put the server in its MCP settings and the instruction block from
  the hivemind-setup skill into `.junie/AGENTS.md` (without a key). Keep the key out of
  any project file.
- **The JetBrains AI Assistant chat** has no plugins or hooks. It takes
  an MCP server under Settings | Tools | AI Assistant | Model Context
  Protocol, and project rules for instructions. Add the server by hand
  (the same literal URL and header) and put the instruction block in a
  rule. There are no skills and no reminders, and no incognito switch:
  disable the server in that settings page.
