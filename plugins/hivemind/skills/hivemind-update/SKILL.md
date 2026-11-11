---
name: hivemind-update
description: Check the installed Hivemind plugin, and the Hivemind instructions copied into your own files, for drift from the latest release, then update them the way your harness needs. Use when the user asks to update Hivemind or check it for drift, or has just updated the plugin.
---

# Hivemind update

Two things fall behind a new Hivemind release in different ways:

- **the plugin**, which each harness installs, and so updates, its own
  way: a marketplace command, a reinstall at a new tag, a version pinned
  in a config entry, a `git pull`, a button in the harness, or files
  copied by hand;
- **your copies**: the instruction block, rules, hooks and skill folders
  that setup wrote outside the plugin. Nothing updates them.

The server's own part (tool descriptions, `hive_whoami`) updates when the
admin upgrades the server, so it is not covered here.

**Ground rules:**

- **Use only your own harness's instructions**, in `references/` next to
  this `SKILL.md`. Another harness's update command does not work in
  yours, and the wrong step can lose settings the harness stores with
  the plugin.
- **Show every change to a file outside the current project before
  making it, and ask first.** Afterwards, say which file changed and how
  to undo it.
- **Never print or copy a key.** No step here touches one: if a file you
  are about to change holds a key, leave that part alone.
- **The check only reads.** It fetches the latest release to compare
  with and never runs anything from it.

## 1. Which harness am I in?

Decide as the hivemind-setup skill's "Which harness am I in?" says: from
your system prompt, your tools and the processes above your shell, never
from your model. If you have not done that in this session, load that
skill and follow that section. If the evidence is missing or disagrees,
ask the user.

| Harness | Its instructions |
|---|---|
| Claude Code | `references/claude-code.md` |
| Codex | `references/codex.md` |
| GitHub Copilot CLI (`copilot`) | `references/copilot-cli.md` |
| Cursor (`cursor`, `cursor-agent`) | `references/cursor.md` |
| Gemini CLI | `references/gemini-cli.md` |
| Antigravity (`agy`, the App or the IDE) | `references/antigravity.md` |
| DeepSeek Harness (`dsh`) | `references/deepseek-harness.md` |
| Hermes | `references/hermes.md` |
| Junie (`junie`) | `references/junie.md` |
| Pi | `references/pi.md` |
| Oh My Pi (`omp`) | `references/oh-my-pi.md` |
| OpenCode | `references/opencode.md` |
| Cline | `references/cline.md` |
| KiloCode (`kilo`) | `references/kilocode.md` |

(Hermes: `skill_view("hivemind:hivemind-update",
file_path="references/hermes.md")`.) Any other harness: run the check
with `--block` on the file that holds your instruction block and
`--skills` on the folder you copied the skills into, update by copying
the new skill folders over the old ones, and refresh the block as in
step 4.

## 2. Check for drift

Run the **Check** command from your harness's file, where `<skill>` is
this skill's folder (the one holding this `SKILL.md`). It runs
`scripts/hivemind-drift.sh`, which compares with the newest release tag
of the Hivemind repository (it needs `git` and network access) and
reports:

| Line | Meaning | Next |
|---|---|---|
| `Installed plugin: …: behind` | an older release is installed | step 3 |
| `Installed plugin: …: current`, with `differs` or `missing` lines | the install was changed or is incomplete | step 3, reinstalling |
| `newer than the release compared with` | installed from a branch or a newer tag | nothing, unless the user wants the release; compare your copies with that branch with `--ref <branch>` |
| `Instruction block in …: differs`, or a count of markers | your block is old or doubled | step 4 |
| `Instruction block in …: none` | no block in that file | your harness's file says whether one belongs there |
| `Skill copies in …` or `Copy … of …` with `differs`, `missing` or `only in your copy` | hand-copied files are old | step 4 |
| `No drift.` | everything matches the latest release | tell the user, and stop |

**Where it looks.** The `Latest:` line names the repository it compared
with and why: `--repo` if given; else the installed plugin's own git
remote, when the plugin is a clone of the repository; else the first
line of `~/.config/hivemind/plugin-source`; else the public repository
on GitHub. Any git address works (a self-hosted server, an internal
mirror, `ssh://`, `file://` or a local path). If the organization hosts
Hivemind somewhere else and the plugin is not a clone, ask the user for
that address and, with approval, save it in that file so later checks
use it; your harness's file says where the install source is recorded.
A mirror needs the release tags; without them, pass `--ref <branch>`.
With no route to any git server, ask for a checkout or copy of the
repository and pass `--latest DIR`.

Exit status 2 means it could not compare: no `git`, the address
unreachable, or no release tag there. The message says which source it
tried.

Show the user the report, including the differences in the blocks,
before you change anything. The plugin's `CHANGELOG.md` (in the new
release) says per release what changed and which copies need refreshing.

## 3. Update the plugin

Follow the **Update** section of your harness's file exactly. Many
updates are commands or clicks for the user, or need a restart; say
what to do and wait for them. Where the version is pinned (a tag in a
config entry, a fixed ref), the update is changing the pin to the tag the
check printed on its `Latest:` line.

Afterwards run the check again, with `--installed` if the plugin now
lives in a new folder. Some harnesses load the new plugin only after a
restart; then check again in the new session.

## 4. Refresh your copies

For each copy the check reported, with the user's approval:

- **Instruction block:** replace the whole marked block, from
  `<!-- hivemind:begin -->` to `<!-- hivemind:end -->`, with the output of
  the check command plus `--print-block`. Leave the rest of the file
  alone. One block per file: remove any extra.
- **Copied skill folders and files:** run the check with `--keep DIR` (a
  new folder) to keep the fetched release, copy the plugin's files from
  `DIR/plugins/hivemind/` over your copies, and delete files the check
  lists as `only in your copy` inside the Hivemind skill folders (the
  release removed them). Then delete `DIR`.
- **Anything your harness's file says no longer belongs** (for example
  a block that the plugin now provides): remove it.

Run the check once more. It should end with `No drift.`

## 5. Finish

1. Tell the user to restart the harness, or at least start a new
   session: a running session keeps the instructions it loaded.
2. Report exactly what you changed, file by file, and how to undo it.

**Without `sh`** (native Windows without Git Bash or WSL): compare by
hand. The latest block is in step 4 of the hivemind-setup skill of the
new release; your harness's file lists the copies to compare.
