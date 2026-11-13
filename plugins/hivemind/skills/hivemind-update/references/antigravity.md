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

Antigravity copies the plugin there and keeps no record of where it came
from (checked with 1.5.5, for a GitHub link and for a local folder). So
the check compares with `~/.config/hivemind/plugin-source` if that file
exists, and otherwise says `from the default`. If the user installed
from anywhere but the public GitHub repository, or you are not sure, ask
for the Git URL and save it as step 2 of the skill says.

## Update

**From GitHub**, the user runs the install command again with the tag
from the `Latest:` line:

```sh
agy plugin install https://github.com/<owner>/hivemind/tree/<tag>/plugins/antigravity
```

Keep the `/tree/<tag>/plugins/antigravity` part.

**From any other Git server**, `agy plugin install` refuses the link
(it only takes github.com links), so the user installs from a folder
instead. Antigravity copies the folder, so the clone can go afterwards:

```sh
git clone --depth 1 --branch <tag> <address> /tmp/hivemind-<tag>
agy plugin install /tmp/hivemind-<tag>/plugins/antigravity
rm -rf /tmp/hivemind-<tag>
```

Either way, installing again replaces the plugin and keeps the stored
address and key, and the plugin stays on. **Do not uninstall first**:
uninstalling deletes them, and the plugin then stays off until they are
entered again in `/plugin`. Start a new session.

## Your copies

- None: the plugin's rule carries the instruction block. The check
  should report `none` or `no such file` for `~/.gemini/AGENTS.md`. If an
  older setup put a block there, remove it, with approval.
