# Hivemind update: OpenCode

Only for **OpenCode**. If you have not confirmed that this is your harness, go
back to "Which harness am I in?" in the hivemind-setup skill.

## Check

```sh
sh <skill>/scripts/hivemind-drift.sh --block ~/.config/opencode/AGENTS.md
```

OpenCode also reads `~/.claude/CLAUDE.md` as a fallback: if that holds a
block, add `--block ~/.claude/CLAUDE.md`. The `plugin` entry in
`opencode.json` names the address and the tag; pass the address as
`--repo` when the
`Latest:` line names another source.

## Update

Change the tag in the `plugin` entry
(`git+https://…/hivemind.git#v<version>`, under `plugin` on v1 and
migrated to `plugins` on v2) to the tag from the `Latest:` line, then
restart OpenCode. Check both `~/.config/opencode/opencode.json` and the
project's `opencode.json`. An unpinned entry never updates: pin it, or
delete OpenCode's cached copy under `~/.cache/opencode/` and restart.

## Your copies

- The instruction block in `~/.config/opencode/AGENTS.md` (and in
  `~/.claude/CLAUDE.md`, if one is there).
