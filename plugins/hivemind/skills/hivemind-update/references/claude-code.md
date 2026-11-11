# Hivemind update: Claude Code

Only for **Claude Code**. If you have not confirmed that this is your harness, go
back to "Which harness am I in?" in the hivemind-setup skill.

## Check

```sh
sh <skill>/scripts/hivemind-drift.sh --block ~/.claude/CLAUDE.md
```

Without the plugin (skills copied into `~/.claude/skills/`), add
`--skills ~/.claude/skills`. A marketplace added from a Git URL, on
GitHub, GitLab or any other host, is a clone in
`~/.claude/plugins/marketplaces/hivemind`, and the check compares with
its address. `claude plugin marketplace list` shows that address too; a
marketplace added from a local folder has none: pass `--repo` or save
one as step 2 of the skill says. (Checked with Claude Code 2.1.322: the
same address is in `~/.claude/plugins/known_marketplaces.json`, which
`--source-from` also reads.)

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
