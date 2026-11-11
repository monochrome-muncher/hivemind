# Hivemind update: KiloCode

Only for **KiloCode**. If you have not confirmed that this is your harness, go
back to "Which harness am I in?" in the hivemind-setup skill.

## Check

```sh
sh <skill>/scripts/hivemind-drift.sh --block ~/.config/kilo/AGENTS.md
```

The `plugin` entry in `kilo.jsonc` names the address and the ref; pass the
address as `--repo` when the
`Latest:` line names another source.

## Update

Change the ref in the `plugin` entry (`git:…/hivemind@v<version>`) to
the tag from the `Latest:` line, or to that tag's full commit SHA on a
machine that is often offline, then restart KiloCode. Check both
`~/.config/kilo/kilo.jsonc` and the project's `kilo.jsonc`. A tag doesn't
move, so changing it is the update; a branch entry updates on its own
about hourly. Keep the entry without a `#subpath`.

## Your copies

- The instruction block in `~/.config/kilo/AGENTS.md`.
