# Hivemind update: Pi

Only for **Pi**. If you have not confirmed that this is your harness, go
back to "Which harness am I in?" in the hivemind-setup skill.

## Check

```sh
sh <skill>/scripts/hivemind-drift.sh --block ~/.pi/agent/AGENTS.md
```

Without the package (skills copied into `~/.pi/agent/skills/`), add
`--skills ~/.pi/agent/skills`. A package installed from a local path is
your checkout, so the check compares with its address. Where Pi records
a Git URL install was not checked. If the `Latest:` line says `from the default` but the plugin came from
another address, ask the user for that Git URL and save it as step 2 of
the skill says.

## Update

- Installed from a local path: `git pull` in that checkout (Pi loads it
  in place).
- Installed from git or npm: `pi update --extensions`. A pinned tag
  (`@v<version>`) stays put, so install again at the new tag.

Then restart Pi.

## Your copies

- The instruction block in `~/.pi/agent/AGENTS.md`.
- Without the package: the skill folders in `~/.pi/agent/skills/`.
