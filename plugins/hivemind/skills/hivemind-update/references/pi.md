# Hivemind update: Pi

Only for **Pi**. If you have not confirmed that this is your harness, go
back to "Which harness am I in?" in the hivemind-setup skill.

## Check

```sh
sh <skill>/scripts/hivemind-drift.sh --block ~/.pi/agent/AGENTS.md
```

Without the package (skills copied into `~/.pi/agent/skills/`), add
`--skills ~/.pi/agent/skills`. Pass the address the package came from as
`--repo` when the
`Latest:` line names another source.

## Update

- Installed from a local path: `git pull` in that checkout (Pi loads it
  in place).
- Installed from git or npm: `pi update --extensions`. A pinned tag
  (`@v<version>`) stays put, so install again at the new tag.

Then restart Pi.

## Your copies

- The instruction block in `~/.pi/agent/AGENTS.md`.
- Without the package: the skill folders in `~/.pi/agent/skills/`.
