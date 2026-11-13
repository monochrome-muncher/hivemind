# Hivemind update: Oh My Pi

Only for **Oh My Pi**. If you have not confirmed that this is your harness, go
back to "Which harness am I in?" in the hivemind-setup skill.

## Check

```sh
sh <skill>/scripts/hivemind-drift.sh --block ~/.omp/agent/AGENTS.md --source-from ~/.omp/marketplaces.json
```

The plugin is a copy in `~/.omp/plugins/cache/plugins/`, not a clone, so
`--source-from` reads the marketplace's address from the `sourceUri` that
Oh My Pi recorded (checked with Oh My Pi 18.18.8). A marketplace added as
`<owner>/hivemind` is on GitHub, and that record holds no URL. If the
`Latest:` line says `from the default` but the plugin came from another
address, ask the user for that Git URL and save it as step 2 of the skill
says.

## Update

`omp plugin marketplace update hivemind`, then `omp plugin upgrade` (or
`omp plugin install hivemind@hivemind --force`). The new version goes in
a new folder of the plugin cache. Then restart Oh My Pi.

## Your copies

- The instruction block in `~/.omp/agent/AGENTS.md`.
