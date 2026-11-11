# Hivemind update: Claude Code

Only for **Claude Code**. If you have not confirmed that this is your harness, go
back to "Which harness am I in?" in the hivemind-setup skill.

## Check

```sh
sh <skill>/scripts/hivemind-drift.sh --block ~/.claude/CLAUDE.md
```

Without the plugin (skills copied into `~/.claude/skills/`), add
`--skills ~/.claude/skills`. `claude plugin marketplace list` shows the
address the plugin came from; pass it as `--repo` when it is not the
default.

In a cloud session (setup's "Remote and cloud sessions") the block is in
the repository's `CLAUDE.md` or `AGENTS.md`: pass that file to `--block`
instead, and there is no plugin to update.

## Update

`claude plugin marketplace update hivemind`, then `claude plugin update
hivemind@hivemind`, then restart. Updating the marketplace alone only
refreshes the catalog. The new version lives in a new folder of the
plugin cache, so run the check again after the restart.

Without the plugin, copy the new skill folders over the ones in
`~/.claude/skills/` (step 4 of the skill). A `SessionStart` hook in
`~/.claude/settings.json` runs the script inside the copied
`hivemind-setup` folder, so it is refreshed with it.

## Your copies

- The instruction block in `~/.claude/CLAUDE.md`.
- Without the plugin: the skill folders in `~/.claude/skills/`.
