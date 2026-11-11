# Hivemind update: Hermes

Only for **Hermes**. If you have not confirmed that this is your harness, go
back to "Which harness am I in?" in the hivemind-setup skill.

## Check

```sh
sh <skill>/scripts/hivemind-drift.sh --block ~/.hermes/SOUL.md
```

A named profile keeps its own `SOUL.md` in `~/.hermes/profiles/<name>/`:
check the one the profile loads. Skills copied into `~/.hermes/skills/`:
add `--skills ~/.hermes/skills`. The installed plugin is a clone, so the
check compares with the address it was cloned from.

## Update

`hermes plugins update hivemind` (a `git pull` of the installed plugin).

A plugin installed at a pinned ref needs `hermes plugins install <source>
--force --ref <commit-sha>` instead. The ref must be the full
40-character commit SHA; a tag or branch name is refused. Find the SHA of
the tag from the `Latest:` line with:

```sh
git ls-remote <repo> 'refs/tags/<tag>' 'refs/tags/<tag>^{}'
```

(use the `^{}` line if there is one). Off GitHub, the source ends in
`#plugins/hivemind`; without it nothing loads. The install scan asks
"Install anyway?": ask the user before using `--force`. Start a new
session.

## Your copies

- The instruction block in `SOUL.md`: required on a Hermes older than
  v2026.9.24 (0.21.5), harmless on a newer one.
- The skill folders in `~/.hermes/skills/`, if setup copied them there.
