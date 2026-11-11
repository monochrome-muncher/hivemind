# Hivemind update: Junie

Only for **Junie**. If you have not confirmed that this is your harness, go
back to "Which harness am I in?" in the hivemind-setup skill.

## Check

```sh
sh <skill>/scripts/hivemind-drift.sh --source-from ~/.junie/extensions/marketplaces.json
```

The plugin is a copy in `~/.junie/extensions/<marketplace>/hivemind/`,
and the check finds it from this skill's folder. It compares the plugin's
guidelines, `guidelines/hivemind.md`, too. `--source-from` reads the
marketplace's address from that file (checked with Junie 26.9.7; it
skips JetBrains' own marketplace, whose address has no "hivemind"). If the
project has a block in `.junie/AGENTS.md`, add `--block .junie/AGENTS.md`.
If the `Latest:` line says `from the default` but the plugin came from
another address, ask the user for that Git URL and save it as step 2 of
the skill says.

## Update

A Git extension updates only when the user picks Update on hivemind in
the Installed tab of `/extensions` (it fetches the marketplace itself);
an extension from a local folder refreshes when Junie starts. Junie notes
that "MCP settings will be reset to extension defaults": that is the
extension's own MCP entry, and `~/.junie/mcp/mcp.json` stays as it was.
Then start a new session.

## Your copies

- None needed: the plugin's guidelines carry the stay-aware line. A
  block in a project's `.junie/AGENTS.md` is optional.
- `~/.junie/mcp/mcp.json` does not change between releases. It holds
  the key: leave it alone.
