# Hivemind update: DeepSeek Harness

Only for **DeepSeek Harness**. If you have not confirmed that this is your harness, go
back to "Which harness am I in?" in the hivemind-setup skill.

## Check

```sh
sh <skill>/scripts/hivemind-drift.sh --block ~/.dsh/AGENTS.md
```

Without the bundle (skills copied into `~/.agents/skills/`), add
`--skills ~/.agents/skills`. Pass the address the bundle came from as
`--repo` when it is not the default.

## Update

The bundle belongs to one profile: update it in each profile the user
installed it in.

- Installed from the Git URL or npm: `dsh plugin --profile <name> update
  hivemind-agent-plugin`. A Git URL pinned with `#v<version>` stays put,
  so run `add` again with the new tag.
- Installed from a checkout: `git pull` in the checkout (the profile
  links it).
- Installed before 2.0.1, the package is named `hivemind-dsh-plugin`:
  remove it under that name (`dsh plugin --profile <name> remove
  hivemind-dsh-plugin`) and add the plugin again.

Then restart `dsh`.

## Your copies

- The instruction block in `~/.dsh/AGENTS.md`.
- Without the bundle: the skill folders in `~/.agents/skills/`.
