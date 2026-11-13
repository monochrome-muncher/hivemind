# Hivemind update: Pi

Only for **Pi**. If you have not confirmed that this is your harness, go
back to "Which harness am I in?" in the hivemind-setup skill.

## Check

```sh
sh <skill>/scripts/hivemind-drift.sh --block ~/.pi/agent/AGENTS.md
```

Without the package (skills copied into `~/.pi/agent/skills/`), add
`--skills ~/.pi/agent/skills`. A package installed from a Git URL is a
clone in `~/.pi/agent/git/<host>/<path>` (checked with Pi 1.5.2), and
one installed from a local path is your checkout, so the check compares
with its address. `pi list` shows the address, as does `packages` in
`~/.pi/agent/settings.json`. If the `Latest:` line says `from the
default` but the plugin came from another address, ask the user for that
Git URL and save it as step 2 of the skill says.

## Update

- Installed from a local path: `git pull` in that checkout (Pi loads it
  in place).
- Installed from git or npm: `pi update --extensions`. A pinned tag
  (`@v<version>`) stays put, so install again at the new tag (`pi install
  <address>@<tag>`, with the address from `pi list`): that replaces the
  old entry.

Then restart Pi.

## Your copies

- The instruction block in `~/.pi/agent/AGENTS.md`.
- Without the package: the skill folders in `~/.pi/agent/skills/`.
