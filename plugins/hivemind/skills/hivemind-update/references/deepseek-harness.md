# Hivemind update: DeepSeek Harness

Only for **DeepSeek Harness**. If you have not confirmed that this is your harness, go
back to "Which harness am I in?" in the hivemind-setup skill.

## Check

```sh
sh <skill>/scripts/hivemind-drift.sh --block ~/.dsh/AGENTS.md --source-from ~/.dsh/profiles/<profile>/package.json
```

`<profile>` is the profile this session runs (the path of this skill's
folder names it). A bundle installed from a Git URL is a package copy in
`~/.dsh/profiles/<profile>/node_modules/`, not a clone, and
`--source-from` reads its address from the profile's `package.json`
(checked with DSH 0.2.2-rc.1). A bundle installed from a checkout is
linked to it, so the check compares with the checkout's address; drop
`--source-from` there. Without the bundle (skills copied into
`~/.agents/skills/`), drop it too and add `--skills ~/.agents/skills`.
If the `Latest:` line says `from the default` but the plugin came from
another address, ask the user for that Git URL and save it as step 2 of
the skill says.

## Update

The bundle belongs to one profile: update it in each profile the user
installed it in.

- Installed from the Git URL or npm: `dsh plugin --profile <name> update
  hivemind-agent-plugin`. A Git URL pinned with `#v<version>` stays put,
  so run `add` again with the new tag (`dsh plugin --profile <name> add
  git+<address>#<tag>`): that replaces the pin.
- Installed from a checkout: `git pull` in the checkout (the profile
  links it).
- Installed before 2.0.1, the package is named `hivemind-dsh-plugin`:
  remove it under that name (`dsh plugin --profile <name> remove
  hivemind-dsh-plugin`) and add the plugin again.

Then restart `dsh`.

## Your copies

- The instruction block in `~/.dsh/AGENTS.md`.
- Without the bundle: the skill folders in `~/.agents/skills/`.
