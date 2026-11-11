# Hivemind update: Codex

Only for **Codex**. If you have not confirmed that this is your harness, go
back to "Which harness am I in?" in the hivemind-setup skill.

## Check

```sh
sh <skill>/scripts/hivemind-drift.sh --block ~/.codex/AGENTS.md --source-from ~/.codex/config.toml
```

`--source-from` reads the marketplace's address from the
`[marketplaces.hivemind]` table Codex writes there (checked with Codex
0.167.1, a Git-URL marketplace). Without the plugin, drop it and add
`--skills ~/.agents/skills`. If the `Latest:` line says `from the default` but the plugin came from
another address, ask the user for that Git URL and save it as step 2 of
the skill says.

## Update

`codex plugin marketplace upgrade hivemind`, then reinstall with `codex
plugin add hivemind@hivemind` (Codex runs a cached copy), check with
`codex plugin list --marketplace hivemind`, then start a new session.

**Then the user must trust the hooks again in `/hooks`.** Codex trusts a
plugin's files as they were, so an update that changes them lists the
hooks as modified and skips them until they are trusted again (checked
with Codex 0.166.0). Until then there is no `HIVEMIND:` line at session
start and no stop reminder.

Without the plugin, copy the new skill folders over the ones in
`~/.agents/skills/`; a hook in `~/.codex/hooks.json` that runs the copied
script needs trusting again too.

## Your copies

- The instruction block in `~/.codex/AGENTS.md`.
- Without the plugin: the skill folders in `~/.agents/skills/`.
