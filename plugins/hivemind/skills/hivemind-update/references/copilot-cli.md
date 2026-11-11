# Hivemind update: GitHub Copilot CLI

Only for **GitHub Copilot CLI**. If you have not confirmed that this is your harness, go
back to "Which harness am I in?" in the hivemind-setup skill.

## Check

```sh
sh <skill>/scripts/hivemind-drift.sh --block ~/.copilot/copilot-instructions.md
```

Without the plugin, add `--skills ~/.copilot/skills` (or
`--skills ~/.agents/skills`, wherever setup copied them). Pass the
marketplace's address as `--repo` when it is not the default.

## Update

`copilot plugin marketplace update hivemind`, then `copilot plugin update
hivemind@hivemind`, then start a new session. A marketplace added from a
local path is read in place, so there `git pull` in that checkout is the
update.

Without the plugin, copy the new skill folders over the old ones. The
hook in `~/.copilot/hooks/hivemind.json` runs the script inside the
copied `hivemind-setup` folder, so it is refreshed with it.

## Your copies

- The instruction block in `~/.copilot/copilot-instructions.md`, which
  Copilot sends with every prompt: the durable layer here, so keep it
  current.
- Without the plugin: the copied skill folders.
