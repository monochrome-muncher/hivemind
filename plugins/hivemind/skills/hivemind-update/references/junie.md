# Hivemind update: Junie

Only for **Junie**. If you have not confirmed that this is your harness, go
back to "Which harness am I in?" in the hivemind-setup skill.

## Check

```sh
sh <skill>/scripts/hivemind-drift.sh
```

The plugin is in `~/.junie/extensions/<marketplace>/hivemind/`, and the
check finds it from this skill's folder. It compares the plugin's
guidelines, `guidelines/hivemind.md`, too. If the project has a block in
`.junie/AGENTS.md`, add `--block .junie/AGENTS.md`. Where Junie records
the marketplace's address was not checked. If the `Latest:` line says `from the default` but the plugin came from
another address, ask the user for that Git URL and save it as step 2 of
the skill says.

## Update

A Git extension updates only when the user picks Update on hivemind in
the Installed tab of `/extensions`; an extension from a local folder
refreshes when Junie starts. Then start a new session.

## Your copies

- None needed: the plugin's guidelines carry the stay-aware line. A
  block in a project's `.junie/AGENTS.md` is optional.
- `~/.junie/mcp/mcp.json` does not change between releases. It holds
  the key: leave it alone.
