# Hivemind update: Cursor

Only for **Cursor**. If you have not confirmed that this is your harness, go
back to "Which harness am I in?" in the hivemind-setup skill.

## Check

```sh
sh <skill>/scripts/hivemind-drift.sh --block ~/.cursor/rules/hivemind.mdc
```

The rule holds the instruction block below its frontmatter; the check
compares only the block. `git -C ~/.cursor/plugins/local/.hivemind-src
remote get-url origin` shows the address the plugin came from; pass it
as `--repo` when it is not the default. Without the plugin, add
`--skills ~/.cursor/skills`.

## Update

With the local folder (the usual route): `git -C
~/.cursor/plugins/local/.hivemind-src pull`, then restart Cursor or run
"Developer: Reload Window", and start a new conversation. That clone
follows `main`, so the check may then say it is newer than the release:
that is fine. To stay on releases instead, check out the tag from the
`Latest:` line (`git -C ~/.cursor/plugins/local/.hivemind-src fetch
--tags` and `checkout <tag>`).

With `~/.cursor/plugins/local-marketplaces.json` (local-model mode),
Cursor fetches the `gitRef` again at each start, so a restart is the
update; set `gitRef` to a tag to pin a release.

Without the plugin, copy the new skill folders over the ones in
`~/.cursor/skills/`.

## Your copies

- The user rule `~/.cursor/rules/hivemind.mdc`: replace only the block,
  and keep the frontmatter (`alwaysApply: true`). It is the layer that
  survives a compaction here.
- The user `stop` hook in `~/.cursor/hooks.json`. Check that its command
  still names
  `$HOME/.cursor/plugins/local/hivemind/skills/hivemind-setup/scripts/hivemind-stop.sh`
  and that the file exists (`test -f`); if the plugin moved, point it at
  the new place, with approval.
- Without the plugin: the skill folders in `~/.cursor/skills/`.
