# The plugin's update skill checks for drift against the latest release

Builds on [ADR 0085](0085-the-mcp-package-splits-into-contract-server-and-runners.md) (the agent contract text has one home) and follows the setup skill's split into a harness-neutral skill plus one reference file per harness.

Status: Accepted.

## Context

The plugin reaches fourteen harnesses, and each installs it, and so updates it, its own way: a marketplace command, a reinstall at a new tag, a version pinned in a config entry, a `git pull` in a clone, a button in the harness, or files copied by hand. Some steps have traps that only one harness has: Codex skips a plugin's hooks after an update until the user trusts them again, Hermes takes only a full commit SHA as a pinned ref, Antigravity deletes the stored address and key on uninstall.

Setup also writes text outside the plugin that nothing updates: the instruction block in the harness's always-loaded file, Cursor's user rule, hand-copied skills, Cline's hooks. When a release changes that text, the copies drift. Until now the only help was step 5 of the setup skill, which asked the agent to compare the block by eye and named each harness's update command in its setup file.

## Decision

1. A third skill, `hivemind-update`, owns updating. It is loaded on demand, so it costs no always-on text. Like the setup skill it is harness-neutral: it routes to `references/<harness>.md`, which holds that harness's check command, update steps and the copies setup wrote. The setup skill's step 5 and the setup files' Update sections move there.
2. Drift is measured against the newest `vX.Y.Z` tag of the repository, by content: the installed plugin's version and its skill and always-on files, and each copy outside it, are compared with the release's files. The instruction block's reference text is the block in step 4 of the release's setup skill, so it keeps one home.
3. The comparison is a shell script, `hivemind-drift.sh`, using only `git`, `diff`, `cmp`, `sed` and `awk`. It reads and never writes, and never runs anything from the fetched release. Its exit status (0, 1, 2) and line-per-finding output let any agent act on it. The agent makes the changes, with the user's approval per file, as setup does.
4. Hivemind may be re-hosted, self-hosted or mirrored into an offline network, so nothing assumes GitHub: the check uses only git, and takes its repository from, in order, `--repo`, the remote of the clone the installed plugin lives in (when that clone is the Hivemind repository), the first line of `~/.config/hivemind/plugin-source`, and only then the public URL. It prints which one it used. With no git server reachable at all, `--latest DIR` compares with a copy of the repository and touches no network. A mirror must carry the `vX.Y.Z` tags, or the check is given `--ref <branch>`. Credentials in a remote URL are never printed.

## Considered and not done

- **Checking for a new release at session start.** It would need the network in every session start hook, and a hook that fails slow delays every session. The check runs when the user asks.
- **Comparing versions only.** A matching version says nothing about the copies outside the plugin, which are what drifts unnoticed.
- **A version file in each copy.** It would add a fourteenth file to every version bump and still miss hand edits; comparing content catches both.
- **Having the script write the files.** The changes touch the user's own files outside any project; the setup rules already require showing each change and asking first, which an agent does better than a script.

## Consequences

- Each new harness needs an update file next to its setup file; a test checks that every block and skills folder a setup file writes is passed to the check in the update file.
- The check needs a reachable git server (any host), or a checkout passed with `--latest`.
- An install that is neither a clone nor covered by `plugin-source` falls back to the public URL; on a mirror-only network that fails with exit 2 and a message naming the source, and the skill has the agent ask for the mirror and save it.
- An install that follows a branch reports itself as newer than the release, which is not drift.
