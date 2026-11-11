# Hivemind update: Gemini CLI

Only for **Gemini CLI**. If you have not confirmed that this is your harness, go
back to "Which harness am I in?" in the hivemind-setup skill.

## Check

```sh
sh <skill>/scripts/hivemind-drift.sh --block ~/.gemini/GEMINI.md
```

`gemini extensions list` shows the extension's source and ref; pass the
source as `--repo` when the
`Latest:` line names another source. Without the extension, add
`--skills ~/.gemini/skills` (or `--skills ~/.agents/skills`, wherever
setup copied them).

## Update

With the extension: `gemini extensions update hivemind`. An install
pinned with `--ref` does not move: `gemini extensions uninstall hivemind`,
then install again with the new ref. Start a new session.

Without the extension, copy the new skill folders over the old ones. The
hooks in `~/.gemini/settings.json` run the scripts inside the copied
`hivemind-setup` folder, so they are refreshed with it.

## Your copies

- The instruction block in `~/.gemini/GEMINI.md`, the durable layer here:
  nothing re-injects the reminder after the context is compressed.
- Without the extension: the copied skill folders.
