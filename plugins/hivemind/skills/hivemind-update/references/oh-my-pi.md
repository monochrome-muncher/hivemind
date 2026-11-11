# Hivemind update: Oh My Pi

Only for **Oh My Pi**. If you have not confirmed that this is your harness, go
back to "Which harness am I in?" in the hivemind-setup skill.

## Check

```sh
sh <skill>/scripts/hivemind-drift.sh --block ~/.omp/agent/AGENTS.md
```

Where Oh My Pi records the marketplace's address was not checked.
If the `Latest:` line says `from the default` but the plugin came from
another address, ask the user for that Git URL and save it as step 2 of
the skill says.

## Update

`omp plugin marketplace update hivemind`, then `omp plugin upgrade` (or
`omp plugin install hivemind@hivemind --force`). Then restart Oh My Pi.

## Your copies

- The instruction block in `~/.omp/agent/AGENTS.md`.
