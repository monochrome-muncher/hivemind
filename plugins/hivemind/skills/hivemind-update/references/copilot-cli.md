# Hivemind update: GitHub Copilot CLI

Only for **GitHub Copilot CLI**. If you have not confirmed that this is your harness, go
back to "Which harness am I in?" in the hivemind-setup skill.

## Check

```sh
sh <skill>/scripts/hivemind-drift.sh --block ~/.copilot/copilot-instructions.md --source-from ~/.copilot/settings.json
```

`--source-from` reads the marketplace's address from
`extraKnownMarketplaces` there (checked with Copilot CLI 1.0.109, a
Git-URL marketplace; `~/.copilot/config.json` records it too). A
marketplace added from a local folder has no address: pass `--repo` or
save one. Without the plugin, drop `--source-from` and add
`--skills ~/.copilot/skills` (or `--skills ~/.agents/skills`, wherever
setup copied them).

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
