# Hivemind update: OpenCode

Only for **OpenCode**. If you have not confirmed that this is your harness, go
back to "Which harness am I in?" in the hivemind-setup skill.

## Check

```sh
sh <skill>/scripts/hivemind-drift.sh --block ~/.config/opencode/AGENTS.md --source-from ~/.config/opencode/opencode.json
```

OpenCode also reads `~/.claude/CLAUDE.md` as a fallback: if that holds a
block, add `--block ~/.claude/CLAUDE.md`. The plugin is a package copy
under `~/.cache/opencode/packages/`, not a clone, so `--source-from` reads
the address from the `plugin` entry
(`git+https://<git-server>/<owner>/hivemind.git#v<version>`), dropping the
`git+` prefix and the tag (checked with OpenCode 1.19.5). If the entry is
in the project's `opencode.json`, point `--source-from` at that file.

## Update

Change the tag in the `plugin` entry
(`git+https://…/hivemind.git#v<version>`, under `plugin` on v1 and
migrated to `plugins` on v2) to the tag from the `Latest:` line, then
restart OpenCode, which fetches the new tag into a new folder. Check both
`~/.config/opencode/opencode.json` and the project's `opencode.json`. An
unpinned entry never updates: pin it, or delete OpenCode's cached copy
under `~/.cache/opencode/` and restart.

## Your copies

- The instruction block in `~/.config/opencode/AGENTS.md` (and in
  `~/.claude/CLAUDE.md`, if one is there).
