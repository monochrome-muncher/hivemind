# Hivemind update: Hermes

Only for **Hermes**. If you have not confirmed that this is your harness, go
back to "Which harness am I in?" in the hivemind-setup skill.

## Check

```sh
sh <skill>/scripts/hivemind-drift.sh --block ~/.hermes/SOUL.md --source-from ~/.hermes/plugins/.install-metadata.json
```

A named profile keeps its own `SOUL.md` in `~/.hermes/profiles/<name>/`:
check the one the profile loads. Skills copied into `~/.hermes/skills/`:
add `--skills ~/.hermes/skills`. The installed plugin is a copy, not a
clone, and `--source-from` reads the address it came from in Hermes's
install record, which holds the full Git URL even for an
`<owner>/hivemind/plugins/hivemind` install (checked with Hermes
v2026.9.24). If the `Latest:` line says `from the default` but the
plugin came from another address, ask the user for that Git URL and save
it as step 2 of the skill says.

## Update

`hermes plugins update hivemind`: it installs the plugin again from the
recorded address and prints the install scan; it ends with "Plugin
hivemind updated" and the old and new commits.

A plugin installed at a pinned ref needs `hermes plugins install <source>
--force --ref <commit-sha>` instead. The ref must be the full
40-character commit SHA; a tag or branch name is refused. Find the SHA of
the tag from the `Latest:` line with:

```sh
git ls-remote <repo> 'refs/tags/<tag>' 'refs/tags/<tag>^{}'
```

(use the `^{}` line if there is one). Off GitHub, the source ends in
`#plugins/hivemind`; without it nothing loads. The install scan gives
Hivemind a "caution" verdict and asks "Install anyway?" (without a
terminal it stops and says to use `--force`): ask the user before using
`--force`. A "dangerous" verdict cannot be overridden: stop and tell the
user. Start a new session.

## Your copies

- The instruction block in `SOUL.md`: required on a Hermes older than
  v2026.9.24 (0.21.5), harmless on a newer one.
- The skill folders in `~/.hermes/skills/`, if setup copied them there.
