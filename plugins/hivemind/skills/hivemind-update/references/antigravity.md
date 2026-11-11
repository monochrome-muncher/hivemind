# Hivemind update: Antigravity

Only for **Antigravity**. If you have not confirmed that this is your harness, go
back to "Which harness am I in?" in the hivemind-setup skill.

## Check

```sh
sh <skill>/scripts/hivemind-drift.sh --block ~/.gemini/AGENTS.md
```

The plugin is in `~/.gemini/config/plugins/hivemind` (the CLI, the App
and the IDE share it), and the check finds it from this skill's folder.
It compares the plugin's rule, `rules/AGENTS.md`, too.

## Update

The user runs the install command again with the tag from the `Latest:`
line:

```sh
agy plugin install https://github.com/<owner>/hivemind/tree/<tag>/plugins/antigravity
```

Keep the `/tree/<tag>/plugins/antigravity` part, and use the address the
plugin came from. Installing again replaces the plugin and keeps the
stored address and key. **Do not uninstall first**: uninstalling deletes
them, and the plugin then stays off until they are entered again. Start
a new session.

## Your copies

- None: the plugin's rule carries the instruction block. The check should
  report `none` for `~/.gemini/AGENTS.md`. If an older setup put a block
  there, remove it, with approval.
