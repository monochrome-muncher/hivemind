# Hivemind update: Cline

Only for **Cline**. If you have not confirmed that this is your harness, go
back to "Which harness am I in?" in the hivemind-setup skill.

## Check

Cline has no package plugin: setup copied the skills and the hooks, and
these copies are what the check compares.

```sh
sh <skill>/scripts/hivemind-drift.sh --skills ~/.cline/skills \
  --copy ~/Documents/Cline/Hooks/UserPromptSubmit=cline/UserPromptSubmit \
  --copy ~/Documents/Cline/Hooks/TaskStart=cline/TaskStart \
  --copy ~/Documents/Cline/Hooks/PostToolUse=cline/PostToolUse
```

Nothing records where the copies came from: the check uses the saved
address in `~/.config/hivemind/plugin-source`, else the public one. If the `Latest:` line says `from the default` but the plugin came from
another address, ask the user for that Git URL and save it as step 2 of
the skill says.

On Windows (in Git Bash) compare the `.ps1` twins the same way
(`--copy ~/Documents/Cline/Hooks/UserPromptSubmit.ps1=cline/UserPromptSubmit.ps1`
and so on).

## Update

Updating is copying: run the check with `--keep DIR`, then, with
approval, copy `DIR/plugins/hivemind/cline/`'s hook scripts over the old
ones in `~/Documents/Cline/Hooks/` (keep them executable) and
`DIR/plugins/hivemind/skills/`'s folders over the old copies in
`~/.cline/skills/`, so that each `SKILL.md` keeps a `name` that matches its
folder. Delete `DIR`, then restart Cline. The MCP entry does not change
between releases.

## Your copies

- The three hook scripts in `~/Documents/Cline/Hooks/` (their `.ps1`
  twins on Windows).
- The skill folders in `~/.cline/skills/`.
- No instruction block is needed. If one in `~/.agents/AGENTS.md` or a
  project's `AGENTS.md` was written for another harness, that harness's
  update refreshes it.
