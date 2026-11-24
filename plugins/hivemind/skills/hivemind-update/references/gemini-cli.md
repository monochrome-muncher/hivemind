# Hivemind update: Gemini CLI

Only for **Gemini CLI**. If you have not confirmed that this is your harness, go
back to "Which harness am I in?" in the hivemind-setup skill.

## Check

```sh
sh <skill>/scripts/hivemind-drift.sh --block ~/.gemini/GEMINI.md
```

An extension installed from a Git URL is a clone in
`~/.gemini/extensions/hivemind` (checked with Gemini CLI 0.68.0), and a
linked one is your checkout, so the check compares with its address.
Without the extension, add
`--skills ~/.gemini/skills` (or `--skills ~/.agents/skills`, wherever
setup copied them).

## Update

With the extension: `gemini extensions update hivemind`. An install
pinned with `--ref` does not move: `gemini extensions uninstall hivemind`,
then install again with the new ref. A linked extension is updated with
`git pull` in its folder. Start a new session.

Gemini CLI 0.70.0 to 0.71.1 cannot update an extension installed from a
Git URL: the update prints `Failed to check for updates ... Use of
"GIT_CONFIG_SYSTEM" is not permitted without enabling
allowUnsafeConfigPaths` and then says the extension is already up to
date, which is wrong (a Gemini CLI bug: google-gemini/gemini-cli#30416).
The installed version keeps working. To update anyway, the user runs
`gemini extensions uninstall hivemind`, clones the repository and runs
`gemini extensions link` on the clone (hivemind-setup, Gemini CLI).

Without the extension, copy the new skill folders over the old ones. The
hooks in `~/.gemini/settings.json` run the scripts inside the copied
`hivemind-setup` folder, so they are refreshed with it.

## Your copies

- The instruction block in `~/.gemini/GEMINI.md`, the durable layer here:
  nothing re-injects the reminder after the context is compressed.
- Without the extension: the copied skill folders.
